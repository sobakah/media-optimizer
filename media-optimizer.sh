#!/bin/bash
# ==============================================================================
# media-optimizer.sh  -  orchestrator
#
# Counts the media types in the source directory and runs the matching stages
# in order: images, GIFs, AVIF (optional), video. Settings and progress are
# saved so an interrupted run can be resumed.
#
# STRUCTURE
#   CONFIGURATION   defaults, config file, options, interactive prompts
#   STATE           save_state / handle_interrupt for resume
#   PRE-FLIGHT      optional extension correction, collision warning
#   PIPELINE        stage selection, numbering, run_stage
#   FINISH          summary, restart offer
#
# INVARIANTS
#   - Every setting follows VAR="${VAR:-default}" so that environment and
#     config file keep precedence over the built-in default.
#   - Sub-scripts are started with an argument array, never a flat string;
#     paths may contain spaces.
#   - The stage count is computed before the first stage so the numbering in
#     the output is correct.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/scripts/lib/common.sh"
_MO_ROOT="$SCRIPT_DIR"; load_config
mo_install_exit_handler

SCRIPT_IMG="$SCRIPT_DIR/scripts/img-to-jxl.sh"
SCRIPT_GIF="$SCRIPT_DIR/scripts/gif-to-webp.sh"
SCRIPT_VIDEO="$SCRIPT_DIR/scripts/h264-to-h265.sh"
SCRIPT_AVIF="$SCRIPT_DIR/scripts/video-to-avif.sh"
SCRIPT_VERIFY="$SCRIPT_DIR/scripts/verify-output.sh"
STATE_FILE="$SCRIPT_DIR/.media_optimizer_state.env"

usage() {
    cat <<'EOF'
media-optimizer.sh [<input>] [<output>] [options]

  -i, --input  <dir>   source directory
  -o, --output <dir>   target directory (empty = in place)
      --delete         move originals to the trash after success
      --no-delete      keep originals
      --force-delete   if the trash fails, rm without asking
      --verify-deep    fully decode image outputs (slower)
      --preflight      fix file extensions by MIME type beforehand
      --verify-visual    compare picture content while converting (default)
      --no-verify-visual skip that comparison
      --strict-visual    discard suspicious video outputs instead of warning
      --verify-output    re-check the finished target directory afterwards
      --no-verify-output skip that final check (default)
      --avif           extra stage: short silent videos to AVIF
      --no-avif        skip that stage (default)
      --log <file>     log file (default: media-optimizer.log next to this)
      --no-log         do not write a log
  -y, --yes            no prompts, use defaults
  -n, --dry-run        preview only, write nothing
      --reset          discard the saved state and start over
      --hold           keep the window open at the end
      --no-hold        never keep the window open
  -h, --help           this help
EOF
}

# ------------------------------------------------------------------------------
# DEFAULTS
# ------------------------------------------------------------------------------
INPUT_DIR=""
OUTPUT_DIR=""
CHECK_EXTENSIONS_PREFLIGHT="${CHECK_EXTENSIONS_PREFLIGHT:-false}"
MAX_WORKERS="${MAX_WORKERS:-$(nproc)}"
# No fixed default: the preset depends on whether a target
# Zielverzeichnis angegeben wurde (siehe mo_apply_inplace_defaults).
# A value from the environment or config file counts as explicit.
if [[ -n "${DELETE_ORIGINAL+x}" ]]; then DELETE_ORIGINAL_EXPLICIT=true; fi
DELETE_ORIGINAL="${DELETE_ORIGINAL:-false}"      # destruktiver Default entschaerft
FORCE_DELETE="${FORCE_DELETE:-false}"
JXL_EFFORT="${JXL_EFFORT:-7}"
IMG_TARGET="${IMG_TARGET:-jxl}"
IMG_WEBP_QUALITY="${IMG_WEBP_QUALITY:-85}"
IMG_DISCARD_IF_LARGER="${IMG_DISCARD_IF_LARGER:-false}"
PNG_MODE="${PNG_MODE:-lossless}"
PNG_QUALITY="${PNG_QUALITY:-90}"
CJXL_THREADS="${CJXL_THREADS:-1}"
COMPRESSION_METHOD="${COMPRESSION_METHOD:-6}"
ENCODER_MODE="${ENCODER_MODE:-auto}"
BITRATE_THRESHOLD_KBPS="${BITRATE_THRESHOLD_KBPS:-3500}"
GPU_QP="${GPU_QP:-26}"
CPU_CRF="${CPU_CRF:-22}"
CPU_PRESET="${CPU_PRESET:-medium}"
CPU_X265_PARAMS="${CPU_X265_PARAMS:-aq-mode=3:no-sao=1}"
ENABLE_PROBE="${ENABLE_PROBE:-true}"
PROBE_MARGIN_PCT="${PROBE_MARGIN_PCT:-90}"
DISCARD_IF_LARGER="${DISCARD_IF_LARGER:-true}"
KEEP_SALVAGED_CORRUPT="${KEEP_SALVAGED_CORRUPT:-true}"
VERIFY_DEEP="${VERIFY_DEEP:-false}"
# Extra stage: short silent videos to AVIF. Off by default because the
# savings are small and AVIF cannot store audio.
ENABLE_AVIF_STAGE="${ENABLE_AVIF_STAGE:-false}"

# Picture check DURING the conversion: every video is compared against its
# source right after encoding. On by default, because it catches encoder
# faults that the runtime check cannot see.
VERIFY_VISUAL="${VERIFY_VISUAL:-true}"
VISUAL_STRICT="${VISUAL_STRICT:-false}"

# Picture check AFTER the run: scripts/verify-output.sh walks the finished
# target directory once more. Off by default, needs a target directory
# because it compares against the untouched originals.
ENABLE_VERIFY_OUTPUT="${ENABLE_VERIFY_OUTPUT:-false}"
CHECK_VISUAL="${CHECK_VISUAL:-true}"
DRY_RUN=false
ASSUME_YES=false
RESET_STATE=false

STAGE_1_DONE=false; STAGE_2_DONE=false; STAGE_3_DONE=false; STAGE_AVIF_DONE=false
RESUMING=false

# For the restart at the end: remember the invocation options.
MO_ORIGINAL_ARGS=("$@")

POSITIONAL=()
while (( $# > 0 )); do
    case "$1" in
        -i|--input)     INPUT_DIR="$2"; shift 2 ;;
        -o|--output)    OUTPUT_DIR="$2"; shift 2 ;;
        --delete)       DELETE_ORIGINAL=true; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --no-delete)    DELETE_ORIGINAL=false; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --force-delete) FORCE_DELETE=true; shift ;;
        --verify-deep)  VERIFY_DEEP=true; shift ;;
        --log)          MO_LOG_FILE="$2"; shift 2 ;;
        --no-log)       MO_LOG=false; shift ;;
        --verify-visual)    VERIFY_VISUAL=true; shift ;;
        --no-verify-visual) VERIFY_VISUAL=false; shift ;;
        --strict-visual)    VISUAL_STRICT=true; shift ;;
        --verify-output)    ENABLE_VERIFY_OUTPUT=true; shift ;;
        --no-verify-output) ENABLE_VERIFY_OUTPUT=false; shift ;;
        --avif)         ENABLE_AVIF_STAGE=true; shift ;;
        --no-avif)      ENABLE_AVIF_STAGE=false; shift ;;
        --preflight)    CHECK_EXTENSIONS_PREFLIGHT=true; shift ;;
        -y|--yes)       ASSUME_YES=true; shift ;;
        -n|--dry-run)   DRY_RUN=true; shift ;;
        --reset)        RESET_STATE=true; shift ;;
        --hold)         MO_HOLD=1; shift ;;
        --no-hold)      MO_HOLD=0; shift ;;
        -h|--help)      usage; exit 0 ;;
        --)             shift; while (( $# > 0 )); do POSITIONAL+=("$1"); shift; done ;;
        -*)             echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)              POSITIONAL+=("$1"); shift ;;
    esac
done
[[ -n "${POSITIONAL[0]:-}" ]] && INPUT_DIR="${POSITIONAL[0]}"
[[ -n "${POSITIONAL[1]:-}" ]] && OUTPUT_DIR="${POSITIONAL[1]}"
CLI_INPUT="$INPUT_DIR"; CLI_OUTPUT="$OUTPUT_DIR"

for s in "$SCRIPT_IMG" "$SCRIPT_GIF" "$SCRIPT_VIDEO" "$SCRIPT_AVIF" "$SCRIPT_VERIFY"; do
    if [[ ! -f "$s" ]]; then
        printf "%bError: script '%s' is missing.%b\n" "$C_RED" "$s" "$C_RESET" >&2
        exit 1
    fi
    [[ -x "$s" ]] || chmod +x "$s"
done

# ------------------------------------------------------------------------------
# STATE AND TRAP HANDLING
# ------------------------------------------------------------------------------
save_state() {
    local status="$1"
    [[ -z "$INPUT_DIR" || "$DRY_RUN" == true ]] && return 0
    cat > "$STATE_FILE" <<EOF
STATUS="$status"
INPUT_DIR="$INPUT_DIR"
OUTPUT_DIR="$OUTPUT_DIR"
MAX_WORKERS="$MAX_WORKERS"
DELETE_ORIGINAL="$DELETE_ORIGINAL"
FORCE_DELETE="$FORCE_DELETE"
JXL_EFFORT="$JXL_EFFORT"
PNG_MODE="$PNG_MODE"
PNG_QUALITY="$PNG_QUALITY"
COMPRESSION_METHOD="$COMPRESSION_METHOD"
ENCODER_MODE="$ENCODER_MODE"
BITRATE_THRESHOLD_KBPS="$BITRATE_THRESHOLD_KBPS"
GPU_QP="$GPU_QP"
CPU_CRF="$CPU_CRF"
CPU_PRESET="$CPU_PRESET"
ENABLE_PROBE="$ENABLE_PROBE"
PROBE_MARGIN_PCT="$PROBE_MARGIN_PCT"
DISCARD_IF_LARGER="$DISCARD_IF_LARGER"
KEEP_SALVAGED_CORRUPT="$KEEP_SALVAGED_CORRUPT"
VERIFY_DEEP="$VERIFY_DEEP"
STAGE_1_DONE="$STAGE_1_DONE"
STAGE_2_DONE="$STAGE_2_DONE"
STAGE_3_DONE="$STAGE_3_DONE"
STAGE_AVIF_DONE="$STAGE_AVIF_DONE"
ENABLE_AVIF_STAGE="$ENABLE_AVIF_STAGE"
EOF
}

handle_interrupt() {
    printf "\n\n%b[INTERRUPTED] Aborted by user.%b\n" "$C_YELLOW" "$C_RESET"
    mo_log "abbruch" "Aborted by user"
    mo_log_close 130
    save_state "INTERRUPTED"
    printf "%bProgress and settings have been saved.%b\n" "$C_CYAN" "$C_RESET"
    printf "%bThe next start will resume where this stopped.%b\n" "$C_CYAN" "$C_RESET"
    exit 130
}
trap handle_interrupt SIGINT SIGTERM

# No more eval: printf -v sets the target variable directly.
prompt_val() {
    local question="$1" default="$2" result_var="$3" input
    read -rp "  $question [$default]: " input || input=""
    printf -v "$result_var" '%s' "${input:-$default}"
}

prompt_bool() {
    local question="$1" default="$2" result_var="$3" input
    local def_str="Y/n"; [[ "$default" == false ]] && def_str="y/N"
    read -rp "  $question [$def_str]: " input || input=""
    case "${input,,}" in
        j|ja|y|yes) printf -v "$result_var" '%s' true ;;
        n|nein|no)  printf -v "$result_var" '%s' false ;;
        *)          printf -v "$result_var" '%s' "$default" ;;
    esac
}

# ------------------------------------------------------------------------------
# START
# ------------------------------------------------------------------------------
printf "%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b                       %bMEDIA OPTIMIZER%b                        %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_BLUE" "$C_RESET"

[[ "$RESET_STATE" == true ]] && rm -f "$STATE_FILE"

if [[ -f "$STATE_FILE" ]]; then
    source "$STATE_FILE" 2>/dev/null || true
    if [[ "${STATUS:-}" == "INTERRUPTED" || "${STATUS:-}" == "RUNNING" ]]; then
        printf "%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_YELLOW" "$C_RESET"
        printf "%b║%b               %bINCOMPLETE PREVIOUS RUN DETECTED%b               %b║%b\n" "$C_YELLOW" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_YELLOW" "$C_RESET"
        printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_YELLOW" "$C_RESET"
        printf "  Source: %b%s%b | images: %s | GIFs: %s | video: %s\n\n" \
            "$C_BOLD" "$INPUT_DIR" "$C_RESET" \
            "$([[ "$STAGE_1_DONE" == true ]] && echo done || echo open)" \
            "$([[ "$STAGE_2_DONE" == true ]] && echo done || echo open)" \
            "$([[ "$STAGE_3_DONE" == true ]] && echo done || echo open)"

        if [[ -t 0 && "$ASSUME_YES" == false ]]; then
            read -rp "Resume the last run with its saved settings? [Y/n]: " res_choice || res_choice=""
            case "${res_choice,,}" in
                n|nein|no)
                    echo "-> Discarding state."; echo ""
                    rm -f "$STATE_FILE"
                    restart_args=(--reset)
                    [[ -n "$CLI_INPUT"  ]] && restart_args+=(--input  "$CLI_INPUT")
                    [[ -n "$CLI_OUTPUT" ]] && restart_args+=(--output "$CLI_OUTPUT")
                    exec "$0" "${restart_args[@]}"
                    ;;
                *) RESUMING=true; printf -- "-> %bSettings loaded, resuming.%b\n\n" "$C_GREEN" "$C_RESET" ;;
            esac
        else
            RESUMING=true
        fi
    fi
fi

if [[ "$RESUMING" == false ]]; then
    if [[ -z "$INPUT_DIR" && -t 0 ]]; then
        read -rp "Source directory: " INPUT_DIR || INPUT_DIR=""
    fi
    [[ -z "$INPUT_DIR" ]]  && { printf "%bNo source directory given.%b\n" "$C_RED" "$C_RESET" >&2; exit 1; }
    [[ -d "$INPUT_DIR" ]]  || { printf "%bDirectory not found: %s%b\n" "$C_RED" "$INPUT_DIR" "$C_RESET" >&2; exit 1; }

    if [[ -z "$OUTPUT_DIR" && -t 0 && -z "$CLI_OUTPUT" && "$ASSUME_YES" == false ]]; then
        read -rp "Target directory (Enter = in place): " OUTPUT_DIR || OUTPUT_DIR=""
    fi

    if [[ -t 0 && "$ASSUME_YES" == false ]]; then
        echo ""
        read -rp "Use the default settings for all media types? [Y/n]: " std_choice || std_choice=""
        if [[ "${std_choice,,}" =~ ^(n|nein|no)$ ]]; then
            printf "\n%b─── GLOBAL SETTINGS ───%b\n" "$C_YELLOW" "$C_RESET"
            prompt_bool "Correct wrong file extensions by MIME type first?" "$CHECK_EXTENSIONS_PREFLIGHT" CHECK_EXTENSIONS_PREFLIGHT
            prompt_bool "Move originals to the trash after a successful conversion?" "$DELETE_ORIGINAL" DELETE_ORIGINAL
            prompt_bool "Fully decode image outputs to verify them (slower)?" "$VERIFY_DEEP" VERIFY_DEEP
            prompt_bool "Also convert short silent videos to AVIF?" "$ENABLE_AVIF_STAGE" ENABLE_AVIF_STAGE
            prompt_val  "Parallel workers for images/GIFs" "$MAX_WORKERS" MAX_WORKERS

            printf "\n%b─── IMAGES & GIFS ───%b\n" "$C_YELLOW" "$C_RESET"
            prompt_val "Image target format (jxl or webp)" "$IMG_TARGET" IMG_TARGET
            [[ "$IMG_TARGET" == "webp" ]] && prompt_val "WebP quality, higher is better (1-100)" "$IMG_WEBP_QUALITY" IMG_WEBP_QUALITY
            prompt_val "JXL effort, higher is slower and smaller (1-9)" "$JXL_EFFORT" JXL_EFFORT
            prompt_val "PNG mode (lossless or lossy)" "$PNG_MODE" PNG_MODE
            [[ "$PNG_MODE" == "lossy" ]] && prompt_val "JXL quality for PNG, higher is better (1-100)" "$PNG_QUALITY" PNG_QUALITY
            prompt_val "WebP compression level for GIFs (0-6)" "$COMPRESSION_METHOD" COMPRESSION_METHOD

            printf "\n%b─── VIDEO (H.265) ───%b\n" "$C_YELLOW" "$C_RESET"
            prompt_val "Video encoder (auto, gpu or cpu)" "$ENCODER_MODE" ENCODER_MODE
            [[ "${ENCODER_MODE,,}" == "auto" ]] && prompt_val "Bitrate above which the GPU is used (kb/s)" "$BITRATE_THRESHOLD_KBPS" BITRATE_THRESHOLD_KBPS
            [[ "${ENCODER_MODE,,}" =~ ^(auto|gpu)$ ]] && prompt_val "GPU quality, lower is better (24-28)" "$GPU_QP" GPU_QP
            if [[ "${ENCODER_MODE,,}" =~ ^(auto|cpu)$ ]]; then
                prompt_val "CPU quality, lower is better (20-24)" "$CPU_CRF" CPU_CRF
                prompt_val "CPU preset (medium or slow)" "$CPU_PRESET" CPU_PRESET
            fi
            prompt_bool "Encode a short test slice first to check whether it is worth it?" "$ENABLE_PROBE" ENABLE_PROBE
            prompt_bool "Discard a video if the result is larger than the original?" "$DISCARD_IF_LARGER" DISCARD_IF_LARGER
            prompt_bool "Keep videos salvaged from damaged sources even if they grew?" "$KEEP_SALVAGED_CORRUPT" KEEP_SALVAGED_CORRUPT
            prompt_bool "Compare the picture against the source while converting?" "$VERIFY_VISUAL" VERIFY_VISUAL
            [[ "$VERIFY_VISUAL" == true ]] && \
                prompt_bool "Discard a video when that comparison looks wrong?" "$VISUAL_STRICT" VISUAL_STRICT
            if [[ -n "$OUTPUT_DIR" ]]; then
                prompt_bool "Re-check the finished target directory at the end?" \
                    "$ENABLE_VERIFY_OUTPUT" ENABLE_VERIFY_OUTPUT
            fi
        fi
    fi
fi

INPUT_DIR="${INPUT_DIR%/}"
if [[ -n "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="${OUTPUT_DIR%/}"
    [[ "$DRY_RUN" == true ]] || mkdir -p "$OUTPUT_DIR"
fi

mo_apply_inplace_defaults "$OUTPUT_DIR"

mo_log_init "media-optimizer.sh ${MO_ORIGINAL_ARGS[*]}" "$INPUT_DIR" "$OUTPUT_DIR"
mo_log_settings DELETE_ORIGINAL RENAME_INPLACE FORCE_DELETE MAX_WORKERS \
    IMG_TARGET IMG_WEBP_QUALITY IMG_DISCARD_IF_LARGER \
    JXL_EFFORT PNG_MODE PNG_QUALITY CJXL_THREADS COMPRESSION_METHOD GIF_TARGET \
    ENCODER_MODE BITRATE_THRESHOLD_KBPS GPU_QP CPU_CRF CPU_PRESET CPU_X265_PARAMS \
    GPU_CODEC GPU_RC_MODE HEVC_TAG ENABLE_PROBE PROBE_MARGIN_PCT DISCARD_IF_LARGER \
    KEEP_SALVAGED_CORRUPT VERIFY_DEEP VERIFY_VISUAL VISUAL_STRICT \
    ENABLE_VERIFY_OUTPUT CHECK_VISUAL ENABLE_AVIF_STAGE CHECK_EXTENSIONS_PREFLIGHT \
    DRY_RUN

# ------------------------------------------------------------------------------
# CHECK DEPENDENCIES ONCE
# ------------------------------------------------------------------------------
missing_any=false
require_cmds cjxl cwebp file || missing_any=true
require_cmds gif2webp        || missing_any=true
require_cmds ffmpeg ffprobe  || missing_any=true
if [[ "$missing_any" == true ]]; then
    printf "%bPlease install the missing packages (libjxl-tools, webp, ffmpeg).%b\n" "$C_RED" "$C_RESET" >&2
    exit 1
fi
command -v gio >/dev/null 2>&1 || command -v trash-put >/dev/null 2>&1 || \
    printf "%b[NOTE]%b Neither gio nor trash-put found. Originals are only deleted after a prompt.\n" \
        "$C_YELLOW" "$C_RESET"

[[ "$DRY_RUN" == true ]] && printf "\n%b[DRY-RUN]%b No writes, no deletions.\n" "$C_CYAN" "$C_RESET"

# ------------------------------------------------------------------------------
# EXPORT FOR THE SUB-SCRIPTS
# ------------------------------------------------------------------------------
export NON_INTERACTIVE=true
export MAX_WORKERS DELETE_ORIGINAL FORCE_DELETE RENAME_INPLACE
export DELETE_ORIGINAL_EXPLICIT=true RENAME_INPLACE_EXPLICIT=true
export JXL_EFFORT PNG_MODE PNG_QUALITY COMPRESSION_METHOD
export IMG_TARGET IMG_WEBP_QUALITY IMG_DISCARD_IF_LARGER VIDEO_EXTENSIONS
export ENCODER_MODE BITRATE_THRESHOLD_KBPS GPU_QP CPU_CRF CPU_PRESET CPU_X265_PARAMS CJXL_THREADS
export ENABLE_PROBE PROBE_MARGIN_PCT DISCARD_IF_LARGER KEEP_SALVAGED_CORRUPT
export VERIFY_DEEP DRY_RUN RESUMING
export VERIFY_VISUAL VISUAL_STRICT CHECK_VISUAL
export SOURCE_DIR="$INPUT_DIR" OUTPUT_DIR

# ------------------------------------------------------------------------------
# PRE-FLIGHT: FILE EXTENSIONS
# ------------------------------------------------------------------------------
if [[ "$CHECK_EXTENSIONS_PREFLIGHT" == true && "$RESUMING" == false ]]; then
    printf "\n%bChecking file extensions against MIME types...%b\n" "$C_CYAN" "$C_RESET"
    while IFS= read -r -d '' filepath; do
        mime=$(file -b --mime-type "$filepath" 2>/dev/null || echo unknown)
        ext="${filepath##*.}"; ext="${ext,,}"
        correct_ext=""
        case "$mime" in
            image/jpeg) correct_ext="jpg" ;;  image/png) correct_ext="png" ;;
            image/webp) correct_ext="webp" ;; image/jxl) correct_ext="jxl" ;;
            image/gif)  correct_ext="gif" ;;  video/mp4) correct_ext="mp4" ;;
        esac
        [[ "$correct_ext" == "jpg" && "$ext" == "jpeg" ]] && continue
        if [[ -n "$correct_ext" && "$ext" != "$correct_ext" ]]; then
            target="${filepath%.*}.${correct_ext}"
            c=1; while [[ -e "$target" ]]; do target="${filepath%.*}_${c}.${correct_ext}"; c=$(( c + 1 )); done
            if [[ -n "$OUTPUT_DIR" ]]; then
                # With a target directory the source stays untouched. The
                # correction is handled by the conversion scripts, which place the
                # result in the target under the correct name.
                printf "  %b[NOTE]%b '%s' is actually %s (source left unchanged)\n" \
                    "$C_YELLOW" "$C_RESET" "$(basename "$filepath")" "$mime"
            else
                printf "  %b[CORRECTED]%b '%s' (%s) -> '%s'\n" "$C_YELLOW" "$C_RESET" \
                    "$(basename "$filepath")" "$mime" "$(basename "$target")"
                [[ "$DRY_RUN" == true ]] || mv "$filepath" "$target"
            fi
        fi
    done < <(find "$INPUT_DIR" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" \
             -o -iname "*.gif" -o -iname "*.webp" -o -iname "*.jxl" -o -iname "*.mp4" \) -print0 2>/dev/null)
fi

# ------------------------------------------------------------------------------
# WARNING: SAME BASE NAME, DIFFERENT EXTENSIONS
# foo.jpg and foo.png both map to foo.jxl. The workers lock the
# target name, but it is better to surface this beforehand.
# ------------------------------------------------------------------------------
collisions=$(find "$INPUT_DIR" -type f \
    \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.gif" \) 2>/dev/null |
    sed 's/\.[^.\/]*$//' | sort | uniq -d || true)
if [[ -n "$collisions" ]]; then
    printf "\n%b[WARNING]%b Files share a base name with different extensions.\n" "$C_YELLOW" "$C_RESET"
    printf "          Only the first is converted, the others are left untouched:\n"
    echo "$collisions" | head -n 10 | sed 's/^/            /'
    n=$(echo "$collisions" | wc -l)
    (( n > 10 )) && printf "            ... and %d more\n" "$(( n - 10 ))"
fi

# ------------------------------------------------------------------------------
# PIPELINE
# ------------------------------------------------------------------------------
COUNT_IMG=$(find "$INPUT_DIR" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" \) 2>/dev/null | wc -l || true)
COUNT_GIF=$(find "$INPUT_DIR" -type f -iname "*.gif" 2>/dev/null | wc -l || true)
VIDEO_EXTENSIONS="${VIDEO_EXTENSIONS:-mp4 m4v mov mkv webm avi ts m2ts wmv flv}"
_vid_find=(-type f \()
_first=true
for _ext in $VIDEO_EXTENSIONS; do
    [[ "$_first" == true ]] || _vid_find+=(-o)
    _vid_find+=(-iname "*.${_ext}"); _first=false
done
_vid_find+=(\) ! -name "*.part.*")
COUNT_VID=$(find "$INPUT_DIR" "${_vid_find[@]}" 2>/dev/null | wc -l || true)

(( COUNT_IMG == 0 )) && STAGE_1_DONE=true
(( COUNT_GIF == 0 )) && STAGE_2_DONE=true
(( COUNT_VID == 0 )) && STAGE_3_DONE=true
[[ "$ENABLE_AVIF_STAGE" != true ]] && STAGE_AVIF_DONE=true
(( COUNT_VID == 0 )) && STAGE_AVIF_DONE=true

# Number of stages that will actually run, so the numbering is correct.
STAGE_TOTAL=0
(( COUNT_IMG > 0 )) && STAGE_TOTAL=$(( STAGE_TOTAL + 1 ))
(( COUNT_GIF > 0 )) && STAGE_TOTAL=$(( STAGE_TOTAL + 1 ))
(( COUNT_VID > 0 )) && STAGE_TOTAL=$(( STAGE_TOTAL + 1 ))
if [[ "$ENABLE_AVIF_STAGE" == true ]] && (( COUNT_VID > 0 )); then
    STAGE_TOTAL=$(( STAGE_TOTAL + 1 ))
fi
STAGE_NO=0

save_state "RUNNING"

# Arguments for the sub-scripts (array because paths may contain spaces)
STAGE_ARGS=(--input "$INPUT_DIR")
[[ -n "$OUTPUT_DIR" ]] && STAGE_ARGS+=(--output "$OUTPUT_DIR")

run_stage() {
    local label="$1"; shift
    local e=0
    # MO_HOLD=0 for the sub-script only: otherwise every stage waits.
    # The orchestrator handles keeping the window open at the end.
    env MO_HOLD=0 "$@" || e=$?
    # 130 = SIGINT, 124/125 = timeout bzw. xargs-Abbruch.
    # 126/127 are execution errors (not executable / not found)
    # and must NOT count as a user abort.
    if (( e == 130 || e == 124 || e == 125 )); then handle_interrupt; fi
    if (( e == 126 || e == 127 )); then
        printf "\n%b[ERROR]%b Stage '%s' could not be started (code %d).\n" \
            "$C_RED" "$C_RESET" "$label" "$e" >&2
        printf "         Executable? chmod +x, and is the directory on a noexec mount?\n" >&2
        exit "$e"
    fi
    if (( e != 0 )); then
        printf "\n%b[ERROR]%b Stage '%s' exited with code %d.\n" "$C_RED" "$C_RESET" "$label" "$e" >&2
        printf "%bThe state is kept, resume after fixing the problem.%b\n" "$C_CYAN" "$C_RESET" >&2
        exit "$e"
    fi
    return 0
}

if (( COUNT_IMG > 0 )) && [[ "$STAGE_1_DONE" != true ]]; then
    STAGE_NO=$(( STAGE_NO + 1 ))
    printf "\n%b▶ [%d/%d] Starting image conversion (%d files)...%b\n" \
        "$C_CYAN" "$STAGE_NO" "$STAGE_TOTAL" "$COUNT_IMG" "$C_RESET"
    mo_log "start" "stage images ($COUNT_IMG candidates)"
    run_stage "Bilder" "$SCRIPT_IMG" "${STAGE_ARGS[@]}"
    STAGE_1_DONE=true; save_state "RUNNING"
fi

if (( COUNT_GIF > 0 )) && [[ "$STAGE_2_DONE" != true ]]; then
    STAGE_NO=$(( STAGE_NO + 1 ))
    printf "\n%b▶ [%d/%d] Starting GIF conversion (%d files)...%b\n" \
        "$C_CYAN" "$STAGE_NO" "$STAGE_TOTAL" "$COUNT_GIF" "$C_RESET"
    mo_log "start" "stage GIFs ($COUNT_GIF candidates)"
    run_stage "GIFs" "$SCRIPT_GIF" "${STAGE_ARGS[@]}"
    STAGE_2_DONE=true; save_state "RUNNING"
fi

# AVIF runs BEFORE HEVC encoding: short silent clips should end up as AVIF
# and not be converted to HEVC first. The HEVC stage then
# skips sources that already have an AVIF output.
if (( COUNT_VID > 0 )) && [[ "$ENABLE_AVIF_STAGE" == true && "$STAGE_AVIF_DONE" != true ]]; then
    STAGE_NO=$(( STAGE_NO + 1 ))
    printf "\n%b▶ [%d/%d] Starting AVIF stage (short, silent videos)...%b\n" \
        "$C_CYAN" "$STAGE_NO" "$STAGE_TOTAL" "$C_RESET"
    mo_log "start" "stage AVIF"
    run_stage "AVIF" "$SCRIPT_AVIF" "${STAGE_ARGS[@]}"
    STAGE_AVIF_DONE=true; save_state "RUNNING"
fi

if (( COUNT_VID > 0 )) && [[ "$STAGE_3_DONE" != true ]]; then
    STAGE_NO=$(( STAGE_NO + 1 ))
    printf "\n%b▶ [%d/%d] Starting video encoding (%d files)...%b\n" \
        "$C_CYAN" "$STAGE_NO" "$STAGE_TOTAL" "$COUNT_VID" "$C_RESET"
    mo_log "start" "stage video ($COUNT_VID candidates)"
    run_stage "Videos" "$SCRIPT_VIDEO" "${STAGE_ARGS[@]}"
    STAGE_3_DONE=true; save_state "RUNNING"
fi

# ------------------------------------------------------------------------------
# OFFER A RESTART
#
# Rebuilds the command line without directory arguments and replaces the process.
# exec fires no EXIT trap, so this run's log is already
# closed. Inherited state must be cleared first, otherwise the new
# run would inherit run ID, warning state and resolved settings.
offer_restart() {
    [[ -t 0 ]] || return 0
    [[ "$ASSUME_YES" == true ]] && return 0

    local answer
    printf "\n"
    read -rp "  Process another directory? [y/N]: " answer || answer=""
    MO_HOLD=0   # the question was already asked, do not also wait for Enter
    [[ "${answer,,}" =~ ^(j|ja|y|yes)$ ]] || return 0

    local -a restart=() skip=false a
    for a in "${MO_ORIGINAL_ARGS[@]}"; do
        if [[ "$skip" == true ]]; then skip=false; continue; fi
        case "$a" in
            -i|--input|-o|--output) skip=true; continue ;;
            -*) restart+=("$a") ;;
            *)  continue ;;   # positional arguments are the directories
        esac
    done

    unset MO_RUN_ID MO_INPLACE_WARNED RESUMING NON_INTERACTIVE
    unset DELETE_ORIGINAL RENAME_INPLACE
    unset DELETE_ORIGINAL_EXPLICIT RENAME_INPLACE_EXPLICIT
    unset SOURCE_DIR OUTPUT_DIR

    printf "\n%b─── New run ───%b\n\n" "$C_CYAN" "$C_RESET"
    exec "$0" "${restart[@]}"
}

# ------------------------------------------------------------------------------
# FINAL CHECK
# Only useful with a target directory: verify-output.sh compares the finished
# outputs against the untouched originals. Read-only, it never deletes.
# ------------------------------------------------------------------------------
if [[ "$ENABLE_VERIFY_OUTPUT" == true && "$DRY_RUN" != true ]]; then
    if [[ -z "$OUTPUT_DIR" ]]; then
        printf "\n%b[NOTE]%b --verify-output needs a target directory; the originals\n" \
            "$C_YELLOW" "$C_RESET"
        printf "       would already be gone in place. Skipping the final check.\n"
    elif (( COUNT_VID == 0 )); then
        printf "\n%b[NOTE]%b No videos in the source, nothing to re-check.\n" "$C_YELLOW" "$C_RESET"
    else
        printf "\n%b▶ Final check of the target directory...%b\n" "$C_CYAN" "$C_RESET"
        mo_log "verify" "final check of $OUTPUT_DIR"
        verify_rc=0
        env MO_HOLD=0 NON_INTERACTIVE=true "$SCRIPT_VERIFY" \
            -i "$INPUT_DIR" -o "$OUTPUT_DIR" || verify_rc=$?
        if (( verify_rc != 0 )); then
            printf "%b[NOTE]%b The check reported findings. Nothing was changed;\n" \
                "$C_YELLOW" "$C_RESET"
            printf "       repair with: %s -i %q -o %q --fix --run\n" \
                "$SCRIPT_VERIFY" "$INPUT_DIR" "$OUTPUT_DIR"
        fi
    fi
fi

rm -f "$STATE_FILE"
mo_log_close 0
printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_GREEN" "$C_RESET"
printf "%b║%b              %bALL STAGES COMPLETED SUCCESSFULLY%b               %b║%b\n" "$C_GREEN" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_GREEN" "$C_RESET"
printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_GREEN" "$C_RESET"

offer_restart
