#!/usr/bin/env python3
"""Check a JLCPCB placement file against JLCPCB's own footprints.

For every row of cpl.csv, fetch the EasyEDA/LCSC footprint of the part (the one JLCPCB's
placement preview draws), put it on the board at the CPL position and rotation, and check
that each of its pads lands on a pad of the KiCad footprint, the right way round.
This catches wrong rotation offsets, origin offsets and swapped polarity before ordering.

Run with KiCad's Python (needs pcbnew):  $KICAD_PY scripts/cplcheck.py <board.kicad_pcb> <bom.csv> <cpl.csv>
Footprints are cached in .scratch/easyeda/. Exit code 1 if any part fails, 2 if a footprint
could not be fetched (offline).
"""
import csv, json, math, os, subprocess, sys, time

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.scratch', 'easyeda')
URL = 'https://easyeda.com/api/products/{}/components?version=6.4.19.5'
TOL = 0.05                         # mm, pad centre may sit this far outside the KiCad pad
POLAR_LIBS = ('LED_SMD', 'Diode_SMD')      # KiCad convention: pad 1 = cathode
NUMBERED_LIBS = ('Package_', 'shutter')    # pad numbers must agree with JLCPCB's


def easyeda(lcsc):
    """-> (package name, [(pad number, pin name, x, y)]) in mm, y down, relative to the footprint origin."""
    path = os.path.join(CACHE, lcsc + '.json')
    res = None
    for attempt in range(4):       # the API answers 403 for a while when asked too quickly
        if not os.path.exists(path):
            os.makedirs(CACHE, exist_ok=True)
            subprocess.run(['curl', '-sS', '-m', '30', '-o', path + '.tmp', URL.format(lcsc)], check=True)
            os.replace(path + '.tmp', path)
        try:
            res = json.load(open(path)).get('result')
        except ValueError:
            res = None
        if res:
            break
        os.replace(path, path + '.bad')
        time.sleep(3 * (attempt + 1))
    if not res:
        raise RuntimeError(f'no EasyEDA footprint for {lcsc}')
    names = {}
    for s in res['dataStr']['shape']:
        if s.startswith('P~'):
            seg = s.split('^^')
            names[seg[0].split('~')[3]] = seg[3].split('~')[4] if len(seg) > 3 else ''
    ds = res['packageDetail']['dataStr']
    ox, oy = float(ds['head']['x']), float(ds['head']['y'])
    pads = []
    for s in ds['shape']:
        f = s.split('~')
        if f[0] == 'PAD':
            pads.append((f[8], names.get(f[8], ''), (float(f[2]) - ox) * 0.254, (float(f[3]) - oy) * 0.254))
    return res['packageDetail']['title'], pads


def main(board, bomf, cplf):
    import pcbnew
    b = pcbnew.LoadBoard(board)
    lcsc = {ref.strip(): r['LCSC Part #'] for r in csv.DictReader(open(bomf, newline='')) for ref in r['Designator'].split(',')}
    failed = offline = 0
    for r in csv.DictReader(open(cplf, newline='')):
        ref = r['Designator']
        try:
            pkg, epads = easyeda(lcsc[ref])
        except Exception as e:
            print(f'SKIP {ref}: {e}')
            offline += 1
            continue
        fp = b.FindFootprintByReference(ref)
        lib = str(fp.GetFPID().GetLibNickname())
        kpads = []
        for p in fp.Pads():
            if p.GetNumber():
                bb = p.GetBoundingBox()
                kpads.append((p.GetNumber(), p.GetNetname(), pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()),
                              pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom())))
        X, Y = float(r['Mid X'].rstrip('m')), -float(r['Mid Y'].rstrip('m'))
        th = math.radians(float(r['Rotation']))
        problems, landed = [], {}
        for num, name, ex, ey in epads:
            x = X + ex * math.cos(th) + ey * math.sin(th)        # counter-clockwise seen from the top
            y = Y - ex * math.sin(th) + ey * math.cos(th)
            hit = [k for k in kpads if k[2] - TOL <= x <= k[4] + TOL and k[3] - TOL <= y <= k[5] + TOL]
            if not hit:
                d = min(math.hypot(x - (k[2] + k[4]) / 2, y - (k[3] + k[5]) / 2) for k in kpads)
                problems.append(f'JLC pad {num} misses the footprint by {d:.2f} mm')
                continue
            landed.setdefault(hit[0][0], []).append(num)
            if lib in POLAR_LIBS and name.upper() in ('A', 'K', 'C') and (hit[0][0] == '1') != (name.upper() != 'A'):
                problems.append(f'polarity: JLC pin {name} lands on pad {hit[0][0]} ({hit[0][1]})')
        if lib.startswith(NUMBERED_LIBS):
            problems += [f'pad {k} gets JLC pads {v}' for k, v in landed.items() if k not in v]
        if problems:
            failed += 1
            print(f'FAIL {ref} {lcsc[ref]} {pkg}: ' + '; '.join(sorted(set(problems))[:4]))
    print(f'cplcheck: {failed} bad placement(s), {offline} not checked')
    return 1 if failed else 2 if offline else 0


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
