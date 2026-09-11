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
#     write the same file.
#   - Temp files carry PID and a random number so parallel workers never
#     share one.
#   - JPEG is transcoded bit-exactly lossless in the JXL path. PNG_MODE only
#     affects PNG.
#   - With a target directory the source directory is never modified, not
#     even when an extension has to be corrected.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
_MO_ROOT="$(dirname "$SCRIPT_DIR")"; load_config
mo_install_exit_handler

usage() {
    cat <<'EOF'
img-to-jxl.sh - converts JPG/PNG to JPEG XL or WebP

  -i, --input   <dir>    source directory
  -o, --output  <dir>    target directory (empty = in place)
  -j, --workers <n>      parallel workers (default: nproc)
      --target <f>       jxl | webp (default: jxl)
      --webp-quality <q> quality for lossy WebP (default: 85)
      --discard-larger   discard the result if larger than the original
  -e, --effort  <1-9>    JXL effort (default: 7)
      --png-mode  <m>    lossless | lossy (default: lossless)
      --png-quality <q>  only with --png-mode lossy (default: 90)
      --cjxl-threads <n> threads per cjxl process (default: 1)
      --delete           move originals to the trash after success
      --no-delete        keep originals
      --force-delete     if the trash fails, rm without asking
      --verify-deep      fully decode the output (slower, safer)
      --log <file>       log file
      --no-log           do not write a log
      --hold             keep the window open at the end
      --no-hold          never keep the window open
  -n, --dry-run          preview only, write nothing
  -h, --help             this help

Positional arguments are still accepted: img-to-jxl.sh <input> [output]
EOF
}

# ------------------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------------------
SOURCE_DIR="${SOURCE_DIR:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
MAX_WORKERS="${MAX_WORKERS:-$(nproc)}"
# No fixed default: the preset depends on whether a target
# Zielverzeichnis angegeben wurde (siehe mo_apply_inplace_defaults).
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
PNG_MODE="${PNG_MODE:-lossless}"
PNG_QUALITY="${PNG_QUALITY:-90}"
DRY_RUN="${DRY_RUN:-false}"
VERIFY_DEEP="${VERIFY_DEEP:-false}"
# Each worker is its own cjxl process. By default cjxl would start as
# many threads as there are cores (16 workers x 16 threads
# on an 8C/16T chip). One thread per process avoids the oversubscription.
CJXL_THREADS="${CJXL_THREADS:-1}"
SKIP_EXISTING=true
WEBP_FALLBACK_METHOD=6

POSITIONAL=()
while (( $# > 0 )); do
    case "$1" in
        -i|--input)       SOURCE_DIR="$2"; shift 2 ;;
        -o|--output)      OUTPUT_DIR="$2"; shift 2 ;;
        -j|--workers)     MAX_WORKERS="$2"; shift 2 ;;
        -e|--effort)      JXL_EFFORT="$2"; shift 2 ;;
        --target)         IMG_TARGET="$2"; shift 2 ;;
        --webp-quality)   IMG_WEBP_QUALITY="$2"; shift 2 ;;
        --discard-larger) IMG_DISCARD_IF_LARGER=true; shift ;;
        --png-mode)       PNG_MODE="$2"; shift 2 ;;
        --png-quality)    PNG_QUALITY="$2"; shift 2 ;;
        --delete)         DELETE_ORIGINAL=true; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --no-delete)      DELETE_ORIGINAL=false; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --force-delete)   FORCE_DELETE=true; shift ;;
        --cjxl-threads)   CJXL_THREADS="$2"; shift 2 ;;
        --verify-deep)    VERIFY_DEEP=true; shift ;;
        -n|--dry-run)     DRY_RUN=true; shift ;;
        --log)            MO_LOG_FILE="$2"; shift 2 ;;
        --no-log)         MO_LOG=false; shift ;;
        --hold)           MO_HOLD=1; shift ;;
        --no-hold)        MO_HOLD=0; shift ;;
        -h|--help)        usage; exit 0 ;;
        --)               shift; while (( $# > 0 )); do POSITIONAL+=("$1"); shift; done ;;
        -*)               echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)                POSITIONAL+=("$1"); shift ;;
    esac
done
[[ -n "${POSITIONAL[0]:-}" ]] && SOURCE_DIR="${POSITIONAL[0]}"
[[ -n "${POSITIONAL[1]:-}" ]] && OUTPUT_DIR="${POSITIONAL[1]}"

if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" ]]; then
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    printf "%bImage converter: JPG/PNG -> JXL (with WebP fallback)%b\n" "$C_BOLD" "$C_RESET"
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    read -rp "Run with the default settings? [Y/n]: " start_choice || start_choice=""
    if [[ "${start_choice,,}" =~ ^(n|nein|no)$ ]]; then
        read -rp "  Source directory [$SOURCE_DIR]: " x && SOURCE_DIR="${x:-$SOURCE_DIR}"
        read -rp "  Target directory (empty = in place) [$OUTPUT_DIR]: " x && OUTPUT_DIR="${x:-$OUTPUT_DIR}"
        read -rp "  Move originals to trash? [y/N]: " x
        [[ "${x,,}" =~ ^(j|ja|y|yes)$ ]] && DELETE_ORIGINAL=true || DELETE_ORIGINAL=false
        read -rp "  JXL effort (1-9) [$JXL_EFFORT]: " x && JXL_EFFORT="${x:-$JXL_EFFORT}"
        read -rp "  PNG mode (lossless/lossy) [$PNG_MODE]: " x && PNG_MODE="${x:-$PNG_MODE}"
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
export MO_LOG_TAG="img"
mo_log_init "img-to-jxl.sh" "$SOURCE_DIR" "$OUTPUT_DIR"

require_cmds cjxl cwebp file || exit 1
[[ "$PNG_MODE" == "lossless" || "$PNG_MODE" == "lossy" ]] || {
    echo "--png-mode muss 'lossless' oder 'lossy' sein." >&2; exit 2; }
[[ "$IMG_TARGET" == "jxl" || "$IMG_TARGET" == "webp" ]] || {
    echo "--target muss 'jxl' oder 'webp' sein." >&2; exit 2; }
[[ "$IMG_TARGET" == "webp" ]] && printf "%b[IMAGES]%b target: WebP (JPEG lossy at q%s, PNG %s)\n" \
    "$C_CYAN" "$C_RESET" "$IMG_WEBP_QUALITY" "$PNG_MODE"

[[ "$DRY_RUN" == true ]] && printf "%b[DRY-RUN]%b Nothing will be written or deleted.\n" "$C_CYAN" "$C_RESET"

cleanup_stale_parts "${OUTPUT_DIR:-$SOURCE_DIR}" "*.part.*.jxl" "*.part.*.webp"

# ------------------------------------------------------------------------------
# STATISTICS
# ------------------------------------------------------------------------------
STATS_FILE="${SOURCE_DIR}/.img_stats.env"
PENDING_DELETE_LOG="${SOURCE_DIR}/.img_pending_deletes.txt"
CURRENT_RUN_LOG=$(mktemp /tmp/img_stats_XXXXXX)
export CURRENT_RUN_LOG PENDING_DELETE_LOG DRY_RUN VERIFY_DEEP FORCE_DELETE

if [[ "${RESUMING:-false}" == "true" && -f "$STATS_FILE" ]]; then
    source "$STATS_FILE" 2>/dev/null || true
else
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
        cat > "$STATS_FILE" <<EOF
TOTAL_PROCESSED=$TOTAL_PROCESSED
TOTAL_SKIPPED=$TOTAL_SKIPPED
TOTAL_FAILED=$TOTAL_FAILED
TOTAL_COLLISION=$TOTAL_COLLISION
TOTAL_DISCARDED=$TOTAL_DISCARDED
TOTAL_ORIG_BYTES=$TOTAL_ORIG_BYTES
TOTAL_NEW_BYTES=$TOTAL_NEW_BYTES
PREV_ELAPSED=$tot_elapsed
EOF
        resolve_pending_deletes
        exit 130
    fi
    rm -f "$STATS_FILE"

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

trap 'printf "\n%b[ABORT]%b image conversion stopped.\n" "$C_RED" "$C_RESET"; finalize_stats 130' SIGINT SIGTERM

# ------------------------------------------------------------------------------
# WORKER
# ------------------------------------------------------------------------------
# Wrapper: computes the target names and claims them with a lock directory
# concurrent access by a second worker (e.g. foo.jpg + foo.png,
# which would both map to foo.jxl).
convert_image() {
    local src="$1"
    local orig_size; orig_size=$(stat -c%s "$src" 2>/dev/null || echo 0)
    local dir; dir="$(dirname "$src")"
    local file; file="$(basename "$src")"
    local stem="${file%.*}"
    local ext="${file##*.}"; ext="${ext,,}"

    local target_folder dest_jxl dest_webp
    if [[ -n "${OUTPUT_DIR:-}" ]]; then
        local rel_dir; rel_dir="$(dirname "${src#"$SOURCE_DIR"/}")"
        [[ "$rel_dir" == "." ]] && target_folder="$OUTPUT_DIR" || target_folder="$OUTPUT_DIR/$rel_dir"
        [[ "$DRY_RUN" == true ]] || mkdir -p "$target_folder"
    else
        target_folder="$dir"
    fi
    dest_jxl="$target_folder/${stem}.jxl"
    dest_webp="$target_folder/${stem}.webp"

    if [[ "$SKIP_EXISTING" == true && ( -s "$dest_jxl" || -s "$dest_webp" ) ]]; then
        echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
    fi

    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        echo "DRY" >> "$CURRENT_RUN_LOG"
        printf "%b[DRY-RUN]%b '%s' -> '%s'\n" "$C_CYAN" "$C_RESET" "$file" "$(basename "$dest_jxl")"
        return 0
    fi

    # Unique temp names: two workers must never share the same .part file
    local uniq="$$.${RANDOM}"
    local temp_jxl="${dest_jxl}.part.${uniq}.jxl"
    local temp_webp="${dest_webp}.part.${uniq}.webp"

    # Zielnamen exklusiv beanspruchen
    local lockdir="$target_folder/.${stem}.molock"
    if ! mkdir "$lockdir" 2>/dev/null; then
        echo "COLLISION" >> "$CURRENT_RUN_LOG"
        mo_log_file "$MO_LOG_TAG" "CONFLICT" "$src"
        printf "%b[CONFLICT]%b '%s': target name '%s' already claimed, original kept.\n" \
            "$C_YELLOW" "$C_RESET" "$file" "$(basename "$dest_jxl")" >&2
        return 0
    fi

    local rc=0
    _convert_image_locked || rc=$?
    rmdir "$lockdir" 2>/dev/null || true
    return $rc
}

# Handles a file whose extension does not match its actual content.
#
# In place: the source file is renamed, which is the whole point.
# With a target directory: THE SOURCE STAYS UNTOUCHED. Everything that
# should be corrected lands under the correct name in the target. Otherwise
# a separate target directory would be pointless, because the source
# veraendert wuerde.
#
# Returns: 0 = handled (caller should return)
#            1 = weiterverarbeiten, Variablen src/file/ext wurden angepasst
_handle_wrong_extension() {
    local real_mime="$1" correct_ext="$2"
    local cand n=1

    if [[ -n "${OUTPUT_DIR:-}" ]]; then
        cand="$target_folder/${stem}.${correct_ext}"
        while [[ -e "$cand" ]]; do cand="$target_folder/${stem}_${n}.${correct_ext}"; n=$(( n + 1 )); done
        printf "%b[CORRECTED]%b '%s' (%s) -> target '%s', source unchanged\n" \
            "$C_MAGENTA" "$C_RESET" "$file" "$real_mime" "$(basename "$cand")"

        case "$correct_ext" in
            webp|jxl|gif)
                # Bereits ein modernes Format: unveraendert uebernehmen.
                cp -p "$src" "$cand"
                mo_log_file "$MO_LOG_TAG" "CORRECTED" "$src" "$(basename "$cand")"
                echo "SKIP" >> "$CURRENT_RUN_LOG"
                return 0 ;;
            png|jpg)
                # Keep processing, but from the unchanged source.
                ext="$correct_ext"
                return 1 ;;
        esac
        echo "SKIP" >> "$CURRENT_RUN_LOG"
        return 0
    fi

    # In-Place: umbenennen
    cand="$dir/${stem}.${correct_ext}"
    while [[ -e "$cand" ]]; do cand="$dir/${stem}_${n}.${correct_ext}"; n=$(( n + 1 )); done
    mv "$src" "$cand"
    printf "%b[CORRECTED]%b '%s' (%s) -> '%s'\n" \
        "$C_MAGENTA" "$C_RESET" "$file" "$real_mime" "$(basename "$cand")"
    mo_log_file "$MO_LOG_TAG" "CORRECTED" "$src" "$(basename "$cand")"

    case "$correct_ext" in
        webp|jxl|gif) echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0 ;;
    esac
    src="$cand"; file="$(basename "$src")"; ext="$correct_ext"
    return 1
}

# Inner function: uses the wrapper's locals (dynamic scoping).
_convert_image_locked() {
    local new_size

    _commit() {   # _commit <tempdatei> <zieldatei> <label>
        local tmp="$1" dest="$2" label="$3"
        if ! verify_output_image "$tmp"; then
            rm -f "$tmp"
            echo "FAIL" >> "$CURRENT_RUN_LOG"
            printf "%b[ERROR]%b output not verifiable: '%s' (original kept)\n" \
                "$C_RED" "$C_RESET" "$file" >&2
            return 1
        fi
        new_size=$(stat -c%s "$tmp" 2>/dev/null || echo 0)
        if [[ "$IMG_DISCARD_IF_LARGER" == true ]] && (( orig_size > 0 && new_size >= orig_size )); then
            rm -f "$tmp"
            echo "DISCARD" >> "$CURRENT_RUN_LOG"
            mo_log_file "$MO_LOG_TAG" "DISCARDED" "$src" "" "$orig_size" "$new_size"
            printf "%b[DISCARDED]%b '%s': result would be larger, original kept.\n" \
                "$C_YELLOW" "$C_RESET" "$file"
            return 1
        fi
        mv "$tmp" "$dest"
        touch -r "$src" "$dest"
        echo "SUCCESS $orig_size $new_size" >> "$CURRENT_RUN_LOG"
        [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src"
        mo_log_file "$MO_LOG_TAG" "OK" "$src" "$(basename "$dest")" "$orig_size" "$new_size"
        printf "%b%s%b '%s' -> '%s'\n" "$C_GREEN" "$label" "$C_RESET" "$file" "$(basename "$dest")"
        return 0
    }

    # ---------------- Zielformat WebP ----------------
    if [[ "$IMG_TARGET" == "webp" ]]; then
        local -a webp_args=(-quiet -m "$WEBP_FALLBACK_METHOD" -metadata all)
        local label
        if [[ "$ext" == "png" && "$PNG_MODE" == "lossless" ]]; then
            webp_args+=(-lossless -z 9); label="[PNG->WEBP-LOSSLESS]"
        elif [[ "$ext" == "png" ]]; then
            webp_args+=(-q "$IMG_WEBP_QUALITY"); label="[PNG->WEBP-q${IMG_WEBP_QUALITY}]"
        else
            # JPEG: lossless would be larger than the source, so always -q.
            webp_args+=(-q "$IMG_WEBP_QUALITY"); label="[JPG->WEBP-q${IMG_WEBP_QUALITY}]"
        fi
        if cwebp "${webp_args[@]}" "$src" -o "$temp_webp" 2>/dev/null && [[ -s "$temp_webp" ]]; then
            _commit "$temp_webp" "$dest_webp" "$label" && return 0
            return 1
        fi
        rm -f "$temp_webp"
        # Failed: usually a wrong extension.
        local real_mime; real_mime=$(file -b --mime-type "$src" 2>/dev/null || echo unknown)
        local correct_ext=""
        case "$real_mime" in
            image/webp) correct_ext="webp" ;; image/png)  correct_ext="png" ;;
            image/jxl)  correct_ext="jxl"  ;; image/gif)  correct_ext="gif" ;;
            image/jpeg) correct_ext="jpg"  ;;
        esac
        if [[ -n "$correct_ext" && "$correct_ext" != "$ext" ]]; then
            _handle_wrong_extension "$real_mime" "$correct_ext" && return 0
            if cwebp -quiet -m "$WEBP_FALLBACK_METHOD" -metadata all \
                     -q "$IMG_WEBP_QUALITY" "$src" -o "$temp_webp" 2>/dev/null \
               && [[ -s "$temp_webp" ]]; then
                _commit "$temp_webp" "$dest_webp" "[WEBP-q${IMG_WEBP_QUALITY}]" && return 0
                return 1
            fi
            rm -f "$temp_webp"
        fi
        printf "%b[ERROR]%b Konvertierung failed: '%s'\n" "$C_RED" "$C_RESET" "$file" >&2
        mo_log_file "$MO_LOG_TAG" "ERROR" "$src"
        echo "FAIL" >> "$CURRENT_RUN_LOG"; return 1
    fi


    # ---------------- JPEG: verlustfreies JXL-Recompress ----------------
    if [[ "$ext" == "jpg" || "$ext" == "jpeg" ]]; then
        if cjxl "$src" "$temp_jxl" -e "$JXL_EFFORT" --num_threads="$CJXL_THREADS" --quiet 2>/dev/null && [[ -s "$temp_jxl" ]]; then
            _commit "$temp_jxl" "$dest_jxl" "[JXL-LOSSLESS]" && return 0
            return 1
        fi
        rm -f "$temp_jxl"

        # Wrong extension? Check the MIME type and correct it.
        local real_mime; real_mime=$(file -b --mime-type "$src" 2>/dev/null || echo unknown)
        if [[ "$real_mime" != "image/jpeg" ]]; then
            local correct_ext=""
            case "$real_mime" in
                image/webp) correct_ext="webp" ;; image/png) correct_ext="png" ;;
                image/jxl)  correct_ext="jxl"  ;; image/gif) correct_ext="gif" ;;
            esac
            if [[ -n "$correct_ext" ]]; then
                _handle_wrong_extension "$real_mime" "$correct_ext" && return 0
            fi
        fi
        if [[ "$ext" != "png" ]]; then
            printf "%b[ERROR]%b JXL-Transcoding failed: '%s'\n" "$C_RED" "$C_RESET" "$file" >&2
            echo "FAIL" >> "$CURRENT_RUN_LOG"; return 1
        fi
    fi

    # ---------------- PNG ----------------
    if [[ "$ext" == "png" ]]; then
        local -a cjxl_args=(-e "$JXL_EFFORT" --num_threads="$CJXL_THREADS" --quiet)
        local label="[PNG->JXL-LOSSLESS]"
        if [[ "$PNG_MODE" == "lossy" ]]; then
            cjxl_args+=(-q "$PNG_QUALITY"); label="[PNG->JXL-q${PNG_QUALITY}]"
        else
            cjxl_args+=(-d 0)
        fi

        if cjxl "$src" "$temp_jxl" "${cjxl_args[@]}" 2>/dev/null && [[ -s "$temp_jxl" ]]; then
            _commit "$temp_jxl" "$dest_jxl" "$label" && return 0
            return 1
        fi
        rm -f "$temp_jxl"

        local real_mime; real_mime=$(file -b --mime-type "$src" 2>/dev/null || echo unknown)
        if [[ "$real_mime" != "image/png" ]]; then
            local correct_ext=""
            case "$real_mime" in
                image/jpeg) correct_ext="jpg" ;; image/webp) correct_ext="webp" ;;
                image/jxl)  correct_ext="jxl" ;; image/gif)  correct_ext="gif"  ;;
            esac
            if [[ -n "$correct_ext" ]]; then
                _handle_wrong_extension "$real_mime" "$correct_ext" && return 0
                # It is actually a JPEG: transcode losslessly.
                if [[ "$ext" == "jpg" ]]; then
                    if cjxl "$src" "$temp_jxl" -e "$JXL_EFFORT" \
                            --num_threads="$CJXL_THREADS" --quiet 2>/dev/null \
                       && [[ -s "$temp_jxl" ]]; then
                        _commit "$temp_jxl" "$dest_jxl" "[JXL-LOSSLESS]" && return 0
                        return 1
                    fi
                    rm -f "$temp_jxl"
                fi
                echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
            fi
        fi

        # Fallback: verlustfreies WebP inkl. Metadaten
        printf "%b[FALLBACK]%b '%s' failed -> trying WebP...\n" "$C_YELLOW" "$C_RESET" "$file"
        if cwebp -quiet -lossless -z 9 -m "$WEBP_FALLBACK_METHOD" -metadata all \
                 "$src" -o "$temp_webp" 2>/dev/null && [[ -s "$temp_webp" ]]; then
            _commit "$temp_webp" "$dest_webp" "[PNG->WEBP]" && return 0
            return 1
        fi
        rm -f "$temp_webp"
        printf "%b[ERROR]%b Konvertierung failed: '%s'\n" "$C_RED" "$C_RESET" "$file" >&2
        mo_log_file "$MO_LOG_TAG" "ERROR" "$src"
        echo "FAIL" >> "$CURRENT_RUN_LOG"; return 1
    fi

    echo "FAIL" >> "$CURRENT_RUN_LOG"; return 1
}

export SOURCE_DIR OUTPUT_DIR SKIP_EXISTING DELETE_ORIGINAL
export JXL_EFFORT PNG_MODE PNG_QUALITY WEBP_FALLBACK_METHOD CJXL_THREADS
export IMG_TARGET IMG_WEBP_QUALITY IMG_DISCARD_IF_LARGER
export -f convert_image _convert_image_locked _handle_wrong_extension

exit_code=0
find "$SOURCE_DIR" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" \) -print0 |
    xargs -0 -r -n 1 -P "$MAX_WORKERS" bash -c 'convert_image "$1"' _ || exit_code=$?

# xargs reports 124/125 on abort, 123 on worker errors (not an abort)
if (( exit_code == 124 || exit_code == 125 || exit_code == 130 )); then
    finalize_stats 130
else
    finalize_stats 0
fi
