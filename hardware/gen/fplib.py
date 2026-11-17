"""Custom footprints, written as KiCad .kicad_mod text."""

def _head(name, descr, attr='smd'):
    return (f'(footprint "{name}" (version 20240108) (generator "shutter_gen") (layer "F.Cu")\n'
            f'  (descr "{descr}")\n'
            f'  (property "Reference" "REF**" (at 0 -3.2 0) (layer "F.SilkS") (effects (font (size 0.8 0.8) (thickness 0.12))))\n'
            f'  (property "Value" "{name}" (at 0 3.2 0) (layer "F.Fab") (effects (font (size 0.8 0.8) (thickness 0.12))))\n'
            f'  (attr {attr})\n')

def _rect(x0, y0, x1, y1, layer, w=0.12):
    return f'  (fp_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width {w}) (type solid)) (fill no) (layer "{layer}"))\n'

def _line(x0, y0, x1, y1, layer, w=0.12):
    return f'  (fp_line (start {x0} {y0}) (end {x1} {y1}) (stroke (width {w}) (type solid)) (layer "{layer}"))\n'

def _smd(num, x, y, w, h):
    return f'  (pad "{num}" smd rect (at {x} {y}) (size {w} {h}) (layers "F.Cu" "F.Paste" "F.Mask"))\n'


def vbpw34sr(aperture=0.8):
    """Vishay VBPW34SR reverse gullwing PIN photodiode (datasheet 81128, drawing 6.541-5085.01-4).
    Recommended land: two 1.75 x 1.8 pads, 5.4 inner gap, 8.9 overall. The sensitive face looks
    down at the board, so pad 3 is a plated hole that acts as the optical aperture."""
    t = _head('VBPW34SR_Aperture', 'VBPW34SR reverse-mount photodiode over a plated aperture hole')
    t += _smd('1', -3.575, 0, 1.75, 1.8) + _smd('2', 3.575, 0, 1.75, 1.8)
    t += f'  (pad "3" thru_hole circle (at 0 0) (size {aperture + 0.7} {aperture + 0.7}) (drill {aperture}) (layers "*.Cu" "*.Mask"))\n'
    t += _rect(-2.2, -1.95, 2.2, 1.95, 'F.Fab', 0.1) + _rect(-4.7, -2.1, 4.7, 2.1, 'F.CrtYd', 0.05)
    t += _line(-2.3, -2.05, 2.3, -2.05, 'F.SilkS') + _line(-2.3, 2.05, 2.3, 2.05, 'F.SilkS')
    return t + ')\n'


def esp32_c6_wroom_1():
    """Espressif ESP32-C6-WROOM-1 land pattern (datasheet v1.4 fig. 11-1). Origin is the centre of
    the two pad columns, matching JLCPCB's own footprint so placement coordinates line up."""
    t = _head('ESP32-C6-WROOM-1', 'ESP32-C6-WROOM-1, 18 x 25.5 mm, 28 castellated pads + 9 ground pads')
    for i in range(14):
        y = round(-8.255 + i * 1.27, 3)
        t += _smd(i + 1, -8.75, y, 1.5, 0.9) + _smd(28 - i, 8.75, y, 1.5, 0.9)
    for gx in (-2.755, -1.505, -0.255):
        for gy in (-3.785, -2.535, -1.285):
            t += _smd(29, gx, gy, 0.8, 0.8)
    top, bot = -15.745, 9.755
    t += _rect(-9, top, 9, bot, 'F.Fab', 0.1) + _rect(-9.75, top - 0.25, 9.75, bot + 0.25, 'F.CrtYd', 0.05)
    t += _line(-9, top, 9, top, 'F.SilkS') + _line(-9, top, -9, -9.1, 'F.SilkS') + _line(9, top, 9, -9.1, 'F.SilkS')
    t += _line(-9, bot, 9, bot, 'F.SilkS') + _line(-9, 9.1, -9, bot, 'F.SilkS') + _line(9, 9.1, 9, bot, 'F.SilkS')
    t += _line(-9, top + 6, 9, top + 6, 'Dwgs.User', 0.1)
    t += f'  (fp_circle (center -10.2 -8.255) (end -10.05 -8.255) (stroke (width 0.2) (type solid)) (fill yes) (layer "F.SilkS"))\n'
    return t + ')\n'


def npth(dia):
    t = _head(f'NPTH_{dia}mm', f'{dia} mm unplated sighting hole', 'exclude_from_pos_files exclude_from_bom')
    t += f'  (pad "" np_thru_hole circle (at 0 0) (size {dia} {dia}) (drill {dia}) (layers "*.Cu" "*.Mask"))\n'
    return t + ')\n'
