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
# Formatting
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

# pct_change <old> <new> -> percentage change, safe against division by zero
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
    IMG_TARGET IMG_WEBP_QUALITY IMG_DISCARD_IF_LARGER IMG_COPY_METADATA PNG_MODE PNG_QUALITY CJXL_THREADS COMPRESSION_METHOD GIF_KMIN
    ENCODER_MODE BITRATE_THRESHOLD_KBPS GPU_MIN_PIXELS GPU_QP CPU_CRF CPU_PRESET
    CPU_X265_PARAMS VAAPI_DEVICE ENABLE_PROBE PROBE_MARGIN_PCT
    DISCARD_IF_LARGER KEEP_SALVAGED_CORRUPT VIDEO_EXTENSIONS VIDEO_CONTAINER SOURCE_CODECS AVIF_SOURCE_CODECS MIN_SIZE_MB FASTSTART USE_CACHE
    GIF_TARGET GIF_AVIF_CRF AVIF_MAX_SECONDS AVIF_CRF AVIF_CRF_RETRY
    AVIF_PRESET AVIF_PIX_FMT AVIF_REQUIRE_SILENT ENABLE_AVIF_STAGE
    DELETE_ORIGINAL_EXPLICIT RENAME_INPLACE_EXPLICIT MO_LOG MO_LOG_FILE
    MO_LOG_MAX_KB DURATION_TOLERANCE_PCT PROBE_DURATION PROBE_SLICES RENAME_INPLACE
    GPU_ALLOW_10BIT VERIFY_VISUAL VISUAL_PSNR_MIN VISUAL_SAMPLES
    VISUAL_STRICT CHECK_VISUAL ENABLE_VERIFY_OUTPUT AUTO_FIX_LIST HEVC_TAG GPU_CODEC VULKAN_DEVICE
    GPU_RC_MODE GPU_BF GPU_LOW_POWER GPU_ASYNC_DEPTH GPU_BITRATE
    INCLUDE_HIDDEN
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

# At the end of a run: one prompt for everything that could not go to the
# trash.
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
        echo "  List: $log"
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
# cleanup_stale_parts <directory> <glob> [<glob> ...]
# ------------------------------------------------------------------------------
cleanup_stale_parts() {
    local dir="$1"; shift
    [[ -d "$dir" ]] || return 0
    [[ "${DRY_RUN:-false}" == true ]] && return 0   # dry run deletes nothing
    local pattern f n=0
    for pattern in "$@"; do
        while IFS= read -r -d '' f; do
            rm -f -- "$f" 2>/dev/null && n=$(( n + 1 ))
        done < <(find "$dir" -type f -name "$pattern" -print0 2>/dev/null)
    done
    # orphaned lock directories
    while IFS= read -r -d '' f; do
        rmdir -- "$f" 2>/dev/null && n=$(( n + 1 ))
    done < <(find "$dir" -type d -name "*.molock" -print0 2>/dev/null)

    (( n > 0 )) && printf "%b[CLEANUP]%b %d leftover(s) from an aborted run removed.\n" \
        "$C_CYAN" "$C_RESET" "$n"
    return 0
}

# ------------------------------------------------------------------------------
# Output verification
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
            local t; t=$(mktemp --suffix=.ppm "${TMPDIR:-/tmp}/mo_verify_XXXXXX") || return 0
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
    if (( rc == MO_RC_PARTIAL )); then
        printf "\n%b[DONE]%b Finished, but some files failed (see the summary above).\n" \
            "$C_YELLOW" "$C_RESET" >&2
    elif (( rc != 0 )); then
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
    trap '_mo_rc=$?; [[ -z "$_MO_ERR_INFO" ]] && _MO_ERR_INFO="line $LINENO: \`$BASH_COMMAND\` (code $_mo_rc)"' ERR
    trap '_mo_exit_rc=$?; mo_remove_temp_files; mo_hold_open $_mo_exit_rc' EXIT
}

# ------------------------------------------------------------------------------
# Comparing picture content
#
# The runtime check catches truncated files but no content damage: a green
# picture with artefacts still has the correct duration. So source and target
# are compared at several positions.
#
# One ffmpeg call per sample does the comparison directly in memory. Writing
# the frames out first is not only slower (three processes per sample instead
# of one), it is also less accurate: seeking both files inside a single
# process aligns the frames far better, which removes the false alarms that
# variable frame rates used to produce.
#
# Stills are written only when VISUAL_KEEP_DIR is set (--keep-samples).
#
# verify_visual <source> <target> [min_psnr]
# 0 = fine or not measurable, 1 = broken (VISUAL_PSNR set)
# ------------------------------------------------------------------------------
VISUAL_PSNR=""        # best sample
VISUAL_PSNR_ALL=""    # every sample, for traceability

_visual_sample() {
    local src="$1" dst="$2" t="$3"
    ffmpeg -nostdin -hide_banner -noautorotate -ss "$t" -i "$dst" \
           -noautorotate -ss "$t" -i "$src" \
        -lavfi "[0:v]select=eq(n\,0),setpts=0[x];[1:v]select=eq(n\,0),setpts=0[y];[x][y]scale2ref=flags=bilinear[a][b];[a][b]psnr" \
        -frames:v 1 -f null - </dev/null 2>&1 |
        grep -oE 'average:[0-9]+\.[0-9]+' | head -1 | cut -d: -f2
}

_visual_keep() {
    local src="$1" dst="$2" t="$3" base="$4"
    mkdir -p "$VISUAL_KEEP_DIR" 2>/dev/null || return 0
    ffmpeg -nostdin -y -v error -noautorotate -ss "$t" -i "$src" -map 0:v:0 \
        -frames:v 1 "$VISUAL_KEEP_DIR/${base}_${t}s_source.png" </dev/null 2>/dev/null || true
    ffmpeg -nostdin -y -v error -noautorotate -ss "$t" -i "$dst" -map 0:v:0 \
        -frames:v 1 "$VISUAL_KEEP_DIR/${base}_${t}s_target.png" </dev/null 2>/dev/null || true
    return 0
}

verify_visual() {
    local src="$1" dst="$2" minp="${3:-18}"
    VISUAL_PSNR=""; VISUAL_PSNR_ALL=""
    local dur; dur=$(media_duration "$src")
    [[ -n "$dur" && "$dur" != "N/A" ]] || return 0

    local samples="${VISUAL_SAMPLES:-3}"
    local base; base="$(basename "$dst")"; base="${base%.*}"

    local i frac t p best="" measured=0
    for (( i = 1; i <= samples; i++ )); do
        frac=$(( 100 * i / (samples + 1) ))
        t=$(awk -v d="$dur" -v f="$frac" 'BEGIN { printf "%.2f", d * f / 100 }')

        p=$(_visual_sample "$src" "$dst" "$t")
        if [[ "$p" =~ ^[0-9]+\.[0-9]+$ ]]; then
            measured=1
            VISUAL_PSNR_ALL="${VISUAL_PSNR_ALL:+$VISUAL_PSNR_ALL, }${t}s=${p%.*} dB"
            if [[ -z "$best" ]] || awk -v n="$p" -v o="$best" 'BEGIN { exit (n > o) ? 0 : 1 }'; then
                best="$p"
            fi
        fi

        [[ -n "${VISUAL_KEEP_DIR:-}" ]] && _visual_keep "$src" "$dst" "$t" "$base"
    done

    (( measured == 1 )) || return 0
    VISUAL_PSNR="${best%.*}"

    # The BEST sample decides. A single bad measurement can still come from a
    # seek offset; a genuinely broken picture is bad at every position.
    awk -v p="$best" -v m="$minp" 'BEGIN { exit (p < m) ? 0 : 1 }' && return 1
    return 0
}

export -f media_duration verify_video verify_visual _visual_sample _visual_keep

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

# mo_prompt_rescan
# The interactive "scan everything again?" question for the video cache.
# Asked the other way round than USE_CACHE, because ignoring the cache is the
# deliberate choice; the default shown is the current setting.
mo_prompt_rescan() {
    local rescan=false
    [[ "${USE_CACHE:-true}" == false ]] && rescan=true
    mo_prompt_bool "Scan all videos again, ignoring the cache of finished ones (slower)?" \
        "$rescan" rescan
    if [[ "$rescan" == true ]]; then USE_CACHE=false; else USE_CACHE=true; fi
}

# mo_prompt_delete_original <output_dir>
# The interactive "delete originals?" question. The answer is a deliberate
# choice, so it sets DELETE_ORIGINAL_EXPLICIT; without the marker
# mo_apply_inplace_defaults would overwrite it again. The default shown is
# the one that mode would apply, so pressing Enter changes nothing.
mo_prompt_delete_original() {
    local outdir="$1" def
    if [[ "${DELETE_ORIGINAL_EXPLICIT:-false}" == true ]]; then def="$DELETE_ORIGINAL"
    elif [[ -n "$outdir" ]]; then def=false
    else def=true; fi
    mo_prompt_bool "Move originals to the trash after a successful conversion?" \
        "$def" DELETE_ORIGINAL
    DELETE_ORIGINAL_EXPLICIT=true
}

mo_apply_inplace_defaults() {
    local outdir="$1" with_rename="${2:-false}"   # with_rename: script has RENAME_INPLACE

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
    [[ "$with_rename" == true ]] && \
        printf "    Remove _h265 suffix      : %b%s%b\n" "$C_BOLD" "$RENAME_INPLACE" "$C_RESET"
    [[ "$DELETE_ORIGINAL" == true ]] && \
        printf "  Originals go to the trash and can be restored from there.\n"

    # Already decided by flag, environment, config file or prompt: the
    # banner is information only, there is nothing left to ask.
    if [[ "${DELETE_ORIGINAL_EXPLICIT:-false}" == true ]] \
       && [[ "$with_rename" != true || "${RENAME_INPLACE_EXPLICIT:-false}" == true ]]; then
        echo ""
        return 0
    fi
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
        DELETE_ORIGINAL_EXPLICIT=true
        export DELETE_ORIGINAL_EXPLICIT
        if [[ "$with_rename" == true ]]; then
            mo_prompt_bool "Rename the result to the original name after deleting it?" \
                "$RENAME_INPLACE" RENAME_INPLACE
            RENAME_INPLACE_EXPLICIT=true
            export RENAME_INPLACE_EXPLICIT
        fi
    fi
    echo ""
    return 0
}

# ------------------------------------------------------------------------------
# Log
#
# A continuous text file next to media-optimizer.sh. Every run appends a
# block: header with settings, one line per processed file, and the
# summary at the end. Orchestrator and sub-scripts share MO_RUN_ID so
# lines that belong together stay recognisable.
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
    # A dry run promises to write nothing, the log included.
    [[ "${DRY_RUN:-false}" == true ]] && MO_LOG=false
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

# ------------------------------------------------------------------------------
# Writing state files
#
# State files are read back with "source", so every value must be quoted in a
# way that bash cannot interpret. printf '%q' does exactly that: a directory
# named  x"; rm -rf ~; #  would otherwise end up as executable code.
#
# mo_write_vars <file> <VAR> [<VAR> ...]
# ------------------------------------------------------------------------------
mo_write_vars() {
    local file="$1"; shift
    local v
    : > "$file" || return 1
    for v in "$@"; do
        printf '%s=%q\n' "$v" "${!v-}" >> "$file"
    done
    return 0
}

# ------------------------------------------------------------------------------
# Fatal versus per-file failure
#
# xargs keeps going on exit codes 1-124 and aborts the whole batch on 255.
# A missing or non-executable encoder is not a per-file problem: every
# following file would fail the same way. Workers therefore return 255 in
# that case and 1 for an ordinary failure.
#
# What xargs itself then returns (measured with GNU xargs):
#   123  at least one worker returned 1-125: per-file failures, batch complete
#   124  a worker returned 255: batch aborted. NOT 255, and not a Ctrl+C.
#   125  a worker was killed by a signal
#
# Script exit codes seen by the orchestrator:
#   0              everything converted, skipped or deliberately discarded
#   MO_RC_PARTIAL  batch complete, but some files failed; the next stage
#                  can still run
#   255            fatal, batch aborted
#   130            interrupted, resumable
# ------------------------------------------------------------------------------
# A constant, not a setting: not in MO_CONFIG_VARS, so load_config ignores it.
MO_RC_PARTIAL=3
export MO_RC_PARTIAL
mo_encoder_missing() {
    local cmd="$1"
    command -v "$cmd" >/dev/null 2>&1 && return 1
    printf "%b[FATAL]%b %s not found. Aborting the batch.\n" \
        "$C_RED" "$C_RESET" "$cmd" >&2
    return 0
}
export -f mo_encoder_missing

# ------------------------------------------------------------------------------
# Temp files
#
# mo_mktemp <var> <prefix> [<suffix>]
# Creates a temp file under $TMPDIR (default /tmp), stores its path in <var>
# and registers it, so the EXIT trap removes it even after an error exit.
# Out-variable instead of $(...): a registration made in a subshell would be
# lost.
# ------------------------------------------------------------------------------
_MO_TEMP_FILES=()

mo_mktemp() {
    local _var="$1" _prefix="$2" _suffix="${3:-}" _f
    if [[ -n "$_suffix" ]]; then
        _f=$(mktemp --suffix="$_suffix" "${TMPDIR:-/tmp}/${_prefix}_XXXXXX") || return 1
    else
        _f=$(mktemp "${TMPDIR:-/tmp}/${_prefix}_XXXXXX") || return 1
    fi
    _MO_TEMP_FILES+=("$_f")
    printf -v "$_var" '%s' "$_f"
}

mo_remove_temp_files() {
    local f
    for f in "${_MO_TEMP_FILES[@]}"; do rm -f -- "$f" 2>/dev/null || true; done
    _MO_TEMP_FILES=()
    return 0
}

# ------------------------------------------------------------------------------
# Option parsing and validation
#
# mo_need_value "$@"  - call in an option parser before reading "$2". Under
#                       set -u a missing value would otherwise crash with
#                       "unbound variable".
# mo_validate <VAR>... - checks each setting against MO_RULES and exits 2
#                       with one message per bad value. Run it after the
#                       config file, the CLI and the prompts, so every source
#                       of a value is covered.
#
# Rules: int:<min>:<max>, num:<min>:<max> (decimals allowed), bool, or the
# allowed words separated by "|".
# ------------------------------------------------------------------------------
mo_need_value() {
    if (( $# < 2 )); then
        printf "Option %s needs a value.\n" "$1" >&2
        exit 2
    fi
}

declare -A MO_RULES=(
    [MAX_WORKERS]="int:1:256"            [JXL_EFFORT]="int:1:10"
    [PNG_QUALITY]="int:1:100"            [IMG_WEBP_QUALITY]="int:0:100"
    [CJXL_THREADS]="int:0:256"           [IMG_TARGET]="jxl|webp"
    [PNG_MODE]="lossless|lossy"          [IMG_COPY_METADATA]="auto|always|never"
    [COMPRESSION_METHOD]="int:0:6"       [GIF_KMIN]="int:0:100000"
    [GIF_TARGET]="webp|avif"             [GIF_AVIF_CRF]="int:0:63"
    [AVIF_MAX_SECONDS]="num:0.1:86400"   [AVIF_CRF]="int:0:63"
    [AVIF_CRF_RETRY]="int:0:63"          [AVIF_PRESET]="int:0:13"
    [AVIF_PIX_FMT]="yuv420p|yuv444p"     [ENCODER_MODE]="auto|gpu|cpu"
    [BITRATE_THRESHOLD_KBPS]="int:0:10000000"
    [GPU_QP]="int:0:255"                 [CPU_CRF]="num:0:51"
    [CPU_PRESET]="ultrafast|superfast|veryfast|faster|fast|medium|slow|slower|veryslow|placebo"
    [MIN_SIZE_MB]="num:0:10000000"       [PROBE_MARGIN_PCT]="int:1:100"
    [VIDEO_CONTAINER]="auto|mp4|mkv|keep"
    [DELETE_ORIGINAL]="bool"             [FORCE_DELETE]="bool"
    [VERIFY_DEEP]="bool"                 [IMG_DISCARD_IF_LARGER]="bool"
    [DISCARD_IF_LARGER]="bool"           [DRY_RUN]="bool"
    [VERIFY_VISUAL]="bool"               [VISUAL_STRICT]="bool"
    [AVIF_REQUIRE_SILENT]="bool"         [ENABLE_PROBE]="bool"
    [USE_CACHE]="bool"                   [KEEP_SALVAGED_CORRUPT]="bool"
    [RENAME_INPLACE]="bool"              [INCLUDE_HIDDEN]="bool"
    [ENABLE_AVIF_STAGE]="bool"           [CHECK_VISUAL]="bool"
    [PSNR_MIN]="num:1:100"               [SAMPLES]="int:1:100"
    [DURATION_TOL]="num:0:100"
)

mo_validate() {
    local v rule val lo hi kind bad=0 why
    for v in "$@"; do
        rule="${MO_RULES[$v]:-}"
        [[ -n "$rule" && -n "${!v+x}" ]] || continue
        val="${!v}"; why=""
        case "$rule" in
            int:*|num:*)
                IFS=: read -r kind lo hi <<< "$rule"
                if [[ "$kind" == int && ! "$val" =~ ^[0-9]+$ ]]; then
                    why="a whole number from $lo to $hi"
                elif [[ "$kind" == num && ! "$val" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
                    why="a number from $lo to $hi"
                elif ! awk -v x="$val" -v a="$lo" -v b="$hi" 'BEGIN { exit (x >= a && x <= b) ? 0 : 1 }'; then
                    why="from $lo to $hi"
                fi ;;
            bool)
                [[ "$val" == true || "$val" == false ]] || why="true or false" ;;
            *)
                [[ "|$rule|" == *"|$val|"* ]] || why="one of: ${rule//|/, }" ;;
        esac
        if [[ -n "$why" ]]; then
            printf "%b[SETTING]%b %s='%s' is invalid, expected %s.\n" \
                "$C_RED" "$C_RESET" "$v" "$val" "$why" >&2
            bad=1
        fi
    done
    (( bad == 0 )) || exit 2
}

# ------------------------------------------------------------------------------
# Finding source files
#
# mo_find_files <dir> [-maxdepth <n>] <match expression...>
# Prints the matching regular files below <dir>, NUL-separated.
#   - Trash and NAS system folders are always skipped: .Trash, .Trash-<uid>,
#     $RECYCLE.BIN, @eaDir, #recycle, #snapshot, System Volume Information,
#     lost+found. Converting a trashed file would resurrect it elsewhere.
#   - AppleDouble files (._name) are always skipped; they only look like
#     images.
#   - Other hidden files and folders are skipped unless INCLUDE_HIDDEN=true.
# The start directory itself is never filtered, so a hidden source works.
# Always returns 0: an unreadable subfolder is reported by find on stderr but
# must not turn a complete batch into a failed one.
# Every scanner uses this, so the orchestrator's counts match the stages.
# ------------------------------------------------------------------------------
_mo_skip_dirs() {
    _MO_SKIP=(-name '.Trash' -o -name '.Trash-*' -o -name '$RECYCLE.BIN' -o -name '@eaDir'
              -o -name '#recycle' -o -name '#snapshot' -o -name 'System Volume Information'
              -o -name 'lost+found')
    [[ "${INCLUDE_HIDDEN:-false}" == true ]] || _MO_SKIP+=(-o -name '.*')
}

mo_find_files() {
    local dir="$1"; shift
    local -a depth=() skip=() hidden=() _MO_SKIP=()
    if [[ "${1:-}" == "-maxdepth" ]]; then depth=(-maxdepth "$2"); shift 2; fi
    _mo_skip_dirs; skip=("${_MO_SKIP[@]}")
    [[ "${INCLUDE_HIDDEN:-false}" == true ]] || hidden=(! -name '.*')
    find "$dir" -mindepth 1 "${depth[@]}" \
        \( -type d \( "${skip[@]}" \) -prune \) -o \
        \( -type f ! -name '._*' "${hidden[@]}" \( "$@" \) -print0 \) || true
}

# mo_find_state_files <dir> <name>
# Prints the state files called <name> in subfolders of <dir>, NUL-separated;
# <dir> itself is not included. The same folders are skipped as in
# mo_find_files, so a cache in the trash is never picked up. Always returns 0.
mo_find_state_files() {
    local dir="$1" name="$2" _MO_SKIP=()
    _mo_skip_dirs
    find "$dir" -mindepth 1 \( -type d \( "${_MO_SKIP[@]}" \) -prune \) -o \
        \( -mindepth 2 -type f -name "$name" -print0 \) 2>/dev/null || true
}

# mo_stem <filename>  -> name without extension; ".png" stays ".png" instead
# of becoming an empty name.
mo_stem() {
    local f="$1" s="${1%.*}"
    [[ -n "$s" ]] || s="$f"
    printf '%s' "$s"
}

# mo_real_type <file> -> jpg | png | webp | jxl | gif | mp4 | "" (unknown)
# The one place that maps MIME types to extensions.
mo_real_type() {
    case "$(file -b --mime-type -- "$1" 2>/dev/null || true)" in
        image/jpeg) echo jpg ;;  image/png) echo png ;;
        image/webp) echo webp ;; image/jxl) echo jxl ;;
        image/gif)  echo gif ;;  video/mp4) echo mp4 ;;
        *)          echo "" ;;
    esac
}

# mo_migrate_name <old path> <new path>
# Renames a state file from an older (German) name once, if only the old one
# exists. Not in a dry run.
mo_migrate_name() {
    [[ "${DRY_RUN:-false}" == true ]] && return 0
    if [[ -e "$1" && ! -e "$2" ]]; then mv -n -- "$1" "$2" 2>/dev/null || true; fi
    return 0
}

export -f mo_find_files mo_stem mo_real_type

# ------------------------------------------------------------------------------
# One run per working directory
#
# mo_lock_workdir <dir>
# The scripts clean up .part files and lock directories at startup, which
# would belong to a second run on the same tree. The lock is on the directory
# itself (no lock file is left behind) and the kernel releases it when the
# process ends, even after SIGKILL. Not taken in a dry run, which touches
# nothing. MO_LOCK_FD is closed for background jobs (see mo_job_start), so a
# stray child cannot keep the next stage out.
# ------------------------------------------------------------------------------
MO_LOCK_FD=""

mo_lock_workdir() {
    local dir="$1"
    [[ "${DRY_RUN:-false}" == true ]] && return 0
    command -v flock >/dev/null 2>&1 || return 0
    exec {MO_LOCK_FD}<"$dir"
    if ! flock -n "$MO_LOCK_FD"; then
        printf "%b[BUSY]%b Another media-optimizer run is working on '%s'.\n" \
            "$C_RED" "$C_RESET" "$dir" >&2
        exit 1
    fi
}

# ------------------------------------------------------------------------------
# Long-running jobs that stay stoppable
#
# Bash runs a trap only after the foreground command has finished. With the
# batch or an ffmpeg encode in the foreground, SIGTERM would wait for the
# whole batch. So such jobs run in the background and the script waits;
# "wait" returns as soon as a signal arrives.
#
#   mo_job_start <command...>   start in the background, stdin kept
#   mo_job_wait                 wait for it, return its exit status
#   mo_job_stop                 SIGINT to the whole process tree, then wait
#
# Measured details this depends on:
#   - Bash starts background jobs with SIGINT ignored, and that is inherited
#     by every worker. The job resets it, otherwise neither Ctrl+C nor
#     mo_job_stop would reach the workers.
#   - SIGINT first: workers end the same way as on Ctrl+C, run their EXIT
#     trap, and bash prints no "Terminated" line per killed child.
#   - SIGTERM as well, for a script that was itself started in the
#     background of a non-interactive shell (nohup, cron, another script):
#     it has SIGINT ignored from the start and cannot undo that, so every
#     process below it ignores SIGINT too. Both are sent while the process
#     is frozen; pending signals arrive lowest number first, so SIGINT wins
#     wherever it is not ignored.
#   - The tree is frozen with SIGSTOP first, so xargs cannot start a new
#     worker while the tree is walked.
# ------------------------------------------------------------------------------
MO_JOB_PID=""

mo_job_start() {
    if [[ -n "$MO_LOCK_FD" ]]; then
        { trap - INT QUIT; "$@"; } <&0 {MO_LOCK_FD}<&- &
    else
        { trap - INT QUIT; "$@"; } <&0 &
    fi
    MO_JOB_PID=$!
}

mo_job_wait() {
    local rc=0
    [[ -n "$MO_JOB_PID" ]] || return 0
    wait "$MO_JOB_PID" || rc=$?
    MO_JOB_PID=""
    return "$rc"
}

_MO_STOPPED_PIDS=()

_mo_kill_tree() {
    local pid="$1" c
    kill -STOP "$pid" 2>/dev/null || return 0
    _MO_STOPPED_PIDS+=("$pid")
    if command -v pgrep >/dev/null 2>&1; then
        for c in $(pgrep -P "$pid" 2>/dev/null || true); do _mo_kill_tree "$c"; done
    fi
    kill -INT "$pid" 2>/dev/null || true
    kill -TERM "$pid" 2>/dev/null || true
    kill -CONT "$pid" 2>/dev/null || true
}

# Waiting for the job's top process is not enough: when it ends, its children
# may still be finishing. ffmpeg, for one, flushes the encoder and writes the
# trailer on SIGINT, which took about a second in a test. So wait for every
# process that was signalled, up to MO_STOP_TIMEOUT seconds (default 10),
# then SIGKILL what is left.
mo_job_stop() {
    [[ -n "$MO_JOB_PID" ]] || return 0
    _MO_STOPPED_PIDS=()
    _mo_kill_tree "$MO_JOB_PID"
    wait "$MO_JOB_PID" 2>/dev/null || true
    MO_JOB_PID=""

    local p alive i limit=$(( ${MO_STOP_TIMEOUT:-10} * 10 ))
    for (( i = 0; i < limit; i++ )); do
        alive=false
        for p in "${_MO_STOPPED_PIDS[@]}"; do
            if kill -0 "$p" 2>/dev/null; then alive=true; break; fi
        done
        [[ "$alive" == true ]] || return 0
        sleep 0.1
    done
    for p in "${_MO_STOPPED_PIDS[@]}"; do kill -KILL "$p" 2>/dev/null || true; done
    return 0
}

# ------------------------------------------------------------------------------
# Worker scaffold for the xargs scripts (img, gif, avif)
#
# A worker handles exactly one file in its own "bash -c", started as
#     bash -c 'mo_worker_start "$1"; convert_x "$1"' _
# mo_worker_start switches on set -euo pipefail. Any command that fails
# unexpectedly therefore ends the worker before it reaches the deletion step;
# the EXIT trap then records FAIL, removes the temp file and releases the
# lock. Ctrl+C and mo_job_stop end it without FAIL: the run is resumable and
# the file is retried.
#
# Every handled outcome records exactly one token with mo_w_result and
# returns 0. A worker exits 255 only when an encoder is missing, which makes
# xargs abort the batch.
#
# Note: set -e is ignored inside a function called as "f || x". Do not wrap
# worker functions that way, or the safety net is off.
# ------------------------------------------------------------------------------
mo_worker_start() {
    set -euo pipefail
    _MO_W_SRC="$1"; _MO_W_TEMP=""; _MO_W_LOCK=""; _MO_W_RESULT=""
    trap '_mo_w_exit $?' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

# mo_w_result <TOKEN> [<orig_bytes> <new_bytes>]
mo_w_result() {
    _MO_W_RESULT="$1"
    echo "$*" >> "$CURRENT_RUN_LOG"
}

# mo_w_fail <src> <reason>  - FAIL, original kept
mo_w_fail() {
    mo_w_result FAIL
    mo_log_file "$MO_LOG_TAG" "ERROR" "$1"
    printf "%b[ERROR]%b %s: '%s' (original kept)\n" \
        "$C_RED" "$C_RESET" "$2" "$(basename -- "$1")" >&2
}

# mo_w_conflict <src> <message>  - COLLISION, original kept
mo_w_conflict() {
    mo_w_result COLLISION
    mo_log_file "$MO_LOG_TAG" "CONFLICT" "$1"
    printf "%b[CONFLICT]%b '%s': %s, original kept.\n" \
        "$C_YELLOW" "$C_RESET" "$(basename -- "$1")" "$2" >&2
}

_mo_w_exit() {
    local rc="$1"
    if [[ -n "${_MO_W_TEMP:-}" ]]; then rm -f -- "$_MO_W_TEMP"; fi
    if [[ -n "${_MO_W_LOCK:-}" ]]; then rmdir -- "$_MO_W_LOCK" 2>/dev/null || true; fi
    if [[ -z "${_MO_W_RESULT:-}" ]] && (( rc < 128 )); then
        mo_w_fail "${_MO_W_SRC:-?}" "unexpected error (code $rc)"
    fi
    return 0
}

# mo_w_target_folder <src>  -> folder for the output: mirrored below
# OUTPUT_DIR, or next to the source in place.
mo_w_target_folder() {
    local src="$1" rel
    if [[ -z "${OUTPUT_DIR:-}" ]]; then dirname -- "$src"; return 0; fi
    rel="$(dirname -- "${src#"$SOURCE_DIR"/}")"
    if [[ "$rel" == "." ]]; then printf '%s' "$OUTPUT_DIR"
    else printf '%s' "$OUTPUT_DIR/$rel"; fi
}

# mo_w_claim <src> <target_folder> <stem> <dest>
# Creates the folder, claims the name with a lock directory and checks again,
# under the lock, that <dest> does not exist yet: a worker for foo.jpg may
# have finished foo.jxl since the caller's unlocked check.
# Returns 0 when claimed; 1 when a result was recorded and the caller stops.
mo_w_claim() {
    local src="$1" folder="$2" stem="$3" dest="$4" lockdir
    if ! mkdir -p -- "$folder" 2>/dev/null; then
        mo_w_fail "$src" "cannot create target folder '$folder'"; return 1
    fi
    lockdir="$folder/.${stem}.molock"
    if ! mkdir -- "$lockdir" 2>/dev/null; then
        if [[ ! -d "$lockdir" ]]; then
            mo_w_fail "$src" "cannot create a lock in '$folder'"; return 1
        fi
        mo_w_conflict "$src" "target name '$(basename -- "$dest")' already claimed"
        return 1
    fi
    _MO_W_LOCK="$lockdir"
    if [[ -e "$dest" ]]; then
        mo_w_conflict "$src" "'$(basename -- "$dest")' already exists"; return 1
    fi
    return 0
}

# mo_w_temp <dest>
# Sets _MO_W_TEMP to a unique temp name next to <dest>. The EXIT trap removes
# it unless mo_w_commit moved it into place. Call it directly, not in $(...),
# and read the name from $_MO_W_TEMP.
mo_w_temp() {
    _MO_W_TEMP="${1}.part.$$.${RANDOM}.${1##*.}"
}

# mo_w_commit <src> <tmp> <dest>
# Moves a verified temp file into place without ever overwriting.
# Returns 0 when moved; 1 when a result was recorded and the caller stops.
mo_w_commit() {
    local src="$1" tmp="$2" dest="$3"
    touch -r "$src" "$tmp" 2>/dev/null || true
    # GNU mv -n returns 0 even when it refuses (coreutils 9.10), so whether
    # the temp file is gone is the test.
    if ! mv -n -- "$tmp" "$dest"; then
        mo_w_fail "$src" "could not move the output into place"; return 1
    fi
    if [[ -e "$tmp" ]]; then
        rm -f -- "$tmp"
        mo_w_conflict "$src" "'$(basename -- "$dest")' already exists"; return 1
    fi
    _MO_W_TEMP=""
    return 0
}

export -f mo_worker_start mo_w_result mo_w_fail mo_w_conflict _mo_w_exit \
    mo_w_target_folder mo_w_claim mo_w_temp mo_w_commit
