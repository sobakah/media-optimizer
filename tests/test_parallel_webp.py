"""Animated WebP for short clips, and parallel video encoding."""

from __future__ import annotations

import subprocess
import threading
import time

import pytest
from PIL import Image

from conftest import leftovers, running, wait_running
from media_optimizer import gpu, proc
from media_optimizer import media as mo_media
from media_optimizer.gpu import GpuArgs

FAST = ["--min-size", "0", "--no-probe", "--preset", "ultrafast"]


# --- animated WebP -------------------------------------------------------------------
def test_webp_clip_animates_and_loops(folder, tmp_path, run_cli):
    d = folder("w1", "c.mp4=clip.mp4")
    out = run_cli("clips", d, "--format", "webp", "--keep-larger", "--delete", "--force-delete")
    assert out.rc == 0, out
    with Image.open(d / "c.webp") as im:
        assert im.n_frames == 75, "3 s at 25 fps"
        assert im.info.get("loop") == 0, "loops forever, like a GIF"
    assert "picture n/a" not in out and " dB)" in out, "the picture was really compared"
    assert not (d / "c.mp4").exists()


def test_webp_larger_than_source_is_retried_then_discarded(folder, run_cli):
    """testsrc2 in H.264 is small; WebP stays larger even at a lower quality."""
    d = folder("w2", "c.mp4=clip.mp4")
    out = run_cli("clips", d, "--format", "webp", "--delete", "--force-delete")
    assert "[RETRY]" in out and "quality 25" in out, out
    assert "[DISCARDED]" in out and (d / "c.mp4").exists() and not (d / "c.webp").exists()


def test_webp_wrong_picture_is_detected(media, tmp_path):
    other = tmp_path / "other.webp"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=320x240:rate=25",
                    "-t", "3", "-c:v", "libwebp_anim", "-quality", "50", "-loop", "0", str(other)], check=True)
    result = mo_media.verify_visual(media / "clip.mp4", other, 18, 3)
    assert not result.ok and result.best is not None and result.best < 18, result
    right = tmp_path / "right.webp"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media / "clip.mp4"), "-c:v", "libwebp_anim",
                    "-quality", "50", "-loop", "0", str(right)], check=True)
    result = mo_media.verify_visual(media / "clip.mp4", right, 18, 3)
    # Measured 23.7 dB for this small, very detailed clip at quality 50 (32 dB
    # at 640x360): the margin above 18 dB is smaller than for video.
    assert result.ok and result.best > 21, result


def test_nothing_measured_is_not_a_pass(media, tmp_path, monkeypatch):
    """The AVIF bug: no sample could be compared, and that counted as fine."""
    real = proc.run

    def no_psnr(cmd, **kw):
        if "-lavfi" in cmd:
            return proc.Result(1, "", "no frame")
        return real(cmd, **kw)
    monkeypatch.setattr(proc, "run", no_psnr)
    result = mo_media.verify_visual(media / "clip.mp4", media / "clip.mp4", 18, 3)
    assert not result.ok and "no sample" in result.samples


def test_video_skips_clip_with_webp(folder, run_cli):
    d = folder("w3", "l.mp4=long.mp4")
    (d / "l.webp").write_bytes(b"x" * 100)
    out = run_cli("video", d, "--encoder", "cpu", *FAST, "--no-delete", env={"MO_CLIP_FORMAT": "webp"})
    assert not (d / "l_h265.mp4").exists(), out


# --- parallel video encoding -------------------------------------------------------------
@pytest.fixture
def fake_gpu(monkeypatch):
    """A 'GPU' that is libx265 underneath, so the lanes can be tested on any
    machine. Records how many encodes ran at the same time per lane."""
    stats = {"now": {"gpu": 0, "cpu": 0}, "max": {"gpu": 0, "cpu": 0}}
    lock = threading.Lock()
    monkeypatch.setattr(gpu, "detect_device", lambda s: ("/dev/fake", ""))
    monkeypatch.setattr(gpu, "gpu_args", lambda s, device, pix="", codec=None: GpuArgs(
        (), ("-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error:fakegpu=1"), ()))
    real = proc.run

    def counting(cmd, **kw):
        if "-progress" not in cmd:
            return real(cmd, **kw)
        lane = "gpu" if any("fakegpu" in a for a in cmd) else "cpu"
        with lock:
            stats["now"][lane] += 1
            stats["max"][lane] = max(stats["max"][lane], stats["now"][lane])
        try:
            time.sleep(0.5)              # make overlaps visible even for tiny files
            return real([a.replace(":fakegpu=1", "") for a in cmd], **kw)
        finally:
            with lock:
                stats["now"][lane] -= 1
    monkeypatch.setattr(proc, "run", counting)
    return stats


def test_gpu_lane_runs_in_parallel(folder, tmp_path, main, fake_gpu):
    d = folder("p1", *(f"v{i}.mp4=long.mp4" for i in range(5)))
    out = main("video", d, tmp_path / "p1out", "--encoder", "gpu", "--gpu-workers", "2", *FAST)
    assert out.rc == 0, out
    assert fake_gpu["max"]["gpu"] == 2, fake_gpu
    assert len(list((tmp_path / "p1out").glob("*.mp4"))) == 5
    assert "Re-encoded:           5 file(s) (GPU: 5 | CPU: 0)" in out


def test_gpu_workers_1_is_sequential(folder, tmp_path, main, fake_gpu):
    d = folder("p2", *(f"v{i}.mp4=long.mp4" for i in range(3)))
    main("video", d, tmp_path / "p2out", "--encoder", "gpu", "--gpu-workers", "1", *FAST)
    assert fake_gpu["max"]["gpu"] == 1


def test_cpu_lane_runs_alongside(folder, tmp_path, main, fake_gpu):
    """auto: large frames to the 'GPU', the small clip to the CPU, both at once."""
    d = folder("p3", "a.mp4=long.mp4", "b.mp4=long.mp4", "c.mp4=clip.mp4")
    out = main("video", d, tmp_path / "p3out", "--encoder", "auto", "--gpu-min-pixels", "200000",
               "--keep-larger", *FAST)
    assert out.rc == 0, out
    assert fake_gpu["max"]["cpu"] == 1 and fake_gpu["max"]["gpu"] == 2, fake_gpu
    assert "(GPU: 2 | CPU: 1)" in out


def test_gpu_failure_moves_everything_to_cpu(folder, tmp_path, main, monkeypatch):
    d = folder("p4", *(f"v{i}.mp4=long.mp4" for i in range(4)))
    monkeypatch.setattr(gpu, "detect_device", lambda s: ("/dev/fake", ""))
    monkeypatch.setattr(gpu, "gpu_args", lambda s, device, pix="", codec=None: GpuArgs(
        (), ("-c:v", "no_such_encoder"), ()))
    out = main("video", d, tmp_path / "p4out", "--encoder", "gpu", "--gpu-workers", "2", *FAST)
    assert out.rc == 0, out
    assert "The GPU failed during the run" in out
    assert "(GPU: 0 | CPU: 4)" in out
    assert len(list((tmp_path / "p4out").glob("*.mp4"))) == 4


def _vaapi_works() -> bool:
    if not mo_media.has_encoder("hevc_vaapi"):
        return False
    from media_optimizer.config import Settings
    return gpu.detect_device(Settings())[0] is not None


@pytest.mark.slow
@pytest.mark.skipif(not _vaapi_works(), reason="no working VAAPI HEVC encoder on this machine")
def test_real_gpu_parallel(folder, tmp_path, run_cli):
    d = folder("p5", *(f"v{i}.mp4=slow.mp4" for i in range(3)))
    o = tmp_path / "p5out"
    out = run_cli("video", d, o, "--encoder", "gpu", "--gpu-workers", "2", "--min-size", "0", "--no-probe")
    assert out.rc == 0, out
    assert "(GPU: 3 | CPU: 0)" in out and "Picture verified:     3 file(s)" in out


@pytest.mark.slow
def test_sigterm_stops_every_running_encode(folder, tmp_path, start_cli):
    """Two encodes in parallel (CPU lane + a big file on the GPU lane, or two
    CPU-heavy encodes): all stop, nothing is left behind."""
    d = folder("p6", "a.mp4=slow.mp4", "b.mp4=slow.mp4", "c.mp4=long.mp4")
    o = tmp_path / "p6out"
    encoder = "auto" if _vaapi_works() else "cpu"
    p = start_cli("video", d, o, "--encoder", encoder, "--gpu-workers", "2", "--min-size", "0", "--no-probe",
                  "--preset", "slow", out=tmp_path / "term.out")
    assert wait_running("ffmpeg", str(d))
    time.sleep(1.5)
    t0 = time.monotonic()
    p.terminate()
    rc = p.wait(timeout=30)
    took = time.monotonic() - t0
    assert rc == 130 and took < 5, (rc, took, (tmp_path / "term.out").read_text())
    time.sleep(0.3)
    assert not running("ffmpeg", str(d))
    assert not leftovers(o)
    assert len(list(d.glob("*.mp4"))) == 3
