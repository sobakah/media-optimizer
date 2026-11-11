# media-optimizer – notes for Claude Code

media-optimizer (package `media_optimizer/`) batch-converts pictures, GIFs and videos to more
efficient formats and checks every output against its original before the original goes. It
computes little itself: it orchestrates `ffmpeg`, `cjxl`, `cwebp`, `gif2webp` and `exiftool`, so
treat it as a process orchestrator. Python 3.10+, dependencies send2trash and Pillow; a full run
asks on a page in the browser (Preact + htm, no build step), `-y`, the stages and `verify` run
without questions. User docs: `README.md`; the file journal format is picktag's
`docs/JOURNAL_SPEC.md`. **This program deletes the owner's files**: every change touching
deletion, renaming or verification is tested against real files before it is called done.

## The program family

<!-- This section is the same in all six repositories. Change it in all of them or none. -->

Six programs by the same owner, each in its own private repository `sobakah/<name>` with its own
CLAUDE.md. Other sessions work in the other repositories: change one only when the owner asks.

| Program | What it does | Works with |
|---|---|---|
| picktag | Tags and searches pictures, videos and documents (GTK 4, libadwaita) | reads keepbest's and media-optimizer's file journals; starts keepbest for *Find Duplicates* |
| keepbest | Finds duplicate pictures and videos and keeps the best copy (page in the browser) | hands its tab to media-optimizer; writes a file journal |
| media-optimizer | Converts pictures, GIFs and videos to smaller formats (page in the browser) | started from keepbest; writes a file journal |
| cdport | Rips CDs, tags them and adds covers and lyrics (page in the browser) | starts lyrembed; D-Bus service for Jewelcase |
| lyrembed | Finds lyrics, embeds them in music files, romanizes them (page in the browser) | started from cdport; D-Bus service for Jewelcase |
| Jewelcase | A GNOME music player for an album library, with Spotify (GTK 4, libadwaita) | uses cdport and lyrembed; never writes music files |

The file journal format is specified in picktag's `docs/JOURNAL_SPEC.md`. The programs with a page
share its look (libadwaita's design language): `static/base.css` and `static/icons.js` are
lyrembed's, copied unchanged into cdport, keepbest and media-optimizer; change them in lyrembed and
copy them back. Setup details of the owner's machine are in `CLAUDE.local.md` (not in git).

### Working rules

- **Commits**: commit finished work once its tests pass, locally, on the default branch;
  experiments stay uncommitted. **Push, tags and releases only when the owner says so**; a request
  to release includes commit, tag and push. Pushed history is never rewritten; a mistake in a
  release is fixed with a new version.
- **Commit messages**: English; a subject of at most 72 characters saying what changed; a body
  wrapped at 72 saying why and what was verified. Written directly, no draft files.
- **Before pushing**, check the diff for personal data: the owner's name, home paths, file and
  folder names or contents from the owner's libraries, details of the owner's machine. Those
  belong in `CLAUDE.local.md`.
- **Verify, don't assert.** Run the code before saying something works; say what was tested and
  what wasn't. Outside reviews have been wrong before: check each claim.
- **Every bug fix comes with a test that fails without it.**
- **Tests never touch the owner's data** (libraries, music, settings, journals, the trash), the
  network or real hardware; they use temporary folders and generated media.
- **Ask first** before installing system packages, deleting or overwriting the owner's files, or
  changing a default the owner chose.
- Stop processes by PID, never with `pkill -f`. Commands for the owner to paste are written for
  fish; examples in the docs use bash.

### Versions and releases

- Versions have three parts and tags no `v` (`1.2.0`). New features or changed behaviour raise the
  minor version, fixes alone the patch. The version lives in one place (see Releasing below).
- Every user-visible change goes under `## [Unreleased]` at the top of `CHANGELOG.md` when it is
  committed. Don't propose a release after every change: say what is waiting and suggest one when
  a feature or a few fixes have gathered.
- A release: the version raised, `## [Unreleased]` becomes `## [X.Y.Z] — YYYY-MM-DD`, the full test
  suite passes, one commit `<program> X.Y.Z: <what it brings>`, an annotated tag
  `git tag -a X.Y.Z -m "<program> X.Y.Z"`, push of the branch and the tag, then a GitHub release
  titled `<program> X.Y.Z` whose notes are that version's CHANGELOG section.
- Installed copies look for newer releases by their tags, so a tag above the current version must
  never exist. Pre-releases below 1.0.0 on GitHub are an archive; they are not mentioned in the
  repository.

### Documentation

- `README.md` is for people using the program: what it does, installing, using it, settings,
  working with the other programs, limits. Current state only.
- `CHANGELOG.md` follows Keep a Changelog, newest first: `### Added`, `### Changed`, `### Fixed`,
  bullets in the user's terms (a bold lead-in for the important ones), no internal reasons, no
  commit hashes.
- `CLAUDE.md` is for Claude sessions: the same section order in every repository. Keep a reason
  only where it stops a session from undoing a decision.
- Update README and CHANGELOG in the commit that changes the behaviour.

### Wording

British spelling (colour, behaviour, analyse); names of code, GTK and GNOME stay as they are.
Program names are written as above (lower case, except Jewelcase). Terms used in all six:

| Use | Not |
|---|---|
| folder | directory (except in code and option names) |
| the page (the program's page in the browser) | web UI, frontend, site |
| settings | options, preferences (except GNOME's *Preferences* window in the GTK apps) |
| dry run (`--dry-run`) | simulation, test run |
| the trash; *move to the trash* | recycle bin, wastebin |
| the owner (in CLAUDE.md) | the user, the maintainer, a name |
| you (in README) | the user |
| Enter, Esc, Ctrl+C | ENTER, Return, ^C |

## Layout

Package `media_optimizer/`, launcher `run.py`, tests in `tests/`, version only in `config.py`. All
imports inside the package are relative.

```
cli.py           argparse, one subcommand per stage, signal handlers, main()
config.py        Settings dataclass: default + rule + doc per field; JSON config,
                 MO_* environment, renamed keys, exit codes, state/log locations
orchestrator.py  full run: counts, preflight, stages in-process, resume state
stages/base.py   Context, results, Counters, mode defaults, BatchStage (thread pool)
stages/images.py gifs.py clips.py   parallel stages on BatchStage
stages/video.py  HEVC stage: decisions in the main thread, GPU and CPU lanes, VideoCache
stages/verify.py check outputs, --fix, --run
safety.py        RunLock, Claim, temp names, commit (no overwrite), Trash
proc.py          running tools in their own process group, stop_all, Stopped/ToolMissing
media.py         ffprobe JSON, verify_video, verify_visual (PSNR; WebP via Pillow), AV1 slots
gpu.py           gpu_args (the only place), device detection, self-test
files.py         find_files, source_files (folder choice), real_type, jpeg_info, gif_width, names
folders.py       which folders a run covers (keepbest's model): counts, cached files for the page
cache.py         per-stage caches (v2 format), old-format detection, conversion, pruning
ui.py            StyleUI, say/badge, boxes, progress line
runlog.py        the run log
journal.py       the file journal for picktag (JSON Lines)
events.py        --events FILE: the run's progress as JSON lines, for the page
results.py       --results FILE: the run's numbers at its end, for keepbest; Tally, the fold both use
questions.py     the full run's questions as data, the page's settings schema
web.py           the page's server: rules copied from cdport and lyrembed
animated_webp.py media-optimizer-webp, its own command: animated WebPs from GIFs -> AVIF (a BatchStage)
static/          the page: app.js, style.css, base.css and icons.js (lyrembed's), logo.svg,
                 icon.svg, index.html, vendor/ with LICENSES.txt
desktop.py       the app-menu entry (--install-desktop-entry), as in keepbest and lyrembed
notify.py        a desktop notification (notify-send with an "Open" button, else gdbus)
selfupdate.py    a newer release: git ls-remote tags, at every start in a thread (known: the last
                 answer), pipx commands, restart (exec)
```

## Commands

```bash
pipx install .                                   # or: python3 -m pip install --user send2trash Pillow
./run.py DIR                                     # the page on DIR, from the checkout
./run.py -y -n SRC DST                           # a dry run without the page
python3 -m pytest                                # all, about 420 tests, 3 minutes
python3 -m pytest -m "not slow"                  # without the encodes and signal tests
python3 -m pytest tests/test_video.py -k cache
python3 -m pyflakes media_optimizer tests run.py # the linter (dev extra)
media-optimizer gpu-selftest /path/to/video.mp4  # which GPU options work on this machine
djxl out.jxl dec.png && cmp dec.png source.png   # really lossless?
```

`config.example.json` is generated from `Settings` (the command is in `tests/test_units.py`); a
test fails until it is in sync.

## Rules that must not break

Breaking any of these has caused data loss or silent corruption before.

1. **Write to a temp file first.** Output goes to `<target>.part.<pid>.<random>.<ext>`
   (`Claim.temp`) and is moved into place only after it passed verification.
2. **Never delete an original before the output is verified and in place.** Deletion is the last
   step of a worker, after `safety.commit`; for video after `verify_video` (runtime) and
   `verify_visual` (picture). Never after a suspicious picture or for a salvaged source.
3. **Never overwrite.** `safety.commit` links the temp file to the target name (fails if it
   exists); target names are claimed with `.<stem>.molock` and re-checked under the claim.
4. **With a target folder the source folder is never modified**, not even to correct a wrong
   extension.
5. **A picture check that measured nothing fails.** Counting "not measurable" as a pass once let
   every AVIF original be deleted unchecked.
6. **Every external tool goes through `proc.run`**, never `subprocess` directly: otherwise a stop
   does not reach it (own process group, registry) and an interrupted file counts as failed.
7. **GPU arguments only from `gpu.gpu_args`**, built per file, a pure function, so probe and encode
   cannot drift apart.
8. **Leftovers are recognised only by `files.is_temp_name`** (target name, `.part.<pid>.<random>.`,
   the same extension again). A glob like `*.part.*` deleted files such as `Movie.part.1.mp4`.
9. **End a file with an exception, not a return code**: `safety.Failed`, `safety.Conflict`.
   `proc.Stopped` means "interrupted, do not count"; `proc.ToolMissing` aborts the batch (exit 255).
   `BatchStage._guarded` turns any other exception into FAIL with the original kept, so don't catch
   broadly inside a worker before the commit.
10. **Video jobs run in parallel.** Anything a job changes that others see (counters, cache, lists,
    encoder_mode, the progress board) goes through `self.lock` / `self._count`; decisions stay in
    the main thread. A job the GPU failed on is handed back (`self.requeue`) to the one CPU lane; a
    worker never starts a CPU encode itself.
11. **Nothing asks in the terminal.** A full run without `-y` is the page (`cli._run_web`); `-y`,
    the stages and `verify` go by their settings and need their folders given. What would be a
    question has a safe answer there (a failed trash lists the originals, a saved state resumes).
    The terminal output stays: the page's log shows it, and `-y` runs have nothing else.
12. **The resume state is per source folder** (`orchestrator.state_file`); a source on the command
    line only resumes its own. A dry run never removes it; declining to resume keeps it.
13. **Scans of the source only through `files.source_files`** (same skip rules and folder choice
    for stages, counts and preflight). The folder choice (`subfolders`, `exclude_folders`) is
    settled by `folders.resolve` before the run state is saved; an excluded folder loses only its
    own files. It belongs to the folder it was given with: another folder starts from the config
    file alone (`config.FOLDER_CHOICE`; the page: `Session._folder_choice`).
14. **Every change to a media file goes into the journal** (`journal.py`): `converted`, `renamed`,
    `copied`, `removed`, after the action succeeded, never for skips, discards, failures or in a
    dry run, and none when size and time can't be read (picktag matches by them). A new place that
    renames, copies or removes a file needs its event and a test. Take size and mtime before an
    action changes the file (`journal.snapshot`). Changing a field's meaning needs `"v": 2`.
    picktag also relies on the temp and lock names: renaming them breaks picktag too.
15. **The events file follows the output calls** (`events.py`): `log.file` reports every file
    outcome, `ui.say` every message (`ui.Color` keeps the level without colours), each stage one
    `done` per finished file (`BatchStage._guarded`, `VideoStage._decide`/`_process`). A new way for
    a file to end needs its `done`. The terminal output stays byte-for-byte the same with and
    without `--events`.
16. **The results file is keepbest's contract** (`results.py`): written once at `run_end` from the
    same events, so the page's counts (`web.Progress` builds on `results.Tally`) and keepbest's
    agree. Changing a field's meaning needs `"v": 2`. stdout and the exit code stay as they are; a
    write problem warns once.
17. **The page is a layer, not a second program** (`web.py`). It never runs a stage itself: it
    starts `run -y … --events FILE` as a child on a pty with every setting as `MO_*`
    (`config.to_env`; the environment beats the config file and inherited variables). Its security
    rules each have a test in `tests/test_web.py`: 127.0.0.1 only, the token-to-cookie redirect with
    the port in the cookie name, Host and Origin checks, JSON-only POSTs, the CSP, static files only
    from `static/` with a fixed list of suffixes. The address line (`web.ADDRESS_LINE`) is read by
    keepbest with cdport's `_HANDOFF` pattern: keep its wording. The process id stays across an
    update (`exec`): keepbest waits on it.
18. **No silent process left behind.** When the page goes away during a run, the run gets Ctrl+C
    (130, state saved) unless "Keep running in the background" was chosen; a background run ends
    with a notification and the program waits `BACKGROUND_WAIT` for the page (`web.Watch`). The
    owner wants both halves.
19. **A saved state needs an explicit choice on the page** (resume or `--reset`): with `-y` the
    child would resume on its own.
20. **Updates** (`selfupdate.py`): only a person's page checks (not with `MO_CALLER` or
    `--return-url`, never `-y` or a stage). The page installs only when no run is going on
    (`Session._update_ready`), refuses starts meanwhile, then `serve` `exec`s with
    `MO_RESTART_PAGE` = "port token" so the tab keeps its address.
21. **Folders the former Bash version worked on keep working**: flock on the working folder, the
    temp and lock names, `.video_conversion_cache.txt`, `.broken_videos.txt`, `.video_suspect.txt`
    (older German names are renamed once), `*_pending_deletes.txt`, the log format. Tested in
    `test_compat.py`.
22. Exit codes: `0` done, `3` some files failed (originals kept; a full run continues with the next
    stage), `2` bad option or setting, `1` cannot start, `130` interrupted, `255` an encoder
    vanished.

### Defaults and methods chosen on purpose (measure again before changing one)

- `hevc_tag` stays empty: `-tag:v hvc1` gives undecodable files when the encoder writes parameter
  sets in-band (5 dB in the self-test).
- The GPU pixel format is fixed (`nv12`, `p010` only with `gpu_allow_10bit`): `format=nv12|p010`
  lets the filter pick p010 and half-fills the buffer.
- `cjxl_threads` 1: without it every parallel cjxl starts a thread per core.
- `cjxl --compress_boxes=0` when metadata is copied: Brotli-compressed boxes are invisible to
  exiftool without Brotli; JPEG → JXL reconstruction stays bit-exact (tested with EXIF, GPS, XMP).
- exiftool with `-all` (`-all:all` drops PNG text chunks) and `-m` (a "[minor]" warning exits 1);
  metadata goes into the temp file before the checks, and an exiftool error keeps the original.
  JPEG sources get no exiftool call: cjxl and cwebp keep their metadata.
- No pixel-based fallback for JPEGs cjxl can't read: a truncated JPEG "succeeds" as an equally
  damaged, larger copy. CMYK and arithmetic-coded JPEGs are `[UNSUPPORTED]`.
- The picture check: the best of several samples counts (single samples drop with variable frame
  rate), threshold 18 dB (`visual_psnr_min`). Measured in YUV: sound re-encodes 31–38 dB, CRF 51
  24 dB, broken colour formats 9–12 dB, correct AVIF 42–45 dB, wrong AVIF 7 dB; WebP via Pillow
  (YCbCr 4:1:1) correct 23.7–44 dB, other content 10–11 dB. RGB and YUV values aren't comparable:
  a new method needs new measurements.
- Both sides are compared as displayed (no `-noautorotate`): a correct portrait encode measured
  8 dB against the sideways source. An animated AVIF is compared on its animation stream (`[0:v]`
  is a still); a WebP-to-AVIF pair by frame number, not time (one frame off: 16 dB).
- Probe slices are spread over the runtime: a single slice from the middle predicted "74 % larger"
  for a file that shrank by 53 %.
- HEVC, VP9 and AV1 sources are skipped: re-encoding them costs quality without saving space.
- `container auto`: MP4 for MP4/MOV/M4V, MKV otherwise (MP4 holds neither Opus nor SRT); one retry
  with AAC and without subtitles.
- `gpu_workers` 2 plus exactly one CPU encode: a second GPU encode is clearly faster, a third barely;
  x265 already uses every core.
- AV1 encodes go through `media.run_av1`, which waits for a slot (`_AV1Slots`): one encoder per four
  cores with `lp=4`, fewer when free memory is short. One encoder per worker froze a desktop
  session and was slower; `lp` doesn't change the output.
- libsvtav1 hangs forever on frames narrower than about 31 px (only SIGKILL ends it; `timeout`
  without `-k` waits too): `media.av1_encoder(width)` takes libaom-av1 there, and AV1 encodes of
  GIFs and clips stop after 10 minutes (`AV1_TIMEOUT`).
- `avif_crf` 28: 32 softened the weakest frames visibly; at 24 several real clips grew past their
  source.
- `avif_crf_retry` 0: a second try at a higher CRF lost visible quality to save little, and
  discarding the AVIF keeps the original without new loss.
- GIFs default to lossless WebP; results larger than the original are discarded.
- `delete_original` and `rename_inplace` are `null` and follow the mode (target folder: off; in
  place: on).
- `lossless` in the journal follows measurements: `cjxl -d 0` is bit-exact; lossless WebP turns a
  16-bit PNG into 8 bits and changes colours under fully transparent pixels.
- Caches never store failures. Own outputs in the video cache hold for any settings (otherwise a
  changed setting encodes an output a second time); `gpu_bitrate`, `gpu_bf`, `gpu_low_power` and
  `gpu_allow_10bit` count for the fingerprint only when changed, so existing caches stay valid.
- `verify --fix` takes only videos as originals: ffprobe reports a JPEG as 0.04 s, so a live
  photo's JPEG failed as `[DURATION]` and `--fix` deleted the output.
- The home folder, those above it, `/` and system folders are never preset or chosen as the source
  (keepbest's rule): a launcher starts in the home folder.
- Skipped files get no line in the run log, or cache hits would fill it.
- Not done on purpose: a memory throttle for cjxl (about 266 MB per process on 12 MP), more than
  one CPU video encode, keeping cover art or MKV attachments (the encode maps `0:v:0`, audio and
  subtitles).

## Conventions

- Everything is English: messages, comments, docs.
- **New settings**: a field in `Settings` with rule and doc, then regenerate `config.example.json`.
  Its topic on the page follows from its place among the fields (`config.GROUP_STARTS`). A setting
  the full run asks about goes into `questions.FULL_RUN`; a condition there is data
  (`when=("png_mode", "lossy")`) so the page can evaluate it. CLI flags set settings through
  `_value`/`_flag` in `cli.py`, so the same rule validates them. A renamed setting goes into
  `config.RENAMED`. A setting that changes a stage's result goes into its `cache_settings`
  (`video.CACHE_SETTINGS`).
- **The page's *Defaults* edit the config file** (`config.save_config`, `Session.save_defaults`):
  what the file says, read without this start's environment and options
  (`load_settings(ignore_env=...)`). Comments and unknown keys are kept, a value back at its default
  leaves the file, the folder choice is never saved.
- **`cache_write` false** keeps a run's outcomes in memory only (`StageCache(record=False)`).
- **Caches** (`cache.py`): a stage remembers an outcome by returning `FileResult(..., cache="…")`;
  `BatchStage._guarded` looks entries up before `handle` and records them after it, only for
  outcomes that leave no output to find next time. Old-format video caches are converted under the
  run lock (`convert_legacy_caches`). `cache.fingerprint` counts a number in its default's type
  (the page and the environment give 22.0 for 22). Paths are compared with symlinks resolved
  (`cache.canonical`). The page's folder tree reads the caches with `folders.CacheView`
  (`StageCache(read_only=True)`, never compacted or written); a new stage cache belongs there too.
- **An output name that is taken** (`BatchStage.output_name`): a stage never asks `nonempty(dest)`
  itself. `media.same_picture` says whether the file there is src's output (same modification time)
  or shows the same; else the next `stem_N.ext` is tried. A new way of writing outputs must keep
  the time (`safety.commit`), or a kept original gets a new number on every run. The video stage
  doesn't do this. Workers with one stem run one at a time (`_one_per_name`).
- **The page's look** is lyrembed's, cdport's and keepbest's (libadwaita): `static/base.css`,
  `static/icons.js` and `vendor/` are lyrembed's copies (compare with `diff`); only the purple
  accent and what only this page has live in `style.css`. Header bar: program (the folder under
  it), steps in the middle (icons, no numbers), actions. `logo.svg` for the header and the tab, the
  full `icon.svg` only at 112 px on status pages. What shows after the server stopped is in the page
  from the start (`hidden`). A new file under `static/` needs a pattern in `pyproject.toml`'s
  package-data (tested).
- **The run view** reads top to bottom as keepbest's page (a section per stage, the summary tiles,
  the end card) and follows the end while the reader is there. A stage sends only its newest files
  (`RECENT_FILES`); a dry run's files and the files a run left alone are kept whole by reason
  (`Progress.groups`) and only sent when their list is opened (`/api/files`). A new reason needs its
  text in `WHY` in `app.js`. The form's values exist only in the run they started (`Session._last`):
  `run_for_real` and `back_to_form` take them from there.
- **The folder picker** is keepbest's: the server lists folders (`web.list_folders`,
  `/api/folders`), names only; a symlink keeps the path it was reached by. Choosing another folder
  makes a new form, so `useFolder` carries the form's changes over (`edits`).
- The page says *preview* for a dry run (`-n`, `--dry-run`); the docs say dry run.
- Box output uses fixed columns (`ui.box`): don't pad labels by hand. Terminal width is counted in
  columns (`ui.fit_width`): CJK and emoji take two.

## Testing

- `run_cli` runs the real program in a subprocess; `main` runs it in-process so a test can
  monkeypatch one step and force a failure exactly there. Both add `-y` to a full run
  (`without_page`). `stub(tool, script)` puts a stand-in for an external tool first in PATH, which
  does its damage and runs the real tool via `PATH="${PATH#*:}"`. `vanishing_path(tool)` makes an
  encoder disappear mid-run.
- Every test has its own home, config and state folder. `MO_TRASH=none` makes the trash fail; tests
  delete with `--force-delete`. Never let a test reach a real trash: on /tmp, send2trash creates
  `/tmp/.Trash-<uid>`. `MO_UPDATE_REPO` points the update check at a missing repository.
- Guards in `conftest.py`: a test fails if it writes the checkout's `media-optimizer.log` or the
  owner's real journal (`~/.local/state/media-optimizer/journal.jsonl`). If the journal guard fires,
  check whether the owner was running media-optimizer at that moment before assuming a leak. Every
  journal line a test writes is checked against the format.
- Safety tests were checked by breaking the code on purpose (commit overwriting, no re-check under
  the claim, a suspicious video deleted, deletion before the move, `gpu_workers` ignored): each one
  fails. Repeat timing-dependent tests (signals) several times.
- The page is tested without a browser. Clicking through is a manual check: Playwright in a venv
  outside the repo, `chromium.launch(channel="chrome")`, both `color_scheme`s, 390 px wide with
  `scrollWidth` against `innerWidth`, on generated files.

## Releasing

The version lives only in `media_optimizer/config.py` (`__version__`; `pyproject.toml` reads it).
Without a raised version `pipx upgrade` doesn't pick up a change. A release, once the owner says so:

1. Raise `__version__`; `## [Unreleased]` in `CHANGELOG.md` becomes `## [X.Y.Z] — YYYY-MM-DD`, with
   the compare links at the end updated.
2. The full test suite and pyflakes pass.
3. One commit `media-optimizer X.Y.Z: <what it brings>`.
4. `git tag -a X.Y.Z -m "media-optimizer X.Y.Z"`, then `git push origin main X.Y.Z`.
5. `gh release create X.Y.Z --latest --title "media-optimizer X.Y.Z" --notes "<that version's
   CHANGELOG section>"`.

`selfupdate` compares release tags by their numbers and also reads two-part tags.

## Gotchas

- `gif2webp` has no `-lossless` (it is always lossless); passing it made every call fail.
- ffmpeg 8.1 cannot decode animated WebP; Pillow can, but a frame's duration is only known after
  `load()`. ffmpeg 9 can (frames identical to Pillow's) but reports no runtime, so a WebP source
  needs `verify_from_webp`, or `verify_visual` would pass without measuring.
- `-pix_fmt yuv420p` flattens a transparent GIF to white without an error.
- glycin 2.1 (GNOME's image viewer and thumbnails) shows animated AVIF and JPEG XL as stills;
  glycin 2.2 plays AVIF. That is why `clip_format webp` exists.
- ffmpeg finishes the file (flush, trailer) after SIGINT, about a second with x265 `slow`:
  `proc.stop_all` waits for every process group.
- A program started in the background of a non-interactive shell inherits SIGINT as ignored, and
  its children too, unless it installs a handler (`cli.install_signal_handlers`; tested).
- Options before the command (`--gpu-workers 2 video DIR`) are known from the parser: no option may
  be a flag in one command and take a value in another (tested).
- The pty ends lines with `\r\n`; a read that ends between the two must not be taken for a progress
  redraw.
- A `-c copy` slice cut with `-ss` starts at the previous keyframe; the MP4 edit list hides the
  pre-roll for the picture, but it counts for sizes (open item 1).

## Open items

1. **The probe over-predicts savings with long keyframe intervals.** The reference slice is cut
   with `-c copy` and starts at the previous keyframe; on a synthetic clip with x264's default GOP
   the probe said "71 % smaller", the result was 2.6 % smaller. Needs real footage before the
   method changes.
2. **12-bit JPEGs are untested** (no tool here writes one).
3. **The video and clip stages keep container tags (`-map_metadata 0`), but not an XMP box.**
   Matters only if picktag ever writes its tags into the files instead of its database.
4. **The gif stage's AVIF path flattens transparency** and deletes the GIF. A second stream with
   `alphaextract` (gray, libaom) would encode alpha, but nothing here reads it back to check it.
   `media-optimizer-webp` refuses transparent WebPs.
