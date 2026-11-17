#!/usr/bin/env bash
# Build the JLCPCB upload package for one board: gerbers.zip, bom.csv, cpl.csv
# Usage: scripts/fab.sh <sensor-board|light-board> [--force]   (--force skips the ERC/DRC gate)
set -euo pipefail
. "$(dirname "$0")/_env.sh" "${1:-sensor-board}"
if [ "${2:-}" != "--force" ]; then
  "$ROOT/scripts/check.sh" "$BOARD" || { echo "ERC/DRC not clean. Fix, or rerun with --force." >&2; exit 1; }
fi
OUT="$ROOT/fab/$BOARD/jlcpcb"; GERB="$OUT/gerbers"
mkdir -p "$GERB"
find "$GERB" -type f \( -name '*.g*' -o -name '*.drl' \) -exec rm {} +

# Protel extensions (.gtl, .gbl, .gm1 ...) as in JLCPCB's KiCad instructions
quiet "$KICAD_CLI" pcb export gerbers -o "$GERB/" --subtract-soldermask --check-zones \
  --layers "F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts" "$PCB" >/dev/null
quiet "$KICAD_CLI" pcb export drill -o "$GERB/" --format excellon --excellon-units mm --excellon-separate-th "$PCB" >/dev/null
(cd "$GERB" && rm -f ../gerbers.zip && zip -q ../gerbers.zip ./*)

quiet "$KICAD_CLI" sch export bom -o "$OUT/bom.csv" \
  --fields 'Value,Reference,Footprint,LCSC,MPN' --labels 'Comment,Designator,Footprint,LCSC Part #,MPN' \
  --group-by 'Value,Footprint,LCSC' --ref-range-delimiter '' --exclude-dnp "$SCH" >/dev/null
quiet "$KICAD_CLI" pcb export pos -o "$OUT/cpl.raw.csv" --format csv --units mm --side both --exclude-dnp "$PCB" >/dev/null

# JLCPCB places each part as its own library footprint (the EasyEDA one) is drawn, turned
# counter-clockwise by the CPL rotation about that footprint's origin. Where that footprint is
# drawn differently from KiCad's, the CPL row needs a correction. Each entry below was worked out
# from the EasyEDA footprint of the exact LCSC part on the BOM; scripts/cplcheck.py re-checks
# every placement against those footprints further down.
#   pattern on the KiCad footprint name, rotation to add, origin shift (dx, dy) in the
#   KiCad footprint's own frame (mm, y down)
python3 -I - "$OUT/cpl.raw.csv" "$OUT/cpl.csv" "$OUT/bom.csv" <<'PY'
import csv, math, re, sys
src, dst, bom = sys.argv[1:4]
FIX = [(r'^SOT-223', 180, 0, 0),
       (r'^SOIC-', 270, 0, 0),
       (r'^LED_PLCC_2835', 180, 0, 0),                              # JLC draws the cathode pad on the right
       (r'^USB_C_Receptacle_HRO_TYPE-C-31-M-12', 0, 0, -1.394)]     # JLC origin is 1.394 mm nearer the pads
rows = list(csv.DictReader(open(src, newline="")))
with open(dst, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
    for r in rows:
        rot, x, y = float(r["Rot"]), float(r["PosX"]), float(r["PosY"])     # PosY is already y-up
        for pat, off, dx, dy in FIX:
            if re.search(pat, r["Package"]):
                th = math.radians(rot)
                x += dx * math.cos(th) + dy * math.sin(th)
                y -= -dx * math.sin(th) + dy * math.cos(th)
                rot += off
        rot %= 360          # KiCad writes -90 for 270; keep every row in 0..360
        w.writerow([r["Ref"], f"{x:.4f}mm", f"{y:.4f}mm", r["Side"].capitalize(), f"{rot:g}"])
b = list(csv.DictReader(open(bom, newline="")))
missing = [r["Designator"] for r in b if not r.get("LCSC Part #", "").strip()]
parts = sum(len(r["Designator"].split(",")) for r in b)
print(f"{len(rows)} placements, {len(b)} BOM lines, {parts} parts")
bad = False
if parts != len(rows): print("ERROR: BOM part count and placement count differ"); bad = True
if missing: print("ERROR: no LCSC part number for:", ", ".join(missing)); bad = True
if any(r["Side"].lower() != "top" for r in rows): print("ERROR: parts on the bottom side (order is top side only)"); bad = True
sys.exit(1 if bad else 0)
PY
rm "$OUT/cpl.raw.csv"

# Overlay JLCPCB's footprints on the board at the CPL positions. Needs network the first time.
rc=0; quiet "$KICAD_PY" "$ROOT/scripts/cplcheck.py" "$PCB" "$OUT/bom.csv" "$OUT/cpl.csv" || rc=$?
if [ "$rc" = 2 ]; then echo "WARNING: some placements could not be checked (offline?). Rerun before ordering." >&2
elif [ "$rc" != 0 ]; then echo "Placement check failed. Do not order from this package." >&2; exit 1; fi
echo "Package ready in $OUT"; ls -1 "$OUT" | grep -v '^gerbers$'
