#!/usr/bin/env python3
"""画像をホームページ用に整えて保存する。

- 大きすぎる写真を縮小する（長い辺を最大1600px。--max で変更可）
- スマートフォン写真の向きを正しく直す
- 撮影場所（位置情報）などの情報を取り除く
- 保存先の拡張子（.jpg / .png / .webp / .gif）で保存形式を決める

使い方:
  python tools/prepare_image.py 元の画像 保存先 [--max 1600]
  例: python tools/prepare_image.py /tmp/issue-images/image-1.jpg site/assets/img/news/news_20261201_office.jpg
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*\.(?:jpg|jpeg|png|webp|gif)$")


def human(size: int) -> str:
    return f"{size / 1024 / 1024:.1f}MB" if size >= 1024 * 1024 else f"{size / 1024:.0f}KB"


def main() -> int:
    parser = argparse.ArgumentParser(description="画像をホームページ用に整えて保存します")
    parser.add_argument("source", help="元の画像")
    parser.add_argument("dest", help="保存先（site/assets/img/ から始まるパス）")
    parser.add_argument("--max", type=int, default=1600, help="長い辺の最大ピクセル数（初期値 1600）")
    args = parser.parse_args()

    src = Path(args.source)
    dest = Path(args.dest)
    if not src.is_file():
        print(f"[エラー] 元の画像が見つかりません: {src}")
        return 1
    if dest.is_absolute() or dest.parts[:3] != ("site", "assets", "img"):
        print("[エラー] 保存先は site/assets/img/ から始まるパスにしてください（例: site/assets/img/news/news_20261201_office.jpg）")
        return 1
    if not NAME_RE.match(dest.name):
        print("[エラー] ファイル名は半角の英小文字・数字・ハイフン・アンダーバーにし、拡張子は .jpg .png .webp .gif のどれかにしてください")
        return 1
    out = ROOT / dest
    out.parent.mkdir(parents=True, exist_ok=True)
    ext = out.suffix.lower()

    with Image.open(src) as original:
        source_format = original.format
        if ext == ".gif":
            if source_format != "GIF":
                print("[エラー] .gif で保存できるのは、元がGIFの画像だけです")
                return 1
            shutil.copyfile(src, out)
            print(f"保存しました: {dest}（GIFはそのままコピー、{human(out.stat().st_size)}）")
            return 0

        img = ImageOps.exif_transpose(original)
        before = img.size
        if max(img.size) > args.max:
            img.thumbnail((args.max, args.max), Image.LANCZOS)

        if ext in (".jpg", ".jpeg"):
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert("RGB")
            img.save(out, "JPEG", quality=82, optimize=True, progressive=True)
        elif ext == ".png":
            img.save(out, "PNG", optimize=True)
        elif ext == ".webp":
            img.save(out, "WEBP", quality=82, method=6)

    print(
        f"保存しました: {dest}（{before[0]}x{before[1]} → {img.size[0]}x{img.size[1]}、"
        f"{human(src.stat().st_size)} → {human(out.stat().st_size)}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
