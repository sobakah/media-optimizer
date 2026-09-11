#!/bin/bash
# ==============================================================================
# gif-to-webp.sh  -  GIF -> animated WebP, or AVIF with --target avif
#
# STRUCTURE: CONFIGURATION -> STATISTICS -> WORKER -> xargs call at the end.
# Parallelisation and export rules are the same as in img-to-jxl.sh.
#
# INVARIANTS
#   - gif2webp encodes losslessly by default. A "-lossless" flag does NOT
#     exist; passing it makes the whole call fail.
#   - With GIF_TARGET=avif, ffmpeg takes over with an AV1 encoder. That path
#     is lossy, unlike the WebP one.
#   - Results larger than the original are discarded.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
_MO_ROOT="$(dirname "$SCRIPT_DIR")"; load_config
mo_install_exit_handler

usage() {
    cat <<'EOF'
gif-to-webp.sh - converts GIF to animated WebP or AVIF

  -i, --input   <dir>   source directory
  -o, --output  <dir>   target directory (empty = in place)
  -j, --workers <n>     parallel workers (default: nproc)
  -m, --method  <0-6>   WebP compression level (default: 6)
      --target <f>      webp | avif (default: webp)
      --avif-crf <n>    AV1 quality with --target avif (default: 20)
      --delete          move originals to the trash after success
      --no-delete       keep originals
      --force-delete    if the trash fails, rm without asking
      --keep-larger     keep the result even when it is larger
      --verify-deep     check the output with webpinfo
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
# No fixed default: the preset depends on whether a target
# Zielverzeichnis angegeben wurde (siehe mo_apply_inplace_defaults).
# A value from the environment or config file counts as explicit.
if [[ -n "${DELETE_ORIGINAL+x}" ]]; then DELETE_ORIGINAL_EXPLICIT=true; fi
DELETE_ORIGINAL="${DELETE_ORIGINAL:-false}"
FORCE_DELETE="${FORCE_DELETE:-false}"
COMPRESSION_METHOD="${COMPRESSION_METHOD:-6}"
DISCARD_IF_LARGER="${DISCARD_IF_LARGER:-true}"
# Keyframe distance in WebP. 0 = no keyframes, best compression,
# at the cost of slower seeking inside the animation.
GIF_KMIN="${GIF_KMIN:-0}"

# Target format: webp is lossless and widely supported, avif about half
# the size but lossy. Size comparison in the CHANGELOG.
GIF_TARGET="${GIF_TARGET:-webp}"
GIF_AVIF_CRF="${GIF_AVIF_CRF:-20}"
DRY_RUN="${DRY_RUN:-false}"
VERIFY_DEEP="${VERIFY_DEEP:-false}"
SKIP_EXISTING=true

POSITIONAL=()
while (( $# > 0 )); do
    case "$1" in
        -i|--input)     SOURCE_DIR="$2"; shift 2 ;;
        -o|--output)    OUTPUT_DIR="$2"; shift 2 ;;
        -j|--workers)   MAX_WORKERS="$2"; shift 2 ;;
        -m|--method)    COMPRESSION_METHOD="$2"; shift 2 ;;
        --target)       GIF_TARGET="$2"; shift 2 ;;
        --avif-crf)     GIF_AVIF_CRF="$2"; shift 2 ;;
        --delete)       DELETE_ORIGINAL=true; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --no-delete)    DELETE_ORIGINAL=false; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --force-delete) FORCE_DELETE=true; shift ;;
        --keep-larger)  DISCARD_IF_LARGER=false; shift ;;
        --verify-deep)  VERIFY_DEEP=true; shift ;;
        -n|--dry-run)   DRY_RUN=true; shift ;;
        --log)          MO_LOG_FILE="$2"; shift 2 ;;
        --no-log)       MO_LOG=false; shift ;;
        --hold)         MO_HOLD=1; shift ;;
        --no-hold)      MO_HOLD=0; shift ;;
        -h|--help)      usage; exit 0 ;;
        --)             shift; while (( $# > 0 )); do POSITIONAL+=("$1"); shift; done ;;
        -*)             echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)              POSITIONAL+=("$1"); shift ;;
    esac
done
[[ -n "${POSITIONAL[0]:-}" ]] && SOURCE_DIR="${POSITIONAL[0]}"
[[ -n "${POSITIONAL[1]:-}" ]] && OUTPUT_DIR="${POSITIONAL[1]}"

if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" ]]; then
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    printf "%bBatch GIF to WebP converter (via gif2webp)%b\n" "$C_BOLD" "$C_RESET"
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    read -rp "Run with the default settings? [Y/n]: " start_choice || start_choice=""
    if [[ "${start_choice,,}" =~ ^(n|nein|no)$ ]]; then
        read -rp "  Source folder [$SOURCE_DIR]: " x && SOURCE_DIR="${x:-$SOURCE_DIR}"
        read -rp "  Target folder (empty = in place) [$OUTPUT_DIR]: " x && OUTPUT_DIR="${x:-$OUTPUT_DIR}"
        read -rp "  Move originals to trash? [y/N]: " x
        [[ "${x,,}" =~ ^(j|ja|y|yes)$ ]] && DELETE_ORIGINAL=true || DELETE_ORIGINAL=false
        read -rp "  Parallel workers [$MAX_WORKERS]: " x && MAX_WORKERS="${x:-$MAX_WORKERS}"
        read -rp "  Compression level (0-6) [$COMPRESSION_METHOD]: " x && COMPRESSION_METHOD="${x:-$COMPRESSION_METHOD}"
    fi
fi

[[ -z "$SOURCE_DIR" ]] && { echo "No source directory given." >&2; exit 1; }
[[ -d "$SOURCE_DIR" ]] || { echo "Source directory not found: $SOURCE_DIR" >&2; exit 1; }
SOURCE_DIR="${SOURCE_DIR%/}"
if [[ -n "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="${OUTPUT_DIR%/}"
    [[ "$DRY_RUN" == true ]] || mkdir -p "$OUTPUT_DIR"
fi

mo_apply_inplace_defaults "$OUTPUT_DIR"
export MO_LOG_TAG="gif"
mo_log_init "gif-to-webp.sh" "$SOURCE_DIR" "$OUTPUT_DIR"

if [[ "$GIF_TARGET" == "avif" ]]; then
    require_cmds ffmpeg || exit 1
    _enc=$(ffmpeg -hide_banner -encoders 2>/dev/null || true)
    if [[ "$_enc" == *" libsvtav1 "* ]]; then
        GIF_AV1_ENCODER=libsvtav1; GIF_AV1_SPEED=(-preset 6)
    elif [[ "$_enc" == *" libaom-av1 "* ]]; then
        GIF_AV1_ENCODER=libaom-av1; GIF_AV1_SPEED=(-cpu-used 6)
    else
        printf "%b[MISSING]%b No AV1 encoder for --target avif.\n" "$C_RED" "$C_RESET" >&2
        exit 1
    fi
    printf "%b[GIF]%b target: AVIF (%s, CRF %s) - lossy!\n" \
        "$C_CYAN" "$C_RESET" "$GIF_AV1_ENCODER" "$GIF_AVIF_CRF"
else
    require_cmds gif2webp || exit 1
fi
[[ "$DRY_RUN" == true ]] && printf "%b[DRY-RUN]%b Nothing will be written or deleted.\n" "$C_CYAN" "$C_RESET"

cleanup_stale_parts "${OUTPUT_DIR:-$SOURCE_DIR}" "*.part.*.webp" "*.part.*.avif"

# ------------------------------------------------------------------------------
# STATISTICS
# ------------------------------------------------------------------------------
STATS_FILE="${SOURCE_DIR}/.gif_stats.env"
PENDING_DELETE_LOG="${SOURCE_DIR}/.gif_pending_deletes.txt"
CURRENT_RUN_LOG=$(mktemp /tmp/gif_stats_XXXXXX)
export CURRENT_RUN_LOG PENDING_DELETE_LOG DRY_RUN VERIFY_DEEP FORCE_DELETE

if [[ "${RESUMING:-false}" == "true" && -f "$STATS_FILE" ]]; then
    source "$STATS_FILE" 2>/dev/null || true
else
    rm -f "$STATS_FILE"
fi

TOTAL_PROCESSED=${TOTAL_PROCESSED:-0}; TOTAL_SKIPPED=${TOTAL_SKIPPED:-0}
TOTAL_FAILED=${TOTAL_FAILED:-0};       TOTAL_DISCARDED=${TOTAL_DISCARDED:-0}
TOTAL_COLLISION=${TOTAL_COLLISION:-0}
TOTAL_ORIG_BYTES=${TOTAL_ORIG_BYTES:-0}; TOTAL_NEW_BYTES=${TOTAL_NEW_BYTES:-0}
PREV_ELAPSED=${PREV_ELAPSED:-0}
START_TIME=$(date +%s)

finalize_stats() {
    local exit_code="$1"
    local tot_elapsed=$(( PREV_ELAPSED + $(date +%s) - START_TIME ))

    local c_proc=0 c_skip=0 c_fail=0 c_disc=0 c_coll=0 c_orig=0 c_new=0 c_dry=0
    if [[ -f "$CURRENT_RUN_LOG" ]]; then
        c_proc=$(grep -c "^SUCCESS"   "$CURRENT_RUN_LOG" || true)
        c_skip=$(grep -c "^SKIP"      "$CURRENT_RUN_LOG" || true)
        c_fail=$(grep -c "^FAIL"      "$CURRENT_RUN_LOG" || true)
        c_disc=$(grep -c "^DISCARD"   "$CURRENT_RUN_LOG" || true)
        c_coll=$(grep -c "^COLLISION" "$CURRENT_RUN_LOG" || true)
        c_dry=$(grep -c "^DRY"        "$CURRENT_RUN_LOG" || true)
        c_orig=$(awk '/^SUCCESS/ {s += $2} END {print s+0}' "$CURRENT_RUN_LOG")
        c_new=$(awk  '/^SUCCESS/ {s += $3} END {print s+0}' "$CURRENT_RUN_LOG")
        rm -f "$CURRENT_RUN_LOG"
    fi

    TOTAL_PROCESSED=$(( TOTAL_PROCESSED + c_proc ))
    TOTAL_SKIPPED=$(( TOTAL_SKIPPED + c_skip ))
    TOTAL_FAILED=$(( TOTAL_FAILED + c_fail ))
    TOTAL_DISCARDED=$(( TOTAL_DISCARDED + c_disc ))
    TOTAL_COLLISION=$(( TOTAL_COLLISION + c_coll ))
    TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + c_orig ))
    TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + c_new ))

    if [[ $exit_code -eq 130 ]]; then
        cat > "$STATS_FILE" <<EOF
TOTAL_PROCESSED=$TOTAL_PROCESSED
TOTAL_SKIPPED=$TOTAL_SKIPPED
TOTAL_FAILED=$TOTAL_FAILED
TOTAL_DISCARDED=$TOTAL_DISCARDED
TOTAL_COLLISION=$TOTAL_COLLISION
TOTAL_ORIG_BYTES=$TOTAL_ORIG_BYTES
TOTAL_NEW_BYTES=$TOTAL_NEW_BYTES
PREV_ELAPSED=$tot_elapsed
EOF
        resolve_pending_deletes
        exit 130
    fi
    rm -f "$STATS_FILE"

    if [[ "$DRY_RUN" == true ]]; then
        printf "\n%b[DRY-RUN]%b %s GIF(s) would be converted, %s skipped.\n" \
            "$C_CYAN" "$C_RESET" "$c_dry" "$TOTAL_SKIPPED"
        return 0
    fi

    local saved=$(( TOTAL_ORIG_BYTES - TOTAL_NEW_BYTES ))
    printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
    printf "%b║%b                         %bGIF SUMMARY%b                          %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
    printf "%b╠══════════════════════════════════════════════════════════════╣%b\n" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Total runtime:" "$(format_duration "$tot_elapsed")" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Converted:" "$TOTAL_PROCESSED file(s)" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Skipped:" "$TOTAL_SKIPPED file(s)" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Discarded (larger):" "$TOTAL_DISCARDED file(s)" "$C_BLUE" "$C_RESET"
    (( TOTAL_COLLISION > 0 )) && \
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Name conflict:" "$TOTAL_COLLISION file(s)" "$C_BLUE" "$C_RESET"
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

    mo_log "gif" "summary: $TOTAL_PROCESSED converted, $TOTAL_SKIPPED skipped, $TOTAL_DISCARDED discarded, $TOTAL_FAILED failed"
    resolve_pending_deletes
}

trap 'printf "\n%b[ABORT]%b GIF conversion stopped.\n" "$C_RED" "$C_RESET"; finalize_stats 130' SIGINT SIGTERM

# ------------------------------------------------------------------------------
# WORKER
# ------------------------------------------------------------------------------
convert_gif() {
    local src="$1"
    local orig_size; orig_size=$(stat -c%s "$src" 2>/dev/null || echo 0)
    local filename; filename="$(basename "$src")"
    local stem="${filename%.*}"

    local target_folder
    if [[ -n "${OUTPUT_DIR:-}" ]]; then
        local rel_dir; rel_dir="$(dirname "${src#"$SOURCE_DIR"/}")"
        [[ "$rel_dir" == "." ]] && target_folder="$OUTPUT_DIR" || target_folder="$OUTPUT_DIR/$rel_dir"
        [[ "$DRY_RUN" == true ]] || mkdir -p "$target_folder"
    else
        target_folder="$(dirname "$src")"
    fi
    local ext="webp"; [[ "$GIF_TARGET" == "avif" ]] && ext="avif"
    local final_dest="$target_folder/${stem}.${ext}"

    if [[ "$SKIP_EXISTING" == true && -s "$final_dest" ]]; then
        echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
    fi

    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        echo "DRY" >> "$CURRENT_RUN_LOG"
        printf "%b[DRY-RUN]%b '%s' -> '%s'\n" "$C_CYAN" "$C_RESET" "$filename" "$(basename "$final_dest")"
        return 0
    fi

    local lockdir="$target_folder/.${stem}.molock"
    if ! mkdir "$lockdir" 2>/dev/null; then
        echo "COLLISION" >> "$CURRENT_RUN_LOG"
        printf "%b[CONFLICT]%b '%s': target name already claimed, original kept.\n" \
            "$C_YELLOW" "$C_RESET" "$filename" >&2
        return 0
    fi

    local temp_dest="${final_dest}.part.$$.${RANDOM}.${ext}"
    local rc=0

    # Hinweis: gif2webp encoded per Default verlustfrei. Ein Flag "-lossless"
    # does not exist and makes the whole call fail.
    encode_gif() {
        if [[ "$GIF_TARGET" == "avif" ]]; then
            ffmpeg -nostdin -y -v error -i "$src" -c:v "$GIF_AV1_ENCODER" \
                -crf "$GIF_AVIF_CRF" "${GIF_AV1_SPEED[@]}" -pix_fmt yuv420p \
                -an -f avif -loop 0 "$temp_dest" </dev/null 2>/dev/null
        else
            gif2webp -m "$COMPRESSION_METHOD" -kmin "$GIF_KMIN" -metadata all -quiet \
                "$src" -o "$temp_dest" 2>/dev/null
        fi
    }

    if encode_gif && [[ -s "$temp_dest" ]] \
       && { [[ "$GIF_TARGET" == "avif" ]] || verify_output_image "$temp_dest"; }; then

        local new_size; new_size=$(stat -c%s "$temp_dest" 2>/dev/null || echo 0)

        if [[ "$DISCARD_IF_LARGER" == true ]] && (( new_size >= orig_size )); then
            rm -f "$temp_dest"
            echo "DISCARD" >> "$CURRENT_RUN_LOG"
            mo_log_file "$MO_LOG_TAG" "DISCARDED" "$src" "" "$orig_size" "$new_size"
            printf "%b[DISCARDED]%b '%s': WebP would be %s larger, original kept.\n" \
                "$C_YELLOW" "$C_RESET" "$filename" "$(format_bytes $(( new_size - orig_size )))"
        else
            mv "$temp_dest" "$final_dest"
            touch -r "$src" "$final_dest"
            echo "SUCCESS $orig_size $new_size" >> "$CURRENT_RUN_LOG"
            mo_log_file "$MO_LOG_TAG" "OK" "$src" "$(basename "$final_dest")" "$orig_size" "$new_size"
            [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src"
            printf "%b[OK]%b   '%s' -> '%s'\n" "$C_GREEN" "$C_RESET" "$filename" "$(basename "$final_dest")"
        fi
    else
        rm -f "$temp_dest"
        echo "FAIL" >> "$CURRENT_RUN_LOG"
        mo_log_file "$MO_LOG_TAG" "ERROR" "$src"
        printf "%b[ERROR]%b '%s' (original kept)\n" "$C_RED" "$C_RESET" "$filename" >&2
        rc=1
    fi

    rmdir "$lockdir" 2>/dev/null || true
    return $rc
}

export SOURCE_DIR OUTPUT_DIR SKIP_EXISTING DELETE_ORIGINAL COMPRESSION_METHOD DISCARD_IF_LARGER GIF_KMIN
export GIF_TARGET GIF_AVIF_CRF GIF_AV1_ENCODER="${GIF_AV1_ENCODER:-}"
export GIF_AV1_SPEED_STR="${GIF_AV1_SPEED[*]:-}"
export -f convert_gif

exit_code=0
find "$SOURCE_DIR" -type f -iname "*.gif" -print0 |
    xargs -0 -r -n 1 -P "$MAX_WORKERS" bash -c 'read -r -a GIF_AV1_SPEED <<< "$GIF_AV1_SPEED_STR"; convert_gif "$1"' _ || exit_code=$?

# xargs reports 124/125 on abort, 123 on worker errors (not an abort)
if (( exit_code == 124 || exit_code == 125 || exit_code == 130 )); then
    finalize_stats 130
else
    finalize_stats 0
fi
