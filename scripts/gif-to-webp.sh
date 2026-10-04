#!/bin/bash
# ==============================================================================
# gif-to-webp.sh  -  GIF -> animated WebP, or AVIF with --target avif
#
# STRUCTURE: CONFIGURATION -> STATISTICS -> WORKER -> xargs call at the end.
# Parallelisation, export rules and the worker scaffold (mo_worker_start,
# mo_w_*) are the same as in img-to-jxl.sh.
#
# INVARIANTS
#   - gif2webp encodes losslessly by default. A "-lossless" flag does NOT
#     exist; passing it makes the whole call fail.
#   - With GIF_TARGET=avif, ffmpeg takes over with an AV1 encoder. That path
#     is lossy, unlike the WebP one.
#   - Results larger than the original are discarded.
#   - The existing-output check is repeated under the lock and the final move
#     never overwrites. Workers run with set -euo pipefail.
#   - With a target directory no state files are written to the source.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
_MO_ROOT="$(dirname "$SCRIPT_DIR")"; load_config
mo_install_exit_handler

usage() {
    cat <<'USAGE'
gif-to-webp.sh - converts GIF to animated WebP or AVIF

  -i, --input   <dir>   source directory
  -o, --output  <dir>   target directory (empty = in place)
  -j, --workers <n>     parallel workers (default: nproc)
  -m, --method  <0-6>   WebP compression level (default: 6)
      --target <f>      webp | avif (default: webp)
      --avif-crf <n>    AV1 quality with --target avif, 0-63 (default: 20)
      --delete          move originals to the trash after success
      --no-delete       keep originals
      --force-delete    if the trash fails, rm without asking
      --keep-larger     keep the result even when it is larger
      --verify-deep     check the output with webpinfo
  -y, --yes             do not ask, keep the settings as given
      --log <file>      log file
      --no-log          do not write a log
      --hold            keep the window open at the end
      --no-hold         never keep the window open
  -n, --dry-run         preview only, write nothing
  -h, --help            this help

Hidden files and folders are skipped unless INCLUDE_HIDDEN=true.
USAGE
}

SOURCE_DIR="${SOURCE_DIR:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
MAX_WORKERS="${MAX_WORKERS:-$(nproc)}"
# No fixed default: the preset depends on whether a target directory was
# given (see mo_apply_inplace_defaults).
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
ASSUME_YES="${ASSUME_YES:-false}"
SKIP_EXISTING=true
SOURCE_FROM_CLI=false

POSITIONAL=()
while (( $# > 0 )); do
    case "$1" in
        -i|--input)     mo_need_value "$@"; SOURCE_DIR="$2"; SOURCE_FROM_CLI=true; shift 2 ;;
        -o|--output)    mo_need_value "$@"; OUTPUT_DIR="$2"; shift 2 ;;
        -j|--workers)   mo_need_value "$@"; MAX_WORKERS="$2"; shift 2 ;;
        -m|--method)    mo_need_value "$@"; COMPRESSION_METHOD="$2"; shift 2 ;;
        --target)       mo_need_value "$@"; GIF_TARGET="$2"; shift 2 ;;
        --avif-crf)     mo_need_value "$@"; GIF_AVIF_CRF="$2"; shift 2 ;;
        --delete)       DELETE_ORIGINAL=true; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --no-delete)    DELETE_ORIGINAL=false; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --force-delete) FORCE_DELETE=true; shift ;;
        --keep-larger)  DISCARD_IF_LARGER=false; shift ;;
        --verify-deep)  VERIFY_DEEP=true; shift ;;
        -y|--yes)       ASSUME_YES=true; shift ;;
        -n|--dry-run)   DRY_RUN=true; shift ;;
        --log)          mo_need_value "$@"; MO_LOG_FILE="$2"; shift 2 ;;
        --no-log)       MO_LOG=false; shift ;;
        --hold)         MO_HOLD=1; shift ;;
        --no-hold)      MO_HOLD=0; shift ;;
        -h|--help)      usage; exit 0 ;;
        --)             shift; while (( $# > 0 )); do POSITIONAL+=("$1"); shift; done ;;
        -*)             echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)              POSITIONAL+=("$1"); shift ;;
    esac
done
if [[ -n "${POSITIONAL[0]:-}" ]]; then SOURCE_DIR="${POSITIONAL[0]}"; SOURCE_FROM_CLI=true; fi
[[ -n "${POSITIONAL[1]:-}" ]] && OUTPUT_DIR="${POSITIONAL[1]}"

# The settings menu is for interactive use without arguments.
if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" && "$SOURCE_FROM_CLI" != true \
      && "$ASSUME_YES" != true ]]; then
    _banner="GIF to animated WebP (via gif2webp)"
    [[ "$GIF_TARGET" == "avif" ]] && _banner="GIF to animated AVIF (via ffmpeg, lossy)"
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    printf "%bBatch converter: %s%b\n" "$C_BOLD" "$_banner" "$C_RESET"
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    read -rp "Run with the default settings? [Y/n]: " start_choice || start_choice=""
    if [[ "${start_choice,,}" =~ ^(n|nein|no)$ ]]; then
        read -rp "  Source folder [$SOURCE_DIR]: " x && SOURCE_DIR="${x:-$SOURCE_DIR}"
        read -rp "  Target folder (empty = in place) [$OUTPUT_DIR]: " x && OUTPUT_DIR="${x:-$OUTPUT_DIR}"
        mo_prompt_delete_original "$OUTPUT_DIR"
        read -rp "  Parallel workers [$MAX_WORKERS]: " x && MAX_WORKERS="${x:-$MAX_WORKERS}"
        read -rp "  Compression level (0-6) [$COMPRESSION_METHOD]: " x && COMPRESSION_METHOD="${x:-$COMPRESSION_METHOD}"
    fi
fi

mo_validate MAX_WORKERS COMPRESSION_METHOD GIF_TARGET GIF_AVIF_CRF GIF_KMIN \
    DISCARD_IF_LARGER DELETE_ORIGINAL FORCE_DELETE VERIFY_DEEP DRY_RUN INCLUDE_HIDDEN

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

GIF_AV1_ENCODER=""; GIF_AV1_SPEED=()
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

# State, temp and lock files live in the target directory, or in the source
# directory in place; see img-to-jxl.sh.
WORK_DIR="${OUTPUT_DIR:-$SOURCE_DIR}"
mo_lock_workdir "$WORK_DIR"
cleanup_stale_parts "$WORK_DIR" "*.part.*.webp" "*.part.*.avif"

# ------------------------------------------------------------------------------
# STATISTICS
# ------------------------------------------------------------------------------
STATS_FILE="${WORK_DIR}/.gif_stats.env"
PENDING_DELETE_LOG="${WORK_DIR}/.gif_pending_deletes.txt"
mo_mktemp CURRENT_RUN_LOG gif_stats
export CURRENT_RUN_LOG PENDING_DELETE_LOG DRY_RUN VERIFY_DEEP FORCE_DELETE

if [[ "${RESUMING:-false}" == "true" && -f "$STATS_FILE" ]]; then
    source "$STATS_FILE" 2>/dev/null || true
elif [[ "$DRY_RUN" != true ]]; then
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
        [[ "$DRY_RUN" == true ]] && exit 130
        PREV_ELAPSED="$tot_elapsed"
        mo_write_vars "$STATS_FILE" \
            TOTAL_PROCESSED TOTAL_SKIPPED TOTAL_FAILED TOTAL_DISCARDED \
            TOTAL_COLLISION TOTAL_ORIG_BYTES TOTAL_NEW_BYTES PREV_ELAPSED
        resolve_pending_deletes
        exit 130
    fi
    [[ "$DRY_RUN" == true ]] || rm -f "$STATS_FILE"

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

on_stop() {
    trap '' INT TERM
    printf "\n%b[ABORT]%b GIF conversion stopped.\n" "$C_RED" "$C_RESET"
    mo_job_stop
    finalize_stats 130
}
trap on_stop SIGINT SIGTERM

# ------------------------------------------------------------------------------
# WORKER (one GIF per "bash -c", scaffold from common.sh)
# ------------------------------------------------------------------------------

# _gif_encode <src> <tmp>
# gif2webp encodes losslessly by default. A "-lossless" flag does not exist
# and makes the whole call fail.
_gif_encode() {
    local src="$1" tmp="$2"
    local -a speed=()
    if [[ "$GIF_TARGET" == "avif" ]]; then
        read -r -a speed <<< "$GIF_AV1_SPEED_STR"
        ffmpeg -nostdin -y -v error -i "$src" -c:v "$GIF_AV1_ENCODER" \
            -crf "$GIF_AVIF_CRF" "${speed[@]}" -pix_fmt yuv420p \
            -an -f avif -loop 0 "$tmp" </dev/null 2>/dev/null
    else
        gif2webp -m "$COMPRESSION_METHOD" -kmin "$GIF_KMIN" -metadata all -quiet \
            "$src" -o "$tmp" 2>/dev/null
    fi
}

convert_gif() {
    local src="$1"
    local filename stem ext target_folder dest tmp orig_size new_size
    filename="$(basename -- "$src")"
    stem="$(mo_stem "$filename")"
    ext="webp"; [[ "$GIF_TARGET" == "avif" ]] && ext="avif"
    target_folder="$(mo_w_target_folder "$src")"
    dest="$target_folder/${stem}.${ext}"

    if [[ "$SKIP_EXISTING" == true && -s "$dest" ]]; then
        mo_w_result SKIP; return 0
    fi
    if [[ "$DRY_RUN" == true ]]; then
        mo_w_result DRY
        printf "%b[DRY-RUN]%b '%s' -> '%s'\n" "$C_CYAN" "$C_RESET" "$filename" "$(basename -- "$dest")"
        return 0
    fi

    orig_size=$(stat -c%s -- "$src")
    mo_w_claim "$src" "$target_folder" "$stem" "$dest" || return 0
    mo_w_temp "$dest"; tmp="$_MO_W_TEMP"

    # verify_output_image checks WebP; for AVIF it only checks the size.
    if ! { _gif_encode "$src" "$tmp" && [[ -s "$tmp" ]] && verify_output_image "$tmp"; }; then
        mo_w_fail "$src" "conversion failed"
        if [[ "$GIF_TARGET" == "avif" ]]; then
            if mo_encoder_missing ffmpeg; then exit 255; fi
        elif mo_encoder_missing gif2webp; then
            exit 255
        fi
        return 0
    fi

    new_size=$(stat -c%s -- "$tmp")
    if [[ "$DISCARD_IF_LARGER" == true ]] && (( new_size >= orig_size )); then
        mo_w_result DISCARD
        mo_log_file "$MO_LOG_TAG" "DISCARDED" "$src" "" "$orig_size" "$new_size"
        printf "%b[DISCARDED]%b '%s': %s would be %s larger, original kept.\n" \
            "$C_YELLOW" "$C_RESET" "$filename" "${ext^^}" "$(format_bytes $(( new_size - orig_size )))"
        return 0
    fi

    mo_w_commit "$src" "$tmp" "$dest" || return 0
    mo_w_result SUCCESS "$orig_size" "$new_size"
    mo_log_file "$MO_LOG_TAG" "OK" "$src" "$(basename -- "$dest")" "$orig_size" "$new_size"
    printf "%b[OK]%b   '%s' -> '%s'\n" "$C_GREEN" "$C_RESET" "$filename" "$(basename -- "$dest")"
    if [[ "$DELETE_ORIGINAL" == true ]]; then safe_remove "$src"; fi
    return 0
}

export SOURCE_DIR OUTPUT_DIR SKIP_EXISTING DELETE_ORIGINAL COMPRESSION_METHOD DISCARD_IF_LARGER GIF_KMIN
export GIF_TARGET GIF_AVIF_CRF GIF_AV1_ENCODER
# GIF_AV1_SPEED is an array and does not survive export; rebuilt in the worker.
export GIF_AV1_SPEED_STR="${GIF_AV1_SPEED[*]:-}"
export -f convert_gif _gif_encode

run_batch() {
    mo_find_files "$SOURCE_DIR" -iname "*.gif" |
        xargs -0 -r -n 1 -P "$MAX_WORKERS" \
            bash -c 'mo_worker_start "$1"; convert_gif "$1"' _
}

exit_code=0
mo_job_start run_batch
mo_job_wait || exit_code=$?

# Exit codes of xargs: see "Fatal versus per-file failure" in common.sh.
# Per-file failures are counted from the FAIL tokens, not from xargs.
case "$exit_code" in
    0|123) ;;
    124)
        printf "\n%b[ABORT]%b Batch stopped: a required encoder is missing.\n" \
            "$C_RED" "$C_RESET" >&2
        finalize_stats 0
        exit 255 ;;
    125|130)
        finalize_stats 130 ;;
    *)
        printf "\n%b[ERROR]%b xargs failed (code %d), the batch did not run completely.\n" \
            "$C_RED" "$C_RESET" "$exit_code" >&2
        finalize_stats 0
        exit 1 ;;
esac
finalize_stats 0
if (( TOTAL_FAILED > 0 )); then exit "$MO_RC_PARTIAL"; fi
exit 0
