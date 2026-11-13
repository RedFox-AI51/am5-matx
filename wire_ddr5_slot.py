#!/usr/bin/env python3
"""Wire one DDR5 DIMM slot on a KiCad 10 schematic sheet.

Usage:
    python wire_ddr5_slot.py DDR5_SHEET.kicad_sch REF PREFIX [--pwr-start N] [--dry-run]
Example:
    python wire_ddr5_slot.py ddr5.kicad_sch J4 CHA --pwr-start 200

What it adds for every pin of symbol REF (all units):
  VSS               -> GND power symbol
  VIN_BULK          -> +5V power symbol
  RFU, CB*          -> no-connect flag   (non-ECC build: check bits are not used)
  DQ/DQS/DM/CA/CS/CK/RESET_n/ALERT_n -> net label PREFIX_<sub>_<signal>
  HSCL/HSDA/HAS/PWR_EN/PWR_GOOD -> left alone for now (sideband, wire later)

Label rule: the module's trailing _A/_B (subchannel) moves after the prefix:
  DQ5_B -> CHA_B_DQ5     CK0_A_t -> CHA_A_CK0_t     RESET_n -> CHA_RESET_n

Close KiCad first. power_input.kicad_sch must sit next to the sheet (its GND and +5V
symbol definitions are copied into this sheet). A .bak copy is written first.
Safe to re-run: pins that already have a label, flag or power symbol are skipped.
"""
import argparse, math, os, re, shutil, sys, uuid

# ---------- minimal s-expression parser ----------
def tokenize(s):
    i, n, out = 0, len(s), []
    while i < n:
        c = s[i]
        if c in ' \t\r\n': i += 1
        elif c in '()': out.append(c); i += 1
        elif c == '"':
            j, buf = i + 1, []
            while s[j] != '"':
                if s[j] == '\\': j += 1
                buf.append(s[j]); j += 1
            out.append(('s', ''.join(buf))); i = j + 1
        else:
            j = i
            while j < n and s[j] not in ' \t\r\n()': j += 1
            out.append(('a', s[i:j])); i = j
    return out

def parse(s):
    toks, pos = tokenize(s), 0
    def rd():
        nonlocal pos
        t = toks[pos]
        if t == '(':
            pos += 1; lst = []
            while toks[pos] != ')': lst.append(rd())
            pos += 1; return lst
        pos += 1; return t[1]
    return rd()

def find(node, key): return [c for c in node if isinstance(c, list) and c and c[0] == key]
def first(node, key):
    r = find(node, key); return r[0] if r else None
def prop(sym, name):
    for p in find(sym, 'property'):
        if p[1] == name: return p[2]

def match_paren(text, start):
    d, i, ins = 0, start, False
    while i < len(text):
        c = text[i]
        if ins:
            if c == '\\': i += 1
            elif c == '"': ins = False
        elif c == '"': ins = True
        elif c == '(': d += 1
        elif c == ')':
            d -= 1
            if d == 0: return i + 1
        i += 1
    raise ValueError('unbalanced parentheses')

def num(v):
    v = round(v, 4)
    return ('%.4f' % v).rstrip('0').rstrip('.') if v != int(v) else str(int(v))

def uid(): return str(uuid.uuid4())

def endpoint(px, py, at, rot):
    x, y = px, -py
    th = math.radians(rot)
    return (round(at[0] + x * math.cos(th) + y * math.sin(th), 4),
            round(at[1] - x * math.sin(th) + y * math.cos(th), 4))

def outward(angle_pin, rot):
    """Unit vector (screen coords) pointing away from the symbol body at this pin."""
    th = math.radians(angle_pin)
    vx, vy = math.cos(th), -math.sin(th)          # direction toward the body, y flipped
    r = math.radians(rot)
    wx = vx * math.cos(r) + vy * math.sin(r)
    wy = -vx * math.sin(r) + vy * math.cos(r)
    return (-round(wx), -round(wy))

# ---------- pin classification ----------
SIDEBAND = {'HSCL', 'HSDA', 'HAS', 'PWR_EN', 'PWR_GOOD'}
def classify(name):
    if name == 'VSS': return 'gnd'
    if name == 'VIN_BULK': return 'v5'
    if name == 'RFU' or re.match(r'^CB\d+_[AB]$', name): return 'nc'
    if name in SIDEBAND: return 'skip'
    return 'signal'

def net_name(prefix, name):
    m = re.match(r'^(.*?)_([AB])(_[tcn])?$', name)
    if m: return '%s_%s_%s%s' % (prefix, m.group(2), m.group(1), m.group(3) or '')
    return '%s_%s' % (prefix, name)          # RESET_n, ALERT_n

# ---------- text templates (copied from KiCad 10 output) ----------
def tpl_label(text, x, y, ang):
    just = 'right bottom' if ang in (180, 270) else 'left bottom'
    return ('\t(label "%s"\n\t\t(at %s %s %d)\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n'
            '\t\t\t(justify %s)\n\t\t)\n\t\t(uuid "%s")\n\t)\n') % (text, num(x), num(y), ang, just, uid())

def tpl_nc(x, y):
    return '\t(no_connect\n\t\t(at %s %s)\n\t\t(uuid "%s")\n\t)\n' % (num(x), num(y), uid())

def tpl_power(lib, value, desc, x, y, rot, refnum, project, path, hide_value):
    hv = '\n\t\t\t(hide yes)' if hide_value else ''
    eff = '\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)'
    def p(name, val, extra=''):
        return ('\t\t(property "%s" "%s"\n\t\t\t(at %s %s 0)%s\n\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)%s\n\t\t)\n'
                % (name, val, num(x), num(y), extra, eff))
    return ('\t(symbol\n\t\t(lib_id "%s")\n\t\t(at %s %s %d)\n\t\t(unit 1)\n\t\t(body_style 1)\n\t\t(exclude_from_sim no)\n'
            '\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(in_pos_files yes)\n\t\t(dnp no)\n\t\t(fields_autoplaced yes)\n'
            '\t\t(uuid "%s")\n' % (lib, num(x), num(y), rot, uid())
            + p('Reference', '#PWR%03d' % refnum, '\n\t\t\t(hide yes)')
            + p('Value', value, hv)
            + p('Footprint', '', '\n\t\t\t(hide yes)')
            + p('Datasheet', '', '\n\t\t\t(hide yes)')
            + p('Description', desc, '\n\t\t\t(hide yes)')
            + '\t\t(pin "1"\n\t\t\t(uuid "%s")\n\t\t)\n' % uid()
            + '\t\t(instances\n\t\t\t(project "%s"\n\t\t\t\t(path "%s"\n\t\t\t\t\t(reference "#PWR%03d")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n'
            % (project, path, refnum))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sheet'); ap.add_argument('ref'); ap.add_argument('prefix')
    ap.add_argument('--pwr-start', type=int, default=200)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    text = open(a.sheet, encoding='utf-8').read()
    tree = parse(text)
    libs = {s[1]: s for s in find(first(tree, 'lib_symbols'), 'symbol')}

    insts = [s for s in find(tree, 'symbol') if first(s, 'lib_id') and prop(s, 'Reference') == a.ref]
    if not insts: sys.exit('No symbol with reference %s found in %s' % (a.ref, a.sheet))
    ins0 = first(first(insts[0], 'instances'), 'project')
    project, path = ins0[1], first(ins0, 'path')[1]

    # what is already wired (re-run safety)
    taken = set()
    for l in find(tree, 'label') + find(tree, 'no_connect'):
        at = first(l, 'at'); taken.add((round(float(at[1]), 4), round(float(at[2]), 4)))
    for s in find(tree, 'symbol'):
        lid = first(s, 'lib_id')
        if lid and lid[1] in ('power:GND', 'power:+5V'):
            at = first(s, 'at'); taken.add((round(float(at[1]), 4), round(float(at[2]), 4)))

    add, counts, skipped, pwr = [], {}, [], a.pwr_start
    for s in insts:
        at = first(s, 'at'); ax, ay = float(at[1]), float(at[2]); rot = float(at[3]) if len(at) > 3 else 0
        if first(s, 'mirror'): sys.exit('Mirrored symbols are not supported by this script.')
        unit = int(first(s, 'unit')[1]); lib = libs[first(s, 'lib_id')[1]]
        for sub in find(lib, 'symbol'):
            m = re.search(r'_(\d+)_(\d+)$', sub[1])
            if int(m.group(1)) not in (0, unit): continue
            for p in find(sub, 'pin'):
                pa = first(p, 'at'); name, number = first(p, 'name')[1], first(p, 'number')[1]
                x, y = endpoint(float(pa[1]), float(pa[2]), (ax, ay), rot)
                ox, oy = outward(float(pa[3]), rot)
                kind = classify(name)
                counts[kind] = counts.get(kind, 0) + 1
                if kind == 'skip': skipped.append('%s(pin %s)' % (name, number)); continue
                if (x, y) in taken: counts['already'] = counts.get('already', 0) + 1; continue
                if kind == 'nc': add.append(tpl_nc(x, y))
                elif kind == 'signal':
                    ang = {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}[(ox, oy)]
                    add.append(tpl_label(net_name(a.prefix, name), x, y, ang))
                else:
                    # GND body points down at rot 0, +5V points up; turn the body to point away from the DIMM
                    if kind == 'gnd':
                        r = {(-1, 0): 270, (1, 0): 90, (0, -1): 180, (0, 1): 0}[(ox, oy)]
                        add.append(tpl_power('power:GND', 'GND', 'Power symbol creates a global label with name \\"GND\\" , ground',
                                             x, y, r, pwr, project, path, True))
                    else:
                        r = {(-1, 0): 90, (1, 0): 270, (0, -1): 0, (0, 1): 180}[(ox, oy)]
                        add.append(tpl_power('power:+5V', '+5V', 'Power symbol creates a global label with name \\"+5V\\"',
                                             x, y, r, pwr, project, path, False))
                    pwr += 1

    print('Slot %s -> prefix %s' % (a.ref, a.prefix))
    print('  pins seen by class:', {k: v for k, v in sorted(counts.items())})
    print('  left for later (sideband):', ', '.join(skipped) or 'none')
    print('  new schematic items:', len(add), '| next free #PWR number:', pwr)
    if a.dry_run: return

    # copy GND / +5V symbol definitions into this sheet if missing
    src = os.path.join(os.path.dirname(os.path.abspath(a.sheet)), 'power_input.kicad_sch')
    if not os.path.exists(src): sys.exit('power_input.kicad_sch not found next to the sheet')
    srctext = open(src, encoding='utf-8').read()
    defs = ''
    for nm in ('power:GND', 'power:+5V'):
        if '(symbol "%s"' % nm not in text:
            i = srctext.index('(symbol "%s"' % nm)
            defs += '\t\t' + srctext[i:match_paren(srctext, i)] + '\n'
    if defs:
        i = text.index('(lib_symbols'); j = match_paren(text, i) - 1
        text = text[:j] + '\n' + defs + '\t' + text[j:]
    m = re.search(r'\n\t\(embedded_fonts', text)         # top-level only (one tab), not inside symbol definitions
    k = m.start() + 1 if m else text.rstrip().rfind(')')
    n_lab = sum(1 for x in add if x.startswith('\t(label'))
    n_nc = sum(1 for x in add if x.startswith('\t(no_connect'))
    n_sym = sum(1 for x in add if x.startswith('\t(symbol'))
    before = (len(find(tree, 'label')), len(find(tree, 'no_connect')), len(find(tree, 'symbol')))
    text = text[:k] + ''.join(add) + text[k:]
    t2 = parse(text)                                   # sanity: new items must be top-level children of the sheet
    after = (len(find(t2, 'label')), len(find(t2, 'no_connect')), len(find(t2, 'symbol')))
    if after != (before[0] + n_lab, before[1] + n_nc, before[2] + n_sym):
        sys.exit('Internal check failed (items not added at sheet level); nothing was written.')
    shutil.copyfile(a.sheet, a.sheet + '.bak')
    open(a.sheet, 'w', encoding='utf-8').write(text)
    print('Done. Backup:', a.sheet + '.bak')

if __name__ == '__main__':
    main()