import numpy as np
from PIL import Image

img_path = r"C:\Users\Thuy\Pictures\duck\14205.png"
img = Image.open(img_path).convert("RGBA")
# Resize to 80x40
img_small = img.resize((80, 40))
arr = np.array(img_small)
chars = " .:-=+*#%@"
for y in range(40):
    row = ""
    for x in range(80):
        r, g, b, a = arr[y, x]
        if a < 128:
            row += " "
        else:
            intensity = int((r + g + b) / 3 / 256 * 10)
            row += chars[min(9, intensity)]
    print(row)
