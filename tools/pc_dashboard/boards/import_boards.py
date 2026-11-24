"""Imports owner's board drawings from docs/boards (2026-11-24) into tools/pc_dashboard/boards.
Per board: <slug>-light[-vN]-<uuid>.svg + <slug>-dark[-vN]-<uuid>.svg (highest vN wins, "-rgb" variants skipped).
Both are cleaned with svgclean.py; the dark one is put onto the light one's coordinates by matching the two
drawings' bounding boxes (the boards are traced from the same product shot, only offset / scaled), one shared box.
Prints the light bbox per board (the units for zones / tags / headers in boards.js).
  py import_boards.py [slug ...]"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "..", "..", "docs", "boards"))
CLEAN = os.path.join(HERE, "svgclean.py")
# suffixes after the theme: -vN (newer wins), -logo (with the board's logo: wins over plain), -from-light (a dark one
# owner derived from the light drawing, same coordinates), -rgb (skipped)
pat = re.compile(r"^(.+?)-(light|dark)(-rgb)?(?:-v(\d+))?(-logo)?(?:-from-light)?-[0-9a-f-]{36}\.svg$")

pairs = {}
for f in os.listdir(SRC):
    m = pat.match(f)
    if not m or m[3]:
        continue
    slug, theme, ver = m[1], m[2], int(m[4] or 1) + (100 if m[5] else 0)
    cur = pairs.setdefault(slug, {}).get(theme)
    if not cur or ver > cur[0]:
        pairs[slug][theme] = (ver, os.path.join(SRC, f))


def clean(src, dst, *extra):
    out = subprocess.run([sys.executable, CLEAN, src, dst, *extra], capture_output=True, text=True, check=True).stdout
    return [float(v) for v in re.search(r"bbox ([\d.\-]+) ([\d.\-]+) ([\d.\-]+) ([\d.\-]+)\s*$", out.strip()).groups()]


want = sys.argv[1:]
for slug in sorted(pairs):
    if want and slug not in want:
        continue
    p = pairs[slug]
    if "light" not in p or "dark" not in p:
        print("skip (one theme only):", slug)
        continue
    lo = os.path.join(HERE, f"{slug}-light.svg")
    do = os.path.join(HERE, f"{slug}-dark.svg")
    L = clean(p["light"][1], lo)
    D = clean(p["dark"][1], do)
    a, d = (L[2] - L[0]) / (D[2] - D[0]), (L[3] - L[1]) / (D[3] - D[1])
    e, f = L[0] - a * D[0], L[1] - d * D[1]
    box = ",".join(f"{v:g}" for v in L)
    clean(p["light"][1], lo, "--box", box)
    clean(p["dark"][1], do, "--affine", f"{a:.5f},{d:.5f},{e:.2f},{f:.2f}", "--box", box)
    print(f"{slug:32s} box {box:28s} dark scale {a:.4f} / {d:.4f}  {os.path.getsize(lo)//1024} / {os.path.getsize(do)//1024} KB")
