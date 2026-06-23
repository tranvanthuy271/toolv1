import os
from PIL import Image
import numpy as np

img_path = r"C:\Users\Thuy\Pictures\duck\14205.png"
print(f"Loading {img_path}")
img = Image.open(img_path).convert("RGBA")
arr = np.array(img)

print(f"Top-left: {arr[0, 0]}")
print(f"Top-right: {arr[0, -1]}")
print(f"Bottom-left: {arr[-1, 0]}")
print(f"Bottom-right: {arr[-1, -1]}")

colors, counts = np.unique(arr.reshape(-1, 4), axis=0, return_counts=True)
top_colors = colors[np.argsort(-counts)][:5]
top_counts = np.sort(counts)[::-1][:5]
print("Top 5 colors:")
for c, count in zip(top_colors, top_counts):
    print(f"Color {c}: count {count}")
