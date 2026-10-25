"""Which folders of the source a run covers: the subfolder question and the
folder picker (the same model as keepbest's).

The choice ends up in two settings, so a resumed run keeps it and a config
can preset it:

    subfolders       "include" or "exclude"; "ask" asks here first
    exclude_folders  folders whose own files are left out, relative to the
                     source in "/" form, "." for the files directly in it

Excluding a folder covers only its own files. In the picker, switching a
folder asks whether the folders below it switch along (Enter: yes), so a
folder can be left out while its subfolders are still processed.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from . import files, ui
from .ui import StyleUI as S

ROOT = "."
IMAGE_EXTS = ("jpg", "jpeg", "png", "gif")


# --- counting --------------------------------------------------------------------
def media_counts(source: Path, settings, found: list | None = None) -> dict[str, tuple[int, int]]:
    """(images, videos) directly in each folder below source, over the whole
    tree regardless of the current choice, so the picker can show what an
    excluded folder holds. *found* gets (folder, path, is_video) of each file."""
    images = files.extension_matcher(IMAGE_EXTS)
    videos = files.extension_matcher(settings.video_extensions, exclude_parts=True)
    counts: dict[str, tuple[int, int]] = {}
    ui.progress("  Looking for subfolders...")
    try:
        for path in files.find_files(source, lambda name: images(name) or videos(name),
                                     include_hidden=settings.include_hidden, report=False):
            rel = files.relative_folder(path.parent, source)
            i, v = counts.get(rel, (0, 0))
            is_video = videos(path.name)
            counts[rel] = (i, v + 1) if is_video else (i + 1, v)
            if found is not None:
                found.append((rel, path, is_video))
    finally:
        ui.progress_done()
    return counts


class CacheView:
    """Which of the picker's files a run would skip because a stage's cache
    already has their outcome (done, discarded as larger, already HEVC, ...):
    the image, GIF and video stages' caches, read once and never written.
    An entry counts only while the file is unchanged and the settings it
    depends on are the same, so counts() takes the settings to check with.
    Not the clip stage's cache: what it remembers (not selected, discarded)
    still goes to the video stage."""

    def __init__(self, source: Path, work_dir: Path, found: list, include_hidden: bool) -> None:
        from . import cache
        from .stages import gifs, images, video

        self.found = found
        self._stages = []
        for name, keys, optional in ((".img_cache.txt", images.ImageStage.cache_settings, ()),
                                     (".gif_cache.txt", gifs.GifStage.cache_settings, ()),
                                     (cache.VIDEO_CACHE, video.CACHE_SETTINGS, video.CACHE_SETTINGS_OPTIONAL)):
            path = work_dir / name
            if name == cache.VIDEO_CACHE and not path.exists() and (source / name).is_file():
                path = source / name          # the video stage copies it over at its start
            stage = cache.StageCache(path, enabled=True, dry_run=True, fingerprint="", read_only=True)
            stage.load()
            stage.merge_subfolders(work_dir, source, include_hidden)
            self._stages.append((name, keys, optional, stage))

    def _stage_for(self, path: Path, is_video: bool):
        name = ".gif_cache.txt" if path.suffix.lower() == ".gif" else ".img_cache.txt"
        if is_video:
            from .cache import VIDEO_CACHE
            name = VIDEO_CACHE
        return next(stage for stage in self._stages if stage[0] == name)

    def counts(self, settings) -> dict[str, tuple[int, int]]:
        """(images, videos) per folder that the stages would skip with *settings*."""
        from . import cache

        prints = {name: cache.fingerprint(settings, keys, optional) for name, keys, optional, _ in self._stages}
        skipped: dict[str, tuple[int, int]] = {}
        for rel, path, is_video in self.found:
            name, _, _, stage = self._stage_for(path, is_video)
            if stage.lookup(path, prints[name]) is None:
                continue
            i, v = skipped.get(rel, (0, 0))
            skipped[rel] = (i, v + 1) if is_video else (i + 1, v)
        return skipped


def subfolder_entries(counts: dict[str, tuple[int, int]]) -> list[str]:
    """Subfolders with images or videos, plus the folders between them and
    the source so the tree has no gaps, in tree order."""
    rels: set[str] = set()
    for rel in counts:
        parts = PurePosixPath(rel).parts
        for depth in range(1, len(parts) + 1):
            rels.add("/".join(parts[:depth]))
    rels.discard(ROOT)
    return sorted(rels, key=lambda r: PurePosixPath(r).parts)


def media_text(images: int, videos: int) -> str:
    out = []
    if images:
        out.append(f"{images} image{'s' if images != 1 else ''}")
    if videos:
        out.append(f"{videos} video{'s' if videos != 1 else ''}")
    return " · ".join(out)


def _in_scope(counts, excluded: set[str], with_subfolders: bool = True) -> tuple[int, int]:
    images = videos = 0
    for rel, (i, v) in counts.items():
        if rel in excluded or (not with_subfolders and rel != ROOT):
            continue
        images += i
        videos += v
    return images, videos


# --- switching (as in keepbest) -------------------------------------------------------
def parse_numbers(text: str, upper: int) -> list[int] | None:
    """'3', '2-5', '1,4 7' -> numbers in 1..upper, or None when invalid."""
    numbers = []
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            return None
        low, high = sorted((int(match[1]), int(match[2] or match[1])))
        if low < 1 or high > upper:
            return None
        numbers.extend(range(low, high + 1))
    return numbers or None


def _below(rel: str, entries: list[str]) -> list[str]:
    return [r for r in entries if r == rel or r.startswith(rel + "/")]


def switch_folders(excluded: set[str], entries: list[str], numbers: list[int],
                   with_subfolders: bool = True) -> None:
    """Switch the listed folders (1-based numbers into entries). Each is
    judged by its state before this input ("4,1" selects 4 and unselects 1).
    With with_subfolders a folder takes the folders below it along; parents
    go first, so a listed subfolder's own switch wins."""
    listed = {entries[n - 1] for n in numbers}
    switch_on = {rel: rel in excluded for rel in listed}
    for rel in sorted(listed, key=lambda r: r.count("/")):
        for below in (_below(rel, entries) if with_subfolders else [rel]):
            if switch_on[rel]:
                excluded.discard(below)
            else:
                excluded.add(below)


def subfolders_along(excluded: set[str], entries: list[str], numbers: list[int]) -> tuple[list[str], int]:
    """The listed folders whose switch would change folders below them, and
    how many would change. ([], 0) when nothing below would change."""
    alone, along = set(excluded), set(excluded)
    switch_folders(alone, entries, numbers, with_subfolders=False)
    switch_folders(along, entries, numbers)
    changed = alone ^ along
    listed = [entries[n - 1] for n in sorted(set(numbers))]
    parents = [rel for rel in listed if any(c.startswith(rel + "/") for c in changed)]
    return parents, len(changed)


# --- the picker ---------------------------------------------------------------------
def choose_folders(source: Path, counts: dict[str, tuple[int, int]], excluded: set[str]) -> None:
    """Tree of subfolders with on/off boxes; edits excluded in place. The
    main folder is switched only with its own key (m): numbers, a and n
    change subfolders only, so it is not left out by accident."""
    entries = subfolder_entries(counts)
    names = ["  " * (rel.count("/")) + PurePosixPath(rel).name + "/" for rel in entries]
    while True:
        ui.out()
        ui.banner(f"CHOOSE FOLDERS ({len(entries)} subfolders)")
        main_on = ROOT not in excluded
        own = media_text(*counts.get(ROOT, (0, 0))) or "no files"
        if main_on:
            ui.out(f"  [{S.GREEN}  m{S.RESET}] {S.GREEN}[✓]{S.RESET} {S.BOLD}Main folder{S.RESET}"
                   f"  {S.DIM}{own} directly in it{S.RESET}\n")
        else:
            ui.out(f"  [{S.GREEN}  m{S.RESET}] {S.DIM}[ ]{S.RESET} {S.YELLOW}Main folder - excluded{S.RESET}"
                   f"  {S.DIM}{own} directly in it{S.RESET}\n")
        width = min(max(ui.visible_width(n) for n in names), 50)
        for number, (rel, name) in enumerate(zip(entries, names), 1):
            on = rel not in excluded
            box = f"{S.GREEN}[✓]{S.RESET}" if on else f"{S.DIM}[ ]{S.RESET}"
            shown = ui.pad_width(ui.fit_width(name, width), width)
            shown = shown if on else f"{S.DIM}{shown}{S.RESET}"
            amount = media_text(*counts.get(rel, (0, 0))) or "—"
            ui.out(f"  [{S.GREEN}{number:3d}{S.RESET}] {box} {shown}  {S.DIM}{amount}{S.RESET}")

        chosen = sum(1 for rel in entries if rel not in excluded)
        main_text = "main folder" if main_on else f"{S.YELLOW}main folder excluded{S.RESET}"
        total = media_text(*_in_scope(counts, excluded)) or "no files"
        ui.out(f"\n{S.BOLD}Selected:{S.RESET} {main_text} + {chosen} of {len(entries)} subfolders · {total}")
        span = f"1-{len(entries)}"
        key = max(len("Enter"), len(span))
        ui.out(f"\n  {S.BOLD}{'Enter':<{key}}{S.RESET}  Done")
        for k, text in ((span, "Switch a folder on/off (asks about its subfolders)"),
                        ("a", "Select all subfolders"), ("n", "Select no subfolders"),
                        ("m", "Switch the main folder on/off")):
            ui.out(f"  {S.GREEN}{k:<{key}}{S.RESET}  {text}")
        ui.out(f"  {S.DIM}Several at once: 2-5 or 1,3,7 - each listed folder is switched.{S.RESET}")
        choice = ui.ask(f"{S.BOLD}Folders: {S.RESET}").strip().lower()
        if choice == "":
            if chosen == 0 and not main_on:
                ui.say("NOTE", S.YELLOW, "Nothing selected - no files will be processed.")
            elif chosen == 0:
                ui.say("NOTE", S.YELLOW, "No subfolder selected - only the main folder will be processed.")
            return
        if choice == "m":
            if main_on:
                excluded.add(ROOT)
            else:
                excluded.discard(ROOT)
        elif choice == "a":
            excluded.difference_update(entries)
        elif choice == "n":
            excluded.update(entries)
        else:
            numbers = parse_numbers(choice, len(entries))
            if numbers is None:
                ui.say("NOTE", S.YELLOW, "Unknown option.")
                continue
            parents, count = subfolders_along(excluded, entries, numbers)
            along = True
            if parents:
                listed = ", ".join(f"{rel}/" for rel in parents)
                along = ui.prompt_bool(f"Also switch the {count} subfolder{'s' if count != 1 else ''} "
                                       f"below {listed}?", True)
            switch_folders(excluded, entries, numbers, with_subfolders=along)


# --- the question -----------------------------------------------------------------
def resolve(source: Path, settings, *, assume_yes: bool) -> None:
    """Settle subfolders="ask" before a run. Asked only on a terminal without
    -y and only when there are subfolders with images or videos; otherwise
    the default applies: include."""
    if settings.subfolders != "ask":
        return
    if assume_yes or not ui.interactive():
        settings.subfolders = "include"
        return
    counts = media_counts(source, settings)
    entries = subfolder_entries(counts)
    if not entries:
        settings.subfolders = "include"
        return
    excluded = {files.normalize_folder(f) for f in settings.exclude_folders}
    below = media_text(*_in_scope(
        {rel: c for rel, c in counts.items() if rel != ROOT}, excluded)) or "no files"
    own = media_text(*counts.get(ROOT, (0, 0))) or "no files"
    ui.out()
    ui.say("SUBFOLDERS", S.CYAN, f"{len(entries)} subfolder(s) with images or videos: {below} in them, "
                                 f"{own} directly in the source.")
    if excluded:
        ui.out(f"          exclude_folders leaves out {len(excluded)} folder(s); "
               "choose custom to change that.")
    while True:
        answer = ui.ask("  Process subfolders? [I]nclude / [e]xclude / [c]ustom: ").strip().lower()
        if answer in ("", "i", "include", "y", "yes", "j", "ja"):
            settings.subfolders = "include"
            return
        if answer in ("e", "exclude", "n", "no", "nein"):
            settings.subfolders = "exclude"
            ui.out(f"  -> Only the files directly in the source ({own}).")
            return
        if answer in ("c", "custom"):
            choose_folders(source, counts, excluded)
            settings.subfolders = "include"
            settings.exclude_folders = sorted(excluded, key=lambda r: PurePosixPath(r).parts)
            return
        ui.out("  Please answer i, e or c.")
