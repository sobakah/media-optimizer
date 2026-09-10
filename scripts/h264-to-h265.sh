#!/bin/bash
# ==============================================================================
# h264-to-h265.sh  -  H.264 -> H.265/HEVC (VAAPI oder libx265)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
_MO_ROOT="$(dirname "$SCRIPT_DIR")"; load_config
mo_install_exit_handler

usage() {
    cat <<'EOF'
h264-to-h265.sh - reencodiert H.264-MP4s nach HEVC

  -i, --input   <dir>    Quellverzeichnis
  -o, --output  <dir>    Zielverzeichnis (leer = In-Place, Suffix _h265)
      --encoder <mode>   auto | gpu | cpu   (Default: auto)
      --threshold <kbps> Schwelle CPU/GPU im auto-Modus (Default: 3500)
      --qp <n>           GPU CQP (Default: 26)
      --crf <n>          CPU CRF (Default: 22)
      --preset <p>       CPU x265-Preset (Default: medium)
      --min-size <mb>    Dateien darunter ueberspringen (Default: 5)
      --no-probe         Kein 10s-Testslice vorab
      --probe-margin <p> Ueberspringen, wenn Probe >= p% des Originals (Default: 90)
                         100 = nur ueberspringen, wenn es wirklich groesser wird
      --probe-duration <s> Laenge des Testslice in Sekunden (Default: 10)
      --keep-larger      Groessere Ergebnisse trotzdem behalten
      --delete           Originale nach Erfolg in den Papierkorb
      --no-delete        Originale behalten (Default)
      --force-delete     Falls Papierkorb fehlschlaegt: ohne Rueckfrage rm
      --gpu-device <p>   VAAPI-Render-Node, oder "auto" zum Durchprobieren
      --force-gpu        Startpruefung ueberspringen, GPU direkt verwenden
      --x265-params <s>  x265-Parameter (Default: aq-mode=3:no-sao=1)
      --no-faststart     moov-Atom nicht nach vorn schreiben (spart 1 Pass)
      --rename-inplace   Nach dem Loeschen des Originals das _h265-Suffix
                         entfernen (nur In-Place, nur wenn Original weg ist)
      --no-cache         Cache-Datei ignorieren
  -n, --dry-run          Nur anzeigen, nichts schreiben
      --hold             Fenster am Ende offen halten (fuer Doppelklick-Start)
      --no-hold          Fenster nie offen halten
  -h, --help             Diese Hilfe
EOF
}

# ------------------------------------------------------------------------------
# KONFIGURATION
# ------------------------------------------------------------------------------
SOURCE_DIR="${SOURCE_DIR:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
DELETE_ORIGINAL="${DELETE_ORIGINAL:-false}"
FORCE_DELETE="${FORCE_DELETE:-false}"
ENCODER_MODE="${ENCODER_MODE:-auto}"
BITRATE_THRESHOLD_KBPS="${BITRATE_THRESHOLD_KBPS:-3500}"
GPU_QP="${GPU_QP:-26}"
CPU_CRF="${CPU_CRF:-22}"
CPU_PRESET="${CPU_PRESET:-medium}"
ENABLE_PROBE="${ENABLE_PROBE:-true}"
PROBE_MARGIN_PCT="${PROBE_MARGIN_PCT:-90}"
DISCARD_IF_LARGER="${DISCARD_IF_LARGER:-true}"
KEEP_SALVAGED_CORRUPT="${KEEP_SALVAGED_CORRUPT:-true}"
MIN_SIZE_MB="${MIN_SIZE_MB:-5}"
USE_CACHE="${USE_CACHE:-true}"
DRY_RUN="${DRY_RUN:-false}"

VAAPI_DEVICE="${VAAPI_DEVICE:-auto}"   # auto = alle Render-Nodes durchprobieren
# Portabler Default ohne Hardware-Annahmen. pools/frame-threads entsprechen
# ohnehin dem, was x265 auf 16 Threads selbst waehlt. asm=avx512 ist hier
# NICHT gesetzt, weil es auf vielen CPUs bremst; auf Zen 5 kann es helfen.
# Ueber die Konfigdatei oder --x265-params gezielt wieder aktivierbar.
CPU_X265_PARAMS="${CPU_X265_PARAMS:-aq-mode=3:no-sao=1}"
FORCE_GPU="${FORCE_GPU:-false}"
RENAME_INPLACE="${RENAME_INPLACE:-false}"
GPU_FELL_BACK=false
FASTSTART="${FASTSTART:-true}"
RECURSIVE=true
PROBE_DURATION="${PROBE_DURATION:-10}"
PROBE_MIN_DURATION=60
SKIP_EXISTING=true
DURATION_TOLERANCE_PCT="${DURATION_TOLERANCE_PCT:-2}"

POSITIONAL=()
while (( $# > 0 )); do
    case "$1" in
        -i|--input)      SOURCE_DIR="$2"; shift 2 ;;
        -o|--output)     OUTPUT_DIR="$2"; shift 2 ;;
        --encoder)       ENCODER_MODE="$2"; shift 2 ;;
        --threshold)     BITRATE_THRESHOLD_KBPS="$2"; shift 2 ;;
        --qp)            GPU_QP="$2"; shift 2 ;;
        --crf)           CPU_CRF="$2"; shift 2 ;;
        --preset)        CPU_PRESET="$2"; shift 2 ;;
        --min-size)      MIN_SIZE_MB="$2"; shift 2 ;;
        --no-probe)      ENABLE_PROBE=false; shift ;;
        --probe-margin)  PROBE_MARGIN_PCT="$2"; shift 2 ;;
        --probe-duration) PROBE_DURATION="$2"; shift 2 ;;
        --keep-larger)   DISCARD_IF_LARGER=false; shift ;;
        --delete)        DELETE_ORIGINAL=true; shift ;;
        --no-delete)     DELETE_ORIGINAL=false; shift ;;
        --force-delete)  FORCE_DELETE=true; shift ;;
        --gpu-device)    VAAPI_DEVICE="$2"; shift 2 ;;
        --force-gpu)     FORCE_GPU=true; ENCODER_MODE=gpu; shift ;;
        --x265-params)   CPU_X265_PARAMS="$2"; shift 2 ;;
        --no-faststart)  FASTSTART=false; shift ;;
        --rename-inplace) RENAME_INPLACE=true; shift ;;
        --no-cache)      USE_CACHE=false; shift ;;
        -n|--dry-run)    DRY_RUN=true; shift ;;
        --hold)          MO_HOLD=1; shift ;;
        --no-hold)       MO_HOLD=0; shift ;;
        -h|--help)       usage; exit 0 ;;
        --)              shift; while (( $# > 0 )); do POSITIONAL+=("$1"); shift; done ;;
        -*)              echo "Unbekannte Option: $1" >&2; usage >&2; exit 2 ;;
        *)               POSITIONAL+=("$1"); shift ;;
    esac
done
[[ -n "${POSITIONAL[0]:-}" ]] && SOURCE_DIR="${POSITIONAL[0]}"
[[ -n "${POSITIONAL[1]:-}" ]] && OUTPUT_DIR="${POSITIONAL[1]}"

if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" ]]; then
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    printf "%bVideo Batch Re-Encoder (H.264 -> H.265 / HEVC)%b\n" "$C_BOLD" "$C_RESET"
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    read -rp "Mit Standardeinstellungen ausfuehren? [J/n]: " start_choice || start_choice=""
    if [[ "${start_choice,,}" =~ ^(n|nein|no)$ ]]; then
        read -rp "  Quellverzeichnis [$SOURCE_DIR]: " x && SOURCE_DIR="${x:-$SOURCE_DIR}"
        read -rp "  Zielverzeichnis (leer = In-Place) [$OUTPUT_DIR]: " x && OUTPUT_DIR="${x:-$OUTPUT_DIR}"
        read -rp "  Encoder (auto/gpu/cpu) [$ENCODER_MODE]: " x && ENCODER_MODE="${x:-$ENCODER_MODE}"
        read -rp "  Originale in den Papierkorb? [j/N]: " x
        [[ "${x,,}" =~ ^(j|ja|y|yes)$ ]] && DELETE_ORIGINAL=true || DELETE_ORIGINAL=false
    fi
fi

[[ -z "$SOURCE_DIR" ]] && { echo "Kein Quellverzeichnis." >&2; exit 1; }
[[ -d "$SOURCE_DIR" ]] || { echo "Quellverzeichnis fehlt: $SOURCE_DIR" >&2; exit 1; }
SOURCE_DIR="${SOURCE_DIR%/}"
if [[ -n "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="${OUTPUT_DIR%/}"
    [[ "$DRY_RUN" == true ]] || mkdir -p "$OUTPUT_DIR"
fi

require_cmds ffmpeg ffprobe || exit 1
gain_needed=$(( 100 - PROBE_MARGIN_PCT ))
[[ "$DRY_RUN" == true ]] && printf "%b[DRY-RUN]%b Es wird nichts geschrieben oder geloescht.\n" "$C_CYAN" "$C_RESET"

CACHE_FILE="${SOURCE_DIR}/.video_conversion_cache.txt"
STATS_FILE="${SOURCE_DIR}/.h265_stats.env"
PENDING_DELETE_LOG="${SOURCE_DIR}/.video_pending_deletes.txt"
MIN_SIZE_BYTES=$(( MIN_SIZE_MB * 1024 * 1024 ))

# Reste aus abgebrochenen Laeufen entfernen, bevor find sie einsammelt
cleanup_stale_parts "${OUTPUT_DIR:-$SOURCE_DIR}" "*.part.*.mp4"

FIND_OPTS=(-type f -iname "*.mp4" ! -name "*.part.*.mp4")
[[ "$RECURSIVE" != true ]] && FIND_OPTS=(-maxdepth 1 "${FIND_OPTS[@]}")

# ------------------------------------------------------------------------------
# GPU-FAEHIGKEIT EINMALIG PRUEFEN
# ------------------------------------------------------------------------------
# Ein einzelnes Render-Node testen. Der ffmpeg-Fehler landet in GPU_PROBE_ERR,
# damit ein Fehlschlag nicht stumm bleibt.
# Genau EINE Definition der GPU-Encoder-Argumente. Probe und echter Lauf
# muessen identisch sein, sonst testet der Check etwas anderes als das,
# was spaeter wirklich ausgefuehrt wird.
gpu_encoder_args() {
    GPU_ENC_ARGS=(-vf 'format=nv12|p010,hwupload' -c:v hevc_vaapi
                  -rc_mode CQP -global_quality "$GPU_QP")
}
gpu_encoder_args

GPU_PROBE_ERR=""
gpu_probe_device() {
    local dev="$1"
    [[ -e "$dev" ]] || { GPU_PROBE_ERR="Geraet existiert nicht"; return 1; }
    [[ -r "$dev" && -w "$dev" ]] || { GPU_PROBE_ERR="keine Lese-/Schreibrechte (Gruppe 'render'?)"; return 1; }
    if GPU_PROBE_ERR=$(ffmpeg -nostdin -hide_banner -loglevel error \
            -vaapi_device "$dev" -f lavfi -i "testsrc=s=1280x720:r=30" -frames:v 5 \
            "${GPU_ENC_ARGS[@]}" -an -f null - 2>&1); then
        GPU_PROBE_ERR=""
        return 0
    fi
    return 1
}

# Alle Render-Nodes durchgehen. Auf Systemen mit iGPU und dGPU ist renderD128
# haeufig die iGPU; die dGPU liegt dann auf renderD129.
detect_vaapi_device() {
    local -a candidates=() others=() d
    while IFS= read -r d; do
        [[ -e "$d" ]] || continue
        if [[ "$d" == "$VAAPI_DEVICE" ]]; then candidates=("$d" "${candidates[@]}"); else others+=("$d"); fi
    done < <(printf '%s\n' /dev/dri/renderD* 2>/dev/null)

    if [[ "${VAAPI_DEVICE,,}" != "auto" ]]; then
        candidates=("$VAAPI_DEVICE" "${others[@]}")
    else
        candidates=("${candidates[@]}" "${others[@]}")
    fi

    local first_err="" tried=0
    for d in "${candidates[@]}"; do
        [[ -n "$d" && -e "$d" ]] || continue
        tried=$(( tried + 1 ))
        if gpu_probe_device "$d"; then
            if [[ "$d" != "$VAAPI_DEVICE" && "${VAAPI_DEVICE,,}" != "auto" ]]; then
                printf "%b[GPU]%b %s liefert kein HEVC, aber %b%s%b schon. Verwende dieses.\n" \
                    "$C_YELLOW" "$C_RESET" "$VAAPI_DEVICE" "$C_BOLD" "$d" "$C_RESET"
                printf "      Dauerhaft setzen: VAAPI_DEVICE=\"%s\" in media-optimizer.conf\n" "$d"
            fi
            VAAPI_DEVICE="$d"
            return 0
        fi
        [[ -z "$first_err" ]] && first_err="$d: $GPU_PROBE_ERR"
    done
    (( tried == 0 )) && first_err="keine Render-Nodes unter /dev/dri/ gefunden"
    GPU_PROBE_ERR="$first_err"
    return 1
}

gpu_diagnose() {
    local d
    printf "      %bDiagnose:%b\n" "$C_BOLD" "$C_RESET" >&2
    printf "        ls -l /dev/dri/by-path/          # welches Node ist die dGPU\n" >&2
    for d in /dev/dri/renderD*; do
        [[ -e "$d" ]] || continue
        printf "        vainfo --display drm --device %s | grep -i 'hevc.*enc'\n" "$d" >&2
    done
    printf "        id | grep -o render               # Gruppenmitgliedschaft\n" >&2
    printf "        ffmpeg -hide_banner -encoders | grep hevc_vaapi\n" >&2
    printf "      RDNA4 (VCN 5) braucht Mesa 25.0+ und Kernel 6.13+.\n" >&2
}

if [[ "$FORCE_GPU" == true ]]; then
    [[ "${VAAPI_DEVICE,,}" == "auto" ]] && VAAPI_DEVICE="/dev/dri/renderD128"
    printf "%b[GPU]%b Startpruefung uebersprungen, verwende %s.\n" "$C_CYAN" "$C_RESET" "$VAAPI_DEVICE"
elif [[ "$ENCODER_MODE" =~ ^(auto|gpu)$ ]]; then
    if detect_vaapi_device; then
        printf "%b[GPU]%b hevc_vaapi auf %s verfuegbar.\n" "$C_GREEN" "$C_RESET" "$VAAPI_DEVICE"
    elif [[ "$ENCODER_MODE" == "gpu" ]]; then
        printf "%b[FEHLER]%b hevc_vaapi nicht nutzbar, --encoder gpu war aber ausdruecklich gesetzt.\n" \
            "$C_RED" "$C_RESET" >&2
        printf "      Grund: %s\n" "${GPU_PROBE_ERR:-unbekannt}" >&2
        gpu_diagnose
        exit 1
    else
        printf "%b[GPU]%b hevc_vaapi nicht nutzbar -> Fallback auf CPU/libx265.\n" "$C_YELLOW" "$C_RESET"
        printf "      Grund: %s\n" "${GPU_PROBE_ERR:-unbekannt}" >&2
        gpu_diagnose
        ENCODER_MODE="cpu"
    fi
fi

# ------------------------------------------------------------------------------
# PERSISTENTE STATISTIKEN
# ------------------------------------------------------------------------------
if [[ "${RESUMING:-false}" == "true" && -f "$STATS_FILE" ]]; then
    source "$STATS_FILE" 2>/dev/null || true
else
    rm -f "$STATS_FILE"
fi

COUNT_PROCESSED=${COUNT_PROCESSED:-0}; COUNT_SALVAGED=${COUNT_SALVAGED:-0}
COUNT_SKIPPED=${COUNT_SKIPPED:-0};     COUNT_CACHE_SKIPPED=${COUNT_CACHE_SKIPPED:-0}
COUNT_PROBE_SKIPPED=${COUNT_PROBE_SKIPPED:-0}; COUNT_DISCARDED=${COUNT_DISCARDED:-0}
COUNT_FAILED=${COUNT_FAILED:-0}; COUNT_GPU=${COUNT_GPU:-0}; COUNT_CPU=${COUNT_CPU:-0}
COUNT_UNVERIFIED=${COUNT_UNVERIFIED:-0}; COUNT_DRY=${COUNT_DRY:-0}
TOTAL_ORIG_BYTES=${TOTAL_ORIG_BYTES:-0}; TOTAL_NEW_BYTES=${TOTAL_NEW_BYTES:-0}
PREV_ELAPSED=${PREV_ELAPSED:-0}
START_TIME=$(date +%s)

save_stats() {
    local tot_elapsed=$(( PREV_ELAPSED + $(date +%s) - START_TIME ))
    cat > "$STATS_FILE" <<EOF
COUNT_PROCESSED=$COUNT_PROCESSED
COUNT_SALVAGED=$COUNT_SALVAGED
COUNT_SKIPPED=$COUNT_SKIPPED
COUNT_CACHE_SKIPPED=$COUNT_CACHE_SKIPPED
COUNT_PROBE_SKIPPED=$COUNT_PROBE_SKIPPED
COUNT_DISCARDED=$COUNT_DISCARDED
COUNT_FAILED=$COUNT_FAILED
COUNT_UNVERIFIED=$COUNT_UNVERIFIED
COUNT_GPU=$COUNT_GPU
COUNT_CPU=$COUNT_CPU
TOTAL_ORIG_BYTES=$TOTAL_ORIG_BYTES
TOTAL_NEW_BYTES=$TOTAL_NEW_BYTES
PREV_ELAPSED=$tot_elapsed
EOF
}

on_interrupt() {
    printf "\n%b[ABBRUCH]%b Video-Encoding gestoppt.\n" "$C_RED" "$C_RESET"
    [[ -n "${temp_file:-}" ]] && rm -f "$temp_file"
    [[ -n "${err_log:-}" ]] && rm -f "$err_log"
    save_stats
    resolve_pending_deletes
    exit 130
}
trap on_interrupt SIGINT SIGTERM

# ------------------------------------------------------------------------------
# CACHE
# Der Cache ist die einzige Resume-Quelle. Eine Datei kommt erst hinein,
# wenn sie fertig verarbeitet (oder bewusst uebersprungen) wurde.
# ------------------------------------------------------------------------------
declare -A CACHE_MAP=()
CACHE_PRUNED=0
if [[ "$USE_CACHE" == true && -f "$CACHE_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        # Eintraege ohne Datei sind tot: find kann sie nie zurueckliefern.
        if [[ -e "$line" ]]; then
            CACHE_MAP["$line"]=1
        else
            CACHE_PRUNED=$(( CACHE_PRUNED + 1 ))
        fi
    done < "$CACHE_FILE"

    # Datei einmal neu schreiben, damit sie nicht unbegrenzt waechst
    if (( CACHE_PRUNED > 0 )) && [[ "$DRY_RUN" != true ]]; then
        printf '%s\n' "${!CACHE_MAP[@]}" > "$CACHE_FILE" 2>/dev/null || true
        printf "%b[CACHE]%b %d verwaiste Eintraege entfernt.\n" \
            "$C_CYAN" "$C_RESET" "$CACHE_PRUNED"
    fi
fi

add_to_cache() {
    local target="$1"
    [[ "$DRY_RUN" == true ]] && return 0
    if [[ "$USE_CACHE" == true && "${CACHE_MAP["$target"]:-0}" -eq 0 ]]; then
        CACHE_MAP["$target"]=1
        echo "$target" >> "$CACHE_FILE"
    fi
}

# Zieldateien nur vormerken, wenn sie im durchsuchten Baum liegen. Im
# In-Place-Modus findet der naechste Lauf "name_h265.mp4" wieder und spart
# durch den Eintrag ein ffprobe. Bei separatem Zielordner sieht find dort
# nie hin, der Eintrag waere nur Ballast im Cache.
add_dest_to_cache() {
    local dest="$1"
    [[ -z "$OUTPUT_DIR" ]] || return 0
    add_to_cache "$dest"
}

# slice_kbps <datei> <startsekunde> -> Bitrate eines Video-only-Ausschnitts
# Beide Vergleichswerte (Original und Probe) werden identisch gemessen:
# gleicher Startpunkt, gleiche Laenge, ohne Audio, ueber die Dateigroesse.
slice_kbps() {
    local file="$1" start="$2"
    local tmp; tmp=$(mktemp --suffix=.mp4 /tmp/mo_slice_XXXXXX) || return 1
    if ffmpeg -nostdin -y -hide_banner -loglevel error \
        -ss "$start" -t "$PROBE_DURATION" -i "$file" -map 0:v:0 -an -c copy "$tmp" </dev/null 2>/dev/null; then
        local sz; sz=$(stat -c%s "$tmp" 2>/dev/null || echo 0)
        rm -f "$tmp"
        echo $(( sz * 8 / PROBE_DURATION / 1000 ))
        return 0
    fi
    rm -f "$tmp"
    echo 0
    return 1
}

# ------------------------------------------------------------------------------
# HAUPTSCHLEIFE
# ------------------------------------------------------------------------------
while IFS= read -r -d '' -u 9 src_file; do

    if [[ "$USE_CACHE" == true && "${CACHE_MAP["$src_file"]:-0}" -eq 1 ]]; then
        COUNT_CACHE_SKIPPED=$(( COUNT_CACHE_SKIPPED + 1 ))
        continue
    fi

    src_size=$(stat -c%s "$src_file" 2>/dev/null || echo 0)
    if (( src_size < MIN_SIZE_BYTES )); then
        COUNT_SKIPPED=$(( COUNT_SKIPPED + 1 ))
        add_to_cache "$src_file"; continue
    fi

    current_codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
        -of default=noprint_wrappers=1:nokey=1 "$src_file" 2>/dev/null || echo unknown)
    if [[ "$current_codec" != "h264" ]]; then
        COUNT_SKIPPED=$(( COUNT_SKIPPED + 1 ))
        add_to_cache "$src_file"; continue
    fi

    filename=$(basename "$src_file")
    stem="${filename%.*}"

    if [[ -n "$OUTPUT_DIR" ]]; then
        rel_dir="$(dirname "${src_file#"$SOURCE_DIR"/}")"
        [[ "$rel_dir" == "." ]] && target_folder="$OUTPUT_DIR" || target_folder="$OUTPUT_DIR/$rel_dir"
        [[ "$DRY_RUN" == true ]] || mkdir -p "$target_folder"
        dest_file="$target_folder/${stem}.mp4"
    else
        dest_file="$(dirname "$src_file")/${stem}_h265.mp4"
    fi

    if [[ "$SKIP_EXISTING" == true && -s "$dest_file" ]]; then
        COUNT_SKIPPED=$(( COUNT_SKIPPED + 1 ))
        add_to_cache "$src_file"; add_dest_to_cache "$dest_file"; continue
    fi

    # ---------- Encoder-Wahl ----------
    bitrate_raw=$(ffprobe -v error -select_streams v:0 -show_entries stream=bit_rate \
        -of default=noprint_wrappers=1:nokey=1 "$src_file" 2>/dev/null || true)
    if [[ -z "$bitrate_raw" || "$bitrate_raw" == "N/A" ]]; then
        bitrate_raw=$(ffprobe -v error -show_entries format=bit_rate \
            -of default=noprint_wrappers=1:nokey=1 "$src_file" 2>/dev/null || echo 0)
    fi
    bitrate_kbps=0
    [[ "$bitrate_raw" =~ ^[0-9]+(\.[0-9]+)?$ ]] && bitrate_kbps=$(( ${bitrate_raw%.*} / 1000 ))

    active_encoder="$ENCODER_MODE"
    if [[ "$ENCODER_MODE" == "auto" ]]; then
        if (( bitrate_kbps > 0 && bitrate_kbps < BITRATE_THRESHOLD_KBPS )); then
            active_encoder="cpu"
        else
            active_encoder="gpu"
        fi
    fi

    if [[ "$DRY_RUN" == true ]]; then
        printf "%b[DRY-RUN]%b '%s' (%s, %s kb/s) -> '%s'\n" \
            "$C_CYAN" "$C_RESET" "$filename" "${active_encoder^^}" "$bitrate_kbps" "$(basename "$dest_file")"
        COUNT_DRY=$(( COUNT_DRY + 1 ))
        continue
    fi

    # ---------- Probe ----------
    if [[ "$ENABLE_PROBE" == true ]]; then
        duration_raw=$(media_duration "$src_file")
        duration_sec="${duration_raw%.*}"; duration_sec="${duration_sec:-0}"

        if [[ "$duration_sec" =~ ^[0-9]+$ ]] && (( duration_sec >= PROBE_MIN_DURATION )); then
            start_sec=$(( (duration_sec - PROBE_DURATION) / 2 ))
            probe_file=$(mktemp --suffix=.mp4 /tmp/mo_probe_XXXXXX)
            PROBE_CMD=(ffmpeg -nostdin -y -hide_banner -loglevel error)
            [[ "$active_encoder" == "gpu" ]] && PROBE_CMD+=(-vaapi_device "$VAAPI_DEVICE")
            PROBE_CMD+=(-reinit_filter 0 -ss "$start_sec" -t "$PROBE_DURATION" -i "$src_file")
            if [[ "$active_encoder" == "gpu" ]]; then
                PROBE_CMD+=("${GPU_ENC_ARGS[@]}")
            else
                PROBE_CMD+=(-c:v libx265 -crf "$CPU_CRF" -preset "$CPU_PRESET")
                [[ -n "$CPU_X265_PARAMS" ]] && PROBE_CMD+=(-x265-params "$CPU_X265_PARAMS")
            fi
            PROBE_CMD+=(-an -map 0:v:0 "$probe_file")

            if "${PROBE_CMD[@]}" </dev/null; then
                probe_size=$(stat -c%s "$probe_file" 2>/dev/null || echo 0)
                probe_kbps=$(( probe_size * 8 / PROBE_DURATION / 1000 ))
                rm -f "$probe_file"

                # Referenz identisch messen: gleicher Ausschnitt, video-only
                ref_kbps=$(slice_kbps "$src_file" "$start_sec" || echo 0)

                if (( ref_kbps > 0 && probe_kbps * 100 >= ref_kbps * PROBE_MARGIN_PCT )); then
                    probe_delta=$(pct_change "$ref_kbps" "$probe_kbps")
                    if (( probe_kbps > ref_kbps )); then
                        probe_verdict="waere ${probe_delta#-}% GROESSER"
                    else
                        probe_verdict="nur ${probe_delta#-}% kleiner, Schwelle ${gain_needed}%"
                    fi
                    printf "%b[UEBERSPRUNGEN]%b %s (Probe %s vs. Original %s kb/s: %s)\n" \
                        "$C_YELLOW" "$C_RESET" "$src_file" "$probe_kbps" "$ref_kbps" "$probe_verdict"
                    COUNT_PROBE_SKIPPED=$(( COUNT_PROBE_SKIPPED + 1 ))
                    add_to_cache "$src_file"
                    continue
                fi
            else
                rm -f "$probe_file"
            fi
        fi
    fi

    # ---------- Encoding ----------
    printf "\n%b>>> Verarbeite:%b %s (%s)\n" "$C_BOLD" "$C_RESET" "$src_file" "${active_encoder^^}"
    temp_file="${dest_file}.part.$$.${RANDOM}.mp4"
    err_log=$(mktemp /tmp/mo_enc_err_XXXXXX)

    build_ffmpeg_cmd() {   # $1 = gpu | cpu
        FFMPEG_CMD=(ffmpeg -nostdin -y -hide_banner -loglevel warning -stats)
        [[ "$1" == "gpu" ]] && FFMPEG_CMD+=(-vaapi_device "$VAAPI_DEVICE")
        FFMPEG_CMD+=(-reinit_filter 0 -i "$src_file")
        if [[ "$1" == "gpu" ]]; then
            FFMPEG_CMD+=("${GPU_ENC_ARGS[@]}")
        else
            FFMPEG_CMD+=(-c:v libx265 -crf "$CPU_CRF" -preset "$CPU_PRESET")
            [[ -n "$CPU_X265_PARAMS" ]] && FFMPEG_CMD+=(-x265-params "$CPU_X265_PARAMS")
        fi
        FFMPEG_CMD+=(-tag:v hvc1 -map 0:v:0 -map 0:a? -map 0:s? -map_metadata 0 -c:a copy -c:s copy)
        [[ "$FASTSTART" == true ]] && FFMPEG_CMD+=(-movflags +faststart)
        FFMPEG_CMD+=("$temp_file")
    }

    enc_ok=true
    build_ffmpeg_cmd "$active_encoder"
    "${FFMPEG_CMD[@]}" </dev/null 2>&1 | tee "$err_log" || enc_ok=false

    # Scheitert die GPU an einer echten Datei, wird diese Datei sofort auf der
    # CPU wiederholt und der Rest des Laufs laeuft ebenfalls auf CPU weiter.
    # Damit kann eine falsch negative oder falsch positive Startpruefung den
    # Durchlauf nicht mehr sprengen.
    if [[ "$enc_ok" == false && "$active_encoder" == "gpu" ]]; then
        printf "%b[GPU]%b Encoding von '%s' fehlgeschlagen. Letzte Meldungen:\n" \
            "$C_YELLOW" "$C_RESET" "$filename" >&2
        tail -n 3 "$err_log" | sed 's/^/        /' >&2
        printf "%b[GPU]%b Wiederhole auf CPU und bleibe fuer den Rest des Laufs dabei.\n" \
            "$C_YELLOW" "$C_RESET" >&2
        rm -f "$temp_file"
        active_encoder="cpu"
        ENCODER_MODE="cpu"
        GPU_FELL_BACK=true
        enc_ok=true
        build_ffmpeg_cmd cpu
        "${FFMPEG_CMD[@]}" </dev/null 2>&1 | tee "$err_log" || enc_ok=false
    fi

    if [[ "$enc_ok" == true ]]; then
        dest_size=$(stat -c%s "$temp_file" 2>/dev/null || echo 0)
        diff_bytes=$(( src_size - dest_size ))

        is_salvaged=false
        grep -Eqi "corrupt|partial file|decoding error|invalid nal unit|error splitting" \
            "$err_log" 2>/dev/null && is_salvaged=true
        rm -f "$err_log"; err_log=""

        # ---------- Verifikation vor jeder Loeschentscheidung ----------
        # Bei geretteten Dateien weicht die Dauer legitim ab; dort wird nur
        # auf Decodierbarkeit geprueft und das Original nie geloescht.
        verify_tol="$DURATION_TOLERANCE_PCT"
        [[ "$is_salvaged" == true ]] && verify_tol=100

        if ! verify_video "$src_file" "$temp_file" "$verify_tol"; then
            printf "%b[UNGUELTIG]%b '%s': Ausgabe unvollstaendig oder nicht lesbar. Verworfen, Original bleibt.\n" \
                "$C_RED" "$C_RESET" "$filename" >&2
            rm -f "$temp_file"
            COUNT_UNVERIFIED=$(( COUNT_UNVERIFIED + 1 ))
            save_stats
            continue
        fi

        if (( diff_bytes <= 0 )); then
            inc_pct=$(pct_change "$src_size" "$dest_size")
            inc_h=$(format_bytes $(( dest_size - src_size )))
            src_h=$(format_bytes "$src_size"); dest_h=$(format_bytes "$dest_size")

            if [[ "$is_salvaged" == true && "$KEEP_SALVAGED_CORRUPT" == true ]]; then
                mv "$temp_file" "$dest_file"; touch -r "$src_file" "$dest_file"
                add_to_cache "$src_file"; add_dest_to_cache "$dest_file"
                printf "%b┌──────────────────────────────────────────────────────────────┐%b\n" "$C_MAGENTA" "$C_RESET"
                printf "%b│%b  %bDATEI GERETTET / REPARIERT:%b %s\n" "$C_MAGENTA" "$C_RESET" "$C_BOLD" "$C_RESET" "$filename"
                printf "%b│%b  Original: %s (war beschaedigt)\n" "$C_MAGENTA" "$C_RESET" "$src_h"
                printf "%b│%b  Neu:      %s (+%s%%, +%s)\n" "$C_MAGENTA" "$C_RESET" "$dest_h" "$inc_pct" "$inc_h"
                printf "%b│%b  Status:   Behalten. Original wird NICHT geloescht.\n" "$C_MAGENTA" "$C_RESET"
                printf "%b└──────────────────────────────────────────────────────────────┘%b\n" "$C_MAGENTA" "$C_RESET"
                COUNT_PROCESSED=$(( COUNT_PROCESSED + 1 ))
                COUNT_SALVAGED=$(( COUNT_SALVAGED + 1 ))
                [[ "$active_encoder" == "gpu" ]] && COUNT_GPU=$(( COUNT_GPU + 1 )) || COUNT_CPU=$(( COUNT_CPU + 1 ))
                TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + src_size ))
                TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + dest_size ))
                save_stats
                continue
            elif [[ "$DISCARD_IF_LARGER" == true ]]; then
                rm -f "$temp_file"; add_to_cache "$src_file"
                printf "%b┌──────────────────────────────────────────────────────────────┐%b\n" "$C_YELLOW" "$C_RESET"
                printf "%b│%b  %bDATEI VERWORFEN:%b %s\n" "$C_YELLOW" "$C_RESET" "$C_BOLD" "$C_RESET" "$filename"
                printf "%b│%b  Original: %s -> Neu: %s\n" "$C_YELLOW" "$C_RESET" "$src_h" "$dest_h"
                printf "%b│%b  Status:   Verworfen, da (+%s%%, +%s) groesser.\n" "$C_YELLOW" "$C_RESET" "$inc_pct" "$inc_h"
                printf "%b└──────────────────────────────────────────────────────────────┘%b\n" "$C_YELLOW" "$C_RESET"
                COUNT_DISCARDED=$(( COUNT_DISCARDED + 1 ))
                save_stats
                continue
            fi
        fi

        mv "$temp_file" "$dest_file"; touch -r "$src_file" "$dest_file"

        COUNT_PROCESSED=$(( COUNT_PROCESSED + 1 ))
        [[ "$active_encoder" == "gpu" ]] && COUNT_GPU=$(( COUNT_GPU + 1 )) || COUNT_CPU=$(( COUNT_CPU + 1 ))
        TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + src_size ))
        TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + dest_size ))

        saved_pct=$(pct_change "$src_size" "$dest_size")
        printf "%b┌──────────────────────────────────────────────────────────────┐%b\n" "$C_GREEN" "$C_RESET"
        printf "%b│%b  %bDATEI:%b    %s\n" "$C_GREEN" "$C_RESET" "$C_BOLD" "$C_RESET" "$filename"
        printf "%b│%b  Original: %s -> Neu: %s\n" "$C_GREEN" "$C_RESET" "$(format_bytes "$src_size")" "$(format_bytes "$dest_size")"
        printf "%b│%b  Ersparnis: %b %s%% %b %b%b(-%s)%b\n" "$C_GREEN" "$C_RESET" \
            "$C_BG_GREEN" "$saved_pct" "$C_RESET" "$C_GREEN" "$C_BOLD" "$(format_bytes "$diff_bytes")" "$C_RESET"
        printf "%b└──────────────────────────────────────────────────────────────┘%b\n" "$C_GREEN" "$C_RESET"

        [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src_file"

        # Optionales Umbenennen im In-Place-Modus: "urlaub_h265.mp4" wird
        # wieder zu "urlaub.mp4". Nur wenn das Original tatsaechlich weg ist,
        # sonst wuerde eine noch vorhandene Quelldatei ueberschrieben.
        if [[ "$RENAME_INPLACE" == true && -z "$OUTPUT_DIR" ]]; then
            if [[ -e "$src_file" ]]; then
                printf "%b[UMBENENNEN]%b uebersprungen: '%s' existiert noch.\n" \
                    "$C_YELLOW" "$C_RESET" "$filename" >&2
            elif mv -n "$dest_file" "$src_file" 2>/dev/null && [[ ! -e "$dest_file" ]]; then
                printf "%b[UMBENENNEN]%b '%s' -> '%s'\n" \
                    "$C_CYAN" "$C_RESET" "$(basename "$dest_file")" "$filename"
                dest_file="$src_file"
            else
                printf "%b[UMBENENNEN]%b fehlgeschlagen, '%s' bleibt bestehen.\n" \
                    "$C_YELLOW" "$C_RESET" "$(basename "$dest_file")" >&2
            fi
        fi

        add_to_cache "$src_file"; add_dest_to_cache "$dest_file"
        save_stats
    else
        printf "%b[FEHLER]%b '%s'\n" "$C_RED" "$C_RESET" "$src_file" >&2
        rm -f "$temp_file"; [[ -n "$err_log" ]] && rm -f "$err_log"
        COUNT_FAILED=$(( COUNT_FAILED + 1 ))
        save_stats
    fi
    temp_file=""; err_log=""

done 9< <(find "$SOURCE_DIR" "${FIND_OPTS[@]}" -print0)

# ------------------------------------------------------------------------------
# ABSCHLUSS
# ------------------------------------------------------------------------------
if [[ "$DRY_RUN" == true ]]; then
    printf "\n%b[DRY-RUN]%b %s Video(s) wuerden reencodiert.\n" "$C_CYAN" "$C_RESET" "$COUNT_DRY"
    exit 0
fi

TOTAL_ELAPSED=$(( PREV_ELAPSED + $(date +%s) - START_TIME ))
TOTAL_SAVED=$(( TOTAL_ORIG_BYTES - TOTAL_NEW_BYTES ))

printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b                  %bGESAMTAUSWERTUNG%b                            %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
printf "%b╠══════════════════════════════════════════════════════════════╣%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b  Gesamtlaufzeit:       %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$(format_duration "$TOTAL_ELAPSED")" "$C_BLUE" "$C_RESET"
printf "%b║%b  Neu kodiert:          %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$COUNT_PROCESSED Datei(en) (GPU: $COUNT_GPU | CPU: $COUNT_CPU)" "$C_BLUE" "$C_RESET"
(( COUNT_SALVAGED > 0 )) && \
printf "%b║%b  %bGerettet (repariert):%b %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$C_MAGENTA" "$C_RESET" "$COUNT_SALVAGED Datei(en)" "$C_BLUE" "$C_RESET"
printf "%b║%b  Cache (abgeschlossen):%-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$COUNT_CACHE_SKIPPED Datei(en)" "$C_BLUE" "$C_RESET"
printf "%b║%b  Uebersprungen:        %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$COUNT_SKIPPED Datei(en)" "$C_BLUE" "$C_RESET"
printf "%b║%b  Probe (kein Gewinn):  %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$COUNT_PROBE_SKIPPED Datei(en)" "$C_BLUE" "$C_RESET"
printf "%b║%b  Verworfen (groesser): %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$COUNT_DISCARDED Datei(en)" "$C_BLUE" "$C_RESET"
(( COUNT_UNVERIFIED > 0 )) && \
printf "%b║%b  %bVerifikation fehlgesch.:%b %-35s %b║%b\n" "$C_BLUE" "$C_RESET" "$C_RED" "$C_RESET" "$COUNT_UNVERIFIED Datei(en)" "$C_BLUE" "$C_RESET"
printf "%b║%b  Fehlgeschlagen:       %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$COUNT_FAILED Datei(en)" "$C_BLUE" "$C_RESET"
printf "%b╟──────────────────────────────────────────────────────────────╢%b\n" "$C_BLUE" "$C_RESET"

if (( COUNT_PROCESSED > 0 && TOTAL_ORIG_BYTES > 0 )); then
    printf "%b║%b  Speicher vorher:      %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$(format_bytes "$TOTAL_ORIG_BYTES")" "$C_BLUE" "$C_RESET"
    printf "%b║%b  Speicher nachher:     %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$(format_bytes "$TOTAL_NEW_BYTES")" "$C_BLUE" "$C_RESET"
    PCT=$(pct_change "$TOTAL_ORIG_BYTES" "$TOTAL_NEW_BYTES")
    if (( TOTAL_SAVED > 0 )); then
        printf "%b║%b  %bGesamtersparnis:%b      %b%-38s%b %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_GREEN" "-$(format_bytes "$TOTAL_SAVED") (${PCT}%)" "$C_RESET" "$C_BLUE" "$C_RESET"
    else
        printf "%b║%b  %bZuwachs:%b              %b%-38s%b %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_YELLOW" "+$(format_bytes $(( -TOTAL_SAVED ))) (+${PCT}%)" "$C_RESET" "$C_BLUE" "$C_RESET"
    fi
else
    printf "%b║%b  %-60s %b║%b\n" "$C_BLUE" "$C_RESET" "Keine neuen Videos uebernommen." "$C_BLUE" "$C_RESET"
fi
printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_BLUE" "$C_RESET"

if [[ "$GPU_FELL_BACK" == true ]]; then
    printf "\n%b[HINWEIS]%b Die GPU hat waehrend des Laufs versagt, es wurde auf CPU umgestellt.\n" \
        "$C_YELLOW" "$C_RESET"
    printf "          Ursache oben in den ffmpeg-Meldungen. Bis dahin: --encoder cpu.\n"
fi

resolve_pending_deletes
rm -f "$STATS_FILE"
