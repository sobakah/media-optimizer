# Media Optimizer Suite

Bash scripts that batch-convert images, GIFs and videos to more efficient
formats. An orchestrator runs the sub-scripts in sequence; each one also works
on its own.

## Features

* **One entry point.** `media-optimizer.sh` counts the file types in the source
  folder and starts the matching stages.
* **Resumable.** `Ctrl+C` stores progress, statistics and settings; the next
  start continues where it stopped.
* **Safe deletion.** Originals go to the desktop trash via `gio trash` or
  `trash-cli`. If that fails nothing is deleted and you are asked once at the
  end.
* **Verification before deletion.** An output is only accepted once it is
  readable; for video also with a runtime and picture comparison.
* **In place or mirrored.** Output either next to the source or into a separate
  directory with the folder structure preserved.
* **Dry run.** `-n` walks through everything without writing or deleting.
* **Logging.** Every run appends settings, processed files and statistics to
  `media-optimizer.log`.

## Requirements

**Fedora / RHEL**
```bash
sudo dnf install ffmpeg libjxl-utils libwebp-tools file trash-cli libva-utils
```

**Debian / Ubuntu / Linux Mint**
```bash
sudo apt install ffmpeg libjxl-tools webp file trash-cli vainfo
```

**Fedora Silverblue / Bazzite / Bluefin**
```bash
rpm-ostree install libjxl-utils libwebp-tools trash-cli libva-utils
systemctl reboot
```

`ffmpeg` and `file` are already part of these images; layering them again can
conflict on updates. Bazzite ships the full ffmpeg, Silverblue and Bluefin
ship the stripped `ffmpeg-free`, which has no HEVC encoder — swap it with
`rpm-ostree override remove ffmpeg-free --install ffmpeg` from RPM Fusion.

`gio` ships with GNOME and KDE, `trash-cli` is the fallback. `libva-utils` is
only needed to debug GPU encoding. AVIF output needs an AV1 encoder in ffmpeg
(`libsvtav1` or `libaom-av1`). Dependencies are checked at startup.

## Layout

```
media-optimizer.sh          orchestrator: pre-flight, stages, resume
media-optimizer.conf        settings (optional)
scripts/img-to-jxl.sh       JPG/PNG  -> JXL or WebP
scripts/gif-to-webp.sh      GIF      -> animated WebP or AVIF
scripts/h264-to-h265.sh     video    -> HEVC (GPU or CPU)
scripts/video-to-avif.sh    short silent clips -> AVIF   (optional)
scripts/verify-output.sh    check outputs against the originals
scripts/lib/common.sh       shared functions, sourced
```

Stages run in this order, numbering adapts: images, GIFs, AVIF, video. AVIF
runs **before** HEVC so short silent clips end up as AVIF; the HEVC stage then
skips any source that already has an AVIF output.

## Usage

```bash
./media-optimizer.sh -n ~/Pictures                     # preview only
./media-optimizer.sh ~/Pictures                        # in place
./media-optimizer.sh ~/Pictures /mnt/archive --delete  # target dir, trash originals
```

Sub-scripts can be called directly and then show their own menu:

```bash
./scripts/h264-to-h265.sh ~/Videos
./scripts/h264-to-h265.sh -i ~/Videos --encoder cpu --crf 20
```

Positional arguments and options can be mixed, `--help` exists everywhere.

## Scripts and options

Options that behave the same in several scripts:

| Option | Effect |
|---|---|
| `-i, --input <dir>` | source directory |
| `-o, --output <dir>` | target directory, empty = in place |
| `-j, --workers <n>` | parallel workers (default `nproc`) |
| `--delete` / `--no-delete` | move originals to trash (default depends on mode) |
| `--force-delete` | if trash fails, `rm` without asking |
| `--keep-larger` | keep the result even when it is larger |
| `--log <file>` / `--no-log` | log file / disable logging |
| `--hold` / `--no-hold` | keep the window open at the end |
| `-n, --dry-run` | preview only |
| `-h, --help` | help |

### `media-optimizer.sh`

```
      --preflight      fix file extensions by MIME type beforehand
      --verify-deep    fully decode image outputs (slower)
      --verify-visual    compare picture content while converting (default)
      --no-verify-visual skip that comparison
      --strict-visual    discard suspicious video outputs instead of warning
      --verify-output    re-check the finished target directory afterwards
      --no-verify-output skip that final check (default)
      --no-cache         ignore the video cache for this run
      --cache            use the video cache (default)
      --avif           extra stage: short silent videos to AVIF
      --no-avif        skip that stage (default)
  -y, --yes            no prompts, use defaults
      --reset          discard the saved state
```

Warns beforehand when files share a base name with different extensions, since
`foo.jpg` and `foo.png` both map to `foo.jxl`.

Two independent picture checks are available for video:

| | when | default | effect |
|---|---|---|---|
| `VERIFY_VISUAL` | during conversion | on | every encoded video is compared against its source right away; the result is shown per file and counted in the summary, `VISUAL_STRICT` discards instead of warning |
| `ENABLE_VERIFY_OUTPUT` | after the run | off | `verify-output.sh` walks the finished target directory once more |

The final check needs a target directory, because it compares against the
untouched originals; in place it is skipped with a note. It is read-only and
never deletes. If it reports findings, the orchestrator prints the repair
command rather than acting on its own.

### `img-to-jxl.sh`

JPEG is transcoded to JXL bit-exactly lossless. PNG is converted losslessly or
with a quality setting; if that fails a lossless WebP fallback kicks in.

```
      --target <f>       jxl | webp (default: jxl)
      --webp-quality <q> quality for lossy WebP (default: 85)
      --discard-larger   discard the result if larger than the original
  -e, --effort <1-9>     JXL effort (default: 7)
      --png-mode <m>     lossless | lossy (default: lossless)
      --png-quality <q>  only with lossy (default: 90)
      --cjxl-threads <n> threads per cjxl process (default: 1)
      --verify-deep      fully decode the output
```

`--target webp` converts everything to WebP instead. JPEG is necessarily
re-encoded lossily then, because lossless WebP from an already DCT-compressed
JPEG would be larger than the source. `PNG_MODE` still decides whether PNG is
lossless.

When the extension does not match the content (a WebP named `.jpg`), behaviour
depends on the mode. **With a target directory the source stays untouched** and
the file lands under the correct name in the target. In place the source file
is renamed, which is the whole point there. The same applies to `--preflight`.

### `gif-to-webp.sh`

```
  -m, --method <0-6>  WebP compression level (default: 6)
      --target <f>    webp | avif (default: webp)
      --avif-crf <n>  AV1 quality with --target avif (default: 20)
      --verify-deep   check the output with webpinfo
```

WebP is lossless here, AVIF roughly halves the size but is lossy. Results
larger than the original are discarded.

### `h264-to-h265.sh`

Re-encodes video to HEVC, switching between hardware (VA-API, Vulkan) and
software (libx265) based on the source bitrate and frame size. Large frames
always go to the GPU, because a sparsely encoded 4K file has a low bitrate but
would still stall the batch on `libx265`.

```
      --extensions <list>  source extensions, space separated
                           (default: mp4 m4v mov mkv webm avi ts m2ts wmv flv)
      --container <c>      auto | mp4 | mkv | keep (default: auto)
      --source-codecs <l>  which source codecs get re-encoded
                           (default: h264 mpeg4 msmpeg4v3 wmv3 vc1 mpeg2video)
      --encoder <mode>     auto | gpu | cpu (default: auto)
      --threshold <kbps>   CPU/GPU threshold in auto mode (default: 3500)
      --qp <n>             GPU quality (default: 26)
      --crf <n>            CPU CRF (default: 22)
      --preset <p>         x265 preset (default: medium)
      --x265-params <s>    x265 parameters (default: aq-mode=3:no-sao=1)
      --min-size <mb>      skip files below this size (default: 5)
      --no-probe           no test slice beforehand
      --probe-margin <p>   skip at p % of the original (default: 90)
      --probe-duration <s> total length of the test material (default: 6)
      --probe-slices <n>   how many slices it is split into (default: 3)
      --gpu-min-pixels <n> in auto mode, always use the GPU from this frame
                           size upwards (default: 2073600, 0 disables)
      --from-list <file|auto>  only the listed source files
      --rename-inplace     drop the _h265 suffix after deleting the original
      --hevc-tag <t>       container tag, e.g. hvc1 (default: none)
      --gpu-codec <c>      hevc_vaapi | av1_vaapi | hevc_vulkan
      --gpu-device <p>     render node or "auto"
      --rc-mode <m>        CQP | VBR | ICQ | QVBR | CBR (default: CQP)
      --bf <n>             max B-frames on the GPU
      --low-power          VAAPI low-power encoder
      --force-gpu          skip the startup check
      --gpu-selftest <file>  try option variants, then exit
      --gpu-10bit          encode 10-bit sources in 10 bit
      --no-verify-visual   no PSNR comparison
      --strict-visual      discard suspicious outputs instead of warning
      --no-faststart       do not move the moov atom to the front
      --no-cache           ignore the cache file for this run
      --cache              use the cache file (default)
```

Key behaviours:

* **Container choice.** MP4 accepts neither Opus audio nor SRT subtitles, both
  common in MKV and WebM. `auto` therefore writes MP4 for MP4/MOV/M4V sources
  and MKV for everything else. If copying a stream still fails, one retry runs
  with AAC audio and without subtitles.
* **Source codecs.** VP9, AV1 and HEVC are already efficient and are skipped by
  default; re-encoding them costs quality without saving space.
* **Probe slices.** Several short slices spread over the runtime are encoded
  and compared against the same slices of the original (both video only,
  measured by file size). Skipped as soon as less than `100 − --probe-margin`
  percent savings are expected. One slice from the middle is not enough: on a
  clip with a quiet middle section it predicted "74 % larger" for a file that
  actually shrank by 53 %.
* **Damaged sources.** If the decoder reports corruption the intact frames are
  salvaged. For those files the original is always kept, even with `--delete`.
* **Size check.** Results larger than the original are discarded.
* **Progress.** `>>> [12/347] Processing: …`. The counter covers every
  candidate found, including the silently skipped ones.

### `video-to-avif.sh` (optional stage)

Included via `--avif` or called directly. Converts short, silent videos to
animated AVIF. A file is selected only if all criteria match: source codec in
`AVIF_SOURCE_CODECS`, runtime below `--max-seconds`, no audio track. The audio
condition is necessary because AVIF cannot store sound.

```
      --max-seconds <s> only videos shorter than s (default: 10)
      --extensions <l>  source extensions, space separated
      --crf <n>         AV1 quality (default: 32)
      --crf-retry <n>   penalty for the second attempt (default: 6)
      --preset <n>      SVT-AV1 preset 0-13 (default: 6)
      --pix-fmt <p>     yuv420p | yuv444p (default: yuv420p)
      --allow-audio     also videos with sound (sound is lost)
      --no-verify       no PSNR comparison
```

If the output ends up larger than the original a second attempt runs with a
higher CRF before it is discarded.

### `verify-output.sh`

Checks a target directory against the source directory. Files are matched by
relative path, falling back to the base name when the container differs, since
`VIDEO_CONTAINER=auto` turns an `.avi` source into an `.mkv` output.

```
      --extensions <l>   output extensions to check, space separated
      --no-visual        skip the picture comparison, only read and duration
      --psnr-min <db>    threshold for suspicion (default: 15)
      --duration-tol <p> allowed runtime deviation in percent (default: 2)
      --samples <n>      samples per file (default: 3)
      --keep-samples <d> store the compared stills
      --fix              delete broken outputs and drop them from the cache
      --run              start the repair run after --fix
```

| Message | Meaning |
|---|---|
| `[UNLESBAR]` | `ffprobe` finds no video stream |
| `[DAUER]` | runtime deviates beyond the tolerance |
| `[BILD?]` | runtime fine, picture comparison below the PSNR threshold |

Called without `-y` on a terminal the script offers an interactive setup.
`--keep-samples` is only offered there when the picture comparison is
actually going to run, since without it there would be no stills to store;
combining `--no-visual` with `--keep-samples` prints a note instead of
silently doing nothing.

A low PSNR is a **suspicion, not proof**. Without `--fix` nothing is changed.
`--keep-samples` stores the compared stills so every message can be judged.
`--run` starts the repair with `--from-list` on the defect list and archives
that list afterwards.

`[DAUER]` also catches **salvaged** files that are legitimately shorter. The
message therefore prints both runtimes; `--duration-tol` raises the limit.

## Configuration

`media-optimizer.conf` is loaded when it sits next to `media-optimizer.sh`.
Alternatives are `$XDG_CONFIG_HOME/media-optimizer.conf` or a path in
`MO_CONFIG`. Deleting it restores the built-in defaults.

Precedence: **CLI flag > interactive input > environment variable > config file
> default.**

The interactive prompts (answer `n` to "use default settings?") show the config
values as presets. Everything else is reachable through the config file,
environment or flags; `MO_CONFIG_VARS` in `scripts/lib/common.sh` lists all
supported names.

### Defaults by mode

`DELETE_ORIGINAL` and `RENAME_INPLACE` have no fixed default. They depend on
whether a target directory was given:

| Mode | `DELETE_ORIGINAL` | `RENAME_INPLACE` | Note |
|---|---|---|---|
| target directory | `false` | `false` | no message |
| in place | `true` | `true` | notice with the option to change |

In place the original would otherwise sit next to the output and the `_h265`
suffix would stay forever. Because that is destructive, a notice appears with
both values and the option to change them. It also appears after choosing "use
default settings". With `-y` or without a terminal it is only printed.

An explicit setting always wins and is never overwritten: via CLI, environment
or config file. Both values are commented out in the shipped config for that
reason.

## Automated Background Processing

To automate media optimization—for example, automatically processing a local ingest directory or an `rclone` VFS cache mount—a `systemd` user service and timer provide the cleanest integration.

**1. Create the Service (`~/.config/systemd/user/media-optimizer.service`):**
```ini
[Unit]
Description=Media Optimizer Batch Processing
After=network.target

[Service]
Type=oneshot
# MO_HOLD=0 prevents interactive prompts and window-hold logic
Environment="MO_HOLD=0"
# Example processing an rclone mount. Adjust paths as needed.
ExecStart=%h/bin/media-optimizer.sh --yes %h/GoogleDrive/Ingest %h/GoogleDrive/Optimized
```

**2.Create the Timer `(~/.config/systemd/user/media-optimizer.timer)`:**
```
[Unit]
Description=Run Media Optimizer nightly

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

**3. Enable and start the schedule:**
```bash
systemctl --user enable --now media-optimizer.timer
loginctl enable-linger "$USER"    # run even while not logged in
```

Two things to watch:

* `ExecStart` must point at the script inside the unpacked suite, not at a
  lone copy. The script resolves `scripts/` relative to its own location, so
  `%h/bin/media-optimizer.sh` only works if `%h/bin/scripts/` exists as well.
* If the target lives on an `rclone` mount, guard the unit so it does not run
  against an empty directory after a failed mount:
  `ConditionPathIsMountPoint=%h/GoogleDrive`

### Hardware tuning

The shipped configuration targets a Ryzen 7 9700X with an RX 9070 XT.
`pools` and `frame-threads` are deliberately unset because x265 picks the same
values itself on 16 threads. `asm=avx512` is enabled since Zen 5 has a full
512-bit datapath. `CJXL_THREADS=1` avoids 16 parallel cjxl processes each
spawning 16 threads.

*Note on AVX-512:* it is enabled here because Zen 5 has a full 512-bit
datapath. On CPUs without AVX-512 at all (Zen 3 and older, most consumer
Intel since Alder Lake) the flag is simply ignored — x265 masks unsupported
instruction sets. It only hurts on chips that do support AVX-512 but downclock
for it, notably Skylake-X and Ice Lake server parts, and on Zen 4, which
double-pumps 256-bit and gains little. Remove `asm=avx512` from
`CPU_X265_PARAMS` there.

### Logging

Every run appends a block to `media-optimizer.log` next to the script:
settings, one line per processed file, statistics per stage.

```
RUN 260911-120014-926-4711  started 2026-09-11 12:00:14
  Call   : media-optimizer.sh -i /tmp/pics
  Source : /tmp/pics
  Settings:
    ...
----------------------------------------------------------------
12:00:14  start  stage images (2 candidates)
12:00:14  img    OK  /tmp/pics/a.jpg -> a.jxl (8.85 KB -> 7.11 KB, -19.6%)
12:00:14  img    summary: 2 converted, 0 skipped, 0 failed
----------------------------------------------------------------
RUN 260911-120014-926-4711  finished 2026-09-11 12:00:16  (code 0)
```

Skipped files get no individual line, otherwise the file would be dominated by
cache hits; they appear in the per-stage summary.

| Variable | Flag | Default | Effect |
|---|---|---|---|
| `MO_LOG` | `--no-log` | `true` | write a log at all |
| `MO_LOG_FILE` | `--log <file>` | next to the script | path |
| `MO_LOG_MAX_KB` | — | `5120` | rotation limit in KB |

**Systemd/Journalctl Integration:** The internal rotation (`MO_LOG_MAX_KB`) 
works well for standalone usage. However, if you are running the suite as a 
background service, you can disable the internal log (`--no-log`) and pipe 
standard output directly to the system journal (`journalctl`) 
or manage it externally via `logrotate`.

## Things to know

* **Atomic writes.** Output always goes to a `.part` file with a unique name
  first. The original is only replaced or trashed once the output passed its
  check.
* **Trash.** Space is only freed once the trash is emptied. If moving fails
  (different mount, no `gio`/`trash-cli`) nothing is deleted; the paths collect
  in `.<type>_pending_deletes.txt` and one prompt appears at the end.
* **Leftovers.** `.part` files and lock directories from hard aborts are
  removed at the next start.
* **State files** live hidden in the source directory: `.img_stats.env`,
  `.gif_stats.env`, `.h265_stats.env`, `.avif_stats.env`,
  `.video_conversion_cache.txt` and the pending lists.
* **Video cache.** `.video_conversion_cache.txt` remembers finished files so a
  later run does not run `ffprobe` over everything again. It is purely a
  speed-up; deleting it only changes runtime. Since absolute paths are stored,
  it stops matching once the folder is moved.
* **Counters across resumes.** Completed work carries over; cache hits describe
  the current scan and restart at zero.
* **In place for video.** Source and target would share a path, so the output
  is named `name_h265.<ext>`. `--rename-inplace` drops the suffix afterwards,
  but only when the original's path is free **and** the target container has
  the same extension. An MKV output from an AVI source keeps the suffix rather
  than being misleadingly named `video.avi`.

### Launching by double-click

Started from a file manager, the terminal emulator closes the window as soon as
the script ends. An exit handler therefore keeps it open, also on success, and
on an abort prints exit code, line number and the failed command.

Detection reads the parent process command line: an interactive shell has no
script and no `-c` argument, a terminal emulator does. `MO_HOLD=1`/`0` forces
the behaviour, `MO_HOLD_TIMEOUT=15` closes by itself. For a `.desktop` file
`Exec=env MO_HOLD=1 /path/media-optimizer.sh` with `Terminal=true` is the most
reliable choice.

### Another run

After a successful run the orchestrator asks whether to process another
directory. Answering yes restarts it with the same options and prompts for a
new source; the directory arguments of the first call are dropped. With `-y` or
without a terminal the question is skipped.

## Troubleshooting

### GPU encoding does not start

`VAAPI_DEVICE="auto"` tries every render node under `/dev/dri/`. If all fail
the script names the actual ffmpeg error. Common causes:

1. **Wrong render node.** With an iGPU plus dGPU, `renderD128` is often the
   integrated one. `ls -l /dev/dri/by-path/` maps nodes to PCI addresses.
2. **Missing permissions.** `id | grep render`, otherwise
   `sudo usermod -aG render $USER` and log in again.
3. **Driver too old.** RDNA 4 with VCN 5 needs Mesa 25.0+ and kernel 6.13+.
4. **ffmpeg without VAAPI.** `ffmpeg -hide_banner -encoders | grep hevc_vaapi`
   must return a line.
5. **Fedora.** The stock packages are stripped for patent reasons:
   `sudo dnf swap mesa-va-drivers mesa-va-drivers-freeworld`

With `--encoder gpu` the script aborts instead of silently falling back to CPU.
If the GPU fails on an actual file, that file is retried on the CPU and the
rest of the run stays there.

### GPU runs but the picture is broken

A VAAPI encoder can produce unusable pictures without any error. Typical look:
fragments at the top, the rest solid dark green — a zeroed YUV buffer, memory
that never received picture data.

```bash
./scripts/h264-to-h265.sh --gpu-selftest /path/to/video.mp4
```

The self-test runs option variants against a real file and rates each result by
picture comparison:

```
  Variant       Description                        Result     PSNR
  original      original command, device after -i  usable     42 dB
  hvc1          like original + -tag:v hvc1        BROKEN      8 dB
  av1           av1_vaapi instead of hevc_vaapi    usable     47 dB
```

Known pitfalls:

* **`-tag:v hvc1`** requires VPS/SPS/PPS to live only in the `hvcC` box. If the
  encoder emits them in-band the muxer builds an incomplete `hvcC`: correctly
  encoded, no longer correctly decodable. `HEVC_TAG` is empty for that reason.
* **Pixel format.** Writing `format=nv12|p010` as an alternative lets the filter
  choose. If it picks p010 while the encoder runs 8 bit, the buffer is only
  partly filled. The format is therefore fixed; 10 bit only via `--gpu-10bit`.
* **B-frames.** Some VCN generations struggle through Mesa — try `--bf 0`.
* **Rate control.** `CQP` is the classic path, `--rc-mode VBR` the alternative.
* **Different encoder.** `--gpu-codec av1_vaapi` or `hevc_vulkan` (ffmpeg 7.1+).

### Checking files that were already produced

```bash
./scripts/verify-output.sh -i ~/Videos -o /mnt/archive --keep-samples /tmp/samples
./scripts/verify-output.sh -i ~/Videos -o /mnt/archive --fix --run
```

## Known limits

* Video runs sequentially. On CPU, x265 saturates the cores itself; on GPU
  parallelism makes little sense anyway.
* The PSNR comparison is a heuristic. Both files are seeked inside a single
  ffmpeg process, which keeps the frames aligned, and only the best of several
  samples counts. Measured separation: sound re-encodes 31-38 dB, even CRF 51
  still 24 dB, broken colour formats 9-12 dB; the threshold sits at 18 dB.
  Still inspect reported files yourself.
* Short probe slices slightly overestimate the new bitrate because the first
  keyframe weighs heavily. `--probe-duration 30` helps more than a looser
  threshold.
* The collision warning uses `sort | uniq` and misses file names containing
  newlines. The worker-side lock still holds.
* `--verify-deep` fully decodes every image output and costs noticeable time.

## History

The first version of the scripts was written with **Google Gemini**, a later
revision with **Claude (Anthropic)**. Development history, fixed bugs,
measurements and discarded approaches are in [CHANGELOG.md](CHANGELOG.md).
