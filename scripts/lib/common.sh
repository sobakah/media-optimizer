#!/bin/bash
# ==============================================================================
# lib/common.sh  -  shared helpers
#
# Sourced by every script, never executed directly.
#
# CONTENTS
#   colours          C_* variables, empty when stdout is not a terminal
#   formatting       format_bytes, format_duration, pct_change
#   configuration    load_config and MO_CONFIG_VARS
#   dependencies     require_cmds
#   deletion         safe_remove, resolve_pending_deletes
#   cleanup          cleanup_stale_parts
#   verification     verify_output_image, verify_video, verify_visual
#   window handling  mo_install_exit_handler, mo_hold_open
#   mode presets     mo_apply_inplace_defaults
#   logging          mo_log_init, mo_log, mo_log_file, mo_log_close
#
# WHEN EXTENDING THIS FILE
#   - New configuration variables must be listed in MO_CONFIG_VARS, otherwise
#     the config file cannot set them.
#   - Functions used inside parallel xargs workers need "export -f". Without
#     it the call is simply "command not found" there, which in some places is
#     counted as a failed check rather than an error.
#   - Do not preset log variables at source time: common.sh is sourced before
#     load_config, and a value set here would look like an explicit setting.
# ==============================================================================

[[ -n "${_MO_COMMON_LOADED:-}" ]] && return 0
_MO_COMMON_LOADED=1

# ------------------------------------------------------------------------------
# Colours (disabled when stdout is not a terminal or NO_COLOR is set)
# ------------------------------------------------------------------------------
# Capture the TTY state while nothing has been redirected yet
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
# The master loads the file once and exports the values to the workers;
# _MO_CONFIG_APPLIED verhindert doppeltes Laden.
# ------------------------------------------------------------------------------
# Variables a config file is allowed to set.
MO_CONFIG_VARS=(
    MAX_WORKERS DELETE_ORIGINAL FORCE_DELETE VERIFY_DEEP JXL_EFFORT
    IMG_TARGET IMG_WEBP_QUALITY IMG_DISCARD_IF_LARGER PNG_MODE PNG_QUALITY CJXL_THREADS COMPRESSION_METHOD GIF_KMIN
    ENCODER_MODE BITRATE_THRESHOLD_KBPS GPU_QP CPU_CRF CPU_PRESET
    CPU_X265_PARAMS VAAPI_DEVICE ENABLE_PROBE PROBE_MARGIN_PCT
    DISCARD_IF_LARGER KEEP_SALVAGED_CORRUPT VIDEO_EXTENSIONS VIDEO_CONTAINER SOURCE_CODECS AVIF_SOURCE_CODECS MIN_SIZE_MB FASTSTART USE_CACHE
    GIF_TARGET GIF_AVIF_CRF AVIF_MAX_SECONDS AVIF_CRF AVIF_CRF_RETRY
    AVIF_PRESET AVIF_PIX_FMT AVIF_REQUIRE_SILENT ENABLE_AVIF_STAGE
    DELETE_ORIGINAL_EXPLICIT RENAME_INPLACE_EXPLICIT MO_LOG MO_LOG_FILE
    MO_LOG_MAX_KB DURATION_TOLERANCE_PCT PROBE_DURATION RENAME_INPLACE
    GPU_ALLOW_10BIT VERIFY_VISUAL VISUAL_PSNR_MIN VISUAL_SAMPLES
    VISUAL_STRICT CHECK_VISUAL ENABLE_VERIFY_OUTPUT AUTO_FIX_LIST HEVC_TAG GPU_CODEC VULKAN_DEVICE
    GPU_RC_MODE GPU_BF GPU_LOW_POWER GPU_ASYNC_DEPTH GPU_BITRATE
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

    # Remember values already set (environment, or exported by the master)
    # so they keep precedence over the file: env/CLI > config > default.
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
        printf "%b[MISSING]%b Required programs not found: %s\n" \
            "$C_RED" "$C_RESET" "${missing[*]}" >&2
        return 1
    fi
    return 0
}

# ------------------------------------------------------------------------------
# Deletion: trash, otherwise queue instead of deleting outright
#
# If the trash fails (file on another mount, no gio/trash-cli),
# the file is NOT deleted but queued in $PENDING_DELETE_LOG.
# resolve_pending_deletes() asks once at the end of the run.
# ------------------------------------------------------------------------------
safe_remove() {
    local target="$1"
    [[ -e "$target" ]] || return 0

    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        printf "%b[DRY-RUN]%b original would be kept: %s\n" \
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
    printf "%b[TRASH]%b failed, original kept for now: %s\n" \
        "$C_YELLOW" "$C_RESET" "$(basename "$target")"
    return 0
}
export -f safe_remove

# At the end of a run: one prompt for everything that could not go to
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
    printf "%b│%b  %bTRASH NOT AVAILABLE%b\n" "$C_YELLOW" "$C_RESET" "$C_BOLD" "$C_RESET"
    printf "%b│%b  %d original(s) could not be moved to the trash.\n" \
        "$C_YELLOW" "$C_RESET" "${#files[@]}"
    printf "%b│%b  They were deliberately NOT deleted.\n" "$C_YELLOW" "$C_RESET"
    printf "%b└──────────────────────────────────────────────────────────────┘%b\n" "$C_YELLOW" "$C_RESET"
    for f in "${files[@]:0:15}"; do echo "    $f"; done
    (( ${#files[@]} > 15 )) && echo "    ... and $(( ${#files[@]} - 15 )) more (see $log)"

    if [[ "${FORCE_DELETE:-false}" == "true" ]]; then
        echo "  --force-delete given: deleting permanently."
    elif [[ ! -t 0 ]]; then
        printf "  %bCannot prompt (not interactive).%b Originals are kept.\n" \
            "$C_CYAN" "$C_RESET"
        echo "  Liste: $log"
        return 0
    else
        local ans
        read -rp "  Delete these file(s) permanently now (rm, no trash)? [y/N]: " ans
        if [[ ! "${ans,,}" =~ ^(j|ja|y|yes)$ ]]; then
            echo "  Originals are kept. List: $log"
            return 0
        fi
    fi

    local ok=0 err=0
    for f in "${files[@]}"; do
        if rm -f -- "$f" 2>/dev/null; then ok=$(( ok + 1 )); else err=$(( err + 1 )); fi
    done
    printf "  %b%d deleted%b" "$C_GREEN" "$ok" "$C_RESET"
    (( err > 0 )) && printf ", %b%d failed%b" "$C_RED" "$err" "$C_RESET"
    echo ""
    (( err == 0 )) && rm -f "$log"
    return 0
}

# ------------------------------------------------------------------------------
# Clean up leftovers from aborted runs (SIGKILL, power loss)
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

    (( n > 0 )) && printf "%b[CLEANUP]%b %d leftover(s) from an aborted run removed.\n" \
        "$C_CYAN" "$C_RESET" "$n"
    return 0
}

# ------------------------------------------------------------------------------
# Ausgabe-Verifikation
# ------------------------------------------------------------------------------
# verify_output_image <datei>
# Light: file not empty. With VERIFY_DEEP=true also a full decode.
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

# These functions also run in xargs workers, so export them.
# Without the export the call is simply "command not found" there and
# would wrongly count as a failed check.
# media_duration <file> -> duration in seconds (float) or empty
media_duration() {
    ffprobe -v error -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "$1" 2>/dev/null || true
}

# verify_video <quelle> <ziel> [toleranz_prozent]
# Checks that the target decodes and its duration matches the source.
verify_video() {
    local src="$1" dst="$2" tol="${3:-2}"
    [[ -s "$dst" ]] || return 1

    local sd dd
    sd=$(media_duration "$src"); dd=$(media_duration "$dst")

    # target must report a plausible duration, otherwise it is broken
    [[ -z "$dd" || "$dd" == "N/A" ]] && return 1
    # source unreadable -> only the target's decodability counts
    [[ -z "$sd" || "$sd" == "N/A" ]] && return 0

    awk -v a="$sd" -v b="$dd" -v t="$tol" 'BEGIN {
        if (a <= 0) exit 0
        d = (b > a) ? b - a : a - b
        exit ((d / a * 100) > t) ? 1 : 0
    }'
}

# ------------------------------------------------------------------------------
# Keep the window open and report where the error occurred
#
# When a script is started by double-click or a .desktop file, the
# terminal emulator closes the window as soon as the script ends. After an
# abort through "set -e" the error message would no longer be readable.
#
# MO_HOLD=1     immer offen halten
# MO_HOLD=0     nie offen halten
# MO_HOLD=auto  (default) keep open when the parent is not a shell
# MO_HOLD_TIMEOUT=<s>  close by itself after s seconds (0 = wait)
# ------------------------------------------------------------------------------
_MO_ERR_INFO=""

_mo_parent_comm() {
    local c=""
    [[ -r "/proc/${PPID}/comm" ]] && read -r c < "/proc/${PPID}/comm" 2>/dev/null
    printf '%s' "$c"
}

# Is the parent an interactive shell? Then the script was started by hand
# in a terminal and the window stays open anyway.
# The command line decides, not comm: for a script, comm holds the
# script name. An interactive shell has neither a script nor a
# -c argument, a terminal emulator or "sh -c ..." does.
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
    # Without a terminal nobody would see the message
    [[ "$_MO_TTY_IN" == true && "$_MO_TTY_OUT" == true ]] || return 1
    _mo_parent_is_interactive_shell && return 1
    return 0
}

mo_hold_open() {
    local rc="${1:-0}"
    if (( rc != 0 )); then
        printf "\n%b[ABORT]%b Script exited with code %d.\n" "$C_RED" "$C_RESET" "$rc" >&2
        [[ -n "$_MO_ERR_INFO" ]] && printf "          %s\n" "$_MO_ERR_INFO" >&2
    fi
    mo_should_hold || return 0
    printf "\n"
    if (( rc == 0 )); then
        printf "%b[DONE]%b Finished without errors.\n" "$C_GREEN" "$C_RESET"
    fi
    local timeout="${MO_HOLD_TIMEOUT:-0}"
    if (( timeout > 0 )); then
        printf "Window closes in %ds, or press Enter. " "$timeout"
        read -r -t "$timeout" _ < /dev/tty 2>/dev/null || true
    else
        printf "Window stays open. Press Enter to close. "
        read -r _ < /dev/tty 2>/dev/null || true
    fi
    printf "\n"
    return 0
}

# Remembers line and command of the first error. Needs "set -E" so
# that the trap also fires inside functions.
mo_install_exit_handler() {
    set -E
    trap '_mo_rc=$?; [[ -z "$_MO_ERR_INFO" ]] && _MO_ERR_INFO="Zeile $LINENO: \`$BASH_COMMAND\` (Code $_mo_rc)"' ERR
    trap 'mo_hold_open $?' EXIT
}

# ------------------------------------------------------------------------------
# Bildinhalt gegenpruefen
#
# The runtime check catches truncated files but no content
# damage: a green picture with artefacts has the correct duration. So
# stills from source and target are compared at three positions.
# A real re-encode lands at 35-45 dB PSNR, broken colour formats below.
#
# verify_visual <quelle> <ziel> [min_psnr]
# 0 = fine or not measurable, 1 = broken (VISUAL_PSNR set)
# ------------------------------------------------------------------------------
VISUAL_PSNR=""        # bester Messwert
VISUAL_PSNR_ALL=""    # alle Stichproben, zum Nachvollziehen
verify_visual() {
    local src="$1" dst="$2" minp="${3:-20}"
    VISUAL_PSNR=""; VISUAL_PSNR_ALL=""
    local dur; dur=$(media_duration "$src")
    [[ -n "$dur" && "$dur" != "N/A" ]] || return 0

    local samples="${VISUAL_SAMPLES:-3}"
    local keep="${VISUAL_KEEP_DIR:-}"
    [[ -n "$keep" ]] && mkdir -p "$keep" 2>/dev/null

    local i frac t a b p best="" measured=0
    for (( i = 1; i <= samples; i++ )); do
        frac=$(( 100 * i / (samples + 1) ))
        t=$(awk -v d="$dur" -v f="$frac" 'BEGIN { printf "%.2f", d * f / 100 }')
        a=$(mktemp --suffix=.png /tmp/mo_vv_a_XXXXXX) || return 0
        b=$(mktemp --suffix=.png /tmp/mo_vv_b_XXXXXX) || { rm -f "$a"; return 0; }

        # -map 0:v:0 pins the real video stream (not an embedded
        # Vorschaubild), -noautorotate haelt beide Seiten gleich orientiert.
        if ffmpeg -nostdin -y -v error -noautorotate -ss "$t" -i "$src" \
                  -map 0:v:0 -frames:v 1 "$a" </dev/null 2>/dev/null \
        && ffmpeg -nostdin -y -v error -noautorotate -ss "$t" -i "$dst" \
                  -map 0:v:0 -frames:v 1 "$b" </dev/null 2>/dev/null; then
            p=$(ffmpeg -nostdin -hide_banner -i "$b" -i "$a" \
                -lavfi "scale2ref=flags=bilinear[x][y];[x][y]psnr" -f null - 2>&1 \
                | grep -oE 'average:[0-9]+\.[0-9]+' | head -1 | cut -d: -f2)
            if [[ "$p" =~ ^[0-9]+\.[0-9]+$ ]]; then
                measured=1
                VISUAL_PSNR_ALL="${VISUAL_PSNR_ALL:+$VISUAL_PSNR_ALL, }${t}s=${p%.*} dB"
                if [[ -z "$best" ]] || awk -v n="$p" -v o="$best" 'BEGIN { exit (n > o) ? 0 : 1 }'; then
                    best="$p"
                fi
            fi
        fi
        if [[ -n "$keep" ]]; then
            mv "$a" "$keep/$(basename "$dst" .mp4)_${t}s_quelle.png" 2>/dev/null || rm -f "$a"
            mv "$b" "$keep/$(basename "$dst" .mp4)_${t}s_ziel.png"   2>/dev/null || rm -f "$b"
        else
            rm -f "$a" "$b"
        fi
    done

    (( measured == 1 )) || return 0
    VISUAL_PSNR="${best%.*}"

    # The BEST sample decides. A single bad measurement can come from a
    # seek offset; a genuinely broken
    # picture is bad at EVERY position. This avoids false alarms.
    awk -v p="$best" -v m="$minp" 'BEGIN { exit (p < m) ? 0 : 1 }' && return 1
    return 0
}

export -f media_duration verify_video verify_visual

# ------------------------------------------------------------------------------
# Mode-dependent presets for DELETE_ORIGINAL and RENAME_INPLACE
#
# In place (no target directory): both true. Without deletion the original
# sits next to the output, without renaming the _h265 suffix stays. In this
# mode that is rarely wanted. Because it is destructive, a notice is
# shown and the setting can be changed on the spot.
#
# With a target directory: both false, no message. The original is not
# touched there anyway.
#
# An explicit setting via CLI, environment or config file always takes
# precedence; nothing is overwritten then.
# ------------------------------------------------------------------------------
mo_prompt_bool() {
    local question="$1" default="$2" result_var="$3" input
    local def_str="Y/n"; [[ "$default" == false ]] && def_str="y/N"
    read -rp "  $question [$def_str]: " input || input=""
    case "${input,,}" in
        j|ja|y|yes) printf -v "$result_var" '%s' true ;;
        n|nein|no)  printf -v "$result_var" '%s' false ;;
        *)          printf -v "$result_var" '%s' "$default" ;;
    esac
}

mo_apply_inplace_defaults() {
    local outdir="$1"

    if [[ -n "$outdir" ]]; then
        [[ "${DELETE_ORIGINAL_EXPLICIT:-false}" == true ]] || DELETE_ORIGINAL=false
        [[ "${RENAME_INPLACE_EXPLICIT:-false}" == true ]] || RENAME_INPLACE=false
        return 0
    fi

    [[ "${DELETE_ORIGINAL_EXPLICIT:-false}" == true ]] || DELETE_ORIGINAL=true
    [[ "${RENAME_INPLACE_EXPLICIT:-false}" == true ]] || RENAME_INPLACE=true

    # Warn once per call chain, not per sub-script.
    [[ -n "${MO_INPLACE_WARNED:-}" ]] && return 0
    export MO_INPLACE_WARNED=1

    printf "\n%b╔══════════════════════════════════════════════════════════════╗%b\n" "$C_YELLOW" "$C_RESET"
    printf "%b║%b  %bIN-PLACE MODE: originals will be modified%b                  %b║%b\n" \
        "$C_YELLOW" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_YELLOW" "$C_RESET"
    printf "%b╚══════════════════════════════════════════════════════════════╝%b\n" "$C_YELLOW" "$C_RESET"
    printf "  No target directory given, so:\n"
    printf "    Move originals to trash  : %b%s%b\n" "$C_BOLD" "$DELETE_ORIGINAL" "$C_RESET"
    printf "    Remove _h265 suffix      : %b%s%b\n" "$C_BOLD" "$RENAME_INPLACE" "$C_RESET"
    printf "  Originals go to the trash and can be restored from there.\n"

    if [[ ! -t 0 ]]; then
        printf "  %bCannot prompt (not interactive).%b\n" "$C_CYAN" "$C_RESET"
        return 0
    fi
    if [[ "${ASSUME_YES:-false}" == true ]]; then
        printf "  %b-y given, settings are kept.%b\n" "$C_CYAN" "$C_RESET"
        return 0
    fi

    local answer
    read -rp "  Keep these settings? [Y/n]: " answer || answer=""
    if [[ "${answer,,}" =~ ^(n|nein|no)$ ]]; then
        mo_prompt_bool "Move originals to the trash after a successful conversion?" \
            "$DELETE_ORIGINAL" DELETE_ORIGINAL
        mo_prompt_bool "Rename the result to the original name after deleting it?" \
            "$RENAME_INPLACE" RENAME_INPLACE
        DELETE_ORIGINAL_EXPLICIT=true
        RENAME_INPLACE_EXPLICIT=true
        export DELETE_ORIGINAL_EXPLICIT RENAME_INPLACE_EXPLICIT
    fi
    echo ""
    return 0
}

# ------------------------------------------------------------------------------
# Protokoll
#
# A continuous text file next to media-optimizer.sh. Every run appends a
# block: header with settings, one line per processed file, and the
# summary at the end. Orchestrator and sub-scripts share MO_RUN_ID so
# zusammengehoerende Zeilen erkennbar bleiben.
#
# Lines are appended by parallel workers. That is safe as long as a
# line stays short: appending under O_APPEND is atomic up to the system
# buffer size. So no long messages here.
# ------------------------------------------------------------------------------
# No preset at this level: common.sh is sourced BEFORE load_config.
# A value set here would count as explicit for load_config and would
# overwrite the config file entry again. The defaults
# therefore live in mo_log_init, after the configuration is loaded.
mo_log_enabled() {
    [[ "${MO_LOG:-true}" == true && -n "${MO_LOG_FILE:-}" ]]
}

# mo_log_init <skriptname> <quelle> <ziel>
# Sets run ID and path, rotates if needed and writes the header.
# The header is written by the first caller only; sub-scripts started by
# the orchestrator inherit MO_RUN_ID and only log one line.
mo_log_init() {
    local script="$1" src="$2" dst="$3"
    : "${MO_LOG:=true}"
    : "${MO_LOG_MAX_KB:=5120}"
    # Always export, even when disabled: otherwise the sub-scripts do not
    # inherit the decision and would log anyway.
    export MO_LOG MO_LOG_MAX_KB
    [[ "$MO_LOG" == true ]] || return 0
    : "${MO_LOG_FILE:=${_MO_ROOT:-.}/media-optimizer.log}"
    export MO_LOG_FILE

    if ! touch "$MO_LOG_FILE" 2>/dev/null; then
        printf "%b[LOG]%b not writable, skipping: %s\n" \
            "$C_YELLOW" "$C_RESET" "$MO_LOG_FILE" >&2
        MO_LOG=false; export MO_LOG
        return 0
    fi

    # One-time rotation so the file does not grow without bound.
    local size_kb; size_kb=$(( $(stat -c%s "$MO_LOG_FILE" 2>/dev/null || echo 0) / 1024 ))
    if (( size_kb > MO_LOG_MAX_KB )); then
        mv -f "$MO_LOG_FILE" "${MO_LOG_FILE}.1" 2>/dev/null || true
        : > "$MO_LOG_FILE"
    fi

    if [[ -z "${MO_RUN_ID:-}" ]]; then
        # A random part is needed: on a restart via exec the PID stays the
        # same, and within the same second the ID would otherwise repeat.
        MO_RUN_ID=$(date '+%y%m%d-%H%M%S')-$$-$RANDOM
        export MO_RUN_ID
        {
            printf '\n%s\n' "================================================================"
            printf 'RUN %s  started %s\n' "$MO_RUN_ID" "$(date '+%F %T')"
            printf '  Call   : %s\n' "$script"
            printf '  Source : %s\n' "$src"
            printf '  Target : %s\n' "${dst:-(in place)}"
        } >> "$MO_LOG_FILE" 2>/dev/null || true
    else
        # Sub-script inside an orchestrator run: only one line,
        # the header is already in the log from the orchestrator.
        mo_log "${MO_LOG_TAG:-stufe}" "started ($script)"
    fi
    return 0
}

# mo_log_settings <VAR> [<VAR> ...] - writes "NAME=value" per line
mo_log_settings() {
    mo_log_enabled || return 0
    local v
    printf '  Settings:\n' >> "$MO_LOG_FILE" 2>/dev/null || return 0
    for v in "$@"; do
        printf '    %-22s %s\n' "$v" "${!v-}" >> "$MO_LOG_FILE" 2>/dev/null || true
    done
    printf '%s\n' "----------------------------------------------------------------" \
        >> "$MO_LOG_FILE" 2>/dev/null || true
}

# mo_log <tag> <meldung...>
mo_log() {
    mo_log_enabled || return 0
    local tag="$1"; shift
    printf '%s  %-6s %s\n' "$(date '+%H:%M:%S')" "$tag" "$*" \
        >> "$MO_LOG_FILE" 2>/dev/null || true
}

# mo_log_file <tag> <status> <quelle> [<ziel>] [<bytes_alt>] [<bytes_neu>]
# One line per processed file. Sizes are only appended when both
# values are present and meaningful.
mo_log_file() {
    mo_log_enabled || return 0
    local tag="$1" status="$2" src="$3" dst="${4:-}" old="${5:-}" new="${6:-}"
    local extra=""
    if [[ "$old" =~ ^[0-9]+$ && "$new" =~ ^[0-9]+$ ]] && (( old > 0 )); then
        extra=" ($(format_bytes "$old") -> $(format_bytes "$new"), $(pct_change "$old" "$new")%)"
    fi
    printf '%s  %-6s %-9s %s%s%s\n' "$(date '+%H:%M:%S')" "$tag" "$status" \
        "$src" "${dst:+ -> $dst}" "$extra" >> "$MO_LOG_FILE" 2>/dev/null || true
}

# mo_log_close <exitcode>  - only from the outermost caller
mo_log_close() {
    mo_log_enabled || return 0
    printf '%s\n' "----------------------------------------------------------------" \
        >> "$MO_LOG_FILE" 2>/dev/null || true
    printf 'RUN %s  finished %s  (code %s)\n' "$MO_RUN_ID" "$(date '+%F %T')" "${1:-0}" \
        >> "$MO_LOG_FILE" 2>/dev/null || true
    printf '%s\n' "================================================================" \
        >> "$MO_LOG_FILE" 2>/dev/null || true
}

export -f mo_log_enabled mo_log mo_log_file
