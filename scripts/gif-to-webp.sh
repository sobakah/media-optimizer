#!/bin/bash
# ==============================================================================
# gif-to-webp.sh
# ==============================================================================
set -euo pipefail

C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[1;32m'; C_RED='\033[1;31m'
C_YELLOW='\033[1;33m'; C_CYAN='\033[1;36m'; C_BLUE='\033[1;34m'

format_bytes() {
    local b="$1"; awk -v b="$b" 'BEGIN { split("B KB MB GB TB", u); i = 1; while (b >= 1024 && i < 5) { b /= 1024; i++; } printf "%.2f %s", b, u[i]; }'
}
format_duration() {
    local seconds="$1"
    local h=$(( seconds / 3600 )); local m=$(( (seconds % 3600) / 60 )); local s=$(( seconds % 60 ))
    if (( h > 0 )); then printf "%dh %02dm %02ds" "$h" "$m" "$s"
    elif (( m > 0 )); then printf "%dm %02ds" "$m" "$s"
    else printf "%ds" "$s"; fi
}
safe_remove() {
    local target="$1"
    if command -v gio &>/dev/null; then gio trash "$target" 2>/dev/null || rm "$target"
    elif command -v trash-put &>/dev/null; then trash-put "$target" 2>/dev/null || rm "$target"
    else rm "$target"; fi
}
export -f safe_remove

SOURCE_DIR="${1:-${SOURCE_DIR:-/home/max/Bilder}}"
OUTPUT_DIR="${2:-${OUTPUT_DIR:-}}"
MAX_WORKERS="${MAX_WORKERS:-$(nproc)}"
DELETE_ORIGINAL="${DELETE_ORIGINAL:-true}"
COMPRESSION_METHOD="${COMPRESSION_METHOD:-6}"
SKIP_EXISTING=true

if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" ]]; then
    echo -e "\033[1;36m══════════════════════════════════════════════════════════════\033[0m"
    echo -e "\033[1mBatch GIF zu WebP Konverter (via gif2webp)\033[0m"
    echo -e "\033[1;36m══════════════════════════════════════════════════════════════\033[0m"
    read -rp "Mit Standardeinstellungen ausführen? [J/n]: " start_choice
    if [[ "${start_choice,,}" =~ ^(n|nein|no)$ ]]; then
        read -rp "  Quellordner [$SOURCE_DIR]: " input && SOURCE_DIR="${input:-$SOURCE_DIR}"
        read -rp "  Zielordner (leer = In-Place) [${OUTPUT_DIR}]: " input && OUTPUT_DIR="${input:-$OUTPUT_DIR}"
        read -rp "  Originale in Papierkorb verschieben? [J/n]: " input
        [[ "${input,,}" =~ ^(n|nein|no)$ ]] && DELETE_ORIGINAL=false || DELETE_ORIGINAL=true
        read -rp "  Parallele Worker [$MAX_WORKERS]: " input && MAX_WORKERS="${input:-$MAX_WORKERS}"
        read -rp "  Kompressionsstufe (0-6) [$COMPRESSION_METHOD]: " input && COMPRESSION_METHOD="${input:-$COMPRESSION_METHOD}"
    fi
fi

SOURCE_DIR="${SOURCE_DIR%/}"; [[ -n "$OUTPUT_DIR" ]] && OUTPUT_DIR="${OUTPUT_DIR%/}"

# ==============================================================================
# STATISTIK-VERWALTUNG
# ==============================================================================
STATS_FILE="${SOURCE_DIR}/.gif_stats.env"
CURRENT_RUN_LOG=$(mktemp /tmp/gif_stats_XXXXXX)
export CURRENT_RUN_LOG

if [[ "${RESUMING:-false}" == "true" && -f "$STATS_FILE" ]]; then
    source "$STATS_FILE" 2>/dev/null || true
else
    rm -f "$STATS_FILE"
fi

TOTAL_PROCESSED=${TOTAL_PROCESSED:-0}; TOTAL_SKIPPED=${TOTAL_SKIPPED:-0}
TOTAL_FAILED=${TOTAL_FAILED:-0}; TOTAL_ORIG_BYTES=${TOTAL_ORIG_BYTES:-0}
TOTAL_NEW_BYTES=${TOTAL_NEW_BYTES:-0}; PREV_ELAPSED=${PREV_ELAPSED:-0}
START_TIME=$(date +%s)

finalize_stats() {
    local exit_code="$1"
    local cur_elapsed=$(( $(date +%s) - START_TIME ))
    local tot_elapsed=$(( PREV_ELAPSED + cur_elapsed ))
    
    local c_proc=0; local c_skip=0; local c_fail=0; local c_orig=0; local c_new=0
    if [[ -f "$CURRENT_RUN_LOG" ]]; then
        c_proc=$(grep -c "^SUCCESS" "$CURRENT_RUN_LOG" || true)
        c_skip=$(grep -c "^SKIP" "$CURRENT_RUN_LOG" || true)
        c_fail=$(grep -c "^FAIL" "$CURRENT_RUN_LOG" || true)
        c_orig=$(awk '/^SUCCESS/ {sum += $2} END {print sum+0}' "$CURRENT_RUN_LOG")
        c_new=$(awk '/^SUCCESS/ {sum += $3} END {print sum+0}' "$CURRENT_RUN_LOG")
        rm -f "$CURRENT_RUN_LOG"
    fi
    
    TOTAL_PROCESSED=$(( TOTAL_PROCESSED + c_proc ))
    TOTAL_SKIPPED=$(( TOTAL_SKIPPED + c_skip ))
    TOTAL_FAILED=$(( TOTAL_FAILED + c_fail ))
    TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + c_orig ))
    TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + c_new ))
    
    if [[ $exit_code -eq 130 ]]; then
        cat <<EOF > "$STATS_FILE"
TOTAL_PROCESSED=$TOTAL_PROCESSED
TOTAL_SKIPPED=$TOTAL_SKIPPED
TOTAL_FAILED=$TOTAL_FAILED
TOTAL_ORIG_BYTES=$TOTAL_ORIG_BYTES
TOTAL_NEW_BYTES=$TOTAL_NEW_BYTES
PREV_ELAPSED=$tot_elapsed
EOF
        exit 130
    else
        rm -f "$STATS_FILE"
    fi

    local TOTAL_SAVED_BYTES=$(( TOTAL_ORIG_BYTES - TOTAL_NEW_BYTES ))
    echo -e "\n${C_BLUE}╔══════════════════════════════════════════════════════════════╗${C_RESET}"
    echo -e "${C_BLUE}║${C_RESET}                  ${C_BOLD}GIF-AUSWERTUNG${C_RESET}                              ${C_BLUE}║${C_RESET}"
    echo -e "${C_BLUE}╠══════════════════════════════════════════════════════════════╣${C_RESET}"
    printf "${C_BLUE}║${C_RESET}  Gesamtlaufzeit:       %-38s ${C_BLUE}║${C_RESET}\n" "$(format_duration "$tot_elapsed")"
    printf "${C_BLUE}║${C_RESET}  Konvertiert:          %-38s ${C_BLUE}║${C_RESET}\n" "$TOTAL_PROCESSED Datei(en)"
    printf "${C_BLUE}║${C_RESET}  Übersprungen:         %-38s ${C_BLUE}║${C_RESET}\n" "$TOTAL_SKIPPED Datei(en)"
    printf "${C_BLUE}║${C_RESET}  Fehlgeschlagen:       %-38s ${C_BLUE}║${C_RESET}\n" "$TOTAL_FAILED Datei(en)"
    echo -e "${C_BLUE}╟──────────────────────────────────────────────────────────────╢${C_RESET}"
    
    if (( TOTAL_PROCESSED > 0 )); then
        printf "${C_BLUE}║${C_RESET}  Speicher vorher:      %-38s ${C_BLUE}║${C_RESET}\n" "$(format_bytes "$TOTAL_ORIG_BYTES")"
        printf "${C_BLUE}║${C_RESET}  Speicher nachher:     %-38s ${C_BLUE}║${C_RESET}\n" "$(format_bytes "$TOTAL_NEW_BYTES")"
        
        if (( TOTAL_SAVED_BYTES > 0 )); then
            local pct=$(awk "BEGIN {printf \"%.1f\", (1 - $TOTAL_NEW_BYTES / $TOTAL_ORIG_BYTES) * 100}")
            printf "${C_BLUE}║${C_RESET}  ${C_BOLD}Gesamtersparnis:${C_RESET}      ${C_GREEN}%-38s${C_RESET} ${C_BLUE}║${C_RESET}\n" "-$(format_bytes "$TOTAL_SAVED_BYTES") (-${pct}%)"
        else
            local pct=$(awk "BEGIN {printf \"%.1f\", ($TOTAL_NEW_BYTES / $TOTAL_ORIG_BYTES - 1) * 100}")
            printf "${C_BLUE}║${C_RESET}  ${C_BOLD}Zuwachs:${C_RESET}              ${C_YELLOW}%-38s${C_RESET} ${C_BLUE}║${C_RESET}\n" "+$(format_bytes "$((TOTAL_NEW_BYTES - TOTAL_ORIG_BYTES))") (+${pct}%)"
        fi
    fi
    echo -e "${C_BLUE}╚══════════════════════════════════════════════════════════════╝${C_RESET}"
}

trap 'echo -e "\n\033[1;31m[ABBRUCH]\033[0m GIF-Konvertierung gestoppt."; finalize_stats 130' SIGINT SIGTERM

# ==============================================================================
# WORKER
# ==============================================================================
convert_gif() {
    local src="$1"
    local orig_size; orig_size=$(stat -c%s "$src" 2>/dev/null || echo 0)
    
    local filename; filename="$(basename "$src")"
    local stem="${filename%.*}"
    local final_dest

    if [[ -n "${OUTPUT_DIR:-}" ]]; then
        local rel_dir; rel_dir="$(dirname "${src#"$SOURCE_DIR"/}")"
        local target_folder="$OUTPUT_DIR/$rel_dir"
        [[ "$rel_dir" == "." ]] && target_folder="$OUTPUT_DIR"
        mkdir -p "$target_folder"
        final_dest="$target_folder/${stem}.webp"
    else
        final_dest="$(dirname "$src")/${stem}.webp"
    fi

    local temp_dest="${final_dest}.part.webp"

    if [[ "$SKIP_EXISTING" == true && -s "$final_dest" ]]; then
        echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
    fi

    if gif2webp -lossless -m "$COMPRESSION_METHOD" -kmin 0 -quiet "$src" -o "$temp_dest" 2>/dev/null; then
        if [[ -s "$temp_dest" ]]; then
            mv "$temp_dest" "$final_dest"
            touch -r "$src" "$final_dest"
            local new_size; new_size=$(stat -c%s "$final_dest" 2>/dev/null || echo 0)
            echo "SUCCESS $orig_size $new_size" >> "$CURRENT_RUN_LOG"
            [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src"
            echo -e "\033[1;32m[OK]\033[0m   '$filename' -> '$(basename "$final_dest")'"
        else
            rm -f "$temp_dest"
            echo "FAIL" >> "$CURRENT_RUN_LOG"; return 1
        fi
    else
        rm -f "$temp_dest"
        echo "FAIL" >> "$CURRENT_RUN_LOG"; return 1
    fi
}

export SOURCE_DIR OUTPUT_DIR SKIP_EXISTING DELETE_ORIGINAL COMPRESSION_METHOD
export -f convert_gif

find "$SOURCE_DIR" -type f -iname "*.gif" -print0 | \
    xargs -0 -n 1 -P "$MAX_WORKERS" bash -c 'convert_gif "$1"' _ || exit_code=$?

if [[ "${exit_code:-0}" -ge 124 ]]; then finalize_stats 130; else finalize_stats 0; fi
