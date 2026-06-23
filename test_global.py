import numpy as np
from PIL import Image

def remove_bg_global(img: Image.Image, tolerance: int = 18) -> Image.Image:
    if img.mode not in ("RGBA", "RGB"):
        img = img.convert("RGBA")
    
    arr = np.array(img.convert("RGBA"))
    height, width = arr.shape[:2]
    
    # Get border pixels
    top = arr[0, :, :3]
    bottom = arr[height-1, :, :3]
    left = arr[:, 0, :3]
    right = arr[:, width-1, :3]
    
    borders = np.concatenate([top, bottom, left, right], axis=0)
    # Only consider opaque-ish pixels if alpha exists
    if arr.shape[2] == 4:
        alpha_borders = np.concatenate([arr[0, :, 3], arr[height-1, :, 3], arr[:, 0, 3], arr[:, width-1, 3]])
        borders = borders[alpha_borders > 10]
        
    if len(borders) == 0:
        return img
        
    colors, counts = np.unique(borders, axis=0, return_counts=True)
    bg_color = colors[np.argmax(counts)]
    
    rgb_f = arr[:, :, :3].astype(np.float32)
    color_diff = np.sqrt(((rgb_f - bg_color[None, None, :]) ** 2).sum(axis=2))
    
    mask = color_diff <= tolerance
    
    result = arr.copy()
    result[mask, :3] = 0
    result[mask, 3] = 0
    return Image.fromarray(result, mode="RGBA")

img_path = r"C:\Users\Thuy\Pictures\duck\14205.png"
img = Image.open(img_path)
out = remove_bg_global(img, 18)
out.save("test_global_out.png")
print("Saved test_global_out.png")
