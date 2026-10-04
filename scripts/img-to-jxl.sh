#!/bin/bash
# ==============================================================================
# img-to-jxl.sh  -  JPG/PNG -> JPEG XL, or WebP with --target webp
#
# STRUCTURE: CONFIGURATION -> STATISTICS -> WORKER -> xargs call at the end.
#
# Conversion runs in parallel: find supplies the files and xargs starts one
# shell per file that calls convert_image. Everything the worker needs must
# therefore be exported, variables with "export" and functions with
# "export -f", otherwise it is simply not present in that shell.
#
# INVARIANTS
#   - Target names are claimed with a lock directory. "foto.jpg" and
#     "foto.png" both map to "foto.jxl"; without the lock two workers would
#     write the same file. The existing-output check is repeated under the
#     lock and the final move never overwrites (mo_w_claim, mo_w_commit).
#   - Temp files carry PID and a random number so parallel workers never
#     share one.
#   - The encoder settings follow the real file type, never the extension.
#     cjxl and cwebp detect the format by content, so a PNG named .jpg would
#     otherwise get the JPEG settings, which for cjxl means lossy.
#   - JPEG is transcoded bit-exactly lossless in the JXL path. PNG_MODE only
#     affects PNG. There is deliberately no pixel-based fallback for JPEGs
#     cjxl cannot transcode: for a truncated JPEG it would "succeed" and
#     replace the damaged original with an equally damaged copy.
#   - With a target directory the source directory is never modified, not
#     even when an extension has to be corrected, and no state files are
#     written there.
#   - Workers run with set -euo pipefail (mo_worker_start). An unexpected
#     error ends the worker before the deletion step; its EXIT trap records
#     FAIL.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
_MO_ROOT="$(dirname "$SCRIPT_DIR")"; load_config
mo_install_exit_handler

usage() {
    cat <<'USAGE'
img-to-jxl.sh - converts JPG/PNG to JPEG XL or WebP

  -i, --input   <dir>    source directory
  -o, --output  <dir>    target directory (empty = in place)
  -j, --workers <n>      parallel workers (default: nproc)
      --target <f>       jxl | webp (default: jxl)
      --webp-quality <q> quality for lossy WebP, 0-100 (default: 85)
      --discard-larger   discard the result if larger than the original
      --copy-metadata <m> auto | always | never: copy PNG metadata
                         with exiftool (default: auto)
  -e, --effort  <1-10>   JXL effort (default: 7)
      --png-mode  <m>    lossless | lossy (default: lossless)
      --png-quality <q>  only with --png-mode lossy, 1-100 (default: 90)
      --cjxl-threads <n> threads per cjxl process (default: 1)
      --delete           move originals to the trash after success
      --no-delete        keep originals
      --force-delete     if the trash fails, rm without asking
      --verify-deep      fully decode the output (slower, safer)
  -y, --yes              do not ask, keep the settings as given
      --log <file>       log file
      --no-log           do not write a log
      --hold             keep the window open at the end
      --no-hold          never keep the window open
  -n, --dry-run          preview only, write nothing
  -h, --help             this help

Positional arguments are still accepted: img-to-jxl.sh <input> [output]
Hidden files and folders are skipped unless INCLUDE_HIDDEN=true.
USAGE
}

# ------------------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------------------
SOURCE_DIR="${SOURCE_DIR:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
MAX_WORKERS="${MAX_WORKERS:-$(nproc)}"
# No fixed default: the preset depends on whether a target directory was
# given (see mo_apply_inplace_defaults).
# A value from the environment or config file counts as explicit.
if [[ -n "${DELETE_ORIGINAL+x}" ]]; then DELETE_ORIGINAL_EXPLICIT=true; fi
DELETE_ORIGINAL="${DELETE_ORIGINAL:-false}"
FORCE_DELETE="${FORCE_DELETE:-false}"
JXL_EFFORT="${JXL_EFFORT:-7}"
# Target format for JPG and PNG. "webp" converts everything to WebP.
# JPEG is necessarily re-encoded lossily, because lossless
# WebP from an already DCT-compressed JPEG would be larger than the source.
IMG_TARGET="${IMG_TARGET:-jxl}"
IMG_WEBP_QUALITY="${IMG_WEBP_QUALITY:-85}"
IMG_DISCARD_IF_LARGER="${IMG_DISCARD_IF_LARGER:-false}"

# Metadata. cjxl 0.11 and cwebp copy EXIF and XMP, but not the PNG text
# chunks, where many tools put Artist and Copyright. cjxl also Brotli-
# compresses the metadata boxes by default, which many readers (exiftool
# without an extra Perl module, for one) cannot decode. So cjxl always writes
# them uncompressed, for JPEG and PNG alike; measured: JPEG reconstruction
# stays bit-exact. For PNG, exiftool then adds the text chunks into the temp
# file, before verification.
#   auto   copy when exiftool is installed (default)
#   always copy; refuse to start when exiftool is missing
#   never  do not copy
# If exiftool fails on a file, that file counts as failed and its original
# is kept, so metadata is never lost silently.
IMG_COPY_METADATA="${IMG_COPY_METADATA:-auto}"
PNG_MODE="${PNG_MODE:-lossless}"
PNG_QUALITY="${PNG_QUALITY:-90}"
DRY_RUN="${DRY_RUN:-false}"
VERIFY_DEEP="${VERIFY_DEEP:-false}"
# Each worker is its own cjxl process. By default cjxl would start as
# many threads as there are cores (16 workers x 16 threads
# on an 8C/16T chip). One thread per process avoids the oversubscription.
CJXL_THREADS="${CJXL_THREADS:-1}"
ASSUME_YES="${ASSUME_YES:-false}"
SKIP_EXISTING=true
WEBP_FALLBACK_METHOD=6
SOURCE_FROM_CLI=false

POSITIONAL=()
while (( $# > 0 )); do
    case "$1" in
        -i|--input)       mo_need_value "$@"; SOURCE_DIR="$2"; SOURCE_FROM_CLI=true; shift 2 ;;
        -o|--output)      mo_need_value "$@"; OUTPUT_DIR="$2"; shift 2 ;;
        -j|--workers)     mo_need_value "$@"; MAX_WORKERS="$2"; shift 2 ;;
        -e|--effort)      mo_need_value "$@"; JXL_EFFORT="$2"; shift 2 ;;
        --target)         mo_need_value "$@"; IMG_TARGET="$2"; shift 2 ;;
        --webp-quality)   mo_need_value "$@"; IMG_WEBP_QUALITY="$2"; shift 2 ;;
        --discard-larger) IMG_DISCARD_IF_LARGER=true; shift ;;
        --copy-metadata)  mo_need_value "$@"; IMG_COPY_METADATA="$2"; shift 2 ;;
        --png-mode)       mo_need_value "$@"; PNG_MODE="$2"; shift 2 ;;
        --png-quality)    mo_need_value "$@"; PNG_QUALITY="$2"; shift 2 ;;
        --delete)         DELETE_ORIGINAL=true; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --no-delete)      DELETE_ORIGINAL=false; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --force-delete)   FORCE_DELETE=true; shift ;;
        --cjxl-threads)   mo_need_value "$@"; CJXL_THREADS="$2"; shift 2 ;;
        --verify-deep)    VERIFY_DEEP=true; shift ;;
        -y|--yes)         ASSUME_YES=true; shift ;;
        -n|--dry-run)     DRY_RUN=true; shift ;;
        --log)            mo_need_value "$@"; MO_LOG_FILE="$2"; shift 2 ;;
        --no-log)         MO_LOG=false; shift ;;
        --hold)           MO_HOLD=1; shift ;;
        --no-hold)        MO_HOLD=0; shift ;;
        -h|--help)        usage; exit 0 ;;
        --)               shift; while (( $# > 0 )); do POSITIONAL+=("$1"); shift; done ;;
        -*)               echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)                POSITIONAL+=("$1"); shift ;;
    esac
done
if [[ -n "${POSITIONAL[0]:-}" ]]; then SOURCE_DIR="${POSITIONAL[0]}"; SOURCE_FROM_CLI=true; fi
[[ -n "${POSITIONAL[1]:-}" ]] && OUTPUT_DIR="${POSITIONAL[1]}"

# The settings menu is for interactive use without arguments. Whoever names
# the source on the command line has already chosen, and is not asked again.
if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" && "$SOURCE_FROM_CLI" != true \
      && "$ASSUME_YES" != true ]]; then
    _banner="JPG/PNG -> JXL (with WebP fallback)"
    [[ "$IMG_TARGET" == "webp" ]] && _banner="JPG/PNG -> WebP"
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    printf "%bImage converter: %s%b\n" "$C_BOLD" "$_banner" "$C_RESET"
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    read -rp "Run with the default settings? [Y/n]: " start_choice || start_choice=""
    if [[ "${start_choice,,}" =~ ^(n|nein|no)$ ]]; then
        read -rp "  Source directory [$SOURCE_DIR]: " x && SOURCE_DIR="${x:-$SOURCE_DIR}"
        read -rp "  Target directory (empty = in place) [$OUTPUT_DIR]: " x && OUTPUT_DIR="${x:-$OUTPUT_DIR}"
        mo_prompt_delete_original "$OUTPUT_DIR"
        read -rp "  JXL effort (1-10) [$JXL_EFFORT]: " x && JXL_EFFORT="${x:-$JXL_EFFORT}"
        read -rp "  PNG mode (lossless/lossy) [$PNG_MODE]: " x && PNG_MODE="${x:-$PNG_MODE}"
    fi
fi

# After config file, command line and prompts: every source of a value.
mo_validate MAX_WORKERS JXL_EFFORT IMG_TARGET IMG_WEBP_QUALITY IMG_DISCARD_IF_LARGER \
    IMG_COPY_METADATA PNG_MODE PNG_QUALITY CJXL_THREADS DELETE_ORIGINAL FORCE_DELETE \
    VERIFY_DEEP DRY_RUN INCLUDE_HIDDEN

[[ -z "$SOURCE_DIR" ]] && { echo "No source directory given." >&2; exit 1; }
[[ -d "$SOURCE_DIR" ]] || { echo "Source directory not found: $SOURCE_DIR" >&2; exit 1; }
SOURCE_DIR="${SOURCE_DIR%/}"
if [[ -n "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="${OUTPUT_DIR%/}"
    [[ "$DRY_RUN" == true ]] || mkdir -p "$OUTPUT_DIR"
fi

mo_apply_inplace_defaults "$OUTPUT_DIR"
export MO_LOG_TAG="img"
mo_log_init "img-to-jxl.sh" "$SOURCE_DIR" "$OUTPUT_DIR"

require_cmds cjxl cwebp file || exit 1
if [[ "$IMG_COPY_METADATA" == "always" ]] && ! command -v exiftool >/dev/null 2>&1; then
    printf "%b[MISSING]%b --copy-metadata always needs exiftool, which was not found.\n" \
        "$C_RED" "$C_RESET" >&2
    exit 1
fi

# Uncompressed metadata boxes, only when this cjxl knows the option: an
# unknown option would make every file fail (and every PNG fall back to WebP).
# Not "cjxl -h | grep -q": grep exits at the first match, cjxl may then die
# of SIGPIPE, and pipefail turns that into "not supported" - measured as a
# test that failed only sometimes.
CJXL_META_ARGS=""
_cjxl_help="$(cjxl -h -v -v -v 2>&1 || true)"
if [[ "$_cjxl_help" == *"--compress_boxes"* ]]; then
    CJXL_META_ARGS="--compress_boxes=0"
fi

[[ "$IMG_TARGET" == "webp" ]] && printf "%b[IMAGES]%b target: WebP (JPEG lossy at q%s, PNG %s)\n" \
    "$C_CYAN" "$C_RESET" "$IMG_WEBP_QUALITY" "$PNG_MODE"

[[ "$DRY_RUN" == true ]] && printf "%b[DRY-RUN]%b Nothing will be written or deleted.\n" "$C_CYAN" "$C_RESET"

# Everything this script writes besides the outputs (stats, pending deletes,
# temp and lock files) lives in the working directory: the target directory,
# or the source directory in place. The source is never written in target
# mode.
WORK_DIR="${OUTPUT_DIR:-$SOURCE_DIR}"
mo_lock_workdir "$WORK_DIR"
cleanup_stale_parts "$WORK_DIR" "*.part.*.jxl" "*.part.*.webp"

# ------------------------------------------------------------------------------
# STATISTICS
# ------------------------------------------------------------------------------
STATS_FILE="${WORK_DIR}/.img_stats.env"
PENDING_DELETE_LOG="${WORK_DIR}/.img_pending_deletes.txt"
mo_mktemp CURRENT_RUN_LOG img_stats
export CURRENT_RUN_LOG PENDING_DELETE_LOG DRY_RUN VERIFY_DEEP FORCE_DELETE

if [[ "${RESUMING:-false}" == "true" && -f "$STATS_FILE" ]]; then
    source "$STATS_FILE" 2>/dev/null || true
elif [[ "$DRY_RUN" != true ]]; then
    rm -f "$STATS_FILE"
fi

TOTAL_PROCESSED=${TOTAL_PROCESSED:-0}; TOTAL_SKIPPED=${TOTAL_SKIPPED:-0}
TOTAL_FAILED=${TOTAL_FAILED:-0};       TOTAL_COLLISION=${TOTAL_COLLISION:-0}
TOTAL_DISCARDED=${TOTAL_DISCARDED:-0}
TOTAL_ORIG_BYTES=${TOTAL_ORIG_BYTES:-0}; TOTAL_NEW_BYTES=${TOTAL_NEW_BYTES:-0}
PREV_ELAPSED=${PREV_ELAPSED:-0}
START_TIME=$(date +%s)

finalize_stats() {
    local exit_code="$1"
    local tot_elapsed=$(( PREV_ELAPSED + $(date +%s) - START_TIME ))

    local c_proc=0 c_skip=0 c_fail=0 c_coll=0 c_disc=0 c_orig=0 c_new=0 c_dry=0
    if [[ -f "$CURRENT_RUN_LOG" ]]; then
        c_proc=$(grep -c "^SUCCESS"   "$CURRENT_RUN_LOG" || true)
        c_skip=$(grep -c "^SKIP"      "$CURRENT_RUN_LOG" || true)
        c_fail=$(grep -c "^FAIL"      "$CURRENT_RUN_LOG" || true)
        c_coll=$(grep -c "^COLLISION" "$CURRENT_RUN_LOG" || true)
        c_disc=$(grep -c "^DISCARD"   "$CURRENT_RUN_LOG" || true)
        c_dry=$(grep -c "^DRY"        "$CURRENT_RUN_LOG" || true)
        c_orig=$(awk '/^SUCCESS/ {s += $2} END {print s+0}' "$CURRENT_RUN_LOG")
        c_new=$(awk  '/^SUCCESS/ {s += $3} END {print s+0}' "$CURRENT_RUN_LOG")
        rm -f "$CURRENT_RUN_LOG"
    fi

    TOTAL_PROCESSED=$(( TOTAL_PROCESSED + c_proc ))
    TOTAL_SKIPPED=$(( TOTAL_SKIPPED + c_skip ))
    TOTAL_FAILED=$(( TOTAL_FAILED + c_fail ))
    TOTAL_COLLISION=$(( TOTAL_COLLISION + c_coll ))
    TOTAL_DISCARDED=$(( TOTAL_DISCARDED + c_disc ))
    TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + c_orig ))
    TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + c_new ))

    if [[ $exit_code -eq 130 ]]; then
        [[ "$DRY_RUN" == true ]] && exit 130
        PREV_ELAPSED="$tot_elapsed"
        mo_write_vars "$STATS_FILE" \
            TOTAL_PROCESSED TOTAL_SKIPPED TOTAL_FAILED TOTAL_COLLISION \
            TOTAL_DISCARDED TOTAL_ORIG_BYTES TOTAL_NEW_BYTES PREV_ELAPSED
        resolve_pending_deletes
        exit 130
    fi
    [[ "$DRY_RUN" == true ]] || rm -f "$STATS_FILE"

    if [[ "$DRY_RUN" == true ]]; then
        printf "\n%b[DRY-RUN]%b %s file(s) would be converted, %s skipped.\n" \
            "$C_CYAN" "$C_RESET" "$c_dry" "$TOTAL_SKIPPED"
        return 0
    fi

    local saved=$(( TOTAL_ORIG_BYTES - TOTAL_NEW_BYTES ))
    printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
    printf "%b║%b                        %bIMAGE SUMMARY%b                         %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
    printf "%b╠══════════════════════════════════════════════════════════════╣%b\n" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Total runtime:" "$(format_duration "$tot_elapsed")" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Converted:" "$TOTAL_PROCESSED file(s)" "$C_BLUE" "$C_RESET"
    printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Skipped:" "$TOTAL_SKIPPED file(s)" "$C_BLUE" "$C_RESET"
    (( TOTAL_DISCARDED > 0 )) && \
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

    mo_log "img" "summary: $TOTAL_PROCESSED converted, $TOTAL_SKIPPED skipped, $TOTAL_FAILED failed, $(format_bytes "$TOTAL_ORIG_BYTES") -> $(format_bytes "$TOTAL_NEW_BYTES")"
    resolve_pending_deletes
}

# Ctrl+C and SIGTERM: stop the workers first, so their results are counted
# and their temp files removed, then save the counters for a resume.
on_stop() {
    trap '' INT TERM
    printf "\n%b[ABORT]%b image conversion stopped.\n" "$C_RED" "$C_RESET"
    mo_job_stop
    finalize_stats 130
}
trap on_stop SIGINT SIGTERM

# ------------------------------------------------------------------------------
# WORKER
#
# One file per "bash -c", on the scaffold from common.sh (mo_worker_start and
# the mo_w_* helpers). Values are passed as arguments.
#
# Per file:
#   1. detect the real type from the content
#   2. claim the target name (lock), then check again for an existing output
#   3. encode into a temp file, copy PNG metadata into that temp file
#   4. verify and size-check the temp file, which is now the final artefact
#   5. move it into place without overwriting, then delete the original
# ------------------------------------------------------------------------------

_img_want_metadata() {
    [[ "$IMG_COPY_METADATA" != "never" ]] && command -v exiftool >/dev/null 2>&1
}

# _img_unique_name <dir> <stem> <ext>  -> first free <stem>[_n].<ext>
_img_unique_name() {
    local d="$1" s="$2" e="$3" n=1 c
    c="$d/$s.$e"
    while [[ -e "$c" ]]; do c="$d/${s}_$n.$e"; n=$(( n + 1 )); done
    printf '%s' "$c"
}

# _img_take_over <src> <real_type> <target_folder> <stem>
# The file is already WebP, JXL or GIF under a wrong extension: nothing to
# convert. In place it gets the correct extension; with a target directory a
# correctly named copy goes there and the source stays untouched.
_img_take_over() {
    local src="$1" real="$2" folder="$3" stem="$4" cand file
    file="$(basename -- "$src")"
    if [[ -n "${OUTPUT_DIR:-}" ]]; then
        cand="$(_img_unique_name "$folder" "$stem" "$real")"
        if ! cp -p -- "$src" "$cand"; then
            mo_w_fail "$src" "could not copy to '$cand'"; return 0
        fi
        printf "%b[CORRECTED]%b '%s' is %s -> target '%s', source unchanged\n" \
            "$C_MAGENTA" "$C_RESET" "$file" "$real" "$(basename -- "$cand")"
    else
        cand="$(_img_unique_name "$(dirname -- "$src")" "$stem" "$real")"
        mv -n -- "$src" "$cand"
        printf "%b[CORRECTED]%b '%s' is %s -> '%s'\n" \
            "$C_MAGENTA" "$C_RESET" "$file" "$real" "$(basename -- "$cand")"
    fi
    mo_log_file "$MO_LOG_TAG" "CORRECTED" "$src" "$(basename -- "$cand")"
    mo_w_result SKIP
}

# _img_unsupported <src> <reason>  - a known limitation, not an error: SKIP
# with the reason, so it does not fail every run again.
_img_unsupported() {
    mo_w_result SKIP
    mo_log_file "$MO_LOG_TAG" "UNSUPPORTED" "$1"
    printf "%b[UNSUPPORTED]%b '%s': %s, original kept.\n" \
        "$C_YELLOW" "$C_RESET" "$(basename -- "$1")" "$2" >&2
}

# _img_commit <src> <real_type> <tmp> <dest> <orig_size> <label>
_img_commit() {
    local src="$1" real="$2" tmp="$3" dest="$4" orig_size="$5" label="$6"
    local file new_size
    file="$(basename -- "$src")"

    # Metadata first, so that verification and the size check see the file
    # exactly as it will be kept. With -m, exiftool returns 0 also when
    # there was nothing to copy; anything else is a real error.
    if [[ "$real" == "png" ]] && _img_want_metadata; then
        if ! exiftool -m -overwrite_original -quiet -quiet \
                -tagsFromFile "$src" -all "$tmp" >/dev/null 2>&1; then
            mo_w_fail "$src" "could not copy metadata (exiftool)"
            return 0
        fi
    fi

    if ! verify_output_image "$tmp"; then
        mo_w_fail "$src" "output not verifiable"
        return 0
    fi

    new_size=$(stat -c%s -- "$tmp")
    if [[ "$IMG_DISCARD_IF_LARGER" == true ]] && (( orig_size > 0 && new_size >= orig_size )); then
        mo_w_result DISCARD
        mo_log_file "$MO_LOG_TAG" "DISCARDED" "$src" "" "$orig_size" "$new_size"
        printf "%b[DISCARDED]%b '%s': result would be larger, original kept.\n" \
            "$C_YELLOW" "$C_RESET" "$file"
        return 0
    fi

    mo_w_commit "$src" "$tmp" "$dest" || return 0

    mo_w_result SUCCESS "$orig_size" "$new_size"
    mo_log_file "$MO_LOG_TAG" "OK" "$src" "$(basename -- "$dest")" "$orig_size" "$new_size"
    printf "%b%s%b '%s' -> '%s'\n" "$C_GREEN" "$label" "$C_RESET" "$file" "$(basename -- "$dest")"
    # Only now, with the output in place and verified.
    if [[ "$DELETE_ORIGINAL" == true ]]; then safe_remove "$src"; fi
    return 0
}

# _img_encode <src> <real_type> <dest> <orig_size>
_img_encode() {
    local src="$1" real="$2" dest="$3" orig_size="$4"
    local tmp label errs
    local -a cmd

    mo_w_temp "$dest"; tmp="$_MO_W_TEMP"
    if [[ "$IMG_TARGET" == "webp" ]]; then
        cmd=(cwebp -quiet -m "$WEBP_FALLBACK_METHOD" -metadata all)
        if [[ "$real" == "png" && "$PNG_MODE" == "lossless" ]]; then
            cmd+=(-lossless -z 9); label="[PNG->WEBP-LOSSLESS]"
        else
            # JPEG: lossless WebP would be larger than the source, so always -q.
            cmd+=(-q "$IMG_WEBP_QUALITY"); label="[${real^^}->WEBP-q${IMG_WEBP_QUALITY}]"
        fi
        cmd+=("$src" -o "$tmp")
    elif [[ "$real" == "jpg" ]]; then
        cmd=(cjxl "$src" "$tmp" -e "$JXL_EFFORT" --num_threads="$CJXL_THREADS"
             --lossless_jpeg=1 --quiet)
        label="[JXL-LOSSLESS]"
    else
        cmd=(cjxl "$src" "$tmp" -e "$JXL_EFFORT" --num_threads="$CJXL_THREADS" --quiet)
        if [[ "$PNG_MODE" == "lossy" ]]; then
            cmd+=(-q "$PNG_QUALITY"); label="[PNG->JXL-q${PNG_QUALITY}]"
        else
            cmd+=(-d 0); label="[PNG->JXL-LOSSLESS]"
        fi
    fi
    if [[ "${cmd[0]}" == cjxl && -n "$CJXL_META_ARGS" ]]; then cmd+=("$CJXL_META_ARGS"); fi

    if "${cmd[@]}" 2>/dev/null && [[ -s "$tmp" ]]; then
        _img_commit "$src" "$real" "$tmp" "$dest" "$orig_size" "$label"
        return 0
    fi
    rm -f -- "$tmp"

    # JPEG that the encoder cannot read. CMYK is a known limitation of both
    # cjxl and cwebp: report it as unsupported instead of failing every run.
    # Anything else (truncated, damaged) stays a failure.
    if [[ "$real" == "jpg" ]]; then
        errs="$(file -b -- "$src" 2>/dev/null || true)"
        if [[ "$errs" == *"components 4"* ]]; then
            _img_unsupported "$src" "CMYK JPEG, ${cmd[0]} cannot convert it"
        else
            mo_w_fail "$src" "JPEG could not be decoded (damaged or an unsupported variant)"
        fi
        if mo_encoder_missing "${cmd[0]}"; then exit 255; fi
        return 0
    fi

    # PNG that cjxl cannot handle: lossless WebP including metadata.
    if [[ "$IMG_TARGET" == "jxl" && "$real" == "png" ]]; then
        local wdest="${dest%.*}.webp"
        if [[ -e "$wdest" ]]; then
            mo_w_conflict "$src" "JXL failed and '$(basename -- "$wdest")' already exists"
            return 0
        fi
        printf "%b[FALLBACK]%b '%s' failed -> trying WebP...\n" \
            "$C_YELLOW" "$C_RESET" "$(basename -- "$src")"
        mo_w_temp "$wdest"; tmp="$_MO_W_TEMP"
        if cwebp -quiet -lossless -z 9 -m "$WEBP_FALLBACK_METHOD" -metadata all \
                 "$src" -o "$tmp" 2>/dev/null && [[ -s "$tmp" ]]; then
            _img_commit "$src" "$real" "$tmp" "$wdest" "$orig_size" "[PNG->WEBP]"
            return 0
        fi
        rm -f -- "$tmp"
    fi

    mo_w_fail "$src" "conversion failed"
    if mo_encoder_missing cjxl || mo_encoder_missing cwebp; then exit 255; fi
    return 0
}

convert_image() {
    local src="$1"
    local dir file stem ext real orig_size target_folder dest
    dir="$(dirname -- "$src")"
    file="$(basename -- "$src")"
    stem="$(mo_stem "$file")"
    ext="${file##*.}"; ext="${ext,,}"
    if [[ "$ext" == "jpeg" ]]; then ext="jpg"; fi

    target_folder="$(mo_w_target_folder "$src")"
    dest="$target_folder/${stem}.${IMG_TARGET}"

    # Cheap check without the lock, for resumed runs. Only the configured
    # target format counts: an unrelated foo.webp must not stop foo.png from
    # becoming foo.jxl.
    if [[ "$SKIP_EXISTING" == true && -s "$dest" ]]; then
        mo_w_result SKIP; return 0
    fi

    if [[ "$DRY_RUN" == true ]]; then
        mo_w_result DRY
        printf "%b[DRY-RUN]%b '%s' -> '%s'\n" "$C_CYAN" "$C_RESET" "$file" "$(basename -- "$dest")"
        return 0
    fi

    real="$(mo_real_type "$src")"
    orig_size=$(stat -c%s -- "$src")

    mo_w_claim "$src" "$target_folder" "$stem" "$dest" || return 0

    case "$real" in
        jpg|png) ;;
        webp|jxl|gif) _img_take_over "$src" "$real" "$target_folder" "$stem"; return 0 ;;
        *) mo_w_fail "$src" "not a JPEG or PNG ($(file -b --mime-type -- "$src" 2>/dev/null || echo unknown))"
           return 0 ;;
    esac

    if [[ "$real" != "$ext" && -n "${OUTPUT_DIR:-}" ]]; then
        printf "%b[CORRECTED]%b '%s' is actually %s, converted as such, source unchanged\n" \
            "$C_MAGENTA" "$C_RESET" "$file" "$real"
    fi

    _img_encode "$src" "$real" "$dest" "$orig_size"

    # In place, a kept original gets the extension that matches its content.
    # This runs after the conversion, so the worker never has to follow a
    # renamed source. Not after a successful conversion with deletion: if
    # the trash failed, the original sits in the pending-delete list under
    # its current name, and a rename would make that entry miss it.
    local doomed=false
    if [[ "$_MO_W_RESULT" == "SUCCESS" && "$DELETE_ORIGINAL" == true ]]; then doomed=true; fi
    if [[ "$real" != "$ext" && -z "${OUTPUT_DIR:-}" && "$doomed" == false && -e "$src" ]]; then
        local cand; cand="$(_img_unique_name "$dir" "$stem" "$real")"
        if mv -n -- "$src" "$cand" && [[ ! -e "$src" ]]; then
            printf "%b[CORRECTED]%b '%s' (%s) -> '%s'\n" \
                "$C_MAGENTA" "$C_RESET" "$file" "$real" "$(basename -- "$cand")"
            mo_log_file "$MO_LOG_TAG" "CORRECTED" "$src" "$(basename -- "$cand")"
        fi
    fi
    return 0
}

export SOURCE_DIR OUTPUT_DIR SKIP_EXISTING DELETE_ORIGINAL
export JXL_EFFORT PNG_MODE PNG_QUALITY WEBP_FALLBACK_METHOD CJXL_THREADS CJXL_META_ARGS
export IMG_TARGET IMG_WEBP_QUALITY IMG_DISCARD_IF_LARGER IMG_COPY_METADATA
export -f convert_image _img_encode _img_commit _img_take_over _img_unsupported \
    _img_unique_name _img_want_metadata

run_batch() {
    mo_find_files "$SOURCE_DIR" -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" |
        xargs -0 -r -n 1 -P "$MAX_WORKERS" \
            bash -c 'mo_worker_start "$1"; convert_image "$1"' _
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
