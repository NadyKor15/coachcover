"""Region-by-region diff of screen C: render of the approved variant-C.html vs render of web/index.html?fixture=1.

Usage: python3 tests/web/compare_regions.py web/shots/ref-variant-C.png web/shots/C-fixture-1920.png
Blocks below the first card row are compared 55 px lower: the page is 1080 high, the approved one 1024.
MAE is the mean brightness difference (0 = identical), edgeF1 the match of contours (1 = identical). Needs Pillow."""
import sys
from PIL import Image, ImageChops, ImageStat, ImageFilter
ref, ren = Image.open(sys.argv[1]).convert('L'), Image.open(sys.argv[2]).convert('L')
D = 55
# name, ref box (x0,y0,x1,y1), dy in our render, expected: same / edit
R = [
 ('header',                 (0, 0, 1920, 64), 0, 'same'),
 ('sidebar nav',            (0, 66, 244, 560), 0, 'edit: menu order + Cases'),
 ('sidebar bottom',         (0, 600, 244, 976), 0, 'edit: judge mode'),
 ('h1 title',               (270, 66, 1900, 143), 0, 'edit: title'),
 ('band photo + timeline',  (270, 144, 1900, 326), 0, 'edit: Sofia Reyes in place of Anna Kim'),
 ('call-out card: head+voice', (271, 343, 837, 505), 0, 'same'),
 ('call-out card: text',    (271, 505, 837, 638), 0, 'edit: RU + EN + whisper line'),
 ('staff card: title+dots', (856, 343, 1598, 555), 0, 'same'),
 ('staff card: chips',      (856, 555, 1598, 638), 0, 'chip widths from data'),
 ('right column cards',     (1621, 343, 1900, 760), 0, 'same'),
 ('shift card',             (271, 656, 837, 786), D, 'same (moved down 55)'),
 ('offers card',            (856, 656, 1598, 786), D, 'edit: Sofia + why first'),
 ('covered banner',         (271, 806, 1600, 873), D, 'same (moved down 55)'),
 ('plan B',                 (271, 885, 1602, 960), D, 'same (moved down 55)'),
 ('footer model log',       (16, 980, 900, 1020), D, 'same (moved down 55)'),
]
def edge(im):
    return im.filter(ImageFilter.FIND_EDGES).point(lambda x: 255 if x > 40 else 0)
def f1(a, b):
    ea, eb = edge(a), edge(b)
    da, db = ea.filter(ImageFilter.MaxFilter(5)), eb.filter(ImageFilter.MaxFilter(5))
    cnt = lambda im: sum(1 for p in im.getdata() if p)
    na, nb = cnt(ea), cnt(eb)
    if na == 0 and nb == 0: return 1.0
    p = cnt(ImageChops.multiply(eb, da)) / max(1, nb); r = cnt(ImageChops.multiply(ea, db)) / max(1, na)
    return round(2 * p * r / max(1e-9, p + r), 3)
print(f"{'region':28} {'MAE':>6} {'edgeF1':>7}  expected")
for name, (x0, y0, x1, y1), dy, exp in R:
    a = ref.crop((x0, y0, x1, y1)); b = ren.crop((x0, y0 + dy, x1, y1 + dy))
    mae = ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
    print(f"{name:28} {mae:6.2f} {f1(a, b):7.3f}  {exp}")
