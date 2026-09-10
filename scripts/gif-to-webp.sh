#!/bin/bash
# ==============================================================================
# gif-to-webp.sh  -  GIF -> animiertes WebP (verlustfrei)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
_MO_ROOT="$(dirname "$SCRIPT_DIR")"; load_config
mo_install_exit_handler

usage() {
    cat <<'EOF'
gif-to-webp.sh - konvertiert GIF nach verlustfreiem WebP (gif2webp)

  -i, --input   <dir>   Quellverzeichnis
  -o, --output  <dir>   Zielverzeichnis (leer = In-Place)
  -j, --workers <n>     Parallele Worker (Default: nproc)
  -m, --method  <0-6>   Kompressionsstufe (Default: 6)
      --delete          Originale nach Erfolg in den Papierkorb
      --no-delete       Originale behalten (Default)
      --force-delete    Falls Papierkorb fehlschlaegt: ohne Rueckfrage rm
      --keep-larger     Ergebnis auch behalten, wenn es groesser ist
      --verify-deep     Ausgabe mit webpinfo pruefen
  -n, --dry-run         Nur anzeigen, nichts schreiben
      --hold            Fenster am Ende offen halten (fuer Doppelklick-Start)
      --no-hold         Fenster nie offen halten
  -h, --help            Diese Hilfe
EOF
}

SOURCE_DIR="${SOURCE_DIR:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
MAX_WORKERS="${MAX_WORKERS:-$(nproc)}"
DELETE_ORIGINAL="${DELETE_ORIGINAL:-false}"
FORCE_DELETE="${FORCE_DELETE:-false}"
COMPRESSION_METHOD="${COMPRESSION_METHOD:-6}"
DISCARD_IF_LARGER="${DISCARD_IF_LARGER:-true}"
GIF_KMIN="${GIF_KMIN:-0}"   # 0 = keine Keyframes (beste Kompression, wie bisher)
DRY_RUN="${DRY_RUN:-false}"
VERIFY_DEEP="${VERIFY_DEEP:-false}"
SKIP_EXISTING=true

POSITIONAL=()
while (( $# > 0 )); do
    case "$1" in
        -i|--input)     SOURCE_DIR="$2"; shift 2 ;;
        -o|--output)    OUTPUT_DIR="$2"; shift 2 ;;
        -j|--workers)   MAX_WORKERS="$2"; shift 2 ;;
        -m|--method)    COMPRESSION_METHOD="$2"; shift 2 ;;
        --delete)       DELETE_ORIGINAL=true; shift ;;
        --no-delete)    DELETE_ORIGINAL=false; shift ;;
        --force-delete) FORCE_DELETE=true; shift ;;
        --keep-larger)  DISCARD_IF_LARGER=false; shift ;;
        --verify-deep)  VERIFY_DEEP=true; shift ;;
        -n|--dry-run)   DRY_RUN=true; shift ;;
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

if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" ]]; then
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    printf "%bBatch GIF zu WebP Konverter (via gif2webp)%b\n" "$C_BOLD" "$C_RESET"
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    read -rp "Mit Standardeinstellungen ausfuehren? [J/n]: " start_choice || start_choice=""
    if [[ "${start_choice,,}" =~ ^(n|nein|no)$ ]]; then
        read -rp "  Quellordner [$SOURCE_DIR]: " x && SOURCE_DIR="${x:-$SOURCE_DIR}"
        read -rp "  Zielordner (leer = In-Place) [$OUTPUT_DIR]: " x && OUTPUT_DIR="${x:-$OUTPUT_DIR}"
        read -rp "  Originale in den Papierkorb? [j/N]: " x
        [[ "${x,,}" =~ ^(j|ja|y|yes)$ ]] && DELETE_ORIGINAL=true || DELETE_ORIGINAL=false
        read -rp "  Parallele Worker [$MAX_WORKERS]: " x && MAX_WORKERS="${x:-$MAX_WORKERS}"
        read -rp "  Kompressionsstufe (0-6) [$COMPRESSION_METHOD]: " x && COMPRESSION_METHOD="${x:-$COMPRESSION_METHOD}"
    fi
fi

[[ -z "$SOURCE_DIR" ]] && { echo "Kein Quellverzeichnis." >&2; exit 1; }
[[ -d "$SOURCE_DIR" ]] || { echo "Quellverzeichnis fehlt: $SOURCE_DIR" >&2; exit 1; }
SOURCE_DIR="${SOURCE_DIR%/}"
if [[ -n "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="${OUTPUT_DIR%/}"
    [[ "$DRY_RUN" == true ]] || mkdir -p "$OUTPUT_DIR"
fi

require_cmds gif2webp || exit 1
[[ "$DRY_RUN" == true ]] && printf "%b[DRY-RUN]%b Es wird nichts geschrieben oder geloescht.\n" "$C_CYAN" "$C_RESET"

cleanup_stale_parts "${OUTPUT_DIR:-$SOURCE_DIR}" "*.part.*.webp"

# ------------------------------------------------------------------------------
# STATISTIK
# ------------------------------------------------------------------------------
STATS_FILE="${SOURCE_DIR}/.gif_stats.env"
PENDING_DELETE_LOG="${SOURCE_DIR}/.gif_pending_deletes.txt"
CURRENT_RUN_LOG=$(mktemp /tmp/gif_stats_XXXXXX)
export CURRENT_RUN_LOG PENDING_DELETE_LOG DRY_RUN VERIFY_DEEP FORCE_DELETE

if [[ "${RESUMING:-false}" == "true" && -f "$STATS_FILE" ]]; then
    source "$STATS_FILE" 2>/dev/null || true
else
    rm -f "$STATS_FILE"
fi

TOTAL_PROCESSED=${TOTAL_PROCESSED:-0}; TOTAL_SKIPPED=${TOTAL_SKIPPED:-0}
TOTAL_FAILED=${TOTAL_FAILED:-0};       TOTAL_DISCARDED=${TOTAL_DISCARDED:-0}
TOTAL_COLLISION=${TOTAL_COLLISION:-0}
TOTAL_ORIG_BYTES=${TOTAL_ORIG_BYTES:-0}; TOTAL_NEW_BYTES=${TOTAL_NEW_BYTES:-0}
PREV_ELAPSED=${PREV_ELAPSED:-0}
START_TIME=$(date +%s)

finalize_stats() {
    local exit_code="$1"
    local tot_elapsed=$(( PREV_ELAPSED + $(date +%s) - START_TIME ))

    local c_proc=0 c_skip=0 c_fail=0 c_disc=0 c_coll=0 c_orig=0 c_new=0 c_dry=0
    if [[ -f "$CURRENT_RUN_LOG" ]]; then
        c_proc=$(grep -c "^SUCCESS"   "$CURRENT_RUN_LOG" || true)
        c_skip=$(grep -c "^SKIP"      "$CURRENT_RUN_LOG" || true)
        c_fail=$(grep -c "^FAIL"      "$CURRENT_RUN_LOG" || true)
        c_disc=$(grep -c "^DISCARD"   "$CURRENT_RUN_LOG" || true)
        c_coll=$(grep -c "^COLLISION" "$CURRENT_RUN_LOG" || true)
        c_dry=$(grep -c "^DRY"        "$CURRENT_RUN_LOG" || true)
        c_orig=$(awk '/^SUCCESS/ {s += $2} END {print s+0}' "$CURRENT_RUN_LOG")
        c_new=$(awk  '/^SUCCESS/ {s += $3} END {print s+0}' "$CURRENT_RUN_LOG")
        rm -f "$CURRENT_RUN_LOG"
    fi

    TOTAL_PROCESSED=$(( TOTAL_PROCESSED + c_proc ))
    TOTAL_SKIPPED=$(( TOTAL_SKIPPED + c_skip ))
    TOTAL_FAILED=$(( TOTAL_FAILED + c_fail ))
    TOTAL_DISCARDED=$(( TOTAL_DISCARDED + c_disc ))
    TOTAL_COLLISION=$(( TOTAL_COLLISION + c_coll ))
    TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + c_orig ))
    TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + c_new ))

    if [[ $exit_code -eq 130 ]]; then
        cat > "$STATS_FILE" <<EOF
TOTAL_PROCESSED=$TOTAL_PROCESSED
TOTAL_SKIPPED=$TOTAL_SKIPPED
TOTAL_FAILED=$TOTAL_FAILED
TOTAL_DISCARDED=$TOTAL_DISCARDED
TOTAL_COLLISION=$TOTAL_COLLISION
TOTAL_ORIG_BYTES=$TOTAL_ORIG_BYTES
TOTAL_NEW_BYTES=$TOTAL_NEW_BYTES
PREV_ELAPSED=$tot_elapsed
EOF
        resolve_pending_deletes
        exit 130
    fi
    rm -f "$STATS_FILE"

    if [[ "$DRY_RUN" == true ]]; then
        printf "\n%b[DRY-RUN]%b %s GIF(s) wuerden konvertiert, %s uebersprungen.\n" \
            "$C_CYAN" "$C_RESET" "$c_dry" "$TOTAL_SKIPPED"
        return 0
    fi

    local saved=$(( TOTAL_ORIG_BYTES - TOTAL_NEW_BYTES ))
    printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
    printf "%b║%b                  %bGIF-AUSWERTUNG%b                              %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
    printf "%b╠══════════════════════════════════════════════════════════════╣%b\n" "$C_BLUE" "$C_RESET"
    printf "%b║%b  Gesamtlaufzeit:       %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$(format_duration "$tot_elapsed")" "$C_BLUE" "$C_RESET"
    printf "%b║%b  Konvertiert:          %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$TOTAL_PROCESSED Datei(en)" "$C_BLUE" "$C_RESET"
    printf "%b║%b  Uebersprungen:        %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$TOTAL_SKIPPED Datei(en)" "$C_BLUE" "$C_RESET"
    printf "%b║%b  Verworfen (groesser): %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$TOTAL_DISCARDED Datei(en)" "$C_BLUE" "$C_RESET"
    (( TOTAL_COLLISION > 0 )) && \
    printf "%b║%b  Namenskonflikt:       %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$TOTAL_COLLISION Datei(en)" "$C_BLUE" "$C_RESET"
    printf "%b║%b  Fehlgeschlagen:       %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$TOTAL_FAILED Datei(en)" "$C_BLUE" "$C_RESET"
    printf "%b╟──────────────────────────────────────────────────────────────╢%b\n" "$C_BLUE" "$C_RESET"

    if (( TOTAL_PROCESSED > 0 && TOTAL_ORIG_BYTES > 0 )); then
        printf "%b║%b  Speicher vorher:      %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$(format_bytes "$TOTAL_ORIG_BYTES")" "$C_BLUE" "$C_RESET"
        printf "%b║%b  Speicher nachher:     %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$(format_bytes "$TOTAL_NEW_BYTES")" "$C_BLUE" "$C_RESET"
        local pct; pct=$(pct_change "$TOTAL_ORIG_BYTES" "$TOTAL_NEW_BYTES")
        if (( saved > 0 )); then
            printf "%b║%b  %bGesamtersparnis:%b      %b%-38s%b %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_GREEN" "-$(format_bytes "$saved") (${pct}%)" "$C_RESET" "$C_BLUE" "$C_RESET"
        else
            printf "%b║%b  %bZuwachs:%b              %b%-38s%b %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_YELLOW" "+$(format_bytes $(( -saved ))) (+${pct}%)" "$C_RESET" "$C_BLUE" "$C_RESET"
        fi
    fi
    printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_BLUE" "$C_RESET"

    resolve_pending_deletes
}

trap 'printf "\n%b[ABBRUCH]%b GIF-Konvertierung gestoppt.\n" "$C_RED" "$C_RESET"; finalize_stats 130' SIGINT SIGTERM

# ------------------------------------------------------------------------------
# WORKER
# ------------------------------------------------------------------------------
convert_gif() {
    local src="$1"
    local orig_size; orig_size=$(stat -c%s "$src" 2>/dev/null || echo 0)
    local filename; filename="$(basename "$src")"
    local stem="${filename%.*}"

    local target_folder
    if [[ -n "${OUTPUT_DIR:-}" ]]; then
        local rel_dir; rel_dir="$(dirname "${src#"$SOURCE_DIR"/}")"
        [[ "$rel_dir" == "." ]] && target_folder="$OUTPUT_DIR" || target_folder="$OUTPUT_DIR/$rel_dir"
        [[ "$DRY_RUN" == true ]] || mkdir -p "$target_folder"
    else
        target_folder="$(dirname "$src")"
    fi
    local final_dest="$target_folder/${stem}.webp"

    if [[ "$SKIP_EXISTING" == true && -s "$final_dest" ]]; then
        echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
    fi

    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        echo "DRY" >> "$CURRENT_RUN_LOG"
        printf "%b[DRY-RUN]%b '%s' -> '%s'\n" "$C_CYAN" "$C_RESET" "$filename" "$(basename "$final_dest")"
        return 0
    fi

    local lockdir="$target_folder/.${stem}.molock"
    if ! mkdir "$lockdir" 2>/dev/null; then
        echo "COLLISION" >> "$CURRENT_RUN_LOG"
        printf "%b[KONFLIKT]%b '%s': Zielname bereits belegt, Original bleibt.\n" \
            "$C_YELLOW" "$C_RESET" "$filename" >&2
        return 0
    fi

    local temp_dest="${final_dest}.part.$$.${RANDOM}.webp"
    local rc=0

    # Hinweis: gif2webp kodiert per Default verlustfrei. Ein Flag "-lossless"
    # existiert nicht und laesst den Aufruf komplett fehlschlagen.
    if gif2webp -m "$COMPRESSION_METHOD" -kmin "$GIF_KMIN" -metadata all -quiet \
                "$src" -o "$temp_dest" 2>/dev/null \
       && [[ -s "$temp_dest" ]] && verify_output_image "$temp_dest"; then

        local new_size; new_size=$(stat -c%s "$temp_dest" 2>/dev/null || echo 0)

        if [[ "$DISCARD_IF_LARGER" == true ]] && (( new_size >= orig_size )); then
            rm -f "$temp_dest"
            echo "DISCARD" >> "$CURRENT_RUN_LOG"
            printf "%b[VERWORFEN]%b '%s': WebP waere %s groesser, Original bleibt.\n" \
                "$C_YELLOW" "$C_RESET" "$filename" "$(format_bytes $(( new_size - orig_size )))"
        else
            mv "$temp_dest" "$final_dest"
            touch -r "$src" "$final_dest"
            echo "SUCCESS $orig_size $new_size" >> "$CURRENT_RUN_LOG"
            [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src"
            printf "%b[OK]%b   '%s' -> '%s'\n" "$C_GREEN" "$C_RESET" "$filename" "$(basename "$final_dest")"
        fi
    else
        rm -f "$temp_dest"
        echo "FAIL" >> "$CURRENT_RUN_LOG"
        printf "%b[FEHLER]%b '%s' (Original bleibt)\n" "$C_RED" "$C_RESET" "$filename" >&2
        rc=1
    fi

    rmdir "$lockdir" 2>/dev/null || true
    return $rc
}

export SOURCE_DIR OUTPUT_DIR SKIP_EXISTING DELETE_ORIGINAL COMPRESSION_METHOD DISCARD_IF_LARGER GIF_KMIN
export -f convert_gif

exit_code=0
find "$SOURCE_DIR" -type f -iname "*.gif" -print0 |
    xargs -0 -r -n 1 -P "$MAX_WORKERS" bash -c 'convert_gif "$1"' _ || exit_code=$?

# xargs meldet 124/125 bei Abbruch, 123 bei Worker-Fehlern (nicht Abbruch)
if (( exit_code == 124 || exit_code == 125 || exit_code == 130 )); then
    finalize_stats 130
else
    finalize_stats 0
fi
