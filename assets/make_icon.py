"""Generate assets/tray.ico — a simple ThaoResearch tray icon.

Produces a 64x64 multi-resolution .ico with a teal background and a white
"TR" mark. Run once: ``python assets/make_icon.py``.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _make(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Rounded-square background
    bg = (30, 136, 168, 255)  # teal
    pad = max(2, size // 12)
    d.rounded_rectangle((pad, pad, size - pad, size - pad),
                        radius=size // 6, fill=bg)
    # "TR" text
    text = "TR"
    font_size = max(10, size // 2)
    try:
        font = ImageFont.truetype("arialbd.ttf", font_size)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()
    # Center text
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1] - max(1, size // 32)
    d.text((tx, ty), text, fill=(255, 255, 255, 255), font=font)
    return img


def main() -> None:
    out = Path(__file__).resolve().parent / "tray.ico"
    # Generate 4 sizes and combine into a single multi-resolution .ico
    sizes = [16, 24, 32, 48, 64, 128, 256]
    base = _make(256)
    base.save(
        out,
        format="ICO",
        sizes=[(s, s) for s in sizes],
    )
    print(f"Wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()