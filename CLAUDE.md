# CLAUDE.md

Working notes for continuing on this project. README.md describes what the
tool does for a user; this file describes how to change it without breaking
it. CHANGELOG.md records why things look the way they do.

## What this is

A set of Bash scripts that batch-convert images, GIFs and video to more
efficient formats. About 4,800 lines of code across seven scripts, plus the
test suite. The suite does not compute anything itself — it orchestrates
`ffmpeg`, `cjxl`, `cwebp`, `gif2webp` and `exiftool`. Treat it as a process
orchestrator.

```
media-optimizer.sh          orchestrator: stages, resume, restart offer
media-optimizer.conf        settings, loaded automatically when next to it
scripts/img-to-jxl.sh       JPG/PNG  -> JXL or WebP        (xargs parallel)
scripts/gif-to-webp.sh      GIF      -> WebP or AVIF       (xargs parallel)
scripts/h264-to-h265.sh     video    -> HEVC, GPU or CPU   (sequential)
scripts/video-to-avif.sh    short silent clips -> AVIF     (xargs parallel)
scripts/verify-output.sh    check outputs against originals (xargs parallel)
scripts/lib/common.sh       shared helpers, sourced by all of the above
tests/run-tests.sh          regression tests against real files
tests/lib/pty_run.py        drives the interactive prompts on a pseudo-terminal
```

**This tool deletes user files.** Every change touching deletion, renaming or
verification needs to be tested against real files before it is called done.
Run `tests/run-tests.sh` (all groups, several minutes) or a group by name
(`tests/run-tests.sh gif-`) before calling a change done, and add a test for
every bug fixed: reproduce it first, ideally against the old version.

## Language

Everything is English: messages, prompts, comments, docs. German leftovers are
bugs. Prompts still accept "j"/"ja"/"nein" as answers; that is tolerance on
input, not a leftover. State files with older German names are renamed once
by `mo_migrate_name`.

## Hard invariants

Breaking any of these has caused real data loss or silent corruption before.

1. **Write to a temp file first.** Output goes to
   `<target>.part.<pid>.<random>.<ext>` and is only moved into place after it
   passed verification. Temp names must stay unique per worker.
2. **Never delete an original before the output is verified.** For video that
   means `verify_video` (runtime within tolerance) and optionally
   `verify_visual`. For images it means the file is non-empty, plus a full
   decode with `--verify-deep`.
3. **With a target directory the source directory is never modified.** Not
   even to correct a wrong file extension. In place, renaming is allowed
   because that is the point. See `_handle_wrong_extension`.
4. **`gpu_encoder_args()` must be rebuilt** after any change to
   `VAAPI_DEVICE`, `GPU_CODEC` or the pixel format. Probe and real encode must
   use the identical argument array, otherwise the check tests something other
   than what runs.
5. **Functions used in `xargs` workers need `export -f`.** Without it the call
   is `command not found` in the worker, which in several places is counted as
   a failed check rather than an error. Same for variables and `export`.
6. **State files are read back with `source`.** Always write them through
   `mo_write_vars`, which quotes with `printf '%q'`. A directory named
   `x"; rm -rf ~; #` would otherwise become executable code.
7. **New configuration variables must be added to `MO_CONFIG_VARS`** in
   `common.sh`, otherwise the config file silently cannot set them.
8. **Do not preset variables at the top of `common.sh`.** It is sourced before
   `load_config`, so a value set there looks like an explicit user setting and
   overrides the config file.

## Conventions

- Settings follow `VAR="${VAR:-default}"` so environment and config keep
  precedence. Precedence overall: CLI flag > interactive input > environment >
  config file > built-in default.
- CLI flags and prompts that change a setting must also set the matching
  `*_EXPLICIT` marker where one exists (currently `DELETE_ORIGINAL`,
  `RENAME_INPLACE`). Ask about deletion only through
  `mo_prompt_delete_original`, which sets it.
- Shared building blocks in `common.sh`, use them instead of writing new
  variants:
  - `mo_find_files` for every scan, so stages and orchestrator counts see
    the same files (skips trash/NAS folders, hidden ones unless
    `INCLUDE_HIDDEN=true`, always exits 0).
  - `mo_worker_start` and `mo_w_*` for xargs workers: one result token per
    file, `mo_w_claim` (lock + re-check), `mo_w_commit` (`mv -n` + check).
  - `mo_job_start`/`mo_job_wait`/`mo_job_stop` for anything long-running
    (xargs batch, ffmpeg encode, a stage), so signals are handled at once.
  - `mo_validate` with a rule in `MO_RULES` for every new setting.
  - `mo_mktemp` for temp files, `mo_lock_workdir` for the run lock.
- Exit codes: `2` bad option or setting, `1` cannot start (missing tool,
  busy directory).
- Box output uses fixed columns: `printf "%b║%b  %-21s %-37s %b║%b\n"`. Do not
  pad labels by hand, it breaks as soon as wording changes.
- Exit codes of the xargs scripts: `0` done, `MO_RC_PARTIAL` (3) finished
  with per-file failures, `130` interrupted, `255` fatal. The orchestrator
  continues after 3 and aborts on anything else. See common.sh.
- Per-file results go to `$CURRENT_RUN_LOG` as single tokens (`SUCCESS`,
  `SKIP`, `FAIL`, `DISCARD`, `COLLISION`, `SUSPECT`, `DRY`), counted in
  `finalize_stats`. Do not translate those tokens.
- Log lines must stay short: parallel workers append to one file, which is
  only atomic below the system buffer size.

## Verification practice

Claims about behaviour should be measured, not reasoned about. The test suite
shows the patterns: a stub placed first in `PATH` forces a failure at exactly
one point (`mv` refusing, exiftool failing, an encoder vanishing mid-run, a
competing worker writing the output) and then runs the real tool via
`PATH="${PATH#*:}"`. Timing-dependent behaviour is worth repeating a few
hundred times: the `cjxl -h | grep -q` check below failed 3 times in 300. Things that
looked obviously right and were not:

- `gif2webp -lossless` does not exist; it made every call fail for months.
- `-tag:v hvc1` produces correctly encoded but undecodable files on some
  encoders.
- `format=nv12|p010` lets the filter pick p010 and half-fills the buffer.
- A single probe slice from the middle predicted "74 % larger" for a file that
  actually shrank by 53 %.
- GNU `mv -n` returns 0 even when it refuses to overwrite (coreutils 9.10).
  Test whether the source is gone, not the exit code.
- GNU xargs returns 124 when a worker exits 255, not 255.
- `cjxl` 0.11 does copy EXIF/XMP from PNG, but Brotli-compressed; exiftool
  without `IO::Uncompress::Brotli` then sees nothing and "updates 0 files"
  with exit 0. `--compress_boxes=0` fixes that.
- `exiftool -tagsFromFile src -all:all` drops PNG text chunks (`Artist`,
  `Copyright`); `-all` maps them to EXIF. Without `-m`, a harmless "[minor]"
  warning makes exiftool exit 1.
- `set -e` is ignored inside a function called as `f || rc=$?`. Worker
  bodies must not be wrapped that way, or the safety net is off.
- Bash starts background jobs with SIGINT ignored. A script that was itself
  started in the background of a non-interactive shell has SIGINT ignored
  from the start and cannot reset it, so neither can anything below it.
  `mo_job_stop` therefore sends INT and TERM.
- A `bash -c` worker killed by SIGINT without an INT trap skips its EXIT
  trap. With `trap 'exit 130' INT` it runs.
- ffmpeg finishes the file (flush, trailer) after SIGINT; with x265 `slow`
  that took about a second. Waiting for the job's top process is not enough.
- `cjxl` cannot read CMYK JPEGs in any mode; `file` shows "components 4".
  A pixel-based fallback for a *truncated* JPEG "succeeds" and produces a
  larger, equally damaged file.
- `--compress_boxes=0` keeps JPEG → JXL reconstruction bit-exact (tested
  with EXIF, GPS and XMP).
- `cmd | grep -q` under `pipefail` is unreliable: grep exits at the first
  match, `cmd` may die of SIGPIPE, and the pipeline then reports failure.
  Capture the output into a variable first.

Useful checks:

```bash
# is the output really lossless?
jxlinfo out.jxl | grep -i loss
djxl out.jxl dec.png && cmp dec.png source.png

# picture comparison, same method the suite uses
ffmpeg -ss T -i out.mp4 -ss T -i src.mp4 \
  -lavfi "[0:v]select=eq(n\,0),setpts=0[x];[1:v]select=eq(n\,0),setpts=0[y];[x][y]psnr" \
  -frames:v 1 -f null -

# which GPU encoder options actually work on this machine
./scripts/h264-to-h265.sh --gpu-selftest /path/to/video.mp4
```

Measured PSNR separation with the in-memory method: sound re-encodes 31–38 dB,
CRF 51 still 24 dB, broken colour formats 9–12 dB. Threshold is 18 dB. If the
comparison method changes, re-measure the threshold — it is not portable
between RGB and YUV comparisons.

## Open issues

The external review of `scripts/img-to-jxl.sh` (24 points, see CHANGELOG) is
fixed across all scripts as of 2026-10-04 and was tested against real files,
including fault injection with stub commands and signal tests. Left over:

1. **In place, a renamed misnamed source can be seen twice by `find`.** A
   kept `foo.jpg` that is really a PNG becomes `foo.png` after conversion;
   if `find` reaches the new name later it is counted once more as skipped.
   Counts only, no data effect.

2. **Arithmetic-coded and 12-bit JPEGs are untested.** No tool here could
   produce one. They should end as `[ERROR] JPEG could not be decoded`
   (original kept), but that is reasoned, not measured.

3. **The orchestrator's state file does not store every setting.**
   `IMG_TARGET`, `IMG_COPY_METADATA`, `GIF_TARGET` and others are not in
   `save_state`, so a resumed run takes them from config/environment again.

4. **`h264-to-h265.sh` moves its output with plain `mv`.** It is sequential
   and holds the run lock, so nothing else writes there; still, `mv -n` with
   the same check as `mo_w_commit` would make it airtight.

5. **"Terminated" lines.** When a script was started with SIGINT ignored
   (see verification notes), stopping it prints one bash "Terminated" line
   per killed encoder. Cosmetic.

## Things deliberately not done

- **Parallel video encoding.** For CPU it gains nothing, x265 already
  saturates the cores. For GPU it would help, but only with a limit of 2–3,
  and the sequential loop carries the progress counter, resumable counters,
  the mid-run GPU→CPU switch and the salvage logic. See CHANGELOG.
- **Porting to Python.** Assessed: it would remove roughly half the bug
  classes (quoting, `set -e`, `export -f`, string parsing) but none of the
  domain bugs, which were the expensive ones. The audit value of a readable
  shell script for a tool that deletes files was judged higher. Revisit if
  parallel GPU encoding becomes important, and only with a test suite written
  against the current behaviour first.
- **A memory throttle for `cjxl`.** Measured 266 MB peak per process on a
  12 MP image, so 16 workers need ~4.3 GB. Not a problem in practice.
