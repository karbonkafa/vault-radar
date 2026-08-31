#!/usr/bin/env bash
# Render assets/cover.html into the repo's cover artwork.
#
#   cover.png  — 1280x640 still, used as the GitHub social preview
#   cover.gif  — looping animation for the README
#   cover.mp4  — same animation, for video/B-roll use
#
# Frames are captured with headless Chrome's virtual clock, so the output is
# deterministic: the same HTML always produces the same frames.
#
#   ./assets/render-cover.sh            # all three
#   ./assets/render-cover.sh png        # still only
#
# Requires: Google Chrome, ffmpeg.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/cover.html"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ -x "$CHROME" ] || CHROME="$(command -v google-chrome || command -v chromium || true)"
[ -n "$CHROME" ] && [ -x "$CHROME" ] || { echo "Chrome not found." >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg not found." >&2; exit 1; }
[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }

W=1280; H=640
LOOP_MS=6000     # animation loop length in cover.html
FRAMES=20
FPS=10
STILL_AT=2600    # ms into the loop that makes the best still frame

shot() { # shot <milliseconds> <output>
  # Each capture gets its own profile: parallel Chrome instances cannot share one.
  "$CHROME" --headless --disable-gpu --hide-scrollbars \
    --force-device-scale-factor=1 \
    --window-size=$W,$H \
    --virtual-time-budget="$1" \
    --screenshot="$2" \
    "file://$SRC" 2>/dev/null
}

want="${1:-all}"

echo "· still frame"
shot "$STILL_AT" "$HERE/cover.png"

if [ "$want" = "png" ]; then
  echo "done: assets/cover.png"
  exit 0
fi

# Sequential on purpose: parallel headless Chrome instances fight over the
# profile directory and hang. 20 frames over the 6s loop is plenty for a GIF.
echo "· capturing $FRAMES frames"
i=0
step=$(( LOOP_MS / FRAMES ))
for (( t=step/2; t<LOOP_MS; t+=step )); do
  shot "$t" "$(printf '%s/f%03d.png' "$WORK" "$i")"
  i=$((i+1))
  printf '\r  %d/%d' "$i" "$FRAMES"
done
echo

echo "· gif"
ffmpeg -y -loglevel error -framerate $FPS -i "$WORK/f%03d.png" \
  -vf "scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3" \
  -loop 0 "$HERE/cover.gif"

echo "· mp4"
ffmpeg -y -loglevel error -framerate $FPS -i "$WORK/f%03d.png" \
  -c:v libx264 -pix_fmt yuv420p -crf 18 -movflags +faststart \
  -vf "fps=$FPS,format=yuv420p" "$HERE/cover.mp4"

ls -lh "$HERE"/cover.{png,gif,mp4} | awk '{print "  " $9 "  " $5}'
