#!/usr/bin/env bash
# Ebook Prep launcher.
#
#   ebook-prep.sh on              build if needed, start, print the address (default)
#   ebook-prep.sh off             stop the container
#   ebook-prep.sh logs            follow the container's output
#   ebook-prep.sh update          rebuild from scratch (newer OCR and word lists)
#   ebook-prep.sh cli FILE [opts] run the converter on one file without the UI;
#                                 writes FILE's name .tts.txt next to it.
#                                 opts: --sentences  --keep-furniture  -o NAME
#
# Folders: BOOKS_DIR (default ~/audiobooks/books) and TEXT_DIR (default ~/audiobooks/text).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

export UID_GID="$(id -u):$(id -g)"
export BOOKS_DIR="${BOOKS_DIR:-$HOME/audiobooks/books}"
export TEXT_DIR="${TEXT_DIR:-$HOME/audiobooks/text}"
pretty() { case "$1" in "$HOME"/*) printf '~%s' "${1#"$HOME"}" ;; *) printf '%s' "$1" ;; esac; }
export BOOKS_HOST="$(pretty "$BOOKS_DIR")" TEXT_HOST="$(pretty "$TEXT_DIR")"
mkdir -p "$BOOKS_DIR" "$TEXT_DIR"     # create them as you, so Docker doesn't make them root-owned

case "${1:-on}" in
  on)
    docker compose up -d --build
    echo "Ebook Prep is running: http://localhost:7880"
    ;;
  off)
    docker compose down
    ;;
  logs)
    docker compose logs -f
    ;;
  update)
    docker compose build --pull --no-cache
    docker compose up -d
    echo "Updated. Ebook Prep is running: http://localhost:7880"
    ;;
  cli)
    shift
    [ $# -ge 1 ] && [ -f "$1" ] || { echo "Usage: $(basename "$0") cli FILE [--sentences] [--keep-furniture]"; exit 1; }
    f="$(readlink -f "$1")"; shift
    docker image inspect ebook-prep >/dev/null 2>&1 || docker compose build
    docker run --rm -u "$UID_GID" -v "$(dirname "$f")":/work -w /work ebook-prep \
      python3 /app/prep_text.py "$(basename "$f")" "$@"
    ;;
  *)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
