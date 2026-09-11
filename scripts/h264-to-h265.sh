#!/bin/bash
# ==============================================================================
# h264-to-h265.sh  -  video -> H.265/HEVC (VAAPI, Vulkan or libx265)
#
# STRUCTURE (in this order in the code):
#   KONFIGURATION      Defaults, Konfigdatei, Optionen. Jede Einstellung folgt
# the pattern VAR="${VAR:-default}" so that environment and
# config file take precedence over the default.
# GPU CAPABILITY     find the render node and run a real test encode.
#                      fahren. Danach steht VAAPI_DEVICE konkret fest.
# GPU SELF-TEST      only with --gpu-selftest: try option variants.
# STATISTICS         counters that are carried over
# across aborts (.h265_stats.env).
# CACHE              finished files, so that a second
# run does not have to ffprobe every file again.
# TARGET FILES       find over the tree or the lines from --from-list.
# MAIN LOOP          per file: filter, encoder choice, probe, encode,
#                      Verifikation, Groessenpruefung, Loeschentscheidung.
# FINISH             summary and pending deletions.
#
# IMPORTANT INVARIANTS when changing this:
# - Output always goes to "<target>.part.<pid>.<random>.mp4" first
# and only renamed after passing its check.
# - An original is NEVER deleted before verify_video (and optionally
# verify_visual) confirmed the output.
# - gpu_encoder_args() must be rebuilt after any change to VAAPI_DEVICE, GPU_CODEC
# or the pixel format, otherwise probe and
# encode would use stale arguments.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
_MO_ROOT="$(dirname "$SCRIPT_DIR")"; load_config
mo_install_exit_handler

usage() {
    cat <<'EOF'
h264-to-h265.sh - re-encodes video to HEVC

  -i, --input   <dir>    source directory
  -o, --output  <dir>    target directory (empty = in place, _h265 suffix)
      --extensions <list> source extensions, space separated
                         (default: mp4 m4v mov mkv webm avi ts m2ts wmv flv)
      --container <c>    auto | mp4 | mkv | keep   (default: auto)
      --source-codecs <l> which source codecs get re-encoded
                         (default: h264 mpeg4 msmpeg4v3 wmv3 vc1 mpeg2video)
      --encoder <mode>   auto | gpu | cpu   (default: auto)
      --threshold <kbps> CPU/GPU threshold in auto mode (default: 3500)
      --qp <n>           GPU CQP (default: 26)
      --crf <n>          CPU CRF (default: 22)
      --preset <p>       CPU x265 preset (default: medium)
      --x265-params <s>  x265 parameters (default: aq-mode=3:no-sao=1)
      --min-size <mb>    skip files below this size (default: 5)
      --no-probe         no test slice beforehand
      --probe-margin <p> skip when the probe reaches p% of the original
                         (default: 90; 100 = only when it really grows)
      --probe-duration <s> length of the test slice (default: 10)
      --keep-larger      keep larger results as well
      --delete           move originals to the trash after success
      --no-delete        keep originals
      --force-delete     if the trash fails, rm without asking
      --no-faststart     do not move the moov atom to the front (saves a pass)
      --hevc-tag <t>     container tag, e.g. hvc1 (default: none)
      --gpu-codec <c>    hevc_vaapi | av1_vaapi | hevc_vulkan
                         (default: hevc_vaapi)
      --gpu-device <p>   VAAPI render node, or "auto" to try them all
      --force-gpu        skip the startup check and use the GPU directly
      --rc-mode <m>      GPU rate control: CQP | VBR | ICQ | QVBR | CBR
                         (default: CQP)
      --bf <n>           max B-frames on the GPU (default: not set)
      --low-power        use the VAAPI low-power encoder
      --gpu-selftest <file>
                         try option variants on a real file and report which
                         one works, then exit
      --gpu-10bit        encode 10-bit sources in 10 bit on the GPU
                         (p010 + main10; without it they drop to 8 bit)
      --no-verify-visual no PSNR sampling of the picture content
      --strict-visual    discard suspicious outputs instead of only warning
      --rename-inplace   drop the _h265 suffix after deleting the original
                         (in place only, and only when the path is free)
      --from-list <file|auto>
                         process only the source files listed there (one per
                         line). Ignores cache and existing outputs because the
                         selection was made explicitly. "auto" picks
                         .defekte_videos.txt from the target directory.
      --no-cache         ignore the cache file
      --log <file>       log file
      --no-log           do not write a log
      --hold             keep the window open at the end
      --no-hold          never keep the window open
  -n, --dry-run          preview only, write nothing
  -h, --help             this help
EOF
}

# ------------------------------------------------------------------------------
# KONFIGURATION
# ------------------------------------------------------------------------------
SOURCE_DIR="${SOURCE_DIR:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
# No fixed default: the preset depends on whether a target
# Zielverzeichnis angegeben wurde (siehe mo_apply_inplace_defaults).
# A value from the environment or config file counts as explicit.
if [[ -n "${DELETE_ORIGINAL+x}" ]]; then DELETE_ORIGINAL_EXPLICIT=true; fi
DELETE_ORIGINAL="${DELETE_ORIGINAL:-false}"
FORCE_DELETE="${FORCE_DELETE:-false}"
ENCODER_MODE="${ENCODER_MODE:-auto}"
BITRATE_THRESHOLD_KBPS="${BITRATE_THRESHOLD_KBPS:-3500}"
GPU_QP="${GPU_QP:-26}"
CPU_CRF="${CPU_CRF:-22}"
CPU_PRESET="${CPU_PRESET:-medium}"
ENABLE_PROBE="${ENABLE_PROBE:-true}"
PROBE_MARGIN_PCT="${PROBE_MARGIN_PCT:-90}"
DISCARD_IF_LARGER="${DISCARD_IF_LARGER:-true}"
KEEP_SALVAGED_CORRUPT="${KEEP_SALVAGED_CORRUPT:-true}"
MIN_SIZE_MB="${MIN_SIZE_MB:-5}"
USE_CACHE="${USE_CACHE:-true}"
DRY_RUN="${DRY_RUN:-false}"

VAAPI_DEVICE="${VAAPI_DEVICE:-auto}"   # auto = alle Render-Nodes durchprobieren
# Portable default without hardware assumptions. pools/frame-threads match
# what x265 picks itself on 16 threads. asm=avx512 is deliberately
# NOT set here because it slows many CPUs down; on Zen 5 it can help.
# Re-enable deliberately via the config file or --x265-params.
CPU_X265_PARAMS="${CPU_X265_PARAMS:-aq-mode=3:no-sao=1}"
FORCE_GPU="${FORCE_GPU:-false}"
if [[ -n "${RENAME_INPLACE+x}" ]]; then RENAME_INPLACE_EXPLICIT=true; fi
RENAME_INPLACE="${RENAME_INPLACE:-false}"
FROM_LIST="${FROM_LIST:-}"
STREAM_FALLBACK=false
AUTO_FIX_LIST="${AUTO_FIX_LIST:-false}"
# Empty = ffmpeg default (hev1). Only set "hvc1" if the self-test
# confirms the output stays decodable with it.
HEVC_TAG="${HEVC_TAG:-}"
GPU_CODEC="${GPU_CODEC:-hevc_vaapi}"     # hevc_vaapi | av1_vaapi | hevc_vulkan
VULKAN_DEVICE="${VULKAN_DEVICE:-0}"
GPU_RC_MODE="${GPU_RC_MODE:-CQP}"        # CQP | VBR | ICQ | QVBR | CBR | AUTO
# B-frames on the GPU. Empty means -bf is not set and ffmpeg decides.
# Some VAAPI drivers produce broken pictures with a forced -bf; whether
# that applies here is answered by --gpu-selftest.
GPU_BF="${GPU_BF:-}"
GPU_LOW_POWER="${GPU_LOW_POWER:-false}"
GPU_ASYNC_DEPTH="${GPU_ASYNC_DEPTH:-}"
GPU_BITRATE="${GPU_BITRATE:-8M}"
GPU_SELFTEST=""
GPU_ALLOW_10BIT="${GPU_ALLOW_10BIT:-false}"
VERIFY_VISUAL="${VERIFY_VISUAL:-true}"
VISUAL_STRICT="${VISUAL_STRICT:-false}"   # true = verdaechtige Ausgaben verwerfen
VISUAL_PSNR_MIN="${VISUAL_PSNR_MIN:-15}"
GPU_FELL_BACK=false
FASTSTART="${FASTSTART:-true}"
# Source formats. ffmpeg reads all of these containers; what matters is
# Zielcontainer, siehe VIDEO_CONTAINER.
VIDEO_EXTENSIONS="${VIDEO_EXTENSIONS:-mp4 m4v mov mkv webm avi ts m2ts wmv flv}"

# Zielcontainer:
# auto = mp4 for mp4/m4v/mov, mkv otherwise
#   mp4 | mkv = fest
# keep = keep the source extension
# Background: MP4 accepts neither Opus nor SRT streams, both common in MKV
# and WebM. MKV accepts practically anything.
VIDEO_CONTAINER="${VIDEO_CONTAINER:-auto}"

# Source video codecs that get re-encoded. VP9, AV1 and HEVC are already
# efficient; re-encoding them costs quality without saving space.
SOURCE_CODECS="${SOURCE_CODECS:-h264 mpeg4 msmpeg4v3 wmv3 vc1 mpeg2video}"

RECURSIVE=true
PROBE_DURATION="${PROBE_DURATION:-10}"
PROBE_MIN_DURATION=60
SKIP_EXISTING=true
DURATION_TOLERANCE_PCT="${DURATION_TOLERANCE_PCT:-2}"

POSITIONAL=()
while (( $# > 0 )); do
    case "$1" in
        -i|--input)      SOURCE_DIR="$2"; shift 2 ;;
        -o|--output)     OUTPUT_DIR="$2"; shift 2 ;;
        --encoder)       ENCODER_MODE="$2"; shift 2 ;;
        --threshold)     BITRATE_THRESHOLD_KBPS="$2"; shift 2 ;;
        --qp)            GPU_QP="$2"; shift 2 ;;
        --crf)           CPU_CRF="$2"; shift 2 ;;
        --preset)        CPU_PRESET="$2"; shift 2 ;;
        --min-size)      MIN_SIZE_MB="$2"; shift 2 ;;
        --no-probe)      ENABLE_PROBE=false; shift ;;
        --probe-margin)  PROBE_MARGIN_PCT="$2"; shift 2 ;;
        --probe-duration) PROBE_DURATION="$2"; shift 2 ;;
        --keep-larger)   DISCARD_IF_LARGER=false; shift ;;
        --delete)        DELETE_ORIGINAL=true; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --no-delete)     DELETE_ORIGINAL=false; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --force-delete)  FORCE_DELETE=true; shift ;;
        --gpu-device)    VAAPI_DEVICE="$2"; shift 2 ;;
        --force-gpu)     FORCE_GPU=true; ENCODER_MODE=gpu; shift ;;
        --x265-params)   CPU_X265_PARAMS="$2"; shift 2 ;;
        --no-faststart)  FASTSTART=false; shift ;;
        --rename-inplace) RENAME_INPLACE=true; RENAME_INPLACE_EXPLICIT=true; shift ;;
        --gpu-10bit)     GPU_ALLOW_10BIT=true; shift ;;
        --hevc-tag)      HEVC_TAG="$2"; shift 2 ;;
        --gpu-codec)     GPU_CODEC="$2"; shift 2 ;;
        --rc-mode)       GPU_RC_MODE="$2"; shift 2 ;;
        --bf)            GPU_BF="$2"; shift 2 ;;
        --low-power)     GPU_LOW_POWER=true; shift ;;
        --gpu-selftest)  GPU_SELFTEST="$2"; shift 2 ;;
        --no-verify-visual) VERIFY_VISUAL=false; shift ;;
        --strict-visual) VISUAL_STRICT=true; shift ;;
        --from-list)     FROM_LIST="$2"; shift 2 ;;
        --extensions)    VIDEO_EXTENSIONS="$2"; shift 2 ;;
        --container)     VIDEO_CONTAINER="$2"; shift 2 ;;
        --source-codecs) SOURCE_CODECS="$2"; shift 2 ;;
        --no-cache)      USE_CACHE=false; shift ;;
        -n|--dry-run)    DRY_RUN=true; shift ;;
        --log)           MO_LOG_FILE="$2"; shift 2 ;;
        --no-log)        MO_LOG=false; shift ;;
        --hold)          MO_HOLD=1; shift ;;
        --no-hold)       MO_HOLD=0; shift ;;
        -h|--help)       usage; exit 0 ;;
        --)              shift; while (( $# > 0 )); do POSITIONAL+=("$1"); shift; done ;;
        -*)              echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)               POSITIONAL+=("$1"); shift ;;
    esac
done
[[ -n "${POSITIONAL[0]:-}" ]] && SOURCE_DIR="${POSITIONAL[0]}"
[[ -n "${POSITIONAL[1]:-}" ]] && OUTPUT_DIR="${POSITIONAL[1]}"

if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" ]]; then
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    printf "%bBatch video re-encoder (H.264 -> H.265 / HEVC)%b\n" "$C_BOLD" "$C_RESET"
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    read -rp "Run with default settings? [Y/n]: " start_choice || start_choice=""
    if [[ "${start_choice,,}" =~ ^(n|nein|no)$ ]]; then
        read -rp "  Source directory [$SOURCE_DIR]: " x && SOURCE_DIR="${x:-$SOURCE_DIR}"
        read -rp "  Target directory (empty = in place) [$OUTPUT_DIR]: " x && OUTPUT_DIR="${x:-$OUTPUT_DIR}"
        read -rp "  Encoder (auto/gpu/cpu) [$ENCODER_MODE]: " x && ENCODER_MODE="${x:-$ENCODER_MODE}"
        read -rp "  Move originals to trash? [y/N]: " x
        [[ "${x,,}" =~ ^(j|ja|y|yes)$ ]] && DELETE_ORIGINAL=true || DELETE_ORIGINAL=false
    fi
fi

if [[ -n "$GPU_SELFTEST" && -z "$SOURCE_DIR" ]]; then
    SOURCE_DIR="$(dirname "$GPU_SELFTEST")"   # the self-test only needs the file
fi
[[ -z "$SOURCE_DIR" ]] && { echo "No source directory given." >&2; exit 1; }
[[ -d "$SOURCE_DIR" ]] || { echo "Source directory not found: $SOURCE_DIR" >&2; exit 1; }
SOURCE_DIR="${SOURCE_DIR%/}"
if [[ -n "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="${OUTPUT_DIR%/}"
    [[ "$DRY_RUN" == true ]] || mkdir -p "$OUTPUT_DIR"
fi

mo_apply_inplace_defaults "$OUTPUT_DIR"
export MO_LOG_TAG="h265"
mo_log_init "h264-to-h265.sh" "$SOURCE_DIR" "$OUTPUT_DIR"

require_cmds ffmpeg ffprobe || exit 1
gain_needed=$(( 100 - PROBE_MARGIN_PCT ))

# "auto" or AUTO_FIX_LIST=true takes the defect list from the target directory
# falls vorhanden. Sonst normaler Volldurchlauf.
DEFAULT_FIX_LIST="${OUTPUT_DIR:+$OUTPUT_DIR/.defekte_videos.txt}"
if [[ "${FROM_LIST,,}" == "auto" || ( -z "$FROM_LIST" && "$AUTO_FIX_LIST" == true ) ]]; then
    if [[ -n "$DEFAULT_FIX_LIST" && -s "$DEFAULT_FIX_LIST" ]]; then
        FROM_LIST="$DEFAULT_FIX_LIST"
    else
        [[ "${FROM_LIST,,}" == "auto" ]] && printf "%b[LIST]%b No defect list found, running normally.\n" \
            "$C_CYAN" "$C_RESET"
        FROM_LIST=""
    fi
fi

# A list exists but was not requested: only point it out.
# Silently switching to a subset would be dangerous, because a stale
# list would silently hide the rest of the collection.
if [[ -z "$FROM_LIST" && -n "$DEFAULT_FIX_LIST" && -s "$DEFAULT_FIX_LIST" ]]; then
    printf "%b[NOTE]%b A defect list with %d entry/entries exists:\n" \
        "$C_YELLOW" "$C_RESET" "$(wc -l < "$DEFAULT_FIX_LIST")"
    printf "          %s\n" "$DEFAULT_FIX_LIST"
    printf "          Process only those: --from-list auto\n\n"
fi

if [[ -n "$FROM_LIST" ]]; then
    [[ -f "$FROM_LIST" ]] || { echo "List not found: $FROM_LIST" >&2; exit 1; }
    # An explicit list beats cache and existing outputs: the caller wants
    # Aufrufer will genau diese Dateien neu erzeugen.
    USE_CACHE=false
    SKIP_EXISTING=false
    printf "%b[LIST]%b processing only the files from %s\n" "$C_CYAN" "$C_RESET" "$FROM_LIST"
fi
[[ "$DRY_RUN" == true ]] && printf "%b[DRY-RUN]%b Nothing will be written or deleted.\n" "$C_CYAN" "$C_RESET"

CACHE_FILE="${SOURCE_DIR}/.video_conversion_cache.txt"
STATS_FILE="${SOURCE_DIR}/.h265_stats.env"
PENDING_DELETE_LOG="${SOURCE_DIR}/.video_pending_deletes.txt"
SUSPECT_LIST="${SOURCE_DIR}/.video_verdaechtig.txt"
MIN_SIZE_BYTES=$(( MIN_SIZE_MB * 1024 * 1024 ))

# Remove leftovers from aborted runs before find picks them up
cleanup_stale_parts "${OUTPUT_DIR:-$SOURCE_DIR}" "*.part.*"

# Build the find expression from the extension list:
#   ( -iname "*.mp4" -o -iname "*.mkv" ... ) ! -name "*.part.*"
build_find_opts() {
    local ext first=true
    FIND_OPTS=(-type f \()
    for ext in $VIDEO_EXTENSIONS; do
        [[ "$first" == true ]] || FIND_OPTS+=(-o)
        FIND_OPTS+=(-iname "*.${ext}")
        first=false
    done
    FIND_OPTS+=(\) ! -name "*.part.*")
    [[ "$RECURSIVE" != true ]] && FIND_OPTS=(-maxdepth 1 "${FIND_OPTS[@]}")
    return 0   # otherwise the [[ ]] status falls through and set -e fires
}
build_find_opts

# target_extension <quelldatei> -> Endung des Zielcontainers
target_extension() {
    local src_ext="${1##*.}"; src_ext="${src_ext,,}"
    case "${VIDEO_CONTAINER,,}" in
        mp4)  printf 'mp4' ;;
        mkv)  printf 'mkv' ;;
        keep) printf '%s' "$src_ext" ;;
        *)    case "$src_ext" in
                  mp4|m4v|mov) printf 'mp4' ;;
                  *)           printf 'mkv' ;;
              esac ;;
    esac
}

# ------------------------------------------------------------------------------
# GPU-FAEHIGKEIT EINMALIG PRUEFEN
# ------------------------------------------------------------------------------
# Ein einzelnes Render-Node testen. Der ffmpeg-error landet in GPU_PROBE_ERR,
# so a failure does not stay silent.
# Exactly ONE definition of the GPU encoder arguments. Probe and real run
# must be identical, otherwise the check tests something other than
# what is actually executed later.
# gpu_encoder_args [<quell-pixelformat>]
#
# The pixel format is deliberately fixed and NOT written as an alternative
# ('format=nv12|p010'). With an alternative the filter negotiates
# the format itself; if it picks p010 while the encoder runs without
# -profile:v main10 runs in 8 bit, the result is a green picture with
# artefacts. 10 bit therefore only together with the matching profile.
# GPU_HW_ARGS   global options BEFORE the input (device initialisation)
# GPU_ENC_ARGS  filter and encoder AFTER the input
# GPU_TAG_ARGS  containerspezifische Tags
#
# Three paths, because hevc_vaapi can misbehave on some drivers:
# hevc_vaapi   classic path
# av1_vaapi    separate, newer code path in the driver
# hevc_vulkan  bypasses VA-API entirely via RADV
gpu_encoder_args() {
    local src_pix="${1:-}"
    GPU_HW_ARGS=(); GPU_ENC_ARGS=(); GPU_TAG_ARGS=()

    if [[ "$GPU_CODEC" == "hevc_vulkan" ]]; then
        GPU_HW_ARGS=(-init_hw_device "vulkan=vk:${VULKAN_DEVICE:-0}" -filter_hw_device vk)
        GPU_ENC_ARGS=(-vf hwupload -c:v hevc_vulkan -cq "$GPU_QP")
        [[ -n "$HEVC_TAG" ]] && GPU_TAG_ARGS=(-tag:v "$HEVC_TAG")
        return 0
    fi

    GPU_HW_ARGS=(-vaapi_device "$VAAPI_DEVICE")
    local vf='format=nv12,hwupload'
    local -a prof=()
    if [[ "$GPU_ALLOW_10BIT" == true && "$src_pix" == *10* ]]; then
        vf='format=p010,hwupload'
        [[ "$GPU_CODEC" == "hevc_vaapi" ]] && prof=(-profile:v main10)
    fi
    GPU_ENC_ARGS=(-vf "$vf" -c:v "$GPU_CODEC")

    case "${GPU_RC_MODE^^}" in
        CQP)  GPU_ENC_ARGS+=(-rc_mode CQP -global_quality "$GPU_QP") ;;
        VBR)  GPU_ENC_ARGS+=(-rc_mode VBR -b:v 0 -global_quality "$GPU_QP") ;;
        ICQ)  GPU_ENC_ARGS+=(-rc_mode ICQ -global_quality "$GPU_QP") ;;
        QVBR) GPU_ENC_ARGS+=(-rc_mode QVBR -b:v 0 -global_quality "$GPU_QP") ;;
        CBR)  GPU_ENC_ARGS+=(-rc_mode CBR -b:v "${GPU_BITRATE:-8M}") ;;
        AUTO) GPU_ENC_ARGS+=(-global_quality "$GPU_QP") ;;
        *) printf "%b[ERROR]%b Unbekannter GPU_RC_MODE: %s\n" "$C_RED" "$C_RESET" "$GPU_RC_MODE" >&2
           exit 2 ;;
    esac

    [[ -n "$GPU_BF" ]] && GPU_ENC_ARGS+=(-bf "$GPU_BF")
    [[ "$GPU_LOW_POWER" == true ]] && GPU_ENC_ARGS+=(-low_power 1)
    [[ -n "$GPU_ASYNC_DEPTH" ]] && GPU_ENC_ARGS+=(-async_depth "$GPU_ASYNC_DEPTH")
    GPU_ENC_ARGS+=("${prof[@]}")

    # Container tag only for HEVC and only on explicit request.
    #
    # CAUTION with "hvc1": the tag requires VPS/SPS/PPS to live only
    # in the hvcC box of the sample description. If the encoder emits them
    # in-band instead, the muxer builds an incomplete hvcC.
    # The file is then correctly encoded but no longer correctly decodable
    # (typically: fragments at the top, the rest green). Before setting it, check with
    # --gpu-selftest pruefen.
    [[ "$GPU_CODEC" == "hevc_vaapi" && -n "$HEVC_TAG" ]] && GPU_TAG_ARGS=(-tag:v "$HEVC_TAG")
    return 0
}
gpu_encoder_args

GPU_PROBE_ERR=""
gpu_probe_device() {
    local dev="$1"
    [[ -e "$dev" ]] || { GPU_PROBE_ERR="device does not exist"; return 1; }
    [[ -r "$dev" && -w "$dev" ]] || { GPU_PROBE_ERR="no read/write permission (group 'render'?)"; return 1; }
    local -a hw=("${GPU_HW_ARGS[@]}")
    [[ "$GPU_CODEC" != "hevc_vulkan" ]] && hw=(-vaapi_device "$dev")
    if GPU_PROBE_ERR=$(ffmpeg -nostdin -hide_banner -loglevel error \
            "${hw[@]}" -f lavfi -i "testsrc=s=1280x720:r=30" -frames:v 5 \
            "${GPU_ENC_ARGS[@]}" -an -f null - 2>&1); then
        GPU_PROBE_ERR=""
        return 0
    fi
    return 1
}

# Walk all render nodes. On systems with an iGPU and a dGPU, renderD128
# is often the iGPU; the dGPU then sits on renderD129.
detect_vaapi_device() {
    local -a candidates=() others=() d
    while IFS= read -r d; do
        [[ -e "$d" ]] || continue
        if [[ "$d" == "$VAAPI_DEVICE" ]]; then candidates=("$d" "${candidates[@]}"); else others+=("$d"); fi
    done < <(printf '%s\n' /dev/dri/renderD* 2>/dev/null)

    if [[ "${VAAPI_DEVICE,,}" != "auto" ]]; then
        candidates=("$VAAPI_DEVICE" "${others[@]}")
    else
        candidates=("${candidates[@]}" "${others[@]}")
    fi

    local first_err="" tried=0
    for d in "${candidates[@]}"; do
        [[ -n "$d" && -e "$d" ]] || continue
        tried=$(( tried + 1 ))
        if gpu_probe_device "$d"; then
            if [[ "$d" != "$VAAPI_DEVICE" && "${VAAPI_DEVICE,,}" != "auto" ]]; then
                printf "%b[GPU]%b %s provides no HEVC, but %b%s%b does. Using that one.\n" \
                    "$C_YELLOW" "$C_RESET" "$VAAPI_DEVICE" "$C_BOLD" "$d" "$C_RESET"
                printf "      Set permanently: VAAPI_DEVICE=\"%s\" in media-optimizer.conf\n" "$d"
            fi
            VAAPI_DEVICE="$d"
            return 0
        fi
        [[ -z "$first_err" ]] && first_err="$d: $GPU_PROBE_ERR"
    done
    (( tried == 0 )) && first_err="no render nodes found under /dev/dri/"
    GPU_PROBE_ERR="$first_err"
    return 1
}

gpu_diagnose() {
    local d
    printf "      %bDiagnostics:%b\n" "$C_BOLD" "$C_RESET" >&2
    printf "        ls -l /dev/dri/by-path/          # which node is the dGPU\n" >&2
    for d in /dev/dri/renderD*; do
        [[ -e "$d" ]] || continue
        printf "        vainfo --display drm --device %s | grep -i 'hevc.*enc'\n" "$d" >&2
    done
    printf "        id | grep -o render               # group membership\n" >&2
    printf "        ffmpeg -hide_banner -encoders | grep hevc_vaapi\n" >&2
    printf "      RDNA4 (VCN 5) needs Mesa 25.0+ and kernel 6.13+.\n" >&2
}

if [[ "$FORCE_GPU" == true ]]; then
    [[ "${VAAPI_DEVICE,,}" == "auto" ]] && VAAPI_DEVICE="/dev/dri/renderD128"
    gpu_encoder_args ""
    printf "%b[GPU]%b startup check skipped, using %s.\n" "$C_CYAN" "$C_RESET" "$VAAPI_DEVICE"
elif [[ "$ENCODER_MODE" =~ ^(auto|gpu)$ ]]; then
    if detect_vaapi_device; then
        # Important: only here is the concrete render node known. Without this
        # Neuaufbau bliebe "-vaapi_device auto" in GPU_HW_ARGS stehen.
        gpu_encoder_args ""
        printf "%b[GPU]%b %s available on %s.\n" "$C_GREEN" "$C_RESET" "$GPU_CODEC" "$VAAPI_DEVICE"
    elif [[ "$ENCODER_MODE" == "gpu" ]]; then
        printf "%b[ERROR]%b %s not usable, but --encoder gpu was requested explicitly.\n" \
            "$C_RED" "$C_RESET" "$GPU_CODEC" >&2
        printf "      reason: %s\n" "${GPU_PROBE_ERR:-unbekannt}" >&2
        gpu_diagnose
        exit 1
    else
        printf "%b[GPU]%b %s not usable -> falling back to CPU/libx265.\n" "$C_YELLOW" "$C_RESET" "$GPU_CODEC"
        printf "      reason: %s\n" "${GPU_PROBE_ERR:-unbekannt}" >&2
        gpu_diagnose
        ENCODER_MODE="cpu"
    fi
fi


# ------------------------------------------------------------------------------
# GPU SELF-TEST
# Tries option combinations on a real slice and checks
# each result by picture comparison. Only that reveals on the given
# Hardware feststellen, welche Einstellung wirklich usablee Bilder liefert -
# an encoder producing green areas without any error message is, from
# outside, indistinguishable from success.
# ------------------------------------------------------------------------------
run_gpu_selftest() {
    local src="$1"
    [[ -f "$src" ]] || { echo "Test file not found: $src" >&2; exit 1; }

    if [[ "${VAAPI_DEVICE,,}" == "auto" ]]; then
        local d
        for d in /dev/dri/renderD*; do [[ -e "$d" ]] && { VAAPI_DEVICE="$d"; break; }; done
        [[ "${VAAPI_DEVICE,,}" == "auto" ]] && VAAPI_DEVICE="/dev/dri/renderD128"
    fi

    local dur; dur=$(media_duration "$src")
    local seclen=6
    local start=0
    if [[ -n "$dur" && "$dur" != "N/A" ]]; then
        start=$(awk -v d="$dur" -v l="$seclen" 'BEGIN { s = (d - l) / 2; printf "%d", (s > 0 ? s : 0) }')
    fi

    printf "%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
    printf "%b║%b                        %bGPU SELF-TEST%b                         %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
    printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_BLUE" "$C_RESET"
    printf "  File:   %s\n  Device: %s\n  Slice:  %ss at %ss\n\n" \
        "$(basename "$src")" "$VAAPI_DEVICE" "$seclen" "$start"

    # Referenzausschnitt, unveraendert kopiert
    local ref; ref=$(mktemp --suffix=.mp4 /tmp/mo_st_ref_XXXXXX)
    if ! ffmpeg -nostdin -y -v error -ss "$start" -t "$seclen" -i "$src" \
         -map 0:v:0 -an -c copy "$ref" </dev/null 2>/dev/null; then
        echo "Could not create the reference slice." >&2
        rm -f "$ref"; exit 1
    fi

    # Kombinationen: rc_mode : bf : low_power
    # Each variant is a complete ffmpeg call. "original" is the minimal
    # minimal combination without extra options; the following variants add
    # exactly ONE option each. If only one of them fails, the
    # trigger is named unambiguously. New suspect options belong here as
    # a variant of their own, not in a combined line.
    local enc_list; enc_list=$(ffmpeg -hide_banner -encoders 2>/dev/null || true)
    local -a variants=(
        "original|original command, device after -i"
        "geraet-vorn|like original, device before -i"
        "bf0|like original + -bf 0"
        "hvc1|like original + -tag:v hvc1"
        "faststart|like original + faststart"
        "aktuell|current script defaults"
        "av1|av1_vaapi instead of hevc_vaapi"
        "vulkan|hevc_vulkan via RADV"
    )

    st_build() {   # $1 = variante, $2 = ausgabedatei
        local v="$1" out="$2"
        local -a base=(-vf 'format=nv12,hwupload' -c:v hevc_vaapi
                       -rc_mode CQP -global_quality "$GPU_QP")
        ST_CMD=(ffmpeg -nostdin -y -hide_banner -loglevel error)
        case "$v" in
            original)
                ST_CMD+=(-reinit_filter 0 -ss "$start" -t "$seclen" -i "$src"
                         -vaapi_device "$VAAPI_DEVICE" "${base[@]}") ;;
            geraet-vorn)
                ST_CMD+=(-vaapi_device "$VAAPI_DEVICE" -reinit_filter 0
                         -ss "$start" -t "$seclen" -i "$src" "${base[@]}") ;;
            bf0)
                ST_CMD+=(-reinit_filter 0 -ss "$start" -t "$seclen" -i "$src"
                         -vaapi_device "$VAAPI_DEVICE" "${base[@]}" -bf 0) ;;
            hvc1)
                ST_CMD+=(-reinit_filter 0 -ss "$start" -t "$seclen" -i "$src"
                         -vaapi_device "$VAAPI_DEVICE" "${base[@]}" -tag:v hvc1) ;;
            faststart)
                ST_CMD+=(-reinit_filter 0 -ss "$start" -t "$seclen" -i "$src"
                         -vaapi_device "$VAAPI_DEVICE" "${base[@]}" -movflags +faststart) ;;
            aktuell)
                GPU_CODEC=hevc_vaapi; gpu_encoder_args ""
                ST_CMD+=("${GPU_HW_ARGS[@]}" -reinit_filter 0 -ss "$start" -t "$seclen"
                         -i "$src" "${GPU_ENC_ARGS[@]}" "${GPU_TAG_ARGS[@]}") ;;
            av1)
                GPU_CODEC=av1_vaapi; gpu_encoder_args ""
                ST_CMD+=("${GPU_HW_ARGS[@]}" -reinit_filter 0 -ss "$start" -t "$seclen"
                         -i "$src" "${GPU_ENC_ARGS[@]}" "${GPU_TAG_ARGS[@]}") ;;
            vulkan)
                GPU_CODEC=hevc_vulkan; gpu_encoder_args ""
                ST_CMD+=("${GPU_HW_ARGS[@]}" -ss "$start" -t "$seclen"
                         -i "$src" "${GPU_ENC_ARGS[@]}" "${GPU_TAG_ARGS[@]}") ;;
        esac
        ST_CMD+=(-an -map 0:v:0 "$out")
    }

    local -a winners=()
    local entry v label out res psnr needed

    printf "  %-13s %-34s %-10s %s\n" "Variant" "Description" "Result" "PSNR"
    printf "  %s\n" "---------------------------------------------------------------------------"

    for entry in "${variants[@]}"; do
        v="${entry%%|*}"; label="${entry#*|}"
        needed="hevc_vaapi"
        [[ "$v" == av1 ]] && needed="av1_vaapi"
        [[ "$v" == vulkan ]] && needed="hevc_vulkan"
        if [[ "$enc_list" != *" $needed "* ]]; then
            printf "  %-13s %-34s %-10s %s\n" "$v" "$label" "n/a" "encoder missing"
            continue
        fi

        out=$(mktemp --suffix=.mp4 /tmp/mo_st_out_XXXXXX)
        st_build "$v" "$out"
        if "${ST_CMD[@]}" </dev/null 2>/dev/null && [[ -s "$out" ]]; then
            if verify_visual "$ref" "$out" "$VISUAL_PSNR_MIN"; then
                res="usable"; psnr="${VISUAL_PSNR} dB"; winners+=("$v")
            else
                res="BROKEN"; psnr="${VISUAL_PSNR:-?} dB"
            fi
        else
            res="error"; psnr="-"
        fi
        printf "  %-13s %-34s %-10s %s\n" "$v" "$label" "$res" "$psnr"
        rm -f "$out"
    done
    rm -f "$ref"

    echo ""
    if (( ${#winners[@]} == 0 )); then
        printf "%b[RESULT]%b No combination produced a usable picture.\n" "$C_RED" "$C_RESET"
        printf "           On Fedora check that the full VA drivers are active:\n"
        printf "             rpm -q mesa-va-drivers-freeworld\n"
        printf "             vainfo --display drm --device %s | grep -i 'hevc.*enc'\n" "$VAAPI_DEVICE"
        printf "           Until then: --encoder cpu\n"
        exit 1
    fi

    printf "%b[RESULT]%b Usable: %s\n\n" "$C_GREEN" "$C_RESET" "${winners[*]}"
    case "${winners[0]}" in
        original|geraet-vorn|bf0|hvc1|faststart)
            printf "  hevc_vaapi works in principle. The current script defaults\n"
            printf "  are the problem, not the driver.\n" ;;
        aktuell)
            printf "  The current defaults work, nothing to change.\n" ;;
        av1)
            printf "  For media-optimizer.conf:\n\n    GPU_CODEC=\"av1_vaapi\"\n\n"
            printf "  AV1 saves more space than HEVC but is not played by older\n"
            printf "  players and TVs.\n" ;;
        vulkan)
            printf "  For media-optimizer.conf:\n\n    GPU_CODEC=\"hevc_vulkan\"\n" ;;
    esac
    echo ""
    exit 0
}

[[ -n "$GPU_SELFTEST" ]] && run_gpu_selftest "$GPU_SELFTEST"

# ------------------------------------------------------------------------------
# PERSISTENTE STATISTIKEN
# ------------------------------------------------------------------------------
if [[ "${RESUMING:-false}" == "true" && -f "$STATS_FILE" ]]; then
    source "$STATS_FILE" 2>/dev/null || true
else
    rm -f "$STATS_FILE"
fi

COUNT_PROCESSED=${COUNT_PROCESSED:-0}; COUNT_SALVAGED=${COUNT_SALVAGED:-0}
COUNT_SKIPPED=${COUNT_SKIPPED:-0}
COUNT_PROBE_SKIPPED=${COUNT_PROBE_SKIPPED:-0}; COUNT_DISCARDED=${COUNT_DISCARDED:-0}
# Cache hits describe the current scan, not the work performed.
# Every resume reads the same files again, so carrying the value
# across runs would keep adding up.
COUNT_CACHE_SKIPPED=0
COUNT_FAILED=${COUNT_FAILED:-0}; COUNT_GPU=${COUNT_GPU:-0}; COUNT_CPU=${COUNT_CPU:-0}
COUNT_UNVERIFIED=${COUNT_UNVERIFIED:-0}; COUNT_DRY=${COUNT_DRY:-0}
COUNT_SUSPECT=${COUNT_SUSPECT:-0}
TOTAL_ORIG_BYTES=${TOTAL_ORIG_BYTES:-0}; TOTAL_NEW_BYTES=${TOTAL_NEW_BYTES:-0}
PREV_ELAPSED=${PREV_ELAPSED:-0}
START_TIME=$(date +%s)

save_stats() {
    local tot_elapsed=$(( PREV_ELAPSED + $(date +%s) - START_TIME ))
    cat > "$STATS_FILE" <<EOF
COUNT_PROCESSED=$COUNT_PROCESSED
COUNT_SALVAGED=$COUNT_SALVAGED
COUNT_SKIPPED=$COUNT_SKIPPED
COUNT_PROBE_SKIPPED=$COUNT_PROBE_SKIPPED
COUNT_DISCARDED=$COUNT_DISCARDED
COUNT_FAILED=$COUNT_FAILED
COUNT_UNVERIFIED=$COUNT_UNVERIFIED
COUNT_SUSPECT=$COUNT_SUSPECT
COUNT_GPU=$COUNT_GPU
COUNT_CPU=$COUNT_CPU
TOTAL_ORIG_BYTES=$TOTAL_ORIG_BYTES
TOTAL_NEW_BYTES=$TOTAL_NEW_BYTES
PREV_ELAPSED=$tot_elapsed
EOF
}

on_interrupt() {
    printf "\n%b[ABORT]%b video encoding stopped.\n" "$C_RED" "$C_RESET"
    [[ -n "${temp_file:-}" ]] && rm -f "$temp_file"
    [[ -n "${err_log:-}" ]] && rm -f "$err_log"
    save_stats
    resolve_pending_deletes
    exit 130
}
trap on_interrupt SIGINT SIGTERM

# ------------------------------------------------------------------------------
# CACHE
# The cache is the only resume source. A file is only added once it
# has been fully processed (or deliberately skipped).
# ------------------------------------------------------------------------------
declare -A CACHE_MAP=()
CACHE_PRUNED=0
if [[ "$USE_CACHE" == true && -f "$CACHE_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        # Entries without a file are dead: find can never return them.
        if [[ -e "$line" ]]; then
            CACHE_MAP["$line"]=1
        else
            CACHE_PRUNED=$(( CACHE_PRUNED + 1 ))
        fi
    done < "$CACHE_FILE"

    # Rewrite the file once so it does not grow without bound
    if (( CACHE_PRUNED > 0 )) && [[ "$DRY_RUN" != true ]]; then
        printf '%s\n' "${!CACHE_MAP[@]}" > "$CACHE_FILE" 2>/dev/null || true
        printf "%b[CACHE]%b %d stale entries removed.\n" \
            "$C_CYAN" "$C_RESET" "$CACHE_PRUNED"
    fi
fi

add_to_cache() {
    local target="$1"
    [[ "$DRY_RUN" == true ]] && return 0
    if [[ "$USE_CACHE" == true && "${CACHE_MAP["$target"]:-0}" -eq 0 ]]; then
        CACHE_MAP["$target"]=1
        echo "$target" >> "$CACHE_FILE"
    fi
}

# Only record target files that live inside the scanned tree. In
# in-place mode the next run finds "name_h265.mp4" again and saves
# one ffprobe through that entry. With a separate target folder find
# never looks there, so the entry would only be ballast.
add_dest_to_cache() {
    local dest="$1"
    [[ -z "$OUTPUT_DIR" ]] || return 0
    add_to_cache "$dest"
}

# slice_kbps <datei> <startsekunde> -> Bitrate eines Video-only-Ausschnitts
# Both comparison values (original and probe) are measured identically:
# same start, same length, without audio, measured by file size.
slice_kbps() {
    local file="$1" start="$2"
    local tmp; tmp=$(mktemp --suffix=.mkv /tmp/mo_slice_XXXXXX) || return 1
    if ffmpeg -nostdin -y -hide_banner -loglevel error \
        -ss "$start" -t "$PROBE_DURATION" -i "$file" -map 0:v:0 -an -c copy "$tmp" </dev/null 2>/dev/null; then
        local sz; sz=$(stat -c%s "$tmp" 2>/dev/null || echo 0)
        rm -f "$tmp"
        echo $(( sz * 8 / PROBE_DURATION / 1000 ))
        return 0
    fi
    rm -f "$tmp"
    echo 0
    return 1
}

# ------------------------------------------------------------------------------
# ZU VERARBEITENDE DATEIEN
# Either the whole tree or exactly the lines of a list.
# ------------------------------------------------------------------------------
emit_targets() {
    if [[ -z "$FROM_LIST" ]]; then
        find "$SOURCE_DIR" "${FIND_OPTS[@]}" -print0
        return 0
    fi
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        if [[ -f "$line" ]]; then
            printf '%s\0' "$line"
        else
            printf "%b[LIST]%b skipped, file missing: %s\n" \
                "$C_YELLOW" "$C_RESET" "$line" >&2
        fi
    done < "$FROM_LIST"
}

# ------------------------------------------------------------------------------
# HAUPTSCHLEIFE
# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------
# FORTSCHRITT
# The total count costs one extra pass over the candidates.
# With find that is a directory walk without ffprobe, so it is cheap.
# ------------------------------------------------------------------------------
if [[ -n "$FROM_LIST" ]]; then
    TOTAL_TARGETS=$(grep -cvE '^\s*(#|$)' "$FROM_LIST" 2>/dev/null || echo 0)
else
    TOTAL_TARGETS=$(find "$SOURCE_DIR" "${FIND_OPTS[@]}" -print0 2>/dev/null | tr -dc '\0' | wc -c || echo 0)
fi
IDX=0
PROGRESS_OPEN=false

# Single-line progress for files that pass through without output
# (cache, too small, wrong codec). Only on a terminal so log files stay
# bleiben.
progress_tick() {
    [[ -t 1 && -z "${NO_COLOR:-}" ]] || return 0
    printf "\r  [%d/%d] checked, %d skipped\033[K" \
        "$IDX" "$TOTAL_TARGETS" "$(( COUNT_CACHE_SKIPPED + COUNT_SKIPPED + COUNT_PROBE_SKIPPED ))"
    PROGRESS_OPEN=true
}
progress_clear() {
    [[ "$PROGRESS_OPEN" == true ]] || return 0
    printf "\r\033[K"
    PROGRESS_OPEN=false
}

printf "\n%b%d candidate(s) found.%b\n" "$C_BOLD" "$TOTAL_TARGETS" "$C_RESET"

while IFS= read -r -d '' -u 9 src_file; do
    IDX=$(( IDX + 1 ))

    if [[ "$USE_CACHE" == true && "${CACHE_MAP["$src_file"]:-0}" -eq 1 ]]; then
        COUNT_CACHE_SKIPPED=$(( COUNT_CACHE_SKIPPED + 1 ))
        progress_tick
        continue
    fi

    src_size=$(stat -c%s "$src_file" 2>/dev/null || echo 0)
    if (( src_size < MIN_SIZE_BYTES )); then
        COUNT_SKIPPED=$(( COUNT_SKIPPED + 1 ))
        add_to_cache "$src_file"; progress_tick; continue
    fi

    current_codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
        -of default=noprint_wrappers=1:nokey=1 "$src_file" 2>/dev/null || echo unknown)
    src_pix_fmt=$(ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt \
        -of default=noprint_wrappers=1:nokey=1 "$src_file" 2>/dev/null || echo "")
    if [[ " $SOURCE_CODECS " != *" $current_codec "* ]]; then
        COUNT_SKIPPED=$(( COUNT_SKIPPED + 1 ))
        add_to_cache "$src_file"; progress_tick; continue
    fi

    filename=$(basename "$src_file")
    stem="${filename%.*}"

    if [[ -n "$OUTPUT_DIR" ]]; then
        rel_dir="$(dirname "${src_file#"$SOURCE_DIR"/}")"
        [[ "$rel_dir" == "." ]] && target_folder="$OUTPUT_DIR" || target_folder="$OUTPUT_DIR/$rel_dir"
        [[ "$DRY_RUN" == true ]] || mkdir -p "$target_folder"
        dest_file="$target_folder/${stem}.$(target_extension "$src_file")"
    else
        dest_file="$(dirname "$src_file")/${stem}_h265.$(target_extension "$src_file")"
    fi

    # Existing output? That also covers an AVIF file for the same
    # source: if the optional AVIF stage ran first, the clip is already
    # converted there and should not also be produced as HEVC.
    # The AVIF name derives from the SOURCE name, not from dest_file: in
    # in-place mode dest_file carries the _h265 suffix.
    dest_avif="$(dirname "$dest_file")/${stem}.avif"
    if [[ "$SKIP_EXISTING" == true ]] && { [[ -s "$dest_file" ]] || [[ -s "$dest_avif" ]]; }; then
        COUNT_SKIPPED=$(( COUNT_SKIPPED + 1 ))
        add_to_cache "$src_file"; add_dest_to_cache "$dest_file"; progress_tick; continue
    fi

    # ---------- Encoder-Wahl ----------
    bitrate_raw=$(ffprobe -v error -select_streams v:0 -show_entries stream=bit_rate \
        -of default=noprint_wrappers=1:nokey=1 "$src_file" 2>/dev/null || true)
    if [[ -z "$bitrate_raw" || "$bitrate_raw" == "N/A" ]]; then
        bitrate_raw=$(ffprobe -v error -show_entries format=bit_rate \
            -of default=noprint_wrappers=1:nokey=1 "$src_file" 2>/dev/null || echo 0)
    fi
    bitrate_kbps=0
    [[ "$bitrate_raw" =~ ^[0-9]+(\.[0-9]+)?$ ]] && bitrate_kbps=$(( ${bitrate_raw%.*} / 1000 ))

    active_encoder="$ENCODER_MODE"
    if [[ "$ENCODER_MODE" == "auto" ]]; then
        if (( bitrate_kbps > 0 && bitrate_kbps < BITRATE_THRESHOLD_KBPS )); then
            active_encoder="cpu"
        else
            active_encoder="gpu"
        fi
    fi

    # Build the encoder arguments for this file BEFORE the probe runs.
    # The probe uses the same arguments; built afterwards, the probe would
    # run with stale values.
    gpu_encoder_args "$src_pix_fmt"

    if [[ "$DRY_RUN" == true ]]; then
        progress_clear
        printf "%b[DRY-RUN]%b '%s' (%s, %s kb/s) -> '%s'\n" \
            "$C_CYAN" "$C_RESET" "$filename" "${active_encoder^^}" "$bitrate_kbps" "$(basename "$dest_file")"
        COUNT_DRY=$(( COUNT_DRY + 1 ))
        continue
    fi

    # ---------- Probe ----------
    if [[ "$ENABLE_PROBE" == true ]]; then
        duration_raw=$(media_duration "$src_file")
        duration_sec="${duration_raw%.*}"; duration_sec="${duration_sec:-0}"

        if [[ "$duration_sec" =~ ^[0-9]+$ ]] && (( duration_sec >= PROBE_MIN_DURATION )); then
            start_sec=$(( (duration_sec - PROBE_DURATION) / 2 ))
            probe_file=$(mktemp --suffix=.mp4 /tmp/mo_probe_XXXXXX)
            PROBE_CMD=(ffmpeg -nostdin -y -hide_banner -loglevel error)
            [[ "$active_encoder" == "gpu" ]] && PROBE_CMD+=("${GPU_HW_ARGS[@]}")
            PROBE_CMD+=(-reinit_filter 0 -ss "$start_sec" -t "$PROBE_DURATION" -i "$src_file")
            if [[ "$active_encoder" == "gpu" ]]; then
                PROBE_CMD+=("${GPU_ENC_ARGS[@]}")
            else
                PROBE_CMD+=(-c:v libx265 -crf "$CPU_CRF" -preset "$CPU_PRESET")
                [[ -n "$CPU_X265_PARAMS" ]] && PROBE_CMD+=(-x265-params "$CPU_X265_PARAMS")
            fi
            PROBE_CMD+=(-an -map 0:v:0 "$probe_file")

            if "${PROBE_CMD[@]}" </dev/null; then
                probe_size=$(stat -c%s "$probe_file" 2>/dev/null || echo 0)
                probe_kbps=$(( probe_size * 8 / PROBE_DURATION / 1000 ))
                rm -f "$probe_file"

                # Referenz identisch messen: gleicher Ausschnitt, video-only
                ref_kbps=$(slice_kbps "$src_file" "$start_sec" || echo 0)

                if (( ref_kbps > 0 && probe_kbps * 100 >= ref_kbps * PROBE_MARGIN_PCT )); then
                    probe_delta=$(pct_change "$ref_kbps" "$probe_kbps")
                    if (( probe_kbps > ref_kbps )); then
                        probe_verdict="would be ${probe_delta#-}% LARGER"
                    else
                        probe_verdict="only ${probe_delta#-}% smaller, threshold ${gain_needed}%"
                    fi
                    progress_clear
                    printf "%b[SKIPPED]%b %s (probe %s vs original %s kb/s: %s)\n" \
                        "$C_YELLOW" "$C_RESET" "$filename" "$probe_kbps" "$ref_kbps" "$probe_verdict"
                    mo_log "h265" "PROBE     $src_file ($probe_verdict)"
                    COUNT_PROBE_SKIPPED=$(( COUNT_PROBE_SKIPPED + 1 ))
                    add_to_cache "$src_file"
                    continue
                fi
            else
                rm -f "$probe_file"
            fi
        fi
    fi

    # ---------- Encoding ----------
    progress_clear
    printf "\n%b>>> [%d/%d] Processing:%b %s (%s)\n" \
        "$C_BOLD" "$IDX" "$TOTAL_TARGETS" "$C_RESET" "$src_file" "${active_encoder^^}"
    temp_file="${dest_file}.part.$$.${RANDOM}.${dest_file##*.}"
    err_log=$(mktemp /tmp/mo_enc_err_XXXXXX)

    build_ffmpeg_cmd() {   # $1 = gpu | cpu
        FFMPEG_CMD=(ffmpeg -nostdin -y -hide_banner -loglevel warning -stats)
        [[ "$1" == "gpu" ]] && FFMPEG_CMD+=("${GPU_HW_ARGS[@]}")
        FFMPEG_CMD+=(-reinit_filter 0 -i "$src_file")
        if [[ "$1" == "gpu" ]]; then
            FFMPEG_CMD+=("${GPU_ENC_ARGS[@]}")
        else
            FFMPEG_CMD+=(-c:v libx265 -crf "$CPU_CRF" -preset "$CPU_PRESET")
            [[ -n "$CPU_X265_PARAMS" ]] && FFMPEG_CMD+=(-x265-params "$CPU_X265_PARAMS")
        fi
        if [[ "$1" == "gpu" ]]; then
            FFMPEG_CMD+=("${GPU_TAG_ARGS[@]}")
        elif [[ -n "$HEVC_TAG" ]]; then
            FFMPEG_CMD+=(-tag:v "$HEVC_TAG")
        fi
        if [[ "${STREAM_FALLBACK:-false}" == true ]]; then
            FFMPEG_CMD+=(-map 0:v:0 -map 0:a? -map_metadata 0 -c:a aac -b:a 192k -sn)
        else
            FFMPEG_CMD+=(-map 0:v:0 -map 0:a? -map 0:s? -map_metadata 0 -c:a copy -c:s copy)
        fi
        [[ "$FASTSTART" == true ]] && FFMPEG_CMD+=(-movflags +faststart)
        FFMPEG_CMD+=("$temp_file")
    }

    enc_ok=true
    build_ffmpeg_cmd "$active_encoder"
    "${FFMPEG_CMD[@]}" </dev/null 2>&1 | tee "$err_log" || enc_ok=false

    # If the GPU fails on a real file, that file is immediately retried on
    # CPU and the rest of the run stays on CPU as well.
    # A false negative or false positive startup check can therefore no
    # longer derail the run.
    # If copying the audio or subtitle stream fails in the target container,
    # retry once without subtitles and with AAC audio. Mainly affects
    # MKV/WebM sources with Opus or SRT that MP4 refuses.
    if [[ "$enc_ok" == false ]] && grep -qiE 'not currently supported in container|could not write header|codec frame size is not set' "$err_log" 2>/dev/null; then
        printf "%b[CONTAINER]%b '%s': stream cannot be copied, retrying with AAC and without subtitles.\n" \
            "$C_YELLOW" "$C_RESET" "$filename" >&2
        rm -f "$temp_file"
        enc_ok=true
        STREAM_FALLBACK=true
        build_ffmpeg_cmd "$active_encoder"
        "${FFMPEG_CMD[@]}" </dev/null 2>&1 | tee "$err_log" || enc_ok=false
        STREAM_FALLBACK=false
    fi

    if [[ "$enc_ok" == false && "$active_encoder" == "gpu" ]]; then
        printf "%b[GPU]%b encoding of '%s' failed. Last messages:\n" \
            "$C_YELLOW" "$C_RESET" "$filename" >&2
        tail -n 3 "$err_log" | sed 's/^/        /' >&2
        printf "%b[GPU]%b retrying on CPU and staying there for the rest of the run.\n" \
            "$C_YELLOW" "$C_RESET" >&2
        rm -f "$temp_file"
        active_encoder="cpu"
        ENCODER_MODE="cpu"
        GPU_FELL_BACK=true
        enc_ok=true
        build_ffmpeg_cmd cpu
        "${FFMPEG_CMD[@]}" </dev/null 2>&1 | tee "$err_log" || enc_ok=false
    fi

    if [[ "$enc_ok" == true ]]; then
        dest_size=$(stat -c%s "$temp_file" 2>/dev/null || echo 0)
        diff_bytes=$(( src_size - dest_size ))

        is_salvaged=false
        grep -Eqi "corrupt|partial file|decoding error|invalid nal unit|error splitting" \
            "$err_log" 2>/dev/null && is_salvaged=true
        rm -f "$err_log"; err_log=""

        # ---------- verification before any deletion decision ----------
        # For salvaged files the runtime legitimately differs; there only
        # decodability is checked and the original is never deleted.
        verify_tol="$DURATION_TOLERANCE_PCT"
        [[ "$is_salvaged" == true ]] && verify_tol=100

        if ! verify_video "$src_file" "$temp_file" "$verify_tol"; then
            printf "%b[INVALID]%b '%s': output incomplete or unreadable. Discarded, original kept.\n" \
                "$C_RED" "$C_RESET" "$filename" >&2
            rm -f "$temp_file"
            mo_log_file "h265" "INVALID" "$src_file"
            COUNT_UNVERIFIED=$(( COUNT_UNVERIFIED + 1 ))
            save_stats
            continue
        fi

        # Check the picture content. The runtime is also correct for broken
        # Farbformaten, deshalb ein Stichprobenvergleich per PSNR.
        if [[ "$VERIFY_VISUAL" == true && "$is_salvaged" != true ]]; then
            if ! verify_visual "$src_file" "$temp_file" "$VISUAL_PSNR_MIN"; then
                printf "%b[PICTURE?]%b '%s': PSNR %s dB below %s dB (samples: %s)\n" \
                    "$C_YELLOW" "$C_RESET" "$filename" "$VISUAL_PSNR" "$VISUAL_PSNR_MIN" \
                    "${VISUAL_PSNR_ALL:-none}" >&2
                mo_log_file "h265" "PICTURE?" "$src_file" "" "" ""
                COUNT_SUSPECT=$(( COUNT_SUSPECT + 1 ))
                printf '%s\n' "$src_file" >> "$SUSPECT_LIST" 2>/dev/null || true

                # Warn only by default. The measurement can be wrong,
                # so a human decides, not the script.
                if [[ "$VISUAL_STRICT" == true ]]; then
                    printf "            --strict-visual: discarded, original kept.\n" >&2
                    rm -f "$temp_file"
                    mo_log_file "h265" "INVALID" "$src_file"
            COUNT_UNVERIFIED=$(( COUNT_UNVERIFIED + 1 ))
                    save_stats
                    continue
                fi
                printf "            File is kept. Check with scripts/verify-output.sh\n" >&2
            fi
        fi

        if (( diff_bytes <= 0 )); then
            inc_pct=$(pct_change "$src_size" "$dest_size")
            inc_h=$(format_bytes $(( dest_size - src_size )))
            src_h=$(format_bytes "$src_size"); dest_h=$(format_bytes "$dest_size")

            if [[ "$is_salvaged" == true && "$KEEP_SALVAGED_CORRUPT" == true ]]; then
                mv "$temp_file" "$dest_file"; touch -r "$src_file" "$dest_file"
                add_to_cache "$src_file"; add_dest_to_cache "$dest_file"
                printf "%b┌──────────────────────────────────────────────────────────────┐%b\n" "$C_MAGENTA" "$C_RESET"
                printf "%b│%b  %bFILE SALVAGED / REPAIRED:%b %s\n" "$C_MAGENTA" "$C_RESET" "$C_BOLD" "$C_RESET" "$filename"
                printf "%b│%b  Original: %s (was damaged)\n" "$C_MAGENTA" "$C_RESET" "$src_h"
                printf "%b│%b  New:      %s (+%s%%, +%s)\n" "$C_MAGENTA" "$C_RESET" "$dest_h" "$inc_pct" "$inc_h"
                printf "%b│%b  Status:   Kept. Original is NOT deleted.\n" "$C_MAGENTA" "$C_RESET"
                printf "%b└──────────────────────────────────────────────────────────────┘%b\n" "$C_MAGENTA" "$C_RESET"
                COUNT_PROCESSED=$(( COUNT_PROCESSED + 1 ))
                mo_log_file "h265" "SALVAGED" "$src_file" "$(basename "$dest_file")" "$src_size" "$dest_size"
                COUNT_SALVAGED=$(( COUNT_SALVAGED + 1 ))
                [[ "$active_encoder" == "gpu" ]] && COUNT_GPU=$(( COUNT_GPU + 1 )) || COUNT_CPU=$(( COUNT_CPU + 1 ))
                TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + src_size ))
                TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + dest_size ))
                save_stats
                continue
            elif [[ "$DISCARD_IF_LARGER" == true ]]; then
                rm -f "$temp_file"; add_to_cache "$src_file"
                printf "%b┌──────────────────────────────────────────────────────────────┐%b\n" "$C_YELLOW" "$C_RESET"
                printf "%b│%b  %bFILE DISCARDED:%b %s\n" "$C_YELLOW" "$C_RESET" "$C_BOLD" "$C_RESET" "$filename"
                printf "%b│%b  Original: %s -> new: %s\n" "$C_YELLOW" "$C_RESET" "$src_h" "$dest_h"
                printf "%b│%b  Status:   Discarded, larger by (+%s%%, +%s).\n" "$C_YELLOW" "$C_RESET" "$inc_pct" "$inc_h"
                printf "%b└──────────────────────────────────────────────────────────────┘%b\n" "$C_YELLOW" "$C_RESET"
                mo_log_file "h265" "DISCARDED" "$src_file" "" "$src_size" "$dest_size"
                COUNT_DISCARDED=$(( COUNT_DISCARDED + 1 ))
                save_stats
                continue
            fi
        fi

        mv "$temp_file" "$dest_file"; touch -r "$src_file" "$dest_file"

        COUNT_PROCESSED=$(( COUNT_PROCESSED + 1 ))
        [[ "$active_encoder" == "gpu" ]] && COUNT_GPU=$(( COUNT_GPU + 1 )) || COUNT_CPU=$(( COUNT_CPU + 1 ))
        TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + src_size ))
        TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + dest_size ))
        mo_log_file "h265" "OK" "$src_file" "$(basename "$dest_file")" "$src_size" "$dest_size"

        saved_pct=$(pct_change "$src_size" "$dest_size")
        printf "%b┌──────────────────────────────────────────────────────────────┐%b\n" "$C_GREEN" "$C_RESET"
        printf "%b│%b  %bFILE:%b    %s\n" "$C_GREEN" "$C_RESET" "$C_BOLD" "$C_RESET" "$filename"
        printf "%b│%b  Original: %s -> new: %s\n" "$C_GREEN" "$C_RESET" "$(format_bytes "$src_size")" "$(format_bytes "$dest_size")"
        printf "%b│%b  Saved: %b %s%% %b %b%b(-%s)%b\n" "$C_GREEN" "$C_RESET" \
            "$C_BG_GREEN" "$saved_pct" "$C_RESET" "$C_GREEN" "$C_BOLD" "$(format_bytes "$diff_bytes")" "$C_RESET"
        printf "%b└──────────────────────────────────────────────────────────────┘%b\n" "$C_GREEN" "$C_RESET"

        [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src_file"

        # Optional rename in place: "holiday_h265.mp4" becomes
        # "holiday.mp4" again. Only when the original is really gone,
        # otherwise a still existing source file would be overwritten.
        # Rename only when the target container has the same extension as the
        # source. Otherwise the file would carry a misleading extension, e.g. an
        # MKV file named "video.avi".
        if [[ "$RENAME_INPLACE" == true && -z "$OUTPUT_DIR" \
              && "${dest_file##*.}" != "${src_file##*.}" ]]; then
            printf "%b[RENAME]%b skipped: target container .%s does not match .%s\n" \
                "$C_CYAN" "$C_RESET" "${dest_file##*.}" "${src_file##*.}"
        elif [[ "$RENAME_INPLACE" == true && -z "$OUTPUT_DIR" ]]; then
            if [[ -e "$src_file" ]]; then
                printf "%b[RENAME]%b skipped: '%s' still exists.\n" \
                    "$C_YELLOW" "$C_RESET" "$filename" >&2
            elif mv -n "$dest_file" "$src_file" 2>/dev/null && [[ ! -e "$dest_file" ]]; then
                printf "%b[RENAME]%b '%s' -> '%s'\n" \
                    "$C_CYAN" "$C_RESET" "$(basename "$dest_file")" "$filename"
                dest_file="$src_file"
            else
                printf "%b[RENAME]%b failed, '%s' kept.\n" \
                    "$C_YELLOW" "$C_RESET" "$(basename "$dest_file")" >&2
            fi
        fi

        add_to_cache "$src_file"; add_dest_to_cache "$dest_file"
        save_stats
    else
        printf "%b[ERROR]%b '%s'\n" "$C_RED" "$C_RESET" "$src_file" >&2
        rm -f "$temp_file"; [[ -n "$err_log" ]] && rm -f "$err_log"
        mo_log_file "h265" "ERROR" "$src_file"
        COUNT_FAILED=$(( COUNT_FAILED + 1 ))
        save_stats
    fi
    temp_file=""; err_log=""

done 9< <(emit_targets)
progress_clear

# ------------------------------------------------------------------------------
# ABSCHLUSS
# ------------------------------------------------------------------------------
if [[ "$DRY_RUN" == true ]]; then
    printf "\n%b[DRY-RUN]%b %s video(s) wuerden reencodiert.\n" "$C_CYAN" "$C_RESET" "$COUNT_DRY"
    exit 0
fi

TOTAL_ELAPSED=$(( PREV_ELAPSED + $(date +%s) - START_TIME ))
TOTAL_SAVED=$(( TOTAL_ORIG_BYTES - TOTAL_NEW_BYTES ))

printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b                           %bSUMMARY%b                            %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
printf "%b╠══════════════════════════════════════════════════════════════╣%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Total runtime:" "$(format_duration "$TOTAL_ELAPSED")" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Re-encoded:" "$COUNT_PROCESSED file(s) (GPU: $COUNT_GPU | CPU: $COUNT_CPU)" "$C_BLUE" "$C_RESET"
(( COUNT_SALVAGED > 0 )) && \
printf "%b║%b  %bSalvaged (repaired):%b  %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "$C_MAGENTA" "$C_RESET" "$COUNT_SALVAGED file(s)" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Cache (this scan):" "$COUNT_CACHE_SKIPPED file(s)" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Skipped:" "$COUNT_SKIPPED file(s)" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Probe (no gain):" "$COUNT_PROBE_SKIPPED file(s)" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Discarded (larger):" "$COUNT_DISCARDED file(s)" "$C_BLUE" "$C_RESET"
(( COUNT_SUSPECT > 0 )) && \
printf "%b║%b  %bPicture suspicious:%b   %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "$C_YELLOW" "$C_RESET" "$COUNT_SUSPECT file(s)" "$C_BLUE" "$C_RESET"
(( COUNT_UNVERIFIED > 0 )) && \
printf "%b║%b  %bVerification failed:%b  %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "$C_RED" "$C_RESET" "$COUNT_UNVERIFIED file(s)" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Failed:" "$COUNT_FAILED file(s)" "$C_BLUE" "$C_RESET"
printf "%b╟──────────────────────────────────────────────────────────────╢%b\n" "$C_BLUE" "$C_RESET"

if (( COUNT_PROCESSED > 0 && TOTAL_ORIG_BYTES > 0 )); then
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Size before:" "$(format_bytes "$TOTAL_ORIG_BYTES")" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Size after:" "$(format_bytes "$TOTAL_NEW_BYTES")" "$C_BLUE" "$C_RESET"
    PCT=$(pct_change "$TOTAL_ORIG_BYTES" "$TOTAL_NEW_BYTES")
    if (( TOTAL_SAVED > 0 )); then
        printf "%b║%b  %-21s %b%-37s%b %b║%b\n" "$C_BLUE" "$C_RESET" "Total saved:" "$C_GREEN" "-$(format_bytes "$TOTAL_SAVED") (${PCT}%)" "$C_RESET" "$C_BLUE" "$C_RESET"
    else
        printf "%b║%b  %-21s %b%-37s%b %b║%b\n" "$C_BLUE" "$C_RESET" "Increase:" "$C_YELLOW" "+$(format_bytes $(( -TOTAL_SAVED ))) (+${PCT}%)" "$C_RESET" "$C_BLUE" "$C_RESET"
    fi
else
    printf "%b║%b                        %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "No new videos accepted." "$C_BLUE" "$C_RESET"
fi
printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_BLUE" "$C_RESET"

if [[ "$GPU_FELL_BACK" == true ]]; then
    printf "\n%b[NOTE]%b The GPU failed during the run, switched to CPU.\n" \
        "$C_YELLOW" "$C_RESET"
    printf "          See the ffmpeg messages above. Until fixed: --encoder cpu.\n"
fi

if (( COUNT_SUSPECT > 0 )); then
    printf "\n%b[NOTE]%b %d output(s) with a suspicious picture comparison. They were KEPT.\n" \
        "$C_YELLOW" "$C_RESET" "$COUNT_SUSPECT"
    printf "          List: %s\n" "$SUSPECT_LIST"
    printf "          Please inspect them yourself; the measurement can be wrong.\n"
fi

mo_log "h265" "summary: $COUNT_PROCESSED encoded (GPU $COUNT_GPU / CPU $COUNT_CPU), $COUNT_SKIPPED skipped, $COUNT_PROBE_SKIPPED per Probe, $COUNT_DISCARDED discarded, $COUNT_FAILED failed"

resolve_pending_deletes
rm -f "$STATS_FILE"
