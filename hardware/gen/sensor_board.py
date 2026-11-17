"""Shutter tester rev B: sensor board. Single source of truth for schematic and layout."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kigen import Design, build_board
import fplib

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sensor-board'))
# 77 x 34 mm. 34 mm tall so the board drops into the 35 mm film channel and lies on the film
# rails (same height as the upstream board). The frame centre is 25 mm from the USB end, as on
# the upstream board, so the USB plug lies in the channel on one side of the gate; the
# electronics reach 52 mm the other way, which still fits inside a 35 mm camera's film cavity.
d = Design('shutter-sensor', OUT, (77.0, 34.0), 'Shutter tester rev B - sensor board')
d.flags = ['GND', 'VBUS']
d.custom_fps = {'VBPW34SR_Aperture': fplib.vbpw34sr(1.2), 'ESP32-C6-WROOM-1': fplib.esp32_c6_wroom_1(), 'NPTH_2.5mm': fplib.npth(2.5)}

names = ['GND', '3V3', 'EN', 'IO4', 'IO5', 'IO6', 'IO7', 'IO0', 'IO1', 'IO8', 'IO10', 'IO11', 'IO12/D-', 'IO13/D+',
         'IO9', 'IO18', 'IO19', 'IO20', 'IO21', 'IO22', 'IO23', 'NC', 'IO15', 'RXD0/IO17', 'TXD0/IO16', 'IO3', 'IO2', 'GND']
d.make_box_symbol('ESP32-C6-WROOM-1', [(str(i + 1), names[i]) for i in range(14)],
                  [(str(i + 1), names[i]) for i in range(14, 28)] + [('29', 'EPAD')], 'shutter:ESP32-C6-WROOM-1',
                  pin_types={'1': 'power_in', '2': 'power_in', '3': 'input', '22': 'passive', '28': 'power_in', '29': 'power_in'})
d.make_box_symbol('VBPW34SR', [('1', 'A'), ('2', 'K')], [('3', 'APERTURE')], 'shutter:VBPW34SR_Aperture', ref='D',
                  pin_types={'1': 'passive', '2': 'passive', '3': 'passive'})

RV = {'5.1k': 'C25905', '10k': 'C25744', '1k': 'C11702', '22k': 'C25768', '1M': 'C26083', '4.7k': 'C25900'}
CV = {'100nF': ('C1525', '0402_1005Metric'), '1uF': ('C52923', '0402_1005Metric'), '6.8pF': ('C1576', '0402_1005Metric'),
      '4.7uF': ('C19666', '0603_1608Metric'), '10uF': ('C15850', '0805_2012Metric'), '22uF': ('C45783', '0805_2012Metric')}

def R(ref, val, n1, n2, x, y, rot=0):
    d.add(ref, val, 'Device:R', 'Resistor_SMD:R_0402_1005Metric', {'1': n1, '2': n2}, (x, y), rot, lcsc=RV[val])

def C(ref, val, n1, n2, x, y, rot=0):
    d.add(ref, val, 'Device:C', 'Capacitor_SMD:C_' + CV[val][1], {'1': n1, '2': n2}, (x, y), rot, lcsc=CV[val][0])

# ---- MCU ---------------------------------------------------------------------------------
u1 = {str(i): None for i in range(1, 30)}
u1.update({'1': 'GND', '28': 'GND', '29': 'GND', '2': '+3V3', '3': 'EN', '10': 'IO8', '13': 'USB_DN', '14': 'USB_DP',
           '15': 'BOOT', '16': 'PWM1', '17': 'PWM2', '18': 'PWM3', '19': 'CMP1', '20': 'CMP2', '21': 'CMP3',
           '24': 'LED2_K', '25': 'LED1_K', '26': 'SIG2', '27': 'SIG1', '9': 'SIG3'})
# Antenna end on the right-hand edge. Top row, left to right, is pins 14..1; bottom row 15..28.
d.add('U1', 'ESP32-C6-WROOM-1-N8', 'shutter:ESP32-C6-WROOM-1', 'shutter:ESP32-C6-WROOM-1', u1, (61.2, 17.0), 270,
      lcsc='C5366877', mpn='ESP32-C6-WROOM-1-N8')
d.keepouts.append((70.7, 7.1, 77.0, 26.9))         # antenna: no copper on either layer
C('C4', '100nF', 'GND', '+3V3', 69.2, 5.8)
R('R3', '10k', 'EN', '+3V3', 65.6, 5.8)
C('C5', '1uF', 'EN', 'GND', 65.6, 3.6)
R('R5', '10k', '+3V3', 'IO8', 58.0, 5.6, 90)
C('C3', '10uF', '+3V3', 'GND', 70.6, 2.6)
d.add('SW1', 'BOOT', 'Switch:SW_Push', 'Button_Switch_SMD:SW_Push_1P1T_XKB_TS-1187A', {'1': 'BOOT', '2': 'GND'}, (13.6, 27.6), 0,
      lcsc='C318884', mpn='TS-1187A-B-A-B')
R('R4', '10k', 'BOOT', '+3V3', 49.4, 24.4)

# ---- USB and power -----------------------------------------------------------------------
j1 = {'A1': 'GND', 'A12': 'GND', 'B1': 'GND', 'B12': 'GND', 'A4': 'VBUS', 'A9': 'VBUS', 'B4': 'VBUS', 'B9': 'VBUS',
      'A5': 'CC1', 'B5': 'CC2', 'A6': 'USB_DP', 'B6': 'USB_DP', 'A7': 'USB_DN', 'B7': 'USB_DN', 'A8': None, 'B8': None, 'SH': 'GND'}
d.add('J1', 'USB-C', 'Connector:USB_C_Receptacle_USB2.0_16P', 'Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12', j1,
      (3.2, 17.0), 270, lcsc='C165948', mpn='TYPE-C-31-M-12')
R('R1', '5.1k', 'GND', 'CC1', 11.6, 14.4)
R('R2', '5.1k', 'GND', 'CC2', 11.6, 19.6)
C('C1', '10uF', 'VBUS', 'GND', 47.4, 10.2)
d.add('U4', 'AMS1117-3.3', 'Regulator_Linear:AMS1117-3.3', 'Package_TO_SOT_SMD:SOT-223-3_TabPin2',
      {'1': 'GND', '2': '+3V3', '3': 'VBUS'}, (50.0, 3.5), 0, lcsc='C6186', mpn='AMS1117-3.3')
C('C2', '22uF', '+3V3', 'GND', 56.4, 2.2)

# ---- photodiodes and transimpedance amplifiers --------------------------------------------
# Both photodiode leads sit at VREF (zero bias), so the circuit works whichever way round the
# diode is; the firmware detects the signal polarity during calibration.
# Rf 1M with 6.8 pF: about 23 kHz, no overshoot in simulation (3.3 pF overshoots 18 %).
# Frame centre is (25, 17); sensors sit on the frame diagonal, 16 mm and 10 mm off centre.
PDXY = ((9.0, 7.0), (25.0, 17.0), (41.0, 27.0))
PD = dict(sym='shutter:VBPW34SR', fp='shutter:VBPW34SR_Aperture', lcsc='C845527', mpn='VBPW34SR')
d.add('D1', 'VBPW34SR', PD['sym'], PD['fp'], {'1': 'VREF', '2': 'PD1_K', '3': 'GND'}, PDXY[0], 0, lcsc=PD['lcsc'], mpn=PD['mpn'])
d.add('D2', 'VBPW34SR', PD['sym'], PD['fp'], {'1': 'PD2_K', '2': 'VREF', '3': 'GND'}, PDXY[1], 0, lcsc=PD['lcsc'], mpn=PD['mpn'])
d.add('D3', 'VBPW34SR', PD['sym'], PD['fp'], {'1': 'PD3_K', '2': 'VREF', '3': 'GND'}, PDXY[2], 0, lcsc=PD['lcsc'], mpn=PD['mpn'])
UX, UY = 25.0, 8.6
d.add('U2', 'MCP6004', 'Amplifier_Operational:MCP6004', 'Package_SO:SOIC-14_3.9x8.7mm_P1.27mm',
      {'1': 'SIG2', '2': 'PD2_K', '3': 'VREF', '4': '+3V3', '5': 'VREF', '6': 'PD3_K', '7': 'SIG3',
       '8': 'VREF', '9': 'VREF', '10': 'VREF_DIV', '11': 'GND', '12': 'VREF', '13': 'PD1_K', '14': 'SIG1'},
      (UX, UY), 90, lcsc='C7378', mpn='MCP6004T-I/SL')
R('R10', '1M', 'SIG1', 'PD1_K', UX - 3.4, UY - 4.8);  C('C9', '6.8pF', 'SIG1', 'PD1_K', UX - 6.0, UY - 4.8)
R('R11', '1M', 'SIG2', 'PD2_K', UX - 3.4, UY + 4.6);  C('C10', '6.8pF', 'PD2_K', 'SIG2', UX - 6.0, UY + 4.6)
R('R12', '1M', 'PD3_K', 'SIG3', UX + 3.4, UY + 4.6);  C('C11', '6.8pF', 'SIG3', 'PD3_K', UX + 6.0, UY + 4.6)
C('C6', '100nF', '+3V3', 'GND', UX, UY + 4.6)
C('C8', '1uF', 'VREF_DIV', 'GND', UX + 0.2, UY - 4.9, 90)
R('R9', '10k', 'VREF_DIV', 'GND', UX + 1.8, UY - 4.9, 90)
R('R8', '10k', 'VREF_DIV', '+3V3', UX + 3.4, UY - 4.9, 90)

# ---- comparators with firmware-set thresholds ---------------------------------------------
d.add('U3', 'LM339', 'Comparator:LM339', 'Package_SO:SOIC-14_3.9x8.7mm_P1.27mm',
      {'1': 'CMP2', '2': 'CMP1', '3': 'VBUS', '4': 'THR1', '5': 'CIN1', '6': 'THR2', '7': 'CIN2',
       '8': 'THR3', '9': 'CIN3', '10': 'GND', '11': 'GND', '12': 'GND', '13': None, '14': 'CMP3'},
      (38.6, 8.8), 0, lcsc='C71036', mpn='LM339DT')
C('C7', '100nF', 'GND', 'VBUS', 38.6, 2.4)
R('R16', '1M', 'CMP1', 'CIN1', 34.6, 15.8);  R('R13', '4.7k', 'CIN1', 'SIG1', 34.6, 18.0)
R('R17', '1M', 'CIN2', 'CMP2', 37.2, 15.8);  R('R14', '4.7k', 'CIN2', 'SIG2', 37.2, 18.0)
R('R18', '1M', 'CMP3', 'CIN3', 39.8, 15.8);  R('R15', '4.7k', 'CIN3', 'SIG3', 39.8, 18.0)
for i, y in enumerate((28.2, 30.0, 31.8), 1):
    R(f'R{21 + i}', '22k', f'PWM{i}', f'THR{i}', 51.4, y, 180)
    C(f'C{11 + i}', '4.7uF', f'THR{i}', 'GND', 48.4, y, 180)
for i, x in enumerate((58.0, 59.7, 61.4), 1):
    R(f'R{18 + i}', '10k', f'CMP{i}', '+3V3', x, 28.6, 270)

# ---- status LEDs (active low) -------------------------------------------------------------
LEDFP = 'LED_SMD:LED_0603_1608Metric'
R('R7', '1k', 'LED2_A', 'VBUS', 64.0, 31.6)
d.add('LED2', 'WHITE', 'Device:LED', LEDFP, {'1': 'LED2_K', '2': 'LED2_A'}, (64.0, 29.4), 0, lcsc='C2290', mpn='KT-0603W')
R('R6', '1k', 'LED1_A', '+3V3', 68.0, 31.6)
d.add('LED1', 'RED', 'Device:LED', LEDFP, {'1': 'LED1_K', '2': 'LED1_A'}, (68.0, 29.4), 0, lcsc='C2286', mpn='KT-0603R')

# ---- mechanics ----------------------------------------------------------------------------
d.holes += [(43.0, 17.0, 2.5), (25.0, 29.0, 2.5)]  # sighting holes on a short and a long frame edge
# Light-tight zone behind each photodiode: no back-layer tracks and no vias under the sensor,
# so the only light path to the die is the 1.2 mm aperture.
d.back_blocks = [(x - 3.2, y - 2.8, x + 3.2, y + 2.8) for (x, y) in PDXY]
for (x0, y0, x1, y1) in ((7, 5, 43, 5), (43, 5, 43, 29), (43, 29, 7, 29), (7, 29, 7, 5)):
    d.silk.append(('line', 'B', x0, y0, x1, y1))
d.silk.append(('text', 'B', 25, 31.8, 'CAMERA SIDE  36x24 FRAME', 1.0))
d.silk.append(('text', 'F', 33, 31.8, 'SHUTTER TESTER B', 0.9))

# via_pad_gap keeps via holes off SMD pads (solder would wick down the barrel).
RULES = dict(track=0.2, clearance=0.15, via=0.6, drill=0.3, grid=0.1, edge=0.4, corner=2.0, via_pad_gap=0.2)

if os.environ.get('MAX_RIPS'):
    RULES['max_rips'] = int(os.environ['MAX_RIPS'])

if __name__ == '__main__':
    t0 = time.time()
    root = d.write_schematic()
    d.write_project(RULES)
    order = ['USB_DP', 'USB_DN', 'CC1', 'CC2', 'PD1_K', 'PD2_K', 'PD3_K']
    first = []          # pads that failed on an earlier attempt get linked up first on the next
    for attempt in range(int(os.environ.get('ATTEMPTS', '8'))):
        failed, gfail = build_board(d, root, RULES, net_order=order, first_pads=first)
        print(f'attempt {attempt}: unrouted={len(failed)} gnd={len(gfail)}', failed, flush=True)
        if not failed and not gfail:
            break
        if not failed:      # routed, but the back ground pour got cut: reshuffle and try again
            order = order[1:] + order[:1]
        first += [f for f in failed if f not in first]
    print(f'parts={len(d.parts)} time={time.time() - t0:.0f}s')
    print('unrouted:', failed)
    print('ground pads without via:', gfail)
