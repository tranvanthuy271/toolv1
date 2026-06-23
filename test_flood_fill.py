import numpy as np
from PIL import Image
from sprite_core import _grow_background_region

img_path = r"C:\Users\Thuy\Pictures\duck\14205.png"
img = Image.open(img_path).convert("RGBA")
arr = np.array(img)
alpha = arr[:, :, 3]
height, width = arr.shape[:2]
existing_alpha = alpha > 10
rgb_f = arr[:, :, :3].astype(np.float32)

edge_rgb = arr[0, :, :3]
edge_alpha = alpha[0, :]
opaque_mask = edge_alpha > 10
opaque_pixels = edge_rgb[opaque_mask].astype(np.float32)

local_bg = np.median(opaque_pixels, axis=0)
local_diff = np.sqrt(((opaque_pixels - local_bg[None, :]) ** 2).sum(axis=1))
percentile = float(np.percentile(local_diff, 85))
print(f"Top edge local_bg: {local_bg}, 85th percentile diff: {percentile}")

color_diff = np.sqrt(((rgb_f - local_bg[None, None, :]) ** 2).sum(axis=2))
print(f"Color diff stats: min={color_diff.min()}, max={color_diff.max()}, median={np.median(color_diff)}")

tolerance = 18.0
local_tol = max(float(tolerance), percentile + 8.0)
local_tol = min(local_tol, float(tolerance) * 3.5)
print(f"Calculated local_tol: {local_tol}")

candidate = (color_diff <= local_tol) & existing_alpha
print(f"Candidate pixels count: {candidate.sum()}")
