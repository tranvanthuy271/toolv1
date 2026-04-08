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


def remove_background(img: Image.Image, tolerance: int = 18) -> Image.Image:
    """Remove background using per-edge independent flood-fill.

    Each of the 4 border edges is processed separately with its own background
    colour estimate.  The four resulting masks are unioned, so a corner that
    has a slightly different shade from the rest still gets removed.
    """
    if img.mode not in ("RGBA", "RGB"):
        img = img.convert("RGBA")

    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    height, width = arr.shape[:2]

    if height == 0 or width == 0:
        return Image.fromarray(arr, mode="RGBA")

    alpha = arr[:, :, 3]
    existing_alpha = alpha > 10
    rgb_f = arr[:, :, :3].astype(np.float32)

    # 4 edges: (pixel rows/cols, border boolean mask)
    top_border = np.zeros((height, width), dtype=bool)
    top_border[0, :] = True
    bot_border = np.zeros((height, width), dtype=bool)
    bot_border[height - 1, :] = True
    lft_border = np.zeros((height, width), dtype=bool)
    lft_border[:, 0] = True
    rgt_border = np.zeros((height, width), dtype=bool)
    rgt_border[:, width - 1] = True

    edge_defs = [
        (arr[0, :, :3],            alpha[0, :],            top_border),
        (arr[height - 1, :, :3],   alpha[height - 1, :],   bot_border),
        (arr[:, 0, :3],            alpha[:, 0],             lft_border),
        (arr[:, width - 1, :3],    alpha[:, width - 1],     rgt_border),
    ]

    background_mask = np.zeros((height, width), dtype=bool)
    any_processed = False

    consistency_tol = max(float(tolerance) + 8.0, 24.0)

    for edge_rgb, edge_alpha, edge_border in edge_defs:
        # Collect opaque pixels on this edge
        opaque_mask = edge_alpha > 10
        opaque_pixels = edge_rgb[opaque_mask].astype(np.float32)
        if len(opaque_pixels) < 5:
            continue  # edge is nearly all transparent — nothing to seed from

        # Estimate background colour for this edge
        local_bg = np.median(opaque_pixels, axis=0)
        local_diff = np.sqrt(((opaque_pixels - local_bg[None, :]) ** 2).sum(axis=1))

        # Skip edge if its pixels don't form a consistent background colour
        if float(np.mean(local_diff <= consistency_tol)) < 0.50:
            continue

        # Per-edge adaptive tolerance
        local_tol = max(float(tolerance), float(np.percentile(local_diff, 85)) + 8.0)
        local_tol = min(local_tol, float(tolerance) * 3.5)

        color_diff = np.sqrt(((rgb_f - local_bg[None, None, :]) ** 2).sum(axis=2))
        candidate = (color_diff <= local_tol) & existing_alpha
        seed = edge_border & candidate

        if seed.any():
            background_mask |= _grow_background_region(candidate, seed)
            any_processed = True

    if not any_processed:
        return rgba.copy()

    result = arr.copy()
    result[background_mask, :3] = 0
    result[background_mask, 3] = 0
    return Image.fromarray(result, mode="RGBA")


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


def analyze_image(img: Image.Image, auto_remove_bg: bool = True) -> dict[str, Any]:
    working = img.convert("RGBA")
    if auto_remove_bg:
        try:
            working = remove_background(working)
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


def analyze_image_path(path: str | os.PathLike[str], auto_remove_bg: bool = True) -> dict[str, Any]:
    image = load_rgba_image(path)
    return analyze_image(image, auto_remove_bg=auto_remove_bg)


def save_background_removed_image(
    image_path: str | os.PathLike[str],
    output_root: Path | None = None,
) -> dict[str, Any]:
    batch_dir = create_output_batch_dir("rm_bg", output_root)
    source_image = load_rgba_image(image_path)
    output_image = remove_background(source_image)

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
) -> Image.Image:
    ref_working = reference_img.convert("RGBA")
    layout_working = layout_img.convert("RGBA")

    if auto_remove_bg:
        try:
            ref_working = remove_background(ref_working)
        except Exception:
            pass
        try:
            layout_working = remove_background(layout_working)
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
        output_img = adapt_weapon(reference_img, layout_img, auto_remove_bg=auto_remove_bg)
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
