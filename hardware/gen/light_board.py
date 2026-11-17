"""Shutter tester rev B: light board. Nine white LEDs on plain DC from USB-C (no PWM, so no flicker)."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kigen import Design, build_board
import fplib

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'light-board'))
d = Design('shutter-light', OUT, (50.0, 50.0), 'Shutter tester rev B - light board')
d.flags = ['GND', 'VBUS']
d.custom_fps = {'NPTH_3.2mm': fplib.npth(3.2)}

n = 0
for y in (10.0, 24.0, 38.0):
    for x in (11.0, 25.0, 39.0):
        n += 1
        # Everlight 67-21S: the large pad (pad 1) is the cathode and goes to the ground pour for heat
        d.add(f'D{n}', 'WHITE 6500K', 'Device:LED', 'LED_SMD:LED_PLCC_2835', {'1': 'GND', '2': f'LED{n}_A'}, (x, y), 0,
              lcsc='C385305', mpn='67-21S/KK7C-H657034Z15/DT')
        # (5 V - 2.9 V) / 100 ohm = 21 mA per LED, 44 mW in the resistor, about 190 mA for the board
        d.add(f'R{n}', '100R', 'Device:R', 'Resistor_SMD:R_0805_2012Metric', {'1': f'LED{n}_A', '2': 'VBUS'}, (x + 4.4, y), 0, lcsc='C17408')

j1 = {'A1': 'GND', 'A12': 'GND', 'B1': 'GND', 'B12': 'GND', 'A4': 'VBUS', 'A9': 'VBUS', 'B4': 'VBUS', 'B9': 'VBUS',
      'A5': 'CC1', 'B5': 'CC2', 'A6': None, 'B6': None, 'A7': None, 'B7': None, 'A8': None, 'B8': None, 'SH': 'GND'}
d.add('J1', 'USB-C', 'Connector:USB_C_Receptacle_USB2.0_16P', 'Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12', j1,
      (32.05, 46.8), 0, lcsc='C165948', mpn='TYPE-C-31-M-12')
d.add('R10', '5.1k', 'Device:R', 'Resistor_SMD:R_0402_1005Metric', {'1': 'CC1', '2': 'GND'}, (30.8, 40.55), 90, lcsc='C25905')
d.add('R11', '5.1k', 'Device:R', 'Resistor_SMD:R_0402_1005Metric', {'1': 'CC2', '2': 'GND'}, (33.8, 40.55), 90, lcsc='C25905')
d.add('C1', '10uF', 'Device:C', 'Capacitor_SMD:C_0805_2012Metric', {'1': 'VBUS', '2': 'GND'}, (20.0, 44.5), 0, lcsc='C15850')

d.holes += [(4.0, 4.0, 3.2), (46.0, 4.0, 3.2), (4.0, 46.0, 3.2), (46.0, 46.0, 3.2)]
d.silk.append(('text', 'F', 25, 4.0, 'SHUTTER TESTER LIGHT B', 1.0))
d.silk.append(('text', 'F', 14, 47.0, '5V USB-C  0.2A', 1.0))
d.silk.append(('text', 'F', 25, 17.0, 'FACE THIS SIDE TO THE LENS', 0.9))

# via_pad_gap: keep every via hole at least this far outside any SMD pad, so reflow does not
# pull solder down the hole
RULES = dict(track=0.3, clearance=0.15, via=0.6, drill=0.3, grid=0.1, edge=0.4, corner=3.0, via_pad_gap=0.2)

if __name__ == '__main__':
    t0 = time.time()
    root = d.write_schematic()
    d.write_project(RULES)
    order = ['CC1', 'CC2']
    for attempt in range(6):
        failed, gfail = build_board(d, root, RULES, net_order=order)
        print(f'attempt {attempt}: unrouted={len(failed)} gnd={len(gfail)}', [f[0] for f in failed], gfail, flush=True)
        bad = [x for x in dict.fromkeys(f[0] for f in failed)]
        if not bad and not gfail:
            break
        order = bad + [x for x in order if x not in bad] if bad else order[1:] + order[:1]
    print(f'parts={len(d.parts)} time={time.time() - t0:.0f}s')
