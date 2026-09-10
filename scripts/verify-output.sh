#!/bin/bash
# ==============================================================================
# verify-output.sh
#
# Prueft ein Zielverzeichnis gegen das Quellverzeichnis: stimmt die Laufzeit
# und passt der Bildinhalt? Faengt damit auch zerstoerte Farbformate, die eine
# reine Laufzeitpruefung passieren (gruenes Bild mit korrekter Dauer).
#
# Standard ist ein reiner Bericht. --fix loescht die defekten Ausgaben und
# nimmt die zugehoerigen Quelldateien aus dem Cache, sodass ein normaler Lauf
# von h264-to-h265.sh genau diese Dateien neu erzeugt. --run startet ihn gleich.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
_MO_ROOT="$(dirname "$SCRIPT_DIR")"; load_config
mo_install_exit_handler

usage() {
    cat <<'EOF'
verify-output.sh - prueft konvertierte Videos gegen ihre Originale

  -i, --input   <dir>   Quellverzeichnis (die Originale)
  -o, --output  <dir>   Zielverzeichnis (die konvertierten Dateien)
  -j, --workers <n>     Parallele Pruefungen (Default: nproc)
      --psnr-min <db>   Schwelle fuer Verdacht (Default: 15)
      --duration-tol <p> Erlaubte Laufzeitabweichung in Prozent (Default: 2).
                         Gerettete Dateien aus beschaedigten Quellen sind
                         legitim kuerzer, hier hilft ein hoeherer Wert.
      --samples <n>     Stichproben pro Datei (Default: 3)
      --keep-samples <d> Verglichene Einzelbilder in <d> ablegen (zum Ansehen)
      --fix             Defekte Ausgaben loeschen und aus dem Cache nehmen
      --run             Nach --fix h264-to-h265.sh neu starten
      --hold            Fenster am Ende offen halten
      --no-hold         Fenster nie offen halten
  -h, --help            Diese Hilfe

Beispiele:
  ./verify-output.sh -i ~/Videos -o /mnt/archiv            # nur pruefen
  ./verify-output.sh -i ~/Videos -o /mnt/archiv --fix --run
EOF
}

SOURCE_DIR="${SOURCE_DIR:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
MAX_WORKERS="${MAX_WORKERS:-$(nproc)}"
PSNR_MIN="${VISUAL_PSNR_MIN:-15}"
SAMPLES="${SAMPLES:-3}"
VISUAL_KEEP_DIR="${VISUAL_KEEP_DIR:-}"
DURATION_TOL="${DURATION_TOLERANCE_PCT:-2}"
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
        --fix)          DO_FIX=true; shift ;;
        --run)          DO_RUN=true; shift ;;
        --hold)         MO_HOLD=1; shift ;;
        --no-hold)      MO_HOLD=0; shift ;;
        -h|--help)      usage; exit 0 ;;
        --)             shift; while (( $# > 0 )); do POSITIONAL+=("$1"); shift; done ;;
        -*)             echo "Unbekannte Option: $1" >&2; usage >&2; exit 2 ;;
        *)              POSITIONAL+=("$1"); shift ;;
    esac
done
[[ -n "${POSITIONAL[0]:-}" ]] && SOURCE_DIR="${POSITIONAL[0]}"
[[ -n "${POSITIONAL[1]:-}" ]] && OUTPUT_DIR="${POSITIONAL[1]}"

[[ -n "$SOURCE_DIR" && -n "$OUTPUT_DIR" ]] || {
    echo "Quell- und Zielverzeichnis werden beide gebraucht." >&2; usage >&2; exit 2; }
[[ -d "$SOURCE_DIR" ]] || { echo "Quellverzeichnis fehlt: $SOURCE_DIR" >&2; exit 1; }
[[ -d "$OUTPUT_DIR" ]] || { echo "Zielverzeichnis fehlt: $OUTPUT_DIR" >&2; exit 1; }
SOURCE_DIR="${SOURCE_DIR%/}"; OUTPUT_DIR="${OUTPUT_DIR%/}"
[[ "$SOURCE_DIR" == "$OUTPUT_DIR" ]] && {
    echo "Quelle und Ziel sind identisch. Fuer den In-Place-Modus ist dieses Skript nicht gedacht." >&2
    exit 2; }

require_cmds ffmpeg ffprobe || exit 1

RESULT_LOG=$(mktemp /tmp/mo_verify_XXXXXX)
BROKEN_LIST="${OUTPUT_DIR}/.defekte_videos.txt"
VISUAL_SAMPLES="$SAMPLES"
export RESULT_LOG PSNR_MIN SAMPLES VISUAL_SAMPLES VISUAL_KEEP_DIR DURATION_TOL SOURCE_DIR OUTPUT_DIR

printf "%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b               %bAUSGABE-PRUEFUNG (VIDEO)%b                       %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_BLUE" "$C_RESET"
printf "  Quelle: %s\n  Ziel:   %s\n  Schwelle: %s dB, %s Stichproben, Laufzeit-Toleranz %s%%, %s Worker\n\n" \
    "$SOURCE_DIR" "$OUTPUT_DIR" "$PSNR_MIN" "$SAMPLES" "$DURATION_TOL" "$MAX_WORKERS"

# ------------------------------------------------------------------------------
# WORKER
# ------------------------------------------------------------------------------
# Vergleicht eine Ausgabedatei mit ihrem Original. Die Zuordnung ist der
# relative Pfad, weil h264-to-h265.sh im Zielverzeichnis die Ordnerstruktur
# spiegelt und den Dateinamen beibehaelt.
check_one() {
    local out="$1"
    local rel="${out#"$OUTPUT_DIR"/}"
    local src="$SOURCE_DIR/$rel"

    if [[ ! -f "$src" ]]; then
        printf 'NOSRC\t%s\t\t\n' "$out" >> "$RESULT_LOG"
        return 0
    fi
    if ! ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
         -of default=noprint_wrappers=1:nokey=1 "$out" >/dev/null 2>&1; then
        printf 'UNREADABLE\t%s\t%s\t\n' "$out" "$src" >> "$RESULT_LOG"
        printf "%b[UNLESBAR]%b %s\n" "$C_RED" "$C_RESET" "$rel" >&2
        return 0
    fi
    if ! verify_video "$src" "$out" "$DURATION_TOL"; then
        local d_src d_out
        d_src=$(media_duration "$src"); d_out=$(media_duration "$out")
        printf 'DURATION\t%s\t%s\t%s\n' "$out" "$src" "${d_out:-?}" >> "$RESULT_LOG"
        printf "%b[DAUER]%b    %s  (Original %ss, Ausgabe %ss, Toleranz %s%%)\n" \
            "$C_RED" "$C_RESET" "$rel" "${d_src:-?}" "${d_out:-?}" "$DURATION_TOL" >&2
        return 0
    fi
    if ! verify_visual "$src" "$out" "$PSNR_MIN"; then
        printf 'VISUAL\t%s\t%s\t%s\n' "$out" "$src" "$VISUAL_PSNR" >> "$RESULT_LOG"
        printf "%b[BILD?]%b     %s  (bester Wert %s dB; Stichproben: %s)\n" \
            "$C_YELLOW" "$C_RESET" "$rel" "$VISUAL_PSNR" "${VISUAL_PSNR_ALL:-keine}" >&2
        return 0
    fi
    printf 'OK\t%s\t%s\t\n' "$out" "$src" >> "$RESULT_LOG"
    printf "%b[OK]%b       %s\n" "$C_GREEN" "$C_RESET" "$rel"
    return 0
}
export -f check_one verify_video verify_visual media_duration

TOTAL=$(find "$OUTPUT_DIR" -type f -iname "*.mp4" ! -name "*.part.*.mp4" 2>/dev/null | wc -l || true)
if (( TOTAL == 0 )); then
    echo "Keine MP4-Dateien im Zielverzeichnis gefunden."
    rm -f "$RESULT_LOG"
    exit 0
fi
printf "  %d Datei(en) werden geprueft...\n\n" "$TOTAL"

find "$OUTPUT_DIR" -type f -iname "*.mp4" ! -name "*.part.*.mp4" -print0 |
    xargs -0 -r -n 1 -P "$MAX_WORKERS" bash -c 'check_one "$1"' _ || true

# ------------------------------------------------------------------------------
# AUSWERTUNG
# ------------------------------------------------------------------------------
n_ok=$(grep -c '^OK' "$RESULT_LOG" || true)
n_vis=$(grep -c '^VISUAL' "$RESULT_LOG" || true)
n_dur=$(grep -c '^DURATION' "$RESULT_LOG" || true)
n_unr=$(grep -c '^UNREADABLE' "$RESULT_LOG" || true)
n_nosrc=$(grep -c '^NOSRC' "$RESULT_LOG" || true)
n_bad=$(( n_vis + n_dur + n_unr ))

printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b                      %bERGEBNIS%b                                %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
printf "%b╠══════════════════════════════════════════════════════════════╣%b\n" "$C_BLUE" "$C_RESET"
printf "%b║%b  In Ordnung:           %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$n_ok Datei(en)" "$C_BLUE" "$C_RESET"
printf "%b║%b  Bild auffaellig:      %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$n_vis Datei(en)" "$C_BLUE" "$C_RESET"
printf "%b║%b  Laufzeit abweichend:  %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$n_dur Datei(en)" "$C_BLUE" "$C_RESET"
printf "%b║%b  Nicht lesbar:         %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$n_unr Datei(en)" "$C_BLUE" "$C_RESET"
printf "%b║%b  Ohne Original:        %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$n_nosrc Datei(en)" "$C_BLUE" "$C_RESET"
printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_BLUE" "$C_RESET"

if (( n_nosrc > 0 )); then
    printf "\n%b[HINWEIS]%b %d Ausgabe(n) ohne passendes Original. Diese werden nicht\n" \
        "$C_YELLOW" "$C_RESET" "$n_nosrc"
    printf "          angetastet, da nichts zum Vergleichen und Neuerzeugen da ist.\n"
fi

if (( n_bad == 0 )); then
    printf "\n%b[FERTIG]%b Keine defekten Dateien gefunden.\n" "$C_GREEN" "$C_RESET"
    rm -f "$RESULT_LOG" "$BROKEN_LIST"
    exit 0
fi

# Liste der betroffenen Quelldateien sichern
awk -F'\t' '/^(VISUAL|DURATION|UNREADABLE)/ && $3 != "" {print $3}' "$RESULT_LOG" | sort -u > "$BROKEN_LIST"
printf "\n%b%d defekte Datei(en).%b Liste der betroffenen Originale: %s\n" \
    "$C_BOLD" "$n_bad" "$C_RESET" "$BROKEN_LIST"

if [[ "$DO_FIX" != true ]]; then
    printf "\n%b[WICHTIG]%b Ein niedriger PSNR ist ein Verdacht, kein Beweis. Bitte erst\n" "$C_YELLOW" "$C_RESET"
    printf "          ein paar der gemeldeten Dateien selbst abspielen. Einzelbilder\n"
    printf "          zum Vergleich ablegen: --keep-samples /tmp/stichproben\n"
    printf "\nNichts geaendert. Erst nach eigener Pruefung reparieren:\n"
    printf "  %s -i %q -o %q --fix --run\n" "$0" "$SOURCE_DIR" "$OUTPUT_DIR"
    rm -f "$RESULT_LOG"
    exit 0
fi

# ------------------------------------------------------------------------------
# REPARATUR
# Nur die defekten Ausgaben entfernen und die zugehoerigen Quelldateien aus
# dem Cache nehmen. h264-to-h265.sh ueberspringt vorhandene Ausgaben, erzeugt
# also beim naechsten Lauf genau die geloeschten neu.
# ------------------------------------------------------------------------------
CACHE_FILE="${SOURCE_DIR}/.video_conversion_cache.txt"
removed=0
while IFS=$'\t' read -r status out src _; do
    case "$status" in VISUAL|DURATION|UNREADABLE) ;; *) continue ;; esac
    [[ -n "$out" && -f "$out" ]] || continue
    if rm -f -- "$out"; then
        removed=$(( removed + 1 ))
        printf "  %b[GELOESCHT]%b %s\n" "$C_CYAN" "$C_RESET" "${out#"$OUTPUT_DIR"/}"
    fi
done < "$RESULT_LOG"

pruned=0
if [[ -f "$CACHE_FILE" ]]; then
    cp -p "$CACHE_FILE" "${CACHE_FILE}.bak"
    if grep -Fxv -f "$BROKEN_LIST" "$CACHE_FILE" > "${CACHE_FILE}.tmp" 2>/dev/null; then :; fi
    pruned=$(( $(wc -l < "$CACHE_FILE") - $(wc -l < "${CACHE_FILE}.tmp") ))
    mv "${CACHE_FILE}.tmp" "$CACHE_FILE"
    printf "\n  %d Ausgabe(n) geloescht, %d Cache-Eintrag/Eintraege entfernt.\n" "$removed" "$pruned"
    printf "  Sicherung des Cache: %s\n" "${CACHE_FILE}.bak"
else
    printf "\n  %d Ausgabe(n) geloescht. Kein Cache vorhanden.\n" "$removed"
fi

rm -f "$RESULT_LOG"

if [[ "$DO_RUN" != true ]]; then
    printf "\nJetzt neu erzeugen mit:\n"
    printf "  %q/h264-to-h265.sh -i %q -o %q --from-list %q\n" \
        "$SCRIPT_DIR" "$SOURCE_DIR" "$OUTPUT_DIR" "$BROKEN_LIST"
    exit 0
fi

printf "\n%b▶ Starte Neuerzeugung fuer %d Datei(en) aus der Liste...%b\n" \
    "$C_CYAN" "$(wc -l < "$BROKEN_LIST")" "$C_RESET"
# --from-list statt kompletter Neudurchlauf: es wird nur genau das
# wiederholt, was als defekt gemeldet wurde. Kein find ueber den ganzen
# Baum, kein ffprobe ueber unbeteiligte Dateien.
MO_HOLD=0 "$SCRIPT_DIR/h264-to-h265.sh" -i "$SOURCE_DIR" -o "$OUTPUT_DIR" --from-list "$BROKEN_LIST"
# Liste archivieren, nicht liegen lassen. Sonst wuerde ein spaeterer Lauf mit
# --from-list auto eine veraltete Auswahl abarbeiten und die uebrigen Dateien
# stillschweigend nie ansehen.
ARCHIV="${OUTPUT_DIR}/.defekte_videos.erledigt-$(date +%Y%m%d-%H%M%S).txt"
if mv "$BROKEN_LIST" "$ARCHIV" 2>/dev/null; then
    printf "\n  Defektliste archiviert als %s\n" "$(basename "$ARCHIV")"
fi

printf "\n%bNeuerzeugung beendet. Zur Kontrolle nochmals pruefen:%b\n" "$C_BOLD" "$C_RESET"
printf "  %s -i %q -o %q\n" "$0" "$SOURCE_DIR" "$OUTPUT_DIR"
