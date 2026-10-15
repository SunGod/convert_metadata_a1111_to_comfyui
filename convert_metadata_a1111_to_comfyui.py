#!/usr/bin/env python3
"""Bulk A1111 PNG -> native ComfyUI workflow/prompt metadata (version 1.01).

Python 3.9+, standard library only. Modifies the original PNG file object IN PLACE,
without os.replace/rename, preserving Windows Date Created AND Date Modified
(and Last Accessed) and file identity. Windows Explorer's generic Date column
may use Date Modified when image capture-time metadata is absent.
A separately staged PNG and independent byte-copy rollback protect ordinary
write failures. --backup keeps a persistent image.bak1.png copy. Original A1111
and other PNG metadata plus IDAT bytes remain unchanged.
In-place writes are not crash-atomic: use --backup on valuable collections.

Produces a best-effort editable node graph, not the original historical graph.
Never substitutes unknown samplers or hires upscalers. When an A1111 scheduler
cannot be mapped, uses ComfyUI 'normal' as an editable fallback; the original
A1111 value remains untouched. Unsupported Hires.fix stages are omitted and
explicitly marked PARTIAL in the graph.
Image-to-image and inpainting need the missing original source image/mask before
execution. Actual ComfyUI runtime execution depends on installed model files,
node versions, and resources. --verify checks embedded metadata and structure,
NOT whether ComfyUI can execute the reconstructed workflow.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
from dataclasses import dataclass
import json
import os
import math
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
from uuid import uuid4
import zlib
from typing import Any

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

PNG_SIG = b"\x89PNG\r\n\x1a\n"
TEXT_TYPES = {b"tEXt", b"iTXt", b"zTXt"}
# Owned ComfyUI fields plus the obsolete custom field removed on overwrite.
REPLACE_KEYS = {"workflow", "prompt", "a1111_import"}
MAX_TEXT = 16 * 1024 * 1024
LORA_REGEX = re.compile(r"<lora:([^<>:]+):(-?\d+(?:\.\d+)?)(?::(-?\d+(?:\.\d+)?))?>", re.I)
SETTING_KEY = re.compile(r"^\s*([^,:\n]{1,100}):\s*(.*)$", re.S)
SETTINGS_START = re.compile(r"(?m)^Steps:\s*\d+\s*(?:,|$)")
# Recognizable PNG backups must not be reprocessed on subsequent directory scans.
BACKUP_NAME = re.compile(r"\.bak[1-9]\d*\.png$", re.I)


@dataclass(frozen=True)
class OriginalTimes:
    atime_ns: int
    mtime_ns: int
    # Exact Windows FILETIME values in 100ns increments since 1601.
    windows_filetimes: tuple[int, int, int] | None = None


def _windows_filetime(path: Path, set_values: tuple[int, int, int] | None = None):
    """Read/write (creation, last-access, last-write) with Win32, no pywin32."""
    if os.name != "nt":
        raise RuntimeError("Windows FILETIME requested on non-Windows system")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    get = kernel.GetFileTime
    get.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    get.restype = wintypes.BOOL
    put = kernel.SetFileTime
    put.argtypes = get.argtypes
    put.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    access = 0x100 if set_values is not None else 0x80  # FILE_WRITE/READ_ATTRIBUTES
    handle = create(str(path), access, 0x1 | 0x2 | 0x4, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    def to_int(ft):
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    def from_int(value):
        return wintypes.FILETIME(value & 0xffffffff, value >> 32)

    try:
        if set_values is not None:
            c, a, m = (from_int(t) for t in set_values)
            if not put(handle, ctypes.byref(c), ctypes.byref(a), ctypes.byref(m)):
                raise ctypes.WinError(ctypes.get_last_error())
        c, a, m = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not get(handle, ctypes.byref(c), ctypes.byref(a), ctypes.byref(m)):
            raise ctypes.WinError(ctypes.get_last_error())
        return to_int(c), to_int(a), to_int(m)
    finally:
        close(handle)


def capture_times(path: Path) -> OriginalTimes:
    stat = path.stat()
    win = _windows_filetime(path) if os.name == "nt" else None
    return OriginalTimes(stat.st_atime_ns, stat.st_mtime_ns, win)


def restore_times(path: Path, times: OriginalTimes) -> None:
    if times.windows_filetimes is not None:
        _windows_filetime(path, times.windows_filetimes)
    else:
        os.utime(path, ns=(times.atime_ns, times.mtime_ns))


def assert_times(path: Path, times: OriginalTimes) -> None:
    if times.windows_filetimes is not None:
        actual = _windows_filetime(path)
        if actual != times.windows_filetimes:
            raise OSError(f"Windows creation/access/modification FILETIMEs changed: "
                          f"expected {times.windows_filetimes}, found {actual}")
    else:
        stat = path.stat()
        if (stat.st_atime_ns, stat.st_mtime_ns) != (times.atime_ns, times.mtime_ns):
            raise OSError("access/modification times were not preserved")


class PngError(ValueError):
    pass


def decompress_limited(payload: bytes) -> bytes:
    d = zlib.decompressobj()
    result = d.decompress(payload, MAX_TEXT + 1)
    if len(result) > MAX_TEXT or d.unconsumed_tail:
        raise PngError("oversized compressed PNG text")
    result += d.flush(MAX_TEXT + 1 - len(result))
    if len(result) > MAX_TEXT:
        raise PngError("oversized PNG text")
    return result


def chunk_key(ctype: bytes, data: bytes) -> str | None:
    if ctype not in TEXT_TYPES:
        return None
    raw = data.split(b"\0", 1)[0]
    try:
        return raw.decode("latin-1")
    except UnicodeDecodeError:
        return None


def text_from_chunk(ctype: bytes, data: bytes) -> tuple[str, str] | None:
    key = chunk_key(ctype, data)
    if key is None:
        return None
    try:
        _, rest = data.split(b"\0", 1)
        if ctype == b"tEXt":
            value = rest.decode("latin-1")
        elif ctype == b"zTXt":
            if not rest or rest[0] != 0:
                return None
            value = decompress_limited(rest[1:]).decode("latin-1")
        else:  # iTXt: compression flag, method, language\0, translated keyword\0, UTF8 text
            flag, method = rest[:2]
            fields = rest[2:].split(b"\0", 2)
            if len(fields) != 3:
                return None
            value_bytes = fields[2]
            if flag == 1:
                if method != 0:
                    return None
                value_bytes = decompress_limited(value_bytes)
            value = value_bytes.decode("utf-8")
    except (ValueError, UnicodeError, zlib.error, IndexError):
        return None
    return (key, value)


def read_png_metadata(path: Path) -> tuple[int, int, dict[str, str]]:
    """Read only PNG structural/text chunks; skips compressed pixel bytes."""
    entries: dict[str, str] = {}
    width = height = 0
    with path.open("rb") as f:
        if f.read(8) != PNG_SIG:
            raise PngError("not a PNG")
        found_end = False
        while True:
            head = f.read(8)
            if len(head) != 8:
                raise PngError("truncated PNG or missing IEND")
            size, kind = struct.unpack(">I4s", head)
            if kind == b"IHDR":
                if size < 13:
                    raise PngError("invalid IHDR")
                data = f.read(size)
                if len(data) != size:
                    raise PngError("truncated IHDR")
                width, height = struct.unpack(">II", data[:8])
                f.seek(4, 1)
            elif kind in TEXT_TYPES:
                if size > MAX_TEXT:
                    raise PngError("oversized text chunk")
                data = f.read(size)
                crc = f.read(4)
                if len(data) != size or len(crc) != 4:
                    raise PngError("truncated text chunk")
                expected = zlib.crc32(data, zlib.crc32(kind)) & 0xffffffff
                if struct.unpack(">I", crc)[0] != expected:
                    raise PngError("bad PNG text chunk CRC")
                parsed = text_from_chunk(kind, data)
                if parsed:
                    entries[parsed[0]] = parsed[1]
            else:
                f.seek(size, 1)
                if len(f.read(4)) != 4:
                    raise PngError("truncated PNG chunk")
            if kind == b"IEND":
                found_end = True
                break
        if not found_end or not width or not height:
            raise PngError("missing IHDR or IEND")
    return width, height, entries


def split_settings(line: str) -> list[tuple[str, str]]:
    """Split top-level comma-delimited fields, respecting JSON, quotes and brackets."""
    tokens = []
    buf = []
    quote = None
    escape = False
    brackets: list[str] = []
    closing = {"{": "}", "[": "]", "(": ")"}
    for ch in line:
        if quote:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch in closing:
            brackets.append(closing[ch])
        elif brackets and ch == brackets[-1]:
            brackets.pop()
        elif ch == "," and not brackets:
            tokens.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    tokens.append("".join(buf).strip())
    pairs = []
    for token in tokens:
        match = SETTING_KEY.match(token)
        if match:
            pairs.append((match.group(1).strip(), match.group(2).strip()))
        elif pairs:
            key, value = pairs[-1]
            pairs[-1] = (key, value + ", " + token)
    return pairs


def normalize_infotext_for_parsing(raw: str) -> str:
    """Handle single-line infotext while leaving genuine multiline prompts alone.

    A1111 normally writes 'Steps:' at the start of the last line. Certain
    exporters join all three sections onto a single line. Split only the
    known separator boundaries, never the original PNG parameters value.
    Inspired by the public Civitai generation-metadata parser (MIT).
    """
    if SETTINGS_START.search(raw):
        return raw
    result = re.sub(r"[ \t]*[,\.][ \t,\.]{0,6}(?=Negative prompt:)", "\n", raw, flags=re.I)
    result = re.sub(r"[ \t]*[,\.][ \t,\.]{0,6}(?=Steps:\s*\d+)", "\n", result, flags=re.I)
    return result


def parse_a1111(raw: str) -> tuple[str, str, list[tuple[str, str]], dict[str, str]]:
    normalized = normalize_infotext_for_parsing(raw)
    matches = list(SETTINGS_START.finditer(normalized))
    if not matches:
        raise ValueError("A1111 metadata lacks a recognizable 'Steps:' settings line")
    start = matches[-1].start()
    prompt_block = normalized[:start].rstrip("\r\n")
    settings_text = normalized[start:].strip()
    neg = re.search(r"(?mi)^Negative prompt:\s*", prompt_block)
    if neg:
        positive = prompt_block[:neg.start()].rstrip("\r\n")
        negative = prompt_block[neg.end():].strip()
    else:
        positive = prompt_block.strip()
        negative = ""
    pairs = split_settings(settings_text)
    fields = {k: v for k, v in pairs}
    if not fields or not field(fields, "Steps"):
        raise ValueError("could not parse A1111 Steps settings")
    return positive, negative, pairs, fields


def addnet_loras(fields: dict[str, str], warnings: list[str]) -> list[dict[str, Any]]:
    """Recognize Additional Networks LoRAs without guessing incomplete weights.

    AddNet Weight A is the model weight; Weight B is CLIP. The extension
    stores model names as 'name(short_hash)' in some versions.
    """
    if field(fields, "AddNet Enabled").casefold() not in ("true", "1", "yes"):
        return []
    indices = sorted({int(m.group(1)) for key in fields
                      if (m := re.fullmatch(r"AddNet Model (\d+)", key, re.I))})
    result: list[dict[str, Any]] = []
    for i in indices:
        module = field(fields, f"AddNet Module {i}").strip()
        raw_name = strip_surrounding_quotes(field(fields, f"AddNet Model {i}").strip())
        if not raw_name or raw_name.casefold() in ("none", "no model"):
            continue
        if module.casefold() != "lora":
            warnings.append(f"AddNet slot {i}: unsupported module {module!r}; preserved only in parameters")
            continue
        match = re.fullmatch(r"(.*?)\(([0-9a-fA-F]{8,64})\)", raw_name)
        name = match.group(1).strip() if match else raw_name
        # An absent B strength is NOT necessarily the same as A. Do not invent it.
        a_raw = field(fields, f"AddNet Weight A {i}", field(fields, f"AddNet Weight {i}")).strip()
        b_raw = field(fields, f"AddNet Weight B {i}").strip()
        try:
            a = float(a_raw)
            b = float(b_raw)
            if not (math.isfinite(a) and math.isfinite(b)):
                raise ValueError("non-finite weights")
        except ValueError:
            warnings.append(f"AddNet slot {i}: missing/invalid A or B weight; preserved only in parameters")
            continue
        if not name:
            continue
        result.append({"name": name, "strength_model": a, "strength_clip": b,
                       "tag": f"Additional Networks slot {i}", "source": "AddNet"})
        if match:
            warnings.append(f"AddNet slot {i} identifies {name!r} by hash {match.group(2)}; "
                            "the filename still needs verifying in ComfyUI")
    return result


def field(fields: dict[str, str], name: str, default: str = "") -> str:
    for k, v in fields.items():
        if k.casefold() == name.casefold():
            return v
    return default


def as_number(value: str, fallback: float, kind: type = float) -> Any:
    try:
        number = kind(value.strip())
        return number if math.isfinite(number) and abs(number) < 2**53 else fallback
    except (ValueError, OverflowError, AttributeError):
        return fallback


def strip_surrounding_quotes(text: str) -> str:
    """A1111 quotes metadata values containing separators, not prompt content."""
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return text


def extension(name: str, default: str) -> str:
    if not name:
        return name
    return name if Path(name).suffix.lower() in (".safetensors", ".ckpt", ".pt", ".pth") else name + default


SAMPLERS = {
    "euler": "euler", "euler a": "euler_ancestral", "euler ancestral": "euler_ancestral",
    "euler cfg++": "euler_cfg_pp", "heun": "heun", "heunpp2": "heunpp2",
    "lms": "lms", "dpm2": "dpm_2", "dpm2 a": "dpm_2_ancestral",
    "dpm++ 2s a": "dpmpp_2s_ancestral", "dpm++ sde": "dpmpp_sde",
    "dpm++ 2m": "dpmpp_2m", "dpm++ 2m sde": "dpmpp_2m_sde",
    "dpm++ 2m sde heun": "dpmpp_2m_sde_heun", "dpm++ 3m sde": "dpmpp_3m_sde",
    "dpm++ 2m cfg++": "dpmpp_2m_cfg_pp", "dpm fast": "dpm_fast",
    "dpm adaptive": "dpm_adaptive", "ddim": "ddim", "ddpm": "ddpm",
    "unipc": "uni_pc", "uni_pc": "uni_pc", "uni-pc": "uni_pc",
    "unipc bh2": "uni_pc_bh2", "lcm": "lcm", "deis": "deis",
    "ipndm": "ipndm", "ipndm_v": "ipndm_v",
}
SCHEDULES = {
    "karras": "karras", "exponential": "exponential", "sgm uniform": "sgm_uniform",
    "sgm_uniform": "sgm_uniform", "simple": "simple", "ddim uniform": "ddim_uniform",
    "ddim_uniform": "ddim_uniform", "normal": "normal", "beta": "beta",
    "polyexponential": "polyexponential", "linear quadratic": "linear_quadratic",
}


def sampler_scheduler(fields: dict[str, str], warnings: list[str]) -> tuple[str, str]:
    raw = field(fields, "Sampler").strip()
    if not raw:
        raise ValueError("A1111 sampler missing; will not insert an invented sampler")
    sampler_label = raw
    trailing_schedule = None
    for suffix in ("Karras", "Exponential", "SGM Uniform", "Simple", "DDIM Uniform", "Beta"):
        if sampler_label.casefold().endswith(" " + suffix.casefold()):
            sampler_label = sampler_label[:-(len(suffix) + 1)].strip()
            trailing_schedule = SCHEDULES[suffix.casefold()]
            break
    sampler = SAMPLERS.get(re.sub(r"\s+", " ", sampler_label).casefold())
    if sampler is None:
        raise ValueError(f"Unsupported A1111 sampler {raw!r}; refusing to invent a ComfyUI equivalent")
    schedule_raw = field(fields, "Schedule type", field(fields, "Scheduler", "")).strip().casefold()
    if not schedule_raw or schedule_raw in ("automatic", "auto"):
        schedule = trailing_schedule or "normal"
        if not trailing_schedule:
            warnings.append("A1111 schedule unspecified/automatic; ComfyUI 'normal' is a default, not a verified match")
    else:
        schedule = SCHEDULES.get(schedule_raw)
        if schedule is None:
            # User-selected behavior: unknown scheduler becomes a valid, editable
            # ComfyUI fallback. This is NOT a claim that A1111 used 'normal'.
            # Original A1111 infotext remains untouched in the PNG parameters.
            warnings.append(
                f"A1111 scheduler {schedule_raw!r} has no verified ComfyUI mapping; "
                "using editable 'normal' fallback (original retained in parameters)"
            )
            schedule = "normal"
        if trailing_schedule and trailing_schedule != schedule:
            warnings.append(f"Sampler label and Schedule type disagree; used Schedule type={schedule}")
    return sampler, schedule


class Graph:
    def __init__(self) -> None:
        self.nodes: list[dict] = []
        self.links: list[list] = []
        self.api: dict[str, dict] = {}

    def node(self, name: str, xy: tuple[int, int], outputs: list[str],
             widgets: list | None = None, values: dict | None = None, title: str | None = None) -> int:
        node_id = len(self.nodes) + 1
        node = {
            "id": node_id, "type": name, "pos": list(xy), "size": [320, 125 if not widgets else 180],
            "flags": {}, "order": node_id - 1, "mode": 0, "inputs": [],
            "outputs": [{"name": typ, "type": typ, "slot_index": i, "links": []}
                        for i, typ in enumerate(outputs)],
            "properties": {"Node name for S&R": name}, "widgets_values": widgets or [],
        }
        if title:
            node["title"] = title
        if name == "CLIPTextEncode":
            node["size"] = [425, 205]
        if name == "KSampler":
            node["size"] = [335, 265]
        self.nodes.append(node)
        self.api[str(node_id)] = {"inputs": dict(values or {}), "class_type": name,
                                  "_meta": {"title": title or name}}
        return node_id

    def connect(self, source: int, out: int, dest: int, in_name: str, typ: str) -> None:
        origin = self.nodes[source - 1]
        target = self.nodes[dest - 1]
        assert origin["outputs"][out]["type"] == typ, (source, dest, typ)
        link_id = len(self.links) + 1
        in_idx = len(target["inputs"])
        target["inputs"].append({"name": in_name, "type": typ, "link": link_id})
        origin["outputs"][out]["links"].append(link_id)
        self.links.append([link_id, source, out, dest, in_idx, typ])
        self.api[str(dest)]["inputs"][in_name] = [str(source), out]

    def export(self) -> tuple[dict, dict]:
        # Legacy/LiteGraph canvas graph format remains accepted for PNG restoration.
        canvas = {
            "last_node_id": len(self.nodes), "last_link_id": len(self.links),
            "nodes": self.nodes, "links": self.links, "groups": [], "config": {},
            "extra": {}, "version": 0.4,
        }
        return canvas, self.api


def detect_generation_type(fields: dict[str, str], denoise: float, hires: bool,
                           requested: str, warnings: list[str]) -> tuple[str, str]:
    """Infotext does not explicitly encode the generation tab. Infer conservatively."""
    if requested != "auto":
        return requested, "forced by --mode"

    # These values are normally emitted for mask/inpainting workflows. A1111
    # does not store the actual mask, so this remains an educated guess.
    evidence = [name for name in ("Masked content", "Mask mode", "Inpaint area",
                                 "Inpaint at full resolution", "Inpaint full res",
                                 "Only masked padding, pixels", "Inpainting mask invert")
                if field(fields, name)]
    if evidence:
        warnings.append("Inpainting inferred from fields (" + ", ".join(evidence) +
                        "); A1111 infotext does not guarantee the generation mode")
        if hires:
            warnings.append("Both hires and inpainting hints found; first stage is inpainting "
                            "and the hires pass is an approximation")
        return "inpaint", "inpainting-specific settings: " + ", ".join(evidence)
    if field(fields, "Mask blur"):
        warnings.append("Mask blur alone does not prove inpainting; "
                        "using img2img unless explicit inpainting settings exist")
    if hires:
        return "txt2img", "hires fix settings (txt2img first pass)"
    if denoise < 1.0:
        warnings.append("img2img inferred from denoising strength < 1; "
                        "A1111 infotext does not explicitly identify its tab")
        return "img2img", "denoising strength < 1 without hires fix"
    return "txt2img", "no img2img or inpainting indicators"


def build_graph(raw: str, png_width: int, png_height: int,
                model_ext: str, mode: str = "auto") -> tuple[dict, dict, dict]:
    positive, negative, _pairs, fields = parse_a1111(raw)
    warnings: list[str] = []
    sampler, schedule = sampler_scheduler(fields, warnings)
    # Use only actual recorded values for indispensable sampler parameters.
    # Never replace missing/invalid/large seeds or settings with plausible defaults.
    try:
        steps = int(field(fields, "Steps"))
        seed = int(field(fields, "Seed"))
        cfg = float(field(fields, "CFG scale"))
    except (ValueError, TypeError) as exc:
        raise ValueError("A1111 Steps, Seed or CFG scale missing/invalid; no values invented") from exc
    if steps < 1 or not 0 <= seed < 2**64 or not math.isfinite(cfg) or cfg < 0:
        raise ValueError("A1111 Steps/Seed/CFG scale outside acceptable range; refusing substitution")
    raw_size = field(fields, "Size")
    size_match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", raw_size)
    if size_match:
        base_width, base_height = (int(size_match.group(1)), int(size_match.group(2)))
    else:
        raise ValueError("A1111 Size missing/invalid; refusing to infer generation dimensions from PNG")
    if min(base_width, base_height) < 8:
        raise ValueError("invalid generation dimensions")

    # Strip standard A1111 LoRA syntax only from graph prompts; original text is retained verbatim.
    loras: list[dict] = []
    def lora_replacer(match: re.Match) -> str:
        name = match.group(1)
        strength_model = float(match.group(2))
        strength_clip = float(match.group(3)) if match.group(3) is not None else strength_model
        if not any(x["name"] == name and x["strength_model"] == strength_model and
                   x["strength_clip"] == strength_clip for x in loras):
            loras.append({"name": name, "strength_model": strength_model,
                          "strength_clip": strength_clip, "tag": match.group(0)})
        return ""
    pos_clean = LORA_REGEX.sub(lora_replacer, positive).strip()
    neg_clean = LORA_REGEX.sub(lora_replacer, negative).strip()
    if re.search(r"<lora:", pos_clean + neg_clean, re.I):
        warnings.append("Some nonstandard LoRA tags could not be converted into LoraLoader nodes")
    for addon in addnet_loras(fields, warnings):
        if not any(x["name"].casefold() == addon["name"].casefold() and
                   x["strength_model"] == addon["strength_model"] and
                   x["strength_clip"] == addon["strength_clip"] for x in loras):
            loras.append(addon)
    if field(fields, "Lora hashes"):
        warnings.append("Original LoRA hashes retained in A1111 parameters; "
                        "ComfyUI loader filenames have not been hash-verified")

    g = Graph()
    ckpt = field(fields, "Model").strip()
    if not ckpt:
        ckpt = "SELECT_CHECKPOINT.safetensors"
        warnings.append("Model not present in A1111 metadata; choose checkpoint in ComfyUI")
    elif Path(ckpt).suffix.lower() not in (".safetensors", ".ckpt"):
        warnings.append(f"Guessed checkpoint extension {model_ext}; verify actual ComfyUI filename")
        ckpt = extension(ckpt, model_ext)
    model_loader = g.node("CheckpointLoaderSimple", (25, 65), ["MODEL", "CLIP", "VAE"],
                          [ckpt], {"ckpt_name": ckpt})
    model_src, model_out = model_loader, 0
    clip_src, clip_out = model_loader, 1
    vae_src, vae_out = model_loader, 2
    vae_name = field(fields, "VAE").strip()
    if vae_name and vae_name.casefold() not in ("automatic", "none"):
        vae_filename = extension(vae_name, model_ext)
        vae_loader = g.node("VAELoader", (25, 415), ["VAE"], [vae_filename],
                            {"vae_name": vae_filename})
        vae_src, vae_out = vae_loader, 0
        warnings.append("External VAE name needs to match a file in ComfyUI's VAE directory")
    clip_skip = int(as_number(field(fields, "Clip skip", "1"), 1, int))
    if clip_skip > 1:
        skip = g.node("CLIPSetLastLayer", (365, 415), ["CLIP"], [-clip_skip],
                      {"stop_at_clip_layer": -clip_skip})
        g.connect(clip_src, clip_out, skip, "clip", "CLIP")
        clip_src, clip_out = skip, 0
        warnings.append("A1111 CLIP skip conventions can differ from ComfyUI, especially for SDXL")
    for i, item in enumerate(loras):
        name = extension(item["name"], model_ext)
        n = g.node("LoraLoader", (400 + 355 * i, 65), ["MODEL", "CLIP"],
                   [name, item["strength_model"], item["strength_clip"]],
                   {"lora_name": name, "strength_model": item["strength_model"],
                    "strength_clip": item["strength_clip"]})
        g.connect(model_src, model_out, n, "model", "MODEL")
        g.connect(clip_src, clip_out, n, "clip", "CLIP")
        model_src, model_out = n, 0
        clip_src, clip_out = n, 1
    if loras:
        warnings.append("LoRA filenames and strengths may need checking; A1111 LoRA tags are not a full recipe")

    x = 540 + 355 * len(loras)
    pos = g.node("CLIPTextEncode", (x, 25), ["CONDITIONING"], [pos_clean], {"text": pos_clean}, "A1111 Positive")
    neg = g.node("CLIPTextEncode", (x, 305), ["CONDITIONING"], [neg_clean], {"text": neg_clean}, "A1111 Negative")
    g.connect(clip_src, clip_out, pos, "clip", "CLIP")
    g.connect(clip_src, clip_out, neg, "clip", "CLIP")
    hires = bool(field(fields, "Hires upscale") or field(fields, "Hires resize") or
                 field(fields, "Hires upscaler") or field(fields, "Hires steps"))
    denoise = float(as_number(field(fields, "Denoising strength", "1"), 1.0))
    if denoise < 0 or denoise > 1:
        raise ValueError("A1111 denoising strength out of range; refusing to clamp")
    generation_type, classification_reason = detect_generation_type(
        fields, denoise, hires, mode, warnings)
    source_image_required = generation_type in ("img2img", "inpaint")
    mask_required = generation_type == "inpaint"
    if source_image_required:
        warnings.append("Original source image not embedded in A1111 metadata; "
                        "replace SELECT_SOURCE_IMAGE.png in LoadImage before execution")
        image = g.node("LoadImage", (x, 570), ["IMAGE", "MASK"],
                       ["SELECT_SOURCE_IMAGE.png", "image"],
                       {"image": "SELECT_SOURCE_IMAGE.png"},
                       "REPLACE: original input image")
        # Original source resize/crop method is generally not in the infotext.
        # Link the source directly; the user must supply the correctly sized
        # original image. Never claim Lanczos/center-crop was used by A1111.
        warnings.append("Original img2img source resize/crop is not specified; "
                        "no resize algorithm has been invented. Supply a suitably sized source image")
        if mask_required:
            warnings.append("Original inpainting mask not embedded in A1111 metadata; "
                            "replace SELECT_MASK_IMAGE.png in LoadImageMask before execution; "
                            "white mask pixels are the region to inpaint")
            mask = g.node("LoadImageMask", (x, 845), ["MASK"],
                          ["SELECT_MASK_IMAGE.png", "red"],
                          {"image": "SELECT_MASK_IMAGE.png", "channel": "red"},
                          "REPLACE: original inpaint mask (white = paint)")
            mask_mode = field(fields, "Mask mode").casefold()
            if "not masked" in mask_mode:
                inverted = g.node("InvertMask", (x + 350, 845), ["MASK"], [], {},
                                  "A1111: Inpaint not masked")
                g.connect(mask, 0, inverted, "mask", "MASK")
                mask = inverted
            # Mask blur is not the same as VAEEncodeForInpaint's grow_mask_by.
            # Do not misrepresent it as dilation; retain the original settings.
            latent = g.node("VAEEncodeForInpaint", (x + 835, 635), ["LATENT"],
                            [0], {"grow_mask_by": 0}, "A1111 Inpainting Encode")
            g.connect(image, 0, latent, "pixels", "IMAGE")
            g.connect(vae_src, vae_out, latent, "vae", "VAE")
            g.connect(mask, 0, latent, "mask", "MASK")
            warnings.append("Inpainting is approximate: A1111 mask blur, masked fill, "
                            "only-masked cropping and final compositing are not reconstructed; "
                            "original settings remain in A1111 parameters")
        else:
            latent = g.node("VAEEncode", (x + 835, 635), ["LATENT"], [], {},
                            "A1111 img2img Encode")
            g.connect(image, 0, latent, "pixels", "IMAGE")
            g.connect(vae_src, vae_out, latent, "vae", "VAE")
    else:
        latent = g.node("EmptyLatentImage", (x, 585), ["LATENT"],
                        [base_width, base_height, 1],
                        {"width": base_width, "height": base_height, "batch_size": 1})
    first_denoise = 1.0 if hires and not source_image_required else denoise
    sampler_node = g.node("KSampler", (x + 525, 150), ["LATENT"],
                          [seed, "fixed", steps, cfg, sampler, schedule, first_denoise],
                          {"seed": seed, "steps": steps, "cfg": cfg,
                           "sampler_name": sampler, "scheduler": schedule, "denoise": first_denoise},
                          "A1111 Sampling / First Pass" if hires else "A1111 Sampling")
    for sid, slot, name, typ in ((model_src, model_out, "model", "MODEL"),
                                 (pos, 0, "positive", "CONDITIONING"),
                                 (neg, 0, "negative", "CONDITIONING"),
                                 (latent, 0, "latent_image", "LATENT")):
        g.connect(sid, slot, sampler_node, name, typ)
    final_latent = sampler_node
    final_x = x + 970
    if hires:
        factor = float(as_number(field(fields, "Hires upscale", "2"), 2))
        if factor <= 0:
            raise ValueError("Invalid Hires upscale factor")
        target_w, target_h = round(base_width * factor), round(base_height * factor)
        resize = re.search(r"(\d+)\s*[xX]\s*(\d+)", field(fields, "Hires resize"))
        if resize and int(resize.group(1)) > 0 and int(resize.group(2)) > 0:
            target_w, target_h = int(resize.group(1)), int(resize.group(2))
        elif (png_width, png_height) != (base_width, base_height) and (png_width, png_height) != (target_w, target_h):
            warnings.append("Final PNG dimensions differ from calculated Hires resize; "
                            "preserved original generation dimensions")
        if target_w < 8 or target_h < 8:
            raise ValueError("invalid hires target size")
        hires_upscaler = field(fields, "Hires upscaler").strip()
        # Only explicitly named latent modes with direct known counterparts.
        latent_methods = {
            "latent (bicubic)": "bicubic",
            "latent (nearest-exact)": "nearest-exact",
            "latent (bislerp)": "bislerp",
        }
        method = latent_methods.get(hires_upscaler.casefold())
        if method is not None and (not field(fields, "Denoising strength") or
                                   LORA_REGEX.search(field(fields, "Hires prompt") +
                                                     field(fields, "Hires negative prompt"))):
            method = None
            warnings.append("Hires stage needs unknown denoising or specialized Hires-only LoRA loading; "
                            "refusing to omit those settings from a reconstructed second pass")
        if method is None:
            warnings.append(f"NOT RECONSTRUCTED: A1111 hires upscaler {hires_upscaler or '(unspecified)'!r}. "
                            "No interpolation or upscale model was substituted. "
                            "ComfyUI graph ends at the first pass; original Hires.fix settings are in parameters")
            # This is intentionally a partial workflow: a node title makes that
            # visible in ComfyUI without inventing an additional PNG metadata key.
            g.nodes[sampler_node - 1]["title"] = "PARTIAL: original A1111 Hires.fix is not reconstructed"
            g.api[str(sampler_node)]["_meta"]["title"] = g.nodes[sampler_node - 1]["title"]
        else:
            up = g.node("LatentUpscale", (final_x, 490), ["LATENT"],
                        [method, target_w, target_h, "disabled"],
                        {"upscale_method": method, "width": target_w,
                         "height": target_h, "crop": "disabled"})
            g.connect(final_latent, 0, up, "samples", "LATENT")
            upscale_latent = up
            hires_steps = int(as_number(field(fields, "Hires steps", str(steps)), steps, int))
            if hires_steps < 1:  # A1111 'Hires steps: 0' means use first-pass steps.
                hires_steps = steps
            if not field(fields, "Denoising strength"):
                warnings.append("Hires denoising strength missing; used 1.0 placeholder")
            hires_pos, hires_neg = pos, neg
            if field(fields, "Hires prompt"):
                hp = LORA_REGEX.sub("", strip_surrounding_quotes(field(fields, "Hires prompt"))).strip()
                hires_pos = g.node("CLIPTextEncode", (final_x + 385, -110), ["CONDITIONING"],
                                   [hp], {"text": hp}, "A1111 Hires Positive")
                g.connect(clip_src, clip_out, hires_pos, "clip", "CLIP")
            if field(fields, "Hires negative prompt"):
                hn = LORA_REGEX.sub("", strip_surrounding_quotes(field(fields, "Hires negative prompt"))).strip()
                hires_neg = g.node("CLIPTextEncode", (final_x + 385, 175), ["CONDITIONING"],
                                   [hn], {"text": hn}, "A1111 Hires Negative")
                g.connect(clip_src, clip_out, hires_neg, "clip", "CLIP")
            second = g.node("KSampler", (final_x + 825, 405), ["LATENT"],
                            [seed, "fixed", hires_steps, cfg, sampler, schedule, denoise],
                            {"seed": seed, "steps": hires_steps, "cfg": cfg,
                             "sampler_name": sampler, "scheduler": schedule, "denoise": denoise},
                            "A1111 Hires Second Pass")
            for sid, slot, name, typ in ((model_src, model_out, "model", "MODEL"),
                                         (hires_pos, 0, "positive", "CONDITIONING"),
                                         (hires_neg, 0, "negative", "CONDITIONING"),
                                         (upscale_latent, 0, "latent_image", "LATENT")):
                g.connect(sid, slot, second, name, typ)
            final_latent = second
            final_x += 1250
    decoded = g.node("VAEDecode", (final_x, 100), ["IMAGE"])
    g.connect(final_latent, 0, decoded, "samples", "LATENT")
    g.connect(vae_src, vae_out, decoded, "vae", "VAE")
    save = g.node("SaveImage", (final_x + 390, 110), [], ["A1111_Imported"],
                  {"filename_prefix": "A1111_Imported"})
    g.connect(decoded, 0, save, "images", "IMAGE")

    # Warn about nontrivial settings that are preserved, not reconstructed.
    complex_keys = ("controlnet", "adetailer", "refiner", "regional", "tiled", "noise multiplier",
                    "variation", "seed resize", "freeu", "sampler eta", "eta noise", "dynamic threshold",
                    "inpaint", "script", "soft inpainting", "forge", "paged")
    unmatched = [k for k in fields if any(term in k.casefold() for term in complex_keys)]
    if unmatched:
        warnings.append("Specialized settings preserved in original parameters, not rebuilt as nodes: " + ", ".join(unmatched))
    if (png_width, png_height) != (base_width, base_height) and not hires:
        warnings.append("PNG pixel dimensions and A1111 Size differ "
                        "(potential inpaint crop, img2img resize, or post-processing)")
    warnings.append("A1111 and ComfyUI may differ in RNG, prompt parsing, VAE, scheduler details and extensions; exact pixel reproducibility is not guaranteed")
    canvas, api = g.export()
    # Conversion diagnostics remain available in --verbose and --report;
    # they are deliberately not written into the PNG.
    return canvas, api, {"generation_type": generation_type, "warnings": warnings}


def png_text_chunk(key: str, value: str) -> bytes:
    """Create a standard PNG tEXt chunk (JSON ensure_ascii=True => valid Latin-1)."""
    payload = key.encode("latin-1") + b"\0" + value.encode("ascii")
    kind = b"tEXt"
    crc = zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def copy_n(source, target, length: int) -> None:
    remaining = length
    while remaining:
        piece = source.read(min(1024 * 1024, remaining))
        if not piece:
            raise PngError("truncated PNG during copy")
        target.write(piece)
        remaining -= len(piece)


def make_unique_backup_path(path: Path) -> Path:
    """Keep the .png extension so Windows recognizes each backup as an image.

    image.png -> image.bak1.png -> image.bak2.png ...
    """
    number = 1
    while True:
        backup_path = path.with_name(f"{path.stem}.bak{number}{path.suffix}")
        if not backup_path.exists():
            return backup_path
        number += 1


def _prepare_png(src, dst, updates: dict[str, str]) -> None:
    """Copy unchanged chunks; replace workflow/prompt and discard legacy custom data."""
    if src.read(8) != PNG_SIG:
        raise PngError("not a PNG")
    dst.write(PNG_SIG)
    inserted = False
    while True:
        head = src.read(8)
        if len(head) != 8:
            raise PngError("truncated PNG while rewriting")
        size, kind = struct.unpack(">I4s", head)
        if kind in (b"IDAT", b"IEND") and not inserted:
            for key, value in updates.items():
                dst.write(png_text_chunk(key, value))
            inserted = True
        if kind in TEXT_TYPES:
            if size > MAX_TEXT:
                raise PngError("oversized text chunk")
            content = src.read(size + 4)
            if len(content) != size + 4:
                raise PngError("truncated PNG text")
            # Also remove legacy a1111_import chunks from earlier converter versions.
            if chunk_key(kind, content[:size]) not in REPLACE_KEYS:
                dst.write(head)
                dst.write(content)
        else:
            dst.write(head)
            copy_n(src, dst, size + 4)
        if kind == b"IEND":
            break
    if not inserted:
        raise PngError("PNG has no IEND")
    if src.read(1):
        raise PngError("PNG has data after IEND (cannot safely preserve it)")


def _temp_path(path: Path, marker: str) -> Path:
    """Generate a collision-resistant same-volume path without creating a file."""
    return path.with_name(f".{path.name}.{marker}.{uuid4().hex}.tmp")


def _stream_overwrite_existing(destination: Path, staged: Path) -> None:
    """Overwrite the SAME on-disk file, never renaming/replacing its filesystem entry.

    This preserves the original NTFS file identity/creation date. The staged file
    is already valid before writing; an independent rollback contains old bytes.
    """
    with staged.open("rb") as inp, destination.open("r+b") as out:
        out.seek(0)
        shutil.copyfileobj(inp, out, 1024 * 1024)
        out.truncate(out.tell())
        out.flush()
        os.fsync(out.fileno())


def _check_original_file_identity(path: Path, identity: tuple[int, int],
                                  creation_filetime: int | None) -> None:
    stat = path.stat()
    if (stat.st_dev, stat.st_ino) != identity:
        raise OSError("file identity changed: original PNG was replaced")
    if creation_filetime is not None:
        current = _windows_filetime(path)[0]
        if current != creation_filetime:
            raise OSError(f"Date Created changed: expected {creation_filetime}, got {current}")


def write_png_with_metadata(path: Path, updates: dict[str, str], backup: bool,
                            times: OriginalTimes, verify: bool = False,
                            before_entries: dict[str, str] | None = None,
                            workflow: dict | None = None,
                            api: dict | None = None) -> None:
    """Stage/validate new PNG, then rewrite the SAME file in place.

    A full independent rollback copy is mandatory: a hardlink is NOT safe for
    in-place writes. On ordinary exceptions, restore old bytes in place. A
    power failure during the write cannot be made atomic without replacement.
    """
    identity = (path.stat().st_dev, path.stat().st_ino)
    creation = times.windows_filetimes[0] if times.windows_filetimes else None
    fd, tmp_name = tempfile.mkstemp(prefix=".a1111_to_comfy_", suffix=".tmp", dir=str(path.parent))
    temp = Path(tmp_name)
    rollback: Path | None = None
    retained_backup = False
    attempted_write = False
    recovered = False
    try:
        with path.open("rb") as source, os.fdopen(fd, "wb") as target:
            fd = -1
            _prepare_png(source, target, updates)
            target.flush()
            os.fsync(target.fileno())
        if backup:
            rollback = make_unique_backup_path(path)
        else:
            rollback = _temp_path(path, "rollback")
        # Copy the original bytes to an independent file before any in-place write.
        with path.open("rb") as inp, rollback.open("xb") as out:
            shutil.copyfileobj(inp, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(path, rollback)
        restore_times(rollback, times)
        retained_backup = backup

        attempted_write = True
        _stream_overwrite_existing(path, temp)
        if verify:
            if before_entries is None or workflow is None or api is None:
                raise ValueError("internal error: missing --verify comparison data")
            verify_conversion(path, updates, before_entries, workflow, api,
                              source_png=rollback)
        # Restore ALL original filesystem timestamps. Preserving only creation
        # was insufficient for Windows Explorer's generic Date column, which
        # commonly falls back to the PNG file's Last Modified timestamp.
        restore_times(path, times)
        _check_original_file_identity(path, identity, creation)
        # Always verify filesystem dates, not just when --verify is supplied.
        assert_times(path, times)
    except BaseException as exc:
        if attempted_write and rollback is not None and rollback.exists():
            try:
                _stream_overwrite_existing(path, rollback)
                restore_times(path, times)
                _check_original_file_identity(path, identity, creation)
                recovered = True
            except BaseException as rollback_exc:
                raise OSError(f"update failed ({exc}); ROLLBACK ALSO FAILED "
                              f"({rollback_exc}); original bytes are at {rollback}") from exc
        raise
    finally:
        if fd != -1:
            os.close(fd)
        if temp.exists():
            temp.unlink()
        if rollback is not None and rollback.exists() and not retained_backup and (not attempted_write or recovered or sys.exc_info()[0] is None):
            rollback.unlink()
        # A user-requested backup is always retained, even after a recovered error.

def png_stream_idat_digest(path: Path) -> tuple[str, int]:
    """Digest the concatenated compressed IDAT data without decoding pixels."""
    hasher = hashlib.sha256()
    chunks = 0
    with path.open("rb") as fp:
        if fp.read(8) != PNG_SIG:
            raise PngError("not a PNG")
        while True:
            head = fp.read(8)
            if len(head) != 8:
                raise PngError("truncated PNG")
            size, kind = struct.unpack(">I4s", head)
            if kind == b"IDAT":
                count = size
                while count:
                    block = fp.read(min(count, 1024 * 1024))
                    if not block:
                        raise PngError("truncated IDAT")
                    hasher.update(block)
                    count -= len(block)
                chunks += 1
            else:
                fp.seek(size, 1)
            if len(fp.read(4)) != 4:
                raise PngError("truncated PNG CRC")
            if kind == b"IEND":
                break
    return hasher.hexdigest(), chunks


def validate_graph(workflow: dict, api: dict) -> None:
    """Structural validation against ComfyUI's legacy 0.4 canvas/API layout."""
    if workflow.get("version") != 0.4:
        raise ValueError("expected ComfyUI legacy workflow version 0.4")
    nodes = workflow.get("nodes")
    links = workflow.get("links")
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise ValueError("invalid ComfyUI workflow nodes/links")
    node_by_id = {n["id"]: n for n in nodes}
    if len(node_by_id) != len(nodes) or len(api) != len(nodes):
        raise ValueError("ComfyUI workflow has mismatched or duplicate node IDs")
    for n in nodes:
        aid = str(n["id"])
        if aid not in api or api[aid].get("class_type") != n["type"]:
            raise ValueError(f"node {aid} API class does not match canvas")
        if not isinstance(n.get("widgets_values", []), list):
            raise ValueError(f"node {aid} widgets_values must be a list")
    seen_links = set()
    for link_id, sid, out_slot, did, in_slot, typ in links:
        if link_id in seen_links:
            raise ValueError("duplicate link ID")
        seen_links.add(link_id)
        if sid not in node_by_id or did not in node_by_id:
            raise ValueError("link references nonexistent node")
        source = node_by_id[sid]
        destination = node_by_id[did]
        if out_slot >= len(source["outputs"]) or in_slot >= len(destination["inputs"]):
            raise ValueError("link references nonexistent node slot")
        if (source["outputs"][out_slot]["type"] != typ or
                destination["inputs"][in_slot]["type"] != typ or
                destination["inputs"][in_slot]["link"] != link_id or
                link_id not in source["outputs"][out_slot]["links"]):
            raise ValueError("canvas link consistency check failed")
        name = destination["inputs"][in_slot]["name"]
        if api[str(did)]["inputs"].get(name) != [str(sid), out_slot]:
            raise ValueError("ComfyUI API connection does not match canvas")
    if not any(n["type"] == "SaveImage" for n in nodes):
        raise ValueError("ComfyUI workflow has no SaveImage output")


def verify_conversion(path: Path, updates: dict[str, str],
                      before_entries: dict[str, str], workflow: dict,
                      api: dict, source_png: Path) -> None:
    """Re-read file; verify native ComfyUI chunks, original text, and IDAT."""
    validate_graph(workflow, api)
    _, _, after = read_png_metadata(path)
    for key, original in before_entries.items():
        if key not in REPLACE_KEYS and after.get(key) != original:
            raise ValueError(f"original PNG text entry {key!r} changed")
    for key, expected in updates.items():
        if after.get(key) != expected:
            raise ValueError(f"converted metadata {key!r} did not round-trip exactly")
    if after.get("parameters") != before_entries.get("parameters"):
        raise ValueError("original A1111 parameters were not preserved")
    if json.loads(after["workflow"]) != workflow or json.loads(after["prompt"]) != api:
        raise ValueError("ComfyUI JSON differs from generated graph")
    if "a1111_import" in after or "a1111_import" in workflow.get("extra", {}):
        raise ValueError("obsolete a1111_import metadata was not removed")
    if png_stream_idat_digest(path) != png_stream_idat_digest(source_png):
        raise ValueError("compressed PNG IDAT contents changed")



def iter_pngs(path: Path, recursive: bool):
    if path.is_file():
        if path.suffix.lower() != ".png":
            raise ValueError("input file must be .png")
        yield path
    elif path.is_dir():
        paths = path.rglob("*") if recursive else path.iterdir()
        for item in paths:
            if (item.is_file() and not item.is_symlink() and
                    item.suffix.lower() == ".png" and
                    not BACKUP_NAME.search(item.name)):
                yield item
    else:
        raise ValueError(f"file or directory not found: {path}")


class Progress:
    """Compact, single-line progress on an interactive terminal."""

    def __init__(self, total: int, enabled: bool, dry_run: bool) -> None:
        self.total = total
        self.enabled = enabled
        self.dry_run = dry_run
        self.terminal = sys.stdout.isatty()
        self.last_width = 0

    def update(self, processed: int, counts: dict[str, int]) -> None:
        if not self.enabled:
            return
        percent = (100 * processed / self.total) if self.total else 100
        columns = shutil.get_terminal_size(fallback=(100, 24)).columns
        verb = "previewed" if self.dry_run else "processed"
        tail = (f" {processed}/{self.total} files {verb} ({percent:5.1f}%) | "
                f"{counts['converted']} converted, {counts['skipped']} skipped, "
                f"{counts['failed']} failed")
        # Windows console width can be narrow; don't wrap the progress line.
        bar_width = min(32, max(3, columns - len(tail) - 4))
        filled = round(bar_width * processed / self.total) if self.total else bar_width
        bar = "#" * filled + "-" * (bar_width - filled)
        display = f"[{bar}]{tail}"
        if self.terminal:
            # Padding overwrites any leftover characters if the line shrinks.
            sys.stdout.write("\r" + display + " " * max(0, self.last_width - len(display)))
            sys.stdout.flush()
            self.last_width = len(display)
        elif processed == self.total:
            # Redirected output should remain readable, not contain many CRs.
            print(display)

    def detail(self, message: str) -> None:
        if self.terminal and self.last_width:
            sys.stdout.write("\r" + " " * self.last_width + "\r")
        print(message)
        self.last_width = 0

    def finish(self) -> None:
        if self.enabled and self.terminal and self.last_width:
            print()
            self.last_width = 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version",
                        version="convert_metadata_a1111_to_comfyui.py 1.01")
    parser.add_argument("path", type=Path, help="PNG file or directory of PNGs")
    parser.add_argument("--recursive", "-r", action="store_true", help="process subdirectories")
    parser.add_argument("--backup", action="store_true",
                        help="keep original backups (image.bak1.png, image.bak2.png, etc.)")
    parser.add_argument("--dry-run", action="store_true", help="preview only: never write PNGs or reports")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace ComfyUI workflow/prompt and remove old a1111_import metadata")
    parser.add_argument("--model-ext", choices=(".safetensors", ".ckpt"), default=".safetensors",
                        help="extension guessed when an A1111 model or LoRA name lacks one")
    parser.add_argument("--mode", choices=("auto", "txt2img", "img2img", "inpaint"), default="auto",
                        help="force generation type when metadata is ambiguous")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="list each image's result and conversion notes")
    parser.add_argument("--no-progress", action="store_true", help="disable progress bar")
    parser.add_argument("--quiet", action="store_true", help="only print errors and totals")
    parser.add_argument("--verify", action="store_true",
                        help="re-read each converted PNG and check metadata, graph links, IDAT")
    parser.add_argument("--report", type=Path, metavar="FILE",
                        help="write per-PNG CSV report (not supported with --dry-run)")
    args = parser.parse_args()
    if args.dry_run and args.report:
        parser.error("--dry-run intentionally writes no files; omit --report")
    count = {"converted": 0, "skipped": 0, "failed": 0}
    types = {"txt2img": 0, "img2img": 0, "inpaint": 0}
    report_rows: list[dict[str, str | int]] = []
    try:
        path = args.path.expanduser()
        candidates = sorted(iter_pngs(path, args.recursive), key=lambda p: str(p).casefold())
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    total = len(candidates)
    progress = Progress(total, not args.quiet and not args.no_progress, args.dry_run)
    if not args.quiet:
        print(f"Found {total} PNG file(s); {'dry-run' if args.dry_run else 'conversion'} mode "
              f"(generation mode: {args.mode}).")
    for idx, file in enumerate(candidates, 1):
        row: dict[str, str | int] = {"file": str(file), "status": "", "generation_type": "",
                                     "nodes": 0, "warnings": "", "error": ""}
        try:
            # Before opening image: capture all filesystem timestamps including
            # Windows Date Created (GetFileTime), not merely st_mtime.
            times = capture_times(file) if not args.dry_run else None
            width, height, entries = read_png_metadata(file)
            raw = entries.get("parameters")
            skip_reason = None
            if not raw:
                skip_reason = "no A1111 parameters"
            elif "workflow" in entries and not args.overwrite:
                skip_reason = "ComfyUI workflow exists"
            elif "prompt" in entries and not args.overwrite:
                skip_reason = "prompt chunk exists; use --overwrite to replace"
            if skip_reason:
                count["skipped"] += 1
                row["status"] = "skipped"
                row["warnings"] = skip_reason
                if args.verbose and not args.quiet:
                    progress.detail(f"SKIP ({skip_reason}): {file}")
            else:
                workflow, api, diagnostics = build_graph(
                    raw, width, height, args.model_ext, args.mode)
                # Always do cheap consistency validation before any modification.
                validate_graph(workflow, api)
                updates = {"workflow": json.dumps(workflow, ensure_ascii=True, separators=(",", ":")),
                           "prompt": json.dumps(api, ensure_ascii=True, separators=(",", ":"))}
                if not args.dry_run:
                    write_png_with_metadata(file, updates, args.backup, times, args.verify,
                                            entries, workflow, api)
                count["converted"] += 1
                gen_type = diagnostics["generation_type"]
                types[gen_type] += 1
                row.update({"status": "would_convert" if args.dry_run else "converted",
                            "generation_type": gen_type,
                            "nodes": len(workflow["nodes"]),
                            "warnings": " | ".join(diagnostics["warnings"])})
                if args.verbose and not args.quiet:
                    progress.detail(f"{'WOULD CONVERT' if args.dry_run else 'CONVERTED'}: {file} "
                                    f"({len(workflow['nodes'])} nodes; {gen_type}; "
                                    f"{len(diagnostics['warnings'])} notes)")
                    for note in diagnostics["warnings"]:
                        progress.detail("    NOTE: " + note)
        except (OSError, ValueError, PngError, UnicodeError) as exc:
            count["failed"] += 1
            row.update({"status": "failed", "error": str(exc)})
            if progress.terminal and progress.enabled and progress.last_width:
                sys.stdout.write("\r" + " " * progress.last_width + "\r")
                sys.stdout.flush()
                progress.last_width = 0
            print(f"ERROR: {file}: {exc}", file=sys.stderr)
        finally:
            if args.report:
                report_rows.append(row)
            progress.update(idx, count)
    progress.finish()
    if args.report:
        try:
            report_path = args.report.expanduser()
            with report_path.open("w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.DictWriter(stream, fieldnames=("file", "status", "generation_type",
                                                            "nodes", "warnings", "error"))
                writer.writeheader()
                writer.writerows(report_rows)
            if not args.quiet:
                print(f"CSV report: {report_path}")
        except OSError as exc:
            print(f"ERROR: cannot write CSV report {args.report}: {exc}", file=sys.stderr)
            count["failed"] += 1
    print("Summary: " + ", ".join(f"{k}={v}" for k, v in count.items()))
    if not args.quiet and count["converted"]:
        print("Detected generation types: " + ", ".join(f"{k}={v}" for k, v in types.items()))
    return 1 if count["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
