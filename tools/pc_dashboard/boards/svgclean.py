"""Cleans a vectorized board SVG (Illustrator / Adobe image trace) for boards.js (2026-11-23).
Drops the metadata and the first path (the canvas background with the board cut out), rounds coordinates to 1 decimal,
collapses Adobe's straight segments written as cubics, crops the viewBox to the board and prints the board's bbox.
  py svgclean.py in.svg out.svg [--affine a,d,e,f] [--box x0,y0,x1,y1]
--affine maps x -> a*x + e, y -> d*y + f (to put a second theme's drawing onto the first one's coordinates);
--box sets the viewBox (both themes of a board share one)."""
import re
import sys

src, dst = sys.argv[1], sys.argv[2]
opt = lambda k: [float(v) for v in sys.argv[sys.argv.index(k) + 1].split(",")] if k in sys.argv else None
aff, box = opt("--affine"), opt("--box")
s = open(src, encoding="utf-8").read()
paths = re.findall(r'<path d="([^"]+)"\s+fill="([^"]+)"\s*/>', s)
paths = paths[1:]                      # background

num = re.compile(r"-?\d+(?:\.\d+)?")
def affine(d):
    if not aff:
        return d
    a, dd, e, f = aff
    return re.sub(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", lambda m: f"{a * float(m[1]) + e},{dd * float(m[2]) + f}", d)

def rnd(m):
    v = round(float(m.group(0)), 1)
    return ("%d" % v) if v == int(v) else ("%.1f" % v)

def line_cubics(d):
    # "C a,b a,b c,d" (control points = endpoints) is a straight line -> "L c,d"
    return re.sub(r"C(-?[\d.]+),(-?[\d.]+) \1,\2 (-?[\d.]+),(-?[\d.]+)", r"L\3,\4", d)

xs, ys, out = [], [], []
for d, fill in paths:
    d = num.sub(rnd, affine(d))
    d = line_cubics(d)
    d = re.sub(r"\s*([MLCZ])\s*", r"\1", d).strip()
    v = [float(t) for t in num.findall(d)]
    xs += v[0::2]
    ys += v[1::2]
    m = re.match(r"rgb\((\d+),(\d+),(\d+)\)", fill)
    fill = "#%02x%02x%02x" % tuple(int(c) for c in m.groups()) if m else fill
    out.append(f'<path d="{d}" fill="{fill}"/>')
x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
print("drawing bbox", x0, y0, x1, y1)
if box:
    x0, y0, x1, y1 = box
open(dst, "w", encoding="utf-8").write(
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:g} {y0:g} {x1 - x0:g} {y1 - y0:g}" width="{x1 - x0:g}" height="{y1 - y0:g}">'
    + "".join(out) + "</svg>")
print(dst, len(out), "paths, bbox", x0, y0, x1, y1)
