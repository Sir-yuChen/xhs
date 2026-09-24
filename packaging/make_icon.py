"""生成 Windows 图标（exe / 安装器 / 托盘共用）。

用法：<python> make_icon.py <源图 jpg/png> <输出 app.ico>
多尺寸内嵌（16/24/32/48/64/128/256），Pillow 完成，无外部依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

SIZES = [16, 24, 32, 48, 64, 128, 256]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: make_icon.py <source-image> <output.ico>")
        return 2
    src = Path(argv[1])
    dst = Path(argv[2])
    if not src.is_file():
        print(f"source image not found: {src}")
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)

    image = Image.open(src).convert("RGBA")
    # 方形画布居中裁剪，避免非正方源图拉伸变形。
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))

    image.save(dst, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"icon written: {dst} ({dst.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
