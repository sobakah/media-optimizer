"""The video stage, its cache, and the verify command."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import leftovers, running, snapshot, wait_running
from media_optimizer import gpu, media
from media_optimizer.config import Settings

FAST = ["--encoder", "cpu", "--min-size", "0", "--no-probe", "--preset", "ultrafast"]


def _cached_paths(cache_file) -> set[str]:
    """Paths in a v2 cache file (size, mtime, fingerprint, outcome, path)."""
    lines = cache_file.read_text().splitlines()
    assert lines[0] == "# media-optimizer cache v2"
    return {line.split("\t", 4)[4] for line in lines[1:] if line}


def _probe_json(path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                         stdout=subprocess.PIPE, text=True).stdout
    return json.loads(out)


# --- GPU arguments (pure) ------------------------------------------------------------
def test_gpu_args_follow_settings():
    s = Settings()
    a = gpu.gpu_args(s, "/dev/dri/renderD129", "yuv420p10le")
    assert a.hw == ("-vaapi_device", "/dev/dri/renderD129")
    assert "format=nv12,hwupload" in a.enc, "10 bit only with gpu_allow_10bit"
    s.gpu_allow_10bit = True
    a = gpu.gpu_args(s, "/dev/dri/renderD129", "yuv420p10le")
    assert "format=p010,hwupload" in a.enc and "main10" in a.enc
    s.gpu_rc_mode, s.gpu_bf, s.hevc_tag = "VBR", 0, "hvc1"
    a = gpu.gpu_args(s, "x")
    assert a.enc[a.enc.index("-rc_mode") + 1] == "VBR" and "-bf" in a.enc and a.tag == ("-tag:v", "hvc1")
    v = gpu.gpu_args(Settings(gpu_codec="hevc_vulkan"), "x")
    assert "hevc_vulkan" in v.enc and v.hw[0] == "-init_hw_device"


# --- conversion ---------------------------------------------------------------------
def test_target_mode(folder, tmp_path, run_cli):
    d = folder("h1", "l.mp4=long.mp4")
    before = snapshot(d)
    o = tmp_path / "h1out"
    out = run_cli("video", d, o, *FAST)
    assert out.rc == 0 and (o / "l.mp4").exists(), out
    assert _probe_json(o / "l.mp4")["streams"][0]["codec_name"] == "hevc"
    assert snapshot(d) == before, "no cache or stats in the source"
    assert (o / ".video_conversion_cache.txt").exists()
    out = run_cli("verify", d, o, "--no-visual")
    assert out.rc == 0 and "checking 1 file" in out, out


def test_in_place_rename_and_delete(folder, run_cli):
    d = folder("h1b", "l.mp4=long.mp4")
    out = run_cli("video", d, *FAST, "--delete", "--force-delete", "--rename-inplace")
    assert out.rc == 0, out
    assert sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) == ["l.mp4"]
    assert _probe_json(d / "l.mp4")["streams"][0]["codec_name"] == "hevc"
    assert "[RENAME]" in out


def test_in_place_without_delete_keeps_suffix(folder, run_cli):
    d = folder("h1c", "l.mp4=long.mp4")
    out = run_cli("video", d, *FAST, "--no-delete")
    assert (d / "l.mp4").exists() and (d / "l_h265.mp4").exists(), out
    assert "skipped: 'l.mp4' still exists" in out


@pytest.mark.parametrize("args", [["--crf", "abc"], ["--encoder", "GPU"], ["--container", "avi"]])
def test_bad_options(folder, run_cli, args):
    d = folder("h1d", "l.mp4=long.mp4")
    assert run_cli("video", d, *args).rc == 2


def test_failed_encode_is_partial(folder, tmp_path, run_cli, stub):
    d = folder("h2", "l.mp4=long.mp4", "c.mp4=long.mp4")
    path = stub("ffmpeg", 'case "$*" in *"/l.mp4"*libx265*) echo "simulated encoder failure" >&2; exit 1 ;; esac\n'
                          'PATH="${PATH#*:}" exec ffmpeg "$@"\n')
    out = run_cli("video", d, tmp_path / "h2out", *FAST, path=path)
    assert out.rc == 3, out
    assert (tmp_path / "h2out" / "c.mp4").exists()
    assert not (tmp_path / "h2out" / "l.mp4").exists()


def test_container_fallback(folder, tmp_path, run_cli):
    """MP4 cannot hold SRT: one retry with AAC audio and without subtitles."""
    d = folder("h2b")
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nhello\n")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                    "-f", "lavfi", "-i", "sine=f=440", "-i", str(srt), "-t", "3", "-map", "0", "-map", "1",
                    "-map", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "libopus", "-c:s", "srt",
                    str(d / "v.mkv")], check=True)
    out = run_cli("video", d, tmp_path / "o", *FAST, "--container", "mp4", "--keep-larger")
    assert "[CONTAINER]" in out and out.rc == 0, out
    kinds = [s["codec_type"] for s in _probe_json(tmp_path / "o" / "v.mp4")["streams"]]
    assert kinds == ["video", "audio"]


def test_portrait_video_passes_the_picture_check(folder, run_cli):
    """ffmpeg turns a portrait video upright and drops the rotation flag; the
    check compared it with the sideways source (8 dB) and kept every
    portrait phone video as suspicious."""
    d = folder("h2r", "p.mp4=portrait.mp4")
    out = run_cli("video", d, *FAST, "--delete", "--force-delete", "--keep-larger")
    assert "[PICTURE?]" not in out and "Picture verified:     1" in out, out
    stream = _probe_json(d / "p.mp4")["streams"][0]
    assert stream["codec_name"] == "hevc" and (stream["width"], stream["height"]) == (240, 320)


# A GPU that fails on every file; x265 encodes are counted while they run.
_FAILING_GPU = """\
case "$*" in *-vaapi_device*) echo "simulated GPU failure" >&2; exit 1 ;; esac
case "$*" in *libx265*)
    mkdir -p "$TRACK"; touch "$TRACK/$$"; ls "$TRACK" | wc -l >> "$TRACK.counts"
    PATH="${PATH#*:}" ffmpeg "$@"; rc=$?; rm -f "$TRACK/$$"; exit $rc ;;
esac
PATH="${PATH#*:}" exec ffmpeg "$@"
"""
GPU_FAILS = ["--encoder", "gpu", "--force-gpu", "--gpu-workers", "2", "--min-size", "0", "--no-probe",
             "--preset", "ultrafast", "--keep-larger"]


def test_gpu_failure_moves_files_to_the_one_cpu_lane(folder, tmp_path, run_cli, stub):
    """Each GPU worker used to retry its file on the CPU itself, next to the
    CPU lane: three x265 encodes at once with two GPU workers."""
    d = folder("h2i", "a.mp4=long.mp4", "b.mp4=long.mp4", "c.mp4=long.mp4")
    o = tmp_path / "h2iout"
    track = tmp_path / "x265"
    path = stub("ffmpeg", _FAILING_GPU)
    out = run_cli("video", d, o, *GPU_FAILS, path=path, env={"TRACK": track})
    assert out.rc == 0 and "GPU: 0 | CPU: 3" in out, out
    assert all((o / n).exists() for n in ("a.mp4", "b.mp4", "c.mp4"))
    counts = [int(n) for n in (tmp_path / "x265.counts").read_text().split()]
    assert len(counts) == 3 and max(counts) == 1, counts


def test_gpu_failure_still_gets_the_container_retry(folder, tmp_path, run_cli, stub):
    """The CPU retry after a GPU failure copied the streams again, without
    the AAC/no-subtitle retry, and failed on an SRT for MP4."""
    d = folder("h2j")
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nhello\n")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                    "-i", str(srt), "-t", "3", "-map", "0", "-map", "1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:s", "srt", str(d / "v.mkv")], check=True)
    path = stub("ffmpeg", _FAILING_GPU)
    out = run_cli("video", d, tmp_path / "o", *GPU_FAILS, "--container", "mp4", path=path,
                  env={"TRACK": tmp_path / "x265"})
    assert out.rc == 0 and "[CONTAINER]" in out, out
    assert (tmp_path / "o" / "v.mp4").exists()


def test_selftest_uses_the_device_the_run_would_use(tmp_path, monkeypatch):
    nodes = [str(tmp_path / "renderD128"), str(tmp_path / "renderD129")]
    for node in nodes:
        Path(node).touch()
    monkeypatch.setattr(gpu, "render_nodes", lambda: nodes)
    monkeypatch.setattr(gpu, "probe_device", lambda s, dev: None if dev == nodes[1] else "no encoder")
    assert gpu.selftest_device(Settings(gpu_codec="hevc_vulkan")) == nodes[1]
    monkeypatch.setattr(gpu, "probe_device", lambda s, dev: "no encoder")
    assert gpu.selftest_device(Settings()) == nodes[0], "none works: the first one is tested"
    assert gpu.selftest_device(Settings(vaapi_device="/dev/x")) == "/dev/x"


# ffmpeg 9.0.2 with Fedora's Mesa (no H.265) on an RX 9070 XT, verbatim.
_NO_HEVC_PROFILE = """\
[hevc_vaapi @ 0x55b0eb1f9bc0] No usable encoding profile found.
[vost#0:0/hevc_vaapi @ 0x55b0eb1f9680] [enc:hevc_vaapi @ 0x55b0eb1f9b40] Error while opening encoder - maybe incorrect parameters such as bit_rate, rate, width or height.
[vf#0:0 @ 0x55b0eb214f40] Error sending frames to consumers: Function not implemented
[vf#0:0 @ 0x55b0eb214f40] Task finished with error code: -38 (Function not implemented)
[vf#0:0 @ 0x55b0eb214f40] Terminating thread with return code -38 (Function not implemented)
[vost#0:0/hevc_vaapi @ 0x55b0eb1f9680] [enc:hevc_vaapi @ 0x55b0eb1f9b40] Could not open encoder before EOF
[vost#0:0/hevc_vaapi @ 0x55b0eb1f9680] Task finished with error code: -22 (Invalid argument)
[vost#0:0/hevc_vaapi @ 0x55b0eb1f9680] Terminating thread with return code -22 (Invalid argument)
[out#0/null @ 0x55b0eb1f9300] Nothing was written into output file, because at least one of its streams received no packets.
"""


def test_gpu_check_names_the_cause_and_the_driver_package(folder, main, stub, tmp_path, monkeypatch):
    """The startup check showed only ffmpeg's last line ("Nothing was written
    into output file"), not that the driver has no HEVC encoder."""
    d = folder("h2k", "v.mp4=long.mp4")
    node = tmp_path / "renderD128"
    node.touch()
    monkeypatch.setattr(gpu, "render_nodes", lambda: [str(node)])
    monkeypatch.setattr(gpu, "_distro", lambda: "fedora")
    (tmp_path / "stderr.txt").write_text(_NO_HEVC_PROFILE)
    stub("ffmpeg", f"""\
        case "$*" in *-vaapi_device*) cat '{tmp_path / "stderr.txt"}' >&2; exit 218 ;; esac
        PATH="${{PATH#*:}}" exec ffmpeg "$@"
        """)
    out = main("video", d, tmp_path / "o", "--encoder", "gpu", "--min-size", "0", "--no-probe")
    assert out.rc == 1, out
    assert f"reason: {node}: [hevc_vaapi] No usable encoding profile found." in out, out
    assert "Nothing was written" not in out
    assert "sudo dnf install mesa-va-drivers-freeworld" in out
    assert "mesa-filesystem" in out and "updates-testing" in out


def test_gpu_error_reason():
    assert gpu.error_reason("") == "ffmpeg failed"
    assert gpu.error_reason("WARNING: radv is not a conformant Vulkan implementation, testing use only.\n"
                            "[hevc_vulkan @ 0x5640dde3e180] Device does not support encoding hevc!\n") \
        == "[hevc_vulkan] Device does not support encoding hevc!"
    # Only consequences: the last line is still better than nothing.
    assert gpu.error_reason("[out#0/null @ 0x1] Nothing was written into output file\n") \
        == "[out#0/null] Nothing was written into output file"
    assert gpu.driver_hint("device does not exist") == []


def test_suspicious_picture_keeps_original(folder, main, monkeypatch):
    """Bash deleted the original after a suspicious picture comparison when
    --delete was on; here it is always kept."""
    d = folder("h2c", "l.mp4=long.mp4")
    monkeypatch.setattr(media, "verify_visual", lambda *a, **k: media.Visual(False, 10.0, "x"))
    out = main("video", d, *FAST, "--delete", "--force-delete")
    assert "[PICTURE?]" in out, out
    assert (d / "l.mp4").exists() and (d / "l_h265.mp4").exists()
    assert "l.mp4" in (d / ".video_suspect.txt").read_text()


def test_strict_visual_discards(folder, main, monkeypatch, tmp_path):
    d = folder("h2d", "l.mp4=long.mp4")
    monkeypatch.setattr(media, "verify_visual", lambda *a, **k: media.Visual(False, 10.0, "x"))
    ev = tmp_path / "h2d.jsonl"
    out = main("video", d, *FAST, "--delete", "--force-delete", "--strict-visual", "--events", ev)
    assert out.rc == 3, out
    assert (d / "l.mp4").exists() and not (d / "l_h265.mp4").exists()
    assert not leftovers(d)
    # A failed picture check, not "discarded (larger)" on the page.
    done = [json.loads(line) for line in ev.read_text().splitlines() if '"done"' in line]
    assert [e["outcome"] for e in done] == ["failed"]


def test_runtime_mismatch_keeps_original(folder, main, monkeypatch):
    d = folder("h2e", "l.mp4=long.mp4")
    monkeypatch.setattr(media, "verify_video", lambda *a, **k: media.DurationCheck(False, 8.0, 3.0))
    out = main("video", d, *FAST, "--delete", "--force-delete")
    assert "[INVALID]" in out and out.rc == 3, out
    assert (d / "l.mp4").exists() and not (d / "l_h265.mp4").exists()


def test_part_in_a_name_is_not_a_leftover(folder, run_cli):
    """A video run deleted 'Movie.part.1.mp4' as a leftover (and never
    converted it, the scan skipped it too)."""
    d = folder("h2g", "Movie.part.1.mp4=long.mp4", "x.mp4.part.4711.12345.mp4=long.mp4")
    out = run_cli("video", d, *FAST, "--no-delete")
    assert out.rc == 0 and (d / "Movie.part.1.mp4").exists(), out
    assert (d / "Movie.part.1_h265.mp4").exists()
    assert not (d / "x.mp4.part.4711.12345.mp4").exists(), "a real leftover still goes"


def test_unrelated_image_of_the_same_name_does_not_skip_a_video(folder, tmp_path, run_cli):
    """IMG.avif (a photo, or IMG.webp from IMG.jpg with img_target webp) made
    the video stage skip IMG.mp4 and cache it as done, although the clip
    stage would never take it (it has sound)."""
    d = folder("h2h", "IMG.mp4=withaudio.mp4")
    o = tmp_path / "h2hout"
    o.mkdir()
    (o / "IMG.avif").write_bytes(b"x" * 100)
    out = run_cli("video", d, o, *FAST, "--keep-larger")
    assert (o / "IMG.mp4").exists(), out


def test_existing_avif_counts_as_done(folder, run_cli):
    d = folder("h2f", "l.mp4=long.mp4")
    (d / "l.avif").write_bytes(b"x" * 100)
    out = run_cli("video", d, *FAST, "--no-delete")
    assert not (d / "l_h265.mp4").exists(), out


@pytest.mark.slow
def test_sigterm_during_encode(folder, tmp_path, start_cli):
    d = folder("h3", "s.mp4=slow.mp4")
    o = tmp_path / "h3out"
    p = start_cli("video", d, o, "--encoder", "cpu", "--min-size", "0", "--no-probe", "--preset", "slow",
                  out=tmp_path / "term.out")
    assert wait_running("ffmpeg", str(d))
    time.sleep(1)
    t0 = time.monotonic()
    p.terminate()
    rc = p.wait(timeout=30)
    took = time.monotonic() - t0
    assert rc == 130, (tmp_path / "term.out").read_text()
    assert took < 5, f"took {took:.1f}s"
    time.sleep(0.3)
    assert not running("ffmpeg", str(d))
    assert not leftovers(o)
    assert (d / "s.mp4").exists()
    assert (o / ".h265_stats.json").exists()


# --- cache ---------------------------------------------------------------------------------
def test_cache_subfolders_in_place(folder, run_cli):
    d = folder("hc", "sub/l.mp4=long.mp4")
    run_cli("video", d / "sub", *FAST, "--no-delete")
    sub_before = (d / "sub" / ".video_conversion_cache.txt").read_text()
    out = run_cli("video", d, *FAST, "--no-delete")
    assert "entries taken over from 1 .video_conversion_cache.txt file" in out, out
    assert _cached_paths(d / ".video_conversion_cache.txt") >= {f"{d}/sub/l.mp4"}
    assert "Cache (this scan):    2 file(s)" in out and "Re-encoded:           0 file(s)" in out
    assert (d / "sub" / ".video_conversion_cache.txt").read_text() == sub_before
    out = run_cli("video", d, *FAST, "--no-delete")
    assert "taken over" not in out, "nothing taken over twice"


def test_cache_dry_run_and_no_cache(folder, run_cli):
    d = folder("hc2", "sub/l.mp4=long.mp4")
    run_cli("video", d / "sub", *FAST, "--no-delete")
    out = run_cli("video", d, *FAST, "--no-delete", "-n")
    assert not (d / ".video_conversion_cache.txt").exists(), out
    out = run_cli("video", d, *FAST, "--no-delete", "--no-cache")
    assert "taken over" not in out and "Ignored for this run" in out, out


def test_cache_target_mode(folder, tmp_path, run_cli):
    d = folder("hc3", "sub/l.mp4=long.mp4")
    o = tmp_path / "hc3out"
    run_cli("video", d / "sub", o / "sub", *FAST)
    out = run_cli("video", d, o, *FAST)
    assert "entries taken over from 1 .video_conversion_cache.txt file" in out, out
    assert not (d / ".video_conversion_cache.txt").exists()


def test_cache_of_other_target_is_ignored(folder, tmp_path, run_cli):
    """A cache in <target>/other from a run on <source>/sub: that output
    went elsewhere, so it must not make the parent run skip the video."""
    d = folder("hc4", "sub/l.mp4=long.mp4")
    o = tmp_path / "hc4out"
    run_cli("video", d / "sub", o / "other", *FAST)
    out = run_cli("video", d, o, *FAST)
    assert "taken over" not in out and (o / "sub" / "l.mp4").exists(), out


def test_verify_fix_prunes_subfolder_caches(folder, tmp_path, media, run_cli):
    d = folder("hc5", "sub/l.mp4=long.mp4")
    o = tmp_path / "hc5out"
    run_cli("video", d / "sub", o / "sub", *FAST)
    (o / "sub" / "l.mp4").write_bytes((media / "long.mp4").read_bytes()[:20000])
    out = run_cli("verify", d, o, "--no-visual", "--fix")
    assert f"{d}/sub/l.mp4" not in (o / "sub" / ".video_conversion_cache.txt").read_text(), out
    out = run_cli("video", d, o, *FAST)
    assert "Re-encoded:           1 file(s)" in out, out
    assert _probe_json(o / "sub" / "l.mp4")["streams"]


def test_verify_finds_broken_and_repairs(folder, tmp_path, media, run_cli):
    d = folder("hv", "a.mp4=long.mp4", "b.mp4=long.mp4")
    o = tmp_path / "hvout"
    run_cli("video", d, o, *FAST)
    (o / "a.mp4").write_bytes((media / "long.mp4").read_bytes()[:20000])
    out = run_cli("verify", d, o, "--no-visual")
    assert out.rc == 1 and "1 broken file(s)" in out, out
    assert (o / "a.mp4").exists(), "nothing changed without --fix"
    assert (o / ".broken_videos.txt").read_text().strip() == f"{d}/a.mp4"
    out = run_cli("verify", d, o, "--no-visual", "--fix", "--run", env={"MO_MIN_SIZE_MB": "0",
                                                                         "MO_ENCODER_MODE": "cpu",
                                                                         "MO_ENABLE_PROBE": "false",
                                                                         "MO_CPU_PRESET": "ultrafast"})
    assert out.rc == 0, out
    assert _probe_json(o / "a.mp4")["streams"][0]["codec_name"] == "hevc"
    assert not (o / ".broken_videos.txt").exists() and list(o.glob(".broken_videos.done-*.txt"))


def test_verify_matches_other_container(folder, tmp_path, run_cli):
    """auto turns an .avi into an .mkv; matched by base name."""
    d = folder("hv2")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                    "-t", "3", "-c:v", "mpeg4", str(d / "v.avi")], check=True)
    o = tmp_path / "hv2out"
    out = run_cli("video", d, o, *FAST, "--keep-larger")
    assert (o / "v.mkv").exists(), out
    out = run_cli("verify", d, o, "--no-visual")
    assert out.rc == 0 and "No original:          0" in out, out


def test_verify_never_takes_a_photo_as_the_original(folder, tmp_path, media, run_cli):
    """IMG.jpg next to the (gone) IMG.mp4 was taken as the original: 0.04 s
    against 8 s failed as DURATION, and --fix deleted the only copy."""
    d = folder("hv3", "IMG.jpg=photo.jpg", "IMG.srt=photo.jpg")
    o = tmp_path / "hv3out"
    o.mkdir()
    shutil.copy2(media / "long.mp4", o / "IMG.mp4")
    out = run_cli("verify", d, o, "--fix")
    assert out.rc == 0 and "No original:          1" in out, out
    assert (o / "IMG.mp4").exists()


def test_from_list_ignores_cache(folder, tmp_path, run_cli):
    d = folder("hl", "a.mp4=long.mp4", "b.mp4=long.mp4")
    o = tmp_path / "hlout"
    run_cli("video", d, o, *FAST)
    lst = tmp_path / "list.txt"
    lst.write_text(f"{d}/a.mp4\n{d}/missing.mp4\n")
    (o / "a.mp4").unlink()
    out = run_cli("video", d, o, *FAST, "--from-list", lst)
    assert "skipped, file missing" in out and (o / "a.mp4").exists(), out
    assert "Re-encoded:           1 file(s)" in out


def test_own_output_is_not_encoded_again_after_a_setting_change(folder, run_cli):
    """With hevc in source_codecs, in place with rename: any changed setting
    made the output (now under the source name) count as new, and it lost
    quality in a second encode."""
    d = folder("hl2", "l.mp4=long.mp4")
    args = ["video", d, *FAST, "--source-codecs", "h264 hevc", "--delete", "--force-delete", "--keep-larger"]
    out = run_cli(*args)
    assert "Re-encoded:           1" in out and not (d / "l_h265.mp4").exists(), out
    before = snapshot(d / "l.mp4")
    out = run_cli(*args, "--crf", "30")
    assert "Re-encoded:           0" in out and "Cache (this scan):    1" in out, out
    assert snapshot(d / "l.mp4") == before


def test_gpu_settings_count_for_the_cache_only_when_changed():
    from media_optimizer import cache
    from media_optimizer.stages import video
    s = Settings()
    plain = cache.fingerprint(s, video.CACHE_SETTINGS)
    assert cache.fingerprint(s, video.CACHE_SETTINGS, video.CACHE_SETTINGS_OPTIONAL) == plain, \
        "existing caches stay valid"
    s.gpu_bitrate = "4M"
    assert cache.fingerprint(s, video.CACHE_SETTINGS, video.CACHE_SETTINGS_OPTIONAL) != plain


def test_from_list_leaves_the_settings_alone(folder, tmp_path):
    from media_optimizer.stages.base import Context
    from media_optimizer.stages.video import VideoStage
    d = folder("hl3", "a.mp4=long.mp4")
    lst = tmp_path / "list.txt"
    lst.write_text(f"{d}/a.mp4\n")
    s = Settings(min_size_mb=0, encoder_mode="cpu", enable_probe=False, cpu_preset="ultrafast",
                 delete_original=False, rename_inplace=False)
    (tmp_path / "hl3out").mkdir()
    VideoStage(Context(s, d, tmp_path / "hl3out"), from_list=str(lst)).run()
    assert (tmp_path / "hl3out" / "a.mp4").exists()
    assert s.use_cache is True, "the next stage or run must not inherit 'no cache'"


def test_from_list_written_for_a_symlinked_source(folder, tmp_path, run_cli):
    """A list written for ~/Videos/Link and a run on the real folder: the
    target folder could not be computed for any file."""
    d = folder("hl4", "a.mp4=long.mp4")
    link = tmp_path / "link"
    link.symlink_to(d)
    lst = tmp_path / "list.txt"
    lst.write_text(f"{link}/a.mp4\n{link}/missing.mp4\n")
    out = run_cli("video", d, tmp_path / "hl4out", *FAST, "--from-list", lst)
    assert out.rc == 0 and (tmp_path / "hl4out" / "a.mp4").exists(), out
    assert "1 candidate(s) found" in out, "the missing file is not counted"


def test_verify_fix_waits_for_a_run_on_the_target(folder, tmp_path, media, run_cli):
    from media_optimizer import safety
    d = folder("hv4", "a.mp4=long.mp4")
    o = tmp_path / "hv4out"
    o.mkdir()
    shutil.copy2(media / "clip.mp4", o / "a.mp4")            # 3 s for 8 s: broken
    with safety.RunLock(o):
        out = run_cli("verify", d, o, "--no-visual", "--fix")
    assert out.rc == 1 and "BUSY" in out and (o / "a.mp4").exists(), out
    out = run_cli("verify", d, o, "--no-visual", "--fix")
    assert "DELETED" in out and not (o / "a.mp4").exists(), out


def test_verify_stills_of_equal_names_are_kept_apart(folder, tmp_path, media, run_cli):
    d = folder("hv5", "a/v.mp4=long.mp4", "b/v.mp4=clip.mp4")
    o = tmp_path / "hv5out"
    for sub, name in (("a", "long.mp4"), ("b", "clip.mp4")):
        (o / sub).mkdir(parents=True)
        shutil.copy2(media / name, o / sub / "v.mp4")
    keep = tmp_path / "stills"
    out = run_cli("verify", d, o, "--keep-samples", keep)
    assert out.rc == 0, out
    assert len(list((keep / "a").glob("v_*_source.png"))) == 3
    assert len(list((keep / "b").glob("v_*_target.png"))) == 3


# --- interactive ---------------------------------------------------------------------------------
def test_interactive_rescan(folder, tmp_path, pty_run, run_cli):
    d = folder("hi", "l.mp4=long.mp4")
    o = tmp_path / "hiout"
    run_cli("video", d, o, *FAST)
    env = {"MO_MIN_SIZE_MB": "0", "MO_ENABLE_PROBE": "false", "MO_CPU_PRESET": "ultrafast"}
    # default settings? n, source, target, delete originals, encoder, rescan
    out = pty_run(["n", str(d), str(o), "", "cpu", "y"], "video", env=env)
    assert "Scan all files again" in out and "Ignored for this run" in out, out
    assert "Cache (this scan):    0 file(s)" in out
    out = pty_run(["n", str(d), str(o), "", "cpu", ""], "video", env=env)
    assert "Cache (this scan):    1 file(s)" in out, out
