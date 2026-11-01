# CLAUDE.md

Working notes for continuing on this project. README.md describes what the
tool does for a user; this file describes how to change it without breaking
it. CHANGELOG.md records why things look the way they do.

## What this is

A Python program (`media-optimizer`, package `media_optimizer/`) that
batch-converts images, GIFs and videos to more efficient formats. It computes
little itself — it orchestrates `ffmpeg`, `cjxl`, `cwebp`, `gif2webp` and
`exiftool`. Treat it as a process orchestrator.

Up to pre-release 0.3.1 it was a set of Bash scripts. They were replaced by this
Python version on 2026-10-10 (last commit with them: `4d3a676`); a backup is
in `~/Software/Claude/Projects/media-optimizer/bash-backup-2026-10-10/`.
Pre-release 0.4 is the first Python version. Python 3.10+, dependencies
send2trash and Pillow. Developed on Fedora 44 with
an RX 9070 XT (VAAPI) and a Ryzen 7 9700X.

**This tool deletes user files.** Every change touching deletion, renaming or
verification is tested against real files before it is called done, and every
bug fixed gets a test that fails without the fix.

## Layout

Same layout as the owner's other Python projects (keepbest, lyrembed):
package folder `media_optimizer/`, launcher `run.py`, tests in `tests/`,
version only in `config.py` (`__version__`). All imports inside the package
are relative.

```
cli.py           argparse, one subcommand per stage, signal handlers, main()
config.py        Settings dataclass: default + rule + doc per field; JSON config,
                 MO_* environment, renamed keys, exit codes, state/log locations
orchestrator.py  full run: counts, preflight, stages in-process, resume state, restart loop
stages/base.py   Context, results, Counters, mode defaults, BatchStage (thread pool)
stages/images.py gifs.py clips.py   parallel stages on BatchStage
stages/video.py  HEVC stage: decisions in the main thread, GPU and CPU lanes, VideoCache
stages/verify.py check outputs, --fix, --run
safety.py        RunLock, Claim, temp names, commit (no overwrite), Trash
proc.py          running tools in their own process group, stop_all, Stopped/ToolMissing
media.py         ffprobe JSON, verify_video, verify_visual (PSNR; WebP via Pillow)
gpu.py           gpu_args (the only place), device detection, self-test
files.py         find_files, source_files (folder choice), real_type, jpeg_info, gif_width, names
folders.py       the subfolder question and the folder picker (keepbest's model)
cache.py         per-stage caches (v2 format), old-format detection, conversion, pruning
ui.py            StyleUI, say/badge, boxes, prompts, progress line, hold-open
runlog.py        the run log
journal.py       the file journal for picktag (JSON Lines)
events.py        --events FILE: the run's progress as JSON lines, for the browser page
results.py       --results FILE: the run's numbers at its end, for keepbest; Tally, the fold both use
questions.py     the full run's questionnaire as data (terminal and page), the page's settings schema
web.py           the browser page's server (--web): copied rules from cdimport/lyrembed
animated_webp.py media-optimizer-webp, its own command: animated WebPs from GIFs -> AVIF (a BatchStage)
static/          the page: app.js (Preact + htm, no build step), style.css, base.css and icons.js
                 (lyrembed's, see below), logo.svg, vendor/ with LICENSES.txt
desktop.py       the app-menu entry (--install-desktop-entry), copied from keepbest and lyrembed
notify.py        a desktop notification (notify-send with an "Open" button, else gdbus), copyable
selfupdate.py    a newer release: git ls-remote tags (x.y or x.y.z), at every start in a thread (known: the last answer), pipx commands, restart (exec)
```

## Language

Everything is English: messages, prompts, comments, docs. Prompts still accept
"j"/"ja"/"nein" as answers; that is tolerance on input. State files with older
German names (`.defekte_videos.txt`, `.video_verdaechtig.txt`) are renamed
once.

## Hard invariants

Breaking any of these has caused real data loss or silent corruption before.

1. **Write to a temp file first.** Output goes to
   `<target>.part.<pid>.<random>.<ext>` (`Claim.temp`) and is only moved into
   place after it passed verification.
2. **Never delete an original before the output is verified and in place.**
   Deletion is the last step of a worker, after `safety.commit`. For video
   that means `verify_video` (runtime) and `verify_visual` (picture). Never
   after a suspicious picture or for a salvaged source.
3. **Never overwrite.** `safety.commit` links the temp file to the target name
   (fails if it exists); target names are claimed with `.<stem>.molock` and
   re-checked under the claim.
4. **With a target directory the source directory is never modified.** Not
   even to correct a wrong file extension.
5. **A picture check that measured nothing fails.** `verify_visual` returns
   not-ok when the source has a runtime but no sample could be compared.
   Turning "not measurable" into a pass hid the AVIF bug: every AVIF original
   was deleted unchecked.
6. **Every external tool goes through `proc.run`**, never `subprocess`
   directly: otherwise a stop does not reach it (own process group,
   registry) and an interrupted file would count as failed.
7. **GPU arguments only from `gpu.gpu_args`**, built per file. It is a pure
   function, so probe and encode cannot drift apart.

## Rules for changes

- **End a file with an exception, not a return code**: `safety.Failed`,
  `safety.Conflict`. `proc.Stopped` means "interrupted, do not count".
  `proc.ToolMissing` aborts the batch (exit 255). `BatchStage._guarded`
  turns any other exception into FAIL with the original kept, so do not
  catch broadly inside a worker before the commit.
- **Video jobs run in parallel.** Anything a job changes that others see
  (counters, cache, lists, encoder_mode, the progress board) goes through
  `self.lock` / `self._count`. Decisions stay in the main thread. A job the
  GPU failed on is handed back (`self.requeue`) and queued on the CPU lane;
  a worker never starts a CPU encode itself (that made three x265 at once).
- **Leftovers are recognised only by `files.is_temp_name`** (target name,
  `.part.<pid>.<random>.`, the same extension again). A glob like
  `*.part.*` deleted user files such as `Movie.part.1.mp4`.
- **The resume state is per source folder** (`orchestrator.state_file`). A
  source given on the command line only ever resumes its own state.
- **New settings**: a field in `Settings` with rule and doc, then regenerate
  `config.example.json` (command in `tests/test_units.py`); the test fails
  until it is in sync. Its topic on the browser page follows from its place
  among the fields (`config.GROUP_STARTS`). A setting the full run should ask
  about goes into `questions.FULL_RUN`, never as a prompt in the
  orchestrator: the terminal and the page both read that table, and a
  condition there is data (`when=("png_mode", "lossy")`) so the page can
  evaluate it too. CLI flags set settings through `_value`/`_flag` in
  `cli.py`, so they are validated by the same rule. A renamed setting goes
  into `config.RENAMED`.
- **The page's *Defaults* edit the config file** (`config.save_config`,
  `Session.save_defaults`): what the file says, read without this start's
  environment and options (`load_settings(ignore_env=...)`), and those stay
  above it on the form. Comments and unknown keys are kept, a value back at
  its default leaves the file, the folder choice is never saved.
- **Scans of the source only through `files.source_files`** (same skip rules
  and the same folder choice everywhere: stages, counts, preflight). It wraps
  `files.find_files`, which the target-side scans (verify, state files) use.
- **The folder choice** (`subfolders`, `exclude_folders`) is settled by
  `folders.resolve` before the run state is saved, so a resumed run keeps it.
  An excluded folder loses only its own files, never its subfolders. It
  belongs to the folder it was given with: another directory starts from the
  config file alone (`config.FOLDER_CHOICE`; the page: `Session._folder_choice`).
- **`cache_write` false** keeps a run's outcomes in memory only
  (`StageCache(record=False)`): read as usual, nothing added to the file.
- **Caches** (`cache.py`): a stage remembers an outcome by returning
  `FileResult(..., cache="<outcome>")`; `BatchStage._guarded` looks entries up
  before `handle` and records them after it. Only outcomes that leave no
  output to find next time, never failures. Each stage lists the settings its
  outcomes depend on (`cache_settings`, `video.CACHE_SETTINGS`); add a new
  setting there when it changes a result. Old-format video caches are only
  converted after `resolve_legacy_caches` asked, and only under the run lock.
  `cache.fingerprint` counts a number in its default's type (the page and
  the environment give 22.0 for 22; a page run missed every cached video).
  The page's folder tree reads the caches with `folders.CacheView`
  (`StageCache(read_only=True)`: never compacted or written) to mark the
  files a run would skip, recounted for the form's settings (`/api/cached`);
  a new stage cache belongs there too.
- **Every change to a media file goes into the journal** (`journal.py`):
  `converted`, `renamed`, `copied`, `removed`, after the action succeeded,
  never for skips, discards, failures or in a dry run. A new place that
  renames, copies or removes a user's file needs its event and a test.
  Take size and mtime of a file before an action changes or removes it
  (`journal.snapshot`). The format is picktag's contract
  (`~/Software/Claude/Projects/picktag/JOURNAL_SPEC.md`, version 1, shared
  with keepbest); changing the meaning of a field or event needs `"v": 2`.
  picktag also relies on the temp and lock names
  (`<name>.part.<pid>.<hex>.<ext>`, `.<stem>.molock`), which it ignores
  while scanning: renaming them breaks picktag too.
- **The events file follows the output calls** (`events.py`): `log.file`
  reports every file outcome, `ui.say` every message (its colour gives the
  level, also without colours: `ui.Color` keeps the name), and each stage
  reports one `done` per finished file (`BatchStage._guarded`,
  `VideoStage._decide`/`_process`). A new way for a file to end needs its
  `done`. The terminal output must stay byte-for-byte the same with and
  without `--events`.
- **The results file is keepbest's contract** (`results.py`, `--results`):
  written once at `run_end` from the same events (`events.listener`), so
  the page's counts (`web.Progress` builds on `results.Tally`) and keepbest's
  are the same. Changing the meaning of a field needs `"v": 2`; keepbest
  checks `v` and shows nothing for a file it can't read. The page passes
  `--results` on to its child run. Same rule as the events: stdout and the
  exit code stay as they are, a write problem warns once.
- **The browser page is a layer, not a second program** (`web.py`). Its
  security rules are copied from cdimport and lyrembed and each has a test
  (`tests/test_web.py`): 127.0.0.1 only, the token-to-cookie redirect with
  the port in the cookie name, the Host and Origin checks, JSON-only POSTs,
  the CSP, static files only from `static/` with a fixed list of suffixes.
  The look is lyrembed's, cdimport's and keepbest's, libadwaita's, which also
  fits picktag: `static/base.css` and `static/icons.js` are copies of
  lyrembed's, unchanged, like `vendor/` (compare with `diff`, change them
  there and copy them back); only the purple accent and what only this page
  has live in `style.css`. The header bar is program (the folder under it),
  steps in the middle (icons, no numbers; the form shows all stages, a run
  its own), actions. `logo.svg` is for the header and the browser tab, the
  full `icon.svg` only at 112 px on status pages. What shows after the server
  stopped is in the page from the start (`hidden`), or its icon is asked of a
  server that is gone. Clicking through is a manual check: Playwright in a
  venv outside the repo, `chromium.launch(channel="chrome")`, both
  `color_scheme`s, 390 px wide with `scrollWidth` against `innerWidth`.
  The address line (`web.ADDRESS_LINE`) is read by keepbest with cdimport's
  `_HANDOFF` pattern: keep its wording. A new file under `static/` needs a
  pattern in `pyproject.toml`'s package-data (tested).
  The run view reads top to bottom as keepbest's page (a section per
  stage, the summary tiles, the end card) and follows the end while the
  reader is there; a stage sends only its newest files (`RECENT_FILES`),
  a finished one folds them away. A preview's files are kept whole
  (`Progress.planned`), but only sent when their list is opened (`/api/planned`).
  The page never runs a stage itself: it starts `run -y … --events FILE`
  as a child on a pty, with every setting as `MO_*` (`config.to_env`; the
  environment beats the config file and inherited variables), reads the
  saved state with `orchestrator._pick_state`, and folds the events into a
  small state (`web.Progress`) instead of sending every event to the page.
  The form's values exist only in the run they started (`Session._last`):
  a preview's real run (`run_for_real`) and the way back to the form
  (`back_to_form`) take them from there. In the terminal `orchestrator.run`
  leaves the real run in `Request.then` (`same_settings`: nothing asked again).
  A saved state needs an explicit choice (resume or `--reset`): with `-y`
  the child would resume on its own. When the page goes away during a run,
  the run gets Ctrl+C (130, state saved) unless "Keep running in the
  background" was chosen; a background run ends with a notification and the
  program waits `BACKGROUND_WAIT` for the page (`web.Watch`). The owner does
  not want a silent process left running: keep both halves of that rule.
- **Updates** (`selfupdate.py`, as in picktag and keepbest): only a full run
  started by a person checks (not with `MO_CALLER`, `--events` or
  `--return-url`); `MO_UPDATE_REPO` replaces the repository (the tests point
  it at a missing one). The terminal asks before the run and `exec`s the new
  version with the same argv; the page installs only when no run is going on
  (`Session._update_ready`), refuses starts meanwhile, then `serve` `exec`s
  with `MO_RESTART_PAGE` = "port token" so the tab keeps its address and
  reloads when `boot` changes. The process id stays: keepbest waits on it.
- **Keep compatible with folders the Bash version worked on**: flock on the
  working directory, `.part.<pid>.<random>.<ext>` and `.<stem>.molock`,
  `.video_conversion_cache.txt`, `.broken_videos.txt`, `.video_suspect.txt`,
  `*_pending_deletes.txt`, the log format. Tests in `test_compat.py`.
- Exit codes: `0` done, `3` some files failed (originals kept; a full run
  continues with the next stage), `2` bad option or setting, `1` cannot start,
  `130` interrupted, `255` an encoder vanished.
- Box output uses fixed columns (`ui.box`): do not pad labels by hand.

## Commits, pushing and releases

The same as in keepbest, picktag and lyrembed. This project used to work
differently (a bare "Release X" commit, two-part versions, notes in /tmp);
that is history, not a pattern.

- **Commits**: commit locally when a piece of work is done and its tests
  pass; experiments stay uncommitted. English, subject at most 72
  characters, body wrapped at 72, saying what changed and why (what was
  measured, not what was assumed). End the report with what was committed
  and its message.
- **Pushing**: commits the owner has approved are pushed to `main` without
  asking again, and so are finished, substantial ones that belong in a
  future release. Trivial or experimental commits stay local. Before
  pushing, check the diff for details of the owner's own files and folders.
  **Tags and GitHub releases only with the owner's go-ahead.**
- **Unreleased changes**: each user-visible change goes under
  `## Unreleased` at the end of `CHANGELOG.md` (this file's format: a
  `Change | Why` table per release, oldest first). Don't propose a release
  after every change: say what is waiting, and suggest a release with the
  number it would get once enough has gathered (a feature, or a few fixes,
  or one fix that loses data). The owner may also ask to fold a change into
  the current release.
- **Version numbers**: always three parts (`1.2.0`), tags without `v`.
  New features or changed behaviour raise the minor version (1.1.1 → 1.2.0),
  pure fixes the patch (1.1.2). `__version__` in `config.py` is the only
  source (`pyproject.toml` reads it); without a bump `pipx upgrade` doesn't
  pick up changes. The tag `1.0` keeps its two parts (history is not
  rewritten); `selfupdate` reads both forms.
- **Old numbers**: the pre-releases 0.1–0.8 are an archive (the fixes
  between them are tags only); nothing is released as a pre-release any
  more. Earlier numbering is never mentioned anywhere in the repo, the
  tags or the release notes.
- **History**: since 1.0 it is never rewritten (no rebase, no force-push of
  `main`); a mistake in a release is fixed with a new version.

A release, once the owner said so:

1. `config.__version__` raised; the `## Unreleased` heading in
   `CHANGELOG.md` becomes `## X.Y.Z – Title`.
2. The full test suite passes.
3. One commit `media-optimizer X.Y.Z: <what it brings, lower case>`, its
   body a short summary for users and `Tests: N passing`.
4. `git tag -a X.Y.Z -m "media-optimizer X.Y.Z"`, then
   `git push origin main X.Y.Z`.
5. `gh release create X.Y.Z --latest --title "media-optimizer X.Y.Z – Title"
   --notes-file <notes>`. The notes: one line on what changed for the user,
   `### Added` / `### Changed` / `### Fixed` as needed (bullets with a bold
   lead-in), `### Upgrading` (`pipx upgrade media-optimizer`, or the update
   offer in the terminal and the page's *Update* button), and a link to
   `CHANGELOG.md`.

Commit messages and release notes are drafted outside the checkout, in
`~/Software/Claude/Projects/media-optimizer/` (`COMMIT_MSG_<version>.txt`,
`releases/RELEASE_NOTES_<version>.md`), never in the repo or in /tmp.

## Tests

```bash
python3 -m pytest                  # all, about 420 tests, 5 minutes
python3 -m pytest -m "not slow"    # without the signal tests
python3 -m pytest tests/test_video.py -k cache
```

- `run_cli` runs the real program in a subprocess; `main` runs it
  in-process so a test can monkeypatch one step and force a failure exactly
  there. `stub(tool, script)` puts a stand-in for an external tool first in
  PATH, which does its damage and runs the real tool via `PATH="${PATH#*:}"`.
  `vanishing_path(tool)` makes an encoder disappear mid-run. `pty_run`
  answers prompts on a pseudo-terminal.
- `MO_TRASH=none` makes the trash fail; tests delete with `--force-delete`.
  Never let a test reach a real trash: on /tmp, send2trash would create
  `/tmp/.Trash-<uid>`. The fixture fails any test that writes the project's
  own `media-optimizer.log`.
- Safety tests were checked by breaking the code on purpose (commit
  overwriting, no recheck under the claim, AVIF still compared, suspicious
  video deleted, deletion before the move, gpu_workers ignored): each one
  fails. Timing-dependent tests (signals) were repeated; keep doing that.

## Verification practice

Claims about behaviour should be measured, not reasoned about. Things that
looked obviously right and were not (most found while this was Bash; the tool
behaviour still applies):

- `gif2webp -lossless` does not exist; it made every call fail for months.
- `-tag:v hvc1` produces correctly encoded but undecodable files on some
  encoders (5 dB in the self-test on the RX 9070 XT).
- `format=nv12|p010` lets the filter pick p010 and half-fills the buffer.
- A single probe slice from the middle predicted "74 % larger" for a file that
  actually shrank by 53 %.
- `cjxl` 0.11 copies EXIF/XMP from PNG Brotli-compressed; exiftool without
  `IO::Uncompress::Brotli` then sees nothing. `--compress_boxes=0` fixes that
  and keeps JPEG → JXL reconstruction bit-exact (tested with EXIF, GPS, XMP).
- `exiftool -tagsFromFile src -all:all` drops PNG text chunks (`Artist`,
  `Copyright`); `-all` maps them to EXIF. Without `-m`, a harmless "[minor]"
  warning makes exiftool exit 1.
- `cjxl` cannot read CMYK or arithmetic-coded JPEGs (cwebp reads arithmetic
  ones). A pixel-based fallback for a *truncated* JPEG "succeeds" and produces
  a larger, equally damaged file.
- An animated AVIF has two video streams: a still first, the animation second.
  `[0:v]` is the still; seeking past 0 s in it yields no frame.
- ffmpeg 8.1 cannot decode animated WebP (skips ANIM/ANMF). Pillow can, but a
  frame's duration is only known after `load()`. ffmpeg 9.0 can (composited
  frames identical to Pillow's), but reports no runtime, so `verify_visual`
  with one as its source would pass without measuring: `verify_from_webp`.
- Seeking by time in a WebP-to-AVIF pair can land one frame off: 16 dB on a
  sound encode of a noisy GIF. By frame number the same pair passes.
- `-pix_fmt yuv420p` flattens a transparent GIF to white, with no error: the
  gif stage with `gif_target avif` loses transparency (measured with ffmpeg
  9.0.2). A second stream with `alphaextract`, gray and libaom encodes alpha,
  but nothing here reads it back to check it.
- glycin 2.1.5 (Fedora 44, GNOME's image viewer) animates WebP, GIF and APNG,
  but shows animated AVIF and JPEG XL as stills. 2.2.2 (Fedora 45) plays AVIF.
- libsvtav1 (SVT-AV1 3.1.2) hangs forever on frames narrower than about 31 px
  (8, 16, 24 hung; 31, 32, 64 worked; the height does not matter), and only
  SIGKILL ends it — `timeout` without `-k` waits forever too. libaom-av1
  encodes them. `media.av1_encoder(width)` picks accordingly; AV1 encodes of
  GIFs and clips also have a 10-minute limit (`proc.run(timeout=...)`).
- Cache paths must be compared with symlinks resolved (`cache.canonical`):
  the owner reaches the same disk as `~/Videos/Link` and `/mnt/...`, and
  cached videos were converted again after switching.
- Terminal width is counted in columns, not characters (`ui.fit_width`):
  emoji and CJK file names take two columns and made the progress line wrap.
- Re-running a stage used to copy a misnamed file into the target again on
  every run (`isgif.gif`, `isgif_1.gif`, ...); the take-over now compares
  content first.
- ffmpeg finishes the file (flush, trailer) after SIGINT; with x265 `slow`
  that took about a second. `proc.stop_all` waits for every process group.
- A program started in the background of a non-interactive shell inherits
  SIGINT as ignored; children inherit that too unless the program installs a
  handler (`cli.install_signal_handlers` does; tested).
- Two parallel GPU encodes were 26 % faster than one in plain ffmpeg, three
  barely more; with the picture checks in the pipeline three still helped.
- ffmpeg rotates a portrait video (landscape frames plus a display matrix)
  upright when encoding and drops the matrix. Comparing with
  `-noautorotate` put the upright output against the sideways source: 8 dB
  for a correct encode. Without it both sides are as displayed: 48 dB.
- A `-c copy` slice cut with `-ss` starts at the previous keyframe, but the
  MP4 edit list hides the pre-roll: compared with an accurate re-encode of
  the same range it measured 44 dB (x264, 10 s GOP, ffmpeg 9). For sizes
  the pre-roll still counts (open issue 1).
- ffprobe reports a single JPEG as 0.04 s long, so a photo taken as a
  video's original fails any runtime comparison.
- "Lossless" WebP is not lossless for every PNG: a 16-bit PNG comes back as
  8 bits (different pixels), and colours under fully transparent pixels
  change (invisible). `cjxl -d 0` is bit-exact for both; gif2webp keeps
  every frame and duration (compared frame by frame with Pillow). The
  journal's `lossless` follows these measurements.

- `avif_crf` on eight real clips (five H.265, three H.264; libsvtav1 preset 6;
  size against the source, median; VMAF average, median; clips larger than the
  source): 32: 46 %, 92.6, 0; 28: 62 %, 93.6, 0; 24: 84 %, 94.7, 3; 20: 106 %,
  95.4, 5; 16: 134 %, 96.2, 7. Hence the default of 28.

Picture comparison: one ffmpeg call per sample compares both files in memory;
the best of several samples counts. Measured separation (YUV average): sound
re-encodes 31–38 dB, CRF 51 still 24 dB, broken colour formats 9–12 dB,
correct AVIF 42–45 dB, wrong AVIF 7 dB. For WebP (Pillow, YCbCr weighted
4:1:1): correct 23.7–44 dB, different content 10–11 dB. Threshold 18 dB. If a
comparison method changes, re-measure — RGB and YUV results are not
comparable. Re-measured after dropping `-noautorotate` (2026-10-15): unrotated
files give the same values as before (re-encode 49, CRF 51 29, other content
7, AVIF 46, WebP 31 dB).

Useful checks:

```bash
djxl out.jxl dec.png && cmp dec.png source.png       # really lossless?
media-optimizer gpu-selftest /path/to/video.mp4      # which GPU options work
```

## Open issues

1. **The probe over-predicts savings with long keyframe intervals.** The
   reference slice is cut with `-c copy` and starts at the previous keyframe;
   on a synthetic clip with x264's default GOP the reference was 27 Mb/s for
   an 8 Mb/s file, the probe said "71 % smaller", the result was 2.6 %
   smaller. Needs real footage before the method changes.
2. **12-bit JPEGs are untested** (no tool here writes one).
3. **The video and clip stages keep container tags (`-map_metadata 0`),
   but not an XMP box.** Matters only if picktag ever writes its tags into
   the files instead of its database.

4. **The gif stage's AVIF path flattens transparency** (see Verification
   practice) and deletes the GIF. `media-optimizer-webp` refuses such files.

## Things deliberately not done

- **A memory throttle for `cjxl`.** Measured 266 MB peak per process on a
  12 MP image, so 16 workers need ~4.3 GB. Not a problem in practice.
- **More than one CPU video encode at a time.** x265 already saturates the
  cores.
- **Keeping cover art.** The video encode maps `0:v:0`, audio and subtitles:
  an MP4 cover (attached picture) or an MKV attachment is not carried over
  (measured 2026-10-26). Covers are usually generated and easy to make again.
