A1111 TO COMFYUI PNG METADATA CONVERTER — VERSION 1.01
=====================================================

Requirements: Python 3.9 or newer. Standard library only. No ComfyUI or Node.js
runtime required. Intended for PNG images with AUTOMATIC1111/Forge `parameters`
metadata. On Windows 10, use `py` in place of `python` if necessary.

QUICK START (Windows Command Prompt)
------------------------------------
Dry run, current directory (no subdirectories):
    py convert_metadata_a1111_to_comfyui.py . --dry-run

Convert PNGs in current directory with backups and checks:
    py convert_metadata_a1111_to_comfyui.py . --backup --verify

Convert current directory plus subdirectories (only with -r / --recursive):
    py convert_metadata_a1111_to_comfyui.py . --recursive --backup --verify

Reconvert images previously processed by older converter releases:
    py convert_metadata_a1111_to_comfyui.py . --overwrite --backup --verify

Verbose CSV report for a batch:
    py convert_metadata_a1111_to_comfyui.py . --backup --verify --verbose --report results.csv

A single file:
    py convert_metadata_a1111_to_comfyui.py image.png --backup --verify

BACKUPS
-------
Backup files remain recognizable PNGs: image.bak1.png, image.bak2.png, etc.
Automatic directory scans skip these backups. A backup explicitly passed as the
single file argument is processed if eligible.

OUTPUT
------
- `parameters`: original A1111 text chunk, unchanged.
- `workflow`: editable ComfyUI workflow JSON (best-effort, possibly partial).
- `prompt`: ComfyUI API graph JSON.
- NO `a1111_import` metadata. With --overwrite, old converter-created
  `a1111_import` metadata is removed, including workflow.extra.a1111_import.
- All unrelated PNG metadata and compressed IDAT image data are retained.

IN-PLACE WRITES / WINDOWS FILE DATES
------------------------------------
The script writes into the existing PNG file, not a renamed/replacement file.
Before touching it, it saves Windows native Date Created, Date Accessed and Date
Modified values with GetFileTime. After writing and verifying image metadata, it
restores all THREE timestamps using SetFileTime. This also prevents Windows
Explorer's generic "Date" column from changing when that column displays Date
Modified (a common fallback when PNGs have no capture-time metadata).

Timestamp restoration and comparison happen on every conversion regardless of
--verify. Failure to preserve dates is treated as a failed conversion and the
script attempts to roll back the original bytes and timestamps.

A historical generic "Date" value that was already changed by an earlier script
cannot be reconstructed solely from the converted PNG. If you kept image.bak1.png
(or another original backup), its original Date Modified can be recovered from it.
Running this updated converter again with --overwrite will preserve the current
dates, not recover a previous Date Modified value.

CAUTION: In-place modification is not crash-atomic; a power failure while a
file is being written can corrupt it. Use --backup for irreplaceable
collections. Actual Win32 timestamp restoration in v1.01 still requires a
Windows 10 test; preservation of Date Created was previously confirmed by the
user. --verify validates metadata / IDAT, not execution inside ComfyUI.

PARSING, SCHEDULERS AND ACCURACY
-------------------------------
- More robust parsing of A1111 joined/single-line parameters and quotes / nested
  block delimiters (informed by Civitai's open-source parser approach).
- Support for older Additional Networks (AddNet) LoRA slots. Reads exact
  separate model/CLIP weights A/B; does NOT assume a missing weight.
- Standard prompt LoRA tags are still converted into LoraLoader nodes.
- Original parameters always retain LoRA/model/hash/extension-specific text.
- Unknown sampler names cause a conversion error for that image WITHOUT
  modifying it: no sampler is guessed.
- Recognized explicit schedulers map to ComfyUI equivalents; older sampler
  names ending in a recognized scheduler (e.g. 'DPM++ 2M Karras') also map.
- Unspecified/Automatic schedulers use 'normal' if no explicitly named
  equivalent is present. Unsupported scheduler names also use 'normal' as
  the user-selected EDITABLE FALLBACK, not a verified historical value.
  The original scheduler remains verbatim in the A1111 `parameters` field.
  No special warning node title is written. Conversion notes appear in
  --verbose / --report when a fallback was used.
- No made-up Lanczos img2img source resize. Source image must be supplied and
  appropriately sized by the user.
- Hires upscale operations are only reconstructed when the metadata specifies
  a recognized latent mode (Latent (bicubic), Latent (nearest-exact),
  Latent (bislerp)) and other required settings are present. In other cases,
  the workflow is explicitly labeled PARTIAL and ends after the first pass;
  the original Hires upscale model/algorithm remains in `parameters` with an
  explanatory warning in verbose/CSV output. 4x-UltraSharp is NOT replaced
  with Lanczos (or another upscaler).
- No invented seed, steps, CFG, generation dimensions, or corrected out-of-range
  denoising strength. Files missing essential values are reported as failures
  and remain unchanged.

LIMITATIONS
-----------
This is not the original historical ComfyUI workflow. img2img/inpaint source
images and masks are typically not in A1111 infotext. ADetailer, Tiled
Diffusion regional prompts, variation seeds and specialized extensions are
preserved verbatim in `parameters` but not rebuilt into executable nodes.
Model/LoRA filenames still require verification in ComfyUI. A1111 and ComfyUI
samplers/RNGs may not generate identical pixels even with similar settings.
The script does not reconstruct every ComfyUI node schema variant or test
loading in a running ComfyUI installation.

CLI PARAMETERS
--------------
path                    PNG file or directory (required); '.' = current dir
-r, --recursive         Include nested subdirectories (OFF by default)
--backup                Keep image.bak1.png, image.bak2.png, ...
--dry-run               Preview only, don't write images/reports
--overwrite             Regenerate existing ComfyUI metadata; remove old custom
--model-ext             Default .safetensors; alternative .ckpt
--mode                  auto / txt2img / img2img / inpaint (default: auto)
-v, --verbose           Print individual results and warnings
--no-progress           Disable the single-line progress indicator
--quiet                 Only print errors and summary
--verify                Re-read and validate PNG metadata, IDAT and graph links
--report FILE           CSV with per-file status and warnings; not in dry-run
--version               Print version 1.01
-h, --help              Show all options

PARSER REFERENCE
----------------
Parsing enhancements were informed by publicly documented ideas in the MIT-
licensed Civitai generation-metadata project:
https://github.com/civitai/media-metadata
No JavaScript, Civitai package, or runtime dependency is included.
