#!/usr/bin/env python3
"""Fill in pin names on a multi-unit KiCad symbol using ddr5_udimm_pinmap.csv.

Usage:
    python apply_ddr5_pin_names.py LIBRARY.kicad_sym SYMBOL_NAME [PINMAP.csv]

- Close KiCad first. A backup LIBRARY.kicad_sym.bak is written before any change.
- Only pin NAMES are changed. Pin numbers, positions, types and units are untouched.
"""
import csv, shutil, sys, os, re


def match_paren(text, start):
    """Index just past the ')' that closes the '(' at text[start]. Skips quoted strings."""
    depth, i, in_str = 0, start, False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == '\\':
                i += 1
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    raise ValueError("unbalanced parentheses")


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    lib_path, sym_name = sys.argv[1], sys.argv[2]
    here = os.path.dirname(os.path.abspath(__file__))
    csv_path = sys.argv[3] if len(sys.argv) > 3 else os.path.join(here, "ddr5_udimm_pinmap.csv")

    names = {}
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            names[row["pin"]] = row["name"]
    assert len(names) == 288, "pin map should have 288 rows"

    text = open(lib_path, encoding="utf-8").read()
    m = re.search(r'\(symbol\s+"%s"' % re.escape(sym_name), text)
    if not m:
        sys.exit(f'Symbol "{sym_name}" not found in {lib_path}')
    s_start = m.start()
    s_end = match_paren(text, s_start)
    sym = text[s_start:s_end]

    out, pos, done, missing = [], 0, set(), []
    for pm in re.finditer(r'\(pin\s', sym):
        if pm.start() < pos:
            continue
        p_end = match_paren(sym, pm.start())
        blk = sym[pm.start():p_end]
        num = re.search(r'\(number\s+"([^"]*)"', blk)
        if not num or num.group(1) not in names:
            missing.append(num.group(1) if num else "?")
            continue
        new = names[num.group(1)]
        blk = re.sub(r'\(name\s+"[^"]*"', '(name "%s"' % new, blk, count=1)
        out.append((pm.start(), p_end, blk))
        done.add(num.group(1))
        pos = p_end

    for a, b, blk in reversed(out):
        sym = sym[:a] + blk + sym[b:]

    shutil.copyfile(lib_path, lib_path + ".bak")
    open(lib_path, "w", encoding="utf-8").write(text[:s_start] + sym + text[s_end:])
    print(f"Renamed {len(done)} pins in symbol {sym_name!r}.")
    if len(done) != 288:
        print("WARNING: expected 288. Pins not renamed:", sorted(set(names) - done, key=int))
    if missing:
        print("Pins in the symbol with numbers not in the map:", missing)
    print("Backup written to", lib_path + ".bak")


if __name__ == "__main__":
    main()