# Changelog

All notable changes to media-optimizer are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.1] — 2026-11-14

### Added

- **Fedora Silverblue, Bluefin and other image-based systems:** media-optimizer runs in a toolbox
  or distrobox too: its app menu entry starts it in the container, and the page opens in the
  host's browser. The README's section for these systems covers both ways.

### Fixed

- The README's command for swapping in RPM Fusion's ffmpeg on Silverblue names every library it
  replaces; the GPU driver hint uses `rpm-ostree` on an image-based system.

## [1.0.0] — 2026-11-11

The first release. media-optimizer converts pictures, GIFs and videos to more efficient formats
(JPEG XL, WebP, animated WebP or AVIF, HEVC) and checks every result against its original before
the original goes. `media-optimizer DIR` asks for the settings on a page in the browser and shows
the run there; `-y`, the single stages and `verify` run in the terminal for scripts and timers.

### Features

- **Images.** JPEG becomes JPEG XL bit-exactly (`djxl` gives back the original file), PNG becomes
  JPEG XL losslessly or with a quality setting, with lossless WebP as the fallback; `--target webp`
  writes WebP instead. The real file type decides, not the extension.
- **GIFs and short clips.** GIFs become lossless animated WebP (or AVIF); short, silent videos
  become looping AVIF or WebP animations (`clips`).
- **Video.** Re-encodes to HEVC on the GPU (VAAPI, Vulkan) and the CPU (libx265) at the same time,
  after probe slices show it saves enough; already efficient codecs are skipped, and a GPU failure
  moves the file to the CPU.
- **Checked before anything is deleted.** Every result is written to a temporary file, checked
  (readable; for videos the runtime and a picture comparison) and only then moved into place,
  never over an existing file. Originals go to the trash, and a suspicious or salvaged video keeps
  its original.
- **In place or into a target folder.** Results next to the sources, or in a mirrored folder tree;
  with a target folder the source is never changed.
- **Resumable, and quick the next time.** Ctrl+C, SIGTERM or closing the page saves the run with
  its settings; the next start offers to resume. Each stage caches the files that need no work, as
  long as the file and the settings that matter are unchanged.
- **The page.** The folder picker, the subfolders as a tree with counts (files earlier runs handled
  are marked), the run's settings, *Defaults* saved to the config file, then the run from top to
  bottom with a section per stage, the reasons files were left alone, a summary and the log. A run
  can go on in the background and ends with a desktop notification.
- **Dry run.** `-n` shows what would happen without writing anything; the page then offers the same
  run for real.
- **`verify` and `gpu-selftest`.** Check finished video outputs against their originals and
  regenerate broken ones; find out which GPU options give a usable picture on this computer.
- **`media-optimizer-webp`.** Finds animated WebPs made from GIFs and turns them into AVIF, and
  lists WebPs labelled animated that aren't.
- **Works with keepbest and picktag.** keepbest hands its tab and folder choice over and shows the
  run's numbers next to its own (`--results`); a file journal tells picktag which file became
  which. `--events` gives other programs the run's progress.
- **Settings** in a JSON config file, as `MO_*` environment variables, as options or on the page,
  all checked by the same rules.
- **Updates and the app menu.** The page offers a newer release and installs it with pipx;
  `--install-desktop-entry` adds media-optimizer to the app menu.
- **Install** with `pipx install git+https://github.com/sobakah/media-optimizer.git`; needs Python
  3.10 or newer, ffmpeg, cjxl, cwebp and gif2webp (exiftool optional).

[1.0.1]: https://github.com/sobakah/media-optimizer/compare/1.0.0...1.0.1
[1.0.0]: https://github.com/sobakah/media-optimizer/releases/tag/1.0.0
