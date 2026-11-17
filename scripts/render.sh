#!/usr/bin/env bash
# Render a board to PNG for review: scripts/render.sh <board> [front|back]
set -euo pipefail
. "$(dirname "$0")/_env.sh" "${1:-sensor-board}"
SIDE="${2:-front}"; OUT="$ROOT/fab/$BOARD"; mkdir -p "$OUT"
if [ "$SIDE" = back ]; then L="B.Cu,B.SilkS,Edge.Cuts"; M="--mirror"; else L="F.Cu,F.SilkS,F.Fab,Edge.Cuts"; M=""; fi
quiet "$KICAD_CLI" pcb export svg -o "$OUT/$SIDE.svg" --layers "$L" --mode-single --page-size-mode 2 --exclude-drawing-sheet $M "$PCB" >/dev/null
rsvg-convert -w "${3:-1500}" -b white "$OUT/$SIDE.svg" -o "$OUT/$SIDE.png"
echo "$OUT/$SIDE.png"
