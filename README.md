<img src="images/image_library_rescue_banner.jpg" alt="A1111 to ComfyUI Metadata Converter" width="100%">


# A1111 → ComfyUI PNG Metadata Converter
**A standalone Python script that converts A1111 metadata in existing PNG files into ComfyUI-compatible metadata and workflows.**

*Version 1.02* · Python 3.9+ · *Windows 10/11 and macOS*


## What Is This? 
Got a ton of images you created with A1111, but your ComfyUI image management tools can't make much sense of the metadata in them? That's because A1111 and ComfyUI store their image and workflow information differently, and nobody wants to drag thousands of images into ComfyUI one by one just to fix that. This handy little Python script scans your PNGs in bulk, reads whatever A1111 metadata it finds (even if it's tucked away in EXIF), translates it into ComfyUI-friendly metadata, and adds it right into your existing image files. Your original images and A1111 metadata are left untouched, and when there isn't enough information to reconstruct a complete workflow, the script preserves what it can rather than making stuff up. The result? Your existing A1111 image collection becomes a whole lot more useful in ComfyUI and compatible image library management tools, without spending the rest of your life manually converting metadata. Think of it as a little A1111-to-ComfyUI metadata Rosetta Stone!



## What It Does
`convert_metadata_a1111_to_comfyui.py` converts recognizable **AUTOMATIC1111 (A1111) Stable Diffusion WebUI metadata inside existing PNG files** into native ComfyUI `workflow` and `prompt` metadata. It operates on individual PNGs or large directories without requiring ComfyUI to be installed or running.

A1111 metadata remains in the PNG. The converter **adds** ComfyUI metadata; it does not regenerate images, alter compressed pixel data, or discard existing EXIF data.

> [!IMPORTANT]
> This is a **PNG metadata converter**, not a JPG-to-PNG image converter. It can process PNGs that were previously converted from JPGs by tools such as XnView MP, **provided recognizable A1111 information survived in their PNG EXIF `UserComment`**.

## What's new in 1.02

- **PNG EXIF support:** Reads A1111 metadata from either the conventional PNG `parameters` text field or the EXIF `UserComment` embedded in a PNG `eXIf` chunk.
- **PARTIAL metadata support:** Recognizes A1111 postprocessing-only information, such as `Postprocess upscale by: 4, Postprocess upscaler: 4x-UltraSharp`.
- **Informational workflows:** For PARTIAL data, writes a vanilla ComfyUI canvas `workflow` containing a **Note** node with the available information, with an empty executable `prompt` (`{}`). It does not invent checkpoints or generation steps.
- **Notes for unrepresented fields:** For FULL data, adds a Note **only if some original A1111 fields cannot be expressed through the functional nodes**. Cleanly represented workflows do not get an unnecessary Note.
- **Existing behavior retained:** Same filename, same command-line options, same in-place update, timestamp preservation, and approved `normal` scheduler fallback.

## Requirements

- **Python 3.9 or newer** (no `pip install` necessary).
- Read/write permission on each PNG and its directory. Creating backups and staging files needs additional free disk space.
- Works on Windows 10/11 and macOS; Windows timestamp preservation uses the Win32 API without `pywin32`.

Download `convert_metadata_a1111_to_comfyui.py` and place it anywhere convenient. You can run it from a folder containing PNGs, or specify another directory or one image explicitly.

## Quick start

**Windows (Command Prompt / PowerShell):**

```cmd
py convert_metadata_a1111_to_comfyui.py . --backup --verify
```

**macOS (Terminal):**

```bash
python3 convert_metadata_a1111_to_comfyui.py . --backup --verify
```

The final `.` means **current directory**. The script scans only that directory unless you supply `--recursive`. Files with extensions other than `.png` (such as JPG, MP4 and `.py`) are ignored during directory scans. PNG extension matching is case-insensitive.

Start with a dry run if you want to preview what will happen:

```cmd
py convert_metadata_a1111_to_comfyui.py . --dry-run --verbose
```

For a long-running collection, save an audit CSV:

```cmd
py convert_metadata_a1111_to_comfyui.py . --backup --verify --report conversion.csv
```

> [!WARNING]
> The script modifies eligible **original PNGs in place**. `--backup` is strongly recommended for valuable images. Writes are staged and can be rolled back on ordinary errors, but an unexpected power loss during the in-place write is **not crash-atomic**.

## Metadata the converter can read

| Source inside PNG | What is read | Priority |
| --- | --- | --- |
| PNG `parameters` (`tEXt`, `iTXt`, or `zTXt`) | A1111 generation infotext and recognized postprocessing fields | **First** |
| PNG `eXIf` → EXIF `UserComment` | A1111 text retained when a JPG is converted to PNG, including common Unicode and ASCII EXIF encodings | **Fallback when `parameters` is absent** |

If a `parameters` field exists but contains unrecognizable text, the converter **does not silently replace its interpretation with EXIF**; it skips the file. This avoids combining conflicting processing histories. Other EXIF tags, XMP, ICC profiles, ancillary chunks, and original image bytes are preserved, but are **not** themselves interpreted as A1111 generation instructions.

The converter recognizes standard A1111 settings such as prompts, Steps, Sampler, CFG scale, Seed, Size, model/checkpoint name, VAE, CLIP skip, common LoRA tags, some Additional Networks LoRAs, and supported Hires.fix information. It also recognizes characteristic *partial* A1111 fields and postprocessing information. Recognition is heuristic: another tool might write the same labels, and unusual A1111 extensions might not be recognized.

### FULL, PARTIAL, NONE — and errors

| Classification | Definition | Result |
| --- | --- | --- |
| **FULL** | Recognizable A1111-style `Steps`, `Sampler`, and `Seed` fields. This **does not guarantee** a complete generation recipe. | Construct a functional ComfyUI graph if required parameters can be mapped; otherwise create an explanatory, Note-only workflow instead of guessing. |
| **PARTIAL** | Recognizable A1111-related fields, but not the FULL signature (e.g., Extras upscaling metadata). | Create a **Note-only** canvas with the recorded data and an empty executable `prompt`. |
| **NONE** | No recognizable A1111 generation or postprocessing metadata found in the selected source. Other PNG metadata might exist. | **Skip** without modifying the PNG. |
| **ERROR** (`failed`) | File could not be processed or safely written. | Record/print an error, and attempt rollback if a write began. |

A `FULL` classification is **not** a claim that an image can be reproduced pixel-for-pixel. A FULL image missing indispensable parameters (for example CFG scale or generation Size) is intentionally represented **informationally**, with the reason recorded, rather than inventing those values.

### What gets written

| PNG metadata field | Contents |
| --- | --- |
| `workflow` | **ComfyUI canvas JSON** (legacy workflow format 0.4). Functional nodes when supported, plus Note nodes only where useful. |
| `prompt` | **ComfyUI API graph JSON** for functional workflows. Informational Note nodes are **not** executable nodes; Note-only workflows use `{}`. |
| `parameters` | Original A1111 text is **left unchanged** if present. If A1111 text came from EXIF and `parameters` is absent, the decoded text is copied into a new `parameters` field. |
| `eXIf` and all other original chunks | **Preserved byte-for-byte** (apart from replacement/removal of converter-owned `workflow`, `prompt`, and legacy `a1111_import` text fields when overwriting). |

The original compressed `IDAT` image data is copied unchanged: **no re-encoding, resizing, or loss of image quality**.

### What you see in ComfyUI

- **FULL / functional:** Checkpoint loader, CLIP prompt encoding, sampler, latent/decoding/save nodes, and supported additional nodes; an informational Note appears only if original settings were not represented.
- **PARTIAL or unbuildable FULL:** A Note describing what was actually recorded, without a fabricated model or executable generation pipeline. You can read this on the canvas, but **cannot queue it to recreate the image**.
- **NONE:** The image is unchanged and the converter does not add workflow metadata.

The Note node is built into vanilla ComfyUI; no custom nodes or companion extension are required. ComfyUI must still have the referenced model/LoRA/VAE files to execute a reconstructed graph. **Automated JSON validation is not a substitute for testing drag-and-drop in your installed ComfyUI version.** Some third-party image browsers may show `prompt` metadata but not canvas-only Notes.

## Command-line reference

| Argument | Description |
| --- | --- |
| `path` | **Required**: PNG filename or directory. Use `.` for current directory. |
| `--recursive`, `-r` | Include subdirectories (off by default). |
| `--backup` | Keep original copies as `image.bak1.png`, `image.bak2.png`, etc. Backups are excluded from subsequent folder scans. |
| `--dry-run` | Check files and build/validate prospective workflows **without writing any image or report file**. |
| `--overwrite` | Rebuild/replace existing ComfyUI `workflow`/`prompt` metadata; also removes obsolete converter-created `a1111_import` text entries. Off by default. |
| `--model-ext {.safetensors,.ckpt}` | File extension appended to model/LoRA/VAE names that lack one (default `.safetensors`). Filename mapping is a **guess**, not a hash-based model lookup. |
| `--mode {auto,txt2img,img2img,inpaint}` | Select or override generation mode for functional FULL workflows. Default `auto` uses clues in A1111 settings. Does not fabricate FULL metadata from PARTIAL. |
| `--verbose`, `-v` | Print each file's status and conversion warnings/notes. |
| `--no-progress` | Suppress progress bar (summary still printed). |
| `--quiet` | Show only errors and totals. |
| `--verify` | Re-read converted files and verify ComfyUI JSON/links, original metadata, non-owned PNG chunks, and **compressed `IDAT` bytes**. Also checks timestamps; date checks always occur even without this switch. Does not run ComfyUI. |
| `--report FILE` | Write CSV with path, conversion result, FULL/PARTIAL/NONE, metadata source, workflow type, nodes, notes and errors. **Cannot be combined with `--dry-run`**. |
| `--version` | Print script version (`1.02`). |
| `--help`, `-h` | Show usage and all options. |

### More examples

```cmd
rem Process one image
py convert_metadata_a1111_to_comfyui.py "C:\Images\portrait.png" --backup --verify

rem Process a folder tree
py convert_metadata_a1111_to_comfyui.py "C:\Images" --recursive --backup --verify

rem Replace older generated ComfyUI metadata in existing PNGs
py convert_metadata_a1111_to_comfyui.py . --overwrite --backup --verify

rem Show detailed output without a progress bar
py convert_metadata_a1111_to_comfyui.py . --verbose --no-progress
```

On macOS, substitute `python3` for `py` and use macOS paths. For portable ComfyUI installations on Windows, you can use their bundled Python interpreter if it supports Python 3.9+.

## Safety and limits

- Files are **updated in place**, preserving their filesystem identity. On Windows the script preserves **Date Created, Date Modified, and Date Accessed** through Win32 `GetFileTime`/`SetFileTime`; on macOS it preserves access and modification times. The behavior of Explorer's generic **Date** column depends on which date Windows selects for the file.
- `--backup` keeps an original independent copy; without it, a temporary rollback copy is created during conversion and removed after success. **Backups need disk space.** A power failure may interrupt an in-place write.
- Original EXIF/PNG metadata and compressed pixels are preserved; the converter does **not** claim to recreate all original source images, masks, ControlNet settings, ADetailer passes, proprietary upscalers, or extension behavior.
- **Approved scheduler fallback retained:** Where the A1111 scheduler has no known ComfyUI mapping, `normal` is used as a compatibility default. This is **not** a claim that A1111 originally used `normal`. The original A1111 metadata is still retained.
- Unknown samplers and unsupported Hires.fix upscalers are **not** silently swapped for unrelated algorithms. Unsupported Hires.fix operations are described in Notes rather than fabricated as equivalent nodes.
- The script does not download model files or verify that referenced checkpoint names exist in your ComfyUI installation.
- The converter can detect the same **A1111-style metadata patterns** as the companion JPG metadata scanner, but it does **not** process JPGs directly. Convert JPG to PNG externally first, preserving EXIF if you need that metadata.

## FAQ

### Does this support formats other than PNGs exported from A1111?

**It processes PNG files only**, including A1111-origin PNGs and PNGs made from JPEGs by third-party tools **when their A1111 EXIF `UserComment` survives**. It does not open or convert JPGs directly. Other image generators, including Draw Things, are separate conversion projects; their metadata is not automatically supported just because the image is a PNG.

### Does this create a new image?

**No.** It adds metadata to the original PNG **in place**, preserving the compressed image data. Use `--backup` if you want a separate original copy (for example `image.bak1.png`). No newly rendered image is generated.

### What if my image already has ComfyUI metadata in it?

By default, the converter **skips** it if either `workflow` or `prompt` exists. Use `--overwrite` to replace those fields with a new conversion from the preserved A1111 metadata. Back up the original before overwriting information you care about.

### Does the image quality change?

**No.** The script never decodes or re-encodes pixels and verifies that the original compressed `IDAT` stream is unchanged when `--verify` is enabled. File size can increase because metadata is added.

### Can I still use my A1111-created images in A1111 after I run this script?

**Yes, the original A1111 `parameters` text is retained unchanged**, and existing EXIF is preserved. ComfyUI `workflow`/`prompt` fields are added alongside it. For third-party PNGs where A1111 data originally existed only in EXIF, the script also creates a standard `parameters` field. Compatibility with every A1111 version or third-party extension cannot be guaranteed.

### Is it perfect?

**No.** It reconstructs a useful approximation, not the exact historical graph. Missing original source images/masks, different model files, A1111 extensions, samplers, upscalers, and random-number behavior can prevent exact replay. Incomplete metadata becomes an informational Note, **not made-up parameters**. Test on representative images before running a large collection; use `--backup --verify` and the CSV report.

### Can I take this code and change it, or include it in my other project?

The repository's prior FAQ welcomed **free, community-oriented reuse with attribution** and did **not** grant commercial reuse. However, as of this README update, the repository does not publish a separate `LICENSE` file. **Do not assume broad redistribution/commercial rights from the absence of a license.** For unambiguous permissions, the repository owner should publish a formal license or grant permission directly.

### Was this vibe coded?

**Yes.** The project was developed with AI assistance and checked against real A1111 files and ComfyUI metadata formats. As with any utility that edits a collection of images, review the code if required for your environment, test a representative sample, and keep backups.

---

**Project:** https://github.com/SunGod/convert_metadata_a1111_to_comfyui  
**Script:** `convert_metadata_a1111_to_comfyui.py` · **Release:** 1.02
