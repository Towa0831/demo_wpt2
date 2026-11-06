#!/usr/bin/env python3
"""下書きで変わったページの一覧を、確認用ページのURLつきで書き出す（確認用ページのコメントに使う）。

使い方:
  python tools/changed_pages.py 比べる相手（例: origin/main） 確認用ページのURL [dist フォルダ]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PAGE_NAMES = {
    "": "トップページ",
    "alliance/": "アライアンス",
    "news/": "ニュース一覧",
    "contact/": "お問い合わせ",
    "recruit/": "採用情報トップ",
    "recruit/jobs/": "仕事を知る",
    "recruit/people/": "人を知る",
    "recruit/open/": "オープン採用",
    "recruit/faq/": "よくある質問",
    "recruit/career/": "募集一覧",
    "recruit/entry/": "エントリー",
}

# recruit.json のどの項目が、どのページに出ているか
RECRUIT_PAGES = {
    "openings": ["recruit/career/"],
    "faq": ["recruit/faq/"],
    "open": ["recruit/open/"],
    "jobs": ["recruit/jobs/"],
    "people": ["recruit/people/"],
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout


def load_at(ref: str, path: str):
    text = git("show", f"{ref}:{path}")
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def by_slug(items) -> dict:
    return {i.get("slug"): i for i in items or [] if isinstance(i, dict)}


def main() -> int:
    if len(sys.argv) < 3:
        print("使い方: python tools/changed_pages.py 比べる相手 確認用ページのURL [dist]")
        return 1
    base, url = sys.argv[1], sys.argv[2].rstrip("/") + "/"
    dist = Path(sys.argv[3]) if len(sys.argv) > 3 else ROOT / "dist"
    changed = [f for f in git("diff", "--name-only", f"{base}...HEAD", "--", "site").split() if f]

    pages: list[tuple[str, str]] = []  # (表示名, ページの場所 or None で表示されない)

    def add(label: str, page: str | None) -> None:
        if (label, page) not in pages:
            pages.append((label, page))

    def page_label(page: str) -> str:
        return PAGE_NAMES.get(page, page)

    for f in changed:
        rel = f[len("site/"):]
        if rel.endswith("index.html"):
            page = rel[: -len("index.html")]
            # 記事・仕事・人のページは中身が JSON にあるので、JSON の変更のほうで書き出す
            if (page.startswith("news/") and page.count("/") >= 2) or (page.startswith(("recruit/jobs/", "recruit/people/")) and page.count("/") >= 3):
                continue
            add(page_label(page), page)
        elif rel in ("assets/style.css", "assets/app.js"):
            add("サイト全体の見た目（トップページで確認）", "")
        elif rel == "data/news.json":
            old = by_slug((load_at(base, f) or {}).get("items"))
            new_data = json.loads((ROOT / f).read_text(encoding="utf-8"))
            new = by_slug(new_data.get("items"))
            for slug, item in new.items():
                if old.get(slug) != item:
                    title = item.get("t", slug)
                    page = f"news/{slug}/"
                    add(f"お知らせ『{title}』", page if (dist / page / "index.html").is_file() else None)
            for slug in old.keys() - new.keys():
                add(f"お知らせ『{old[slug].get('t', slug)}』（削除）", None)
            add("ニュース一覧", "news/")
        elif rel == "data/recruit.json":
            old = load_at(base, f) or {}
            new = json.loads((ROOT / f).read_text(encoding="utf-8"))
            for key in sorted(set(old) | set(new)):
                if old.get(key) == new.get(key):
                    continue
                if key in ("jobs", "people"):
                    o, n = by_slug(old.get(key)), by_slug(new.get(key))
                    for slug in sorted(set(o) | set(n)):
                        if o.get(slug) != n.get(slug) and slug in n:
                            name = n[slug].get("name") or n[slug].get("title") or slug
                            add(f"{PAGE_NAMES[f'recruit/{key}/']}『{name}』", f"recruit/{key}/{slug}/")
                        elif slug not in n:
                            name = o[slug].get("name") or o[slug].get("title") or slug
                            add(f"{PAGE_NAMES[f'recruit/{key}/']}『{name}』（削除）", None)
                for page in RECRUIT_PAGES.get(key, ["recruit/"]):
                    add(page_label(page), page)
        elif rel in ("data/site.json", "data/products.json"):
            add("トップページ", "")
            add("アライアンス", "alliance/")

    for label, page in pages:
        if page is None:
            print(f"- {label}: 削除・非公開などのため表示されません。一覧から消えていれば正しい状態です")
        else:
            print(f"- {label}: {url}{page}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
