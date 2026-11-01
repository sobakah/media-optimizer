"""Which folders of the source a run covers (the same model as keepbest's).
The page's tree chooses them.

The choice ends up in two settings, so a resumed run keeps it and a config
can preset it:

    subfolders       "include" or "exclude"; "ask" asks on the page
    exclude_folders  folders whose own files are left out, relative to the
                     source in "/" form, "." for the files directly in it

Excluding a folder covers only its own files, so a folder can be left out
while its subfolders are still processed.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from . import files, ui

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


def resolve(settings) -> None:
    """Settle subfolders="ask" before a run: the page asks with its tree, any
    other run takes the subfolders along."""
    if settings.subfolders == "ask":
        settings.subfolders = "include"
