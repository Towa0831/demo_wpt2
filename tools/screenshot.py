#!/usr/bin/env python3
"""確認用のサイトを作り、パソコンとスマートフォンの画面の画像を撮る。

デザインを変える前と後、記事を作った後に、見た目を確かめるために使う。
撮った画像は /tmp/issue-images/screens/ に保存する（Read で開いて見る）。

tools/build.py --preview で確認用のサイト（中身を書き込んだページ）を作り、
手元に小さなWebサーバーを立てて、ブラウザで開いてから撮る。

使い方:
  python tools/screenshot.py before                        変える前に撮る
  python tools/screenshot.py after                         変えた後に撮る
  python tools/screenshot.py after news/20261105/ recruit/  撮るページを指定する（サイトのトップからの場所）
撮るページを指定しないときは、トップと、いちばん新しいお知らせを撮る。
"""

from __future__ import annotations

import functools
import http.server
import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
OUT = Path("/tmp/issue-images/screens")
SIZES = {"pc": (1280, 900), "sp": (390, 844)}
MAX_HEIGHT = 4000  # 長いページは上から 4000px までを撮る
LABEL_RE = re.compile(r"^[a-z0-9-]+$")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D401
        pass


def default_pages() -> list[str]:
    pages = [""]
    try:
        # dist/ には JSON を置かないので、site/ の news.json から探す（確認用に出ている記事だけ）
        items = json.loads((ROOT / "site" / "data" / "news.json").read_text(encoding="utf-8"))["items"]
        items = [n for n in items if (DIST / "news" / n["slug"] / "index.html").is_file()]
        newest = sorted(items, key=lambda n: n["d"] + n["slug"], reverse=True)
        if newest:
            pages.append(f"news/{newest[0]['slug']}/")
    except (OSError, KeyError, ValueError):
        pass
    return pages


def launch(playwright):
    errors = []
    for options in ({"channel": "chrome"}, {}):
        try:
            return playwright.chromium.launch(**options)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc).splitlines()[0])
    raise RuntimeError(" / ".join(errors))


def page_file(page: str) -> Path:
    path = DIST / page.lstrip("/")
    return path / "index.html" if page.endswith("/") or page == "" else path


def main() -> int:
    if len(sys.argv) < 2 or not LABEL_RE.match(sys.argv[1]):
        print("使い方: python tools/screenshot.py before（または after）[撮るページ…]")
        return 1
    try:
        return take(sys.argv[1], sys.argv[2:])
    finally:
        shutil.rmtree(DIST, ignore_errors=True)


def take(label: str, pages: list[str]) -> int:
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "build.py"), "--preview"], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout + result.stderr)
        print("[エラー] 確認用のサイトを作れなかったため、画面を撮れませんでした。上の内容を直してください。")
        return 1

    pages = pages or default_pages()
    for page in pages:
        if not page_file(page).is_file():
            print(f"[エラー] ページが見つかりません: {page}（例: news/20261105/、recruit/jobs/admin/）")
            return 1

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[注意] 画面を撮る部品（playwright）が入っていないため、撮れませんでした。")
        return 0

    handler = functools.partial(QuietHandler, directory=str(DIST))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"

    OUT.mkdir(parents=True, exist_ok=True)
    saved = []
    try:
        with sync_playwright() as p:
            try:
                browser = launch(p)
            except RuntimeError as exc:
                print(f"[注意] ブラウザを起動できなかったため、撮れませんでした（{exc}）。")
                return 0
            for page in pages:
                for size_name, (width, height) in SIZES.items():
                    tab = browser.new_page(viewport={"width": width, "height": height})
                    errors: list[str] = []
                    tab.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                    try:
                        tab.goto(base + page.lstrip("/"), wait_until="networkidle", timeout=20000)
                    except Exception:  # noqa: BLE001  文字の読み込みが遅いときなど。表示できた分で撮る
                        pass
                    tab.wait_for_timeout(500)
                    full = tab.evaluate("document.documentElement.scrollHeight")
                    overflow = tab.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
                    name = f"{label}-{(page.strip('/').replace('/', '_') or 'top')}-{size_name}.png"
                    tab.screenshot(path=str(OUT / name), full_page=True, clip={"x": 0, "y": 0, "width": width, "height": min(full, MAX_HEIGHT)})
                    notes = []
                    if overflow:
                        notes.append("横にはみ出しています（スマートフォンで横スクロールが出る）")
                    if errors:
                        notes.append(f"ページの表示処理でエラー: {errors[0][:120]}")
                    saved.append(f"{OUT / name}" + (f"  ※{' / '.join(notes)}" if notes else ""))
                    tab.close()
            browser.close()
    finally:
        server.shutdown()

    print("画面を撮りました。Read で開いて見た目を確かめてください。")
    for line in saved:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
