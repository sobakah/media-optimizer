#!/bin/bash
# ==============================================================================
# img-to-jxl.sh
# ==============================================================================
set -euo pipefail

# ANSI-Farbcodes
C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[1;32m'; C_RED='\033[1;31m'
C_YELLOW='\033[1;33m'; C_CYAN='\033[1;36m'; C_MAGENTA='\033[1;35m'; C_BLUE='\033[1;34m'

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
JXL_EFFORT="${JXL_EFFORT:-7}"
CHECK_EXTENSIONS_PREFLIGHT="${CHECK_EXTENSIONS_PREFLIGHT:-false}"
SKIP_EXISTING=true; JXL_PNG_QUALITY=75; WEBP_FALLBACK_METHOD=6; WEBP_FALLBACK_QUALITY=9

if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" ]]; then
    echo -e "\033[1;36m══════════════════════════════════════════════════════════════\033[0m"
    echo -e "\033[1mBildkonverter: JPG/PNG -> JXL (mit WebP-Fallback)\033[0m"
    echo -e "\033[1;36m══════════════════════════════════════════════════════════════\033[0m"
    read -rp "Mit Standardeinstellungen ausführen? [J/n]: " start_choice
    if [[ "${start_choice,,}" =~ ^(n|nein|no)$ ]]; then
        read -rp "  Quellverzeichnis [$SOURCE_DIR]: " input && SOURCE_DIR="${input:-$SOURCE_DIR}"
        read -rp "  Zielverzeichnis (leer = In-Place) [${OUTPUT_DIR}]: " input && OUTPUT_DIR="${input:-$OUTPUT_DIR}"
        read -rp "  Dateiendungen vorab via MIME-Typ prüfen? [j/N]: " input
        [[ "${input,,}" =~ ^(j|ja|y|yes)$ ]] && CHECK_EXTENSIONS_PREFLIGHT=true || CHECK_EXTENSIONS_PREFLIGHT=false
        read -rp "  Originale in den Papierkorb legen? [J/n]: " input
        [[ "${input,,}" =~ ^(n|nein|no)$ ]] && DELETE_ORIGINAL=false || DELETE_ORIGINAL=true
        read -rp "  JXL Effort (1-9) [$JXL_EFFORT]: " input && JXL_EFFORT="${input:-$JXL_EFFORT}"
    fi
fi

SOURCE_DIR="${SOURCE_DIR%/}"; [[ -n "$OUTPUT_DIR" ]] && OUTPUT_DIR="${OUTPUT_DIR%/}"

# ==============================================================================
# STATISTIK-VERWALTUNG
# ==============================================================================
STATS_FILE="${SOURCE_DIR}/.img_stats.env"
CURRENT_RUN_LOG=$(mktemp /tmp/img_stats_XXXXXX)
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
    echo -e "${C_BLUE}║${C_RESET}                  ${C_BOLD}BILDER-AUSWERTUNG${C_RESET}                           ${C_BLUE}║${C_RESET}"
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

trap 'echo -e "\n\033[1;31m[ABBRUCH]\033[0m Bild-Konvertierung gestoppt."; finalize_stats 130' SIGINT SIGTERM

# ==============================================================================
# WORKER
# ==============================================================================
convert_image() {
    local src="$1"
    local orig_size; orig_size=$(stat -c%s "$src" 2>/dev/null || echo 0)
    
    local dir; dir="$(dirname "$src")"
    local file; file="$(basename "$src")"
    local stem="${file%.*}"
    local ext="${file##*.}"; ext="${ext,,}"

    local target_folder; local dest_jxl; local dest_webp
    if [[ -n "${OUTPUT_DIR:-}" ]]; then
        local rel_dir; rel_dir="$(dirname "${src#"$SOURCE_DIR"/}")"
        [[ "$rel_dir" == "." ]] && target_folder="$OUTPUT_DIR" || target_folder="$OUTPUT_DIR/$rel_dir"
        mkdir -p "$target_folder"
        dest_jxl="$target_folder/${stem}.jxl"; dest_webp="$target_folder/${stem}.webp"
    else
        target_folder="$dir"
        dest_jxl="$dir/${stem}.jxl"; dest_webp="$dir/${stem}.webp"
    fi

    local temp_jxl="${dest_jxl}.part.jxl"; local temp_webp="${dest_webp}.part.webp"

    if [[ "$SKIP_EXISTING" == true && ( -s "$dest_jxl" || -s "$dest_webp" ) ]]; then
        echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
    fi

    if [[ "$ext" == "jpg" || "$ext" == "jpeg" ]]; then
        if cjxl "$src" "$temp_jxl" -e "$JXL_EFFORT" --quiet 2>/dev/null; then
            if [[ -s "$temp_jxl" ]]; then
                mv "$temp_jxl" "$dest_jxl"; touch -r "$src" "$dest_jxl"
                local new_size; new_size=$(stat -c%s "$dest_jxl" 2>/dev/null || echo 0)
                echo "SUCCESS $orig_size $new_size" >> "$CURRENT_RUN_LOG"
                [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src"
                echo -e "\033[1;32m[JXL-LOSSLESS]\033[0m '$file' -> '$(basename "$dest_jxl")'"
                return 0
            fi
        fi
        rm -f "$temp_jxl"

        local real_mime; real_mime=$(file -b --mime-type "$src" 2>/dev/null || echo "unknown")
        if [[ "$real_mime" != "image/jpeg" ]]; then
            local correct_ext=""
            case "$real_mime" in
                image/webp) correct_ext="webp" ;; image/png) correct_ext="png" ;; image/jxl) correct_ext="jxl" ;; image/gif) correct_ext="gif" ;;
            esac
            if [[ -n "$correct_ext" ]]; then
                local target_candidate="$dir/${stem}.${correct_ext}"
                local counter=1
                while [[ -e "$target_candidate" ]]; do target_candidate="$dir/${stem}_${counter}.${correct_ext}"; counter=$((counter+1)); done
                mv "$src" "$target_candidate"
                echo -e "\033[1;35m[KORREKTUR]\033[0m    '$file' ($real_mime) -> '$(basename "$target_candidate")'"

                if [[ "$correct_ext" == "webp" || "$correct_ext" == "jxl" ]]; then
                    if [[ -n "${OUTPUT_DIR:-}" ]]; then cp -p "$target_candidate" "$target_folder/"; [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$target_candidate"; fi
                    echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
                fi
                [[ "$correct_ext" == "png" ]] && src="$target_candidate" && file="$(basename "$src")" && ext="png"
            fi
        fi
        if [[ "$ext" != "png" ]]; then
            echo -e "\033[1;31m[FEHLER]\033[0m JXL-Transcoding fehlgeschlagen: '$file'" >&2
            echo "FAIL" >> "$CURRENT_RUN_LOG"; return 1
        fi
    fi

    if [[ "$ext" == "png" ]]; then
        if cjxl "$src" "$temp_jxl" -q "$JXL_PNG_QUALITY" -e "$JXL_EFFORT" --quiet 2>/dev/null; then
            if [[ -s "$temp_jxl" ]]; then
                mv "$temp_jxl" "$dest_jxl"; touch -r "$src" "$dest_jxl"
                local new_size; new_size=$(stat -c%s "$dest_jxl" 2>/dev/null || echo 0)
                echo "SUCCESS $orig_size $new_size" >> "$CURRENT_RUN_LOG"
                [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src"
                echo -e "\033[1;32m[PNG->JXL]\033[0m     '$file' -> '$(basename "$dest_jxl")'"
                return 0
            fi
        fi
        rm -f "$temp_jxl"

        local real_mime; real_mime=$(file -b --mime-type "$src" 2>/dev/null || echo "unknown")
        if [[ "$real_mime" != "image/png" ]]; then
            local correct_ext=""
            case "$real_mime" in
                image/jpeg) correct_ext="jpg" ;; image/webp) correct_ext="webp" ;; image/jxl) correct_ext="jxl" ;; image/gif) correct_ext="gif" ;;
            esac
            if [[ -n "$correct_ext" ]]; then
                local target_candidate="$dir/${stem}.${correct_ext}"; local counter=1
                while [[ -e "$target_candidate" ]]; do target_candidate="$dir/${stem}_${counter}.${correct_ext}"; counter=$((counter+1)); done
                mv "$src" "$target_candidate"
                echo -e "\033[1;35m[KORREKTUR]\033[0m    '$file' ($real_mime) -> '$(basename "$target_candidate")'"

                if [[ "$correct_ext" == "jpg" ]]; then
                    if cjxl "$target_candidate" "$temp_jxl" -e "$JXL_EFFORT" --quiet 2>/dev/null; then
                        mv "$temp_jxl" "$dest_jxl"; touch -r "$target_candidate" "$dest_jxl"
                        local new_size; new_size=$(stat -c%s "$dest_jxl" 2>/dev/null || echo 0)
                        echo "SUCCESS $orig_size $new_size" >> "$CURRENT_RUN_LOG"
                        [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$target_candidate"
                        return 0
                    fi
                fi
                echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
            fi
        fi

        echo -e "\033[1;33m[FALLBACK]\033[0m    '$file' schlug fehl -> Wechsle zu WebP..."
        if cwebp -quiet -m "$WEBP_FALLBACK_METHOD" -q "$WEBP_FALLBACK_QUALITY" "$src" -o "$temp_webp" 2>/dev/null; then
            if [[ -s "$temp_webp" ]]; then
                mv "$temp_webp" "$dest_webp"; touch -r "$src" "$dest_webp"
                local new_size; new_size=$(stat -c%s "$dest_webp" 2>/dev/null || echo 0)
                echo "SUCCESS $orig_size $new_size" >> "$CURRENT_RUN_LOG"
                [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src"
                echo -e "\033[1;32m[PNG->WEBP]\033[0m    '$file' -> '$(basename "$dest_webp")'"
                return 0
            fi
        fi
        rm -f "$temp_webp"
        echo -e "\033[1;31m[FEHLER]\033[0m Konvertierung fehlgeschlagen: '$file'" >&2
        echo "FAIL" >> "$CURRENT_RUN_LOG"; return 1
    fi
}

export SOURCE_DIR OUTPUT_DIR SKIP_EXISTING DELETE_ORIGINAL
export JXL_EFFORT JXL_PNG_QUALITY WEBP_FALLBACK_METHOD WEBP_FALLBACK_QUALITY
export -f convert_image

find "$SOURCE_DIR" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" \) -print0 | \
    xargs -0 -n 1 -P "$MAX_WORKERS" bash -c 'convert_image "$1"' _ || exit_code=$?

if [[ "${exit_code:-0}" -ge 124 ]]; then finalize_stats 130; else finalize_stats 0; fi
