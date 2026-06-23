import numpy as np
from PIL import Image

img_path = r"C:\Users\Thuy\Pictures\duck\14205.png"
img = Image.open(img_path).convert("RGBA")
arr = np.array(img)
h, w = arr.shape[:2]

top = arr[0, :, :]
bottom = arr[h-1, :, :]
left = arr[:, 0, :]
right = arr[:, w-1, :]

borders = np.concatenate([top, bottom, left, right], axis=0)
colors, counts = np.unique(borders, axis=0, return_counts=True)
top_colors = colors[np.argsort(-counts)][:5]
top_counts = np.sort(counts)[::-1][:5]

print("Top 5 border colors:")
for c, count in zip(top_colors, top_counts):
    print(f"Color {c}: count {count}")
