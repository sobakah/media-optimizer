#!/bin/bash
# ==============================================================================
# verify-output.sh  -  check a target directory against the originals
#
# Checks every output file in three stages and reports which ones do not match
# their original. Optionally it deletes the affected outputs and drops the
# matching sources from the cache, so a repair run regenerates exactly those.
#
# MATCHING output -> original is the relative path, because h264-to-h265.sh
# mirrors the folder structure in target mode and keeps the file name.
#
# CHECK STAGES (in this order, the first mismatch wins)
#   UNREADABLE  ffprobe finds no video stream
#   DURATION    runtime deviates beyond DURATION_TOL
#   PICTURE?    PSNR samples fall below PSNR_MIN
#
# IMPORTANT: a low PSNR is a suspicion, not proof. Without --fix the script
# only reports and changes nothing.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
_MO_ROOT="$(dirname "$SCRIPT_DIR")"; load_config
mo_install_exit_handler

usage() {
    cat <<'EOF'
verify-output.sh - checks converted videos against their originals

  -i, --input   <dir>   source directory (the originals)
  -o, --output  <dir>   target directory (the converted files)
  -j, --workers <n>     parallel checks (default: nproc)
      --psnr-min <db>   threshold for suspicion (default: 15)
      --duration-tol <p> allowed runtime deviation in percent (default: 2).
                        Salvaged files from damaged sources are legitimately
                        shorter; a higher value helps there.
      --samples <n>     samples per file (default: 3)
      --extensions <l>  output extensions to check, space separated
      --no-visual       skip the picture comparison, only read and duration
      --keep-samples <d> store the compared stills in <d> for inspection
      --fix             delete broken outputs and drop them from the cache
      --run             after --fix, start h264-to-h265.sh again
      --hold            keep the window open at the end
      --no-hold         never keep the window open
  -h, --help            this help

Examples:
  ./verify-output.sh -i ~/Videos -o /mnt/archive            # check only
  ./verify-output.sh -i ~/Videos -o /mnt/archive --fix --run
EOF
}

SOURCE_DIR="${SOURCE_DIR:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
MAX_WORKERS="${MAX_WORKERS:-$(nproc)}"
PSNR_MIN="${VISUAL_PSNR_MIN:-15}"
SAMPLES="${SAMPLES:-3}"
VISUAL_KEEP_DIR="${VISUAL_KEEP_DIR:-}"
DURATION_TOL="${DURATION_TOLERANCE_PCT:-2}"
CHECK_VISUAL="${CHECK_VISUAL:-true}"
# Same extension list as the converter, otherwise MKV outputs from a
# VIDEO_CONTAINER=auto run would silently not be checked at all.
VIDEO_EXTENSIONS="${VIDEO_EXTENSIONS:-mp4 m4v mov mkv webm avi ts m2ts wmv flv}"
DO_FIX=false
DO_RUN=false

POSITIONAL=()
while (( $# > 0 )); do
    case "$1" in
        -i|--input)     SOURCE_DIR="$2"; shift 2 ;;
        -o|--output)    OUTPUT_DIR="$2"; shift 2 ;;
        -j|--workers)   MAX_WORKERS="$2"; shift 2 ;;
        --psnr-min)     PSNR_MIN="$2"; shift 2 ;;
        --duration-tol) DURATION_TOL="$2"; shift 2 ;;
        --samples)      SAMPLES="$2"; shift 2 ;;
        --keep-samples) VISUAL_KEEP_DIR="$2"; shift 2 ;;
        --no-visual)    CHECK_VISUAL=false; shift ;;
        --extensions)   VIDEO_EXTENSIONS="$2"; shift 2 ;;
        --fix)          DO_FIX=true; shift ;;
        --run)          DO_RUN=true; shift ;;
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

# ------------------------------------------------------------------------------
# INTERACTIVE SETUP
# Only when there is a terminal and the script was not started by another one.
# --keep-samples is offered only if the picture comparison is actually going
# to run; without it there would be no stills to store.
# ------------------------------------------------------------------------------
if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" ]]; then
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    printf "%bOutput check: compare converted videos with their originals%b\n" "$C_BOLD" "$C_RESET"
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    read -rp "Run with the default settings? [Y/n]: " start_choice || start_choice=""
    if [[ "${start_choice,,}" =~ ^(n|no|nein)$ ]]; then
        read -rp "  Source directory (the originals) [$SOURCE_DIR]: " x && SOURCE_DIR="${x:-$SOURCE_DIR}"
        read -rp "  Target directory (the converted files) [$OUTPUT_DIR]: " x && OUTPUT_DIR="${x:-$OUTPUT_DIR}"

        mo_prompt_bool "Compare the picture content as well (PSNR sampling)?" \
            "$CHECK_VISUAL" CHECK_VISUAL

        if [[ "$CHECK_VISUAL" == true ]]; then
            read -rp "  PSNR threshold in dB [$PSNR_MIN]: " x && PSNR_MIN="${x:-$PSNR_MIN}"
            read -rp "  Samples per file [$SAMPLES]: " x && SAMPLES="${x:-$SAMPLES}"

            keep_choice=false
            mo_prompt_bool "Store the compared stills for inspection?" false keep_choice
            if [[ "$keep_choice" == true ]]; then
                read -rp "  Directory for the stills [${VISUAL_KEEP_DIR:-/tmp/mo-samples}]: " x
                VISUAL_KEEP_DIR="${x:-${VISUAL_KEEP_DIR:-/tmp/mo-samples}}"
            fi
        fi

        read -rp "  Allowed runtime deviation in percent [$DURATION_TOL]: " x && DURATION_TOL="${x:-$DURATION_TOL}"
        mo_prompt_bool "Delete broken outputs and drop them from the cache?" "$DO_FIX" DO_FIX
        [[ "$DO_FIX" == true ]] && mo_prompt_bool "Start the repair run afterwards?" "$DO_RUN" DO_RUN
        echo ""
    fi
fi

# --keep-samples without the picture comparison would never produce anything.
if [[ "$CHECK_VISUAL" != true && -n "$VISUAL_KEEP_DIR" ]]; then
    printf "%b[NOTE]%b --keep-samples has no effect without the picture comparison.\n" \
        "$C_YELLOW" "$C_RESET" >&2
    VISUAL_KEEP_DIR=""
fi

[[ -n "$SOURCE_DIR" && -n "$OUTPUT_DIR" ]] || {
    echo "Both source and target directory are required." >&2; usage >&2; exit 2; }
[[ -d "$SOURCE_DIR" ]] || { echo "Source directory not found: $SOURCE_DIR" >&2; exit 1; }
[[ -d "$OUTPUT_DIR" ]] || { echo "Target directory not found: $OUTPUT_DIR" >&2; exit 1; }
SOURCE_DIR="${SOURCE_DIR%/}"; OUTPUT_DIR="${OUTPUT_DIR%/}"
[[ "$SOURCE_DIR" == "$OUTPUT_DIR" ]] && {
    echo "Source and target are identical. This script is not meant for in-place mode." >&2
    exit 2; }

require_cmds ffmpeg ffprobe || exit 1

RESULT_LOG=$(mktemp /tmp/mo_verify_XXXXXX)
BROKEN_LIST="${OUTPUT_DIR}/.defekte_videos.txt"
VISUAL_SAMPLES="$SAMPLES"
export RESULT_LOG PSNR_MIN SAMPLES VISUAL_SAMPLES VISUAL_KEEP_DIR DURATION_TOL
export CHECK_VISUAL SOURCE_DIR OUTPUT_DIR VIDEO_EXTENSIONS

printf "%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b                     %bOUTPUT CHECK (VIDEO)%b                     %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_BLUE" "$C_RESET"
printf "  Source: %s\n  Target: %s\n  picture check: %s, runtime tolerance %s%%, %s workers\n\n" \
    "$SOURCE_DIR" "$OUTPUT_DIR" \
    "$([[ "$CHECK_VISUAL" == true ]] && echo "$PSNR_MIN dB, $SAMPLES samples${VISUAL_KEEP_DIR:+, stills -> $VISUAL_KEEP_DIR}" || echo "off")" \
    "$DURATION_TOL" "$MAX_WORKERS"

# ------------------------------------------------------------------------------
# WORKER
# ------------------------------------------------------------------------------
# Compares one output file with its original. Files are matched by
# relative path, because h264-to-h265.sh mirrors the folder structure
# and keeps the file name.
check_one() {
    local out="$1"
    local rel="${out#"$OUTPUT_DIR"/}"
    local src="$SOURCE_DIR/$rel"

    # The container may differ from the source (VIDEO_CONTAINER=auto turns an
    # .avi into an .mkv), so fall back to matching on the base name.
    if [[ ! -f "$src" ]]; then
        local base="${rel%.*}" cand
        for cand in "$SOURCE_DIR/$base".*; do
            [[ -f "$cand" ]] || continue
            src="$cand"; break
        done
    fi

    if [[ ! -f "$src" ]]; then
        printf 'NOSRC\t%s\t\t\n' "$out" >> "$RESULT_LOG"
        return 0
    fi
    if ! ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
         -of default=noprint_wrappers=1:nokey=1 "$out" >/dev/null 2>&1; then
        printf 'UNREADABLE\t%s\t%s\t\n' "$out" "$src" >> "$RESULT_LOG"
        printf "%b[UNREADABLE]%b %s\n" "$C_RED" "$C_RESET" "$rel" >&2
        return 0
    fi
    if ! verify_video "$src" "$out" "$DURATION_TOL"; then
        local d_src d_out
        d_src=$(media_duration "$src"); d_out=$(media_duration "$out")
        printf 'DURATION\t%s\t%s\t%s\n' "$out" "$src" "${d_out:-?}" >> "$RESULT_LOG"
        printf "%b[DURATION]%b    %s  (original %ss, output %ss, tolerance %s%%)\n" \
            "$C_RED" "$C_RESET" "$rel" "${d_src:-?}" "${d_out:-?}" "$DURATION_TOL" >&2
        return 0
    fi
    if [[ "$CHECK_VISUAL" == true ]] && ! verify_visual "$src" "$out" "$PSNR_MIN"; then
        printf 'VISUAL\t%s\t%s\t%s\n' "$out" "$src" "$VISUAL_PSNR" >> "$RESULT_LOG"
        printf "%b[PICTURE?]%b     %s  (best %s dB; samples: %s)\n" \
            "$C_YELLOW" "$C_RESET" "$rel" "$VISUAL_PSNR" "${VISUAL_PSNR_ALL:-none}" >&2
        return 0
    fi
    printf 'OK\t%s\t%s\t\n' "$out" "$src" >> "$RESULT_LOG"
    printf "%b[OK]%b       %s\n" "$C_GREEN" "$C_RESET" "$rel"
    return 0
}
export -f check_one verify_video verify_visual media_duration

FIND_OPTS=(-type f \()
_first=true
for _ext in $VIDEO_EXTENSIONS; do
    [[ "$_first" == true ]] || FIND_OPTS+=(-o)
    FIND_OPTS+=(-iname "*.${_ext}")
    _first=false
done
FIND_OPTS+=(\) ! -name "*.part.*")

TOTAL=$(find "$OUTPUT_DIR" "${FIND_OPTS[@]}" 2>/dev/null | wc -l || true)
if (( TOTAL == 0 )); then
    echo "No matching video files found in the target directory."
    rm -f "$RESULT_LOG"
    exit 0
fi
printf "  checking %d file(s)...\n\n" "$TOTAL"

find "$OUTPUT_DIR" "${FIND_OPTS[@]}" -print0 |
    xargs -0 -r -n 1 -P "$MAX_WORKERS" bash -c 'check_one "$1"' _ || true

# ------------------------------------------------------------------------------
# SUMMARY
# ------------------------------------------------------------------------------
n_ok=$(grep -c '^OK' "$RESULT_LOG" || true)
n_vis=$(grep -c '^VISUAL' "$RESULT_LOG" || true)
n_dur=$(grep -c '^DURATION' "$RESULT_LOG" || true)
n_unr=$(grep -c '^UNREADABLE' "$RESULT_LOG" || true)
n_nosrc=$(grep -c '^NOSRC' "$RESULT_LOG" || true)
n_bad=$(( n_vis + n_dur + n_unr ))

printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b                            %bRESULT%b                            %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
printf "%b╠══════════════════════════════════════════════════════════════╣%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Fine:" "$n_ok file(s)" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Picture suspicious:" "$n_vis file(s)" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Runtime mismatch:" "$n_dur file(s)" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "Unreadable:" "$n_unr file(s)" "$C_BLUE" "$C_RESET"
printf "%b║%b  %-21s %-37s %b║%b\n" "$C_BLUE" "$C_RESET" "No original:" "$n_nosrc file(s)" "$C_BLUE" "$C_RESET"
printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_BLUE" "$C_RESET"

if (( n_nosrc > 0 )); then
    printf "\n%b[NOTE]%b %d output(s) without a matching original. They are left\n" \
        "$C_YELLOW" "$C_RESET" "$n_nosrc"
    printf "          untouched, since there is nothing to compare or regenerate.\n"
fi

if (( n_bad == 0 )); then
    printf "\n%b[DONE]%b No broken files found.\n" "$C_GREEN" "$C_RESET"
    rm -f "$RESULT_LOG" "$BROKEN_LIST"
    exit 0
fi

# Save the list of affected source files
awk -F'\t' '/^(VISUAL|DURATION|UNREADABLE)/ && $3 != "" {print $3}' "$RESULT_LOG" | sort -u > "$BROKEN_LIST"
printf "\n%b%d broken file(s).%b List of affected originals: %s\n" \
    "$C_BOLD" "$n_bad" "$C_RESET" "$BROKEN_LIST"

if [[ "$DO_FIX" != true ]]; then
    printf "\n%b[IMPORTANT]%b A low PSNR is a suspicion, not proof. First play back a few\n" "$C_YELLOW" "$C_RESET"
    printf "          of the reported files yourself. Store the compared stills\n"
    printf "          with: --keep-samples /tmp/samples\n"
    printf "\nNothing changed. Repair only after checking yourself:\n"
    printf "  %s -i %q -o %q --fix --run\n" "$0" "$SOURCE_DIR" "$OUTPUT_DIR"
    rm -f "$RESULT_LOG"
    # Exit 1 signals "findings" so a caller can react. It is not an error.
    exit 1
fi

# ------------------------------------------------------------------------------
# REPAIR
# Remove only the broken outputs and drop the matching source files from
# the cache. h264-to-h265.sh skips existing outputs, so the next run
# so the next run regenerates exactly the deleted ones.
# ------------------------------------------------------------------------------
CACHE_FILE="${SOURCE_DIR}/.video_conversion_cache.txt"
removed=0
while IFS=$'\t' read -r status out src _; do
    case "$status" in VISUAL|DURATION|UNREADABLE) ;; *) continue ;; esac
    [[ -n "$out" && -f "$out" ]] || continue
    if rm -f -- "$out"; then
        removed=$(( removed + 1 ))
        printf "  %b[DELETED]%b %s\n" "$C_CYAN" "$C_RESET" "${out#"$OUTPUT_DIR"/}"
    fi
done < "$RESULT_LOG"

pruned=0
if [[ -f "$CACHE_FILE" ]]; then
    cp -p "$CACHE_FILE" "${CACHE_FILE}.bak"
    if grep -Fxv -f "$BROKEN_LIST" "$CACHE_FILE" > "${CACHE_FILE}.tmp" 2>/dev/null; then :; fi
    pruned=$(( $(wc -l < "$CACHE_FILE") - $(wc -l < "${CACHE_FILE}.tmp") ))
    mv "${CACHE_FILE}.tmp" "$CACHE_FILE"
    printf "\n  %d output(s) deleted, %d Cache-Eintrag/Eintraege entfernt.\n" "$removed" "$pruned"
    printf "  Sicherung des Cache: %s\n" "${CACHE_FILE}.bak"
else
    printf "\n  %d output(s) deleted. Kein Cache vorhanden.\n" "$removed"
fi

rm -f "$RESULT_LOG"

if [[ "$DO_RUN" != true ]]; then
    printf "\nRegenerate now with:\n"
    printf "  %q/h264-to-h265.sh -i %q -o %q --from-list %q\n" \
        "$SCRIPT_DIR" "$SOURCE_DIR" "$OUTPUT_DIR" "$BROKEN_LIST"
    exit 0
fi

printf "\n%b▶ Regenerating %d file(s) from the list...%b\n" \
    "$C_CYAN" "$(wc -l < "$BROKEN_LIST")" "$C_RESET"
# --from-list instead of a full rerun: only what was reported as
# broken is repeated. No find over the whole
# tree, no ffprobe over unrelated files.
MO_HOLD=0 "$SCRIPT_DIR/h264-to-h265.sh" -i "$SOURCE_DIR" -o "$OUTPUT_DIR" --from-list "$BROKEN_LIST"
# Archive the list instead of leaving it. Otherwise a later run with
# --from-list auto would process a stale selection and silently ignore
# stillschweigend nie ansehen.
ARCHIV="${OUTPUT_DIR}/.defekte_videos.erledigt-$(date +%Y%m%d-%H%M%S).txt"
if mv "$BROKEN_LIST" "$ARCHIV" 2>/dev/null; then
    printf "\n  Defektliste archiviert als %s\n" "$(basename "$ARCHIV")"
fi

printf "\n%bRegeneration finished. Check again with:%b\n" "$C_BOLD" "$C_RESET"
printf "  %s -i %q -o %q\n" "$0" "$SOURCE_DIR" "$OUTPUT_DIR"
