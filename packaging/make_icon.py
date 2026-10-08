# Draws Murmur's icon (original design: green rounded square, cream waveform bars).
import sys
from PIL import Image, ImageDraw
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
pad = 88
top, bot = (79, 148, 99), (44, 102, 64)
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / S
    gd.line([(0, y), (S, y)], fill=tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)) + (255,))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([pad, pad, S - pad, S - pad], radius=190, fill=255)
img.paste(grad, (0, 0), mask)
bars = [0.28, 0.5, 0.78, 0.6, 0.92, 0.55, 0.36]
w, gap = 62, 34
x0 = (S - (len(bars) * w + (len(bars) - 1) * gap)) // 2
for i, h in enumerate(bars):
    bh = int(h * 520)
    x = x0 + i * (w + gap)
    d.rounded_rectangle([x, S // 2 - bh // 2, x + w, S // 2 + bh // 2], radius=w // 2, fill=(251, 248, 238, 255))
img.save(sys.argv[1])
