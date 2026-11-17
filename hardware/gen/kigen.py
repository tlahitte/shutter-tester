"""Generate a KiCad project (schematic + placed, routed board) from one Python netlist.

Run with KiCad's bundled Python (it provides pcbnew), from the repository root:
  . scripts/_env.sh sensor-board && "$KICAD_PY" hardware/gen/sensor_board.py

JLCPCB placement rotation and origin corrections are not part of the design; they are applied
to the placement file in scripts/fab.sh and verified by scripts/cplcheck.py.
"""
import copy, heapq, json, math, os, sys, uuid
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sexpr import Sym, dump, find, first, parse

KS = '/Applications/KiCad/KiCad.app/Contents/SharedSupport'
NS = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')
SCH_VERSION = 20250114


def uid(*a):
    return str(uuid.uuid5(NS, '/'.join(map(str, a))))


def S(*a):
    return [Sym(a[0])] + list(a[1:])


class Part:
    def __init__(self, ref, value, sym, fp, pins, pos, rot=0, lcsc='', mpn=''):
        self.ref, self.value, self.sym, self.fp = ref, value, sym, fp
        self.pins, self.pos, self.rot = pins, pos, rot
        self.lcsc, self.mpn = lcsc, mpn


class Design:
    def __init__(self, name, outdir, size, title=''):
        self.name, self.outdir, self.size, self.title = name, outdir, size, title
        self.parts, self.holes, self.keepouts, self.silk = [], [], [], []
        self.flags = ['GND']
        self.custom_syms, self.custom_fps = {}, {}
        os.makedirs(outdir, exist_ok=True)

    def add(self, *a, **k):
        p = Part(*a, **k)
        self.parts.append(p)
        return p

    def pin_names(self, libid):
        out = {}
        for sub in find(self.symbol(libid), 'symbol'):
            for pin in find(sub, 'pin'):
                out[first(pin, 'number')[1]] = first(pin, 'name')[1]
        return out

    # ---------------------------------------------------------------- symbols
    _libs = {}

    def _lib(self, lib):
        if lib not in self._libs:
            if lib == 'shutter':
                self._libs[lib] = dict(self.custom_syms)
            else:
                t = parse(open(f'{KS}/symbols/{lib}.kicad_sym').read())
                self._libs[lib] = {s[1]: s for s in find(t, 'symbol')}
        return self._libs[lib]

    def symbol(self, libid):
        lib, name = libid.split(':')
        syms = self._lib(lib)
        s = copy.deepcopy(syms[name])
        ext = first(s, 'extends')
        while ext:
            base = copy.deepcopy(syms[ext[1]])
            bname = base[1]
            props = {p[1]: p for p in find(s, 'property')}
            body = [n for n in base[2:] if not (isinstance(n, list) and n[0] == 'property' and n[1] in props)]
            for n in body:
                if isinstance(n, list) and n[0] == 'symbol':
                    n[1] = name + n[1][len(bname):]
            s = [Sym('symbol'), name] + list(props.values()) + body
            ext = first(s, 'extends')
        return s

    @staticmethod
    def sym_pins(s):
        """-> {unit: [(number, x, y, angle)]}; unit 0 is shared by all units."""
        out = {}
        for sub in find(s, 'symbol'):
            unit = int(sub[1].rsplit('_', 2)[-2])
            for pin in find(sub, 'pin'):
                at = first(pin, 'at')
                out.setdefault(unit, []).append((first(pin, 'number')[1], float(at[1]), float(at[2]), int(float(at[3])) if len(at) > 3 else 0))
        return out

    def make_box_symbol(self, name, left, right, fp, ref='U', pin_types=None):
        """Rectangular symbol. left/right: [(number, pin_name)]."""
        pin_types = pin_types or {}
        n = max(len(left), len(right))
        h = (n + 1) * 2.54 / 2
        h = math.ceil(h / 2.54) * 2.54
        w = 12.7
        ef = S('effects', S('font', S('size', 1.27, 1.27)))
        pins = []
        for side, lst, x, ang in (('L', left, -w - 2.54, 0), ('R', right, w + 2.54, 180)):
            for i, (num, pname) in enumerate(lst):
                y = h - 2.54 - i * 2.54
                pins.append(S('pin', Sym(pin_types.get(num, 'bidirectional')), Sym('line'), S('at', x, y, ang), S('length', 2.54),
                              S('name', pname, ef), S('number', num, ef)))
        hide = S('effects', S('font', S('size', 1.27, 1.27)), S('hide', Sym('yes')))
        sym = [Sym('symbol'), name, S('pin_names', S('offset', 1.016)), S('exclude_from_sim', Sym('no')), S('in_bom', Sym('yes')),
               S('on_board', Sym('yes')),
               S('property', 'Reference', ref, S('at', -w, h + 1.27, 0), ef), S('property', 'Value', name, S('at', 0, h + 1.27, 0), ef),
               S('property', 'Footprint', fp, S('at', 0, 0, 0), hide), S('property', 'Datasheet', '', S('at', 0, 0, 0), hide),
               S('property', 'Description', name, S('at', 0, 0, 0), hide),
               [Sym('symbol'), name + '_0_1', S('rectangle', S('start', -w, h), S('end', w, -h), S('stroke', S('width', 0.254), S('type', Sym('default'))),
                                               S('fill', S('type', Sym('background'))))],
               [Sym('symbol'), name + '_1_1'] + pins]
        self.custom_syms[name] = sym

    # -------------------------------------------------------------- schematic
    def write_schematic(self):
        root = uid(self.name, 'root')
        ef = S('effects', S('font', S('size', 1.27, 1.27)))
        hide = S('effects', S('font', S('size', 1.27, 1.27)), S('hide', Sym('yes')))
        libsyms, items = {}, []
        cx, cy, rowh = 30.0, 30.0, 0.0

        def place(libid, ref, value, props, pinnets, units_only=None, hidden_ref=False):
            nonlocal cx, cy, rowh
            if libid not in libsyms:
                s = self.symbol(libid)
                s[1] = libid
                libsyms[libid] = s
            s = libsyms[libid]
            pins = self.sym_pins(s)
            units = sorted(u for u in pins if u != 0) or [0]
            for u in units:
                plist = pins.get(u, []) + (pins.get(0, []) if u != 0 else [])
                xs = [p[1] for p in plist] or [0]
                ys = [p[2] for p in plist] or [0]
                w = max(xs) - min(xs) + 46
                h = max(ys) - min(ys) + 15
                if cx + w > 400:
                    cx, cy, rowh = 30.0, cy + rowh + 5, 0.0
                ox = round((cx - min(xs) + 23) / 2.54) * 2.54
                oy = round((cy + max(ys) + 8) / 2.54) * 2.54
                cx += w
                rowh = max(rowh, h)
                unit = u if u != 0 else 1
                inst = [Sym('symbol'), S('lib_id', libid), S('at', ox, oy, 0), S('unit', unit), S('exclude_from_sim', Sym('no')),
                        S('in_bom', Sym('no' if ref.startswith('#') else 'yes')), S('on_board', Sym('yes')), S('dnp', Sym('no')),
                        S('uuid', uid(self.name, ref, unit)),
                        S('property', 'Reference', ref, S('at', ox, oy - max(ys) - 5, 0), hide if hidden_ref else ef),
                        S('property', 'Value', value, S('at', ox, oy - max(ys) - 3, 0), ef)]
                for k, v in props.items():
                    inst.append(S('property', k, v, S('at', ox, oy, 0), hide))
                for p in plist:
                    inst.append(S('pin', p[0], S('uuid', uid(self.name, ref, unit, 'pin', p[0]))))
                inst.append(S('instances', S('project', self.name, S('path', '/' + root, S('reference', ref), S('unit', unit)))))
                items.append(inst)
                for num, px, py, ang in plist:
                    x, y = round(ox + px, 4), round(oy - py, 4)
                    net = pinnets.get(num, '__missing__')
                    if net == '__missing__':
                        raise SystemExit(f'{ref}: pin {num} has no net assignment')
                    if net is None:
                        items.append(S('no_connect', S('at', x, y), S('uuid', uid(self.name, ref, 'nc', num))))
                        continue
                    lang = {0: 180, 180: 0, 90: 270, 270: 90}[ang]
                    just = 'right' if lang in (180, 270) else 'left'
                    items.append(S('global_label', net, S('shape', Sym('passive')), S('at', x, y, lang),
                                   S('effects', S('font', S('size', 1.27, 1.27)), S('justify', Sym(just))),
                                   S('uuid', uid(self.name, ref, 'lbl', num, u))))

        for p in self.parts:
            props = {'Footprint': p.fp, 'Datasheet': '', 'Description': ''}
            if p.lcsc: props['LCSC'] = p.lcsc
            if p.mpn: props['MPN'] = p.mpn
            place(p.sym, p.ref, p.value, props, p.pins)
        for i, net in enumerate(self.flags):
            place('power:PWR_FLAG', f'#FLG0{i + 1}', 'PWR_FLAG', {'Footprint': '', 'Datasheet': '', 'Description': ''}, {'1': net}, hidden_ref=True)

        doc = [Sym('kicad_sch'), S('version', SCH_VERSION), S('generator', 'shutter_gen'), S('generator_version', '1.0'),
               S('uuid', root), S('paper', 'A3'),
               S('title_block', S('title', self.title or self.name), S('rev', 'B'), S('company', 'Derived from bjpirt/shutter-tester (CERN-OHL-P)')),
               [Sym('lib_symbols')] + list(libsyms.values())] + items + [
               S('sheet_instances', S('path', '/', S('page', '1'))), S('embedded_fonts', Sym('no'))]
        open(f'{self.outdir}/{self.name}.kicad_sch', 'w').write(dump(doc) + '\n')
        if self.custom_syms:
            lib = [Sym('kicad_symbol_lib'), S('version', 20241209), S('generator', 'shutter_gen'), S('generator_version', '1.0')] + list(self.custom_syms.values())
            open(f'{self.outdir}/shutter.kicad_sym', 'w').write(dump(lib) + '\n')
        open(f'{self.outdir}/sym-lib-table', 'w').write(
            '(sym_lib_table\n  (version 7)\n  (lib (name "shutter")(type "KiCad")(uri "${KIPRJMOD}/shutter.kicad_sym")(options "")(descr ""))\n)\n')
        open(f'{self.outdir}/fp-lib-table', 'w').write(
            '(fp_lib_table\n  (version 7)\n  (lib (name "shutter")(type "KiCad")(uri "${KIPRJMOD}/shutter.pretty")(options "")(descr ""))\n)\n')
        os.makedirs(f'{self.outdir}/shutter.pretty', exist_ok=True)
        for name, text in self.custom_fps.items():
            open(f'{self.outdir}/shutter.pretty/{name}.kicad_mod', 'w').write(text)
        return root

    def write_project(self, rules):
        pro = {
            "board": {"design_settings": {"defaults": {}, "rules": {
                "min_clearance": rules['clearance'], "min_track_width": rules['track'], "min_via_diameter": 0.45,
                "min_through_hole_diameter": 0.2, "min_via_annular_width": 0.1, "min_copper_edge_clearance": 0.3,
                "min_hole_clearance": 0.2, "min_hole_to_hole": 0.25, "min_silk_clearance": 0.0, "solder_mask_to_copper_clearance": 0.0,
                "min_text_height": 0.6, "min_text_thickness": 0.08},
                "track_widths": [0.0, rules['track']], "via_dimensions": [{"diameter": 0.0, "drill": 0.0}, {"diameter": rules['via'], "drill": rules['drill']}],
                "rule_severities": {"lib_footprint_issues": "ignore", "lib_footprint_mismatch": "ignore", "silk_overlap": "ignore",
                                    "silk_over_copper": "ignore", "silk_edge_clearance": "ignore", "text_height": "ignore",
                                    "text_thickness": "ignore", "footprint_symbol_field_mismatch": "ignore", "solder_mask_bridge": "warning"}}},
            "erc": {"rule_severities": {"lib_symbol_issues": "ignore", "lib_symbol_mismatch": "ignore", "footprint_link_issues": "ignore",
                                        "label_dangling": "error", "pin_not_connected": "error", "power_pin_not_driven": "error",
                                        "pin_to_pin": "error"}},
            "meta": {"filename": f"{self.name}.kicad_pro", "version": 3},
            "net_settings": {"classes": [{"name": "Default", "clearance": rules['clearance'], "track_width": rules['track'],
                                          "via_diameter": rules['via'], "via_drill": rules['drill'], "bus_width": 12, "wire_width": 6,
                                          "diff_pair_width": 0.2, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25, "line_style": 0,
                                          "microvia_diameter": 0.3, "microvia_drill": 0.1, "pcb_color": "rgba(0, 0, 0, 0.000)",
                                          "schematic_color": "rgba(0, 0, 0, 0.000)", "priority": 2147483647, "tuning_profile": ""}],
                             "meta": {"version": 4}},
            "schematic": {"meta": {"version": 1}}, "sheets": [], "text_variables": {}}
        json.dump(pro, open(f'{self.outdir}/{self.name}.kicad_pro', 'w'), indent=2)


# ======================================================================== board
class Router:
    def __init__(self, w, h, g, rules):
        self.g, self.W, self.H = g, int(round(w / g)) + 1, int(round(h / g)) + 1
        self.rules = rules
        n = self.W * self.H
        self.own = [array('h', [0]) * n, array('h', [0]) * n]
        self.ins = [array('h', [0]) * n, array('h', [0]) * n]
        self.novia = bytearray(n)      # 1 = a via here would sit in or against an SMD pad
        self.pad_infl = rules['clearance'] + rules['track'] / 2 + 0.04
        e = int(math.ceil((rules['edge'] + rules['track'] / 2) / g))
        for l in (0, 1):
            o = self.own[l]
            for y in range(self.H):
                for x in range(self.W):
                    if x < e or y < e or x >= self.W - e or y >= self.H - e:
                        o[y * self.W + x] = -1
        # obstacle maps are already inflated by clearance + half a track, so a via only needs the extra radius
        self.vr = int(math.ceil((rules['via'] / 2 - rules['track'] / 2) / g + 0.5))
        self.vias = []          # (x, y, net) cell coords of every via placed
        self.vsep = int(math.ceil((rules['drill'] + 0.45) / g))
        self.vdisk = [(dx, dy) for dx in range(-self.vr, self.vr + 1) for dy in range(-self.vr, self.vr + 1) if dx * dx + dy * dy <= self.vr * self.vr]

    def _mark(self, l, x0, y0, x1, y1, net, infl):
        g, W, H, o = self.g, self.W, self.H, self.own[l]
        for y in range(max(0, int(math.ceil((y0 - infl) / g - 1e-6))), min(H - 1, int(math.floor((y1 + infl) / g + 1e-6))) + 1):
            base = y * W
            for x in range(max(0, int(math.ceil((x0 - infl) / g - 1e-6))), min(W - 1, int(math.floor((x1 + infl) / g + 1e-6))) + 1):
                v = o[base + x]
                if v == 0:
                    o[base + x] = net
                elif v != net:
                    o[base + x] = -1

    def add_copper(self, layers, box, net, infl=None):
        """box=(x0,y0,x1,y1) mm. net>0 owned copper, net=-1 obstacle."""
        g, W = self.g, self.W
        for l in layers:
            self._mark(l, box[0], box[1], box[2], box[3], net, self.pad_infl if infl is None else infl)
            if net > 0:
                ins = self.ins[l]
                for y in range(int(math.ceil(box[1] / g - 1e-6)), int(math.floor(box[3] / g + 1e-6)) + 1):
                    for x in range(int(math.ceil(box[0] / g - 1e-6)), int(math.floor(box[2] / g + 1e-6)) + 1):
                        if 0 <= x < W and 0 <= y < self.H:
                            ins[y * W + x] = net

    def block_vias(self, box, margin):
        """No via centre within `margin` of this SMD pad, whatever its net: an open via in or
        against a pad drains the solder paste during reflow."""
        g, W, H = self.g, self.W, self.H
        for y in range(max(0, int(math.ceil((box[1] - margin) / g - 1e-6))), min(H - 1, int(math.floor((box[3] + margin) / g + 1e-6))) + 1):
            for x in range(max(0, int(math.ceil((box[0] - margin) / g - 1e-6))), min(W - 1, int(math.floor((box[2] + margin) / g + 1e-6))) + 1):
                self.novia[y * W + x] = 1

    def ok(self, l, i, net):
        v = self.own[l][i]
        return v == 0 or v == net or self.ins[l][i] == net

    def via_ok(self, x, y, net):
        W, H = self.W, self.H
        if x < self.vr + 3 or y < self.vr + 3 or x >= W - self.vr - 3 or y >= H - self.vr - 3:
            return False
        if self.novia[y * W + x]:
            return False
        for (vx, vy, _) in self.vias:
            if abs(vx - x) <= self.vsep and abs(vy - y) <= self.vsep:
                return False
        for l in (0, 1):
            o = self.own[l]
            for dx, dy in self.vdisk:
                v = o[(y + dy) * W + x + dx]
                if v != 0 and v != net:
                    return False
        return True

    def cells_in(self, l, box, net):
        # shrink the box so track ends land well inside the pad copper (rounded pad corners)
        g, W = self.g, self.W
        sh = min(0.3 * min(box[2] - box[0], box[3] - box[1]), 0.5)
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        out = [(l, int(round(cx / g)), int(round(cy / g)))]
        box = (box[0] + sh, box[1] + sh, box[2] - sh, box[3] - sh)
        for y in range(int(math.ceil(box[1] / g - 1e-6)), int(math.floor(box[3] / g + 1e-6)) + 1):
            for x in range(int(math.ceil(box[0] / g - 1e-6)), int(math.floor(box[2] / g + 1e-6)) + 1):
                if 0 <= x < W and 0 <= y < self.H:
                    out.append((l, x, y))
        return out

    def search(self, sources, target, net, via_cost=90, back_cost=3.0, want_via=False):
        """A* from source cells to target cells (set) or, if want_via, to any via-able front cell."""
        W, H = self.W, self.H
        if target:
            tx = sum(t[1] for t in target) / len(target)
            ty = sum(t[2] for t in target) / len(target)
            tr = max(abs(t[1] - tx) + abs(t[2] - ty) for t in target)
            tset = set(target)
        self._viaset = set(self.vias)
        dist, prev, pq = {}, {}, []
        for s in sources:
            dist[s] = 0.0
            hq = 0 if want_via else max(0.0, math.hypot(s[1] - tx, s[2] - ty) - tr)
            heapq.heappush(pq, (hq, 0.0, s))
        moves = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)]
        pops = 0
        while pq:
            f, d, cur = heapq.heappop(pq)
            if d > dist.get(cur, 1e18):
                continue
            l, x, y = cur
            pops += 1
            if pops > 900000:
                break
            if want_via:
                if l == 0 and d > 0 and (x, y, net) in self._viaset:
                    return self._trace(prev, cur)
                if l == 0 and x % 2 == 0 and y % 2 == 0 and d > 5 and self.ins[0][y * W + x] == 0 and self.via_ok(x, y, net):
                    return self._trace(prev, cur)
            elif cur in tset:
                return self._trace(prev, cur)
            mult = back_cost if l == 1 else 1.0
            for dx, dy, c in moves:
                nx, ny = x + dx, y + dy
                if nx < 0 or ny < 0 or nx >= W or ny >= H:
                    continue
                i = ny * W + nx
                if not self.ok(l, i, net):
                    continue
                if dx and dy and not (self.ok(l, y * W + nx, net) and self.ok(l, ny * W + x, net)):
                    continue
                nd = d + c * mult
                nxt = (l, nx, ny)
                if nd < dist.get(nxt, 1e18):
                    dist[nxt] = nd
                    prev[nxt] = cur
                    hq = 0 if want_via else max(0.0, math.hypot(nx - tx, ny - ty) - tr)
                    heapq.heappush(pq, (nd + hq, nd, nxt))
            if not want_via and x % 2 == 0 and y % 2 == 0 and self.ok(1 - l, y * W + x, net) and self.via_ok(x, y, net):
                nxt = (1 - l, x, y)
                nd = d + via_cost
                if nd < dist.get(nxt, 1e18):
                    dist[nxt] = nd
                    prev[nxt] = cur
                    heapq.heappush(pq, (nd + max(0.0, math.hypot(x - tx, y - ty) - tr), nd, nxt))
        return None

    @staticmethod
    def _trace(prev, cur):
        path = [cur]
        while cur in prev:
            cur = prev[cur]
            path.append(cur)
        return path[::-1]

    def commit(self, path, net):
        """Mark a routed path as copper of `net`. Returns via cells."""
        g, r = self.g, self.rules
        tin = r['track'] / 2 + r['clearance'] + r['track'] / 2 + 0.04
        vin = r['via'] / 2 + r['clearance'] + r['track'] / 2 + 0.04
        vias = []
        for k, (l, x, y) in enumerate(path):
            self._mark(l, x * g, y * g, x * g, y * g, net, tin)
            self.ins[l][y * self.W + x] = net
            if k and path[k - 1][0] != l:
                vias.append((x, y))
                self.vias.append((x, y, net))
                for ll in (0, 1):
                    self._mark(ll, x * g, y * g, x * g, y * g, net, vin)
        return vias


def build_board(d, root, rules, net_order=(), no_route=(), gnd='GND', pours=None, extra=None, first_pads=()):
    import pcbnew
    mm = pcbnew.FromMM
    P = lambda x, y: pcbnew.VECTOR2I(mm(x), mm(y))
    W, H = d.size
    b = pcbnew.CreateEmptyBoard()
    ds = b.GetDesignSettings()
    ds.m_MinClearance = mm(rules['clearance'])
    ds.m_CopperEdgeClearance = mm(0.3)

    nets = {}
    for p in d.parts:           # unused pins get KiCad's own "unconnected-(...)" net names so parity is exact
        pn = None
        for num in list(p.pins):
            if p.pins[num] is None:
                pn = pn or d.pin_names(p.sym)
                nm = pn.get(num, '')
                p.pins[num] = f'unconnected-({p.ref}-{nm}-Pad{num})' if nm and nm != '~' else f'unconnected-({p.ref}-Pad{num})'
        for n in p.pins.values():
            if n and n not in nets:
                ni = pcbnew.NETINFO_ITEM(b, n)
                b.Add(ni)
                nets[n] = ni
    netid = {n: i + 1 for i, n in enumerate(nets)}

    # outline with rounded corners
    r = rules.get('corner', 3.0)
    def line(x0, y0, x1, y1, layer=pcbnew.Edge_Cuts, w=0.1):
        s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetStart(P(x0, y0)); s.SetEnd(P(x1, y1))
        s.SetLayer(layer); s.SetWidth(mm(w)); b.Add(s)
    def arc(cx, cy, a0, layer=pcbnew.Edge_Cuts):
        s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_ARC)
        pt = lambda a: P(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
        s.SetArcGeometry(pt(a0), pt(a0 + 45), pt(a0 + 90)); s.SetLayer(layer); s.SetWidth(mm(0.1)); b.Add(s)
    line(r, 0, W - r, 0); line(W, r, W, H - r); line(W - r, H, r, H); line(0, H - r, 0, r)
    arc(r, r, 180); arc(W - r, r, 270); arc(W - r, H - r, 0); arc(r, H - r, 90)

    rt = Router(W, H, rules['grid'], rules)
    # corners: block the square outside the rounded corner
    for (cx, cy) in ((0, 0), (W, 0), (W, H), (0, H)):
        rt.add_copper((0, 1), (min(cx, abs(cx - r * 0.45)), min(cy, abs(cy - r * 0.45)), max(cx, abs(cx - r * 0.45)), max(cy, abs(cy - r * 0.45))), -1, 0.3)

    pads = {}      # net -> [(layers, box, ref, num)]
    fps = {}
    for p in d.parts:
        lib, name = p.fp.split(':')
        path = f'{d.outdir}/shutter.pretty' if lib == 'shutter' else f'{KS}/footprints/{lib}.pretty'
        fp = pcbnew.FootprintLoad(path, name)
        if fp is None:
            raise SystemExit(f'footprint not found: {p.fp}')
        fp.SetReference(p.ref); fp.SetValue(p.value)
        fp.SetFPID(pcbnew.LIB_ID(lib, name))
        fp.SetPath(pcbnew.KIID_PATH('/' + uid(d.name, p.ref, 1)))
        if p.lcsc: fp.SetField('LCSC', p.lcsc)
        if p.mpn: fp.SetField('MPN', p.mpn)
        for f in fp.GetFields():
            if f.GetName() in ('LCSC', 'MPN'):
                f.SetVisible(False)
        fp.SetPosition(P(*p.pos)); fp.SetOrientationDegrees(p.rot)
        b.Add(fp)
        fps[p.ref] = fp
        seen = set()
        for pad in fp.Pads():
            num = pad.GetNumber()
            bb = pad.GetBoundingBox()
            box = (pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()), pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))
            th = pad.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH)
            layers = (0, 1) if th else (0,)
            net = p.pins.get(num) if num else None
            if num and num not in p.pins:
                raise SystemExit(f'{p.ref}: footprint pad {num} missing from pin map')
            seen.add(num)
            if not th and rules.get('via_pad_gap') is not None:     # optional: keep via holes off SMD pads
                rt.block_vias(box, rules['drill'] / 2 + rules['via_pad_gap'])
            if net:
                pad.SetNet(nets[net])
                # Small two-pad parts get thermal spokes into the ground pour so both ends heat
                # evenly in reflow (a solid pour on one end can stand the part up).
                if net == gnd and len(p.pins) == 2 and any(k in p.fp for k in ('_0402_', '_0603_', '_0805_')):
                    pad.SetLocalZoneConnection(pcbnew.ZONE_CONNECTION_THERMAL)
                rt.add_copper(layers, box, netid[net])
                pads.setdefault(net, []).append((layers, box, p.ref, num))
            else:
                rt.add_copper(layers, box, -1, rules['clearance'] + rules['track'] / 2 + 0.04)
        missing = set(k for k in p.pins if k not in seen)
        if missing:
            raise SystemExit(f'{p.ref}: pins {sorted(missing)} have no pad in {p.fp}')

    for (x, y, dia) in d.holes:
        fp = pcbnew.FootprintLoad(f'{d.outdir}/shutter.pretty', f'NPTH_{dia}mm')
        fp.SetReference(f'H{len(fps)}'); fp.SetPosition(P(x, y))
        fp.SetAttributes(pcbnew.FP_BOARD_ONLY | pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
        b.Add(fp); fps[f'H{len(fps)}'] = fp
        rr = dia / 2
        rt.add_copper((0, 1), (x - rr, y - rr, x + rr, y + rr), -1, 0.35 + rules['track'] / 2)
    for box in d.keepouts:
        rt.add_copper((0, 1), box, -1, rules['track'] / 2 + 0.1)
    for box in getattr(d, 'back_blocks', []):     # no back-layer tracks or vias here (keeps the pour light-tight)
        rt.add_copper((1,), box, -1, 0.0)

    def add_track(l, a, c, net):
        t = pcbnew.PCB_TRACK(b); t.SetStart(P(*a)); t.SetEnd(P(*c)); t.SetWidth(mm(rules['track']))
        t.SetLayer(pcbnew.F_Cu if l == 0 else pcbnew.B_Cu); t.SetNet(nets[net]); b.Add(t)
    def add_via(x, y, net):
        v = pcbnew.PCB_VIA(b); v.SetPosition(P(x, y)); v.SetDrill(mm(rules['drill'])); v.SetWidth(mm(rules['via']))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNet(nets[net]); b.Add(v)
    def emit(path, net):
        rt.commit(path, netid[net])
        draw(path, net)
    def draw(path, net):
        g = rt.g
        vias = [(x, y) for k, (l, x, y) in enumerate(path) if k and path[k - 1][0] != l]
        for (x, y) in vias:
            add_via(x * g, y * g, net)
        i = 0
        while i < len(path) - 1:
            l, x, y = path[i]
            if path[i + 1][0] != l:
                i += 1
                continue
            dx, dy = path[i + 1][1] - x, path[i + 1][2] - y
            j = i + 1
            while j < len(path) - 1 and path[j + 1][0] == l and (path[j + 1][1] - path[j][1], path[j + 1][2] - path[j][2]) == (dx, dy):
                j += 1
            add_track(l, (x * g, y * g), (path[j][1] * g, path[j][2] * g), net)
            i = j

    # ground: give every SMD ground pad a via to the back pour
    gfail = []
    if gnd in pads:
        for q in pads[gnd]:
            if q[0] == (0, 1):
                continue
            src = rt.cells_in(0, q[1], netid[gnd])
            path = rt.search(src, None, netid[gnd], want_via=True)
            if path is None or len(path) > 200:
                gfail.append((q[2], q[3]))
                continue
            l, x, y = path[-1]
            if (x, y, netid[gnd]) in rt.vias:
                emit(path, gnd)
            else:
                emit(path + [(1, x, y)], gnd)

    failed = []
    order = [n for n in net_order if n in pads] + sorted((n for n in pads if n not in net_order),
                                                         key=lambda n: sum(q[1][2] - q[1][0] + q[1][3] - q[1][1] for q in pads[n]) + 1000 * len(pads[n]))
    # Pads that got boxed in on an earlier attempt are linked to their nearest neighbour on the
    # same net before anything else is routed.
    ctr = lambda q: ((q[1][0] + q[1][2]) / 2, (q[1][1] + q[1][3]) / 2)
    prelinks = {}
    for (net, ref, pin) in first_pads:
        qa = next((q for q in pads.get(net, []) if (q[2], q[3]) == (ref, pin)), None)
        if qa is None:
            continue
        others = sorted((q for q in pads[net] if q is not qa and math.dist(ctr(q), ctr(qa)) > 0.05),
                        key=lambda q: math.dist(ctr(q), ctr(qa)))
        for qb in others[:3]:
            src = [c for l in qa[0] for c in rt.cells_in(l, qa[1], netid[net])]
            dst = [c for l in qb[0] for c in rt.cells_in(l, qb[1], netid[net])]
            path = rt.search(src, dst, netid[net])
            if path is not None:
                emit(path, net)
                prelinks.setdefault(net, []).append(((qa[2], qa[3]), (qb[2], qb[3]), path + src + dst))
                break
    # Rip-up and re-route: when a pad cannot be reached, the nets with copper next to it are
    # pulled up, the stuck net goes first, and the pulled nets are routed again after it.
    snap = ([a[:] for a in rt.own], [a[:] for a in rt.ins], list(rt.vias))
    routed = {}

    def rebuild():
        rt.own = [a[:] for a in snap[0]]; rt.ins = [a[:] for a in snap[1]]; rt.vias = list(snap[2])
        for n, paths in routed.items():
            for p in paths:
                rt.commit(p, netid[n])

    def route_net(net):
        plist = pads[net]
        groups = {}
        for q in plist:   # pads at the same spot (e.g. USB A/B rows) count once
            groups.setdefault((round(q[1][0], 2), round(q[1][1], 2)), q)
        plist = list(groups.values())
        fails = []
        if len(plist) < 2:
            return fails
        done = [plist[0]]
        todo = plist[1:]
        tree = [c for l in plist[0][0] for c in rt.cells_in(l, plist[0][1], netid[net])]
        links = list(prelinks.get(net, []))

        def absorb():       # pull in pads already joined to the tree by a pre-routed link
            changed = True
            while changed:
                changed = False
                have = {(z[2], z[3]) for z in done}
                for lk in list(links):
                    if lk[0] in have or lk[1] in have:
                        links.remove(lk)
                        tree.extend(lk[2])
                        for z in list(todo):
                            if (z[2], z[3]) in (lk[0], lk[1]):
                                todo.remove(z); done.append(z)
                        changed = True
        absorb()
        while todo:
            todo.sort(key=lambda q: min(math.dist(ctr(q), ctr(z)) for z in done))
            q = todo.pop(0)
            target = [c for l in q[0] for c in rt.cells_in(l, q[1], netid[net])]
            path = rt.search(tree, target, netid[net])
            if path is None:
                # the pad may be fine and the tree end boxed in, so remember both ends
                ends = sorted(done, key=lambda z: math.dist(ctr(z), ctr(q)))[:2]
                fails.append((net, q[2], q[3], [ctr(q)] + [ctr(z) for z in ends]))
                if os.environ.get('ROUTE_DEBUG'):
                    cx, cy = ctr(q); gx, gy = int(round(cx / rt.g)), int(round(cy / rt.g))
                    print('FAIL', net, q[2], q[3], 'target ok cells', sum(rt.ok(c[0], c[2] * rt.W + c[1], netid[net]) for c in target), 'of', len(target))
                    names = {v: k for k, v in netid.items()}
                    seen = {}
                    for yy in range(gy - 14, gy + 15):
                        row = ''
                        for xx in range(gx - 22, gx + 23):
                            v = rt.own[0][yy * rt.W + xx]
                            ch = '.' if v == 0 else '#' if v == -1 else '*' if v == netid[net] else seen.setdefault(v, chr(97 + len(seen)))
                            row += ch
                        print(row)
                    print({c: names.get(v) for v, c in seen.items()})
            else:
                rt.commit(path, netid[net])
                routed.setdefault(net, []).append(path)
                tree += path
            tree += target
            done.append(q)
            absorb()
        return fails

    queue = [n for n in order if n != gnd and n not in no_route]
    last_fail, rips, total, reach = {}, {}, 0, rules.get('rip_radius', 1.2)
    while queue:
        net = queue.pop(0)
        fails = route_net(net)
        last_fail[net] = fails
        if not fails or rips.get(net, 0) >= 4 or total >= rules.get('max_rips', 60):
            continue
        g = rt.g
        near = set()
        spots = [c for f in fails for c in f[3]]
        for n, paths in routed.items():
            if n != net and any(abs(x * g - cx) < reach and abs(y * g - cy) < reach
                                for p in paths for (_, x, y) in p for (cx, cy) in spots):
                near.add(n)
        if not near:
            continue
        rips[net] = rips.get(net, 0) + 1
        total += 1
        for n in near | {net}:
            routed.pop(n, None)
        rebuild()
        queue = [net] + [n for n in order if n in near] + [n for n in queue if n not in near and n != net]
    failed = [(n, f[1], f[2]) for n in last_fail for f in last_fail[n]]
    for n, paths in routed.items():
        for p in paths:
            draw(p, n)

    # pours
    for (netname, layers, box) in (pours or [(gnd, (pcbnew.F_Cu, pcbnew.B_Cu), (0, 0, W, H))]):
        z = pcbnew.ZONE(b); z.SetNet(nets[netname])
        ls = pcbnew.LSET()
        for l in layers: ls.AddLayer(l)
        z.SetLayerSet(ls)
        o = z.Outline(); o.NewOutline()
        for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])): o.Append(mm(x), mm(y))
        z.SetLocalClearance(mm(0.25)); z.SetMinThickness(mm(0.2)); z.SetThermalReliefGap(mm(0.2)); z.SetThermalReliefSpokeWidth(mm(0.25))
        z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
        b.Add(z)
    for box in d.keepouts:
        z = pcbnew.ZONE(b); z.SetIsRuleArea(True)
        ls = pcbnew.LSET(); ls.AddLayer(pcbnew.F_Cu); ls.AddLayer(pcbnew.B_Cu); z.SetLayerSet(ls)
        z.SetDoNotAllowZoneFills(True); z.SetDoNotAllowTracks(True); z.SetDoNotAllowVias(True)
        z.SetDoNotAllowPads(False); z.SetDoNotAllowFootprints(False)
        o = z.Outline(); o.NewOutline()
        for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])): o.Append(mm(x), mm(y))
        b.Add(z)

    for item in d.silk:
        kind = item[0]
        layer = pcbnew.F_SilkS if item[1] == 'F' else pcbnew.B_SilkS
        if kind == 'line':
            line(*item[2:6], layer=layer, w=0.15)
        else:
            t = pcbnew.PCB_TEXT(b); t.SetText(item[4]); t.SetPosition(P(item[2], item[3])); t.SetLayer(layer)
            sz = item[5] if len(item) > 5 else 1.0
            t.SetTextSize(pcbnew.VECTOR2I(mm(sz), mm(sz))); t.SetTextThickness(mm(0.15))
            if item[1] == 'B': t.SetMirrored(True)
            b.Add(t)
    if extra:
        extra(b, pcbnew, nets, add_track, add_via)

    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    islands = []
    for z in b.Zones():
        if z.GetIsRuleArea():
            continue
        for layer in (pcbnew.B_Cu,):
            ps = z.GetFilledPolysList(layer)
            areas = sorted(ps.Outline(i).Area() / 1e12 for i in range(ps.OutlineCount()))
            islands += [round(a, 1) for a in areas[:-1]]
    pcbnew.SaveBoard(f'{d.outdir}/{d.name}.kicad_pcb', b)
    return failed, gfail + [('back-pour-island', a) for a in islands]
