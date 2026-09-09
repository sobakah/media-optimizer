#!/bin/bash
# ==============================================================================
# media-optimizer.sh
# Orchestriert die Konvertierung. Sichert Parameter & Fortschritt für Resume.
# ==============================================================================
set -euo pipefail

# ANSI-Farbcodes
C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[1;32m'; C_RED='\033[1;31m'
C_YELLOW='\033[1;33m'; C_CYAN='\033[1;36m'; C_BLUE='\033[1;34m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_IMG="$SCRIPT_DIR/scripts/img-to-jxl.sh"
SCRIPT_GIF="$SCRIPT_DIR/scripts//gif-to-webp.sh"
SCRIPT_VIDEO="$SCRIPT_DIR/scripts//h264-to-h265.sh"
STATE_FILE="$SCRIPT_DIR/.media_optimizer_state.env"

for s in "$SCRIPT_IMG" "$SCRIPT_GIF" "$SCRIPT_VIDEO"; do
    [[ ! -f "$s" ]] && echo -e "${C_RED}Fehler: Skript '$s' fehlt.${C_RESET}" >&2 && exit 1
    chmod +x "$s"
done

# Standardwerte (werden exportiert)
INPUT_DIR="${1:-}"
OUTPUT_DIR="${2:-}"
CHECK_EXTENSIONS_PREFLIGHT=false
MAX_WORKERS=$(nproc)
DELETE_ORIGINAL=true
JXL_EFFORT=7
COMPRESSION_METHOD=6
ENCODER_MODE="auto"
BITRATE_THRESHOLD_KBPS=3500
GPU_QP=26
CPU_CRF=22
CPU_PRESET="medium"
ENABLE_PROBE=true
DISCARD_IF_LARGER=true
KEEP_SALVAGED_CORRUPT=true

STAGE_1_DONE=false; STAGE_2_DONE=false; STAGE_3_DONE=false
RESUMING=false

# ==============================================================================
# STATUS- & TRAP-VERWALTUNG (STRG+C SCHUTZ)
# ==============================================================================
save_state() {
    local status="$1"
    [[ -z "$INPUT_DIR" ]] && return 0
    cat <<EOF > "$STATE_FILE"
STATUS="$status"
INPUT_DIR="$INPUT_DIR"
OUTPUT_DIR="$OUTPUT_DIR"
CHECK_EXTENSIONS_PREFLIGHT="$CHECK_EXTENSIONS_PREFLIGHT"
MAX_WORKERS="$MAX_WORKERS"
DELETE_ORIGINAL="$DELETE_ORIGINAL"
JXL_EFFORT="$JXL_EFFORT"
COMPRESSION_METHOD="$COMPRESSION_METHOD"
ENCODER_MODE="$ENCODER_MODE"
BITRATE_THRESHOLD_KBPS="$BITRATE_THRESHOLD_KBPS"
GPU_QP="$GPU_QP"
CPU_CRF="$CPU_CRF"
CPU_PRESET="$CPU_PRESET"
ENABLE_PROBE="$ENABLE_PROBE"
DISCARD_IF_LARGER="$DISCARD_IF_LARGER"
KEEP_SALVAGED_CORRUPT="$KEEP_SALVAGED_CORRUPT"
STAGE_1_DONE="$STAGE_1_DONE"
STAGE_2_DONE="$STAGE_2_DONE"
STAGE_3_DONE="$STAGE_3_DONE"
EOF
}

handle_interrupt() {
    echo -e "\n\n${C_YELLOW}[UNTERBROCHEN] Vorgang durch Benutzer abgebrochen.${C_RESET}"
    save_state "INTERRUPTED"
    echo -e "${C_CYAN}Fortschritt & Parameter wurden gespeichert.${C_RESET}"
    echo -e "${C_CYAN}Beim nächsten Start kannst du nahtlos fortsetzen.${C_RESET}"
    exit 130
}
trap handle_interrupt SIGINT SIGTERM

prompt_val() {
    local question="$1"; local default="$2"; local result_var="$3"
    read -rp "  $question [$default]: " input
    eval "$result_var=\"\${input:-$default}\""
}

prompt_bool() {
    local question="$1"; local default="$2"; local result_var="$3"
    local def_str="J/n"; [[ "$default" == false ]] && def_str="j/N"
    read -rp "  $question [$def_str]: " input
    case "${input,,}" in
        j|ja|y|yes) eval "$result_var=true" ;;
        n|nein|no)  eval "$result_var=false" ;;
        *)          eval "$result_var=$default" ;;
    esac
}

# ==============================================================================
# RESUME PRÜFUNG ODER NEU-KONFIGURATION
# ==============================================================================
echo -e "${C_BLUE}╔══════════════════════════════════════════════════════════════╗${C_RESET}"
echo -e "${C_BLUE}║${C_RESET}                   ${C_BOLD}MEDIA OPTIMIZER MASTER${C_RESET}                     ${C_BLUE}║${C_RESET}"
echo -e "${C_BLUE}╚══════════════════════════════════════════════════════════════╝${C_RESET}"

if [[ -f "$STATE_FILE" ]]; then
    source "$STATE_FILE" 2>/dev/null || true
    if [[ "${STATUS:-}" == "INTERRUPTED" || "${STATUS:-}" == "RUNNING" ]]; then
        echo -e "${C_YELLOW}╔══════════════════════════════════════════════════════════════╗${C_RESET}"
        echo -e "${C_YELLOW}║${C_RESET}     ${C_BOLD}UNVOLLSTÄNDIGER VORHERIGER LAUF ERKANNT${C_RESET}                  ${C_YELLOW}║${C_RESET}"
        echo -e "${C_YELLOW}╚══════════════════════════════════════════════════════════════╝${C_RESET}"
        echo -e "  Quelle: ${C_BOLD}${INPUT_DIR}${C_RESET} | Bilder: $([[ "$STAGE_1_DONE" == true ]] && echo "${C_GREEN}Fertig${C_RESET}" || echo "${C_YELLOW}Offen${C_RESET}") | Videos: $([[ "$STAGE_3_DONE" == true ]] && echo "${C_GREEN}Fertig${C_RESET}" || echo "${C_YELLOW}Offen${C_RESET}")\n"
        
        if [[ -t 0 ]]; then
            read -rp "Letzten Lauf mit gespeicherten Parametern fortsetzen? [J/n]: " res_choice
            case "${res_choice,,}" in
                n|nein|no)
                    echo -e "-> Verwerfe Status.\n"; rm -f "$STATE_FILE"
                    INPUT_DIR="${1:-}"; OUTPUT_DIR="${2:-}"
                    STAGE_1_DONE=false; STAGE_2_DONE=false; STAGE_3_DONE=false
                    ;;
                *)
                    RESUMING=true
                    echo -e "-> ${C_GREEN}Parameter geladen. Setze fort.${C_RESET}\n"
                    ;;
            esac
        else
            RESUMING=true
        fi
    fi
fi

if [[ "$RESUMING" == false ]]; then
    [[ -z "$INPUT_DIR" && -t 0 ]] && read -rp "Quellverzeichnis (Input): " INPUT_DIR
    [[ -z "$INPUT_DIR" ]] && echo -e "${C_RED}Kein Quellverzeichnis.${C_RESET}" >&2 && exit 1
    [[ ! -d "$INPUT_DIR" ]] && echo -e "${C_RED}Verzeichnis fehlt.${C_RESET}" >&2 && exit 1
    [[ -z "$OUTPUT_DIR" && -t 0 && $# -lt 2 ]] && read -rp "Zielverzeichnis (Enter = In-Place): " OUTPUT_DIR
    
    if [[ -t 0 ]]; then
        echo ""
        read -rp "Standardeinstellungen für ALLE Medienarten nutzen? [J/n]: " std_choice
        case "${std_choice,,}" in
            n|nein|no)
                echo -e "\n${C_YELLOW}─── GLOBALE EINSTELLUNGEN ───${C_RESET}"
                prompt_bool "Dateiendungen vorab prüfen (MIME-Type Magic Bytes)?" "$CHECK_EXTENSIONS_PREFLIGHT" CHECK_EXTENSIONS_PREFLIGHT
                prompt_bool "Originale nach Erfolg in den Papierkorb verschieben?" "$DELETE_ORIGINAL" DELETE_ORIGINAL
                prompt_val  "Parallele Worker für Bilder/GIFs" "$MAX_WORKERS" MAX_WORKERS
                
                echo -e "\n${C_YELLOW}─── BILDER & GIFS ───${C_RESET}"
                prompt_val "JXL Kompression (1-9)" "$JXL_EFFORT" JXL_EFFORT
                prompt_val "WebP GIF Kompression (0-6)" "$COMPRESSION_METHOD" COMPRESSION_METHOD
                
                echo -e "\n${C_YELLOW}─── VIDEOS (H.265) ───${C_RESET}"
                prompt_val  "Encoder (auto / gpu / cpu)" "$ENCODER_MODE" ENCODER_MODE
                if [[ "${ENCODER_MODE,,}" == "auto" ]]; then
                    prompt_val "Schwelle CPU/GPU (kb/s)" "$BITRATE_THRESHOLD_KBPS" BITRATE_THRESHOLD_KBPS
                fi
                [[ "${ENCODER_MODE,,}" =~ ^(auto|gpu)$ ]] && prompt_val "GPU CQP (24-28)" "$GPU_QP" GPU_QP
                if [[ "${ENCODER_MODE,,}" =~ ^(auto|cpu)$ ]]; then
                    prompt_val "CPU CRF (20-24)" "$CPU_CRF" CPU_CRF
                    prompt_val "CPU Preset (medium/slow)" "$CPU_PRESET" CPU_PRESET
                fi
                prompt_bool "10s-Testslice vorab zur Prüfung berechnen?" "$ENABLE_PROBE" ENABLE_PROBE
                prompt_bool "Videos verwerfen, wenn Output größer wird?" "$DISCARD_IF_LARGER" DISCARD_IF_LARGER
                prompt_bool "Beschädigte gerettete Videos trotz Mehrgröße behalten?" "$KEEP_SALVAGED_CORRUPT" KEEP_SALVAGED_CORRUPT
                ;;
        esac
    fi
fi

INPUT_DIR="${INPUT_DIR%/}"
[[ -n "$OUTPUT_DIR" ]] && OUTPUT_DIR="${OUTPUT_DIR%/}"; mkdir -p "$OUTPUT_DIR"

# Variablen für die Unterskripte exportieren
export NON_INTERACTIVE=true
export CHECK_EXTENSIONS_PREFLIGHT MAX_WORKERS DELETE_ORIGINAL
export JXL_EFFORT COMPRESSION_METHOD ENCODER_MODE BITRATE_THRESHOLD_KBPS
export GPU_QP CPU_CRF CPU_PRESET ENABLE_PROBE DISCARD_IF_LARGER KEEP_SALVAGED_CORRUPT
export SOURCE_DIR="$INPUT_DIR" OUTPUT_DIR
export RESUMING

# ==============================================================================
# OPTIONALER PRE-FLIGHT-CHECK
# ==============================================================================
if [[ "$CHECK_EXTENSIONS_PREFLIGHT" == true && "$RESUMING" == false ]]; then
    echo -e "\n${C_CYAN}Prüfe Dateiendungen anhand von MIME-Typen...${C_RESET}"
    while IFS= read -r -d '' filepath; do
        mime=$(file -b --mime-type "$filepath" 2>/dev/null || echo "unknown")
        ext="${filepath##*.}"; ext="${ext,,}"
        correct_ext=""
        case "$mime" in
            image/jpeg) correct_ext="jpg" ;;
            image/png)  correct_ext="png" ;;
            image/webp) correct_ext="webp" ;;
            image/jxl)  correct_ext="jxl" ;;
            image/gif)  correct_ext="gif" ;;
            video/mp4)  correct_ext="mp4" ;;
        esac
        [[ "$correct_ext" == "jpg" && "$ext" == "jpeg" ]] && continue
        if [[ -n "$correct_ext" && "$ext" != "$correct_ext" ]]; then
            target="${filepath%.*}.${correct_ext}"
            c=1; while [[ -e "$target" ]]; do target="${filepath%.*}_${c}.${correct_ext}"; c=$((c+1)); done
            echo -e "  ${C_YELLOW}[KORREKTUR]${C_RESET} '$(basename "$filepath")' ($mime) -> '$(basename "$target")'"
            mv "$filepath" "$target"
        fi
    done < <(find "$INPUT_DIR" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.gif" -o -iname "*.webp" -o -iname "*.jxl" -o -iname "*.mp4" \) -print0)
fi
export CHECK_EXTENSIONS_PREFLIGHT=false # Nicht doppelt in Unterskripten ausführen

# ==============================================================================
# PIPELINE-DURCHLAUF
# ==============================================================================
COUNT_IMG=$(find "$INPUT_DIR" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" \) 2>/dev/null | wc -l)
COUNT_GIF=$(find "$INPUT_DIR" -type f -iname "*.gif" 2>/dev/null | wc -l)
COUNT_VID=$(find "$INPUT_DIR" -type f -iname "*.mp4" 2>/dev/null | wc -l)

# Leere Stufen direkt als "Erledigt" markieren ---
(( COUNT_IMG == 0 )) && STAGE_1_DONE=true
(( COUNT_GIF == 0 )) && STAGE_2_DONE=true
(( COUNT_VID == 0 )) && STAGE_3_DONE=true
# ---------------------------------------------------------

START_TOTAL=$(date +%s)
save_state "RUNNING"

run_stage() {
    "$@" || {
        local e=$?
        # 130 (SIGINT direkt), 124-125 (xargs Abbruch)
        if [[ $e -eq 130 || $e -ge 124 ]]; then handle_interrupt; fi
        return $e
    }
}

if (( COUNT_IMG > 0 )) && [[ "$STAGE_1_DONE" != true ]]; then
    echo -e "\n${C_CYAN}▶ [1/3] Starte Bildkonvertierung ($COUNT_IMG Dateien)...${C_RESET}"
    run_stage "$SCRIPT_IMG" "$INPUT_DIR" "$OUTPUT_DIR"
    STAGE_1_DONE=true; save_state "RUNNING"
fi

if (( COUNT_GIF > 0 )) && [[ "$STAGE_2_DONE" != true ]]; then
    echo -e "\n${C_CYAN}▶ [2/3] Starte GIF-Konvertierung ($COUNT_GIF Dateien)...${C_RESET}"
    run_stage "$SCRIPT_GIF" "$INPUT_DIR" "$OUTPUT_DIR"
    STAGE_2_DONE=true; save_state "RUNNING"
fi

if (( COUNT_VID > 0 )) && [[ "$STAGE_3_DONE" != true ]]; then
    echo -e "\n${C_CYAN}▶ [3/3] Starte Video-Encoding ($COUNT_VID Dateien)...${C_RESET}"
    run_stage "$SCRIPT_VIDEO" "$INPUT_DIR" "$OUTPUT_DIR"
    STAGE_3_DONE=true; save_state "RUNNING"
fi

rm -f "$STATE_FILE"
echo -e "\n${C_GREEN}╔══════════════════════════════════════════════════════════════╗${C_RESET}"
echo -e "${C_GREEN}║${C_RESET}  ${C_BOLD}GESAMTER VORGANG ERFOLGREICH BEENDET${C_RESET}                         ${C_GREEN}║${C_RESET}"
echo -e "${C_GREEN}╚══════════════════════════════════════════════════════════════╝${C_RESET}"
