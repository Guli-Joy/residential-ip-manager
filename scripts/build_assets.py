from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QRectF, QSize  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "src" / "residential_ip_manager" / "assets"
SVG_PATH = ASSET_DIR / "app-icon.svg"
PNG_PATH = ASSET_DIR / "app-icon.png"
ICO_PATH = ASSET_DIR / "app-icon.ico"
CANVAS_SIZE = 1024
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_png() -> None:
    renderer = QSvgRenderer(str(SVG_PATH))
    if not renderer.isValid():
        raise RuntimeError(f"无法读取 SVG 图标：{SVG_PATH}")

    image = QImage(
        QSize(CANVAS_SIZE, CANVAS_SIZE),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, CANVAS_SIZE, CANVAS_SIZE))
    painter.end()
    if not image.save(str(PNG_PATH), "PNG"):
        raise RuntimeError(f"无法写入 PNG 图标：{PNG_PATH}")


def render_ico() -> None:
    with Image.open(PNG_PATH) as source:
        source.convert("RGBA").save(
            ICO_PATH,
            format="ICO",
            sizes=[(size, size) for size in ICO_SIZES],
        )


def main() -> None:
    render_png()
    render_ico()
    print(f"generated {PNG_PATH}")
    print(f"generated {ICO_PATH}")


if __name__ == "__main__":
    main()
