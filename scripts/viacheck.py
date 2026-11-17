#!/usr/bin/env python3
"""List vias whose hole sits in or right against an SMD pad. DRC does not report these, but
an open via there pulls solder paste down the hole during reflow and can starve the joint.

Run with KiCad's Python:  $KICAD_PY scripts/viacheck.py <board.kicad_pcb> [-v]
Prints a count, and with -v every via. Exit code 1 if any via hole overlaps a pad.
"""
import math, sys

GAP = 0.1      # mm of solder mask wanted between the via hole and the pad


def main(board, verbose):
    import pcbnew
    mm = pcbnew.ToMM
    b = pcbnew.LoadBoard(board)
    pads = []
    for f in b.GetFootprints():
        for p in f.Pads():
            if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD:
                bb = p.GetBoundingBox()
                pads.append((f'{f.GetReference()}.{p.GetNumber()}', mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())))
    inside = against = 0
    for t in b.GetTracks():
        if t.Type() != pcbnew.PCB_VIA_T:
            continue
        x, y, r = mm(t.GetPosition().x), mm(t.GetPosition().y), mm(t.GetDrill()) / 2
        d, name = min((math.hypot(max(p[1] - x, 0, x - p[3]), max(p[2] - y, 0, y - p[4])), p[0]) for p in pads)
        if d < r:
            inside += 1
        elif d < r + GAP:
            against += 1
        else:
            continue
        if verbose:
            print(f'  {t.GetNetname():8} via at ({x:.1f}, {y:.1f}): hole {"overlaps" if d < r else "is against"} pad {name}')
    print(f'Vias: {inside} with the hole overlapping an SMD pad, {against} closer than {GAP} mm to one')
    return 1 if inside else 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], '-v' in sys.argv))
