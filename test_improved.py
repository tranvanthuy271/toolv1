import numpy as np
from PIL import Image
from sprite_core import _grow_background_region

def remove_bg_improved(img: Image.Image, tolerance: int = 18) -> Image.Image:
    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    height, width = arr.shape[:2]

    if height == 0 or width == 0:
        return rgba.copy()

    border_mask = np.zeros((height, width), dtype=bool)
    border_mask[0, :] = True
    border_mask[height - 1, :] = True
    border_mask[:, 0] = True
    border_mask[:, width - 1] = True

    rgb_f = arr[:, :, :3].astype(np.float32)
    alpha = arr[:, :, 3]

    borders = rgb_f[border_mask]
    border_alphas = alpha[border_mask]
    
    valid_borders = borders[border_alphas > 10]
    if len(valid_borders) == 0:
        return rgba.copy()
        
    colors, counts = np.unique(valid_borders, axis=0, return_counts=True)
    bg_color = colors[np.argmax(counts)]
    print(f"Detected bg_color: {bg_color}")
    
    color_diff = np.sqrt(((rgb_f - bg_color[None, None, :]) ** 2).sum(axis=2))
    candidate = (color_diff <= tolerance * 2) & (alpha > 10)
    seed = border_mask & candidate
    
    if seed.any():
        background_mask = _grow_background_region(candidate, seed)
        result = arr.copy()
        result[background_mask, :3] = 0
        result[background_mask, 3] = 0
        return Image.fromarray(result, mode="RGBA")
        
    return rgba.copy()

img_path = r"C:\Users\Thuy\Pictures\duck\14205.png"
img = Image.open(img_path)
out = remove_bg_improved(img, 18)
out.save("test_improved_out.png")
print("Saved test_improved_out.png")

out_arr = np.array(out)
print(f"Result alpha == 0 count: {(out_arr[:, :, 3] == 0).sum()}")
