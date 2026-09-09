#!/bin/bash
# ==============================================================================
# h264-to-h265.sh
# ==============================================================================
set -euo pipefail

# ANSI-Farbcodes
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_GREEN='\033[1;32m'
C_RED='\033[1;31m'
C_YELLOW='\033[1;33m'
C_CYAN='\033[1;36m'
C_BLUE='\033[1;34m'
C_BG_GREEN='\033[42;30m'
C_MAGENTA='\033[1;35m'

# ==============================================================================
# HILFSFUNKTIONEN
# ==============================================================================
format_bytes() {
    local b="$1"
    awk -v b="$b" 'BEGIN { split("B KB MB GB TB", u); i = 1; while (b >= 1024 && i < 5) { b /= 1024; i++; } printf "%.2f %s", b, u[i]; }'
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

# ==============================================================================
# KONFIGURATION
# ==============================================================================
SOURCE_DIR="${1:-${SOURCE_DIR:-/home/max/Videos}}"
OUTPUT_DIR="${2:-${OUTPUT_DIR:-}}"
DELETE_ORIGINAL="${DELETE_ORIGINAL:-false}"

ENCODER_MODE="${ENCODER_MODE:-auto}"
BITRATE_THRESHOLD_KBPS="${BITRATE_THRESHOLD_KBPS:-3500}"
GPU_QP="${GPU_QP:-26}"
CPU_CRF="${CPU_CRF:-22}"
CPU_PRESET="${CPU_PRESET:-medium}"
ENABLE_PROBE="${ENABLE_PROBE:-true}"
DISCARD_IF_LARGER="${DISCARD_IF_LARGER:-true}"
KEEP_SALVAGED_CORRUPT="${KEEP_SALVAGED_CORRUPT:-true}"

VAAPI_DEVICE="/dev/dri/renderD128"
CPU_X265_PARAMS="asm=avx512:pools=16:frame-threads=4:no-sao=1:aq-mode=3"
RECURSIVE=true
MIN_SIZE_MB=5
USE_CACHE=true
PROBE_DURATION=10
PROBE_MIN_DURATION=60
SKIP_EXISTING=true

# ------------------------------------------------------------------------------
# STANDALONE-MENÜ
# ------------------------------------------------------------------------------
if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" ]]; then
    echo -e "\033[1;36m══════════════════════════════════════════════════════════════\033[0m"
    echo -e "\033[1mVideo Batch Re-Encoder (H.264 -> H.265 / HEVC)\033[0m"
    echo -e "\033[1;36m══════════════════════════════════════════════════════════════\033[0m"
    read -rp "Mit Standardeinstellungen ausführen? [J/n]: " start_choice
    if [[ "${start_choice,,}" =~ ^(n|nein|no)$ ]]; then
        read -rp "  Quellverzeichnis [$SOURCE_DIR]: " input && SOURCE_DIR="${input:-$SOURCE_DIR}"
        read -rp "  Zielverzeichnis (leer = In-Place) [${OUTPUT_DIR}]: " input && OUTPUT_DIR="${input:-$OUTPUT_DIR}"
        read -rp "  Encoder (auto/gpu/cpu) [$ENCODER_MODE]: " input && ENCODER_MODE="${input:-$ENCODER_MODE}"
        read -rp "  Originale in Papierkorb? [j/N]: " input
        [[ "${input,,}" =~ ^(j|ja|y|yes)$ ]] && DELETE_ORIGINAL=true || DELETE_ORIGINAL=false
    fi
fi

SOURCE_DIR="${SOURCE_DIR%/}"
[[ -n "$OUTPUT_DIR" ]] && OUTPUT_DIR="${OUTPUT_DIR%/}"
CACHE_FILE="${SOURCE_DIR}/.video_conversion_cache.txt"
MIN_SIZE_BYTES=$(( MIN_SIZE_MB * 1024 * 1024 ))

FIND_OPTS=(-type f -iname "*.mp4")
[[ "$RECURSIVE" != true ]] && FIND_OPTS+=(-maxdepth 1)

# ==============================================================================
# PERSISTENTE STATISTIKEN (RESUME-LOGIK)
# ==============================================================================
STATS_FILE="${SOURCE_DIR}/.h265_stats.env"
declare -A BATCH_MAP=()

if [[ "${RESUMING:-false}" == "true" && -f "$STATS_FILE" ]]; then
    source "$STATS_FILE" 2>/dev/null || true
else
    rm -f "$STATS_FILE"
fi

COUNT_PROCESSED=${COUNT_PROCESSED:-0}; COUNT_SALVAGED=${COUNT_SALVAGED:-0}
COUNT_SKIPPED=${COUNT_SKIPPED:-0}; COUNT_CACHE_SKIPPED=${COUNT_CACHE_SKIPPED:-0}
COUNT_PROBE_SKIPPED=${COUNT_PROBE_SKIPPED:-0}; COUNT_DISCARDED=${COUNT_DISCARDED:-0}
COUNT_FAILED=${COUNT_FAILED:-0}; COUNT_GPU=${COUNT_GPU:-0}; COUNT_CPU=${COUNT_CPU:-0}
TOTAL_ORIG_BYTES=${TOTAL_ORIG_BYTES:-0}; TOTAL_NEW_BYTES=${TOTAL_NEW_BYTES:-0}
PREV_ELAPSED=${PREV_ELAPSED:-0}

START_TIME=$(date +%s)

save_stats() {
    local cur_elapsed=$(( $(date +%s) - START_TIME ))
    local tot_elapsed=$(( PREV_ELAPSED + cur_elapsed ))
    {
        echo "COUNT_PROCESSED=$COUNT_PROCESSED"
        echo "COUNT_SALVAGED=$COUNT_SALVAGED"
        echo "COUNT_SKIPPED=$COUNT_SKIPPED"
        echo "COUNT_CACHE_SKIPPED=$COUNT_CACHE_SKIPPED"
        echo "COUNT_PROBE_SKIPPED=$COUNT_PROBE_SKIPPED"
        echo "COUNT_DISCARDED=$COUNT_DISCARDED"
        echo "COUNT_FAILED=$COUNT_FAILED"
        echo "COUNT_GPU=$COUNT_GPU"
        echo "COUNT_CPU=$COUNT_CPU"
        echo "TOTAL_ORIG_BYTES=$TOTAL_ORIG_BYTES"
        echo "TOTAL_NEW_BYTES=$TOTAL_NEW_BYTES"
        echo "PREV_ELAPSED=$tot_elapsed"
        declare -p BATCH_MAP 2>/dev/null || true
    } > "$STATS_FILE"
}

trap 'echo -e "\n\033[1;31m[ABBRUCH]\033[0m Video-Encoding gestoppt."; save_stats; exit 130' SIGINT SIGTERM

# ==============================================================================
# HAUPTVERARBEITUNG
# ==============================================================================
declare -A CACHE_MAP=()
if [[ "$USE_CACHE" == true && -f "$CACHE_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" =~ ^# ]] && continue
        CACHE_MAP["$line"]=1
    done < "$CACHE_FILE"
fi

add_to_cache() {
    local target="$1"
    if [[ "$USE_CACHE" == true && "${CACHE_MAP["$target"]:-0}" -eq 0 ]]; then
        CACHE_MAP["$target"]=1
        echo "$target" >> "$CACHE_FILE"
    fi
}

while IFS= read -r -d '' -u 9 src_file; do
    if [[ "${BATCH_MAP["$src_file"]:-0}" -eq 1 ]]; then continue; fi
    BATCH_MAP["$src_file"]=1

    if [[ "$USE_CACHE" == true && "${CACHE_MAP["$src_file"]:-0}" -eq 1 ]]; then 
        COUNT_CACHE_SKIPPED=$(( COUNT_CACHE_SKIPPED + 1 ))
        continue
    fi

    src_size=$(stat -c%s "$src_file" 2>/dev/null || echo 0)
    if (( src_size < MIN_SIZE_BYTES )); then
        COUNT_SKIPPED=$(( COUNT_SKIPPED + 1 ))
        add_to_cache "$src_file"; continue
    fi

    current_codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of default=noprint_wrappers=1:nokey=1 "$src_file" || echo "unknown")
    if [[ "$current_codec" != "h264" ]]; then
        COUNT_SKIPPED=$(( COUNT_SKIPPED + 1 ))
        add_to_cache "$src_file"; continue
    fi

    filename=$(basename "$src_file")
    stem="${filename%.*}"
    
    if [[ -n "$OUTPUT_DIR" ]]; then
        rel_dir="$(dirname "${src_file#"$SOURCE_DIR"/}")"
        [[ "$rel_dir" == "." ]] && target_folder="$OUTPUT_DIR" || target_folder="$OUTPUT_DIR/$rel_dir"
        mkdir -p "$target_folder"
        dest_file="$target_folder/${stem}.mp4"
    else
        dest_file="$(dirname "$src_file")/${stem}_h265.mp4"
    fi

    if [[ -f "$dest_file" && "$SKIP_EXISTING" == true && -s "$dest_file" ]]; then
        COUNT_SKIPPED=$(( COUNT_SKIPPED + 1 ))
        add_to_cache "$src_file"; add_to_cache "$dest_file"; continue
    fi

    bitrate_raw=$(ffprobe -v error -select_streams v:0 -show_entries stream=bit_rate -of default=noprint_wrappers=1:nokey=1 "$src_file" 2>/dev/null || true)
    [[ -z "$bitrate_raw" || "$bitrate_raw" == "N/A" ]] && bitrate_raw=$(ffprobe -v error -show_entries format=bit_rate -of default=noprint_wrappers=1:nokey=1 "$src_file" 2>/dev/null || echo 0)
    
    bitrate_kbps=0
    [[ "$bitrate_raw" =~ ^[0-9]+(\.[0-9]+)?$ ]] && bitrate_kbps=$(( ${bitrate_raw%.*} / 1000 ))

    active_encoder="$ENCODER_MODE"
    if [[ "$ENCODER_MODE" == "auto" ]]; then
        (( bitrate_kbps > 0 && bitrate_kbps < BITRATE_THRESHOLD_KBPS )) && active_encoder="cpu" || active_encoder="gpu"
    fi

    if [[ "$ENABLE_PROBE" == true && "$bitrate_kbps" -gt 0 ]]; then
        duration_raw=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$src_file" 2>/dev/null || echo 0)
        duration_sec="${duration_raw%.*}"; duration_sec="${duration_sec:-0}"

        if (( duration_sec >= PROBE_MIN_DURATION )); then
            start_sec=$(( (duration_sec - PROBE_DURATION) / 2 ))
            probe_file=$(mktemp --suffix=".mp4" /tmp/enc_probe_XXXXXX)
            PROBE_CMD=(ffmpeg -nostdin -y -hide_banner -loglevel error)

            if [[ "$active_encoder" == "gpu" ]]; then
                PROBE_CMD+=(-reinit_filter 0 -ss "$start_sec" -t "$PROBE_DURATION" -i "$src_file" -vaapi_device "$VAAPI_DEVICE" -vf 'format=nv12,hwupload' -c:v hevc_vaapi -rc_mode CQP -global_quality "$GPU_QP")
            else
                PROBE_CMD+=(-ss "$start_sec" -t "$PROBE_DURATION" -i "$src_file" -c:v libx265 -crf "$CPU_CRF" -preset "$CPU_PRESET" -x265-params "$CPU_X265_PARAMS")
            fi
            PROBE_CMD+=(-an -map 0:v:0 "$probe_file")

            if "${PROBE_CMD[@]}" < /dev/null; then
                probe_bitrate_raw=$(ffprobe -v error -select_streams v:0 -show_entries stream=bit_rate -of default=noprint_wrappers=1:nokey=1 "$probe_file" 2>/dev/null || echo 0)
                [[ -z "$probe_bitrate_raw" || "$probe_bitrate_raw" == "N/A" ]] && probe_bitrate_raw=$(ffprobe -v error -show_entries format=bit_rate -of default=noprint_wrappers=1:nokey=1 "$probe_file" 2>/dev/null || echo 0)
                
                probe_kbps=0
                if [[ "$probe_bitrate_raw" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
                    probe_kbps=$(( ${probe_bitrate_raw%.*} / 1000 ))
                else
                    probe_size=$(stat -c%s "$probe_file" 2>/dev/null || echo 0)
                    probe_kbps=$(( probe_size * 8 / PROBE_DURATION / 1000 ))
                fi
                rm -f "$probe_file"

                if (( probe_kbps >= bitrate_kbps && probe_kbps > 0 )); then
                    echo -e "\033[1;33m[ÜBERSPRUNGEN]\033[0m $filename (Probe: $probe_kbps kb/s >= Original: $bitrate_kbps kb/s)"
                    COUNT_PROBE_SKIPPED=$(( COUNT_PROBE_SKIPPED + 1 ))
                    add_to_cache "$src_file"
                    continue
                fi
            else
                rm -f "$probe_file"
            fi
        fi
    fi

    echo -e "\n\033[1m>>> Verarbeite:\033[0m $filename (${active_encoder^^})"
    temp_file="${dest_file}.part.mp4"
    err_log=$(mktemp /tmp/enc_err_XXXXXX)
    FFMPEG_CMD=(ffmpeg -nostdin -y -hide_banner)

    if [[ "$active_encoder" == "gpu" ]]; then
        FFMPEG_CMD+=(-reinit_filter 0 -i "$src_file" -vaapi_device "$VAAPI_DEVICE" -vf 'format=nv12,hwupload' -c:v hevc_vaapi -rc_mode CQP -global_quality "$GPU_QP")
    else
        FFMPEG_CMD+=(-i "$src_file" -c:v libx265 -crf "$CPU_CRF" -preset "$CPU_PRESET" -x265-params "$CPU_X265_PARAMS")
    fi
    FFMPEG_CMD+=(-map 0:v:0 -map 0:a? -map 0:s? -map_metadata 0 -c:a copy -c:s copy "$temp_file")

    if "${FFMPEG_CMD[@]}" 2>&1 < /dev/null | tee "$err_log"; then
        dest_size=$(stat -c%s "$temp_file" 2>/dev/null || echo 0)
        diff_bytes=$(( src_size - dest_size ))

        is_salvaged=false
        grep -Eqi "corrupt|partial file|decoding error|invalid nal unit|error splitting" "$err_log" 2>/dev/null && is_salvaged=true
        rm -f "$err_log"

        if (( diff_bytes <= 0 )); then
            inc_bytes=$(( dest_size - src_size ))
            inc_h=$(format_bytes "$inc_bytes")
            inc_pct=$(awk "BEGIN {printf \"%.1f\", ($dest_size / $src_size - 1) * 100}")
            src_h=$(format_bytes "$src_size")
            dest_h=$(format_bytes "$dest_size")

            if [[ "$is_salvaged" == true && "$KEEP_SALVAGED_CORRUPT" == true ]]; then
                mv "$temp_file" "$dest_file"; touch -r "$src_file" "$dest_file"
                add_to_cache "$src_file"; add_to_cache "$dest_file"
                
                echo -e "${C_MAGENTA}┌──────────────────────────────────────────────────────────────┐${C_RESET}"
                echo -e "${C_MAGENTA}│${C_RESET}  ${C_BOLD}DATEI GERETTET / REPARIERT:${C_RESET} $filename"
                echo -e "${C_MAGENTA}│${C_RESET}  Original: $src_h (war beschädigt)"
                echo -e "${C_MAGENTA}│${C_RESET}  Neu:      $dest_h (+${inc_pct}%, +${inc_h})"
                echo -e "${C_MAGENTA}│${C_RESET}  Status:   Behalten (lesbare Segmente gesichert)."
                echo -e "${C_MAGENTA}└──────────────────────────────────────────────────────────────┘${C_RESET}"
                
                COUNT_PROCESSED=$(( COUNT_PROCESSED + 1 ))
                COUNT_SALVAGED=$(( COUNT_SALVAGED + 1 ))
                [[ "$active_encoder" == "gpu" ]] && COUNT_GPU=$(( COUNT_GPU + 1 )) || COUNT_CPU=$(( COUNT_CPU + 1 ))
                TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + src_size ))
                TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + dest_size ))
                
                [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src_file"
                save_stats
                continue
            elif [[ "$DISCARD_IF_LARGER" == true ]]; then
                rm -f "$temp_file"; add_to_cache "$src_file"
                
                echo -e "${C_YELLOW}┌──────────────────────────────────────────────────────────────┐${C_RESET}"
                echo -e "${C_YELLOW}│${C_RESET}  ${C_BOLD}DATEI VERWORFEN:${C_RESET} $filename"
                echo -e "${C_YELLOW}│${C_RESET}  Original: $src_h -> Neu: $dest_h"
                echo -e "${C_YELLOW}│${C_RESET}  Status:   Verworfen, da (+${inc_pct}%, +${inc_h}) größer."
                echo -e "${C_YELLOW}└──────────────────────────────────────────────────────────────┘${C_RESET}"
                
                COUNT_DISCARDED=$(( COUNT_DISCARDED + 1 ))
                save_stats
                continue
            fi
        fi

        mv "$temp_file" "$dest_file"; touch -r "$src_file" "$dest_file"
        add_to_cache "$src_file"; add_to_cache "$dest_file"

        # Zähler aktualisieren
        COUNT_PROCESSED=$(( COUNT_PROCESSED + 1 ))
        [[ "$active_encoder" == "gpu" ]] && COUNT_GPU=$(( COUNT_GPU + 1 )) || COUNT_CPU=$(( COUNT_CPU + 1 ))
        TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + src_size ))
        TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + dest_size ))

        # Detailanzeige
        saved_pct=$(awk "BEGIN {printf \"%.1f\", (1 - $dest_size / $src_size) * 100}")
        diff_h=$(format_bytes "$diff_bytes")
        src_h=$(format_bytes "$src_size")
        dest_h=$(format_bytes "$dest_size")

        echo -e "${C_GREEN}┌──────────────────────────────────────────────────────────────┐${C_RESET}"
        echo -e "${C_GREEN}│${C_RESET}  ${C_BOLD}DATEI:${C_RESET}    $filename"
        echo -e "${C_GREEN}│${C_RESET}  Original: $src_h -> Neu: $dest_h"
        echo -e "${C_GREEN}│${C_RESET}  Ersparnis: ${C_BG_GREEN} -${saved_pct}% ${C_RESET} ${C_GREEN}${C_BOLD}(-${diff_h})${C_RESET}"
        echo -e "${C_GREEN}└──────────────────────────────────────────────────────────────┘${C_RESET}"

        [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src_file"
        save_stats
    else
        exit_c=$?
        if [[ $exit_c -eq 130 || $exit_c -eq 255 ]]; then 
            rm -f "$temp_file" "$err_log"
            exit 130
        fi
        echo -e "\033[1;31m[FEHLER]\033[0m '$src_file'" >&2
        rm -f "$temp_file" "$err_log"
        COUNT_FAILED=$(( COUNT_FAILED + 1 ))
        save_stats
    fi

done 9< <(find "$SOURCE_DIR" "${FIND_OPTS[@]}" -print0)

# ==============================================================================
# ABSCHLUSS-STATISTIK
# ==============================================================================
TOTAL_ELAPSED_SECONDS=$(( PREV_ELAPSED + $(date +%s) - START_TIME ))
TOTAL_SAVED_BYTES=$(( TOTAL_ORIG_BYTES - TOTAL_NEW_BYTES ))

echo -e "\n"
echo -e "${C_BLUE}╔══════════════════════════════════════════════════════════════╗${C_RESET}"
echo -e "${C_BLUE}║${C_RESET}                  ${C_BOLD}GESAMTAUSWERTUNG${C_RESET}                            ${C_BLUE}║${C_RESET}"
echo -e "${C_BLUE}╠══════════════════════════════════════════════════════════════╣${C_RESET}"
printf "${C_BLUE}║${C_RESET}  Gesamtlaufzeit:       %-38s ${C_BLUE}║${C_RESET}\n" "$(format_duration "$TOTAL_ELAPSED_SECONDS")"
printf "${C_BLUE}║${C_RESET}  Neu kodiert:          %-38s ${C_BLUE}║${C_RESET}\n" "$COUNT_PROCESSED Datei(en) (GPU: $COUNT_GPU | CPU: $COUNT_CPU)"
if (( COUNT_SALVAGED > 0 )); then
    printf "${C_BLUE}║${C_RESET}  ${C_MAGENTA}Gerettet (repariert):${C_RESET} %-38s ${C_BLUE}║${C_RESET}\n" "$COUNT_SALVAGED Datei(en)"
fi
printf "${C_BLUE}║${C_RESET}  Cache (abgeschlossen):%-38s ${C_BLUE}║${C_RESET}\n" "$COUNT_CACHE_SKIPPED Datei(en)"
printf "${C_BLUE}║${C_RESET}  Übersprungen (Größe): %-38s ${C_BLUE}║${C_RESET}\n" "$COUNT_SKIPPED Datei(en)"
printf "${C_BLUE}║${C_RESET}  Probe (kein Gewinn):  %-38s ${C_BLUE}║${C_RESET}\n" "$COUNT_PROBE_SKIPPED Datei(en)"
printf "${C_BLUE}║${C_RESET}  Verworfen (größer):   %-38s ${C_BLUE}║${C_RESET}\n" "$COUNT_DISCARDED Datei(en)"
printf "${C_BLUE}║${C_RESET}  Fehlgeschlagen:       %-38s ${C_BLUE}║${C_RESET}\n" "$COUNT_FAILED Datei(en)"
echo -e "${C_BLUE}╟──────────────────────────────────────────────────────────────╢${C_RESET}"

if (( COUNT_PROCESSED > 0 )); then
    TOTAL_ORIG_H=$(format_bytes "$TOTAL_ORIG_BYTES")
    TOTAL_NEW_H=$(format_bytes "$TOTAL_NEW_BYTES")
    printf "${C_BLUE}║${C_RESET}  Speicher vorher:      %-38s ${C_BLUE}║${C_RESET}\n" "$TOTAL_ORIG_H"
    printf "${C_BLUE}║${C_RESET}  Speicher nachher:     %-38s ${C_BLUE}║${C_RESET}\n" "$TOTAL_NEW_H"

    if (( TOTAL_SAVED_BYTES > 0 )); then
        TOTAL_SAVED_H=$(format_bytes "$TOTAL_SAVED_BYTES")
        TOTAL_SAVED_PCT=$(awk "BEGIN {printf \"%.1f\", (1 - $TOTAL_NEW_BYTES / $TOTAL_ORIG_BYTES) * 100}")
        SAVED_STR="-${TOTAL_SAVED_H} (-${TOTAL_SAVED_PCT}%)"
        printf "${C_BLUE}║${C_RESET}  ${C_BOLD}Gesamtersparnis:${C_RESET}      ${C_GREEN}%-38s${C_RESET} ${C_BLUE}║${C_RESET}\n" "$SAVED_STR"
    else
        INCREASED_BYTES=$(( TOTAL_NEW_BYTES - TOTAL_ORIG_BYTES ))
        TOTAL_INC_H=$(format_bytes "$INCREASED_BYTES")
        TOTAL_INC_PCT=$(awk "BEGIN {printf \"%.1f\", ($TOTAL_NEW_BYTES / $TOTAL_ORIG_BYTES - 1) * 100}")
        INC_STR="+${TOTAL_INC_H} (+${TOTAL_INC_PCT}%)"
        printf "${C_BLUE}║${C_RESET}  ${C_BOLD}Zuwachs:${C_RESET}              ${C_YELLOW}%-38s${C_RESET} ${C_BLUE}║${C_RESET}\n" "$INC_STR"
    fi
else
    printf "${C_BLUE}║${C_RESET}  %-60s ${C_BLUE}║${C_RESET}\n" "Keine neuen Videos übernommen."
fi

echo -e "${C_BLUE}╚══════════════════════════════════════════════════════════════╝${C_RESET}"

rm -f "$STATS_FILE"
