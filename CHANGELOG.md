# Changelog

Development history of the Media Optimizer Suite. The README describes the
current state; this file records how it got there and why individual decisions
turned out the way they did.

The first version of the four scripts was written with **Google Gemini**, a
later revision with **Claude (Anthropic)**.

---

## Initial version

`media-optimizer.sh` as orchestrator plus `img-to-jxl.sh`, `gif-to-webp.sh` and
`h264-to-h265.sh`. It already had resume via state files, trash instead of `rm`,
extension correction by MIME type, mirrored folder structure, parallel workers
through `xargs` and GPU/CPU switching based on source bitrate.

---

## 0.1 – First release

The initial version, revised before it was released.

### Bugs fixed in the initial version

| Finding | Effect |
|---|---|
| `mkdir -p ""` | In-place mode aborted the orchestrator immediately with exit 1. |
| `gif2webp -lossless` | The option does not exist. Every call failed, so the GIF stage never converted anything. |
| `cwebp -q 9` | Quality 9 out of 100 in the PNG fallback, probably confused with `-z 9`. |
| `BATCH_MAP` | Videos were recorded before processing and permanently skipped on resume after an abort. |
| Name collision | `foo.jpg` and `foo.png` shared target and temp file, causing data loss with parallel workers. |
| `find *.mp4` | Also matched `.part.mp4` left over from aborted runs. |
| `eval` in `prompt_val` | Path input containing `$(...)` was executed. |
| `find \| wc -l` | Without `\|\| true` a permission error tore down the orchestrator under `pipefail`. |
| PNG with fixed `-q 75` | Lossy while the original was deleted, with no hint in the output. |
| No comparison before deletion | A file truncated by a full disk passed the "larger than 0 bytes" check. |

### New foundations

* `lib/common.sh` replacing the three duplicated sets of helpers.
* Configuration file with the precedence CLI > interactive > environment >
  config > default.
* `--help` and options everywhere, dry run, dependency check.
* Verification of outputs before every deletion decision.
* A prompt instead of a hard `rm` when the trash fails.
* Exit handler that keeps the window open on double-click launches and names
  exit code, line number and failed command.

### Bugs introduced by the revision

These were caused by the revision and fixed afterwards. They are listed because
the same causes can reappear in future changes.

| Bug | Cause | Consequence in the code |
|---|---|---|
| `-tag:v hvc1` set | The tag requires parameter sets only inside the `hvcC` box. If the encoder emits them in-band the file is correctly encoded but no longer decodable: fragments at the top, the rest green. | `HEVC_TAG` empty, `hvc1` only via `--hevc-tag` |
| `format=nv12\|p010` | Written as an alternative, the filter negotiates the format itself. Picking p010 without `main10` leaves the buffer partly filled. | format fixed, 10 bit only with `--gpu-10bit` |
| `export MO_HOLD=0` in the orchestrator | `export` also sets the variable in the own shell, so the orchestrator disabled its own wait. | `env MO_HOLD=0` for the children only |
| Encoder arguments built before device detection | The probe ran with `-vaapi_device auto` and failed while encoding worked. | `gpu_encoder_args()` rebuilt after detection and per file before the probe |
| Config file blocked in workers | Values the orchestrator does not know never arrived. | `load_config` preserves existing environment values and loads in every script |
| `verify_visual` not exported | "command not found" in parallel workers, counted as a failed check. | central `export -f` in `common.sh` |
| PSNR check with deletion rights | A heuristic decided over files. Individual samples drop with variable frame rate (13, 15, 24 dB on a sound video). | best sample counts, threshold 15 dB, warning by default |
| `COUNT_CACHE_SKIPPED` carried over | Every resume re-counted the same cache hits, reaching five-digit values. | counter describes the current scan and starts at zero |
| `run_stage` read exit ≥ 124 as an abort | A non-executable sub-script (126) was reported as a user abort. | only 130, 124 and 125 count as aborts |
| Extension correction changed the source | The correction renamed the source file even with a target directory, breaking the promise that the source stays untouched. | `_handle_wrong_extension` branches by mode; `--preflight` only reports in target mode |
| WebP branch before `_commit` | Function definitions only take effect when executed, so the result stayed a `.part` file. | branch moved behind the helper |

### Format comparison

Measured in this project, always against the original file. Lossless WebP is
bit-exact and therefore has no finite PSNR.

**GIF sources** (240x180, 3 s):

| Target | flat animation | photo-like | PSNR |
|---|---|---|---|
| WebP lossless (default) | 66 % | 84 % | bit-exact |
| AVIF CRF 20 | 46 % | 48 % | 47 / 39 dB |
| AVIF CRF 28 | 34 % | 29 % | 44 / 35 dB |

AVIF halves the size but is lossy. For GIFs with text and hard edges `yuv420p`
also halves chroma resolution; `yuv444p` cost 67 % instead of 37 % for +1.6 dB,
which PSNR reflects poorly.

**Video sources** (H.264 CRF 23, 480x360, 6 s, no audio):

| Target | size vs original | PSNR |
|---|---|---|
| AVIF CRF 18 | 160 % | 51 dB |
| AVIF CRF 28 | 112 % | 47 dB |
| AVIF CRF 32 (default) | 90 % | 45 dB |
| AVIF CRF 35 | 75 % | 44 dB |

The source is already H.264-compressed, so AVIF has to catch up first. Below
CRF 30 the file grows beyond the original. A longer GOP changes nothing;
SVT-AV1 already uses long intervals.

For GIFs AVIF is a real gain. For short videos it is format unification rather
than space saving; the HEVC or AV1 path keeps the audio track and MP4 plays
everywhere.

**Images** (1280x960):

| Source | WebP `-q 85` | JXL lossless |
|---|---|---|
| photo JPEG | 42 % / 44.7 dB | 72 % |
| edge-heavy JPEG | 55 % / 40.8 dB | 65 % |
| PNG (both lossless) | 51 % | 53 % |

**PNG to JXL.** `-q` maps to a Butteraugli distance: 75 → d2.35, 90 → d1.00,
93 → d0.73. Lossless costs four times the size of `-q 75` and roughly 60 % more
CPU time. Chosen: `-q 90` in the config, lossless as the script default.

### Other measurements

* **x265 parameters.** `pools=16` and `frame-threads=4` match what x265 picks
  itself on 16 threads and were removed. `asm=avx512` stays a config option
  because Zen 5 has a full 512-bit datapath.
* **cjxl.** Without `--num_threads=1` each of the 16 parallel processes spawns
  another 16 threads.

### Mode-dependent defaults

`DELETE_ORIGINAL` and `RENAME_INPLACE` no longer have a fixed default. With a
target directory both are `false`, in place both are `true` with a notice that
can be answered. Previously `false` was hardcoded, which left original and
output side by side in place and kept the `_h265` suffix forever.

### Logging

A continuous log file next to the orchestrator with settings, processed files
and statistics per run. Skipped files deliberately get no individual line so
the file is not dominated by cache hits on large collections.

### Another run

After a successful run the orchestrator can restart with the same options for a
different directory. The restart goes through `exec`; inherited state such as
run ID and resolved settings is cleared first. The run ID gained a random
component because `exec` keeps the PID and two runs in the same second would
otherwise share an identifier.

### Additional input formats

Video is no longer limited to MP4. The extension list is configurable and the
target container follows from the source: MP4 accepts neither Opus audio nor
SRT subtitles, both common in MKV and WebM, so `auto` writes MP4 for MP4/MOV/M4V
and MKV otherwise. If copying a stream still fails, one retry runs with AAC and
without subtitles. Source codecs are filtered as well, since re-encoding VP9,
AV1 or HEVC costs quality without saving space.

### Tools that came out of debugging

* **`verify-output.sh`** checks a target directory against the originals in
  three stages and can have broken outputs regenerated selectively.
* **`--gpu-selftest`** runs option variants against a real file and rates each
  by picture comparison, because a VAAPI encoder can produce unusable pictures
  without any error message.
* **`--from-list`** processes only listed source paths, ignoring cache and
  existing outputs.
* **`video-to-avif.sh`** for short, silent videos.

### Discarded diagnoses

Two explanations for the green picture matched the symptom and were wrong
anyway. Both times the cause was not the encoder but an option the revision had
added.

* **"p010 confusion in the filter."** Plausible, because a zeroed YUV buffer
  produces exactly that green. Fixing the format to `nv12` changed nothing.
* **"hevc_vaapi is fundamentally broken on RDNA 4 under Mesa."** Adopted even
  though the observation contradicted it: encoding had worked with the initial
  version. That contradiction led to the bisection and to the actual cause,
  `-tag:v hvc1`.

Lesson for future debugging: when an explanation contradicts a solid
observation, the explanation is wrong, not the observation. Comparing against a
version known to work beats any theory.

---

## 0.2 – Metadata kept for AVIF and PNG sources

| Change | Why |
|---|---|
| Container tags kept in AVIF clips | `video-to-avif.sh` never passed `-map_metadata 0`, so tags such as `creation_time` were lost on every conversion. AVIF has no field for a title, so free-form text tags are still lost; that is a limit of the format. |
| EXIF copied from PNG sources | Neither `cjxl` nor `cwebp` reads a PNG's `eXIf` chunk, so capture date, GPS position and copyright disappeared in both target formats. exiftool copies them into the finished file, controlled by `IMG_COPY_METADATA` (`auto` when exiftool is installed, `always` with a warning at startup when it is missing, `never`). JPEG sources are skipped: the JXL path repacks the JPEG bit-exactly and cwebp reads JPEG metadata itself, so an exiftool call per photo would only cost time. exiftool becomes an optional dependency. |
| Timestamp after the copy | exiftool updates the modification time, so `touch -r` now runs after it. |

---

## 0.3 – Review of img-to-jxl.sh

An external review found several ways the image stage could lose originals or
quality. Each was reproduced against real files before and verified after the
fix, failures forced with stub commands.

| Finding | Effect |
|---|---|
| Interactive "no" to deleting originals | The prompt did not set `DELETE_ORIGINAL_EXPLICIT`, so the in-place default turned the answer back into "yes". All prompting scripts now use `mo_prompt_delete_original`. |
| PNG named `.jpg` | cjxl detects the format by content; the JPEG branch passed no `-d`, so the PNG was encoded lossily and logged as `[JXL-LOSSLESS]`. The real type is now detected first and decides the settings. |
| Unchecked `mv` in the worker | A failed move still logged SUCCESS and deleted the original; the only copy left was a `.part` file the next run cleans up. Workers now run with `set -euo pipefail` and an EXIT trap that records FAIL. |
| Existing-output check before the lock | `foo.png` could replace the `foo.jxl` that `foo.jpg` had just produced. The check is repeated under the lock and the move uses `mv -n`, verified by whether the temp file is gone, because `mv -n` returns 0 when it refuses. |
| exiftool after verification | Errors were ignored, the size check measured the file before metadata was added, and `-all:all` dropped PNG text chunks. Metadata now goes into the temp file first, with `-all`, and an exiftool error keeps the original. |
| xargs returns 124, not 255 | The "encoder missing" abort never triggered and was treated as Ctrl+C. Fixed in all three xargs scripts. |
| Exit 0 despite failures | Scripts now exit 3 when files failed; the orchestrator reports it and carries on with the next stage. |
| Dry run, target mode, concurrency | A dry run deleted leftovers and wrote the log; target mode wrote state files into the source; two runs deleted each other's temp files. |

`cjxl` turned out to copy PNG EXIF after all, contrary to the earlier comment,
but Brotli-compressed. With metadata copying enabled it now writes the boxes
uncompressed so exiftool and other readers can see them.

### Second round: the remaining review points, in all scripts

The first round fixed `img-to-jxl.sh`. The same patterns existed in the other
scripts, and the review's usability and design points applied to all of them.
Shared building blocks in `common.sh` now carry the fixes, so the scripts
cannot drift apart again.

| Finding | Effect |
|---|---|
| GIF and AVIF workers | Same unchecked `mv`, missing re-check under the lock and state files in the source as the image stage had. Both now use the worker scaffold from `common.sh`. In `gif-to-webp.sh` the "encoder missing" code 255 was also overwritten by `rc=1` on the next line, so the batch never stopped. |
| Lock conflict in `video-to-avif.sh` | Counted as a silent skip. Now reported as a conflict. |
| `h264-to-h265.sh` exit code | 0 even when encodes failed or outputs did not verify. Now 3. |
| JPEG → JXL metadata | Brotli-compressed by cjxl's default, invisible to readers without Brotli. Now uncompressed; measured that the JPEG reconstruction stays bit-exact. |
| Unchecked option values | `-j 0` meant unlimited parallelism, a wrong effort made every PNG fall back to WebP, `DELETE_ORIGINAL=yes` silently meant false. All values are now checked against one rule table, whichever way they were set. A flag without its value no longer crashes with "unbound variable". |
| Prompts | The settings menu appeared even with every option on the command line, and the in-place notice asked again although the user had decided explicitly. |
| Hidden and system folders | Trash folders were converted, which would bring deleted files back elsewhere; AppleDouble `._` files were treated as images. |
| SIGTERM | Waited for the whole batch, or the whole ffmpeg encode, before reacting. Background jobs also inherit SIGINT as ignored, and a script started in the background cannot undo that, so stopping now freezes the process tree and sends INT and TERM, then waits until ffmpeg has really finished. |
| CMYK JPEG | Failed on every run. Now reported as unsupported and skipped. A pixel-based fallback was tested and rejected: for a truncated JPEG it "succeeds" and replaces the damaged original with an equally damaged, larger copy. |
| An unreadable subfolder | Turned a complete batch into "xargs failed". The shared `find` helper now leaves that to find's own message. |
| German leftovers | Comments, messages and the state files `.defekte_videos.txt` and `.video_verdaechtig.txt` (now `.broken_videos.txt` and `.video_suspect.txt`, renamed once). |

---

## 0.3.1 – Restart fix

| Finding | Effect |
|---|---|
| Restart crashed | "Process another directory?" and discarding a stopped run restart the orchestrator with `exec "$0"`. Started as `bash media-optimizer.sh`, `$0` is a bare name, exec searched `PATH` and the script died with "not found" (exit 127). Restarts now use the absolute path through `$BASH`. |
| `--log` lost on restart | Rebuilding the command line kept every flag but dropped every plain word as a directory, including the value of `--log`; the new run stopped with "needs a value". |

The tests had missed it because they always started the script as
`./media-optimizer.sh`. The new group `orch-restart` starts it with `bash`
and covers both restart paths; all four tests fail against 0.3.

---

## 0.4 – Python version

The suite is now a Python program, `media-optimizer`, with a subcommand per
stage (`run`, `images`, `gifs`, `clips`, `video`, `verify`, `gpu-selftest`).
The Bash scripts are gone; the last commit with them is `4d3a676`.

### Upgrading from 0.3.x

- Install with `pipx install git+https://github.com/sobakah/media-optimizer`
  (Python 3.10+). The external tools are the same; `file`, `gio` and
  `trash-cli` are no longer needed.
- `media-optimizer.sh SRC DST` becomes `media-optimizer SRC DST`;
  `scripts/img-to-jxl.sh` becomes `media-optimizer images`, and so on. The
  options are the same, except `video-to-avif.sh` → `clips` and `--avif` →
  `--clips`.
- `media-optimizer.conf` is not read any more. Settings go into
  `~/.config/media-optimizer/config.json` with lower-case names
  (`CPU_CRF` → `cpu_crf`); `config.example.json` lists all of them, the README
  the few that were renamed. Environment variables are `MO_<NAME>`.
- An interrupted 0.3 run cannot be resumed by 0.4. Folders processed by 0.3
  continue seamlessly: the video cache, the lists and the run lock are the
  same.

### Last Bash change: video cache from subfolders

| Change | Effect |
|---|---|
| Subfolder caches taken over | A run on a folder reads the `.video_conversion_cache.txt` files that earlier runs on its subfolders left behind and adds their entries to its own cache. Before, a run on the parent probed every one of those videos again. With a target directory only a cache in `<target>/<sub>` with entries below `<source>/<sub>` is used; a cache from a run with a different target would otherwise make the parent skip a video whose output is somewhere else. The subfolder caches are only read, on every run. |
| `verify-output.sh --fix` prunes subfolder caches too | Otherwise the next run would take the removed entry over again from the subfolder cache and never regenerate the broken output. |
| Rescan in the interactive settings | The custom settings of `h264-to-h265.sh` had no way to ignore the cache; the orchestrator asked "Use the cache…?". Both now ask "Scan all videos again, ignoring the cache of finished ones?" (default no). A run that ignores the cache says so at the start. |

Test group `h264-cache` (15 tests); 7 fail against 0.3.1, the two
`verify --fix` checks fail when only the merge is in place.

### Port to Python

One program instead of six scripts; the shell glue is replaced by Python
(thread pool instead of xargs, process groups instead of walking the process
tree, a hard link instead of `mv -n`, magic bytes and the JPEG header instead
of `file`, one `ffprobe -of json` call instead of five, send2trash), the
safety rules and exit codes are the same. Writing it against real files found
problems that the Bash version had:

| Finding | Effect |
|---|---|
| AVIF picture check measured nothing | An animated AVIF has two video streams, a still first and the animation second. `verify_visual` compared `[0:v]`, the still, which has no frame at any sample time after 0 s; no PSNR value came back and "not measurable" counts as passed. Every AVIF original was deleted without a picture check. Comparing the animation stream: a correct AVIF scores 42–45 dB, a wrong one 7 dB. |
| Suspicious video deleted | With `--delete`, the video stage moved the original to the trash after a suspicious picture comparison ("File is kept" referred to the output only). |
| Salvaged video deleted | The README promised the original is always kept for files salvaged from a damaged source; the code only kept it when the output had grown. |
| Arithmetic-coded JPEG | cjxl cannot read them (measured; cwebp can). `file` does not print a component count for them, so they failed as "damaged" on every run. |
| Probe over-predicts with long keyframe intervals | Not fixed. The reference slice is cut with `-c copy` and starts at the previous keyframe; on a synthetic clip with x264's default GOP the probe said "71 % smaller" for a file that shrank 2.6 %. Needs real footage before the method changes. |

Also changed on purpose: the resume state stores every setting; the video
stage moves its output without overwriting; `--force-delete` deletes at once
when the trash fails; an invalid answer in a menu is asked again; a renamed
misnamed source cannot be counted twice; there are no "Terminated" lines.

### WebP for short clips, parallel video encoding

| Change | Why |
|---|---|
| `clip_format webp` for the short-clip stage | Measured with glycin 2.1.5 on Fedora 44 (GNOME's image viewer and thumbnails): animated WebP, GIF and APNG play; animated AVIF and animated JPEG XL show their first frame only. WebP is 32–144 % of an H.264 source against 12–106 % for AVIF, so it is retried at a lower quality and otherwise discarded unless `--keep-larger`. ffmpeg 8.1 cannot decode animated WebP; the picture check decodes it with Pillow and compares in YCbCr: correct 23.7–44 dB, different content 10–11 dB. |
| Parallel video encoding | `gpu_workers` GPU encodes (default 2) plus one CPU encode at the same time. Six 1080p clips on an RX 9070 XT including the picture check: 17.6 s sequential, 12.2 s with two, 10.9 s with three. A GPU failure moves the file and every queued one to the CPU. |
| The short-clip stage is called `clips` | It was `avif` although it can write WebP. Command `clips`, full-run flag `--clips`, settings `clip_stage`, `clip_format`, `clip_max_seconds`, `clip_require_silent`, `clip_source_codecs`, `clip_webp_quality`; the AVIF-only ones keep `avif_`. Old names in a config file or environment stop with "renamed to …"; a saved resume state with old names still loads. State files: `.clip_stats.json`, `.clip_pending_deletes.txt`. |
| A picture check without a single sample fails | Before, "no sample could be compared" counted as passed, which is how the AVIF check passed every file unmeasured. |

### Bash scripts removed

The Python project moved from `python/` to the root; `media-optimizer.sh`,
`scripts/`, `tests/run-tests.sh` and `media-optimizer.conf` are removed. A
backup of the last Bash state is kept outside the repository. README.md and
CLAUDE.md describe the Python version; the measured tool behaviour from the
Bash notes is kept in CLAUDE.md.

Tests: `tests/`, pytest, 133 tests including signals, prompts on a
pseudo-terminal, forced failures, parallel lanes, real GPU encodes where a
VAAPI encoder is available, and folders left by the Bash version
(`tests/test_compat.py`).

---

## 0.5 – Caches for every stage, started from keepbest

| Change | Why |
|---|---|
| A cache per stage | Before, only videos were cached. Discarded images and GIFs were encoded again on every run, only to be discarded again (for GIFs results larger than the original are discarded by default), and CMYK or arithmetic JPEGs were tried every time. `.img_cache.txt`, `.gif_cache.txt` and `.clip_cache.txt` remember results without an output; failures are never cached. |
| Entries store size, modification time and a settings fingerprint | The Bash version's cache stored only the path, so a different file under the same name counted as done. A changed setting that a result depends on (e.g. PNG quality) now makes the files count as new. |
| Cache paths are compared with symlinks resolved | Runs on `~/Videos/Link` (a symlink to `/mnt/disk/Videos`) recorded the link path; a run started on `/mnt/disk/Videos` found none of those entries and converted cached videos again, and the subfolder caches were not taken over, since their paths did not start with the real folder. Entries are written with the folders resolved, and older entries are resolved when read. |
| Old video caches are converted after a question | Format v2 has a header line. An old-format cache is detected at the start (also in subfolders); converted entries keep their path with the current size and time, the old file stays as `.v1.bak`. Declined, the old file is used as before (paths only), entries from subfolder caches are added to it as plain paths, and the question comes again next run. Without a terminal or with `-y` it is converted. A dry run neither asks nor converts. |
| Clips: output check before ffprobe | The clip stage ran ffprobe on every video before looking for an existing output, and probed every long video on every run. Now the output is checked first and "not selected" is cached. |
| `--no-cache` for every stage | `use_cache` moved from the video settings to the general ones; "Scan all files again" is asked in the global part of the settings menu and in every stage menu. |
| Misnamed files copied again on every run | In target mode, a GIF named `.png` was copied to the target on every run (`isgif.gif`, `isgif_1.gif`, ...); the Bash version did the same. The take-over now compares the content with an existing correctly named copy first. |
| SVT-AV1 hangs on narrow frames | Found while testing the cache with a tiny GIF: libsvtav1 (SVT-AV1 3.1.2) hangs forever on frames narrower than about 31 px; only SIGKILL ends it. Such files are encoded with libaom-av1, and every AV1 encode of a GIF or clip is stopped after 10 minutes. |
| Shorter video progress line, cut by display width | The line was cut by characters; file names with CJK characters or emoji (two columns each) made it wrap, and every update left a new line behind. It is now cut by columns and shows `#n GPU 45% 2.3x 1m 05s left` per running encode; the file name is in the `>>> [n/total] Processing:` line above. |
| Started from keepbest (`MO_CALLER=keepbest`) | keepbest offers media-optimizer for the same folder after a run and continues in the terminal afterwards. With a folder on the command line the defaults question was skipped, so the settings could not be changed there; with `MO_CALLER` set it is asked. The closing question says that No goes back to keepbest. |

---

## 0.6 – Subfolder choice, review fixes

| Change | Why |
|---|---|
| Subfolder question and folder picker | Like keepbest: when the source has subfolders with images or videos, a run asks whether to include them (Enter), exclude them (only the files directly in the source) or choose them in a folder list. The list shows the tree with image and video counts; a number switches a folder and asks whether the folders below switch along, `a`/`n`/`m` select all, no subfolders or the main folder. Settings `subfolders` (`ask`, `include`, `exclude`) and `exclude_folders`, options `--subfolders` and `--exclude-folder`. Without a terminal or with `-y`: include, as before. `exclude_folders` also takes a JSON list from the environment, for names with `:`; keepbest passes its own folder choice that way. Every scan of the source (stages, counts, preflight) goes through `files.source_files`, so they all see the same files. |
| Leftover cleanup deleted user files | Before every video run, `*.part.*` was removed in the whole working directory with `unlink`: `Movie.part.1.mp4`, `archive.part.01.rar`, even files in `.Trash-<uid>`. The other stages had the same flaw for their extensions. Only the exact temp form is removed now (target name, `.part.<pid>.<random>.`, the same extension again; Bash's decimal random part included), never inside trash or NAS system folders. The scan skipped such names as sources too. |
| Portrait videos always failed the picture check | ffmpeg turns a portrait phone video (landscape frames plus a rotation flag) upright and drops the flag. The check decoded both with `-noautorotate` and compared the upright output with the sideways source: 8 dB for a correct encode, so every such video was kept as `[PICTURE?]`, discarded with `--strict-visual` and deleted by `verify --fix`. Both sides are now compared as displayed: 48 dB. Unrotated files measure exactly as before (re-encode 49, CRF 51 29, other content 7, AVIF 46, WebP 31 dB). |
| `verify --fix` deleted outputs compared with a photo | When the original video was gone, a file of the same name was taken as the original, e.g. `IMG_1234.jpg` of a live photo. ffprobe reports 0.04 s for a JPEG, so the output failed as `[DURATION]` and `--fix` deleted it, possibly the only copy. Only videos count as originals now. `--fix` also takes the target's run lock. |
| One resume state per source folder | The state was a single file. With `-y` or without a terminal, a run for `/folder/B` (a timer, a script) resumed an interrupted run of `/folder/A` with A's settings and never touched B; a second run at the same time offered to resume the first. Now each folder has its own state (`state-<hash>.json`, the old `state.json` is taken over), a source on the command line only resumes its own, and a run still going on the same folder is not taken over. A dry run deleted the state at its end; it no longer does. |
| GPU failures go to the CPU lane | A GPU worker retried a failed file on the CPU itself, next to the CPU lane: three x265 encodes at once with two GPU workers. That retry also skipped the AAC/no-subtitle retry, so a file needing it failed. The file now goes back to the main thread and waits for the one CPU lane, where it gets the full treatment. |
| An unrelated image skipped a video | `IMG_0001.avif` or `.webp` (a photo, or made from `IMG_0001.jpg` with `img_target webp`) made the video stage skip `IMG_0001.mp4` as "already converted by the clip stage", and cached that. It only counts now for a video the clip stage would take. |
| Own outputs never encoded twice | With `hevc` in `source_codecs` in place, any changed setting made an output (renamed to the source name) count as new, and it lost quality in a second encode. Output entries in the video cache now hold for any settings. `gpu_bitrate`, `gpu_bf`, `gpu_low_power` and `gpu_allow_10bit` count for the cache, but only when changed, so existing caches stay valid. |
| Smaller fixes | A photo named `.gif` became a lossy one-frame AVIF with `--target avif` (and failed every run with WebP); it is skipped as `[UNSUPPORTED]`. In place, a GIF named `.png` is converted in the same run (the GIFs were counted before the image stage renamed it). A `--from-list` written for a symlinked path works on the real folder and vice versa, and missing entries no longer inflate the total. A PNG whose WebP fallback exists already is no `[CONFLICT]` on every run. The name collision warning follows the real output names (`foo.jpg` and `foo.gif` do not clash, `foo.mp4` and `foo.mov` do). `--keep-samples` keeps equal names in different folders apart. A tool started while a stop was under way was not stopped; `timeout` now also applies to streamed output. Options before the command (`-c FILE images DIR`) belong to it. "Process another directory" no longer inherits the folder choice keepbest passed. Box frames count terminal columns; a bad `MO_HOLD_TIMEOUT` no longer crashes; `gpu-selftest` tests the render node a run would use. |

## 0.6.1 – GPU check names the cause

| Change | Why |
|---|---|
| GPU check names the cause | The startup check showed only ffmpeg's last line, `Nothing was written into output file, because at least one of its streams received no packets`, which follows any failure. The cause came first: `No usable encoding profile found`, the driver has no HEVC encoder. On the development machine the Fedora 45 release upgrade (`--allowerasing`) had removed `mesa-va-drivers-freeworld`, because Fedora 45 shipped an older Mesa (26.2.3) than the installed RPM Fusion build (26.2.4); the reinstall then failed on `mesa-filesystem = 26.2.5`, which Fedora only had in `updates-testing`. The check now reports the first line that is not a consequence, without memory addresses, and for a driver without an HEVC encoder explains the freeworld package on Fedora, including that dependency error. |

## 0.6.2 – The current directory is the preset source

| Change | Why |
|---|---|
| The current directory is the preset source | Started without a source directory, a run asked with an empty prompt, so the folder one had just changed into had to be typed again. The question now shows the current directory (`Source directory [/path]:`): Enter takes it, typing replaces it. A prefilled, editable line (readline) was not used: a path typed over it would have been appended to the preset. The path is the one the shell shows (`$PWD`), so a folder reached through a symlink keeps it. Never preset are the home folder, the folders above it, `/` and system folders (keepbest's rule): a launcher starts in the home folder, no question before the run confirms the folder, and Enter at every prompt would have converted the whole home folder in place. A single stage started with its default settings never asked for the source and ended in "No source directory given"; it asks now. |

## 0.7 – File journal for picktag

| Change | Why |
|---|---|
| File journal for picktag | picktag, an image and video tagger, keeps its tags in a database keyed to the picture. When `foo.png` became `foo.jxl` and the original went to the trash, it could only guess (same folder and name, equal modification time, perceptual hash) that the tags belong to `foo.jxl` now. Every run that changes files now appends what it did to `~/.local/state/media-optimizer/journal.jsonl`, one JSON object per line: `run_start`/`run_end`, `converted`, `renamed`, `copied`, `removed`, with sizes and modification times so a reader can tell the same file from a new one under the same name. The format is picktag's contract (version 1), shared with keepbest. Events are written only after the action succeeded, never in a dry run; the file is only appended to, one locked write per line, so parallel workers, two runs and keepbest can write at the same time. Setting `journal_file` (`null` = default, `false` = off, or a path; `MO_JOURNAL_FILE`, `--journal`, `--no-journal`), independent of the run log. A resumed run uses the journal setting of the current call, like the log. A journal that cannot be written gives one warning and changes nothing else. |
| `lossless` measured, not assumed | `cjxl -d 0` and lossless JPEG transcoding are bit-exact, also for 16-bit PNGs and for colours under fully transparent pixels. Lossless WebP keeps every visible pixel of an 8-bit PNG, but reduces a 16-bit PNG to 8 bits (different pixels) and rewrites colours under fully transparent pixels (invisible). A PNG with more than 8 bits per channel therefore counts as not lossless when it becomes WebP. gif2webp keeps every frame and its duration (compared frame by frame with Pillow). |
| Removal knows its successor | The trash helper only received the path of the original. It now also gets the output that replaced it, for `removed` events, also when the trash failed and the original is deleted after the question at the end of the run. The list of such originals keeps the Bash format (one path per line), so for an entry left by an earlier run the successor is unknown (`kept: null`); that run's `converted` event names it. |
| Tests | Every journal line any test writes is checked (complete line, ASCII only, the common fields and those of its event). The tests reset the stop flag of an interrupted in-process run, which tools started directly by a later test still saw. 27 new tests; each rule was broken on purpose once and its test failed. |

## 0.8 – Browser page

| Change | Why |
|---|---|
| `--events FILE` | The groundwork for the browser page (planned with keepbest's page, which hands its tab over to media-optimizer): the run's progress as JSON lines, written while it goes on. A plan of the stages with their candidate counts, per stage a start and an end with the summary counters, one `done` event per finished file with its outcome and the reason for a skip, the log line of each file outcome with the sizes, every message with its level, and the live progress of each video encode. The events come from the calls that print (`log.file`, `ui.say`) and from where each stage finishes a file, never from parsing the printed text, which changes with every wording. The terminal output was compared with 0.7 for a full run, a dry run, in place with deletion, and the image and video stages: identical. Without colours every colour code is empty, so red could not be told from yellow; the codes now carry their name (`ui.Color`), and print the same. |
| The questionnaire as data | The full run's settings questions were written out in the orchestrator. The browser page has to show the same questions with the same conditions (the PNG quality only for lossy PNG, the GPU quality only for the GPU), so they now live in one table (`questions.FULL_RUN`) that the terminal asks and the page will draw; a condition is data the page can evaluate as well. The terminal transcript was compared with 0.7 on a pseudo-terminal for all defaults, every condition switched on, and an invalid value asked again: identical. Every setting can be handed to a run as `MO_*` and comes back exactly (tested for all 75, including lists, empty values and folder names with `:` and spaces); the environment wins over a config file, so the page can rely on it. |
| Browser page: the server (`--web`) | The skeleton of the page keepbest will hand its tab to, behind `--web` on the full run; the terminal stays the default and is not touched. The server is copied from cdimport and lyrembed with every rule they learned by failure: 127.0.0.1 only, the printed token turned into a cookie whose name includes the port, Host and Origin checks, JSON-only requests, a content security policy, static files only from `static/`, long polling, the stop flag set only after the reply. `--no-browser`, `--return-url` (only a page on this computer) and `--return-name` are lyrembed's options for the hand-off, and the address line has the form keepbest looks for. Starting the revisions at 0 would have made the page ask for news in a tight loop while nothing changed; a test caught it. The page's files are in the package data, or a pipx install would have none. |
| Browser page: settings and the run | The page asks for the folder (preset by the terminal's rule), the subfolders as a checkbox tree with image and video counts (switching a folder switches the ones below it, as in the terminal), the questionnaire with its conditions and every other setting folded away with its description and where its value came from (config file, environment, command line). An interrupted run of the folder needs a choice: resume with its saved settings or start over. The values are checked by the same rules as a config file and shown at their field. The run is the real program (`run -y`) on a pseudo-terminal, so its log keeps the colours and the progress line; the page shows a card per stage with a progress bar and the running video encodes, the warnings and errors in one place, the newest files with their sizes, the log, and at the end the outcome with "Process another folder" or "Back to …". A run with thousands of files sends the page a summary, not every event. Clicked through in headless Chrome on generated files. |
| Browser page: closing the tab, the background | Closing the tab during a run stops it as Ctrl+C would (it saves its state and the next start offers to resume), after the grace a reload needs; the browser asks before it closes. "Keep running in the background" lets the run go on without the page: reopening the address shows its progress and Stop still works, and at its end a desktop notification ("media-optimizer finished: 412 files converted, 18.3 GB saved.") has an "Open" button that brings the page back. Not opened within 15 minutes, the program ends with the run's exit code, so no process is left behind. The notification uses `notify-send`, else `gdbus` without the button, else nothing, and never changes the run. Both ways were clicked through in headless Chrome. |

## 0.8.1 – Cached files in the folder tree

| Change | Why |
|---|---|
| The page marks files earlier runs handled | The folder tree counted every image and video, also those a stage's cache already knows (converted before, discarded as larger, already HEVC, too small), which the run then skips: it looked as if they were still to do. Each folder now says how many of its files are "done before: skipped", a folder with nothing else says "all done before", and a note above the tree gives the total and points to "Scan all files again". With that chosen, the marks go and the note says those files are checked again. Whether a cached result still holds depends on the settings it was made with, so the page asks again when the form changes (another image target format brings the images back). The caches are only read for this (`StageCache(read_only=True)`), never compacted or written. Clicked through in headless Chrome with caches written by a real run. |
| A run from the page used the video cache again | The page hands every setting to its run through the environment, and `cpu_crf` came back as `22.0` instead of `22`. The video cache's fingerprint hashes the settings as written, so a run started from the page did not recognise any video a terminal run had handled and checked them all again (probe encodes included); the other way round as well. Fingerprints now count a number in the type of its default; caches written with the defaults keep their fingerprints (tested). Entries written by a page run with 0.8 are not recognised once and are written again. |


## 0.8.2 – Review fixes

Found by a code review of 0.7..0.8.1. Each fix has a test that fails without it.

| Change | Why |
|---|---|
| A second background run gets its notification | The page remembered when the first background run had ended. A second one in the same session ("Process another folder", then "Keep running in the background") ended without a notification, and the program could end at once instead of waiting `BACKGROUND_WAIT` for the page. |
| No log lines lost on the page | The pty ends lines with `\r\n`. When a read ended between the two, the line was taken for a progress redraw and dropped. |
| The last events of a run are read | The events reader could see an empty file, then the run's last events and its exit, and stop without reading them: a stage card stayed "running". It now reads once more after the run has ended. |
| Resuming on the page names the run's own target | The folder text followed the form ("With a target folder the source is never changed") while the resumed run works with its saved target and settings, possibly in place with originals going to the trash. It now says where the interrupted run continues and what happens to originals there. |
| "Start over" with "Preview only" shows the form's settings | `--reset -n` kept the saved state (a dry run removes nothing) and then resumed it, so the preview showed the old run, in the terminal too. A preview of starting over now ignores the saved run; it stays for a real run. |
| One start at a time | Two starts at once (a double click, two tabs) could both pass the check and start two runs, one of them no longer tracked. Starting also no longer scans the folder tree again. |
| A dry run leaves an old-format video cache alone | Taking over subfolder caches into a root cache in the old format appended to it even with `-n`. |
| The strict picture check counts as failed on the page | A video `--strict-visual` threw away was reported as "discarded (larger)", hiding a failed picture check. Its `done` event now says `failed`, like the stage's counters and the log. |

## 0.8.3 – Second review

Found by a review of 0.8.2 (Google Gemini), each finding checked against the code; several did not hold (a Ctrl+C race: the stages wait for their workers; open pipes: measured, none stay open; cover art taken for the main video: real covers come after it). Each fix has a test that fails without it.

| Change | Why |
|---|---|
| Stage options before the command | Only the general options were known to take a value, so `--gpu-workers 2 video DIR` became a full run of a folder called `video` into `DIR` (with such a folder in the current directory, every stage ran on it). The list now comes from the parser: every option of every command. No option is a flag in one command and takes a value in another (tested). |
| Declining to resume keeps the state | Without a source, the most recent stopped run is offered; answering no deleted its state before a folder was chosen, also when the new run was on another folder. It is now removed only when the new run is on the same folder. |
| The rename in place after the last question | When the trash refused an original, `name_h265.mp4` could not take its name; deleting the original at the end of the stage (the one question) left the suffix. The output is renamed then. |
| A path queued twice is deleted once | Queued by two runs, the second unlink failed: "1 failed", and the pending list stayed. |
| The GPU self-test on a working machine | The plain variants run before "current", and the verdict looked at the first usable one: a machine where everything worked was told its current settings were the problem. |
| The GPU self-test takes AVI and WMV | The reference slice was copied into an MP4, which cannot hold MPEG-4 v3 or WMV video (measured: exit 234). It is an MKV now. |
| Subfolder caches: the later line wins | A file listed twice in a subfolder's cache got its older line (a cache miss, never a wrong skip). |
| A resumed run keeps a finished stage | In place, the converted images are gone on resume: recounted, the stage fell out of the numbering (`[1/1]` instead of `[2/2]`) and out of the page's list. The state now remembers the stages a run started with (`planned`); older states behave as before. |
| The window is not held open behind `sudo` | The hold check looked only at the direct parent and took any shell option (`fish --no-config`, `bash --norc`) for a script. Wrappers (`sudo`, `env`, `uv run`, ...) are looked through; only `-c` or a script file makes a shell non-interactive. |
| `-i A B C` is an error | The third folder was ignored without a word. |
| Smaller ones | `jpeg_info` stops at image data before any frame header; a file that ends after `ftyp`, or an AVIF with the brand `MA1A`/`MA1B`, is no MP4; resolved folders are forgotten at each cache load (a symlink pointing elsewhere in a long page session); the tests' pty helper closes its descriptor. |

---

## 1.0 – First stable release

media-optimizer converts images, GIFs and videos to more efficient formats
(JPEG XL, WebP, HEVC, animated WebP or AVIF for short clips), checks every
output against its original before the original goes, and can be stopped
and resumed at any point; from the terminal or a local browser page. The
program is pre-release 0.8.3 plus one addition: it looks for a newer release
once a day (setting `check_for_updates`; git asks GitHub, with the same
access as pipx). A full run in the terminal offers to install it with pipx
before it starts, the browser page has an *Update* button, and either way the
new version starts with the same command; otherwise it shows the command.

---

## 1.1.0 – The run reads from top to bottom

The browser page showed a run as a list of files with the newest at the
top and the summary below it, so after a long run the summary was a long
scroll away. It now reads like keepbest's page and like the terminal, from
top to bottom, and keepbest shows the run's numbers next to its own.

| Change | Why |
|---|---|
| A section per stage, in order | Each stage shows its progress, its video encodes, its own warnings and errors and its newest files (the running one its last six); the stages are steps in the top bar, as keepbest's, with *Progress*/*Log* and *Stop* next to them, so they stay in reach while the page scrolls. The page follows the newest part while the reader is at its end and stays put for one who scrolled up. |
| Finished stages fold their files away | Each stage keeps its last 50 files (the page had 300 for the whole run, newest first) and says how many there were; the log has all of them. The errors stay in view as messages, with the reason, so even after many thousand files the summary is right after the stages. |
| The summary in tiles | Converted, space saved (before → after), skipped, larger results kept, name conflicts, failed and the time, then the outcome and its buttons. A run that could not start shows no numbers. |
| `--results FILE` | The run's numbers as JSON when it ends, also after a stop, for the program that started it: per stage its state, outcomes, sizes and time, and the sums. It comes from the same events as `--events` (one fold, `results.Tally`, which the page builds on too), so the page and keepbest count alike. The terminal output and the exit code are the same with and without it (tested). The page passes it on to its run. |

---

## 1.1.1 – Updates show up right away

| Change | Why |
|---|---|
| Looks for a newer release at every start, not once a day | A release made today only showed up a day after the last check. The check runs in the background: the page shows the update when GitHub answers; a full run in the terminal offers what the last check found before it starts (`selfupdate.known`) and shows a newer answer after the run, so the run never waits for GitHub (it waited up to 3 s before). |
