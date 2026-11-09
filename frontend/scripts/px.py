"""采样截图上的像素颜色，用来客观判断「卡片是否发灰」「对比度是否够」。
用法：python scripts/px.py <png> x,y [x,y ...]   （坐标为图片像素坐标）
     python scripts/px.py <png> --row y x1 x2      （沿一行取样，看渐变）
"""
import sys
from PIL import Image

path = sys.argv[1]
im = Image.open(path).convert("RGB")
print(f"image {im.size[0]}x{im.size[1]}")

if sys.argv[2] == "--row":
    y = int(sys.argv[3])
    xs = [int(v) for v in sys.argv[4:]]
    for x in xs:
        r, g, b = im.getpixel((x, y))
        print(f"  ({x:4d},{y:4d}) #{r:02X}{g:02X}{b:02X}")
else:
    for arg in sys.argv[2:]:
        x, y = (int(v) for v in arg.split(","))
        r, g, b = im.getpixel((x, y))
        # 相对亮度，用于快速判 WCAG
        def lin(c):
            c /= 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        L = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
        print(f"  ({x:4d},{y:4d}) #{r:02X}{g:02X}{b:02X}  L={L:.4f}  Lpct={L * 100:.2f}")
