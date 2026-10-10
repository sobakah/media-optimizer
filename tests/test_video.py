"""The video stage, its cache, and the verify command."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from conftest import leftovers, running, snapshot, wait_running
from media_optimizer import gpu, media
from media_optimizer.config import Settings

FAST = ["--encoder", "cpu", "--min-size", "0", "--no-probe", "--preset", "ultrafast"]


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


def test_suspicious_picture_keeps_original(folder, main, monkeypatch):
    """Bash deleted the original after a suspicious picture comparison when
    --delete was on; here it is always kept."""
    d = folder("h2c", "l.mp4=long.mp4")
    monkeypatch.setattr(media, "verify_visual", lambda *a, **k: media.Visual(False, 10.0, "x"))
    out = main("video", d, *FAST, "--delete", "--force-delete")
    assert "[PICTURE?]" in out, out
    assert (d / "l.mp4").exists() and (d / "l_h265.mp4").exists()
    assert "l.mp4" in (d / ".video_suspect.txt").read_text()


def test_strict_visual_discards(folder, main, monkeypatch):
    d = folder("h2d", "l.mp4=long.mp4")
    monkeypatch.setattr(media, "verify_visual", lambda *a, **k: media.Visual(False, 10.0, "x"))
    out = main("video", d, *FAST, "--delete", "--force-delete", "--strict-visual")
    assert out.rc == 3, out
    assert (d / "l.mp4").exists() and not (d / "l_h265.mp4").exists()
    assert not leftovers(d)


def test_runtime_mismatch_keeps_original(folder, main, monkeypatch):
    d = folder("h2e", "l.mp4=long.mp4")
    monkeypatch.setattr(media, "verify_video", lambda *a, **k: media.DurationCheck(False, 8.0, 3.0))
    out = main("video", d, *FAST, "--delete", "--force-delete")
    assert "[INVALID]" in out and out.rc == 3, out
    assert (d / "l.mp4").exists() and not (d / "l_h265.mp4").exists()


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
    assert "entries taken over from 1 cache file" in out, out
    assert f"{d}/sub/l.mp4" in (d / ".video_conversion_cache.txt").read_text().splitlines()
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
    assert "entries taken over from 1 cache file" in out, out
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


# --- interactive ---------------------------------------------------------------------------------
def test_interactive_rescan(folder, tmp_path, pty_run, run_cli):
    d = folder("hi", "l.mp4=long.mp4")
    o = tmp_path / "hiout"
    run_cli("video", d, o, *FAST)
    env = {"MO_MIN_SIZE_MB": "0", "MO_ENABLE_PROBE": "false", "MO_CPU_PRESET": "ultrafast"}
    out = pty_run(["n", str(d), str(o), "cpu", "", "y"], "video", env=env)
    assert "Scan all videos again" in out and "Ignored for this run" in out, out
    assert "Cache (this scan):    0 file(s)" in out
    out = pty_run(["n", str(d), str(o), "cpu", "", ""], "video", env=env)
    assert "Cache (this scan):    1 file(s)" in out, out
