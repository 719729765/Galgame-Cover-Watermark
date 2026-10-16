# -*- coding: utf-8 -*-
"""Convert the project's 0.png into a multi-resolution Windows 0.ico icon."""
from pathlib import Path
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
PNG_PATH = BASE_DIR / "0.png"
ICO_PATH = BASE_DIR / "0.ico"
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> None:
    if not PNG_PATH.is_file():
        raise SystemExit(
            f"找不到图标文件：{PNG_PATH}\n"
            "请先把你的 0.png 放到 prepare_icon.py 和主程序所在的同一文件夹。"
        )

    with Image.open(PNG_PATH) as opened:
        image = opened.convert("RGBA")

    # Windows icons must be square. Preserve aspect ratio and use transparent
    # padding rather than stretching or cropping a non-square source PNG.
    image.thumbnail((256, 256), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    canvas.alpha_composite(image, ((256 - image.width) // 2, (256 - image.height) // 2))
    canvas.save(ICO_PATH, format="ICO", sizes=SIZES)
    print(f"已生成：{ICO_PATH}")
    print("接下来使用 PyInstaller 的 --icon 0.ico，并用 --add-data \"0.png;.\" 将 PNG 一并打包。")


if __name__ == "__main__":
    main()
