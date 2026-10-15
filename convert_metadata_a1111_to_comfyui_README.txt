A1111 -> ComfyUI PNG Metadata Converter
======================================
Version 1.02
Script: convert_metadata_a1111_to_comfyui.py
Project: https://github.com/SunGod/convert_metadata_a1111_to_comfyui

PURPOSE
-------
This standalone Python tool reads recognizable AUTOMATIC1111 (A1111) Stable
Diffusion WebUI metadata already stored in PNG images and adds native ComfyUI
"workflow" and "prompt" JSON to those same PNGs. It can process one image or
thousands. It does NOT create images, convert JPG files, or require ComfyUI to
be running. No external Python packages are needed (Python 3.9+).

Version 1.02 recognizes A1111 metadata in two PNG locations:
  1. The usual "parameters" text field (tEXt, iTXt or zTXt).
  2. EXIF "UserComment" in an eXIf chunk, if "parameters" is absent.

The second source is useful for JPEG images manually converted to PNG in tools
such as XnView MP, provided that EXIF metadata was preserved.

The script preserves the source A1111 metadata, all other original PNG metadata
(including EXIF), and all compressed image data. It modifies files in place;
use --backup to keep an additional independent original copy.

REQUIREMENTS
------------
Windows 10/11, macOS, or another Python-capable operating system.
Python 3.9 or newer. No pip packages or extra dependencies.
Read/write permission for the PNGs and their parent directory, plus enough
free disk space for temporary staging and any backups.

RUNNING THE SCRIPT
------------------
Windows, current folder:
    py convert_metadata_a1111_to_comfyui.py . --backup --verify

macOS, current folder:
    python3 convert_metadata_a1111_to_comfyui.py . --backup --verify

Dry run (preview without modifying files):
    py convert_metadata_a1111_to_comfyui.py . --dry-run --verbose

Single PNG:
    py convert_metadata_a1111_to_comfyui.py "C:\Images\one.png" --backup --verify

Another folder (nonrecursive):
    py convert_metadata_a1111_to_comfyui.py "C:\Images" --backup --verify

Subfolders too:
    py convert_metadata_a1111_to_comfyui.py "C:\Images" --recursive --backup --verify

CSV report:
    py convert_metadata_a1111_to_comfyui.py . --backup --verify --report conversion.csv

Reconvert a PNG that already has ComfyUI workflow metadata:
    py convert_metadata_a1111_to_comfyui.py . --overwrite --backup --verify

The period (.) denotes the current directory. Only PNG files are considered;
JPG/JPEG, MP4, Python .py and other files are ignored. By default subfolders
are not scanned. Case-insensitive .PNG extensions are supported, and existing
image.bak1.png / image.bak2.png backups are excluded from directory scans.

FULL, PARTIAL, NONE, ERROR
--------------------------
FULL     The selected A1111 text contains recognizable Steps, Sampler, and
         Seed. This is evidence of generation metadata, NOT a guarantee the
         original complete generation workflow can be reconstructed.
         A functional ComfyUI graph is constructed when required fields are
         available and recognized. If essential information is missing or
         unsupported, a Note-only informational workflow is created instead.

PARTIAL  The selected text has recognizable A1111-related metadata but lacks
         the FULL generation signature. For example:
           Postprocess upscale by: 4, Postprocess upscaler: 4x-UltraSharp
         The script creates a vanilla ComfyUI Note-only canvas recording the
         known information and an empty executable "prompt" ({}). It does NOT
         invent checkpoint names, seeds or any missing processing steps.

NONE     No recognizable A1111 metadata was found in the selected source.
         This does NOT mean the PNG contains no metadata of any kind. The
         file is skipped and left unchanged.

ERROR    An error prevented safe processing. The error is displayed and can
         be written to the CSV report. If an in-place write started, the
         script attempts to recover from the independent rollback copy.

Metadata recognition is heuristic. A1111-like fields written by another tool
may match; unusual A1111 extensions may not be recognized. A FULL
classification is not equivalent to a fully reproducible workflow.

INPUT METADATA
--------------
The converter reads original A1111 prompts and fields such as Steps, Sampler,
Seed, CFG scale, Size, Model/checkpoint, VAE, CLIP skip, LoRA tags, supported
Additional Networks LoRA settings and supported Hires.fix information.

It also recognizes partial A1111 fields, including postprocessing-only metadata
stored in EXIF UserComment. EXIF UserComment supports common ASCII and Unicode
encodings. It does not interpret arbitrary metadata (XMP, ICC, unrelated EXIF
fields) as A1111 generation settings, but preserves these unchanged.

When both locations exist, the "parameters" field takes precedence. If it is
present but is not recognizable A1111, the script does not silently substitute
EXIF text that might describe a different history; the PNG is skipped.

OUTPUT METADATA
---------------
workflow    Standard ComfyUI canvas workflow JSON (legacy format 0.4).
            For functional FULL images, this includes reconstructed nodes and,
            ONLY when unrepresented A1111 fields exist, one informational Note.
            For PARTIAL or incomplete FULL images, this is a Note-only canvas.

prompt      Standard ComfyUI executable API graph JSON for functional FULL
            workflows. For Note-only workflows it is empty ({}), because a
            ComfyUI Note is frontend-only and must not be submitted for
            execution.

parameters  Existing A1111 PNG parameters are left unchanged. When A1111 data
            originates in EXIF only, a new PNG "parameters" text field is
            added containing the decoded A1111 information.

eXIf, IDAT  Original EXIF and compressed image data remain byte-identical.
            Original unrelated PNG text/chunks and profiles are also preserved.

A Note-only image can be dropped into vanilla ComfyUI to view the recorded
settings, but there is no executable generation workflow to reproduce the
image. Some third-party viewers display API prompt metadata and may not show
canvas Notes.

COMMAND-LINE OPTIONS
--------------------
path                 Required PNG filename or directory. Use . for current dir.
--recursive, -r      Include subdirectories (default: current folder only).
--backup             Keep image.bak1.png, image.bak2.png, ... originals.
--dry-run            Preview without changing images or creating a report.
--overwrite          Replace existing ComfyUI workflow/prompt fields and remove
                     obsolete a1111_import text metadata.
--model-ext          .safetensors or .ckpt for model names without extensions;
                     default is .safetensors. Names are not hash-verified.
--mode               auto, txt2img, img2img or inpaint (default auto).
                     Override ambiguous generation mode for functional FULL;
                     does not fabricate generation data for PARTIAL.
--verbose, -v        Display the result and diagnostic notes for each file.
--no-progress        Hide the progress bar; still print a final summary.
--quiet              Print errors and totals only.
--verify             Re-read output and verify graph structures and links,
                     metadata, non-owned PNG chunks, and compressed IDAT bytes.
                     ComfyUI itself is not executed.
--report FILE        Save a per-PNG CSV (file, status, FULL/PARTIAL/NONE, source,
                     generation type, node count, warnings, error).
                     Cannot be combined with --dry-run.
--version            Print version (1.02).
--help, -h           Print the help message.

PRESERVATION AND LIMITATIONS
----------------------------
The converter preserves the SAME filesystem file object (no rename/replace),
the original PNG compressed pixels, and existing metadata. Windows Date
Created, Date Modified and Date Accessed are restored using native Win32 file
time APIs; macOS access and modification times are restored. Windows Explorer's
"Date" column can use other date fields depending on image metadata.

The converter stages writes and maintains a separate rollback copy even without
--backup. It can recover from ordinary errors, but in-place rewriting cannot
be crash-atomic. Use --backup for important collections; disk space is needed.

Not every A1111 sampler, advanced extension, mask, img2img source image, VAE,
LoRA, Hires.fix upscaler, or custom model is reproducible in vanilla ComfyUI.
Unsupported upscalers and samplers are NOT silently replaced by other
algorithms. The explicitly approved exception is SCHEDULER fallback: if the
recorded A1111 scheduler cannot be mapped, ComfyUI's "normal" is used to keep
the graph editable. This does not claim "normal" was used in A1111; the
original A1111 metadata remains intact.

File hash/model mapping is not guaranteed. The converter does not fetch or
install model files. --verify validates metadata and byte preservation, not
actual workflow runtime execution in ComfyUI. Test some representative files
before processing a large library.

FAQ (questions carried forward from the GitHub repository)
---------------------------------------------------------
Q: Does this support formats other than PNGs exported from A1111?
A: It processes PNG files only. It also recognizes A1111 metadata in PNG EXIF
   UserComment, so some externally converted JPG-to-PNG files can be processed
   without touching their original JPGs. Metadata from other generators such
   as Draw Things requires a separate converter.

Q: Does this create a new image?
A: No. Eligible original PNGs are updated in place. --backup creates a separate
   original copy if requested. No pixels are regenerated or recompressed.

Q: What if my image already has ComfyUI metadata in it?
A: By default the PNG is skipped if it has either workflow or prompt fields.
   Use --overwrite to replace those fields. Back up important originals first.

Q: Does the image quality change?
A: No. The original compressed PNG IDAT bytes stay unchanged. Added metadata
   can increase the overall file size.

Q: Can I still use my A1111-created images in A1111 after I run this script?
A: Original A1111 parameters metadata remains untouched. Original EXIF data is
   also preserved. For EXIF-only images, a new standard PNG parameters field
   is added, allowing A1111-compatible readers to find the information.

Q: Is it perfect?
A: No. Best effort, not a complete historical reconstruction. Unsupported
   settings remain preserved and become Notes when useful. Some functional
   graphs need external assets or manual changes. Use --backup --verify.

Q: Can I take this code and change it, or include it in my other project?
A: The repository's earlier FAQ welcomed free community reuse with attribution
   but did not grant commercial reuse. No formal LICENSE file was present when
   this documentation was prepared. Do not assume unrestricted rights; the
   owner should publish a license or provide permission where necessary.

Q: Was this vibe coded?
A: Yes, the project was developed with AI assistance and tested with real
   images. Treat it like any tool that edits a valuable image collection:
   inspect the code if appropriate, test samples and keep backups.

Version: 1.02. For GitHub-ready Markdown documentation see README.md.
