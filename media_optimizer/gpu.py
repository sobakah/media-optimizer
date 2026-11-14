"""GPU encoder arguments, render node detection and the GPU self-test.

``gpu_args`` is the only place the GPU arguments are built, and it is a pure
function of the settings, the device and the source's pixel format. The
probe slices, the real encode and the startup check therefore always use
identical arguments; in the Bash version that was an invariant to remember
("rebuild after any change to VAAPI_DEVICE, GPU_CODEC or the pixel format").

The pixel format is fixed, never written as an alternative: with
'format=nv12|p010' the filter picks p010 while the encoder runs 8 bit, and
the result is a half-filled, green picture. 10 bit only together with the
main10 profile (gpu_allow_10bit).
"""

from __future__ import annotations

import dataclasses
import glob
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import container, media, proc, ui
from .config import Settings
from .ui import StyleUI as S


@dataclass(frozen=True)
class GpuArgs:
    hw: tuple[str, ...]       # before the input: device initialisation
    enc: tuple[str, ...]      # after the input: filter and encoder
    tag: tuple[str, ...]      # container tag


def gpu_args(s: Settings, device: str, src_pix_fmt: str = "", codec: str | None = None) -> GpuArgs:
    codec = codec or s.gpu_codec
    tag: tuple[str, ...] = ()
    if codec == "hevc_vulkan":
        if s.hevc_tag:
            tag = ("-tag:v", s.hevc_tag)
        return GpuArgs(("-init_hw_device", f"vulkan=vk:{s.vulkan_device}", "-filter_hw_device", "vk"),
                       ("-vf", "hwupload", "-c:v", "hevc_vulkan", "-cq", str(s.gpu_qp)), tag)

    vf, profile = "format=nv12,hwupload", ()
    if s.gpu_allow_10bit and "10" in src_pix_fmt:
        vf = "format=p010,hwupload"
        if codec == "hevc_vaapi":
            profile = ("-profile:v", "main10")
    enc = ["-vf", vf, "-c:v", codec]
    qp = str(s.gpu_qp)
    enc += {
        "CQP": ["-rc_mode", "CQP", "-global_quality", qp],
        "VBR": ["-rc_mode", "VBR", "-b:v", "0", "-global_quality", qp],
        "ICQ": ["-rc_mode", "ICQ", "-global_quality", qp],
        "QVBR": ["-rc_mode", "QVBR", "-b:v", "0", "-global_quality", qp],
        "CBR": ["-rc_mode", "CBR", "-b:v", s.gpu_bitrate],
        "AUTO": ["-global_quality", qp],
    }[s.gpu_rc_mode.upper()]
    if s.gpu_bf is not None:
        enc += ["-bf", str(s.gpu_bf)]
    if s.gpu_low_power:
        enc += ["-low_power", "1"]
    if s.gpu_async_depth is not None:
        enc += ["-async_depth", str(s.gpu_async_depth)]
    enc += profile
    # "hvc1" requires VPS/SPS/PPS to live only in the hvcC box. If the encoder
    # emits them in-band, the muxer builds an incomplete hvcC: correctly
    # encoded, no longer decodable. Only on explicit request.
    if codec == "hevc_vaapi" and s.hevc_tag:
        tag = ("-tag:v", s.hevc_tag)
    return GpuArgs(("-vaapi_device", device), tuple(enc), tag)


def render_nodes() -> list[str]:
    return sorted(glob.glob("/dev/dri/renderD*"))


def probe_device(s: Settings, device: str) -> str | None:
    """A real five-frame test encode with exactly the arguments the run
    uses. Returns None when it works, else the reason."""
    if s.gpu_codec != "hevc_vulkan":
        if not os.path.exists(device):
            return "device does not exist"
        if not os.access(device, os.R_OK | os.W_OK):
            return "no read/write permission (group 'render'?)"
    args = gpu_args(s, device)
    res = proc.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *args.hw,
                    "-f", "lavfi", "-i", "testsrc=s=1280x720:r=30", "-frames:v", "5",
                    *args.enc, "-an", "-f", "null", "-"])
    return None if res.ok else error_reason(res.stderr)


# ffmpeg names the cause first and its consequences after it. The last line
# alone ("Nothing was written into output file...") was all a run showed for
# a driver without an HEVC encoder.
_CONSEQUENCES = ("Error while opening encoder", "Could not open encoder", "Error sending frames",
                 "Task finished with error", "Terminating thread", "Nothing was written",
                 "Conversion failed", "not a conformant Vulkan implementation")
_ADDRESS = re.compile(r" @ 0x[0-9a-fA-F]+\]")


def error_reason(stderr: str) -> str:
    """The line of ffmpeg's error output that names the cause, without the
    memory addresses ("[hevc_vaapi @ 0x55...]" -> "[hevc_vaapi]")."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "ffmpeg failed"
    cause = next((line for line in lines if not any(c in line for c in _CONSEQUENCES)), lines[-1])
    return _ADDRESS.sub("]", cause)


def detect_device(s: Settings) -> tuple[str | None, str]:
    """Walk the render nodes, the configured one first. On machines with an
    iGPU and a dGPU, renderD128 is often the iGPU and the dGPU renderD129.
    Returns (device, "") or (None, first error)."""
    if s.gpu_codec == "hevc_vulkan":
        err = probe_device(s, "")
        return ("vulkan", "") if err is None else (None, err)
    nodes = render_nodes()
    wanted = s.vaapi_device
    candidates = nodes if wanted.lower() == "auto" else [wanted] + [n for n in nodes if n != wanted]
    first_error = ""
    for dev in candidates:
        if not os.path.exists(dev):
            first_error = first_error or f"{dev}: device does not exist"
            continue
        err = probe_device(s, dev)
        if err is None:
            if wanted.lower() != "auto" and dev != wanted:
                ui.say("GPU", S.YELLOW, f"{wanted} provides no {s.gpu_codec}, but {S.BOLD}{dev}{S.RESET} does. "
                                        "Using that one.")
                ui.out(f'      Set permanently: "vaapi_device": "{dev}" in the config file')
            return dev, ""
        first_error = first_error or f"{dev}: {err}"
    return None, first_error or "no render nodes found under /dev/dri/"


def _distro() -> str:
    """ID from /etc/os-release ("fedora"), "" when unknown."""
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r'^ID="?([^"\n]*)"?$', text, re.MULTILINE)
    return match.group(1).lower() if match else ""


def driver_hint(reason: str) -> list[str]:
    """What to do when the driver has no HEVC encoder at all. Fedora builds
    Mesa without H.264/H.265; the codecs come with RPM Fusion's freeworld
    package, which a release upgrade with --allowerasing removes silently
    when Fedora's Mesa is older than RPM Fusion's build."""
    if "No usable encoding profile" not in reason and "does not support encoding" not in reason:
        return []
    lines = [f"      {S.BOLD}The video driver offers no HEVC encoder.{S.RESET} Distributions that build",
             "      Mesa without patented codecs need a separate driver package."]
    if _distro() == "fedora" and "No usable encoding profile" in reason and container.image_based_host():
        lines += ["      Fedora Silverblue and other image-based systems (RPM Fusion):",
                  "        rpm-ostree install mesa-va-drivers-freeworld    # then restart the computer",
                  "        rpm -q mesa-va-drivers-freeworld    # removed by a release upgrade?"]
    elif _distro() == "fedora" and "No usable encoding profile" in reason:
        lines += ["      Fedora (RPM Fusion):",
                  "        sudo dnf install mesa-va-drivers-freeworld",
                  "        rpm -q mesa-va-drivers-freeworld    # removed by a release upgrade?",
                  '      If dnf answers "nothing provides mesa-filesystem = <version>", Fedora\'s',
                  "      Mesa is older than RPM Fusion's build: wait for the Mesa update, or take",
                  "      it from updates-testing first:",
                  "        sudo dnf upgrade --refresh --enablerepo=updates-testing 'mesa*'"]
    return lines


def diagnose(reason: str = "") -> None:
    lines = driver_hint(reason)
    lines += [f"      {S.BOLD}Diagnostics:{S.RESET}",
              "        ls -l /dev/dri/by-path/          # which node is the dGPU"]
    lines += [f"        vainfo --display drm --device {d} | grep -i 'hevc.*enc'   # libva-utils"
              for d in render_nodes()]
    lines += ["        id | grep -o render               # group membership",
              "        ffmpeg -hide_banner -encoders | grep hevc_vaapi",
              "      RDNA4 (VCN 5) needs Mesa 25.0+ and kernel 6.13+."]
    ui.out("\n".join(lines), err=True)


# --- self-test ---------------------------------------------------------------------------
# Tries option variants on a real slice and rates each result by picture
# comparison. An encoder producing green areas without any error message is,
# from outside, indistinguishable from success; only the picture tells.
# "original" is the minimal combination; each following variant adds exactly
# ONE option, so a single failing one names its trigger.
VARIANTS = [
    ("original", "original command, device after -i"),
    ("device-first", "like original, device before -i"),
    ("bf0", "like original + -bf 0"),
    ("hvc1", "like original + -tag:v hvc1"),
    ("faststart", "like original + faststart"),
    ("current", "current settings"),
    ("av1", "av1_vaapi instead of hevc_vaapi"),
    ("vulkan", "hevc_vulkan via RADV"),
]


def selftest_device(s: Settings) -> str:
    """The render node a run would use: on a machine with an iGPU and a dGPU
    the first node is often the wrong one. When none passes the check (the
    reason for running the self-test), the first node."""
    if s.vaapi_device.lower() != "auto":
        return s.vaapi_device
    device, _ = detect_device(dataclasses.replace(s, gpu_codec="hevc_vaapi"))
    if device is not None:
        return device
    nodes = render_nodes()
    return nodes[0] if nodes else "/dev/dri/renderD128"


def selftest_advice(winners: list[str]) -> str:
    """What the usable variants mean. The current settings first: the plain
    variants run before them, and a working machine was told its settings
    were the problem."""
    if "current" in winners:
        return "  The current settings work, nothing to change."
    if any(name in winners for name in ("original", "device-first", "bf0", "hvc1", "faststart")):
        return "  hevc_vaapi works in principle. The current settings\n  are the problem, not the driver."
    if "av1" in winners:
        return ('  For the config file:\n\n    "gpu_codec": "av1_vaapi"\n\n'
                "  AV1 saves more space than HEVC but is not played by older\n  players and TVs.")
    return '  For the config file:\n\n    "gpu_codec": "hevc_vulkan"'


def selftest(s: Settings, src: Path) -> int:
    if not src.is_file():
        ui.out(f"Test file not found: {src}", err=True)
        return 1
    device = selftest_device(s)
    duration = media.probe(src).duration
    seclen = 6
    start = max(0, int((duration - seclen) / 2)) if duration else 0

    ui.banner("GPU SELF-TEST")
    ui.out(f"  File:   {src.name}\n  Device: {device}\n  Slice:  {seclen}s at {start}s\n")

    with tempfile.TemporaryDirectory(prefix="mo_selftest_") as tmpdir:
        # MKV takes any codec; "-c copy" of an AVI's or WMV's video into MP4 fails.
        ref = Path(tmpdir) / "ref.mkv"
        if not proc.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", str(start), "-t", str(seclen),
                         "-i", str(src), "-map", "0:v:0", "-an", "-c", "copy", str(ref)]).ok:
            ui.out("Could not create the reference slice.", err=True)
            return 1

        base = ["-vf", "format=nv12,hwupload", "-c:v", "hevc_vaapi", "-rc_mode", "CQP",
                "-global_quality", str(s.gpu_qp)]
        cut = ["-ss", str(start), "-t", str(seclen), "-i", str(src)]
        winners = []
        ui.out(f"  {'Variant':<13} {'Description':<34} {'Result':<10} PSNR")
        ui.out("  " + "-" * 75)
        for name, label in VARIANTS:
            needed = {"av1": "av1_vaapi", "vulkan": "hevc_vulkan"}.get(name, "hevc_vaapi")
            if not media.has_encoder(needed):
                ui.out(f"  {name:<13} {label:<34} {'n/a':<10} encoder missing")
                continue
            out = Path(tmpdir) / f"{name}.mp4"
            cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
            dev = ["-vaapi_device", device]
            if name == "original":
                cmd += ["-reinit_filter", "0", *cut, *dev, *base]
            elif name == "device-first":
                cmd += [*dev, "-reinit_filter", "0", *cut, *base]
            elif name == "bf0":
                cmd += ["-reinit_filter", "0", *cut, *dev, *base, "-bf", "0"]
            elif name == "hvc1":
                cmd += ["-reinit_filter", "0", *cut, *dev, *base, "-tag:v", "hvc1"]
            elif name == "faststart":
                cmd += ["-reinit_filter", "0", *cut, *dev, *base, "-movflags", "+faststart"]
            else:
                codec = {"current": "hevc_vaapi", "av1": "av1_vaapi", "vulkan": "hevc_vulkan"}[name]
                args = gpu_args(s, device, codec=codec)
                cmd += [*args.hw]
                if name != "vulkan":
                    cmd += ["-reinit_filter", "0"]
                cmd += [*cut, *args.enc, *args.tag]
            cmd += ["-an", "-map", "0:v:0", str(out)]
            if proc.run(cmd).ok and out.is_file() and out.stat().st_size > 0:
                visual = media.verify_visual(ref, out, s.visual_psnr_min, s.visual_samples)
                if visual.ok:
                    result, psnr = "usable", f"{visual.best_text} dB"
                    winners.append(name)
                else:
                    result, psnr = "BROKEN", f"{visual.best_text} dB"
            else:
                result, psnr = "error", "-"
            ui.out(f"  {name:<13} {label:<34} {result:<10} {psnr}")

    ui.out()
    if not winners:
        ui.say("RESULT", S.RED, "No combination produced a usable picture.")
        ui.out("           On Fedora check that the full VA drivers are active:\n"
               "             rpm -q mesa-va-drivers-freeworld\n"
               f"             vainfo --display drm --device {device} | grep -i 'hevc.*enc'\n"
               "           Until then: --encoder cpu")
        return 1
    ui.say("RESULT", S.GREEN, f"Usable: {' '.join(winners)}\n")
    ui.out(selftest_advice(winners))
    ui.out()
    return 0
