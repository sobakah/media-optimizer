#!/bin/bash
# ==============================================================================
# img-to-jxl.sh  -  JPG/PNG -> JXL (mit WebP-Fallback)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
_MO_ROOT="$(dirname "$SCRIPT_DIR")"; load_config

usage() {
    cat <<'EOF'
img-to-jxl.sh - konvertiert JPG/PNG nach JPEG XL (WebP als Fallback)

  -i, --input   <dir>   Quellverzeichnis
  -o, --output  <dir>   Zielverzeichnis (leer = In-Place)
  -j, --workers <n>     Parallele Worker (Default: nproc)
  -e, --effort  <1-9>   JXL Effort (Default: 7)
      --png-mode  <m>   lossless | lossy   (Default: lossless)
      --png-quality <q> Nur bei --png-mode lossy (Default: 90)
      --delete          Originale nach Erfolg in den Papierkorb
      --no-delete       Originale behalten (Default)
      --force-delete    Falls Papierkorb fehlschlaegt: ohne Rueckfrage rm
      --cjxl-threads <n> Threads pro cjxl-Prozess (Default: 1, da parallel)
      --verify-deep     Ausgabe vollstaendig dekodieren (langsamer, sicherer)
  -n, --dry-run         Nur anzeigen, nichts schreiben
  -h, --help            Diese Hilfe

Positionsargumente werden weiterhin akzeptiert: img-to-jxl.sh <input> [output]
EOF
}

# ------------------------------------------------------------------------------
# KONFIGURATION
# ------------------------------------------------------------------------------
SOURCE_DIR="${SOURCE_DIR:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
MAX_WORKERS="${MAX_WORKERS:-$(nproc)}"
DELETE_ORIGINAL="${DELETE_ORIGINAL:-false}"
FORCE_DELETE="${FORCE_DELETE:-false}"
JXL_EFFORT="${JXL_EFFORT:-7}"
PNG_MODE="${PNG_MODE:-lossless}"
PNG_QUALITY="${PNG_QUALITY:-90}"
DRY_RUN="${DRY_RUN:-false}"
VERIFY_DEEP="${VERIFY_DEEP:-false}"
# Jeder Worker ist ein eigener cjxl-Prozess. cjxl wuerde per Default nochmal
# so viele Threads starten wie Kerne vorhanden sind (16 Worker x 16 Threads
# auf einem 8C/16T-Chip). 1 Thread pro Prozess vermeidet die Ueberbuchung.
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
        --png-mode)       PNG_MODE="$2"; shift 2 ;;
        --png-quality)    PNG_QUALITY="$2"; shift 2 ;;
        --delete)         DELETE_ORIGINAL=true; shift ;;
        --no-delete)      DELETE_ORIGINAL=false; shift ;;
        --force-delete)   FORCE_DELETE=true; shift ;;
        --cjxl-threads)   CJXL_THREADS="$2"; shift 2 ;;
        --verify-deep)    VERIFY_DEEP=true; shift ;;
        -n|--dry-run)     DRY_RUN=true; shift ;;
        -h|--help)        usage; exit 0 ;;
        --)               shift; while (( $# > 0 )); do POSITIONAL+=("$1"); shift; done ;;
        -*)               echo "Unbekannte Option: $1" >&2; usage >&2; exit 2 ;;
        *)                POSITIONAL+=("$1"); shift ;;
    esac
done
[[ -n "${POSITIONAL[0]:-}" ]] && SOURCE_DIR="${POSITIONAL[0]}"
[[ -n "${POSITIONAL[1]:-}" ]] && OUTPUT_DIR="${POSITIONAL[1]}"

if [[ -t 0 && "${NON_INTERACTIVE:-false}" != "true" ]]; then
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    printf "%bBildkonverter: JPG/PNG -> JXL (mit WebP-Fallback)%b\n" "$C_BOLD" "$C_RESET"
    printf "%b══════════════════════════════════════════════════════════════%b\n" "$C_CYAN" "$C_RESET"
    read -rp "Mit Standardeinstellungen ausfuehren? [J/n]: " start_choice || start_choice=""
    if [[ "${start_choice,,}" =~ ^(n|nein|no)$ ]]; then
        read -rp "  Quellverzeichnis [$SOURCE_DIR]: " x && SOURCE_DIR="${x:-$SOURCE_DIR}"
        read -rp "  Zielverzeichnis (leer = In-Place) [$OUTPUT_DIR]: " x && OUTPUT_DIR="${x:-$OUTPUT_DIR}"
        read -rp "  Originale in den Papierkorb? [j/N]: " x
        [[ "${x,,}" =~ ^(j|ja|y|yes)$ ]] && DELETE_ORIGINAL=true || DELETE_ORIGINAL=false
        read -rp "  JXL Effort (1-9) [$JXL_EFFORT]: " x && JXL_EFFORT="${x:-$JXL_EFFORT}"
        read -rp "  PNG-Modus (lossless/lossy) [$PNG_MODE]: " x && PNG_MODE="${x:-$PNG_MODE}"
    fi
fi

[[ -z "$SOURCE_DIR" ]] && { echo "Kein Quellverzeichnis." >&2; exit 1; }
[[ -d "$SOURCE_DIR" ]] || { echo "Quellverzeichnis fehlt: $SOURCE_DIR" >&2; exit 1; }
SOURCE_DIR="${SOURCE_DIR%/}"
if [[ -n "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="${OUTPUT_DIR%/}"
    [[ "$DRY_RUN" == true ]] || mkdir -p "$OUTPUT_DIR"
fi

require_cmds cjxl cwebp file || exit 1
[[ "$PNG_MODE" == "lossless" || "$PNG_MODE" == "lossy" ]] || {
    echo "--png-mode muss 'lossless' oder 'lossy' sein." >&2; exit 2; }

[[ "$DRY_RUN" == true ]] && printf "%b[DRY-RUN]%b Es wird nichts geschrieben oder geloescht.\n" "$C_CYAN" "$C_RESET"

cleanup_stale_parts "${OUTPUT_DIR:-$SOURCE_DIR}" "*.part.*.jxl" "*.part.*.webp"

# ------------------------------------------------------------------------------
# STATISTIK
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
TOTAL_ORIG_BYTES=${TOTAL_ORIG_BYTES:-0}; TOTAL_NEW_BYTES=${TOTAL_NEW_BYTES:-0}
PREV_ELAPSED=${PREV_ELAPSED:-0}
START_TIME=$(date +%s)

finalize_stats() {
    local exit_code="$1"
    local tot_elapsed=$(( PREV_ELAPSED + $(date +%s) - START_TIME ))

    local c_proc=0 c_skip=0 c_fail=0 c_coll=0 c_orig=0 c_new=0 c_dry=0
    if [[ -f "$CURRENT_RUN_LOG" ]]; then
        c_proc=$(grep -c "^SUCCESS"   "$CURRENT_RUN_LOG" || true)
        c_skip=$(grep -c "^SKIP"      "$CURRENT_RUN_LOG" || true)
        c_fail=$(grep -c "^FAIL"      "$CURRENT_RUN_LOG" || true)
        c_coll=$(grep -c "^COLLISION" "$CURRENT_RUN_LOG" || true)
        c_dry=$(grep -c "^DRY"        "$CURRENT_RUN_LOG" || true)
        c_orig=$(awk '/^SUCCESS/ {s += $2} END {print s+0}' "$CURRENT_RUN_LOG")
        c_new=$(awk  '/^SUCCESS/ {s += $3} END {print s+0}' "$CURRENT_RUN_LOG")
        rm -f "$CURRENT_RUN_LOG"
    fi

    TOTAL_PROCESSED=$(( TOTAL_PROCESSED + c_proc ))
    TOTAL_SKIPPED=$(( TOTAL_SKIPPED + c_skip ))
    TOTAL_FAILED=$(( TOTAL_FAILED + c_fail ))
    TOTAL_COLLISION=$(( TOTAL_COLLISION + c_coll ))
    TOTAL_ORIG_BYTES=$(( TOTAL_ORIG_BYTES + c_orig ))
    TOTAL_NEW_BYTES=$(( TOTAL_NEW_BYTES + c_new ))

    if [[ $exit_code -eq 130 ]]; then
        cat > "$STATS_FILE" <<EOF
TOTAL_PROCESSED=$TOTAL_PROCESSED
TOTAL_SKIPPED=$TOTAL_SKIPPED
TOTAL_FAILED=$TOTAL_FAILED
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
        printf "\n%b[DRY-RUN]%b %s Datei(en) wuerden konvertiert, %s uebersprungen.\n" \
            "$C_CYAN" "$C_RESET" "$c_dry" "$TOTAL_SKIPPED"
        return 0
    fi

    local saved=$(( TOTAL_ORIG_BYTES - TOTAL_NEW_BYTES ))
    printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_BLUE" "$C_RESET"
    printf "%b║%b                  %bBILDER-AUSWERTUNG%b                           %b║%b\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BLUE" "$C_RESET"
    printf "%b╠══════════════════════════════════════════════════════════════╣%b\n" "$C_BLUE" "$C_RESET"
    printf "%b║%b  Gesamtlaufzeit:       %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$(format_duration "$tot_elapsed")" "$C_BLUE" "$C_RESET"
    printf "%b║%b  Konvertiert:          %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$TOTAL_PROCESSED Datei(en)" "$C_BLUE" "$C_RESET"
    printf "%b║%b  Uebersprungen:        %-38s %b║%b\n" "$C_BLUE" "$C_RESET" "$TOTAL_SKIPPED Datei(en)" "$C_BLUE" "$C_RESET"
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

trap 'printf "\n%b[ABBRUCH]%b Bild-Konvertierung gestoppt.\n" "$C_RED" "$C_RESET"; finalize_stats 130' SIGINT SIGTERM

# ------------------------------------------------------------------------------
# WORKER
# ------------------------------------------------------------------------------
# Aussenhuelle: berechnet Zielnamen, sichert sie per Lock-Verzeichnis gegen
# gleichzeitigen Zugriff durch einen zweiten Worker (z.B. foo.jpg + foo.png,
# die beide auf foo.jxl zeigen wuerden).
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

    # Eindeutige Temp-Namen: zwei Worker duerfen sich nie dieselbe .part-Datei teilen
    local uniq="$$.${RANDOM}"
    local temp_jxl="${dest_jxl}.part.${uniq}.jxl"
    local temp_webp="${dest_webp}.part.${uniq}.webp"

    # Zielnamen exklusiv beanspruchen
    local lockdir="$target_folder/.${stem}.molock"
    if ! mkdir "$lockdir" 2>/dev/null; then
        echo "COLLISION" >> "$CURRENT_RUN_LOG"
        printf "%b[KONFLIKT]%b '%s': Zielname '%s' wird bereits belegt, Original bleibt.\n" \
            "$C_YELLOW" "$C_RESET" "$file" "$(basename "$dest_jxl")" >&2
        return 0
    fi

    local rc=0
    _convert_image_locked || rc=$?
    rmdir "$lockdir" 2>/dev/null || true
    return $rc
}

# Innere Funktion: nutzt die lokalen Variablen der Huelle (dynamic scoping).
_convert_image_locked() {
    local new_size

    _commit() {   # _commit <tempdatei> <zieldatei> <label>
        local tmp="$1" dest="$2" label="$3"
        if ! verify_output_image "$tmp"; then
            rm -f "$tmp"
            echo "FAIL" >> "$CURRENT_RUN_LOG"
            printf "%b[FEHLER]%b Ausgabe nicht verifizierbar: '%s' (Original bleibt)\n" \
                "$C_RED" "$C_RESET" "$file" >&2
            return 1
        fi
        mv "$tmp" "$dest"
        touch -r "$src" "$dest"
        new_size=$(stat -c%s "$dest" 2>/dev/null || echo 0)
        echo "SUCCESS $orig_size $new_size" >> "$CURRENT_RUN_LOG"
        [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$src"
        printf "%b%s%b '%s' -> '%s'\n" "$C_GREEN" "$label" "$C_RESET" "$file" "$(basename "$dest")"
        return 0
    }

    # ---------------- JPEG: verlustfreies JXL-Recompress ----------------
    if [[ "$ext" == "jpg" || "$ext" == "jpeg" ]]; then
        if cjxl "$src" "$temp_jxl" -e "$JXL_EFFORT" --num_threads="$CJXL_THREADS" --quiet 2>/dev/null && [[ -s "$temp_jxl" ]]; then
            _commit "$temp_jxl" "$dest_jxl" "[JXL-LOSSLESS]" && return 0
            return 1
        fi
        rm -f "$temp_jxl"

        # Falsche Endung? MIME pruefen und korrigieren.
        local real_mime; real_mime=$(file -b --mime-type "$src" 2>/dev/null || echo unknown)
        if [[ "$real_mime" != "image/jpeg" ]]; then
            local correct_ext=""
            case "$real_mime" in
                image/webp) correct_ext="webp" ;; image/png) correct_ext="png" ;;
                image/jxl)  correct_ext="jxl"  ;; image/gif) correct_ext="gif" ;;
            esac
            if [[ -n "$correct_ext" ]]; then
                local cand="$dir/${stem}.${correct_ext}" n=1
                while [[ -e "$cand" ]]; do cand="$dir/${stem}_${n}.${correct_ext}"; n=$(( n + 1 )); done
                mv "$src" "$cand"
                printf "%b[KORREKTUR]%b '%s' (%s) -> '%s'\n" \
                    "$C_MAGENTA" "$C_RESET" "$file" "$real_mime" "$(basename "$cand")"
                if [[ "$correct_ext" == "webp" || "$correct_ext" == "jxl" ]]; then
                    if [[ -n "${OUTPUT_DIR:-}" ]]; then
                        cp -p "$cand" "$target_folder/"
                        [[ "$DELETE_ORIGINAL" == true ]] && safe_remove "$cand"
                    fi
                    echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
                fi
                if [[ "$correct_ext" == "png" ]]; then
                    src="$cand"; file="$(basename "$src")"; ext="png"
                fi
            fi
        fi
        if [[ "$ext" != "png" ]]; then
            printf "%b[FEHLER]%b JXL-Transcoding fehlgeschlagen: '%s'\n" "$C_RED" "$C_RESET" "$file" >&2
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
                local cand="$dir/${stem}.${correct_ext}" n=1
                while [[ -e "$cand" ]]; do cand="$dir/${stem}_${n}.${correct_ext}"; n=$(( n + 1 )); done
                mv "$src" "$cand"
                printf "%b[KORREKTUR]%b '%s' (%s) -> '%s'\n" \
                    "$C_MAGENTA" "$C_RESET" "$file" "$real_mime" "$(basename "$cand")"
                if [[ "$correct_ext" == "jpg" ]]; then
                    if cjxl "$cand" "$temp_jxl" -e "$JXL_EFFORT" --num_threads="$CJXL_THREADS" --quiet 2>/dev/null && [[ -s "$temp_jxl" ]]; then
                        src="$cand"; file="$(basename "$cand")"
                        _commit "$temp_jxl" "$dest_jxl" "[JXL-LOSSLESS]" && return 0
                        return 1
                    fi
                    rm -f "$temp_jxl"
                fi
                echo "SKIP" >> "$CURRENT_RUN_LOG"; return 0
            fi
        fi

        # Fallback: verlustfreies WebP inkl. Metadaten
        printf "%b[FALLBACK]%b '%s' schlug fehl -> versuche WebP...\n" "$C_YELLOW" "$C_RESET" "$file"
        if cwebp -quiet -lossless -z 9 -m "$WEBP_FALLBACK_METHOD" -metadata all \
                 "$src" -o "$temp_webp" 2>/dev/null && [[ -s "$temp_webp" ]]; then
            _commit "$temp_webp" "$dest_webp" "[PNG->WEBP]" && return 0
            return 1
        fi
        rm -f "$temp_webp"
        printf "%b[FEHLER]%b Konvertierung fehlgeschlagen: '%s'\n" "$C_RED" "$C_RESET" "$file" >&2
        echo "FAIL" >> "$CURRENT_RUN_LOG"; return 1
    fi

    echo "FAIL" >> "$CURRENT_RUN_LOG"; return 1
}

export SOURCE_DIR OUTPUT_DIR SKIP_EXISTING DELETE_ORIGINAL
export JXL_EFFORT PNG_MODE PNG_QUALITY WEBP_FALLBACK_METHOD CJXL_THREADS
export -f convert_image _convert_image_locked

exit_code=0
find "$SOURCE_DIR" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" \) -print0 |
    xargs -0 -r -n 1 -P "$MAX_WORKERS" bash -c 'convert_image "$1"' _ || exit_code=$?

if (( exit_code >= 124 )); then finalize_stats 130; else finalize_stats 0; fi
