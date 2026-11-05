#!/usr/bin/env python3
"""Issue（または公開申請）に貼られた画像を /tmp/issue-images/ にダウンロードする。

GitHub Actions の中で、Claude が動く前に実行する。
- 対象: Issue本文と、そのIssueのすべてのコメント（Botのコメントは除く）
- 公開申請（プルリクエスト）の場合は、説明文の「Closes #番号」から元のIssueもたどる
- 保存した画像の一覧は /tmp/issue-images/list.txt に、古い順で書き出す

必要な環境変数: GH_TOKEN, REPO, ISSUE_NUMBER
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request

GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# Issueに貼った画像の元のURL（Markdown形式・imgタグ形式のどちらでも拾える）
ORIGINAL_RE = re.compile(
    r"https://github\.com/user-attachments/assets/[0-9A-Za-z-]+"
    r"|https://(?:private-)?user-images\.githubusercontent\.com/[^\s\"'<>)]+"
)
# 非公開リポジトリで使われる、期限付きのダウンロード用URL
SIGNED_RE = re.compile(r"https://private-user-images\.githubusercontent\.com/[^\s\"'<>]+")
LINKED_ISSUE_RE = re.compile(r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)
JST = dt.timezone(dt.timedelta(hours=9))


def api(path: str):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['REPO']}/{path}",
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github.full+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "issue-image-fetcher"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read()


def image_ext(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def find_images(body: str, body_html: str) -> list[tuple[str, list[str]]]:
    """本文から画像URLを探し、(元のURL, ダウンロードを試すURLの候補) の一覧を返す。"""
    signed = SIGNED_RE.findall(html.unescape(body_html or ""))
    results, seen = [], set()
    for url in ORIGINAL_RE.findall(body or ""):
        match = GUID_RE.search(url)
        key = match.group(0).lower() if match else url
        if key in seen:
            continue
        seen.add(key)
        candidates = [s for s in signed if match and key in s.lower()] + [url]
        results.append((url, candidates))
    return results


def download(candidates: list[str], fetcher=None) -> bytes | None:
    fetcher = fetcher or fetch
    for url in candidates:
        try:
            data = fetcher(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  取得に失敗: {url[:70]}... ({exc})")
            continue
        if image_ext(data):
            return data
        print(f"  画像ではないデータでした: {url[:70]}...")
    return None


def jst_time(text: str) -> str:
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(JST).strftime("%m/%d %H:%M")
    except (AttributeError, ValueError):
        return ""


def collect_posts(issue_no: str, getter=None) -> list[dict]:
    """Issue本文とコメントを古い順に集める。公開申請なら元のIssueも含める。"""
    getter = getter or api
    posts, visited, queue = [], set(), [issue_no]
    while queue:
        number = queue.pop(0)
        if not number or number in visited:
            continue
        visited.add(number)
        try:
            issue = getter(f"issues/{number}")
        except Exception as exc:  # noqa: BLE001
            print(f"#{number} を取得できませんでした: {exc}")
            continue
        posts.append({
            "where": f"#{number} 本文",
            "user": (issue.get("user") or {}).get("login", ""),
            "time": jst_time(issue.get("created_at", "")),
            "created_at": issue.get("created_at", ""),
            "body": issue.get("body") or "",
            "body_html": issue.get("body_html") or "",
        })
        queue += LINKED_ISSUE_RE.findall(issue.get("body") or "")
        try:
            comments = getter(f"issues/{number}/comments?per_page=100")
        except Exception as exc:  # noqa: BLE001
            print(f"#{number} のコメントを取得できませんでした: {exc}")
            comments = []
        for comment in comments:
            user = comment.get("user") or {}
            if user.get("type") == "Bot":
                continue
            posts.append({
                "where": f"#{number} コメント",
                "user": user.get("login", ""),
                "time": jst_time(comment.get("created_at", "")),
                "created_at": comment.get("created_at", ""),
                "body": comment.get("body") or "",
                "body_html": comment.get("body_html") or "",
            })
    posts.sort(key=lambda p: p["created_at"])
    return posts


def main() -> int:
    out_dir = os.environ.get("IMAGE_DIR", "/tmp/issue-images")
    os.makedirs(out_dir, exist_ok=True)
    posts = collect_posts(os.environ.get("ISSUE_NUMBER", ""))

    lines, seen, count = [], set(), 0
    for post in posts:
        for url, candidates in find_images(post["body"], post["body_html"]):
            match = GUID_RE.search(url)
            key = match.group(0).lower() if match else url
            if key in seen:
                continue
            seen.add(key)
            data = download(candidates)
            where = f"{post['where']}（{post['user']} {post['time']}）"
            if data is None:
                print(f"✗ ダウンロードできませんでした: {where} {url}")
                continue
            count += 1
            name = f"image-{count}{image_ext(data)}"
            with open(os.path.join(out_dir, name), "wb") as f:
                f.write(data)
            lines.append(f"{name}\t{where}\t{url}")
            print(f"✓ {name} を保存しました: {where}")

    with open(os.path.join(out_dir, "list.txt"), "w", encoding="utf-8") as f:
        f.write("ファイル名\t貼られていた場所（投稿者 日時）\t元のURL\n")
        f.write("".join(line + "\n" for line in lines))
    print(f"合計 {count} 枚の画像を {out_dir}/ に保存しました（番号が大きいほど新しい）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
