from __future__ import annotations

import io
import os
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
IMAGE_NEAREST = Image.Resampling.NEAREST if hasattr(Image, "Resampling") else Image.NEAREST


def get_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = get_app_root()
if getattr(sys, "frozen", False):
    OUTPUT_ROOT = Path.home() / "Documents" / "WeaponSpriteAdapter" / "outputs"
else:
    OUTPUT_ROOT = APP_ROOT / "outputs"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)



def create_output_batch_dir(prefix: str, output_root: Path | None = None) -> Path:
    base_output = output_root or OUTPUT_ROOT
    base_output.mkdir(exist_ok=True, parents=True)
    batch_dir = base_output / datetime.now().strftime(f"{prefix}_%Y%m%d_%H%M%S_%f")
    batch_dir.mkdir(parents=True, exist_ok=False)
    return batch_dir

def load_rgba_image(path: str | os.PathLike[str]) -> Image.Image:
    with Image.open(path) as opened:
        return opened.convert("RGBA")


def is_png_bytes(data: bytes | bytearray) -> bool:
    return len(data) >= len(PNG_MAGIC) and data[: len(PNG_MAGIC)] == PNG_MAGIC


def reverse_client_asset_bytes(data: bytes | bytearray, prefix_size: int = 51) -> bytes:
    buffer = bytearray(data)
    if len(buffer) < prefix_size:
        return bytes(buffer)

    reversed_prefix = bytearray(prefix_size)
    for index in range(prefix_size):
        reversed_prefix[prefix_size - 1 - index] = buffer[index]
    for index in range(prefix_size):
        buffer[index] = reversed_prefix[index]
    return bytes(buffer)


def decode_client_asset_bytes(data: bytes | bytearray) -> bytes:
    if is_png_bytes(data):
        return bytes(data)

    decoded = reverse_client_asset_bytes(data)
    if is_png_bytes(decoded):
        return decoded

    raise ValueError("File is neither a PNG nor a client-encrypted .dnd image.")


def load_client_asset_image(path: str | os.PathLike[str]) -> tuple[Image.Image, str]:
    raw_data = Path(path).read_bytes()
    source_kind = "png" if is_png_bytes(raw_data) else "dnd"
    decoded = decode_client_asset_bytes(raw_data)

    with Image.open(io.BytesIO(decoded)) as opened:
        return opened.convert("RGBA"), source_kind


def scale_image_for_client(image: Image.Image, scale_level: int) -> Image.Image:
    if scale_level < 1 or scale_level > 4:
        raise ValueError("scale_level must be in the range 1..4")

    if scale_level == 4:
        return image.copy()

    target_width = max(1, image.width * scale_level // 4)
    target_height = max(1, image.height * scale_level // 4)
    return image.resize((target_width, target_height), IMAGE_NEAREST)


def get_alpha_mask(img: Image.Image, threshold: int = 20) -> np.ndarray:
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = np.array(img)[:, :, 3]
    return alpha > threshold


def _grow_background_region(candidate_mask: np.ndarray, seed_mask: np.ndarray) -> np.ndarray:
    height, width = candidate_mask.shape
    background_mask = np.zeros_like(candidate_mask, dtype=bool)
    queue = deque(zip(*np.nonzero(seed_mask)))

    while queue:
        y, x = queue.popleft()
        if background_mask[y, x] or not candidate_mask[y, x]:
            continue

        background_mask[y, x] = True

        if y > 0:
            queue.append((y - 1, x))
        if y + 1 < height:
            queue.append((y + 1, x))
        if x > 0:
            queue.append((y, x - 1))
        if x + 1 < width:
            queue.append((y, x + 1))

    return background_mask


def remove_background(img: Image.Image, tolerance: int = 18, global_remove: bool = False) -> Image.Image:
    """Remove background using flood-fill from borders with the most common border color."""
    if img.mode not in ("RGBA", "RGB"):
        img = img.convert("RGBA")

    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    height, width = arr.shape[:2]

    if height == 0 or width == 0:
        return Image.fromarray(arr, mode="RGBA")

    # Create mask for the 4 edges
    border_mask = np.zeros((height, width), dtype=bool)
    border_mask[0, :] = True
    border_mask[height - 1, :] = True
    border_mask[:, 0] = True
    border_mask[:, width - 1] = True

    rgb_f = arr[:, :, :3].astype(np.float32)
    alpha = arr[:, :, 3]

    borders = rgb_f[border_mask]
    border_alphas = alpha[border_mask]
    
    # Only consider opaque border pixels
    valid_borders = borders[border_alphas > 10]
    if len(valid_borders) == 0:
        return rgba.copy()
        
    # Find the most common color on the border
    colors, counts = np.unique(valid_borders, axis=0, return_counts=True)
    bg_color = colors[np.argmax(counts)]
    
    # Adaptive tolerance based on border color variance
    local_diff = np.sqrt(((valid_borders - bg_color[None, :]) ** 2).sum(axis=1))
    local_tol = max(float(tolerance), float(np.percentile(local_diff, 85)) + 8.0)
    local_tol = min(local_tol, float(tolerance) * 4.0)

    color_diff = np.sqrt(((rgb_f - bg_color[None, None, :]) ** 2).sum(axis=2))
    
    if global_remove:
        # Globally remove the identified background color
        candidate = (color_diff <= local_tol) & (alpha > 10)
        result = arr.copy()
        result[candidate, :3] = 0
        result[candidate, 3] = 0
        return Image.fromarray(result, mode="RGBA")
    else:
        # Use flood fill to only remove connected outer background
        candidate = (color_diff <= local_tol) & (alpha > 10)
        seed = border_mask & candidate

        if seed.any():
            background_mask = _grow_background_region(candidate, seed)
            result = arr.copy()
            result[background_mask, :3] = 0
            result[background_mask, 3] = 0
            return Image.fromarray(result, mode="RGBA")

    return rgba.copy()


def get_weapon_bbox(img: Image.Image) -> tuple[int, int, int, int]:
    mask = get_alpha_mask(img)
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return (0, 0, img.width, img.height)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def get_weapon_size(img: Image.Image) -> tuple[int, int]:
    x0, y0, x1, y1 = get_weapon_bbox(img)
    return (max(1, x1 - x0), max(1, y1 - y0))


def get_weapon_proportion(img: Image.Image) -> float:
    bbox_w, bbox_h = get_weapon_size(img)
    if img.width == 0 or img.height == 0:
        return 0.5
    return max(bbox_w / img.width, bbox_h / img.height)


def get_weapon_center_offset(img: Image.Image) -> tuple[float, float]:
    x0, y0, x1, y1 = get_weapon_bbox(img)
    if img.width == 0 or img.height == 0:
        return (0.5, 0.5)
    return (float((x0 + x1) / (2 * img.width)), float((y0 + y1) / (2 * img.height)))


def analyze_image(img: Image.Image, auto_remove_bg: bool = True, global_remove: bool = False) -> dict[str, Any]:
    working = img.convert("RGBA")
    if auto_remove_bg:
        try:
            working = remove_background(working, global_remove=global_remove)
        except Exception:
            pass

    proportion = get_weapon_proportion(working)
    object_width, object_height = get_weapon_size(working)
    center_x, center_y = get_weapon_center_offset(working)
    return {
        "width": working.width,
        "height": working.height,
        "proportion": round(proportion, 3),
        "object_size": (object_width, object_height),
        "center": (round(center_x, 3), round(center_y, 3)),
        "processed": working,
    }


def analyze_image_path(path: str | os.PathLike[str], auto_remove_bg: bool = True, global_remove: bool = False) -> dict[str, Any]:
    image = load_rgba_image(path)
    return analyze_image(image, auto_remove_bg=auto_remove_bg, global_remove=global_remove)


def save_background_removed_image(
    image_path: str | os.PathLike[str],
    output_root: Path | None = None,
    global_remove: bool = False,
) -> dict[str, Any]:
    batch_dir = create_output_batch_dir("rm_bg", output_root)
    source_image = load_rgba_image(image_path)
    output_image = remove_background(source_image, global_remove=global_remove)

    source_name = Path(image_path).stem
    output_path = batch_dir / f"{source_name}_rm_bg.png"
    output_image.save(output_path, "PNG")

    return {
        "width": output_image.width,
        "height": output_image.height,
        "output_path": str(output_path),
        "preview": output_image,
        "batch_dir": batch_dir,
    }


def adapt_weapon(
    reference_img: Image.Image,
    layout_img: Image.Image,
    auto_remove_bg: bool = True,
    global_remove: bool = False,
) -> Image.Image:
    ref_working = reference_img.convert("RGBA")
    layout_working = layout_img.convert("RGBA")

    if auto_remove_bg:
        try:
            ref_working = remove_background(ref_working, global_remove=global_remove)
        except Exception:
            pass
        try:
            layout_working = remove_background(layout_working, global_remove=global_remove)
        except Exception:
            pass

    target_width, target_height = layout_working.size
    ref_bbox = get_weapon_bbox(ref_working)
    layout_bbox = get_weapon_bbox(layout_working)
    ref_cropped = ref_working.crop(ref_bbox)

    ref_width = max(1, ref_cropped.width)
    ref_height = max(1, ref_cropped.height)
    target_weapon_width = max(1, layout_bbox[2] - layout_bbox[0])
    target_weapon_height = max(1, layout_bbox[3] - layout_bbox[1])
    scale = min(target_weapon_width / ref_width, target_weapon_height / ref_height)

    resized_width = max(1, min(target_weapon_width, int(round(ref_width * scale))))
    resized_height = max(1, min(target_weapon_height, int(round(ref_height * scale))))
    resized = ref_cropped.resize(
        (resized_width, resized_height),
        Image.Resampling.LANCZOS,
    )

    output = Image.new("RGBA", (target_width, target_height), (0, 0, 0, 0))
    paste_x = layout_bbox[0] + max(0, (target_weapon_width - resized.width) // 2)
    paste_y = layout_bbox[1] + max(0, (target_weapon_height - resized.height) // 2)
    paste_x = max(0, min(paste_x, target_width - resized.width))
    paste_y = max(0, min(paste_y, target_height - resized.height))
    output.paste(resized, (paste_x, paste_y), resized)
    return output


def generate_batch(
    reference_path: str | os.PathLike[str],
    layout_paths: list[str | os.PathLike[str] | None],
    auto_remove_bg: bool = True,
    global_remove: bool = False,
    output_root: Path | None = None,
    output_mode: str = "separate",
) -> tuple[Path, list[dict[str, Any] | None]]:
    if output_mode == "common":
        batch_dir = (output_root or OUTPUT_ROOT) / "resize_template_common"
        batch_dir.mkdir(exist_ok=True, parents=True)
    else:
        batch_dir = create_output_batch_dir("batch", output_root)

    reference_img = load_rgba_image(reference_path)
    results: list[dict[str, Any] | None] = []
    used_names: set[str] = set()

    for index, layout_path in enumerate(layout_paths, start=1):
        if not layout_path:
            results.append(None)
            continue

        layout_img = load_rgba_image(layout_path)
        output_img = adapt_weapon(reference_img, layout_img, auto_remove_bg=auto_remove_bg, global_remove=global_remove)
        layout_stem = Path(layout_path).stem or f"slot_{index}"
        filename = f"{layout_stem}.png"
        suffix = 2
        while filename.lower() in used_names or (batch_dir / filename).exists():
            filename = f"{layout_stem}_{suffix}.png"
            suffix += 1
        used_names.add(filename.lower())
        output_path = batch_dir / filename
        output_img.save(output_path, "PNG")
        results.append(
            {
                "slot": index,
                "width": layout_img.width,
                "height": layout_img.height,
                "output_path": str(output_path),
                "preview": output_img,
            }
        )

    return batch_dir, results
