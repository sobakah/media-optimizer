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

## Revision

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
