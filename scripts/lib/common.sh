#!/bin/bash
# ==============================================================================
# lib/common.sh
# Gemeinsame Hilfsfunktionen fuer media-optimizer.sh und die Worker-Skripte.
# Wird gesourct, nicht direkt ausgefuehrt.
# ==============================================================================

[[ -n "${_MO_COMMON_LOADED:-}" ]] && return 0
_MO_COMMON_LOADED=1

# ------------------------------------------------------------------------------
# Farben (werden deaktiviert, wenn stdout kein Terminal ist oder NO_COLOR gesetzt)
# ------------------------------------------------------------------------------
# TTY-Zustand festhalten, solange noch nichts umgeleitet wurde
_MO_TTY_IN=false;  [[ -t 0 ]] && _MO_TTY_IN=true
_MO_TTY_OUT=false; [[ -t 1 ]] && _MO_TTY_OUT=true
export _MO_TTY_IN _MO_TTY_OUT

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RESET='\033[0m';    C_BOLD='\033[1m';     C_GREEN='\033[1;32m'
    C_RED='\033[1;31m';   C_YELLOW='\033[1;33m'; C_CYAN='\033[1;36m'
    C_BLUE='\033[1;34m';  C_MAGENTA='\033[1;35m'; C_BG_GREEN='\033[42;30m'
else
    C_RESET=''; C_BOLD=''; C_GREEN=''; C_RED=''; C_YELLOW=''
    C_CYAN=''; C_BLUE=''; C_MAGENTA=''; C_BG_GREEN=''
fi
export C_RESET C_BOLD C_GREEN C_RED C_YELLOW C_CYAN C_BLUE C_MAGENTA C_BG_GREEN

# ------------------------------------------------------------------------------
# Formatierung
# ------------------------------------------------------------------------------
format_bytes() {
    local b="${1:-0}"
    awk -v b="$b" 'BEGIN {
        split("B KB MB GB TB", u); i = 1
        while (b >= 1024 && i < 5) { b /= 1024; i++ }
        printf "%.2f %s", b, u[i]
    }'
}

format_duration() {
    local seconds="${1:-0}"
    local h=$(( seconds / 3600 )) m=$(( (seconds % 3600) / 60 )) s=$(( seconds % 60 ))
    if   (( h > 0 )); then printf "%dh %02dm %02ds" "$h" "$m" "$s"
    elif (( m > 0 )); then printf "%dm %02ds" "$m" "$s"
    else printf "%ds" "$s"; fi
}

# pct_change <alt> <neu> -> prozentuale Aenderung, division-by-zero-sicher
pct_change() {
    awk -v a="${1:-0}" -v b="${2:-0}" 'BEGIN {
        if (a <= 0) { printf "0.0"; exit }
        printf "%.1f", (b / a - 1) * 100
    }'
}

export -f format_bytes format_duration pct_change

# ------------------------------------------------------------------------------
# Konfigurationsdatei
# Reihenfolge: CLI-Flag > Konfigdatei > eingebauter Default.
# Der Master laedt die Datei einmal und exportiert die Werte an die Worker;
# _MO_CONFIG_APPLIED verhindert doppeltes Laden.
# ------------------------------------------------------------------------------
# Variablen, die eine Konfigdatei setzen darf.
MO_CONFIG_VARS=(
    MAX_WORKERS DELETE_ORIGINAL FORCE_DELETE VERIFY_DEEP
    JXL_EFFORT PNG_MODE PNG_QUALITY CJXL_THREADS
    COMPRESSION_METHOD GIF_KMIN
    ENCODER_MODE BITRATE_THRESHOLD_KBPS GPU_QP CPU_CRF CPU_PRESET
    CPU_X265_PARAMS VAAPI_DEVICE ENABLE_PROBE PROBE_MARGIN_PCT
    DISCARD_IF_LARGER KEEP_SALVAGED_CORRUPT MIN_SIZE_MB FASTSTART USE_CACHE
    DURATION_TOLERANCE_PCT PROBE_DURATION RENAME_INPLACE
)

load_config() {
    local candidates=(
        "${MO_CONFIG:-}"
        "${_MO_ROOT:-}/media-optimizer.conf"
        "${XDG_CONFIG_HOME:-$HOME/.config}/media-optimizer.conf"
    )
    local file="" c
    for c in "${candidates[@]}"; do
        if [[ -n "$c" && -f "$c" ]]; then file="$c"; break; fi
    done
    [[ -n "$file" ]] || return 0

    # Bereits gesetzte Werte (Umgebung, oder vom Master exportiert) merken,
    # damit sie Vorrang vor der Datei behalten: Env/CLI > Konfig > Default.
    local v saved=()
    for v in "${MO_CONFIG_VARS[@]}"; do
        [[ -n "${!v+x}" ]] && saved+=("$v=${!v}")
    done

    # shellcheck disable=SC1090
    source "$file"

    local kv
    for kv in "${saved[@]}"; do
        printf -v "${kv%%=*}" '%s' "${kv#*=}"
    done

    MO_CONFIG="$file"
    export MO_CONFIG
    return 0
}

# ------------------------------------------------------------------------------
# Abhaengigkeiten
# ------------------------------------------------------------------------------
require_cmds() {
    local missing=() c
    for c in "$@"; do command -v "$c" >/dev/null 2>&1 || missing+=("$c"); done
    if (( ${#missing[@]} > 0 )); then
        printf "%b[FEHLT]%b Benoetigte Programme nicht gefunden: %s\n" \
            "$C_RED" "$C_RESET" "${missing[*]}" >&2
        return 1
    fi
    return 0
}

# ------------------------------------------------------------------------------
# Loeschen: Papierkorb, sonst vormerken statt hart loeschen
#
# Schlaegt der Papierkorb fehl (z.B. Datei auf anderem Mount, kein gio/trash-cli),
# wird die Datei NICHT geloescht, sondern in $PENDING_DELETE_LOG vorgemerkt.
# resolve_pending_deletes() fragt am Ende des Laufs einmal nach.
# ------------------------------------------------------------------------------
safe_remove() {
    local target="$1"
    [[ -e "$target" ]] || return 0

    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        printf "%b[DRY-RUN]%b Original bliebe erhalten: %s\n" \
            "$C_CYAN" "$C_RESET" "$(basename "$target")"
        return 0
    fi

    if command -v gio >/dev/null 2>&1 && gio trash "$target" 2>/dev/null; then
        return 0
    fi
    if command -v trash-put >/dev/null 2>&1 && trash-put -- "$target" 2>/dev/null; then
        return 0
    fi

    if [[ -n "${PENDING_DELETE_LOG:-}" ]]; then
        printf '%s\n' "$target" >> "$PENDING_DELETE_LOG" 2>/dev/null || true
    fi
    printf "%b[PAPIERKORB]%b fehlgeschlagen, Original bleibt vorerst: %s\n" \
        "$C_YELLOW" "$C_RESET" "$(basename "$target")"
    return 0
}
export -f safe_remove

# Am Ende eines Laufs: einmalige Rueckfrage fuer alles, was nicht in den
# Papierkorb konnte.
resolve_pending_deletes() {
    local log="${PENDING_DELETE_LOG:-}"
    [[ -n "$log" && -f "$log" ]] || return 0
    if [[ ! -s "$log" ]]; then rm -f "$log"; return 0; fi

    local -a files=(); local f
    while IFS= read -r f; do
        [[ -n "$f" && -e "$f" ]] && files+=("$f")
    done < "$log"

    if (( ${#files[@]} == 0 )); then rm -f "$log"; return 0; fi

    echo ""
    printf "%b┌──────────────────────────────────────────────────────────────┐%b\n" "$C_YELLOW" "$C_RESET"
    printf "%b│%b  %bPAPIERKORB NICHT VERFUEGBAR%b\n" "$C_YELLOW" "$C_RESET" "$C_BOLD" "$C_RESET"
    printf "%b│%b  %d Original(e) konnten nicht in den Papierkorb verschoben\n" \
        "$C_YELLOW" "$C_RESET" "${#files[@]}"
    printf "%b│%b  werden. Sie wurden bewusst NICHT geloescht.\n" "$C_YELLOW" "$C_RESET"
    printf "%b└──────────────────────────────────────────────────────────────┘%b\n" "$C_YELLOW" "$C_RESET"
    for f in "${files[@]:0:15}"; do echo "    $f"; done
    (( ${#files[@]} > 15 )) && echo "    ... und $(( ${#files[@]} - 15 )) weitere (siehe $log)"

    if [[ "${FORCE_DELETE:-false}" == "true" ]]; then
        echo "  --force-delete gesetzt: loesche endgueltig."
    elif [[ ! -t 0 ]]; then
        printf "  %bKeine Rueckfrage moeglich (nicht interaktiv).%b Originale bleiben erhalten.\n" \
            "$C_CYAN" "$C_RESET"
        echo "  Liste: $log"
        return 0
    else
        local ans
        read -rp "  Diese Datei(en) jetzt endgueltig loeschen (rm, kein Papierkorb)? [j/N]: " ans
        if [[ ! "${ans,,}" =~ ^(j|ja|y|yes)$ ]]; then
            echo "  Originale bleiben erhalten. Liste: $log"
            return 0
        fi
    fi

    local ok=0 err=0
    for f in "${files[@]}"; do
        if rm -f -- "$f" 2>/dev/null; then ok=$(( ok + 1 )); else err=$(( err + 1 )); fi
    done
    printf "  %b%d geloescht%b" "$C_GREEN" "$ok" "$C_RESET"
    (( err > 0 )) && printf ", %b%d fehlgeschlagen%b" "$C_RED" "$err" "$C_RESET"
    echo ""
    (( err == 0 )) && rm -f "$log"
    return 0
}

# ------------------------------------------------------------------------------
# Reste aus abgebrochenen Laeufen (SIGKILL, Stromausfall) aufraeumen
# cleanup_stale_parts <verzeichnis> <glob> [<glob> ...]
# ------------------------------------------------------------------------------
cleanup_stale_parts() {
    local dir="$1"; shift
    [[ -d "$dir" ]] || return 0
    local pattern f n=0
    for pattern in "$@"; do
        while IFS= read -r -d '' f; do
            rm -f -- "$f" 2>/dev/null && n=$(( n + 1 ))
        done < <(find "$dir" -type f -name "$pattern" -print0 2>/dev/null)
    done
    # verwaiste Lock-Verzeichnisse
    while IFS= read -r -d '' f; do
        rmdir -- "$f" 2>/dev/null && n=$(( n + 1 ))
    done < <(find "$dir" -type d -name "*.molock" -print0 2>/dev/null)

    (( n > 0 )) && printf "%b[AUFRAEUMEN]%b %d Rest(e) aus einem abgebrochenen Lauf entfernt.\n" \
        "$C_CYAN" "$C_RESET" "$n"
    return 0
}

# ------------------------------------------------------------------------------
# Ausgabe-Verifikation
# ------------------------------------------------------------------------------
# verify_output_image <datei>
# Leicht: Datei nicht leer. Mit VERIFY_DEEP=true zusaetzlich vollstaendiger Decode.
verify_output_image() {
    local f="$1"
    [[ -s "$f" ]] || return 1
    [[ "${VERIFY_DEEP:-false}" == "true" ]] || return 0

    case "${f##*.}" in
        jxl)
            command -v djxl >/dev/null 2>&1 || return 0
            local t; t=$(mktemp --suffix=.ppm /tmp/mo_verify_XXXXXX) || return 0
            if djxl "$f" "$t" >/dev/null 2>&1; then rm -f "$t"; return 0
            else rm -f "$t"; return 1; fi
            ;;
        webp)
            command -v webpinfo >/dev/null 2>&1 || return 0
            webpinfo -quiet "$f" >/dev/null 2>&1 || return 1
            ;;
    esac
    return 0
}
export -f verify_output_image

# media_duration <datei> -> Dauer in Sekunden (float) oder leer
media_duration() {
    ffprobe -v error -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "$1" 2>/dev/null || true
}

# verify_video <quelle> <ziel> [toleranz_prozent]
# Prueft, ob das Ziel decodierbar ist und die Dauer zur Quelle passt.
verify_video() {
    local src="$1" dst="$2" tol="${3:-2}"
    [[ -s "$dst" ]] || return 1

    local sd dd
    sd=$(media_duration "$src"); dd=$(media_duration "$dst")

    # Ziel muss eine plausible Dauer melden, sonst ist es kaputt
    [[ -z "$dd" || "$dd" == "N/A" ]] && return 1
    # Quelle unlesbar -> nur Decodierbarkeit des Ziels zaehlt
    [[ -z "$sd" || "$sd" == "N/A" ]] && return 0

    awk -v a="$sd" -v b="$dd" -v t="$tol" 'BEGIN {
        if (a <= 0) exit 0
        d = (b > a) ? b - a : a - b
        exit ((d / a * 100) > t) ? 1 : 0
    }'
}

# ------------------------------------------------------------------------------
# Fenster offen halten und Fehlerstelle melden
#
# Wird ein Skript per Doppelklick oder .desktop-Datei gestartet, schliesst der
# Terminal-Emulator das Fenster, sobald das Skript endet. Bei einem Abbruch
# durch "set -e" ist die Fehlermeldung dann nicht mehr lesbar.
#
# MO_HOLD=1     immer offen halten
# MO_HOLD=0     nie offen halten
# MO_HOLD=auto  (Default) offen halten, wenn das Elternprozess keine Shell ist
# MO_HOLD_TIMEOUT=<s>  nach s Sekunden von selbst schliessen (0 = warten)
# ------------------------------------------------------------------------------
_MO_ERR_INFO=""

_mo_parent_comm() {
    local c=""
    [[ -r "/proc/${PPID}/comm" ]] && read -r c < "/proc/${PPID}/comm" 2>/dev/null
    printf '%s' "$c"
}

# Ist der Elternprozess eine interaktive Shell? Dann wurde das Skript von Hand
# im Terminal gestartet und das Fenster bleibt ohnehin stehen.
# Entscheidend ist die Kommandozeile, nicht comm: bei einem Skript steht in
# comm der Skriptname. Eine interaktive Shell hat kein Skript- und kein
# -c-Argument, ein Terminal-Emulator oder "sh -c ..." dagegen schon.
_mo_parent_is_interactive_shell() {
    local f="/proc/${PPID}/cmdline"
    [[ -r "$f" ]] || return 1
    local -a argv=()
    mapfile -d '' -t argv < "$f" 2>/dev/null || return 1
    (( ${#argv[@]} > 0 )) || return 1

    local a0="${argv[0]#-}"     # Login-Shell erscheint als "-bash"
    a0="${a0##*/}"
    case "$a0" in
        bash|sh|zsh|dash|fish|ksh|ksh93|tcsh|csh|elvish|nu) ;;
        *) return 1 ;;
    esac

    local a
    for a in "${argv[@]:1}"; do
        [[ -z "$a" ]] && continue
        case "$a" in
            -i|-l|--login|-il|-li|--interactive) continue ;;
            *) return 1 ;;
        esac
    done
    return 0
}

mo_should_hold() {
    case "${MO_HOLD:-auto}" in
        1|true|yes)  return 0 ;;
        0|false|no)  return 1 ;;
    esac
    # Ohne Terminal wuerde niemand die Meldung sehen
    [[ "$_MO_TTY_IN" == true && "$_MO_TTY_OUT" == true ]] || return 1
    _mo_parent_is_interactive_shell && return 1
    return 0
}

mo_hold_open() {
    local rc="${1:-0}"
    if (( rc != 0 )); then
        printf "\n%b[ABBRUCH]%b Skript endete mit Code %d.\n" "$C_RED" "$C_RESET" "$rc" >&2
        [[ -n "$_MO_ERR_INFO" ]] && printf "          %s\n" "$_MO_ERR_INFO" >&2
    fi
    mo_should_hold || return 0
    printf "\n"
    if (( rc == 0 )); then
        printf "%b[FERTIG]%b Ohne Fehler beendet.\n" "$C_GREEN" "$C_RESET"
    fi
    local timeout="${MO_HOLD_TIMEOUT:-0}"
    if (( timeout > 0 )); then
        printf "Fenster schliesst in %ds, oder Enter druecken. " "$timeout"
        read -r -t "$timeout" _ < /dev/tty 2>/dev/null || true
    else
        printf "Fenster bleibt offen. Enter zum Schliessen. "
        read -r _ < /dev/tty 2>/dev/null || true
    fi
    printf "\n"
    return 0
}

# Merkt sich Zeile und Befehl des ersten Fehlers. Braucht "set -E",
# damit der Trap auch innerhalb von Funktionen greift.
mo_install_exit_handler() {
    set -E
    trap '_mo_rc=$?; [[ -z "$_MO_ERR_INFO" ]] && _MO_ERR_INFO="Zeile $LINENO: \`$BASH_COMMAND\` (Code $_mo_rc)"' ERR
    trap 'mo_hold_open $?' EXIT
}
