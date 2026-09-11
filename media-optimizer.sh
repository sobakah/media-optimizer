#!/bin/bash
# ==============================================================================
# media-optimizer.sh
# Orchestriert die Konvertierung. Sichert Parameter & Fortschritt fuer Resume.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/scripts/lib/common.sh"
_MO_ROOT="$SCRIPT_DIR"; load_config
mo_install_exit_handler

SCRIPT_IMG="$SCRIPT_DIR/scripts/img-to-jxl.sh"
SCRIPT_GIF="$SCRIPT_DIR/scripts/gif-to-webp.sh"
SCRIPT_VIDEO="$SCRIPT_DIR/scripts/h264-to-h265.sh"
SCRIPT_AVIF="$SCRIPT_DIR/scripts/video-to-avif.sh"
STATE_FILE="$SCRIPT_DIR/.media_optimizer_state.env"

usage() {
    cat <<'EOF'
media-optimizer.sh [<input>] [<output>] [optionen]

  -i, --input  <dir>   Quellverzeichnis
  -o, --output <dir>   Zielverzeichnis (leer = In-Place)
      --delete         Originale nach Erfolg in den Papierkorb
      --no-delete      Originale behalten (Default)
      --force-delete   Falls Papierkorb fehlschlaegt: ohne Rueckfrage rm
      --verify-deep    Bildausgaben vollstaendig dekodieren (langsamer)
      --preflight      Dateiendungen vorab per MIME-Typ korrigieren
      --log <datei>    Protokolldatei (Default: media-optimizer.log daneben)
      --no-log         Kein Protokoll schreiben
      --avif           Zusatzstufe: kurze, tonlose Videos nach AVIF
      --no-avif        Diese Stufe ueberspringen (Default)
  -y, --yes            Keine Rueckfragen, Standardwerte verwenden
  -n, --dry-run        Nur anzeigen, nichts schreiben
      --reset          Gespeicherten Zustand verwerfen und neu starten
      --hold           Fenster am Ende offen halten (fuer Doppelklick-Start)
      --no-hold        Fenster nie offen halten
  -h, --help           Diese Hilfe
EOF
}

# ------------------------------------------------------------------------------
# STANDARDWERTE
# ------------------------------------------------------------------------------
INPUT_DIR=""
OUTPUT_DIR=""
CHECK_EXTENSIONS_PREFLIGHT="${CHECK_EXTENSIONS_PREFLIGHT:-false}"
MAX_WORKERS="${MAX_WORKERS:-$(nproc)}"
# Kein fester Default: die Vorbelegung haengt davon ab, ob ein
# Zielverzeichnis angegeben wurde (siehe mo_apply_inplace_defaults).
# Ein Wert aus Umgebung oder Konfigdatei gilt als ausdrueckliche Angabe.
if [[ -n "${DELETE_ORIGINAL+x}" ]]; then DELETE_ORIGINAL_EXPLICIT=true; fi
DELETE_ORIGINAL="${DELETE_ORIGINAL:-false}"      # destruktiver Default entschaerft
FORCE_DELETE="${FORCE_DELETE:-false}"
JXL_EFFORT="${JXL_EFFORT:-7}"
IMG_TARGET="${IMG_TARGET:-jxl}"
IMG_WEBP_QUALITY="${IMG_WEBP_QUALITY:-85}"
IMG_DISCARD_IF_LARGER="${IMG_DISCARD_IF_LARGER:-false}"
PNG_MODE="${PNG_MODE:-lossless}"
PNG_QUALITY="${PNG_QUALITY:-90}"
CJXL_THREADS="${CJXL_THREADS:-1}"
COMPRESSION_METHOD="${COMPRESSION_METHOD:-6}"
ENCODER_MODE="${ENCODER_MODE:-auto}"
BITRATE_THRESHOLD_KBPS="${BITRATE_THRESHOLD_KBPS:-3500}"
GPU_QP="${GPU_QP:-26}"
CPU_CRF="${CPU_CRF:-22}"
CPU_PRESET="${CPU_PRESET:-medium}"
CPU_X265_PARAMS="${CPU_X265_PARAMS:-aq-mode=3:no-sao=1}"
ENABLE_PROBE="${ENABLE_PROBE:-true}"
PROBE_MARGIN_PCT="${PROBE_MARGIN_PCT:-90}"
DISCARD_IF_LARGER="${DISCARD_IF_LARGER:-true}"
KEEP_SALVAGED_CORRUPT="${KEEP_SALVAGED_CORRUPT:-true}"
VERIFY_DEEP="${VERIFY_DEEP:-false}"
# Zusatzstufe: kurze, tonlose Videos nach AVIF. Standardmaessig aus, weil die
# Ersparnis gering ist und AVIF keinen Ton speichern kann.
ENABLE_AVIF_STAGE="${ENABLE_AVIF_STAGE:-false}"
DRY_RUN=false
ASSUME_YES=false
RESET_STATE=false

STAGE_1_DONE=false; STAGE_2_DONE=false; STAGE_3_DONE=false; STAGE_AVIF_DONE=false
RESUMING=false

# Fuer den Neustart am Ende: Aufrufoptionen merken.
MO_ORIGINAL_ARGS=("$@")

POSITIONAL=()
while (( $# > 0 )); do
    case "$1" in
        -i|--input)     INPUT_DIR="$2"; shift 2 ;;
        -o|--output)    OUTPUT_DIR="$2"; shift 2 ;;
        --delete)       DELETE_ORIGINAL=true; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --no-delete)    DELETE_ORIGINAL=false; DELETE_ORIGINAL_EXPLICIT=true; shift ;;
        --force-delete) FORCE_DELETE=true; shift ;;
        --verify-deep)  VERIFY_DEEP=true; shift ;;
        --log)          MO_LOG_FILE="$2"; shift 2 ;;
        --no-log)       MO_LOG=false; shift ;;
        --avif)         ENABLE_AVIF_STAGE=true; shift ;;
        --no-avif)      ENABLE_AVIF_STAGE=false; shift ;;
        --preflight)    CHECK_EXTENSIONS_PREFLIGHT=true; shift ;;
        -y|--yes)       ASSUME_YES=true; shift ;;
        -n|--dry-run)   DRY_RUN=true; shift ;;
        --reset)        RESET_STATE=true; shift ;;
        --hold)         MO_HOLD=1; shift ;;
        --no-hold)      MO_HOLD=0; shift ;;
        -h|--help)      usage; exit 0 ;;
        --)             shift; while (( $# > 0 )); do POSITIONAL+=("$1"); shift; done ;;
        -*)             echo "Unbekannte Option: $1" >&2; usage >&2; exit 2 ;;
        *)              POSITIONAL+=("$1"); shift ;;
    esac
done
[[ -n "${POSITIONAL[0]:-}" ]] && INPUT_DIR="${POSITIONAL[0]}"
[[ -n "${POSITIONAL[1]:-}" ]] && OUTPUT_DIR="${POSITIONAL[1]}"
CLI_INPUT="$INPUT_DIR"; CLI_OUTPUT="$OUTPUT_DIR"

for s in "$SCRIPT_IMG" "$SCRIPT_GIF" "$SCRIPT_VIDEO" "$SCRIPT_AVIF"; do
    if [[ ! -f "$s" ]]; then
        printf "%bFehler: Skript '%s' fehlt.%b\n" "$C_RED" "$s" "$C_RESET" >&2
        exit 1
    fi
    [[ -x "$s" ]] || chmod +x "$s"
done

# ------------------------------------------------------------------------------
# STATUS & TRAP
# ------------------------------------------------------------------------------
save_state() {
    local status="$1"
    [[ -z "$INPUT_DIR" || "$DRY_RUN" == true ]] && return 0
    cat > "$STATE_FILE" <<EOF
STATUS="$status"
INPUT_DIR="$INPUT_DIR"
OUTPUT_DIR="$OUTPUT_DIR"
MAX_WORKERS="$MAX_WORKERS"
DELETE_ORIGINAL="$DELETE_ORIGINAL"
FORCE_DELETE="$FORCE_DELETE"
JXL_EFFORT="$JXL_EFFORT"
PNG_MODE="$PNG_MODE"
PNG_QUALITY="$PNG_QUALITY"
COMPRESSION_METHOD="$COMPRESSION_METHOD"
ENCODER_MODE="$ENCODER_MODE"
BITRATE_THRESHOLD_KBPS="$BITRATE_THRESHOLD_KBPS"
GPU_QP="$GPU_QP"
CPU_CRF="$CPU_CRF"
CPU_PRESET="$CPU_PRESET"
ENABLE_PROBE="$ENABLE_PROBE"
PROBE_MARGIN_PCT="$PROBE_MARGIN_PCT"
DISCARD_IF_LARGER="$DISCARD_IF_LARGER"
KEEP_SALVAGED_CORRUPT="$KEEP_SALVAGED_CORRUPT"
VERIFY_DEEP="$VERIFY_DEEP"
STAGE_1_DONE="$STAGE_1_DONE"
STAGE_2_DONE="$STAGE_2_DONE"
STAGE_3_DONE="$STAGE_3_DONE"
STAGE_AVIF_DONE="$STAGE_AVIF_DONE"
ENABLE_AVIF_STAGE="$ENABLE_AVIF_STAGE"
EOF
}

handle_interrupt() {
    printf "\n\n%b[UNTERBROCHEN] Vorgang durch Benutzer abgebrochen.%b\n" "$C_YELLOW" "$C_RESET"
    mo_log "abbruch" "Durch Benutzer abgebrochen"
    mo_log_close 130
    save_state "INTERRUPTED"
    printf "%bFortschritt & Parameter wurden gespeichert.%b\n" "$C_CYAN" "$C_RESET"
    printf "%bBeim naechsten Start kannst du nahtlos fortsetzen.%b\n" "$C_CYAN" "$C_RESET"
    exit 130
}
trap handle_interrupt SIGINT SIGTERM

# Kein eval mehr: printf -v setzt die Zielvariable direkt.
prompt_val() {
    local question="$1" default="$2" result_var="$3" input
    read -rp "  $question [$default]: " input || input=""
    printf -v "$result_var" '%s' "${input:-$default}"
}

prompt_bool() {
    local question="$1" default="$2" result_var="$3" input
    local def_str="J/n"; [[ "$default" == false ]] && def_str="j/N"
    read -rp "  $question [$def_str]: " input || input=""
    case "${input,,}" in
        j|ja|y|yes) printf -v "$result_var" '%s' true ;;
        n|nein|no)  printf -v "$result_var" '%s' false ;;
        *)          printf -v "$result_var" '%s' "$default" ;;
    esac
}

# ------------------------------------------------------------------------------
# START
# ------------------------------------------------------------------------------
printf "%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b                   %bMEDIA OPTIMIZER MASTER%b                     %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_BLUE" "$C_RESET"

[[ "$RESET_STATE" == true ]] && rm -f "$STATE_FILE"

if [[ -f "$STATE_FILE" ]]; then
    source "$STATE_FILE" 2>/dev/null || true
    if [[ "${STATUS:-}" == "INTERRUPTED" || "${STATUS:-}" == "RUNNING" ]]; then
        printf "%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_YELLOW" "$C_RESET"
        printf "%b║%b     %bUNVOLLSTAENDIGER VORHERIGER LAUF ERKANNT%b                 %b║%b\n" "$C_YELLOW" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_YELLOW" "$C_RESET"
        printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_YELLOW" "$C_RESET"
        printf "  Quelle: %b%s%b | Bilder: %s | GIFs: %s | Videos: %s\n\n" \
            "$C_BOLD" "$INPUT_DIR" "$C_RESET" \
            "$([[ "$STAGE_1_DONE" == true ]] && echo Fertig || echo Offen)" \
            "$([[ "$STAGE_2_DONE" == true ]] && echo Fertig || echo Offen)" \
            "$([[ "$STAGE_3_DONE" == true ]] && echo Fertig || echo Offen)"

        if [[ -t 0 && "$ASSUME_YES" == false ]]; then
            read -rp "Letzten Lauf mit gespeicherten Parametern fortsetzen? [J/n]: " res_choice || res_choice=""
            case "${res_choice,,}" in
                n|nein|no)
                    echo "-> Verwerfe Status."; echo ""
                    rm -f "$STATE_FILE"
                    restart_args=(--reset)
                    [[ -n "$CLI_INPUT"  ]] && restart_args+=(--input  "$CLI_INPUT")
                    [[ -n "$CLI_OUTPUT" ]] && restart_args+=(--output "$CLI_OUTPUT")
                    exec "$0" "${restart_args[@]}"
                    ;;
                *) RESUMING=true; printf -- "-> %bParameter geladen. Setze fort.%b\n\n" "$C_GREEN" "$C_RESET" ;;
            esac
        else
            RESUMING=true
        fi
    fi
fi

if [[ "$RESUMING" == false ]]; then
    if [[ -z "$INPUT_DIR" && -t 0 ]]; then
        read -rp "Quellverzeichnis (Input): " INPUT_DIR || INPUT_DIR=""
    fi
    [[ -z "$INPUT_DIR" ]]  && { printf "%bKein Quellverzeichnis.%b\n" "$C_RED" "$C_RESET" >&2; exit 1; }
    [[ -d "$INPUT_DIR" ]]  || { printf "%bVerzeichnis fehlt: %s%b\n" "$C_RED" "$INPUT_DIR" "$C_RESET" >&2; exit 1; }

    if [[ -z "$OUTPUT_DIR" && -t 0 && -z "$CLI_OUTPUT" && "$ASSUME_YES" == false ]]; then
        read -rp "Zielverzeichnis (Enter = In-Place): " OUTPUT_DIR || OUTPUT_DIR=""
    fi

    if [[ -t 0 && "$ASSUME_YES" == false ]]; then
        echo ""
        read -rp "Standardeinstellungen fuer ALLE Medienarten nutzen? [J/n]: " std_choice || std_choice=""
        if [[ "${std_choice,,}" =~ ^(n|nein|no)$ ]]; then
            printf "\n%b─── GLOBALE EINSTELLUNGEN ───%b\n" "$C_YELLOW" "$C_RESET"
            prompt_bool "Dateiendungen vorab pruefen (MIME-Type Magic Bytes)?" "$CHECK_EXTENSIONS_PREFLIGHT" CHECK_EXTENSIONS_PREFLIGHT
            prompt_bool "Originale nach Erfolg in den Papierkorb verschieben?" "$DELETE_ORIGINAL" DELETE_ORIGINAL
            prompt_bool "Ausgaben zusaetzlich vollstaendig verifizieren (langsamer)?" "$VERIFY_DEEP" VERIFY_DEEP
            prompt_bool "Kurze tonlose Videos zusaetzlich nach AVIF wandeln?" "$ENABLE_AVIF_STAGE" ENABLE_AVIF_STAGE
            prompt_val  "Parallele Worker fuer Bilder/GIFs" "$MAX_WORKERS" MAX_WORKERS

            printf "\n%b─── BILDER & GIFS ───%b\n" "$C_YELLOW" "$C_RESET"
            prompt_val "Bildzielformat (jxl/webp)" "$IMG_TARGET" IMG_TARGET
            [[ "$IMG_TARGET" == "webp" ]] && prompt_val "WebP-Qualitaet (1-100)" "$IMG_WEBP_QUALITY" IMG_WEBP_QUALITY
            prompt_val "JXL Effort (1-9)" "$JXL_EFFORT" JXL_EFFORT
            prompt_val "PNG-Modus (lossless/lossy)" "$PNG_MODE" PNG_MODE
            [[ "$PNG_MODE" == "lossy" ]] && prompt_val "PNG JXL-Qualitaet (1-100)" "$PNG_QUALITY" PNG_QUALITY
            prompt_val "WebP GIF Kompression (0-6)" "$COMPRESSION_METHOD" COMPRESSION_METHOD

            printf "\n%b─── VIDEOS (H.265) ───%b\n" "$C_YELLOW" "$C_RESET"
            prompt_val "Encoder (auto / gpu / cpu)" "$ENCODER_MODE" ENCODER_MODE
            [[ "${ENCODER_MODE,,}" == "auto" ]] && prompt_val "Schwelle CPU/GPU (kb/s)" "$BITRATE_THRESHOLD_KBPS" BITRATE_THRESHOLD_KBPS
            [[ "${ENCODER_MODE,,}" =~ ^(auto|gpu)$ ]] && prompt_val "GPU CQP (24-28)" "$GPU_QP" GPU_QP
            if [[ "${ENCODER_MODE,,}" =~ ^(auto|cpu)$ ]]; then
                prompt_val "CPU CRF (20-24)" "$CPU_CRF" CPU_CRF
                prompt_val "CPU Preset (medium/slow)" "$CPU_PRESET" CPU_PRESET
            fi
            prompt_bool "10s-Testslice vorab zur Pruefung berechnen?" "$ENABLE_PROBE" ENABLE_PROBE
            prompt_bool "Videos verwerfen, wenn Output groesser wird?" "$DISCARD_IF_LARGER" DISCARD_IF_LARGER
            prompt_bool "Beschaedigte gerettete Videos trotz Mehrgroesse behalten?" "$KEEP_SALVAGED_CORRUPT" KEEP_SALVAGED_CORRUPT
        fi
    fi
fi

INPUT_DIR="${INPUT_DIR%/}"
if [[ -n "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="${OUTPUT_DIR%/}"
    [[ "$DRY_RUN" == true ]] || mkdir -p "$OUTPUT_DIR"
fi

mo_apply_inplace_defaults "$OUTPUT_DIR"

mo_log_init "media-optimizer.sh ${MO_ORIGINAL_ARGS[*]}" "$INPUT_DIR" "$OUTPUT_DIR"
mo_log_settings DELETE_ORIGINAL RENAME_INPLACE FORCE_DELETE MAX_WORKERS \
    IMG_TARGET IMG_WEBP_QUALITY IMG_DISCARD_IF_LARGER \
    JXL_EFFORT PNG_MODE PNG_QUALITY CJXL_THREADS COMPRESSION_METHOD GIF_TARGET \
    ENCODER_MODE BITRATE_THRESHOLD_KBPS GPU_QP CPU_CRF CPU_PRESET CPU_X265_PARAMS \
    GPU_CODEC GPU_RC_MODE HEVC_TAG ENABLE_PROBE PROBE_MARGIN_PCT DISCARD_IF_LARGER \
    KEEP_SALVAGED_CORRUPT VERIFY_DEEP ENABLE_AVIF_STAGE CHECK_EXTENSIONS_PREFLIGHT \
    DRY_RUN

# ------------------------------------------------------------------------------
# ABHAENGIGKEITEN EINMALIG PRUEFEN
# ------------------------------------------------------------------------------
missing_any=false
require_cmds cjxl cwebp file || missing_any=true
require_cmds gif2webp        || missing_any=true
require_cmds ffmpeg ffprobe  || missing_any=true
if [[ "$missing_any" == true ]]; then
    printf "%bBitte die fehlenden Pakete installieren (libjxl-tools, webp, ffmpeg).%b\n" "$C_RED" "$C_RESET" >&2
    exit 1
fi
command -v gio >/dev/null 2>&1 || command -v trash-put >/dev/null 2>&1 || \
    printf "%b[HINWEIS]%b Weder gio noch trash-put gefunden. Originale werden nur nach Rueckfrage geloescht.\n" \
        "$C_YELLOW" "$C_RESET"

[[ "$DRY_RUN" == true ]] && printf "\n%b[DRY-RUN]%b Kein Schreibzugriff, keine Loeschungen.\n" "$C_CYAN" "$C_RESET"

# ------------------------------------------------------------------------------
# EXPORT FUER UNTERSKRIPTE
# ------------------------------------------------------------------------------
export NON_INTERACTIVE=true
export MAX_WORKERS DELETE_ORIGINAL FORCE_DELETE RENAME_INPLACE
export DELETE_ORIGINAL_EXPLICIT=true RENAME_INPLACE_EXPLICIT=true
export JXL_EFFORT PNG_MODE PNG_QUALITY COMPRESSION_METHOD
export IMG_TARGET IMG_WEBP_QUALITY IMG_DISCARD_IF_LARGER
export ENCODER_MODE BITRATE_THRESHOLD_KBPS GPU_QP CPU_CRF CPU_PRESET CPU_X265_PARAMS CJXL_THREADS
export ENABLE_PROBE PROBE_MARGIN_PCT DISCARD_IF_LARGER KEEP_SALVAGED_CORRUPT
export VERIFY_DEEP DRY_RUN RESUMING
export SOURCE_DIR="$INPUT_DIR" OUTPUT_DIR

# ------------------------------------------------------------------------------
# PRE-FLIGHT: ENDUNGEN
# ------------------------------------------------------------------------------
if [[ "$CHECK_EXTENSIONS_PREFLIGHT" == true && "$RESUMING" == false ]]; then
    printf "\n%bPruefe Dateiendungen anhand von MIME-Typen...%b\n" "$C_CYAN" "$C_RESET"
    while IFS= read -r -d '' filepath; do
        mime=$(file -b --mime-type "$filepath" 2>/dev/null || echo unknown)
        ext="${filepath##*.}"; ext="${ext,,}"
        correct_ext=""
        case "$mime" in
            image/jpeg) correct_ext="jpg" ;;  image/png) correct_ext="png" ;;
            image/webp) correct_ext="webp" ;; image/jxl) correct_ext="jxl" ;;
            image/gif)  correct_ext="gif" ;;  video/mp4) correct_ext="mp4" ;;
        esac
        [[ "$correct_ext" == "jpg" && "$ext" == "jpeg" ]] && continue
        if [[ -n "$correct_ext" && "$ext" != "$correct_ext" ]]; then
            target="${filepath%.*}.${correct_ext}"
            c=1; while [[ -e "$target" ]]; do target="${filepath%.*}_${c}.${correct_ext}"; c=$(( c + 1 )); done
            if [[ -n "$OUTPUT_DIR" ]]; then
                # Mit Zielverzeichnis bleibt die Quelle unangetastet. Die
                # Korrektur uebernehmen die Konvertierungsskripte, die das
                # Ergebnis unter dem richtigen Namen im Ziel ablegen.
                printf "  %b[HINWEIS]%b '%s' ist in Wahrheit %s (Quelle bleibt unveraendert)\n" \
                    "$C_YELLOW" "$C_RESET" "$(basename "$filepath")" "$mime"
            else
                printf "  %b[KORREKTUR]%b '%s' (%s) -> '%s'\n" "$C_YELLOW" "$C_RESET" \
                    "$(basename "$filepath")" "$mime" "$(basename "$target")"
                [[ "$DRY_RUN" == true ]] || mv "$filepath" "$target"
            fi
        fi
    done < <(find "$INPUT_DIR" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" \
             -o -iname "*.gif" -o -iname "*.webp" -o -iname "*.jxl" -o -iname "*.mp4" \) -print0 2>/dev/null)
fi

# ------------------------------------------------------------------------------
# WARNUNG: GLEICHE DATEINAMEN MIT VERSCHIEDENEN ENDUNGEN
# foo.jpg und foo.png zeigen beide auf foo.jxl. Die Worker sperren den
# Zielnamen, aber besser einmal vorab sichtbar machen.
# ------------------------------------------------------------------------------
collisions=$(find "$INPUT_DIR" -type f \
    \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.gif" \) 2>/dev/null |
    sed 's/\.[^.\/]*$//' | sort | uniq -d || true)
if [[ -n "$collisions" ]]; then
    printf "\n%b[WARNUNG]%b Gleiche Basisnamen mit verschiedenen Endungen gefunden.\n" "$C_YELLOW" "$C_RESET"
    printf "          Nur die erste Datei wird konvertiert, die anderen bleiben unangetastet:\n"
    echo "$collisions" | head -n 10 | sed 's/^/            /'
    n=$(echo "$collisions" | wc -l)
    (( n > 10 )) && printf "            ... und %d weitere\n" "$(( n - 10 ))"
fi

# ------------------------------------------------------------------------------
# PIPELINE
# ------------------------------------------------------------------------------
COUNT_IMG=$(find "$INPUT_DIR" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" \) 2>/dev/null | wc -l || true)
COUNT_GIF=$(find "$INPUT_DIR" -type f -iname "*.gif" 2>/dev/null | wc -l || true)
COUNT_VID=$(find "$INPUT_DIR" -type f -iname "*.mp4" ! -name "*.part.*.mp4" 2>/dev/null | wc -l || true)

(( COUNT_IMG == 0 )) && STAGE_1_DONE=true
(( COUNT_GIF == 0 )) && STAGE_2_DONE=true
(( COUNT_VID == 0 )) && STAGE_3_DONE=true
[[ "$ENABLE_AVIF_STAGE" != true ]] && STAGE_AVIF_DONE=true
(( COUNT_VID == 0 )) && STAGE_AVIF_DONE=true

# Anzahl der tatsaechlich anstehenden Stufen, damit die Nummerierung stimmt.
STAGE_TOTAL=0
(( COUNT_IMG > 0 )) && STAGE_TOTAL=$(( STAGE_TOTAL + 1 ))
(( COUNT_GIF > 0 )) && STAGE_TOTAL=$(( STAGE_TOTAL + 1 ))
(( COUNT_VID > 0 )) && STAGE_TOTAL=$(( STAGE_TOTAL + 1 ))
if [[ "$ENABLE_AVIF_STAGE" == true ]] && (( COUNT_VID > 0 )); then
    STAGE_TOTAL=$(( STAGE_TOTAL + 1 ))
fi
STAGE_NO=0

save_state "RUNNING"

# Argumente fuer die Unterskripte (Array wegen Pfaden mit Leerzeichen)
STAGE_ARGS=(--input "$INPUT_DIR")
[[ -n "$OUTPUT_DIR" ]] && STAGE_ARGS+=(--output "$OUTPUT_DIR")

run_stage() {
    local label="$1"; shift
    local e=0
    # MO_HOLD=0 nur fuer das Unterskript: sonst wartet jede Stufe einzeln.
    # Das Offenhalten am Ende uebernimmt der Orchestrator.
    env MO_HOLD=0 "$@" || e=$?
    # 130 = SIGINT, 124/125 = timeout bzw. xargs-Abbruch.
    # 126/127 sind Ausfuehrungsfehler (nicht ausfuehrbar / nicht gefunden)
    # und duerfen NICHT als Benutzerabbruch gelten.
    if (( e == 130 || e == 124 || e == 125 )); then handle_interrupt; fi
    if (( e == 126 || e == 127 )); then
        printf "\n%b[FEHLER]%b Stufe '%s' liess sich nicht starten (Code %d).\n" \
            "$C_RED" "$C_RESET" "$label" "$e" >&2
        printf "         Ausfuehrbar? chmod +x, und liegt das Verzeichnis nicht auf einem noexec-Mount?\n" >&2
        exit "$e"
    fi
    if (( e != 0 )); then
        printf "\n%b[FEHLER]%b Stufe '%s' wurde mit Code %d beendet.\n" "$C_RED" "$C_RESET" "$label" "$e" >&2
        printf "%bZustand bleibt gespeichert, du kannst nach dem Beheben fortsetzen.%b\n" "$C_CYAN" "$C_RESET" >&2
        exit "$e"
    fi
    return 0
}

if (( COUNT_IMG > 0 )) && [[ "$STAGE_1_DONE" != true ]]; then
    STAGE_NO=$(( STAGE_NO + 1 ))
    printf "\n%b▶ [%d/%d] Starte Bildkonvertierung (%d Dateien)...%b\n" \
        "$C_CYAN" "$STAGE_NO" "$STAGE_TOTAL" "$COUNT_IMG" "$C_RESET"
    mo_log "start" "Stufe Bilder ($COUNT_IMG Kandidaten)"
    run_stage "Bilder" "$SCRIPT_IMG" "${STAGE_ARGS[@]}"
    STAGE_1_DONE=true; save_state "RUNNING"
fi

if (( COUNT_GIF > 0 )) && [[ "$STAGE_2_DONE" != true ]]; then
    STAGE_NO=$(( STAGE_NO + 1 ))
    printf "\n%b▶ [%d/%d] Starte GIF-Konvertierung (%d Dateien)...%b\n" \
        "$C_CYAN" "$STAGE_NO" "$STAGE_TOTAL" "$COUNT_GIF" "$C_RESET"
    mo_log "start" "Stufe GIFs ($COUNT_GIF Kandidaten)"
    run_stage "GIFs" "$SCRIPT_GIF" "${STAGE_ARGS[@]}"
    STAGE_2_DONE=true; save_state "RUNNING"
fi

# AVIF laeuft VOR dem HEVC-Encoding: kurze, tonlose Clips sollen als AVIF
# enden und nicht vorher schon nach HEVC gewandelt werden. Die HEVC-Stufe
# ueberspringt Quellen, fuer die bereits eine AVIF-Ausgabe existiert.
if (( COUNT_VID > 0 )) && [[ "$ENABLE_AVIF_STAGE" == true && "$STAGE_AVIF_DONE" != true ]]; then
    STAGE_NO=$(( STAGE_NO + 1 ))
    printf "\n%b▶ [%d/%d] Starte AVIF-Stufe (kurze, tonlose Videos)...%b\n" \
        "$C_CYAN" "$STAGE_NO" "$STAGE_TOTAL" "$C_RESET"
    mo_log "start" "Stufe AVIF"
    run_stage "AVIF" "$SCRIPT_AVIF" "${STAGE_ARGS[@]}"
    STAGE_AVIF_DONE=true; save_state "RUNNING"
fi

if (( COUNT_VID > 0 )) && [[ "$STAGE_3_DONE" != true ]]; then
    STAGE_NO=$(( STAGE_NO + 1 ))
    printf "\n%b▶ [%d/%d] Starte Video-Encoding (%d Dateien)...%b\n" \
        "$C_CYAN" "$STAGE_NO" "$STAGE_TOTAL" "$COUNT_VID" "$C_RESET"
    mo_log "start" "Stufe Videos ($COUNT_VID Kandidaten)"
    run_stage "Videos" "$SCRIPT_VIDEO" "${STAGE_ARGS[@]}"
    STAGE_3_DONE=true; save_state "RUNNING"
fi

# ------------------------------------------------------------------------------
# NEUSTART ANBIETEN
#
# Baut die Aufrufzeile ohne Verzeichnisangaben neu auf und ersetzt den Prozess.
# exec loest keinen EXIT-Trap aus, das Protokoll dieses Laufs ist also bereits
# abgeschlossen. Weitergereichter Zustand muss vorher weg, sonst erbt der neue
# Lauf Run-ID, Warnstatus und aufgeloeste Einstellungen.
offer_restart() {
    [[ -t 0 ]] || return 0
    [[ "$ASSUME_YES" == true ]] && return 0

    local answer
    printf "\n"
    read -rp "  Weiteres Verzeichnis bearbeiten? [j/N]: " answer || answer=""
    MO_HOLD=0   # Die Frage stand bereits, nicht zusaetzlich auf Enter warten.
    [[ "${answer,,}" =~ ^(j|ja|y|yes)$ ]] || return 0

    local -a restart=() skip=false a
    for a in "${MO_ORIGINAL_ARGS[@]}"; do
        if [[ "$skip" == true ]]; then skip=false; continue; fi
        case "$a" in
            -i|--input|-o|--output) skip=true; continue ;;
            -*) restart+=("$a") ;;
            *)  continue ;;   # Positionsargumente sind die Verzeichnisse
        esac
    done

    unset MO_RUN_ID MO_INPLACE_WARNED RESUMING NON_INTERACTIVE
    unset DELETE_ORIGINAL RENAME_INPLACE
    unset DELETE_ORIGINAL_EXPLICIT RENAME_INPLACE_EXPLICIT
    unset SOURCE_DIR OUTPUT_DIR

    printf "\n%b─── Neuer Durchlauf ───%b\n\n" "$C_CYAN" "$C_RESET"
    exec "$0" "${restart[@]}"
}

rm -f "$STATE_FILE"
mo_log_close 0
printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_GREEN" "$C_RESET"
printf "%b║%b  %bGESAMTER VORGANG ERFOLGREICH BEENDET%b                         %b║%b\n" "$C_GREEN" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_GREEN" "$C_RESET"
printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_GREEN" "$C_RESET"

offer_restart
