import numpy as np
from PIL import Image

img_path = r"C:\Users\Thuy\Pictures\duck\14205.png"
img = Image.open(img_path).convert("RGBA")
arr = np.array(img)
alpha = arr[:, :, 3]
height, width = arr.shape[:2]

top_border = np.zeros((height, width), dtype=bool)
top_border[0, :] = True
bot_border = np.zeros((height, width), dtype=bool)
bot_border[height - 1, :] = True
lft_border = np.zeros((height, width), dtype=bool)
lft_border[:, 0] = True
rgt_border = np.zeros((height, width), dtype=bool)
rgt_border[:, width - 1] = True

edge_defs = [
    ("Top", arr[0, :, :3], alpha[0, :]),
    ("Bottom", arr[height - 1, :, :3], alpha[height - 1, :]),
    ("Left", arr[:, 0, :3], alpha[:, 0]),
    ("Right", arr[:, width - 1, :3], alpha[:, width - 1]),
]

for name, edge_rgb, edge_alpha in edge_defs:
    opaque_mask = edge_alpha > 10
    opaque_pixels = edge_rgb[opaque_mask].astype(np.float32)
    if len(opaque_pixels) < 5:
        print(f"{name} edge: not enough opaque pixels")
        continue
    local_bg = np.median(opaque_pixels, axis=0)
    local_diff = np.sqrt(((opaque_pixels - local_bg[None, :]) ** 2).sum(axis=1))
    consistent = float(np.mean(local_diff <= 24.0))
    print(f"{name} edge: local_bg={local_bg}, consistency={consistent:.2f}")
