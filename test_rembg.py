import sys
try:
    from rembg import remove
    from PIL import Image

    img_path = r"C:\Users\Thuy\Pictures\duck\14205.png"
    img = Image.open(img_path)
    print("Running rembg...")
    output = remove(img)
    output.save("test_rembg_out.png")
    print("Saved test_rembg_out.png")
except ImportError:
    print("rembg is not installed.")
