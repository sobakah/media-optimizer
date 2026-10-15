# media-optimizer

Batch-convert images, GIFs and videos to more efficient formats: JPEG and PNG
to JPEG XL (or WebP), GIF to animated WebP, short silent clips to looping
AVIF or WebP, video to HEVC on the GPU or CPU. Every result is verified
before an original is touched.

## Features

* **One command.** `media-optimizer ~/Pictures` counts the file types and runs
  the matching stages; each stage also runs on its own.
* **Safe deletion.** Originals go to the desktop trash. If that fails nothing
  is deleted, and you are asked once at the end.
* **Verification before deletion.** An output is only accepted once it is
  readable; for video also with a runtime and a picture comparison.
* **In place or mirrored.** Output next to the source, or into a separate
  directory with the folder structure preserved; the source is then never
  changed.
* **Resumable.** `Ctrl+C` saves progress and all settings; the next start
  continues where it stopped.
* **Parallel.** Images, GIFs and clips use every core; videos run several
  GPU encodes at once plus one CPU encode.
* **Dry run.** `-n` walks through everything without writing or deleting.

## Requirements

Python 3.10 or newer and these programs:

**Fedora / RHEL**
```bash
sudo dnf install ffmpeg libjxl-utils libwebp-tools perl-Image-ExifTool pipx
```

**Debian / Ubuntu / Linux Mint**
```bash
sudo apt install ffmpeg libjxl-tools webp libimage-exiftool-perl pipx
```

**Fedora Silverblue / Bazzite / Bluefin**
```bash
rpm-ostree install libjxl-utils libwebp-tools perl-Image-ExifTool
systemctl reboot
```

`ffmpeg` is part of these images. Bazzite ships the full ffmpeg, Silverblue
and Bluefin the stripped `ffmpeg-free`, which has no HEVC encoder — swap it
with `rpm-ostree override remove ffmpeg-free --install ffmpeg` from RPM Fusion.

`exiftool` is optional and only carries metadata out of PNG sources.
`libva-utils` (`vainfo`) helps debugging GPU encoding. AVIF output needs an
AV1 encoder in ffmpeg (`libsvtav1` or `libaom-av1`). Missing programs are
reported at startup.

## Install

```bash
pipx install git+https://github.com/sobakah/media-optimizer
media-optimizer --help
```

From a checkout: `pipx install .`, or without installing:

```bash
python3 -m pip install --user send2trash Pillow   # once
./run.py --help
```

## Usage

```bash
media-optimizer -n ~/Pictures                      # preview, writes nothing
media-optimizer ~/Pictures                         # in place
media-optimizer ~/Pictures /mnt/archive --delete   # target dir, trash originals

media-optimizer images ~/Pictures --target webp    # one stage on its own
media-optimizer video ~/Videos --encoder cpu --crf 20
media-optimizer verify ~/Videos /mnt/archive --fix --run
media-optimizer gpu-selftest ~/Videos/clip.mp4
```

| Command | Does |
|---|---|
| `run` (default) | all stages: images, GIFs, clips (optional), video |
| `images` | JPEG/PNG → JPEG XL or WebP |
| `gifs` | GIF → animated WebP or AVIF |
| `clips` | short, silent videos → looping AVIF or WebP |
| `video` | video → HEVC on the GPU (VAAPI, Vulkan) or CPU (libx265) |
| `verify` | check video outputs against their originals |
| `gpu-selftest FILE` | which GPU options produce a usable picture here |

`media-optimizer COMMAND --help` lists the options. Positional source and
target and `-i`/`-o` both work. Started on a terminal without a source
directory, every command offers its settings menu.

Options that behave the same everywhere:

| Option | Effect |
|---|---|
| `-i, --input <dir>` | source directory |
| `-o, --output <dir>` | target directory, empty = in place |
| `-j, --workers <n>` | parallel workers (default: number of cores) |
| `--subfolders <mode>` | `ask` (default), `include` or `exclude` (only the files directly in the source) |
| `--exclude-folder <dir>` | leave out that folder's own files (relative to the source, `.` = the source itself); repeatable |
| `--delete` / `--no-delete` | move originals to the trash (default depends on the mode) |
| `--force-delete` | if the trash fails, delete permanently without asking |
| `--keep-larger` | keep results that are larger than the original |
| `-c, --config <file>` | use this config file |
| `--log <file>` / `--no-log` | log file / no log |
| `--hold` / `--no-hold` | keep the window open at the end |
| `-y, --yes` | no questions, settings as given |
| `-n, --dry-run` | preview only |

## The stages

### Images

JPEG is transcoded to JPEG XL bit-exactly: `djxl` restores the original file
byte for byte. PNG is converted losslessly or with a quality setting
(`png_mode`); if cjxl fails, a lossless WebP is the fallback. The settings
follow the real file type, not the extension: a PNG named `.jpg` is still
converted losslessly. `--target webp` converts everything to WebP instead;
JPEG is then necessarily re-encoded lossily.

CMYK and arithmetic-coded JPEGs cannot be read by `cjxl` and are reported as
`[UNSUPPORTED]`. There is deliberately no pixel-based fallback for JPEGs: for
a truncated file it would "succeed" and replace the damaged original with an
equally damaged copy, so such files count as failed.

When an extension does not match the content (a WebP named `.jpg`), the source
is renamed in place; with a target directory the source stays untouched and
the result gets the correct name there.

### GIFs

Lossless animated WebP by default; `--target avif` is about half the size but
lossy. Results larger than the original are discarded. A photo named `.gif`
is skipped as `[UNSUPPORTED]` and kept (`--preflight` corrects the name).

### Short clips: AVIF or WebP

`clips` (in a full run `--clips`) turns short, silent videos into animations
that loop like a GIF: codec in `clip_source_codecs`, shorter than
`--max-seconds` (default 10), no audio track. `--format webp` (setting
`clip_format`) writes animated WebP instead of AVIF. Measured on Fedora 44
with glycin 2.1.5, the image loader behind GNOME's image viewer (Loupe) and
the file manager's thumbnails:

| Format | GNOME image viewer | Size vs. an H.264 source (4 test clips) |
|---|---|---|
| animated AVIF (CRF 32) | first frame only | 12–106 % |
| animated WebP (quality 50) | plays, loops | 32–144 % |
| animated JPEG XL | first frame only | – |
| GIF / APNG | plays, loops | 10–20× larger |

Browsers play both. WebP is only smaller than the source for noisy,
high-bitrate footage such as phone clips; otherwise it is retried once at a
lower quality and then discarded, unless `--keep-larger` keeps it. In a full
run, clips are converted before the video stage, which then skips them.

### Video

Re-encodes to HEVC, on the GPU above `bitrate_threshold_kbps` and from
1080p frames on, on the CPU below. Up to `gpu_workers` GPU encodes run at the
same time (default 2) plus one CPU encode; measured on an RX 9070 XT with six
1080p clips including the picture check: 17.6 s with one GPU encode at a
time, 12.2 s with two, 10.9 s with three.

* **Container.** MP4 accepts neither Opus nor SRT, both common in MKV and
  WebM; `auto` writes MP4 for MP4/MOV/M4V sources and MKV otherwise. If a
  stream still cannot be copied, one retry runs with AAC audio and without
  subtitles.
* **Source codecs.** HEVC, VP9 and AV1 are already efficient and skipped.
* **Probe slices.** Several short slices spread over the runtime are encoded
  first; a file that would not save enough is skipped (`probe_margin_pct`).
* **Checks.** The runtime must match, and samples of the picture are compared
  with the source (PSNR). A suspicious picture keeps the output but never
  deletes the original; `--strict-visual` discards it instead.
* **Damaged sources.** Intact frames are salvaged; the original is always
  kept for those.
* **GPU failure.** If the GPU fails on a file, that file and everything still
  waiting goes to the CPU.
* **In place** the output is named `name_h265.<ext>`; `--rename-inplace`
  drops the suffix once the original is gone and the container keeps the
  extension.

### Verify

`verify SOURCE TARGET` checks every video output: `[UNREADABLE]`,
`[DURATION]` (runtime off), `[PICTURE?]` (below `visual_psnr_min`, default
18 dB). A low PSNR is a suspicion, not proof; without `--fix` nothing is
changed. `--keep-samples DIR` stores the compared stills (in the folder
structure of the target), `--fix` deletes the
broken outputs and removes their originals from every video cache, `--run`
regenerates them right away.

## Configuration

A JSON file with only the keys you want to change, e.g.
`~/.config/media-optimizer/config.json`:

```json
{ "cpu_x265_params": "asm=avx512:aq-mode=3:no-sao=1", "png_mode": "lossy" }
```

[`config.example.json`](config.example.json) lists every setting with its
default and a comment. Other places: `--config FILE`, `MO_CONFIG=FILE`, or
`config.json` in the project folder when running from a checkout. Every key
can also be set as an environment variable `MO_<KEY>` (`MO_MIN_SIZE_MB=0`).

Precedence: **command line > interactive answer > environment > config file >
default.** Invalid values stop the program with exit code 2 and one
`[SETTING]` line per value, whichever source they came from.

`delete_original` and `rename_inplace` are `null` by default and follow the
mode: with a target directory false, in place true — with a notice that shows
the values and offers to change them (not with `-y`, without a terminal, or
when you set them yourself).

*Hardware tuning:* `asm=avx512` in `cpu_x265_params` helps on CPUs with a full
512-bit datapath (Zen 5) and is ignored where AVX-512 is missing; remove it on
chips that downclock for it (Skylake-X, Ice Lake server) and on Zen 4.
`cjxl_threads` stays at 1, otherwise N workers start N threads each.

## Things to know

* **Atomic writes.** Output goes to a `.part` file with a unique name first
  and is moved into place only after its check, never over an existing file.
* **Trash.** Space is freed once the trash is emptied. If moving fails, the
  paths collect in `.<stage>_pending_deletes.txt` and one question appears at
  the end.
* **State files** live hidden in the working directory (the target directory,
  or the source in place): `.img_stats.json`, `.gif_stats.json`,
  `.clip_stats.json`, `.h265_stats.json` (counters of an interrupted run), the
  caches, `.video_suspect.txt`, `.broken_videos.txt` and the pending lists.
  The resume state of a full run is in `~/.local/state/media-optimizer/`, one
  file per source folder: a run on another folder neither resumes nor
  overwrites it. Without a source on the command line the most recent one is
  offered; a dry run never removes it.
* **Caches.** Each stage remembers the files that need no work next time:
  `.img_cache.txt`, `.gif_cache.txt`, `.clip_cache.txt` keep results without
  an output (discarded as larger, unsupported, clips not selected),
  `.video_conversion_cache.txt` every handled video. Failures are never
  cached. An entry only counts while the file's size and modification time
  are unchanged and the settings it depends on are the same, so a replaced
  file or a changed quality setting is handled again. Caches in subfolders
  from earlier runs on a subfolder are taken over. `--no-cache`, or "Scan all
  files again" in the settings menu, checks everything again; deleting a cache
  only costs time.
* **Old video caches** from version 1.x (paths only) are detected at the start
  and converted to the new format after a question (`.v1.bak` keeps the old
  file; without a terminal or with `-y` they are converted). Declined, they
  are used as before and the question comes again next run. The Bash scripts
  of 1.x cannot read the converted format.
* **Subfolders.** When the source has subfolders with images or videos, a
  run asks first: *include* (Enter), *exclude* (only the files directly in
  the source) or *custom*, which opens a folder list like keepbest's: a tree
  of the subfolders with their image and video counts, where a number
  switches a folder (and asks whether the folders below it switch along),
  `a`/`n` select all or no subfolders and `m` switches the main folder. A
  left-out folder only loses its own files, its subfolders keep their own
  state. Without a terminal or with `-y` subfolders are included. The choice
  is saved with an interrupted run; `subfolders` and `exclude_folders` in the
  config (or the options above) preset it and skip the question.
* **Hidden and system folders are skipped:** trash (`.Trash-*`,
  `$RECYCLE.BIN`, `#recycle`), NAS folders (`@eaDir`, `#snapshot`),
  `lost+found`, AppleDouble files (`._name`), and other hidden entries unless
  `include_hidden` is true.
* **One run per directory.** A second run on the same tree stops with `[BUSY]`.
* **Stopping.** Ctrl+C and SIGTERM stop all running encoders at once and leave
  the run resumable.
* **Exit codes.** `0` done, `3` finished but some files failed (originals
  kept), `2` invalid setting, `1` cannot start, `130` interrupted, `255` batch
  aborted because an encoder vanished.
* **Metadata.** Outputs keep the modification time of their source. JPEG → JXL
  keeps everything (stored uncompressed, so ordinary readers see it); PNG
  metadata is copied with exiftool; video keeps container metadata.

### Logging

Every run appends a block to `media-optimizer.log`: settings, one line per
file, a summary per stage. It lives in the project folder of a checkout, or
in `~/.local/state/media-optimizer/` when installed; `log_file` or `--log`
choose another path. It is renamed to `.log.1` at the start of a run once it
exceeds `log_max_kb`. A dry run writes no log.

### Launching by double-click

Started from a file manager, the terminal would close as soon as the program
ends. The window therefore stays open unless the parent is an interactive
shell. `MO_HOLD=1`/`0` (or `--hold`/`--no-hold`) forces it, `MO_HOLD_TIMEOUT=15`
closes by itself.

### Started from keepbest

[keepbest](https://github.com/sobakah/keepbest) removes duplicate pictures and videos. After a run, its end screen offers `m` to run media-optimizer on the same folder, in the same window. It sets `MO_CALLER=keepbest`: the run still asks whether to use the default settings (normally skipped when the folder is given on the command line), and the closing question reads *Process another directory? (No: back to keepbest)*. keepbest also sets `MO_HOLD=0`, since its window stays open anyway, and passes its folder choice as `MO_SUBFOLDERS` and `MO_EXCLUDE_FOLDERS` (a JSON list), so the run covers the same folders as keepbest's and does not ask about subfolders.

### Automated runs

A systemd user service and timer, e.g. for an ingest folder:

```ini
# ~/.config/systemd/user/media-optimizer.service
[Unit]
Description=Media Optimizer batch processing

[Service]
Type=oneshot
Environment="MO_HOLD=0"
ExecStart=%h/.local/bin/media-optimizer --yes %h/Ingest %h/Optimized
```

```ini
# ~/.config/systemd/user/media-optimizer.timer
[Unit]
Description=Run Media Optimizer nightly

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now media-optimizer.timer
loginctl enable-linger "$USER"    # run even while not logged in
```

If the target is a network mount, guard the unit with
`ConditionPathIsMountPoint=` so it does not run against an empty directory.

## Troubleshooting

### GPU encoding does not start

`vaapi_device` `auto` tries every render node. If all fail, the actual ffmpeg
error is shown. Common causes: the wrong node (`ls -l /dev/dri/by-path/`;
with an iGPU, `renderD128` is often the integrated one), missing permissions
(`id | grep render`), an old driver (RDNA 4 needs Mesa 25.0+ and kernel
6.13+), ffmpeg without VAAPI, or on Fedora the stripped VA drivers
(`sudo dnf swap mesa-va-drivers mesa-va-drivers-freeworld`). With
`--encoder gpu` the run stops instead of falling back to the CPU.

### GPU runs but the picture is broken

A VAAPI encoder can produce unusable pictures without any error (typically:
fragments at the top, the rest green). `media-optimizer gpu-selftest FILE`
runs option variants against a real file and rates each by picture
comparison:

```
  Variant       Description                        Result     PSNR
  original      original command, device after -i  usable     47 dB
  hvc1          like original + -tag:v hvc1        BROKEN     5 dB
  av1           av1_vaapi instead of hevc_vaapi    usable     57 dB
```

Known pitfalls: `hevc_tag hvc1` (incomplete `hvcC` box, measured 5 dB here),
a negotiated pixel format (fixed to nv12, 10 bit only with `gpu_allow_10bit`),
B-frames (`gpu_bf 0`), rate control (`gpu_rc_mode VBR`), or another encoder
(`gpu_codec av1_vaapi` or `hevc_vulkan`).

## Known limits

* The PSNR comparison is a heuristic: only the best of several samples counts.
  Measured separation: sound re-encodes 31–38 dB, even CRF 51 still 24 dB,
  broken colour formats 9–12 dB; threshold 18 dB.
* The probe over-predicts savings for sources with long keyframe intervals,
  because the reference slice starts at the previous keyframe.
* `--verify-deep` fully decodes every image output and costs noticeable time.
* SVT-AV1 3.1.2 hangs forever on frames narrower than about 31 px. Such GIFs
  and clips are encoded with libaom-av1 instead (reported as unsupported if
  that is missing), and every AV1 encode of a GIF or clip is stopped after
  10 minutes.

## Tests

```bash
python3 -m pytest                 # about 200 tests, 3 minutes
python3 -m pytest -m "not slow"   # without the signal tests
```

Needs the tools above plus `pytest`. Every test runs with its own home,
config and state folder; the trash is switched off.

## History

Up to release 1.2.1 this was a set of Bash scripts (`media-optimizer.sh` and
`scripts/`), first written with **Google Gemini**, later revised with
**Claude (Anthropic)**. Release 2.0 replaced them with this Python version;
the last commit with the scripts is `4d3a676`. Settings from the old
`media-optimizer.conf` translate one to one into lower case
(`CPU_CRF` → `cpu_crf`), except `COMPRESSION_METHOD` → `gif_method`,
`CHECK_EXTENSIONS_PREFLIGHT` → `preflight` and `ENABLE_AVIF_STAGE`,
`AVIF_MAX_SECONDS`, `AVIF_REQUIRE_SILENT`, `AVIF_SOURCE_CODECS` →
`clip_stage`, `clip_max_seconds`, `clip_require_silent`, `clip_source_codecs`.
Folders processed by the scripts continue seamlessly: caches, lists and
leftovers use the same names. Development history, fixed bugs and
measurements are in [CHANGELOG.md](CHANGELOG.md).
