#!/usr/bin/env python3
"""Bulk A1111 PNG -> ComfyUI metadata converter, version 1.02.

Python 3.9+; standard library only. Reads A1111 PNG 'parameters' text and PNG
EXIF UserComment (as retained by some JPEG-to-PNG tools). Preserves all original
PNG chunks, image IDAT data, timestamps and file identity. Adds ComfyUI canvas
'workflow' and executable 'prompt' when possible, or a Note-only canvas for
recognizable incomplete A1111 information. No custom ComfyUI nodes are needed.

The converter is best-effort: the generated graph is NOT the historical A1111
graph. Do not assume executing it reproduces the original pixels. A1111
schedulers without verified mappings deliberately use ComfyUI 'normal'; no
sampler or upscaler model is silently substituted. Use --backup for valuables:
in-place writes are NOT power-loss/crash atomic.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
from uuid import uuid4
import zlib
from typing import Any

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

VERSION = '1.02'
PNG_SIG = b'\x89PNG\r\n\x1a\n'
TEXT_TYPES = {b'tEXt', b'iTXt', b'zTXt'}
REPLACE_KEYS = {'workflow', 'prompt', 'a1111_import'}
MAX_TEXT = 16 * 1024 * 1024
BACKUP_NAME = re.compile(r'\.bak[1-9]\d*\.png$', re.I)
LORA_REGEX = re.compile(r'<lora:([^<>:]+):(-?\d+(?:\.\d+)?)(?::(-?\d+(?:\.\d+)?))?>', re.I)
SETTING_KEY = re.compile(r'^\s*([^,:\n]{1,100}):\s*(.*)$', re.S)
SETTINGS_START = re.compile(r'(?m)^Steps:\s*\d+\s*(?:,|$)')
POSTPROCESS_FIELDS = re.compile(r'(?:^|[,;\r\n])\s*(Postprocess\s+[A-Za-z][A-Za-z0-9 /+_.-]{0,60})\s*:\s*([^,;\r\n]+)', re.I)
DISTINCT_FIELDS = re.compile(
    r'(?:^|[,;\r\n])\s*(Hires (?:upscale|upscaler|steps|resize(?:-[12])?|checkpoint)|'
    r'ADetailer [A-Za-z][A-Za-z0-9 _/-]*|(?:Lora|LoRA|TI) hashes|'
    r'Variation seed(?: strength)?|Seed resize from(?:-[hw])?|Clip skip|'
    r'CFG scale|Denoising strength|Model hash|VAE hash|Face restoration|'
    r'Schedule type|Eta noise seed delta|ENSD)\s*:\s*([^,;\r\n]+)', re.I)
GENERAL_FIELDS = {
    'steps': re.compile(r'\bSteps\s*:\s*\d+\b', re.I),
    'sampler': re.compile(r'\bSampler\s*:\s*[^,\r\n]+', re.I),
    'seed': re.compile(r'\bSeed\s*:\s*-?\d+\b', re.I),
    'model': re.compile(r'(?:^|[,;\r\n])\s*Model\s*:\s*[^,;\r\n]+', re.I),
    'size': re.compile(r'\bSize\s*:\s*\d+\s*[xX]\s*\d+\b', re.I),
}

@dataclass(frozen=True)
class OriginalTimes:
    atime_ns: int
    mtime_ns: int
    windows_filetimes: tuple[int, int, int] | None = None


def _windows_filetime(path: Path, set_values: tuple[int, int, int] | None = None):
    """Read or write creation, access, modification FILETIME using Win32."""
    if os.name != 'nt':
        raise RuntimeError('Windows FILETIME requested on non-Windows system')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
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
    access = 0x100 if set_values is not None else 0x80
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
    return OriginalTimes(stat.st_atime_ns, stat.st_mtime_ns,
                         _windows_filetime(path) if os.name == 'nt' else None)


def restore_times(path: Path, times: OriginalTimes) -> None:
    if times.windows_filetimes is not None:
        _windows_filetime(path, times.windows_filetimes)
    else:
        os.utime(path, ns=(times.atime_ns, times.mtime_ns))


def assert_times(path: Path, times: OriginalTimes) -> None:
    if times.windows_filetimes is not None:
        if _windows_filetime(path) != times.windows_filetimes:
            raise OSError('Windows creation/access/modification FILETIMEs changed')
    else:
        stat = path.stat()
        if (stat.st_atime_ns, stat.st_mtime_ns) != (times.atime_ns, times.mtime_ns):
            raise OSError('access/modification timestamps changed')


class PngError(ValueError):
    pass


def decompress_limited(payload: bytes) -> bytes:
    obj = zlib.decompressobj()
    result = obj.decompress(payload, MAX_TEXT + 1)
    if len(result) > MAX_TEXT or obj.unconsumed_tail:
        raise PngError('oversized compressed PNG text')
    result += obj.flush(MAX_TEXT + 1 - len(result))
    if len(result) > MAX_TEXT:
        raise PngError('oversized PNG text')
    return result


def chunk_key(kind: bytes, payload: bytes) -> str | None:
    if kind not in TEXT_TYPES:
        return None
    return payload.split(b'\x00', 1)[0].decode('latin-1')


def text_from_chunk(kind: bytes, payload: bytes) -> tuple[str, str] | None:
    key = chunk_key(kind, payload)
    if key is None:
        return None
    try:
        _, rest = payload.split(b'\x00', 1)
        if kind == b'tEXt':
            val = rest.decode('latin-1')
        elif kind == b'zTXt':
            if not rest or rest[0] != 0:
                return None
            val = decompress_limited(rest[1:]).decode('latin-1')
        else:
            if len(rest) < 2:
                return None
            flag, method = rest[:2]
            parts = rest[2:].split(b'\x00', 2)
            if len(parts) != 3:
                return None
            data = parts[2]
            if flag == 1:
                if method != 0:
                    return None
                data = decompress_limited(data)
            elif flag != 0:
                return None
            val = data.decode('utf-8')
    except (ValueError, UnicodeError, zlib.error, IndexError):
        return None
    return key, val


def decode_exif_bytes(data: bytes) -> str:
    """Decode A1111 UserComment from standard EXIF encoding schemes."""
    if data.startswith(b'UNICODE\x00'):
        raw = data[8:]
        if raw.startswith((b'\xfe\xff', b'\xff\xfe')):
            return raw.decode('utf-16', errors='replace').strip('\x00 \t\r\n')
        return raw.decode('utf-16-be', errors='replace').strip('\x00 \t\r\n')
    if data.startswith(b'ASCII\x00\x00\x00'):
        data = data[8:]
    elif data.startswith(b'JIS\x00\x00\x00\x00\x00'):
        return data[8:].decode('shift_jis', errors='replace').strip('\x00 \t\r\n')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16', errors='replace').strip('\x00 \t\r\n')
    if len(data) >= 8 and data.count(b'\x00') > len(data) // 4:
        encoding = 'utf-16-be' if data[::2].count(0) > data[1::2].count(0) else 'utf-16-le'
        return data.decode(encoding, errors='replace').strip('\x00 \t\r\n')
    try:
        return data.decode('utf-8').strip('\x00 \t\r\n')
    except UnicodeDecodeError:
        return data.decode('latin-1').strip('\x00 \t\r\n')


def extract_exif_usercomment(payload: bytes) -> str | None:
    """Read UserComment from PNG eXIf TIFF. Don't dereference UserComment contents
    as an IFD pointer (the erroneous 'UNIC' offset from the original JPG scanner).
    Malformed EXIF is treated as unavailable, without changing its bytes.
    """
    if payload.startswith(b'Exif\x00\x00'):
        payload = payload[6:]
    if len(payload) < 8 or payload[:2] not in (b'II', b'MM'):
        return None
    fmt = '<' if payload[:2] == b'II' else '>'

    def u16(offset):
        if offset < 0 or offset + 2 > len(payload):
            raise ValueError('EXIF offset outside segment')
        return struct.unpack_from(fmt + 'H', payload, offset)[0]

    def u32(offset):
        if offset < 0 or offset + 4 > len(payload):
            raise ValueError('EXIF offset outside segment')
        return struct.unpack_from(fmt + 'I', payload, offset)[0]

    def ifd(offset):
        count = u16(offset)
        if offset + 2 + count * 12 + 4 > len(payload):
            raise ValueError('EXIF IFD outside segment')
        tags = {}
        widths = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1,
                  8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
        for i in range(count):
            at = offset + 2 + i * 12
            tag, kind = u16(at), u16(at + 2)
            unit = widths.get(kind)
            if unit is None:
                continue
            length = u32(at + 4) * unit
            start = at + 8 if length <= 4 else u32(at + 8)
            # Skip corrupt tags individually; don't discard UserComment due to
            # unrelated malformed thumbnail/maker metadata.
            if start <= len(payload) and length <= len(payload) - start:
                tags[tag] = (kind, payload[start:start + length])
        return tags

    try:
        if u16(2) != 42:
            return None
        top = ifd(u32(4))
        pointer = top.get(0x8769)
        if not pointer or pointer[0] != 4 or len(pointer[1]) < 4:
            return None
        sub = ifd(struct.unpack(fmt + 'I', pointer[1][:4])[0])
        tag = sub.get(0x9286)
        return decode_exif_bytes(tag[1]) if tag else None
    except (ValueError, struct.error, UnicodeError):
        return None


def read_png_metadata(path: Path) -> tuple[int, int, dict[str, str], list[str]]:
    """Scan chunk headers. Do not decode/recompress image pixels."""
    entries: dict[str, str] = {}
    comments: list[str] = []
    width = height = 0
    with path.open('rb') as stream:
        if stream.read(8) != PNG_SIG:
            raise PngError('not a PNG')
        while True:
            header = stream.read(8)
            if len(header) != 8:
                raise PngError('truncated PNG or missing IEND')
            size, kind = struct.unpack('>I4s', header)
            if kind == b'IHDR':
                if size < 13:
                    raise PngError('invalid IHDR')
                payload = stream.read(size)
                if len(payload) != size:
                    raise PngError('truncated IHDR')
                width, height = struct.unpack('>II', payload[:8])
                if len(stream.read(4)) != 4:
                    raise PngError('truncated IHDR CRC')
            elif kind in TEXT_TYPES or kind == b'eXIf':
                if size > MAX_TEXT:
                    raise PngError('oversized PNG metadata chunk')
                payload = stream.read(size)
                crc_bytes = stream.read(4)
                if len(payload) != size or len(crc_bytes) != 4:
                    raise PngError('truncated PNG metadata chunk')
                crc = zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff
                if struct.unpack('>I', crc_bytes)[0] != crc:
                    raise PngError('bad PNG metadata chunk CRC')
                if kind == b'eXIf':
                    comment = extract_exif_usercomment(payload)
                    if comment:
                        comments.append(comment)
                else:
                    pair = text_from_chunk(kind, payload)
                    if pair:
                        entries[pair[0]] = pair[1]
            else:
                stream.seek(size, 1)
                if len(stream.read(4)) != 4:
                    raise PngError('truncated PNG chunk')
            if kind == b'IEND':
                break
        if not width or not height:
            raise PngError('missing IHDR or invalid image dimensions')
    return width, height, entries, comments


def classify_a1111(raw: str) -> str:
    """Same FULL/PARTIAL/NONE evidence categories as JPG scanner 1.00."""
    if not raw or not raw.strip():
        return 'NONE'
    if (GENERAL_FIELDS['steps'].search(raw) and GENERAL_FIELDS['sampler'].search(raw)
            and GENERAL_FIELDS['seed'].search(raw)):
        return 'FULL'
    if POSTPROCESS_FIELDS.search(raw) or DISTINCT_FIELDS.search(raw):
        return 'PARTIAL'
    if sum(bool(p.search(raw)) for p in GENERAL_FIELDS.values()) >= 2:
        return 'PARTIAL'
    return 'NONE'


def select_a1111(entries: dict[str, str], comments: list[str]) -> tuple[str | None, str, str]:
    """parameters wins when it is recognizable; never overwrite existing text.

    If parameters is present but unrecognizable, do NOT silently substitute
    EXIF from elsewhere: those could describe different processing histories.
    """
    if entries.get('parameters'):
        raw = entries['parameters']
        return (raw, 'parameters', classify_a1111(raw))
    for raw in comments:
        status = classify_a1111(raw)
        if status != 'NONE':
            return raw, 'EXIF UserComment', status
    return None, '', 'NONE'

def split_settings(line: str) -> list[tuple[str, str]]:
    """Split A1111 comma-delimited settings outside quotes/brackets/JSON."""
    tokens, buf, quote, brackets, escaped = [], [], None, [], False
    matches = {'{': '}', '[': ']', '(': ')'}
    for char in line:
        if quote is not None:
            buf.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
        elif char in matches:
            brackets.append(matches[char])
        elif brackets and char == brackets[-1]:
            brackets.pop()
        elif char == ',' and not brackets:
            tokens.append(''.join(buf).strip())
            buf = []
            continue
        buf.append(char)
    tokens.append(''.join(buf).strip())
    pairs = []
    for token in tokens:
        match = SETTING_KEY.match(token)
        if match:
            pairs.append((match.group(1).strip(), match.group(2).strip()))
        elif pairs:
            key, val = pairs[-1]
            pairs[-1] = key, val + ', ' + token
    return pairs


def parse_a1111(raw: str):
    normalized = raw
    if not SETTINGS_START.search(normalized):
        normalized = re.sub(r'[ \t]*[,\.][ \t,\.]{0,6}(?=Negative prompt:)',
                            '\n', normalized, flags=re.I)
        normalized = re.sub(r'[ \t]*[,\.][ \t,\.]{0,6}(?=Steps:\s*\d+)',
                            '\n', normalized, flags=re.I)
    starts = list(SETTINGS_START.finditer(normalized))
    if not starts:
        raise ValueError("A1111 generation metadata lacks a recognizable 'Steps:' line")
    before = normalized[:starts[-1].start()].rstrip('\r\n')
    settings = normalized[starts[-1].start():].strip()
    neg = re.search(r'(?mi)^Negative prompt:\s*', before)
    if neg:
        positive, negative = before[:neg.start()].rstrip('\r\n'), before[neg.end():].strip()
    else:
        positive, negative = before.strip(), ''
    pairs = split_settings(settings)
    fields = dict(pairs)
    if not field(fields, 'Steps'):
        raise ValueError('could not parse A1111 Steps field')
    return positive, negative, pairs, fields


def field(fields: dict[str, str], name: str, default: str = '') -> str:
    return next((v for k, v in fields.items() if k.casefold() == name.casefold()), default)


def as_number(value: str, fallback: float, kind=float):
    try:
        num = kind(value.strip())
        return num if math.isfinite(num) and abs(num) < 2**53 else fallback
    except (ValueError, OverflowError, AttributeError):
        return fallback


def strip_surrounding_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return value


def extension(name: str, default: str) -> str:
    return name if not name or Path(name).suffix.lower() in ('.safetensors', '.ckpt', '.pt', '.pth') else name + default


SAMPLERS = {
    'euler': 'euler', 'euler a': 'euler_ancestral', 'euler ancestral': 'euler_ancestral',
    'euler cfg++': 'euler_cfg_pp', 'heun': 'heun', 'heunpp2': 'heunpp2',
    'lms': 'lms', 'dpm2': 'dpm_2', 'dpm2 a': 'dpm_2_ancestral',
    'dpm++ 2s a': 'dpmpp_2s_ancestral', 'dpm++ sde': 'dpmpp_sde',
    'dpm++ 2m': 'dpmpp_2m', 'dpm++ 2m sde': 'dpmpp_2m_sde',
    'dpm++ 2m sde heun': 'dpmpp_2m_sde_heun', 'dpm++ 3m sde': 'dpmpp_3m_sde',
    'dpm++ 2m cfg++': 'dpmpp_2m_cfg_pp', 'dpm fast': 'dpm_fast',
    'dpm adaptive': 'dpm_adaptive', 'ddim': 'ddim', 'ddpm': 'ddpm',
    'unipc': 'uni_pc', 'uni_pc': 'uni_pc', 'uni-pc': 'uni_pc',
    'unipc bh2': 'uni_pc_bh2', 'lcm': 'lcm', 'deis': 'deis',
    'ipndm': 'ipndm', 'ipndm_v': 'ipndm_v',
}
SCHEDULES = {
    'karras': 'karras', 'exponential': 'exponential', 'sgm uniform': 'sgm_uniform',
    'sgm_uniform': 'sgm_uniform', 'simple': 'simple', 'ddim uniform': 'ddim_uniform',
    'ddim_uniform': 'ddim_uniform', 'normal': 'normal', 'beta': 'beta',
    'polyexponential': 'polyexponential', 'linear quadratic': 'linear_quadratic',
}


def sampler_scheduler(fields: dict[str, str], warnings: list[str]) -> tuple[str, str]:
    raw = field(fields, 'Sampler').strip()
    if not raw:
        raise ValueError('A1111 sampler missing; will not insert an invented sampler')
    sampler_label = raw
    trailing = None
    for suffix in ('Karras', 'Exponential', 'SGM Uniform', 'Simple', 'DDIM Uniform', 'Beta'):
        if sampler_label.casefold().endswith(' ' + suffix.casefold()):
            sampler_label = sampler_label[:-(len(suffix) + 1)].strip()
            trailing = SCHEDULES[suffix.casefold()]
            break
    sampler = SAMPLERS.get(re.sub(r'\s+', ' ', sampler_label).casefold())
    if sampler is None:
        raise ValueError(f'Unsupported A1111 sampler {raw!r}; no fabricated substitution')
    raw_schedule = field(fields, 'Schedule type', field(fields, 'Scheduler')).strip().casefold()
    if raw_schedule in ('', 'automatic', 'auto'):
        schedule = trailing or 'normal'
        if not trailing:
            warnings.append("A1111 schedule automatic/unspecified; ComfyUI 'normal' is a fallback")
    else:
        schedule = SCHEDULES.get(raw_schedule)
        if schedule is None:
            # Explicitly approved 1.01 compatibility carveout.
            schedule = 'normal'
            warnings.append(f"Unknown A1111 schedule {raw_schedule!r}; using 'normal' (original preserved)")
        if trailing and trailing != schedule:
            warnings.append('Sampler schedule suffix and Schedule type disagree')
    return sampler, schedule


def addnet_loras(fields: dict[str, str], warnings: list[str]) -> list[dict[str, Any]]:
    if field(fields, 'AddNet Enabled').casefold() not in ('true', '1', 'yes'):
        return []
    indices = sorted({int(m.group(1)) for key in fields
                      if (m := re.fullmatch(r'AddNet Model (\d+)', key, re.I))})
    found = []
    for i in indices:
        module = field(fields, f'AddNet Module {i}').strip()
        raw_name = strip_surrounding_quotes(field(fields, f'AddNet Model {i}').strip())
        if not raw_name or raw_name.casefold() in ('none', 'no model'):
            continue
        if module.casefold() != 'lora':
            warnings.append(f'AddNet slot {i}: unsupported module {module!r}')
            continue
        match = re.fullmatch(r'(.*?)\(([0-9a-fA-F]{8,64})\)', raw_name)
        name = match.group(1).strip() if match else raw_name
        a_str = field(fields, f'AddNet Weight A {i}', field(fields, f'AddNet Weight {i}')).strip()
        b_str = field(fields, f'AddNet Weight B {i}').strip()
        try:
            a, b = float(a_str), float(b_str)
            if not (math.isfinite(a) and math.isfinite(b)):
                raise ValueError('nonfinite')
        except ValueError:
            warnings.append(f'AddNet slot {i}: missing or invalid A/B weights')
            continue
        found.append({'name': name, 'strength_model': a, 'strength_clip': b,
                      'source': 'AddNet', 'index': i})
        if match:
            warnings.append(f'AddNet slot {i} uses hash {match.group(2)}; verify file name')
    return found


class Graph:
    """Native ComfyUI legacy canvas plus API prompt; Note nodes are canvas only."""
    def __init__(self):
        self.nodes: list[dict] = []
        self.links: list[list] = []
        self.api: dict[str, dict] = {}

    def node(self, name: str, xy: tuple[int, int], outputs: list[str],
             widgets: list | None = None, values: dict | None = None,
             title: str | None = None, *, frontend_only: bool = False) -> int:
        nid = len(self.nodes) + 1
        size = [335, 265] if name == 'KSampler' else (
            [425, 205] if name == 'CLIPTextEncode' else (
            [440, 280] if name == 'Note' else [320, 125 if not widgets else 180]))
        node = {
            'id': nid, 'type': name, 'pos': list(xy), 'size': size,
            'flags': {}, 'order': nid - 1, 'mode': 0, 'inputs': [],
            'outputs': [{'name': typ, 'type': typ, 'slot_index': i, 'links': []}
                        for i, typ in enumerate(outputs)],
            'properties': {'Node name for S&R': name},
            'widgets_values': widgets or [],
        }
        if title:
            node['title'] = title
        self.nodes.append(node)
        if not frontend_only:
            self.api[str(nid)] = {
                'inputs': dict(values or {}), 'class_type': name,
                '_meta': {'title': title or name},
            }
        return nid

    def note(self, text: str, xy: tuple[int, int], title='A1111 metadata (informational)') -> int:
        nid = self.node('Note', xy, [], [text], title=title, frontend_only=True)
        self.nodes[nid - 1]['size'] = [min(680, max(380, len(max(text.splitlines(), key=len, default='')) * 6)),
                                       min(720, max(220, len(text.splitlines()) * 18 + 80))]
        return nid

    def connect(self, source: int, slot: int, target: int, name: str, typ: str):
        origin, dest = self.nodes[source - 1], self.nodes[target - 1]
        if origin['outputs'][slot]['type'] != typ:
            raise ValueError('invalid graph output link type')
        lid = len(self.links) + 1
        index = len(dest['inputs'])
        dest['inputs'].append({'name': name, 'type': typ, 'link': lid})
        origin['outputs'][slot]['links'].append(lid)
        self.links.append([lid, source, slot, target, index, typ])
        self.api[str(target)]['inputs'][name] = [str(source), slot]

    def export(self):
        return ({'last_node_id': len(self.nodes), 'last_link_id': len(self.links),
                 'nodes': self.nodes, 'links': self.links, 'groups': [],
                 'config': {}, 'extra': {}, 'version': 0.4}, self.api)


def detect_generation_type(fields: dict[str, str], denoise: float, hires: bool,
                           mode: str, warnings: list[str]) -> tuple[str, str]:
    if mode != 'auto':
        return mode, 'explicit --mode override'
    hints = [name for name in ('Masked content', 'Mask mode', 'Inpaint area',
                              'Inpaint at full resolution', 'Inpaint full res',
                              'Only masked padding, pixels', 'Inpainting mask invert')
             if field(fields, name)]
    if hints:
        warnings.append('Inpainting inferred from fields: ' + ', '.join(hints))
        return 'inpaint', 'inpainting fields'
    if field(fields, 'Mask blur'):
        warnings.append('Mask blur alone does not prove inpainting; using img2img if indicated')
    if hires:
        return 'txt2img', 'hires fix settings'
    if denoise < 1.0:
        warnings.append('img2img inferred from denoising strength < 1')
        return 'img2img', 'denoising strength < 1'
    return 'txt2img', 'no img2img/inpainting indicators'


def informative_graph(raw: str, category: str, reason: str, source: str):
    """Native canvas-only Note node; no misleading API execution nodes."""
    g = Graph()
    prefix = ('A1111 ' + category + ' metadata (' + source + ')\n'
              'No generation workflow has been reconstructed.\n'
              'Reason: ' + reason + '\n\nRecorded original metadata:\n\n')
    g.note(prefix + raw, (80, 90), f'A1111 {category}: informational only')
    canvas, api = g.export()
    return canvas, api, {'generation_type': 'informational', 'warnings': [reason],
                         'classification': category}


def build_graph(raw: str, png_width: int, png_height: int,
                model_ext: str, mode: str = 'auto', source: str = 'parameters'):
    """Best-effort functional reconstruction; unrepresented original fields in Note."""
    category = classify_a1111(raw)
    if category == 'NONE':
        raise ValueError('no recognizable A1111 information')
    if category == 'PARTIAL':
        return informative_graph(raw, 'PARTIAL', 'Generation parameters incomplete', source)
    try:
        positive, negative, pairs, fields = parse_a1111(raw)
        warnings: list[str] = []
        sampler, schedule = sampler_scheduler(fields, warnings)
        try:
            steps = int(field(fields, 'Steps'))
            seed = int(field(fields, 'Seed'))
            cfg = float(field(fields, 'CFG scale'))
        except (ValueError, TypeError) as exc:
            raise ValueError('Steps, Seed or CFG scale missing/invalid') from exc
        if not (steps >= 1 and 0 <= seed < 2**64 and math.isfinite(cfg) and cfg >= 0):
            raise ValueError('Steps/Seed/CFG scale out of range')
        size = re.fullmatch(r'\s*(\d+)\s*[xX]\s*(\d+)\s*', field(fields, 'Size'))
        if not size:
            raise ValueError('A1111 Size missing/invalid; cannot infer generation resolution')
        bw, bh = map(int, size.groups())
        if min(bw, bh) < 8:
            raise ValueError('invalid generation dimensions')
    except (ValueError, OverflowError) as exc:
        # FULL is an evidence classification, not a guarantee of completeness.
        # Information-only is safer than dropping the metadata or making up nodes.
        return informative_graph(raw, 'FULL', str(exc), source)

    represented = {'steps', 'sampler', 'seed', 'cfg scale', 'size', 'model'}
    g = Graph()
    loras: list[dict] = []

    def lora_replacer(match):
        name = match.group(1)
        model_weight = float(match.group(2))
        clip_weight = float(match.group(3)) if match.group(3) else model_weight
        obj = {'name': name, 'strength_model': model_weight, 'strength_clip': clip_weight}
        if obj not in loras:
            loras.append(obj)
        return ''

    pos_text = LORA_REGEX.sub(lora_replacer, positive).strip()
    neg_text = LORA_REGEX.sub(lora_replacer, negative).strip()
    if re.search(r'<lora:', pos_text + neg_text, re.I):
        warnings.append('Unrecognized LoRA tags retained in original parameters')
    for addon in addnet_loras(fields, warnings):
        represented.update({'addnet enabled', f'addnet module {addon["index"]}',
                            f'addnet model {addon["index"]}', f'addnet weight a {addon["index"]}',
                            f'addnet weight b {addon["index"]}', f'addnet weight {addon["index"]}'})
        if not any(item['name'] == addon['name'] and
                   item['strength_model'] == addon['strength_model'] and
                   item['strength_clip'] == addon['strength_clip'] for item in loras):
            loras.append(addon)
    if field(fields, 'Schedule type'):
        represented.add('schedule type')
    if field(fields, 'Scheduler'):
        represented.add('scheduler')

    ckpt = field(fields, 'Model').strip()
    if not ckpt:
        ckpt = 'SELECT_CHECKPOINT.safetensors'
        warnings.append('Checkpoint unrecorded; select model manually in ComfyUI')
    elif Path(ckpt).suffix.lower() not in ('.safetensors', '.ckpt'):
        ckpt = extension(ckpt, model_ext)
        warnings.append('Checkpoint filename extension guessed; verify installed model name')
    loader = g.node('CheckpointLoaderSimple', (25, 65), ['MODEL', 'CLIP', 'VAE'],
                    [ckpt], {'ckpt_name': ckpt})
    model_src, model_out, clip_src, clip_out = loader, 0, loader, 1
    vae_src, vae_out = loader, 2
    vae_name = field(fields, 'VAE').strip()
    if vae_name:
        represented.add('vae')
    if vae_name and vae_name.casefold() not in ('automatic', 'none'):
        vae_file = extension(vae_name, model_ext)
        vl = g.node('VAELoader', (25, 415), ['VAE'], [vae_file], {'vae_name': vae_file})
        vae_src, vae_out = vl, 0
    skip = int(as_number(field(fields, 'Clip skip', '1'), 1, int))
    if field(fields, 'Clip skip'):
        represented.add('clip skip')
    if skip > 1:
        cl = g.node('CLIPSetLastLayer', (365, 415), ['CLIP'], [-skip],
                    {'stop_at_clip_layer': -skip})
        g.connect(clip_src, clip_out, cl, 'clip', 'CLIP')
        clip_src, clip_out = cl, 0
        warnings.append('CLIP skip interpretation may differ by model')
    for i, item in enumerate(loras):
        name = extension(item['name'], model_ext)
        node = g.node('LoraLoader', (400 + 355 * i, 65), ['MODEL', 'CLIP'],
                      [name, item['strength_model'], item['strength_clip']],
                      {'lora_name': name, 'strength_model': item['strength_model'],
                       'strength_clip': item['strength_clip']})
        g.connect(model_src, model_out, node, 'model', 'MODEL')
        g.connect(clip_src, clip_out, node, 'clip', 'CLIP')
        model_src, model_out = node, 0
        clip_src, clip_out = node, 1
    x = 540 + 355 * len(loras)
    pos = g.node('CLIPTextEncode', (x, 25), ['CONDITIONING'],
                 [pos_text], {'text': pos_text}, 'A1111 Positive')
    neg = g.node('CLIPTextEncode', (x, 305), ['CONDITIONING'],
                 [neg_text], {'text': neg_text}, 'A1111 Negative')
    g.connect(clip_src, clip_out, pos, 'clip', 'CLIP')
    g.connect(clip_src, clip_out, neg, 'clip', 'CLIP')
    hires_keys = ('Hires upscale', 'Hires resize', 'Hires upscaler', 'Hires steps')
    hires = any(field(fields, k) for k in hires_keys)
    denoise = float(as_number(field(fields, 'Denoising strength', '1'), 1.0))
    if field(fields, 'Denoising strength'):
        represented.add('denoising strength')
    if not 0 <= denoise <= 1:
        return informative_graph(raw, 'FULL', 'Denoising strength outside range', source)
    gen_type, _ = detect_generation_type(fields, denoise, hires, mode, warnings)
    needs_image = gen_type in ('img2img', 'inpaint')
    if needs_image:
        image = g.node('LoadImage', (x, 570), ['IMAGE', 'MASK'],
                       ['SELECT_SOURCE_IMAGE.png', 'image'],
                       {'image': 'SELECT_SOURCE_IMAGE.png'},
                       'REPLACE: original input image')
        warnings.append('Original img2img source image and resize/crop unavailable')
        if gen_type == 'inpaint':
            mask = g.node('LoadImageMask', (x, 845), ['MASK'],
                          ['SELECT_MASK_IMAGE.png', 'red'],
                          {'image': 'SELECT_MASK_IMAGE.png', 'channel': 'red'},
                          'REPLACE: original mask (white = paint)')
            if 'not masked' in field(fields, 'Mask mode').casefold():
                inv = g.node('InvertMask', (x + 350, 845), ['MASK'], [], {},
                             'A1111: Inpaint not masked')
                g.connect(mask, 0, inv, 'mask', 'MASK')
                mask = inv
            latent = g.node('VAEEncodeForInpaint', (x + 835, 635), ['LATENT'],
                            [0], {'grow_mask_by': 0}, 'A1111 Inpainting Encode')
            g.connect(image, 0, latent, 'pixels', 'IMAGE')
            g.connect(vae_src, vae_out, latent, 'vae', 'VAE')
            g.connect(mask, 0, latent, 'mask', 'MASK')
            warnings.append('Original inpaint mask/cropping/compositing not reconstructable')
            if field(fields, 'Mask mode'):
                represented.add('mask mode')
        else:
            latent = g.node('VAEEncode', (x + 835, 635), ['LATENT'], [], {},
                            'A1111 img2img Encode')
            g.connect(image, 0, latent, 'pixels', 'IMAGE')
            g.connect(vae_src, vae_out, latent, 'vae', 'VAE')
    else:
        latent = g.node('EmptyLatentImage', (x, 585), ['LATENT'],
                        [bw, bh, 1], {'width': bw, 'height': bh, 'batch_size': 1})
    first_denoise = 1.0 if hires and not needs_image else denoise
    sampler_node = g.node('KSampler', (x + 525, 150), ['LATENT'],
                          [seed, 'fixed', steps, cfg, sampler, schedule, first_denoise],
                          {'seed': seed, 'steps': steps, 'cfg': cfg,
                           'sampler_name': sampler, 'scheduler': schedule,
                           'denoise': first_denoise},
                          'A1111 Sampling / First Pass' if hires else 'A1111 Sampling')
    for sid, slot, name, typ in ((model_src, model_out, 'model', 'MODEL'),
                                  (pos, 0, 'positive', 'CONDITIONING'),
                                  (neg, 0, 'negative', 'CONDITIONING'),
                                  (latent, 0, 'latent_image', 'LATENT')):
        g.connect(sid, slot, sampler_node, name, typ)
    final_latent = sampler_node
    final_x = x + 970
    if hires:
        factor = float(as_number(field(fields, 'Hires upscale', '2'), 2))
        if factor <= 0:
            return informative_graph(raw, 'FULL', 'Invalid Hires upscale factor', source)
        target_w, target_h = round(bw * factor), round(bh * factor)
        resize = re.search(r'(\d+)\s*[xX]\s*(\d+)', field(fields, 'Hires resize'))
        if resize and min(map(int, resize.groups())) > 0:
            target_w, target_h = map(int, resize.groups())
        if min(target_w, target_h) < 8:
            return informative_graph(raw, 'FULL', 'Invalid Hires target size', source)
        upscaler = field(fields, 'Hires upscaler').strip()
        methods = {'latent (bicubic)': 'bicubic', 'latent (nearest-exact)': 'nearest-exact',
                   'latent (bislerp)': 'bislerp'}
        method = methods.get(upscaler.casefold())
        if method and (not field(fields, 'Denoising strength') or
                       LORA_REGEX.search(field(fields, 'Hires prompt') +
                                         field(fields, 'Hires negative prompt'))):
            method = None
        if method is None:
            warnings.append('Original Hires.fix upscaler not reconstructed: ' + (upscaler or 'unspecified'))
            g.nodes[sampler_node - 1]['title'] = 'PARTIAL: original A1111 Hires.fix not reconstructed'
            g.api[str(sampler_node)]['_meta']['title'] = g.nodes[sampler_node - 1]['title']
            # Do not mark as represented: its raw values will appear in the Note.
        else:
            represented.update(k.casefold() for k in hires_keys if field(fields, k))
            represented.add('hires upscaler')
            up = g.node('LatentUpscale', (final_x, 490), ['LATENT'],
                        [method, target_w, target_h, 'disabled'],
                        {'upscale_method': method, 'width': target_w,
                         'height': target_h, 'crop': 'disabled'})
            g.connect(final_latent, 0, up, 'samples', 'LATENT')
            hsteps = int(as_number(field(fields, 'Hires steps', str(steps)), steps, int))
            if hsteps < 1:
                return informative_graph(raw, 'FULL', 'Invalid Hires steps', source)
            hpos, hneg = pos, neg
            if field(fields, 'Hires prompt'):
                # Handle separate positive conditioning only when no Hires LoRA tag.
                hp = strip_surrounding_quotes(field(fields, 'Hires prompt'))
                hp = LORA_REGEX.sub('', hp).strip()
                hpos = g.node('CLIPTextEncode', (final_x + 385, -110), ['CONDITIONING'],
                              [hp], {'text': hp}, 'A1111 Hires Positive')
                g.connect(clip_src, clip_out, hpos, 'clip', 'CLIP')
                represented.add('hires prompt')
            if field(fields, 'Hires negative prompt'):
                hn = strip_surrounding_quotes(field(fields, 'Hires negative prompt'))
                hn = LORA_REGEX.sub('', hn).strip()
                hneg = g.node('CLIPTextEncode', (final_x + 385, 175), ['CONDITIONING'],
                              [hn], {'text': hn}, 'A1111 Hires Negative')
                g.connect(clip_src, clip_out, hneg, 'clip', 'CLIP')
                represented.add('hires negative prompt')
            second = g.node('KSampler', (final_x + 825, 405), ['LATENT'],
                            [seed, 'fixed', hsteps, cfg, sampler, schedule, denoise],
                            {'seed': seed, 'steps': hsteps, 'cfg': cfg,
                             'sampler_name': sampler, 'scheduler': schedule,
                             'denoise': denoise}, 'A1111 Hires Second Pass')
            for sid, slot, name, typ in ((model_src, model_out, 'model', 'MODEL'),
                                          (hpos, 0, 'positive', 'CONDITIONING'),
                                          (hneg, 0, 'negative', 'CONDITIONING'),
                                          (up, 0, 'latent_image', 'LATENT')):
                g.connect(sid, slot, second, name, typ)
            final_latent = second
            final_x += 1250
    decoded = g.node('VAEDecode', (final_x, 100), ['IMAGE'])
    g.connect(final_latent, 0, decoded, 'samples', 'LATENT')
    g.connect(vae_src, vae_out, decoded, 'vae', 'VAE')
    saved = g.node('SaveImage', (final_x + 390, 110), [], ['A1111_Imported'],
                   {'filename_prefix': 'A1111_Imported'})
    g.connect(decoded, 0, saved, 'images', 'IMAGE')

    # Only add notes if actual A1111 fields were not represented in nodes.
    # Built-in Note is frontend-only, hence NOT present in the API 'prompt'.
    leftovers = [(k, v) for k, v in pairs if k.casefold() not in represented]
    if leftovers:
        note_text = ('A1111 fields not reconstructed as functional ComfyUI nodes\n'
                     'These are the recorded values; do not infer missing settings.\n\n' +
                     '\n'.join(k + ': ' + v for k, v in leftovers))
        g.note(note_text, (25, 760), 'A1111 settings not represented by nodes')
    if (png_width, png_height) != (bw, bh) and not hires:
        warnings.append('PNG dimensions differ from generation Size')
    canvas, api = g.export()
    return canvas, api, {'generation_type': gen_type, 'warnings': warnings,
                         'classification': 'FULL'}

def png_text_chunk(key: str, value: str) -> bytes:
    key_bytes = key.encode('latin-1')
    try:
        data = key_bytes + b'\x00' + value.encode('latin-1')
        kind = b'tEXt'
    except UnicodeEncodeError:
        data = key_bytes + b'\x00\x00\x00\x00\x00' + value.encode('utf-8')
        kind = b'iTXt'
    if len(data) > MAX_TEXT:
        raise PngError('ComfyUI or parameters metadata exceeds 16 MiB limit')
    crc = zlib.crc32(data, zlib.crc32(kind)) & 0xffffffff
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', crc)


def copy_n(src, dst, n: int) -> None:
    while n:
        data = src.read(min(n, 1024 * 1024))
        if not data:
            raise PngError('truncated PNG while copying')
        dst.write(data)
        n -= len(data)


def _prepare_png(src, dst, updates: dict[str, str]) -> None:
    if src.read(8) != PNG_SIG:
        raise PngError('not a PNG')
    dst.write(PNG_SIG)
    inserted = False
    while True:
        header = src.read(8)
        if len(header) != 8:
            raise PngError('truncated PNG during metadata update')
        size, kind = struct.unpack('>I4s', header)
        if kind in (b'IDAT', b'IEND') and not inserted:
            for key, val in updates.items():
                dst.write(png_text_chunk(key, val))
            inserted = True
        if kind in TEXT_TYPES:
            if size > MAX_TEXT:
                raise PngError('oversized original PNG text chunk')
            payload = src.read(size + 4)
            if len(payload) != size + 4:
                raise PngError('truncated PNG text chunk')
            if chunk_key(kind, payload[:size]) not in REPLACE_KEYS:
                dst.write(header)
                dst.write(payload)
        else:
            dst.write(header)
            copy_n(src, dst, size + 4)
        if kind == b'IEND':
            break
    if src.read(1):
        raise PngError('PNG has data after IEND (cannot safely preserve)')


def make_unique_backup_path(path: Path) -> Path:
    i = 1
    while True:
        candidate = path.with_name(f'{path.stem}.bak{i}{path.suffix}')
        if not candidate.exists():
            return candidate
        i += 1


def _temp_path(path: Path, marker: str) -> Path:
    return path.with_name(f'.{path.name}.{marker}.{uuid4().hex}.tmp')


def _stream_overwrite_existing(path: Path, staged: Path):
    """Keep original NTFS file object instead of using os.replace()."""
    with staged.open('rb') as inp, path.open('r+b') as out:
        out.seek(0)
        shutil.copyfileobj(inp, out, 1024 * 1024)
        out.truncate(out.tell())
        out.flush()
        os.fsync(out.fileno())


def _check_identity(path: Path, identity: tuple[int, int], creation: int | None):
    stat = path.stat()
    if (stat.st_dev, stat.st_ino) != identity:
        raise OSError('file identity changed: original PNG was replaced')
    if creation is not None and _windows_filetime(path)[0] != creation:
        raise OSError('Windows Date Created changed')


def png_chunk_digest(path: Path, *, exclude_owned: bool = False,
                     exclude_added_parameters: bool = False) -> tuple[str, int]:
    """Hash raw bytes of every non-ComfyUI PNG chunk including EXIF, ICC, IDAT.

    Used by --verify: ensures not just lossless pixels but ALL unrelated PNG
    chunks (including their CRCs) have survived exactly, in order.
    """
    h = hashlib.sha256()
    chunks = 0
    with path.open('rb') as inp:
        if inp.read(8) != PNG_SIG:
            raise PngError('not a PNG')
        while True:
            header = inp.read(8)
            if len(header) != 8:
                raise PngError('truncated PNG digest')
            size, kind = struct.unpack('>I4s', header)
            if kind in TEXT_TYPES:
                if size > MAX_TEXT:
                    raise PngError('oversized text chunk during verification')
                data = inp.read(size + 4)
                if len(data) != size + 4:
                    raise PngError('truncated text during verification')
                key = chunk_key(kind, data[:size])
                skip = (exclude_owned and key in REPLACE_KEYS) or (exclude_added_parameters and key == 'parameters')
            else:
                skip = False
            if not skip:
                h.update(header)
                if kind in TEXT_TYPES:
                    h.update(data)
                else:
                    left = size + 4
                    while left:
                        part = inp.read(min(left, 1024 * 1024))
                        if not part:
                            raise PngError('truncated PNG during verification')
                        h.update(part)
                        left -= len(part)
                chunks += 1
            elif kind not in TEXT_TYPES:
                # Defensive; should not be reached for normal PNG metadata.
                inp.seek(size + 4, 1)
            if kind == b'IEND':
                break
    return h.hexdigest(), chunks


def png_stream_idat_digest(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    count = 0
    with path.open('rb') as fp:
        if fp.read(8) != PNG_SIG:
            raise PngError('not a PNG')
        while True:
            header = fp.read(8)
            if len(header) != 8:
                raise PngError('truncated PNG')
            length, kind = struct.unpack('>I4s', header)
            if kind == b'IDAT':
                left = length
                while left:
                    data = fp.read(min(left, 1024 * 1024))
                    if not data:
                        raise PngError('truncated IDAT')
                    h.update(data)
                    left -= len(data)
                count += 1
            else:
                fp.seek(length, 1)
            if len(fp.read(4)) != 4:
                raise PngError('truncated PNG chunk CRC')
            if kind == b'IEND':
                break
    return h.hexdigest(), count


def validate_graph(workflow: dict, api: dict):
    """Check internal ComfyUI canvas/API consistency; Notes are frontend-only."""
    if workflow.get('version') != 0.4:
        raise ValueError('expected ComfyUI workflow version 0.4')
    nodes, links = workflow.get('nodes'), workflow.get('links')
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise ValueError('missing workflow nodes or links')
    by_id = {node['id']: node for node in nodes}
    if len(by_id) != len(nodes):
        raise ValueError('duplicate node ids')
    for node in nodes:
        idstr = str(node['id'])
        if node['type'] == 'Note':
            if idstr in api:
                raise ValueError('Note is frontend-only and must not be in API prompt')
            if node['inputs'] or node['outputs']:
                raise ValueError('Note has unexpected executable links')
        elif idstr not in api or api[idstr].get('class_type') != node['type']:
            raise ValueError('canvas/API node mismatch')
        if not isinstance(node.get('widgets_values'), list):
            raise ValueError('invalid node widgets_values')
    if len(api) != sum(node['type'] != 'Note' for node in nodes):
        raise ValueError('canvas/API executable node count mismatch')
    used = set()
    for linkid, source, output, target, index, typ in links:
        if linkid in used or source not in by_id or target not in by_id:
            raise ValueError('duplicate link or missing node')
        used.add(linkid)
        a, b = by_id[source], by_id[target]
        if output >= len(a['outputs']) or index >= len(b['inputs']):
            raise ValueError('nonexistent link slot')
        if (a['outputs'][output]['type'] != typ or b['inputs'][index]['type'] != typ
                or b['inputs'][index]['link'] != linkid
                or linkid not in a['outputs'][output]['links']):
            raise ValueError('invalid canvas link')
        if api[str(target)]['inputs'].get(b['inputs'][index]['name']) != [str(source), output]:
            raise ValueError('canvas/API link mismatch')
    # Informational Note-only workflows deliberately have no SaveImage node.
    if api and not any(node['type'] == 'SaveImage' for node in nodes):
        raise ValueError('functional workflow has no SaveImage')
    if not api and (not nodes or any(node['type'] != 'Note' for node in nodes)):
        raise ValueError('informational workflow must contain Note nodes only')


def verify_conversion(path: Path, updates: dict[str, str], original_text: dict[str, str],
                      workflow: dict, api: dict, original_file: Path,
                      original_exif: list[str]):
    validate_graph(workflow, api)
    _, _, after, after_exif = read_png_metadata(path)
    for key, val in original_text.items():
        if key not in REPLACE_KEYS and after.get(key) != val:
            raise ValueError(f'original text field {key!r} was changed')
    for key, val in updates.items():
        if after.get(key) != val:
            raise ValueError(f'new metadata {key!r} did not round-trip exactly')
    if after_exif != original_exif:
        raise ValueError('original EXIF UserComment changed')
    if json.loads(after['workflow']) != workflow or json.loads(after['prompt']) != api:
        raise ValueError('generated ComfyUI JSON changed')
    if 'a1111_import' in after or 'a1111_import' in workflow.get('extra', {}):
        raise ValueError('obsolete a1111_import custom metadata present')
    if png_stream_idat_digest(path) != png_stream_idat_digest(original_file):
        raise ValueError('compressed image data changed')
    # Compare all non-owned chunk bytes (including EXIF, ICC and their CRCs).
    # The only permissible extra chunk is a newly created 'parameters' field.
    expected = png_chunk_digest(original_file, exclude_owned=True)
    actual = png_chunk_digest(path, exclude_owned=True,
                              exclude_added_parameters=('parameters' in updates))
    if expected != actual:
        raise ValueError('original PNG chunk bytes (outside owned fields) changed')


def write_png_with_metadata(path: Path, updates: dict[str, str], backup: bool,
                            times: OriginalTimes, verify=False, before_entries=None,
                            before_exif=None, workflow=None, api=None):
    """Stage new file, retain full rollback, then rewrite SAME file identity."""
    stat = path.stat()
    identity = stat.st_dev, stat.st_ino
    created = times.windows_filetimes[0] if times.windows_filetimes is not None else None
    fd, name = tempfile.mkstemp(prefix='.a1111_to_comfy_', suffix='.tmp', dir=str(path.parent))
    staged = Path(name)
    rollback: Path | None = None
    attempted = False
    recovered = False
    retained_backup = False
    try:
        with path.open('rb') as src, os.fdopen(fd, 'wb') as dst:
            fd = -1
            _prepare_png(src, dst, updates)
            dst.flush()
            os.fsync(dst.fileno())
        rollback = make_unique_backup_path(path) if backup else _temp_path(path, 'rollback')
        with path.open('rb') as src, rollback.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(path, rollback)
        restore_times(rollback, times)
        retained_backup = backup
        attempted = True
        _stream_overwrite_existing(path, staged)
        if verify:
            verify_conversion(path, updates, before_entries, workflow, api, rollback,
                              before_exif)
        restore_times(path, times)
        _check_identity(path, identity, created)
        assert_times(path, times)
    except BaseException as error:
        if attempted and rollback and rollback.exists():
            try:
                _stream_overwrite_existing(path, rollback)
                restore_times(path, times)
                _check_identity(path, identity, created)
                recovered = True
            except BaseException as rollback_error:
                raise OSError(f'update failed ({error}); ROLLBACK ALSO FAILED '
                              f'({rollback_error}); original bytes are at {rollback}') from error
        raise
    finally:
        if fd != -1:
            os.close(fd)
        if staged.exists():
            staged.unlink()
        if (rollback and rollback.exists() and not retained_backup and
                (not attempted or recovered or sys.exc_info()[0] is None)):
            rollback.unlink()


def iter_pngs(path: Path, recursive: bool):
    if path.is_file():
        if path.suffix.lower() != '.png':
            raise ValueError('input file must be .png')
        yield path
    elif path.is_dir():
        for item in (path.rglob('*') if recursive else path.iterdir()):
            if (item.is_file() and not item.is_symlink() and item.suffix.lower() == '.png'
                    and not BACKUP_NAME.search(item.name)):
                yield item
    else:
        raise ValueError('input path does not exist')


class Progress:
    def __init__(self, total: int, enabled: bool, dry_run: bool):
        self.total = total
        self.enabled = enabled
        self.dry_run = dry_run
        self.terminal = sys.stdout.isatty()
        self.last_width = 0

    def detail(self, text: str):
        if self.enabled and self.terminal and self.last_width:
            sys.stdout.write('\r' + ' ' * self.last_width + '\r')
        print(text)
        self.last_width = 0

    def update(self, index: int, counts: dict[str, int]):
        if not self.enabled:
            return
        percent = 100 * index / self.total if self.total else 100
        width = shutil.get_terminal_size(fallback=(100, 24)).columns
        label = (f' {index}/{self.total} files ({percent:5.1f}%) | '
                 f'{counts["converted"]} converted, {counts["skipped"]} skipped, '
                 f'{counts["failed"]} failed')
        count = min(32, max(3, width - len(label) - 4))
        done = round(count * index / self.total) if self.total else count
        display = '[' + '#' * done + '-' * (count - done) + ']' + label
        if self.terminal:
            sys.stdout.write('\r' + display + ' ' * max(0, self.last_width - len(display)))
            sys.stdout.flush()
            self.last_width = len(display)
        elif index == self.total:
            print(display)

    def finish(self):
        if self.enabled and self.terminal and self.last_width:
            print()
            self.last_width = 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--version', action='version',
                        version=f'convert_metadata_a1111_to_comfyui.py {VERSION}')
    parser.add_argument('path', type=Path, help='PNG file or directory of PNGs')
    parser.add_argument('--recursive', '-r', action='store_true', help='process subdirectories')
    parser.add_argument('--backup', action='store_true',
                        help='keep original backups (image.bak1.png, image.bak2.png, ...)')
    parser.add_argument('--dry-run', action='store_true', help='preview; never write PNGs or reports')
    parser.add_argument('--overwrite', action='store_true',
                        help='replace existing ComfyUI workflow/prompt metadata')
    parser.add_argument('--model-ext', choices=('.safetensors', '.ckpt'),
                        default='.safetensors', help='extension for unqualified model names')
    parser.add_argument('--mode', choices=('auto', 'txt2img', 'img2img', 'inpaint'),
                        default='auto', help='override generation mode where ambiguous')
    parser.add_argument('--verbose', '-v', action='store_true', help='print each file result and notes')
    parser.add_argument('--no-progress', action='store_true', help='disable progress display')
    parser.add_argument('--quiet', action='store_true', help='print only errors and totals')
    parser.add_argument('--verify', action='store_true',
                        help='verify output metadata, ComfyUI JSON, and compressed IDAT')
    parser.add_argument('--report', type=Path, metavar='FILE', help='save per-file CSV report')
    args = parser.parse_args()
    if args.dry_run and args.report:
        parser.error('--dry-run never writes files; omit --report')
    try:
        candidates = sorted(iter_pngs(args.path.expanduser(), args.recursive),
                            key=lambda p: str(p).casefold())
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    counts = {'converted': 0, 'skipped': 0, 'failed': 0}
    types = {'txt2img': 0, 'img2img': 0, 'inpaint': 0, 'informational': 0}
    category_counts = {'FULL': 0, 'PARTIAL': 0, 'NONE': 0}
    rows = []
    progress = Progress(len(candidates), not args.quiet and not args.no_progress, args.dry_run)
    if not args.quiet:
        print(f'Found {len(candidates)} PNG file(s); '
              f'{"dry-run" if args.dry_run else "conversion"} mode (generation mode: {args.mode}).')
    for index, path in enumerate(candidates, 1):
        row = {'file': str(path), 'status': '', 'classification': '',
               'source': '', 'generation_type': '', 'nodes': 0,
               'warnings': '', 'error': ''}
        try:
            times = capture_times(path) if not args.dry_run else None
            width, height, entries, comments = read_png_metadata(path)
            raw, source, category = select_a1111(entries, comments)
            row['classification'], row['source'] = category, source
            category_counts[category] += 1
            if category == 'NONE':
                skip = 'no recognizable A1111 metadata'
            elif not args.overwrite and 'workflow' in entries:
                skip = 'ComfyUI workflow exists'
            elif not args.overwrite and 'prompt' in entries:
                skip = 'ComfyUI prompt metadata exists'
            else:
                skip = None
            if skip:
                counts['skipped'] += 1
                row['status'] = 'skipped'
                row['warnings'] = skip
                if args.verbose and not args.quiet:
                    progress.detail(f'SKIP ({category}; {skip}): {path}')
            else:
                workflow, api, diag = build_graph(raw, width, height, args.model_ext,
                                                  args.mode, source)
                validate_graph(workflow, api)
                updates = {'workflow': json.dumps(workflow, ensure_ascii=True, separators=(',', ':')),
                           'prompt': json.dumps(api, ensure_ascii=True, separators=(',', ':'))}
                if source == 'EXIF UserComment' and 'parameters' not in entries:
                    updates['parameters'] = raw
                if not args.dry_run:
                    write_png_with_metadata(path, updates, args.backup, times,
                                            args.verify, entries, comments, workflow, api)
                counts['converted'] += 1
                gen_type = diag['generation_type']
                types[gen_type] += 1
                row.update({'status': 'would_convert' if args.dry_run else 'converted',
                            'generation_type': gen_type, 'nodes': len(workflow['nodes']),
                            'warnings': ' | '.join(diag['warnings'])})
                if args.verbose and not args.quiet:
                    progress.detail(f'{"WOULD CONVERT" if args.dry_run else "CONVERTED"}: '
                                    f'{path} ({category}; {source}; {len(workflow["nodes"])} nodes; '
                                    f'{gen_type})')
                    for warning in diag['warnings']:
                        progress.detail('    NOTE: ' + warning)
        except (OSError, ValueError, UnicodeError, PngError) as exc:
            counts['failed'] += 1
            row.update({'status': 'failed', 'error': str(exc)})
            if progress.terminal and progress.enabled and progress.last_width:
                sys.stdout.write('\r' + ' ' * progress.last_width + '\r')
                sys.stdout.flush()
                progress.last_width = 0
            print(f'ERROR: {path}: {exc}', file=sys.stderr)
        finally:
            if args.report:
                rows.append(row)
            progress.update(index, counts)
    progress.finish()
    if args.report:
        try:
            outpath = args.report.expanduser()
            with outpath.open('w', newline='', encoding='utf-8-sig') as out:
                names = ('file', 'status', 'classification', 'source',
                         'generation_type', 'nodes', 'warnings', 'error')
                writer = csv.DictWriter(out, fieldnames=names)
                writer.writeheader()
                writer.writerows(rows)
            if not args.quiet:
                print(f'CSV report: {outpath}')
        except OSError as exc:
            counts['failed'] += 1
            print(f'ERROR: cannot write CSV report {args.report}: {exc}', file=sys.stderr)
    print('Summary: ' + ', '.join(f'{k}={v}' for k, v in counts.items()))
    if not args.quiet and counts['converted']:
        print('A1111 classifications: ' + ', '.join(f'{k}={v}' for k, v in category_counts.items()))
        print('Detected workflow types: ' + ', '.join(f'{k}={v}' for k, v in types.items()))
    return 1 if counts['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
