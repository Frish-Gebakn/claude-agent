"""Generate app_icon.ico -- a Claude-style sunburst on a cream rounded square."""
import math
from pathlib import Path
from PIL import Image, ImageDraw

S = 512
CREAM = (240, 238, 230, 255)
CORAL = (217, 119, 87, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# rounded-square background
pad = 24
d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=96, fill=CREAM)

# sunburst: tapered petals radiating from the center
c = S / 2
R = S * 0.30           # outer radius
r0 = S * 0.055         # inner radius (small gap at center)
spokes = 12
half = math.radians(7)  # angular half-width of each petal base

for i in range(spokes):
    a = math.radians(i * 360 / spokes)
    tip = (c + R * math.cos(a), c + R * math.sin(a))
    b1 = (c + r0 * math.cos(a - half), c + r0 * math.sin(a - half))
    b2 = (c + r0 * math.cos(a + half), c + r0 * math.sin(a + half))
    d.polygon([b1, tip, b2], fill=CORAL)

# small solid center
d.ellipse([c - r0, c - r0, c + r0, c + r0], fill=CORAL)

out = Path(__file__).resolve().parent / "app_icon.ico"
img.save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", out)
