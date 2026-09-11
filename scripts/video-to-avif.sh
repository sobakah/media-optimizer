#!/bin/bash
# ==============================================================================
# video-to-avif.sh  -  short, silent videos -> animated AVIF
#
# Optional extra script. It only runs inside media-optimizer.sh when
# ENABLE_AVIF_STAGE is true, otherwise it is called directly.
#
# STRUCTURE: CONFIGURATION -> STATISTICS -> WORKER -> xargs call at the end.
#
# SELECTION CRITERIA (all must match, otherwise SKIP)
#   video codec listed in AVIF_SOURCE_CODECS, runtime below
#   AVIF_MAX_SECONDS, and no audio track. The audio condition is mandatory
#   because AVIF cannot store sound; --allow-audio lifts it and then drops
#   the audio deliberately.
#
# INVARIANTS
#   - If the output is larger than the original, exactly one more attempt
#     runs with CRF + AVIF_CRF_RETRY before it is discarded.
#   - verify_visual checks the picture before the original is touched.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
_MO_ROOT="$(dirname "$SCRIPT_DIR")"; load_config
mo_install_exit_handler

usage() {
    cat <<'EOF'
video-to-avif.sh - short, silent videos to animated AVIF

  -i, --input   <dir>   source directory
  -o, --output  <dir>   target directory (empty = in place)
  -j, --workers <n>     parallel workers (default: nproc)
      --max-seconds <s> only videos shorter than s (default: 10)
      --extensions <l>  source extensions, space separated
      --crf <n>         AV1 quality, lower is better (default: 32)
      --crf-retry <n>   penalty when the output is too large (default: 6)
      --preset <n>      SVT-AV1 preset 0-13, lower is slower (default: 6)
      --pix-fmt <p>     yuv420p | yuv444p (default: yuv420p)
      --allow-audio     also convert videos with audio (audio is lost!)
      --keep-larger     keep the result even when it is larger
      --delete          move originals to the trash after success
      --no-delete       keep originals
      --force-delete    if the trash fails, rm without asking
      --no-verify       no PSNR comparison after conversion
      --log <file>      log file
      --no-log          do not write a log
      --hold            keep the window open at the end
      --no-hold         never keep the window open
  -n, --dry-run         preview only, write nothing
  -h, --help            this help
EOF
}

SOURCE_DIR="${SOURCE_DIR:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
MAX_WORKERS="${MAX_WORKERS:-$(nproc)}"
AVIF_MAX_SECONDS="${AVIF_MAX_SECONDS:-10}"
AVIF_CRF="${AVIF_CRF:-32}"
AVIF_CRF_RETRY="${AVIF_CRF_RETRY:-6}"
AVIF_PRESET="${AVIF_PRESET:-6}"
AVIF_PIX_FMT="${AVIF_PIX_FMT:-yuv420p}"
AVIF_REQUIRE_SILENT="${AVIF_REQUIRE_SILENT:-true}"
VIDEO_EXTENSIONS="${VIDEO_EXTENSIONS:-mp4 m4v mov mkv webm avi ts m2ts wmv flv}"
AVIF_SOURCE_CODECS="${AVIF_SOURCE_CODECS:-h264 hevc vp8 vp9 av1 mpeg4}"
# No fixed default: the preset depends on whether a target
# Zielverzeichnis angegeben wurde (siehe mo_apply_inplace_defaults).
# A value from the environment or config file counts as explicit.
if [[ -n "${DELETE_ORIGINAL+x}" ]]; then DELETE_ORIGINAL_EXPLICIT=true; fi
DELETE_ORIGINAL="${DELETE_ORIGINAL:-false}"
FORCE_DELETE="${FORCE_DELETE:-false}"
DISCARD_IF_LARGER="${DISCARD_IF_LARGER:-true}"
VERIFY_VISUAL="${VERIFY_VISUAL:-true}"
VISUAL_PSNR_MIN="${VISUAL_PSNR_MIN:-15}"
DRY_RUN="${DRY_RUN:-false}"
SKIP_EXISTING=true

POSITIONAL=()
while (( $# > 0 )); do
    case "$1" in
        -i|--input)      SOURCE_DIR="$2"; shift 2 ;;
        -o|--output)     OUTPUT_DIR="$2"; shift 2 ;;
        -j|--workers)    MAX_WORKERS="$2"; shift 2 ;;
        --max-seconds)   AVIF_MAX_SECONDS="$2"; shift 2 ;;
        --extensions)    VIDEO_EXTENSIONS="$2"; shift 2 ;;
        --crf)           AVIF_CRF="$2"; shift 2 ;;
        --crf-retry)     AVIF_CRF_RETRY="$2"; shift 2 ;;
        --preset)        AVIF_PRESET="$2"; shift 2 ;;
        --pix-fmt)       AVIF_PIX_FMT="$2"; shift 2 ;;
        --allow-audio)   AVIF_REQUIRE_SILENT=false; shift ;;
        --keep-larger)   DISCARD_IF_LARGER=false; shift ;;
        --delete)        DELETE_ORIGINAL=true; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --no-delete)     DELETE_ORIGINAL=false; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --force-delete)  FORCE_DELETE=true; shift ;;
        --no-verify)     VERIFY_VISUAL=false; shift ;;
        --log)           MO_LOG_FILE="$2"; shift 2 ;;
        --no-log)        MO_LOG=false; shift ;;
        --hold)          MO_HOLD=1; shift ;;
        --no-hold)       MO_HOLD=0; shift ;;
        -n|--dry-run)    DRY_RUN=true; shift ;;
        -h|--help)       usage; exit 0 ;;
        --)              shift; while (( $# > 0 )); do POSITIONAL+=("$1"); shift; done ;;
        -*)              echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)               POSITIONAL+=("$1"); shift ;;
    esac
done
[[ -n "${POSITIONAL[0]:-}" ]] && SOURCE_DIR="${POSITIONAL[0]}"
[[ -n "${POSITIONAL[1]:-}" ]] && OUTPUT_DIR="${POSITIONAL[1]}"

[[ -z "$SOURCE_DIR" ]] && { echo "No source directory given." >&2; exit 1; }
[[ -d "$SOURCE_DIR" ]] || { echo "Source directory not found: $SOURCE_DIR" >&2; exit 1; }
SOURCE_DIR="${SOURCE_DIR%/}"
if [[ -n "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="${OUTPUT_DIR%/}"
    [[ "$DRY_RUN" == true ]] || mkdir -p "$OUTPUT_DIR"
fi

mo_apply_inplace_defaults "$OUTPUT_DIR"
export MO_LOG_TAG="avif"
mo_log_init "video-to-avif.sh" "$SOURCE_DIR" "$OUTPUT_DIR"

require_cmds ffmpeg ffprobe || exit 1

# Pick the AV1 encoder. libsvtav1 is considerably faster than libaom.
ENC_LIST=$(ffmpeg -hide_banner -encoders 2>/dev/null || true)
if [[ "$ENC_LIST" == *" libsvtav1 "* ]]; then
    AV1_ENCODER="libsvtav1"; AV1_SPEED=(-preset "$AVIF_PRESET")
elif [[ "$ENC_LIST" == *" libaom-av1 "* ]]; then
    AV1_ENCODER="libaom-av1"; AV1_SPEED=(-cpu-used 6)
else
    printf "%b[MISSING]%b No AV1 encoder (libsvtav1 or libaom-av1) in ffmpeg.\n" \
        "$C_RED" "$C_RESET" >&2
    exit 1
fi
printf "%b[AVIF]%b encoder: %s, CRF %s, %s\n" "$C_CYAN" "$C_RESET" \
    "$AV1_ENCODER" "$AVIF_CRF" "$AVIF_PIX_FMT"
[[ "$DRY_RUN" == true ]] && printf "%b[DRY-RUN]%b Nothing will be written or deleted.\n" "$C_CYAN" "$C_RESET"

cleanup_stale_parts "${OUTPUT_DIR:-$SOURCE_DIR}" "*.part.*.avif"

# ------------------------------------------------------------------------------
# STATISTICS
# ------------------------------------------------------------------------------
STATS_FILE="${SOURCE_DIR}/.avif_stats.env"
PENDING_DELETE_LOG="${SOURCE_DIR}/.avif_pending_deletes.txt"
CURRENT_RUN_LOG=$(mktemp /tmp/avif_stats_XXXXXX)
export CURRENT_RUN_LOG PENDING_DELETE_LOG DRY_RUN FORCE_DELETE VERIFY_VISUAL VISUAL_PSNR_MIN

if [[ "${RESUMING:-false}" == "true" && -f "$STATS_FILE" ]]; then
    source "$STATS_FILE" 2>/dev/null || true
else
    rm -f "$STATS_FILE"
fi

TOTAL_PROCESSED=${TOTAL_PROCESSED:-0}; TOTAL_SKIPPED=${TOTAL_SKIPPED:-0}
TOTAL_FAILED=${TOTAL_FAILED:-0};       TOTAL_DISCARDED=${TOTAL_DISCARDED:-0}
TOTAL_SUSPECT=${TOTAL_SUSPECT:-0}
TOTAL_ORIG_BYTES=${TOTAL_ORIG_BYTES:-0}; TOTAL_NEW_BYTES=${TOTAL_NEW_BYTES:-0}
PREV_ELAPSED=${PREV_ELAPSED:-0}
START_TIME=$(date +%s)

finalize_stats() {
    local exit_code="$1"
    local tot_elapsed=$(( PREV_ELAPSED + $(date +%s) - START_TIME ))
    local c_proc=0 c_skip=0 c_fail=0 c_disc=0 c_susp=0 c_orig=0 c_new=0 c_dry=0
    if [[ -f "$CURRENT_RUN_LOG" ]]; then
        c_proc=$(grep -c "^SUCCESS" "$CURRENT_RUN_LOG" || true)
        c_skip=$(grep -c "^SKIP"    "$CURRENT_RUN_LOG" || true)
        c_fail=$(grep -c "^FAIL"    "$CURRENT_RUN_LOG" || true)
        c_disc=$(grep -c "^DISCARD" "$CURRENT_RUN_LOG" || true)
        c_susp=$(grep -c "^SUSPECT" "$CURRENT_RUN_LOG" || true)
        c_dry=$(grep -c "^DRY"      "$CURRENT_RUN_LOG" || true)
        c_orig=$(awk '/^SUCCESS/ {s += $2} END {print s+0}' "$CURRENT_RUN_LOG")
        c_new=$(awk  '/^SUCCESS/ {s += $3} END {print s+0}' "$CURRENT_RUN_LOG")
        rm -f "$CURRENT_RUN_LOG"
    fi
    TOTAL_PROCESSED=$(( TOTAL_PROCESSED + c_proc )); TOTAL_SKIPPED=$(( TOTAL_SKIPPED + c_skip ))
    TOTAL_FAILED=$(( TOTAL_FAILED + c_fail ));       TOTAL_DISCARDED=$(( TOTAL_DISCARDED + c_disc ))
    TOTAL_SUSPECT=$(( TOTAL_SUSPECT + c_susp ))
    TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + c_orig )); TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + c_new ))

    if [[ $exit_code -eq 130 ]]; then
        cat > "$STATS_FILE" <<EOF
TOTAL_PROCESSED=$TOTAL_PROCESSED
TOTAL_SKIPPED=$TOTAL_SKIPPED
TOTAL_FAILED=$TOTAL_FAILED
TOTAL_DISCARDED=$TOTAL_DISCARDED
TOTAL_SUSPECT=$TOTAL_SUSPECT
TOTAL_ORIG_BYTES=$TOTAL_ORIG_BYTES
TOTAL_NEW_BYTES=$TOTAL_NEW_BYTES
PREV_ELAPSED=$tot_elapsed
EOF
        resolve_pending_deletes
        exit 130
    fi
    rm -f "$STATS_FILE"

    if [[ "$DRY_RUN" == true ]]; then
        printf "\n%b[DRY-RUN]%b %s video(s) would be converted, %s skipped.\n" \
            "$C_CYAN" "$C_RESET" "$c_dry" "$TOTAL_SKIPPED"
        return 0
    fi

    local saved=$(( TOTAL_ORIG_BYTES - TOTAL_NEW_BYTES ))
    printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
    printf "%b║%b                         %bAVIF SUMMARY%b                         %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
    printf "%b╠══════════════════════════════════════════════════════════════╣%b\n" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Total runtime:" "$(format_duration "$tot_elapsed")" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Converted:" "$TOTAL_PROCESSED file(s)" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Skipped:" "$TOTAL_SKIPPED file(s)" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Discarded (larger):" "$TOTAL_DISCARDED file(s)" "$C_BLUE" "$C_RESET"
    (( TOTAL_SUSPECT > 0 )) && \
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Picture suspicious:" "$TOTAL_SUSPECT file(s)" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Failed:" "$TOTAL_FAILED file(s)" "$C_BLUE" "$C_RESET"
    printf "%b╟──────────────────────────────────────────────────────────────╢%b\n" "$C_BLUE" "$C_RESET"
    if (( TOTAL_PROCESSED > 0 && TOTAL_ORIG_BYTES > 0 )); then
        printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Size before:" "$(format_bytes "$TOTAL_ORIG_BYTES")" "$C_BLUE" "$C_RESET"
        printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Size after:" "$(format_bytes "$TOTAL_NEW_BYTES")" "$C_BLUE" "$C_RESET"
        local pct; pct=$(pct_change "$TOTAL_ORIG_BYTES" "$TOTAL_NEW_BYTES")
        if (( saved > 0 )); then
            printf "%b║%b  %-21s %b%-37s%b %b║%b\n" "$C_BLUE" "$C_RESET" "Total saved:" "$C_GREEN" "-$(format_bytes "$saved") (${pct}%)" "$C_RESET" "$C_BLUE" "$C_RESET"
        else
            printf "%b║%b  %-21s %b%-37s%b %b║%b\n" "$C_BLUE" "$C_RESET" "Increase:" "$C_YELLOW" "+$(format_bytes $(( -saved ))) (+${pct}%)" "$C_RESET" "$C_BLUE" "$C_RESET"
        fi
    fi
    printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_BLUE" "$C_RESET"
    mo_log "avif" "summary: $TOTAL_PROCESSED converted, $TOTAL_SKIPPED skipped, $TOTAL_DISCARDED discarded, $TOTAL_FAILED failed"
    resolve_pending_deletes
}

trap 'printf "\n%b[ABORT]%b AVIF conversion stopped.\n" "$C_RED" "$C_RESET"; finalize_stats 130' SIGINT SIGTERM

# ------------------------------------------------------------------------------
# WORKER
# ------------------------------------------------------------------------------
convert_to_avif() {
    local src="$1"
    local orig_size; orig_size=$(stat -c%s "$src" 2>/dev/null || echo 0)
    local filename; filename="$(basename "$src")"
    local stem="${filename%.*}"

    # --- Auswahlkriterien ---
    local codec; codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
        -of default=noprint_wrappers=1:nokey=1 "$src" 2>/dev/null || echo "")
    [[ " $AVIF_SOURCE_CODECS " == *" $codec "* ]] || { echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0; }

    local dur; dur=$(ffprobe -v error -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "$src" 2>/dev/null || echo "")
    [[ "$dur" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0; }
    if ! awk -v d="$dur" -v m="$AVIF_MAX_SECONDS" 'BEGIN { exit (d < m) ? 0 : 1 }'; then
        echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
    fi

    if [[ "$AVIF_REQUIRE_SILENT" == true ]]; then
        local has_audio; has_audio=$(ffprobe -v error -select_streams a -show_entries stream=index \
            -of csv=p=0 "$src" 2>/dev/null | head -1)
        [[ -n "$has_audio" ]] && { echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0; }
    fi

    # --- Zielpfad ---
    local target_folder
    if [[ -n "${OUTPUT_DIR:-}" ]]; then
        local rel_dir; rel_dir="$(dirname "${src#"$SOURCE_DIR"/}")"
        [[ "$rel_dir" == "." ]] && target_folder="$OUTPUT_DIR" || target_folder="$OUTPUT_DIR/$rel_dir"
        [[ "$DRY_RUN" == true ]] || mkdir -p "$target_folder"
    else
        target_folder="$(dirname "$src")"
    fi
    local dest="$target_folder/${stem}.avif"

    if [[ "$SKIP_EXISTING" == true && -s "$dest" ]]; then
        echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
    fi
    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        echo "DRY" >> "$CURRENT_RUN_LOG"
        printf "%b[DRY-RUN]%b '%s' (%ss, %s) -> '%s'\n" \
            "$C_CYAN" "$C_RESET" "$filename" "${dur%.*}" "$codec" "$(basename "$dest")"
        return 0
    fi

    local lockdir="$target_folder/.${stem}.molock"
    mkdir "$lockdir" 2>/dev/null || { echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0; }

    local tmp="${dest}.part.$$.${RANDOM}.avif"
    local rc=0 crf="$AVIF_CRF" new_size=0 attempt

    encode_avif() {   # $1 = crf
        ffmpeg -nostdin -y -v error -i "$src" \
            -c:v "$AV1_ENCODER" -crf "$1" "${AV1_SPEED[@]}" \
            -pix_fmt "$AVIF_PIX_FMT" -an -f avif -loop 0 "$tmp" </dev/null 2>/dev/null
    }

    for attempt in 1 2; do
        if encode_avif "$crf" && [[ -s "$tmp" ]]; then
            new_size=$(stat -c%s "$tmp" 2>/dev/null || echo 0)
            # Too large? Retry once with a higher CRF instead of discarding at once.
            if [[ "$DISCARD_IF_LARGER" == true ]] && (( new_size >= orig_size )) && (( attempt == 1 )); then
                crf=$(( crf + AVIF_CRF_RETRY ))
                printf "%b[RETRY]%b '%s': %s too large, retrying with CRF %s\n" \
                    "$C_YELLOW" "$C_RESET" "$filename" "$(format_bytes "$new_size")" "$crf"
                rm -f "$tmp"; continue
            fi
            break
        fi
        rm -f "$tmp"
        echo "FAIL" >> "$CURRENT_RUN_LOG"
        mo_log_file "$MO_LOG_TAG" "ERROR" "$src"
        printf "%b[ERROR]%b '%s' (original kept)\n" "$C_RED" "$C_RESET" "$filename" >&2
        rmdir "$lockdir" 2>/dev/null || true
        return 1
    done

    if [[ "$DISCARD_IF_LARGER" == true ]] && (( new_size >= orig_size )); then
        rm -f "$tmp"
        echo "DISCARD" >> "$CURRENT_RUN_LOG"
        mo_log_file "$MO_LOG_TAG" "DISCARDED" "$src" "" "$orig_size" "$new_size"
        printf "%b[DISCARDED]%b '%s': AVIF would be %s larger, original kept.\n" \
            "$C_YELLOW" "$C_RESET" "$filename" "$(format_bytes $(( new_size - orig_size )))"
        rmdir "$lockdir" 2>/dev/null || true
        return 0
    fi

    # Check the picture content before touching the original
    local suspect=false visual_note=""
    if [[ "$VERIFY_VISUAL" == true ]] && verify_visual "$src" "$tmp" "$VISUAL_PSNR_MIN"; then
        visual_note=", picture ${VISUAL_PSNR:-n/a} dB"
    elif [[ "$VERIFY_VISUAL" == true ]]; then
        suspect=true
        echo "SUSPECT" >> "$CURRENT_RUN_LOG"
        printf "%b[PICTURE?]%b '%s': PSNR %s dB below %s dB. Kept, original kept.\n" \
            "$C_YELLOW" "$C_RESET" "$filename" "${VISUAL_PSNR:-?}" "$VISUAL_PSNR_MIN" >&2
    fi

    mv "$tmp" "$dest"
    touch -r "$src" "$dest"
    echo "SUCCESS $orig_size $new_size" >> "$CURRENT_RUN_LOG"
    mo_log_file "$MO_LOG_TAG" "OK" "$src" "$(basename "$dest")" "$orig_size" "$new_size"
    [[ "$DELETE_ORIGINAL" == true && "$suspect" == false ]] && safe_remove "$src"
    printf "%b[AVIF]%b '%s' -> '%s'  (%s, %s%%%s)\n" "$C_GREEN" "$C_RESET" \
        "$filename" "$(basename "$dest")" "$(format_bytes "$new_size")" \
        "$(( orig_size > 0 ? new_size * 100 / orig_size : 0 ))" "$visual_note"

    rmdir "$lockdir" 2>/dev/null || true
    return $rc
}

export SOURCE_DIR OUTPUT_DIR SKIP_EXISTING DELETE_ORIGINAL DISCARD_IF_LARGER
export AVIF_CRF AVIF_CRF_RETRY AVIF_PRESET AVIF_PIX_FMT AVIF_MAX_SECONDS
export AVIF_REQUIRE_SILENT AV1_ENCODER AVIF_SOURCE_CODECS
export AV1_SPEED_STR="${AV1_SPEED[*]}"
export -f convert_to_avif

# AV1_SPEED is an array and does not survive export; rebuild it in the worker.
convert_wrapper() {
    read -r -a AV1_SPEED <<< "$AV1_SPEED_STR"
    convert_to_avif "$1"
}
export -f convert_wrapper

exit_code=0
FIND_OPTS=(-type f \()
_first=true
for _ext in $VIDEO_EXTENSIONS; do
    [[ "$_first" == true ]] || FIND_OPTS+=(-o)
    FIND_OPTS+=(-iname "*.${_ext}")
    _first=false
done
FIND_OPTS+=(\) ! -name "*.part.*")

find "$SOURCE_DIR" "${FIND_OPTS[@]}" -print0 |
    xargs -0 -r -n 1 -P "$MAX_WORKERS" bash -c 'convert_wrapper "$1"' _ || exit_code=$?

if (( exit_code == 124 || exit_code == 125 || exit_code == 130 )); then
    finalize_stats 130
else
    finalize_stats 0
fi
