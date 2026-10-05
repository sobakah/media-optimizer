#!/bin/bash
# ==============================================================================
# tests/run-tests.sh  -  regression tests against real files
#
# Usage: tests/run-tests.sh [--keep] [filter]
#   filter   run only test groups whose name contains it, e.g. "img-" or
#            "sigterm". Groups: img-*, gif-*, avif-*, h264-*, orch, orch-restart
#   --keep   keep the work directory for inspection
#
# Everything happens in a fresh directory from mktemp. The tests never touch
# the real trash (gio and trash-put are stubbed to fail, deletions go through
# --force-delete), never write a log, and use an empty config file through
# MO_CONFIG, so media-optimizer.conf does not change the results. The
# orchestrator runs from a copy of the repository, so its state file lands
# there too.
#
# Failures are forced with stub commands placed first in PATH: a stub does
# its damage and then runs the real tool via PATH="${PATH#*:}".
#
# A full run takes several minutes; most of it is generating test images and
# the SIGTERM tests, which need work that is still running when the signal
# arrives. Exit code: 0 all passed, 1 failures, 2 missing tools.
# ==============================================================================
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
R="$(dirname "$TESTS_DIR")"

KEEP=false; FILTER=""
for a in "$@"; do
    case "$a" in
        --keep) KEEP=true ;;
        -h|--help) sed -n '3,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) FILTER="$a" ;;
    esac
done

missing=()
for c in magick exiftool cjxl djxl cwebp gif2webp ffmpeg ffprobe file python3 flock pgrep; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
done
if (( ${#missing[@]} > 0 )); then
    echo "Missing tools: ${missing[*]}" >&2
    exit 2
fi

S="$(mktemp -d "${TMPDIR:-/tmp}/mo-tests.XXXXXX")"
[[ -d "$S" && "$S" == */mo-tests.* ]] || { echo "Could not create a work directory." >&2; exit 2; }
cleanup() {
    chmod -R u+rwx "$S" 2>/dev/null
    if [[ "$KEEP" == true ]]; then echo "Work directory kept: $S"; else rm -rf "$S"; fi
}
trap cleanup EXIT
mkdir -p "$S/tmp" "$S/stub"
: > "$S/test.conf"
export MO_CONFIG="$S/test.conf"

PASS=0; FAILN=0; FAILED=()
ok()    { PASS=$((PASS+1)); printf '  \e[32mPASS\e[0m %s\n' "$1"; }
bad()   { FAILN=$((FAILN+1)); FAILED+=("$CUR: $1"); printf '  \e[31mFAIL\e[0m %s\n' "$1"; }
check() { local d="$1"; shift; if "$@"; then ok "$d"; else bad "$d"; fi; }
t()     { CUR="$1"; [[ -z "$FILTER" || "$1" == *$FILTER* ]] || return 1; echo "== $1"; return 0; }

# Trash always fails, so nothing reaches the real trash.
printf '#!/bin/sh\nexit 1\n' > "$S/stub/gio"
cp "$S/stub/gio" "$S/stub/trash-put"; chmod +x "$S/stub/"*

# run <script> args...  - output in $OUT, exit code in $RC. XPATH overrides PATH.
run() {
    local sc="$1"; shift
    OUT=$(PATH=${XPATH:-$S/stub:$PATH} NON_INTERACTIVE=true MO_HOLD=0 MO_LOG=false TMPDIR=$S/tmp \
          bash "$R/scripts/$sc" "$@" </dev/null 2>&1); RC=$?
}
fresh() { rm -rf "$1"; mkdir -p "$1"; }
# stub <tool>  - stub script from stdin in $S/fi, next to the trash stubs
stub() { rm -rf "$S/fi"; mkdir -p "$S/fi"; cp "$S/stub/"* "$S/fi/"; cat > "$S/fi/$1"; chmod +x "$S/fi/$1"; }
leftovers() { ! find "$1" \( -name '*.part.*' -o -name '*.molock' \) | grep -q .; }
# Encoder processes started by this test run (matched by the work directory
# in their arguments), so unrelated cjxl/ffmpeg on the machine do not count.
running()      { pgrep -af "^$1 " 2>/dev/null | grep -qF "$S"; }
wait_running() { local i; for i in $(seq 1 50); do running "$1" && return 0; sleep 0.2; done; return 1; }
no_strays()    { ! running "$1"; }
# vanishing_path <tool> <dir>  - a PATH with every tool from the current PATH,
# except <tool>, which works for startup checks and then deletes itself.
vanishing_path() {
    local tool="$1" dir="$2" d f
    rm -rf "$dir"; mkdir -p "$dir"
    IFS=: read -r -a _dirs <<< "$PATH"
    for d in "${_dirs[@]}"; do
        [[ -d "$d" ]] || continue
        for f in "$d"/*; do
            [[ -x "$f" && ! -e "$dir/${f##*/}" ]] && ln -s "$f" "$dir/" 2>/dev/null
        done
    done
    rm -f "$dir/gio" "$dir/trash-put" "$dir/$tool"
    cp "$S/stub/gio" "$S/stub/trash-put" "$dir/"
    printf '#!/bin/bash\nrm -f "$0"; exit 1\n' > "$dir/$tool"; chmod +x "$dir/$tool"
}

# ------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------
echo "Generating test files in $S ..."
mkdir -p "$S/fx" "$S/fx_extra" "$S/vfx"
(
    set -e
    cd "$S/fx"
    magick -size 96x64 plasma:fractal -quality 90 photo.jpg
    magick -size 64x64 plasma:fractal meta.png
    exiftool -q -overwrite_original -PNG:Artist=Max -PNG:Copyright=CC -EXIF:GPSLatitude=48.1 \
        -EXIF:GPSLatitudeRef=N -EXIF:DateTimeOriginal="2020:01:02 03:04:05" meta.png
    magick -size 64x64 plasma:fractal png:fake.jpg          # PNG content, .jpg name
    magick -size 16x16 xc:blue gif:isgif.png                # GIF content, .png name
    head -c 5000 /dev/urandom > garbage.png                 # not an image
    magick -size 64x64 plasma:fractal other.png
    echo "unrelated" > other.webp                           # must not block other.jxl

    cd "$S/fx_extra"
    magick -size 640x480 plasma:fractal -quality 88 exif.jpg
    exiftool -q -overwrite_original -Artist=Max -Copyright=CC -GPSLatitude=48.1 \
        -GPSLatitudeRef=N -DateTimeOriginal="2020:01:02 03:04:05" -XMP:Title=Hello exif.jpg
    magick -size 300x200 plasma:fractal -colorspace CMYK -quality 88 cmyk.jpg
    magick -size 1200x900 plasma:fractal -quality 88 big.jpg
    head -c 30000 big.jpg > truncated.jpg; rm big.jpg

    cd "$S/vfx"
    ffmpeg -v error -y -f lavfi -i testsrc2=size=320x240:rate=25 -t 3 -c:v libx264 -pix_fmt yuv420p clip.mp4
    ffmpeg -v error -y -f lavfi -i testsrc2=size=320x240:rate=25 -f lavfi -i sine=f=440 -t 3 \
        -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest withaudio.mp4
    ffmpeg -v error -y -f lavfi -i testsrc2=size=640x360:rate=25 -t 8 -c:v libx264 -b:v 3M -pix_fmt yuv420p long.mp4
    ffmpeg -v error -y -f lavfi -i testsrc2=size=1280x720:rate=25 -t 40 -c:v libx264 -b:v 4M -pix_fmt yuv420p slow.mp4
    magick -delay 10 -size 160x120 plasma:red-blue plasma:green-yellow plasma:fractal plasma:blue-white -loop 0 anim.gif
) || { echo "Generating the test files failed." >&2; exit 2; }

# ------------------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------------------
if t img-inplace; then
  D=$S/r1; rm -rf $D; cp -a $S/fx $D
  run img-to-jxl.sh -i $D --delete --force-delete --png-mode lossless -j 4
  check "exit 3 (garbage.png fails)" [ $RC = 3 ]
  for f in fake meta other; do djxl $D/$f.jxl $S/tmp/dec.png --quiet; src=$S/fx/$f.png; [[ $f == fake ]] && src=$S/fx/fake.jpg
    check "$f.jxl pixel-identical" [ "$(magick compare -metric AE $src $S/tmp/dec.png null: 2>&1 | cut -d' ' -f1)" = 0 ]; done
  djxl $D/photo.jxl $S/tmp/rec.jpg --quiet; check "photo.jxl bit-exact JPEG" cmp -s $S/tmp/rec.jpg $S/fx/photo.jpg
  check "meta.jxl has Artist+GPS" bash -c "exiftool -s -s -s -Artist -GPSLatitude $D/meta.jxl | grep -q Max && exiftool -s -s -s -GPSLatitude $D/meta.jxl | grep -q 48"
  check "originals of successes gone" bash -c "! ls $D | grep -qE '^(photo.jpg|fake.jpg|meta.png|other.png)$'"
  check "garbage.png kept" [ -f $D/garbage.png ]
  check "isgif.png renamed to .gif" [ -f $D/isgif.gif ]
  check "other.webp did not block other.jxl" [ -f $D/other.jxl ]
  check "no leftovers" leftovers $D
fi

if t img-target; then
  D=$S/r2; O=$S/r2out; rm -rf $D $O; cp -a $S/fx $D
  before=$(cd $D && find . -printf '%p %s %T@\n' | sort; find . -type f -exec sha1sum {} + | sort)
  run img-to-jxl.sh -i $D -o $O --png-mode lossless
  after=$(cd $D && find . -printf '%p %s %T@\n' | sort; find . -type f -exec sha1sum {} + | sort)
  check "source byte-identical incl. names+mtimes" [ "$before" = "$after" ]
  check "no state files in target leftover/ok" bash -c "! ls -A $O | grep -q molock"
  check "outputs present" bash -c "ls $O | grep -q photo.jxl && ls $O | grep -q fake.jxl && ls $O | grep -q isgif.gif"
fi

if t img-jpegmeta; then
  D=$S/r3; fresh $D; cp $S/fx_extra/exif.jpg $D/e.jpg
  run img-to-jxl.sh -i $D -o $D/out
  djxl $D/out/e.jxl $S/tmp/rec.jpg --quiet
  check "JPEG->JXL bit-exact with uncompressed boxes" cmp -s $S/tmp/rec.jpg $D/e.jpg
  check "JPEG metadata readable (Artist, GPS)" bash -c "exiftool -s -s -s -Artist $D/out/e.jxl | grep -q Max"
fi

if t img-cmyk; then
  D=$S/r4; fresh $D; cp $S/fx_extra/cmyk.jpg $D/c.jpg; cp $S/fx_extra/truncated.jpg $D/t.jpg
  run img-to-jxl.sh -i $D --delete --force-delete
  check "CMYK reported as unsupported" grep -q "UNSUPPORTED.*c.jpg.*CMYK" <<< "$OUT"
  check "truncated JPEG is a failure" grep -q "ERROR.*damaged.*t.jpg" <<< "$OUT"
  check "exit 3 because of truncated" [ $RC = 3 ]
  check "both originals kept" bash -c "[ -f $D/c.jpg ] && [ -f $D/t.jpg ]"
  fresh $D; cp $S/fx_extra/cmyk.jpg $D/c.jpg; run img-to-jxl.sh -i $D
  check "CMYK alone: exit 0" [ $RC = 0 ]
fi

if t img-faults; then
  for case in race mvn mvfail exif stat; do
    D=$S/r5; fresh $D; cp $S/fx/photo.jpg $S/fx/meta.png $D/
    case $case in
      race) stub file <<'X'
#!/bin/bash
src="${@: -1}"; [[ "$src" == *.jpg || "$src" == *.png ]] && echo "FROM-OTHER-WORKER" > "${src%.*}.jxl"; PATH="${PATH#*:}" exec file "$@"
X
;;
      mvn) stub touch <<'X'
#!/bin/bash
for a; do case "$a" in *.part.*) d="${a%%.part.*}"; echo "FROM-OTHER-WORKER" > "$d";; esac; done; PATH="${PATH#*:}" exec touch "$@"
X
;;
      mvfail) stub mv <<'X'
#!/bin/bash
for a; do [[ "$a" == *.part.* ]] && exit 1; done; PATH="${PATH#*:}" exec mv "$@"
X
;;
      exif) stub exiftool <<'X'
#!/bin/bash
[[ "$*" == *tagsFromFile* ]] && exit 1; PATH="${PATH#*:}" exec exiftool "$@"
X
;;
      stat) stub stat <<'X'
#!/bin/bash
[[ "$*" == *photo.jpg* ]] && exit 1; PATH="${PATH#*:}" exec stat "$@"
X
;;
    esac
    XPATH=$S/fi:$PATH run img-to-jxl.sh -i $D --delete --force-delete --png-mode lossless -j 2
    case $case in
      race|mvn) check "$case: both originals kept" bash -c "[ -f $D/photo.jpg ] && [ -f $D/meta.png ]"
                check "$case: foreign output untouched" grep -q FROM-OTHER-WORKER $D/photo.jxl ;;
      mvfail)   check "$case: originals kept, exit 3" bash -c "[ -f $D/photo.jpg ] && [ -f $D/meta.png ] && [ $RC = 3 ]" ;;
      exif)     check "$case: meta.png kept, no meta.jxl, exit 3" bash -c "[ -f $D/meta.png ] && [ ! -e $D/meta.jxl ] && [ $RC = 3 ]" ;;
      stat)     check "$case: photo.jpg kept + reported" bash -c "[ -f $D/photo.jpg ] && grep -q 'unexpected error' <<< \"\$0\"" "$OUT" ;;
    esac
    check "$case: no leftovers" leftovers $D
  done
fi

if t img-validate; then
  D=$S/r6; fresh $D; cp $S/fx/photo.jpg $D/
  run img-to-jxl.sh -i $D --copy-metadata false;  check "--copy-metadata false -> 2" [ $RC = 2 ]
  run img-to-jxl.sh -i $D -j 0;                   check "-j 0 -> 2" [ $RC = 2 ]
  run img-to-jxl.sh -i $D -e 11;                  check "-e 11 -> 2" [ $RC = 2 ]
  run img-to-jxl.sh -i $D -e 10 -n;               check "-e 10 accepted" [ $RC = 0 ]
  run img-to-jxl.sh -i $D --png-quality abc;      check "--png-quality abc -> 2" [ $RC = 2 ]
  run img-to-jxl.sh -i $D --output;               check "missing value -> 2 + message" bash -c "[ $RC = 2 ] && grep -q 'needs a value' <<< \"\$0\"" "$OUT"
  DELETE_ORIGINAL=yes run img-to-jxl.sh -i $D;    check "DELETE_ORIGINAL=yes -> 2" [ $RC = 2 ]
  check "source untouched by rejected runs" bash -c "ls $D | grep -qx photo.jpg && [ \$(ls -A $D | wc -l) = 1 ]"
fi

if t img-hidden; then
  D=$S/r7; fresh $D; rm -rf $D-out; mkdir -p $D/.hid $D/.Trash-1000 $D/@eaDir $D/sub
  for p in a.jpg .hid/b.jpg .Trash-1000/c.jpg @eaDir/d.jpg ._e.jpg .f.jpg sub/g.jpg; do cp $S/fx/photo.jpg $D/$p; done
  run img-to-jxl.sh -i $D -o $D-out
  check "default: only a.jpg + sub/g.jpg converted" bash -c "[ \"\$(cd $D-out && find . -name '*.jxl' | LC_ALL=C sort | tr '\n' ' ')\" = './a.jxl ./sub/g.jxl ' ]"
  rm -rf $D-out; INCLUDE_HIDDEN=true run img-to-jxl.sh -i $D -o $D-out
  check "INCLUDE_HIDDEN: hidden yes, trash/NAS/AppleDouble no" bash -c "[ \"\$(cd $D-out && find . -name '*.jxl' | LC_ALL=C sort | tr '\n' ' ')\" = './.f.jxl ./.hid/b.jxl ./a.jxl ./sub/g.jxl ' ]"
  if (( EUID != 0 )); then   # root can read a mode-000 folder, the test would mean nothing
    rm -rf $D-out; mkdir -p $D/locked; cp $S/fx/photo.jpg $D/locked/x.jpg; chmod 000 $D/locked
    run img-to-jxl.sh -i $D -o $D-out; chmod 755 $D/locked
    check "unreadable subfolder: exit 0, rest converted" bash -c "[ $RC = 0 ] && [ -f $D-out/a.jxl ]"
  fi
  rm -rf $D-out
fi

if t img-dryrun; then
  D=$S/r8; fresh $D; cp $S/fx/photo.jpg $S/fx/meta.png $D/; touch $D/x.jxl.part.1.2.jxl; mkdir $D/.x.molock; echo s > $D/.img_stats.env
  : > $S/tmp/dry.log; before=$(cd $D && find . | sort; sha1sum $S/tmp/dry.log)
  OUT=$(PATH=$S/stub:$PATH NON_INTERACTIVE=true MO_HOLD=0 MO_LOG_FILE=$S/tmp/dry.log bash $R/scripts/img-to-jxl.sh -i $D -n </dev/null 2>&1)
  after=$(cd $D && find . | sort; sha1sum $S/tmp/dry.log)
  check "dry run changes nothing (tree + log)" [ "$before" = "$after" ]
  run img-to-jxl.sh -i $D -n --target webp
  check "dry run preview names .webp for --target webp" grep -q "photo.jpg' -> 'photo.webp'" <<< "$OUT"
fi

if t img-busy; then
  D=$S/r9; fresh $D; cp $S/fx/photo.jpg $D/
  ( exec 8<$D; flock 8; sleep 2 ) & sleep 0.3
  run img-to-jxl.sh -i $D; wait
  check "concurrent run refused with BUSY" bash -c "[ $RC = 1 ] && grep -q BUSY <<< \"\$0\"" "$OUT"
  fresh $D; mkdir -p $D/sub $D-out; cp $S/fx/photo.jpg $D/sub/; echo f > $D-out/sub
  run img-to-jxl.sh -i $D -o $D-out
  check "mkdir failure reported as such" grep -q "cannot create target folder" <<< "$OUT"; rm -rf $D-out
fi

if t img-encoder; then
  vanishing_path cjxl $S/nocjxl
  D=$S/r10; fresh $D; for n in a b c; do cp $S/fx/photo.jpg $D/$n.jpg; done
  XPATH=$S/nocjxl run img-to-jxl.sh -i $D -j 1 --delete
  check "encoder vanished: exit 255, nothing deleted" bash -c "[ $RC = 255 ] && [ \$(ls $D | wc -l) = 3 ]"
fi

if t img-sigterm; then
  D=$S/r11; fresh $D; for n in $(seq 1 40); do magick -size 1200x900 plasma:fractal $D/p$n.png; done
  PATH=$S/stub:$PATH NON_INTERACTIVE=true MO_HOLD=0 MO_LOG=false TMPDIR=$S/tmp \
    bash $R/scripts/img-to-jxl.sh -i $D -j 2 -e 9 --png-mode lossless </dev/null >$S/tmp/term.out 2>&1 & P=$!
  sleep 2; t0=$(date +%s%N); kill -TERM $P; wait $P; RC=$?; ms=$(( ($(date +%s%N)-t0)/1000000 ))
  check "SIGTERM: exit 130" [ $RC = 130 ]
  check "SIGTERM: stopped within 3 s (took ${ms} ms)" [ $ms -lt 3000 ]
  check "SIGTERM: stats saved for resume" [ -f $D/.img_stats.env ]
  check "SIGTERM: no leftovers" leftovers $D
  sleep 0.3; check "SIGTERM: no cjxl still running" no_strays cjxl
fi

if t img-prompt; then
  D=$S/r12; fresh $D; cp $S/fx/photo.jpg $D/
  OUT=$(PATH=$S/stub:$PATH MO_HOLD=0 MO_LOG=false python3 $TESTS_DIR/lib/pty_run.py "" bash $R/scripts/img-to-jxl.sh -i $D -o $D-out 2>&1)
  check "no settings menu when -i given" bash -c "! grep -q 'Run with the default settings' <<< \"\$0\"" "$OUT"
  rm -rf $D-out; fresh $D; cp $S/fx/photo.jpg $D/
  OUT=$(PATH=$S/stub:$PATH MO_HOLD=0 MO_LOG=false python3 $TESTS_DIR/lib/pty_run.py "" bash $R/scripts/img-to-jxl.sh -i $D --no-delete 2>&1)
  check "in place + explicit --no-delete: no 'Keep these settings' question" bash -c "! grep -q 'Keep these settings' <<< \"\$0\"" "$OUT"
  check "in place banner does not mention _h265" bash -c "! grep -q '_h265' <<< \"\$0\"" "$OUT"
  fresh $D; cp $S/fx/photo.jpg $D/
  OUT=$(cd $D && PATH=$S/stub:$PATH MO_HOLD=0 MO_LOG=false SOURCE_DIR=$D python3 $TESTS_DIR/lib/pty_run.py "n|||n||||" bash $R/scripts/img-to-jxl.sh 2>&1)
  check "interactive 'n' to delete is honoured" bash -c "[ -f $D/photo.jpg ] && [ -f $D/photo.jxl ]"
fi


if t gif-basic; then
  D=$S/g1; fresh $D; rm -rf $D-out; cp $S/vfx/anim.gif $D/a.gif; cp $S/vfx/anim.gif $D/b.gif; head -c 3000 /dev/urandom > $D/broken.gif
  mkdir -p $D/.hid; cp $S/vfx/anim.gif $D/.hid/h.gif
  before=$(cd $D && find . -printf '%p %s %T@\n' | sort)
  run gif-to-webp.sh -i $D -o $D-out
  after=$(cd $D && find . -printf '%p %s %T@\n' | sort)
  check "gif target mode: source unchanged" [ "$before" = "$after" ]
  check "gif: a.webp + b.webp, hidden skipped" bash -c "[ -f $D-out/a.webp ] && [ -f $D-out/b.webp ] && [ ! -e $D-out/.hid ]"
  check "gif: broken.gif fails -> exit 3" [ $RC = 3 ]
  check "gif: stats/pending files not in source" bash -c "! ls -A $D | grep -q '^\.gif_'"
  rm -rf $D-out
  run gif-to-webp.sh -i $D -m 9;  check "gif: -m 9 -> 2" [ $RC = 2 ]
fi

if t gif-faults; then
  for case in race mvfail; do
    D=$S/g2; fresh $D; cp $S/vfx/anim.gif $D/a.gif
    case $case in
      race) stub stat <<'X'
#!/bin/bash
for a; do [[ "$a" == *.gif ]] && echo "FROM-OTHER-WORKER" > "${a%.gif}.webp"; done; PATH="${PATH#*:}" exec stat "$@"
X
;;
      mvfail) stub mv <<'X'
#!/bin/bash
for a; do [[ "$a" == *.part.* ]] && exit 1; done; PATH="${PATH#*:}" exec mv "$@"
X
;;
    esac
    XPATH=$S/fi:$PATH run gif-to-webp.sh -i $D --delete --force-delete
    check "gif $case: original kept" [ -f $D/a.gif ]
    [[ $case == race ]] && check "gif race: foreign output untouched" grep -q FROM-OTHER-WORKER $D/a.webp
    [[ $case == mvfail ]] && check "gif mvfail: exit 3" [ $RC = 3 ]
    check "gif $case: no leftovers" leftovers $D
  done
  vanishing_path gif2webp $S/nog2w
  D=$S/g3; fresh $D; for n in a b c; do cp $S/vfx/anim.gif $D/$n.gif; done
  XPATH=$S/nog2w run gif-to-webp.sh -i $D -j 1 --delete
  check "gif encoder vanished: exit 255 (was overwritten by rc=1 before)" [ $RC = 255 ]
fi

if t avif-basic; then
  D=$S/a1; fresh $D; rm -rf $D-out; cp $S/vfx/clip.mp4 $D/c.mp4; cp $S/vfx/withaudio.mp4 $D/w.mp4; cp $S/vfx/long.mp4 $D/l.mp4
  before=$(cd $D && find . -printf '%p %s %T@\n' | sort)
  run video-to-avif.sh -i $D -o $D-out --max-seconds 5
  after=$(cd $D && find . -printf '%p %s %T@\n' | sort)
  check "avif: c.avif made, audio clip + long clip skipped" bash -c "[ -f $D-out/c.avif ] && [ ! -e $D-out/w.avif ] && [ ! -e $D-out/l.avif ]"
  check "avif target mode: source unchanged" [ "$before" = "$after" ]
  check "avif: exit 0" [ $RC = 0 ]
  # A lock held by another worker: created between the unlocked check and the
  # claim (a stale lock from before the start would be cleaned up, correctly).
  rm -rf $D-out; fresh $D; cp $S/vfx/clip.mp4 $D/c.mp4
  stub stat <<'X'
#!/bin/bash
for a; do [[ "$a" == *.mp4 ]] && mkdir -p "${a%/*}/.c.molock"; done; PATH="${PATH#*:}" exec stat "$@"
X
  XPATH=$S/fi:$PATH run video-to-avif.sh -i $D --no-delete
  check "avif: claimed name is a reported conflict (was a silent skip)" grep -q 'CONFLICT.*c.mp4' <<< "$OUT"
  run video-to-avif.sh -i $D --crf 99;  check "avif: --crf 99 -> 2" [ $RC = 2 ]
fi

if t h264-basic; then
  D=$S/h1; fresh $D; rm -rf $D-out; cp $S/vfx/long.mp4 $D/l.mp4
  before=$(cd $D && find . -printf '%p %s %T@\n' | sort)
  run h264-to-h265.sh -i $D -o $D-out --encoder cpu --min-size 0 --no-probe --preset ultrafast
  after=$(cd $D && find . -printf '%p %s %T@\n' | sort)
  check "h264: output made, exit 0" bash -c "ls $D-out | grep -q '^l\.' && [ $RC = 0 ]"
  check "h264 target mode: source unchanged (no cache/stats there)" [ "$before" = "$after" ]
  check "h264: cache now in target dir" [ -f $D-out/.video_conversion_cache.txt ]
  run verify-output.sh -i $D -o $D-out --no-visual
  check "verify-output: 1 file checked, all fine" bash -c "grep -q 'checking 1 file' <<< \"\$0\" && [ $RC = 0 ]" "$OUT"
  rm -rf $D-out
  run h264-to-h265.sh -i $D --crf abc;  check "h264: --crf abc -> 2" [ $RC = 2 ]
  run h264-to-h265.sh -i $D --encoder GPU;  check "h264: --encoder GPU -> 2" [ $RC = 2 ]
fi

if t h264-partial; then
  D=$S/h2; fresh $D; rm -rf $D-out; cp $S/vfx/long.mp4 $D/l.mp4; cp $S/vfx/long.mp4 $D/c.mp4
  stub ffmpeg <<'X'
#!/bin/bash
case "$*" in *"/l.mp4"*libx265*) echo "simulated encoder failure" >&2; exit 1 ;; esac; PATH="${PATH#*:}" exec ffmpeg "$@"
X
  XPATH=$S/fi:$PATH run h264-to-h265.sh -i $D -o $D-out --encoder cpu --min-size 0 --no-probe --preset ultrafast
  check "h264: one failed encode -> exit 3 (was 0)" [ $RC = 3 ]
  check "h264: the other file was still converted" bash -c "ls $D-out | grep -q '^c\.'"
  rm -rf $D-out
fi

if t h264-sigterm; then
  D=$S/h3; fresh $D; rm -rf $D-out; cp $S/vfx/slow.mp4 $D/s.mp4
  PATH=$S/stub:$PATH NON_INTERACTIVE=true MO_HOLD=0 MO_LOG=false TMPDIR=$S/tmp \
    bash $R/scripts/h264-to-h265.sh -i $D -o $D-out --encoder cpu --min-size 0 --no-probe --preset slow </dev/null >$S/tmp/h264term.out 2>&1 & P=$!
  wait_running ffmpeg; sleep 1
  t0=$(date +%s%N); kill -TERM $P; wait $P; RC=$?; ms=$(( ($(date +%s%N)-t0)/1000000 ))
  check "h264 SIGTERM during encode: exit 130" [ $RC = 130 ]
  check "h264 SIGTERM: stopped within 5 s (took ${ms} ms)" [ $ms -lt 5000 ]
  sleep 0.3; check "h264 SIGTERM: no ffmpeg left" no_strays ffmpeg
  check "h264 SIGTERM: no .part leftovers" leftovers $D-out
  check "h264 SIGTERM: source kept" [ -f $D/s.mp4 ]
  rm -rf $D-out
fi

if t orch; then
  D=$S/o1; fresh $D; rm -rf $D-out; cp $S/fx/photo.jpg $S/fx/garbage.png $D/; cp $S/vfx/anim.gif $D/
  rm -rf $S/repo; mkdir $S/repo; (cd $R && tar -c --exclude=.git . ) | tar -x -C $S/repo
  OUT=$(PATH=$S/stub:$PATH MO_HOLD=0 MO_LOG=false TMPDIR=$S/tmp bash $S/repo/media-optimizer.sh -y -i $D -o $D-out </dev/null 2>&1); RC=$?
  check "orchestrator: image failure reported, GIF stage still ran, exit 3" bash -c "grep -q 'WITH FAILURES' <<< \"\$0\" && [ -f $D-out/anim.webp ] && [ $RC = 3 ]" "$OUT"
  rm -rf $D-out; fresh $D; for n in $(seq 1 30); do magick -size 1000x800 plasma:fractal $D/p$n.png; done
  JXL_EFFORT=9 PNG_MODE=lossless MAX_WORKERS=2 PATH=$S/stub:$PATH MO_HOLD=0 MO_LOG=false TMPDIR=$S/tmp bash $S/repo/media-optimizer.sh -y -i $D -o $D-out </dev/null >$S/tmp/orch.out 2>&1 & P=$!
  wait_running cjxl; sleep 1
  t0=$(date +%s%N); kill -TERM $P; wait $P; RC=$?; ms=$(( ($(date +%s%N)-t0)/1000000 ))
  check "orchestrator SIGTERM: exit 130 within 5 s (took ${ms} ms)" bash -c "[ $RC = 130 ] && [ $ms -lt 5000 ]"
  sleep 0.3; check "orchestrator SIGTERM: no cjxl left" no_strays cjxl
  check "orchestrator SIGTERM: state saved for resume" [ -f $S/repo/.media_optimizer_state.env ]
  check "orchestrator SIGTERM: stage counters saved in target" [ -f $D-out/.img_stats.env ]
  check "orchestrator SIGTERM: no leftovers" leftovers $D-out
  rm -rf $D-out
fi

if t orch-restart; then
  # The orchestrator restarts itself ("Process another directory?", and after
  # discarding a stopped run). Started as "bash media-optimizer.sh", $0 is a
  # bare name and "exec $0" failed with "not found" (exit 127).
  D=$S/rs; rm -rf $D; mkdir -p $D/a $D/b $D/repo
  (cd $R && tar -c --exclude=.git --exclude=tests . ) | tar -x -C $D/repo
  rm -f $D/repo/.media_optimizer_state.env
  cp $S/fx/photo.jpg $D/a/; cp $S/fx/photo.jpg $D/b/
  orch_pty() { # orch_pty "<answers>" args...  - runs from inside the repo copy
    local answers="$1"; shift
    OUT=$(cd $D/repo && PATH=$S/stub:$PATH MO_LOG=false MO_HOLD=0 TMPDIR=$S/tmp PTY_IDLE=8 \
          python3 $TESTS_DIR/lib/pty_run.py "$answers" bash media-optimizer.sh "$@" 2>&1 | tr -d '\r')
  }

  orch_pty "y|$D/b|$D/b-out||n" -i $D/a -o $D/a-out
  check "restart after 'another directory' (started as 'bash media-optimizer.sh')" \
    bash -c "grep -q 'New run' <<< \"\$0\" && [ -f $D/b-out/photo.jxl ]" "$OUT"
  check "restart: no 'not found' from exec" bash -c "! grep -qiE 'exec:.*(not found|nicht gefunden)' <<< \"\$0\"" "$OUT"

  rm -rf $D/a-out $D/b-out; : > $S/tmp/restart.log
  OUT=$(cd $D/repo && PATH=$S/stub:$PATH MO_HOLD=0 TMPDIR=$S/tmp PTY_IDLE=8 \
        python3 $TESTS_DIR/lib/pty_run.py "y|$D/b|$D/b-out||n" bash media-optimizer.sh \
        --log $S/tmp/restart.log -i $D/a -o $D/a-out 2>&1 | tr -d '\r')
  check "restart keeps the value of --log" bash -c "! grep -q 'needs a value' <<< \"\$0\" && [ -f $D/b-out/photo.jxl ] && [ \$(grep -c '^RUN .* started' $S/tmp/restart.log) = 2 ]" "$OUT"

  rm -rf $D/a-out $D/b-out
  { printf 'STATUS=%q\nINPUT_DIR=%q\nOUTPUT_DIR=%q\n' INTERRUPTED "$D/a" "$D/a-out"
    printf 'STAGE_1_DONE=false\nSTAGE_2_DONE=true\nSTAGE_3_DONE=true\nSTAGE_AVIF_DONE=true\n'; } > $D/repo/.media_optimizer_state.env
  orch_pty "n|$D/b|$D/b-out||n"
  check "discarding a stopped run restarts cleanly" \
    bash -c "grep -q 'Discarding state' <<< \"\$0\" && [ -f $D/b-out/photo.jxl ] && [ ! -e $D/a-out/photo.jxl ]" "$OUT"
  rm -rf $D
fi

echo
echo "PASSED: $PASS   FAILED: $FAILN"
for f in "${FAILED[@]}"; do echo "  - $f"; done
(( FAILN == 0 ))
