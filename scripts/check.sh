#!/usr/bin/env bash
# ERC on the schematic, DRC (with schematic parity) on the board.
# Reports list errors and warnings; the exit code is non-zero only for
# error-severity violations, unconnected items or schematic parity issues.
set -uo pipefail
. "$(dirname "$0")/_env.sh" "${1:-sensor-board}"
OUT="$ROOT/fab/$BOARD/reports"; mkdir -p "$OUT"
quiet "$KICAD_CLI" sch erc --severity-error --severity-warning \
  --format report -o "$OUT/erc.rpt" "$SCH" >/dev/null || { echo "ERC failed to run" >&2; exit 2; }
quiet "$KICAD_CLI" pcb drc --severity-error --severity-warning --schematic-parity \
  --format report -o "$OUT/drc.rpt" "$PCB" >/dev/null || { echo "DRC failed to run" >&2; exit 2; }

count() { grep -c "$1" "$2" || true; }
erc_err=$(count '; error' "$OUT/erc.rpt"); erc_warn=$(count '; warning' "$OUT/erc.rpt")
drc_err=$(count '; error' "$OUT/drc.rpt"); drc_warn=$(count '; warning' "$OUT/drc.rpt")
unconn=$(sed -n 's/^\*\* Found \([0-9]*\) unconnected pads.*/\1/p' "$OUT/drc.rpt")
echo "ERC: $erc_err errors, $erc_warn warnings"
echo "DRC: $drc_err errors, $drc_warn warnings, ${unconn:-?} unconnected"
for f in erc drc; do
  grep '^\[' "$OUT/$f.rpt" | sed 's/\]:.*/]/' | sort | uniq -c | sed "s/^/  $f /"
done
# Not a DRC rule, so reported here as a warning only (see scripts/viacheck.py)
quiet "$KICAD_PY" "$ROOT/scripts/viacheck.py" "$PCB" || echo "WARNING: via holes on SMD pads, run scripts/viacheck.py -v for the list" >&2
echo "Reports in $OUT"
[ "$erc_err" = 0 ] && [ "$drc_err" = 0 ] && [ "${unconn:-1}" = 0 ]
