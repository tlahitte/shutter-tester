# Shared settings. Usage: . scripts/_env.sh <board>   (board = sensor-board | light-board)
# Works when sourced from bash or zsh.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
KICAD_CLI="${KICAD_CLI:-/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli}"
KICAD_PY="${KICAD_PY:-/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3}"
BOARD="${1:-sensor-board}"
BDIR="$ROOT/hardware/$BOARD"
PROJECT="$(basename "$(ls "$BDIR"/*.kicad_pro 2>/dev/null | head -1)" .kicad_pro)"
SCH="$BDIR/$PROJECT.kicad_sch"
PCB="$BDIR/$PROJECT.kicad_pcb"
[ -x "$KICAD_CLI" ] || { echo "kicad-cli not found at $KICAD_CLI" >&2; return 1 2>/dev/null || exit 1; }
[ -f "$PCB" ] || { echo "no KiCad project in $BDIR" >&2; return 1 2>/dev/null || exit 1; }
quiet() { "$@" 2> >(grep -v -E '^Fontconfig|wxApp|stdpbase|Debug: Adding duplicate image handler' >&2); }
