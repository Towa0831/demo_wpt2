#!/usr/bin/env python3
"""サイトのチェックと、アップロード用のフォルダ（dist/）づくり。

このサイトは、ページのHTML（site/ の中）が data/*.json を読み込んで中身を表示する作りになっている。
site/ を dist/ に写し、次のことを行う（site/ の中は書き換えない）。

  - data/*.json の書き方の誤り、記事フォルダや画像の見落としをチェックする
  - お知らせ（news.json）の「下書き」「非公開」「公開日が未来」の記事を、本番用からは外す
  - 画像やCSSを差し替えたとき、ブラウザに古いものが残らないよう、?v= の番号を付け直す
  - 確認用（--preview）では、ページの上に黄色い帯を出し、検索エンジンに載らないようにする
  - ページを一度ブラウザで開いて、JSON から表示された中身をそのまま HTML に書き込み（事前の書き込み）、
    JSON を読む表示処理（load( を使う <script>）をページから外す。
    検索エンジンやSNSでの共有でも、記事ごとのタイトルや中身が読めるようにするため。
    ニュース一覧の絞り込み・ページ送りは、JSON を読まない別の <script> にしてあるので、そのまま動く
  - 全ページの書き込みができたら、dist/ から data/ フォルダを外す（本番・確認用のサーバーに JSON を置かない）。
    1ページでも書き込めなかったときは、エラーにして止める（JSON がないと表示できないページを公開しないため）

使い方:
  python tools/build.py --check      チェックだけ（ファイルは作らない。今日の日付も表示）
  python tools/build.py --preview    確認用を dist/ に作る
  python tools/build.py              本番用を dist/ に作る
  python tools/build.py --weekday 2026-12-29 2027-01-04   曜日を確かめる
  python tools/build.py --today 2026-12-01                 今日の日付を変えて試す
  python tools/build.py --no-prerender                     事前の書き込みをしない（手元で急いで試すとき。data/ は残る）
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import http.server
import threading
import hashlib
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DATA = SITE / "data"
DIST = ROOT / "dist"
JST = ZoneInfo("Asia/Tokyo")
SITE_URL = "https://yamaguchi-index.com/demo_wpt2/"  # 本番サイトのURL（SNS用の情報に使う）
DEFAULT_OG_IMAGE = "assets/img/photo/top-hero.jpg"   # 記事に画像がないときに、SNSで共有されたときに出る画像
WEEKDAYS = "月火水木金土日"

IGNORE = shutil.ignore_patterns("*.md", ".DS_Store", ".gitkeep", "Thumbs.db")

STATUS_PUBLIC, STATUS_DRAFT, STATUS_HIDDEN = "公開", "下書き", "非公開"
STATUSES = {STATUS_PUBLIC, STATUS_DRAFT, STATUS_HIDDEN}
SLUG_RE = re.compile(r"^[0-9a-z]+(?:-[0-9a-z]+)*$")
NEWS_DATE_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})$")
BLOCK_TYPES = {"p", "h", "link", "pdf", "image", "table"}
NEWS_KEYS = {"d", "slug", "t", "co", "ca", "lead", "blocks", "src", "status"}

PREVIEW_BAR = (
    '<div style="position:sticky;top:0;z-index:2147483000;padding:8px 16px;background:#fff3c4;'
    'border-bottom:1px solid #e9c95a;color:#5c4500;font:600 14px/1.6 sans-serif;text-align:center">'
    "確認用ページです。まだ一般公開されていません（下書き・公開予定のお知らせも表示しています）</div>"
)
FORM_NOTE = (
    '<p style="padding:24px;border:1px dashed #c9cfcc;border-radius:8px;color:#6b716e;text-align:center">'
    "確認用ページでは、フォームは表示されません（本番サイトで表示されます）。</p>"
)

# Claude が描くイラスト（SVG）の安全チェック
SVG_MAX_BYTES = 500 * 1024
SVG_FORBIDDEN_TAGS = {
    "script", "foreignObject", "iframe", "embed", "object", "image", "a",
    "animate", "animateMotion", "animateTransform", "set", "audio", "video",
}
SVG_EXTERNAL_URL_RE = re.compile(r"url\(\s*['\"]?(?!#)|@import", re.I)


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"[エラー] {where}: {message}")

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(f"[注意]   {where}: {message}")


def today_jst() -> dt.date:
    return dt.datetime.now(JST).date()


def parse_news_date(text) -> dt.date | None:
    m = NEWS_DATE_RE.match(str(text or ""))
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_any_date(text: str) -> dt.date | None:
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text.strip())
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def load_json(path: Path, report: Report):
    label = str(path.relative_to(ROOT))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        lines = path.read_text(encoding="utf-8").splitlines()
        near = lines[exc.lineno - 1].strip() if 0 < exc.lineno <= len(lines) else ""
        report.error(label, f"{exc.lineno}行目の{exc.colno}文字目あたりで、書き方に誤りがあります（{exc.msg}）。その行: {near[:80]}")
        return None


def site_path(value: str) -> Path | None:
    """JSONの「/assets/…」のようなパスを、site/ の中のファイルの場所にする。外部URLは None。"""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return None
    clean = re.split(r"[?#]", value, maxsplit=1)[0]
    return SITE / clean.lstrip("/")


def walk_strings(obj, trail=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from walk_strings(value, f"{trail}.{key}" if trail else str(key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from walk_strings(value, f"{trail}[{i}]")
    elif isinstance(obj, str):
        yield trail, obj


def check_svg(path: Path) -> list[str]:
    """SVG がホームページに載せても安全で、正しく表示できる形かを確かめる。問題があれば理由の一覧を返す。"""
    errors: list[str] = []
    data = path.read_bytes()
    if len(data) > SVG_MAX_BYTES:
        errors.append(f"ファイルが大きすぎます（{len(data) // 1024}KB）。{SVG_MAX_BYTES // 1024}KB 以下にしてください。")
    text = data.decode("utf-8", errors="replace")
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        errors.append("<!DOCTYPE> や <!ENTITY> は使えません。")
        return errors
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        errors.append(f"SVGの書き方に誤りがあります（{exc}）。")
        return errors

    def local(name: str) -> str:
        return name.rsplit("}", 1)[-1]

    if local(root.tag) != "svg":
        errors.append("いちばん外側が <svg> ではありません。")
    if not root.get("viewBox"):
        errors.append("<svg> に viewBox がありません（例: viewBox=\"0 0 1600 900\"）。")
    for el in root.iter():
        tag = local(el.tag)
        if tag in SVG_FORBIDDEN_TAGS:
            errors.append(f"<{tag}> は使えません。図形（rect、circle、ellipse、path など）だけで描いてください。")
        if tag == "style" and el.text and SVG_EXTERNAL_URL_RE.search(el.text):
            errors.append("<style> の中で外部のファイルを読み込むことはできません。")
        for raw_name, value in el.attrib.items():
            name = local(raw_name)
            if name.lower().startswith("on"):
                errors.append(f"{name} 属性（動きをつける仕組み）は使えません。")
            elif name == "href" and not value.startswith("#"):
                errors.append(f"href には同じSVGの中の部品（#名前）だけを指定できます（今は「{value[:40]}」）。")
            elif "javascript:" in value.lower() or (name == "style" and SVG_EXTERNAL_URL_RE.search(value)):
                errors.append(f"{name} 属性に使えない内容が含まれています。")
    return sorted(set(errors), key=errors.index)


# ---------------------------------------------------------------- お知らせ

def news_state(item: dict, today: dt.date) -> str:
    """published / scheduled / draft / hidden のどれか。"""
    status = item.get("status", STATUS_PUBLIC) or STATUS_PUBLIC
    if status == STATUS_HIDDEN:
        return "hidden"
    if status == STATUS_DRAFT:
        return "draft"
    day = parse_news_date(item.get("d"))
    if day and day > today:
        return "scheduled"
    return "published"


def check_news(news, report: Report, today: dt.date) -> dict[str, int]:
    where = "site/data/news.json"
    counts: dict[str, int] = {}
    if not isinstance(news, dict) or not isinstance(news.get("items"), list):
        report.error(where, "items（記事の一覧）が見つかりません。")
        return counts
    companies = news.get("companies") or {}
    categories = news.get("categories") or {}
    seen: dict[str, int] = {}
    for i, item in enumerate(news["items"]):
        if not isinstance(item, dict):
            report.error(where, f"items の {i + 1} 件目が、記事の形（{{ }}）になっていません。")
            continue
        slug = str(item.get("slug") or "")
        label = f"{where}（{slug or f'{i + 1}件目'}「{str(item.get('t') or '')[:20]}」）"
        if not SLUG_RE.match(slug):
            report.error(label, "slug は半角の英小文字・数字・ハイフンだけにしてください（例: 20261105、20261105-2）。")
        elif slug in seen:
            report.error(label, f"slug「{slug}」が、{seen[slug]} 件目の記事と重なっています。")
        seen.setdefault(slug, i + 1)
        if not str(item.get("t") or "").strip():
            report.error(label, "t（タイトル）がありません。")
        if parse_news_date(item.get("d")) is None:
            report.error(label, f"d（日付）は 2026.11.05 の形で書いてください（今は「{item.get('d')}」）。")
        status = item.get("status", STATUS_PUBLIC)
        if status not in STATUSES:
            report.error(label, f"status は「公開」「下書き」「非公開」のどれかにしてください（今は「{status}」）。")
        for key, names, title in (("co", companies, "会社"), ("ca", categories, "カテゴリ")):
            codes = item.get(key)
            if not isinstance(codes, list) or not codes:
                report.error(label, f"{key}（{title}）は [\"コード\"] の形で1つ以上書いてください。")
                continue
            for code in codes:
                if code not in names:
                    report.error(label, f"{key} の「{code}」は、news.json の {'companies' if key == 'co' else 'categories'} にありません（使えるもの: {'、'.join(names)}）。")
        if not str(item.get("lead") or "").strip():
            report.error(label, "lead（最初の段落。一覧にも出る）がありません。")
        blocks = item.get("blocks", [])
        if not isinstance(blocks, list):
            report.error(label, "blocks（本文）は [ ] の形で書いてください。")
            blocks = []
        for n, block in enumerate(blocks, 1):
            kind = block.get("t") if isinstance(block, dict) else None
            if kind not in BLOCK_TYPES:
                report.error(label, f"本文の {n} 番目のブロックの種類「{kind}」は使えません（使えるもの: p、h、table、image、link、pdf）。")
                continue
            value = block.get("v")
            if kind == "table":
                if not isinstance(value, list) or not all(isinstance(r, list) and len(r) == 2 for r in value):
                    report.error(label, f"本文の {n} 番目の表は [[\"項目\", \"内容\"], …] の形で書いてください。")
            elif not isinstance(value, str) or not value.strip():
                report.error(label, f"本文の {n} 番目のブロック（{kind}）の中身 v がありません。")
            if kind == "image" and not str(block.get("alt") or "").strip():
                report.warn(label, f"本文の {n} 番目の画像に、説明（alt）がありません。")
        for key in item:
            if key not in NEWS_KEYS:
                report.warn(label, f"「{key}」という項目は使われません（書き間違いかもしれません）。")
        state = news_state(item, today)
        counts[state] = counts.get(state, 0) + 1
        folder = SITE / "news" / slug
        if slug and state != "hidden" and not (folder / "index.html").is_file():
            report.error(label, f"記事のページ site/news/{slug}/index.html がありません。ほかの記事のフォルダの index.html をコピーして作ってください。")
    for folder in sorted((SITE / "news").iterdir()):
        if folder.is_dir() and folder.name not in seen:
            report.warn(f"site/news/{folder.name}/", "news.json に、このフォルダ名（slug）の記事がありません。")
    return counts


# ---------------------------------------------------------------- 採用・会社情報

def check_recruit(recruit, site, report: Report) -> None:
    where = "site/data/recruit.json"
    if not isinstance(recruit, dict):
        return
    jobs = [j.get("slug") for j in recruit.get("jobs", []) if isinstance(j, dict)]
    for kind, folder in (("jobs", "jobs"), ("people", "people")):
        slugs = []
        for item in recruit.get(kind, []):
            slug = str(item.get("slug") or "") if isinstance(item, dict) else ""
            if not SLUG_RE.match(slug):
                report.error(where, f"{kind} の slug「{slug}」は、半角の英小文字・数字・ハイフンだけにしてください。")
                continue
            if slug in slugs:
                report.error(where, f"{kind} の slug「{slug}」が重なっています。")
            slugs.append(slug)
            if item.get("status") != STATUS_HIDDEN and not (SITE / "recruit" / folder / slug / "index.html").is_file():
                report.error(where, f"{kind}「{slug}」のページ site/recruit/{folder}/{slug}/index.html がありません。同じフォルダのほかの index.html をコピーして作ってください。")
        base = SITE / "recruit" / folder
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if d.is_dir() and d.name not in slugs:
                report.warn(f"site/recruit/{folder}/{d.name}/", f"recruit.json の {kind} に、このフォルダ名（slug）がありません。")
    for person in recruit.get("people", []):
        if isinstance(person, dict) and person.get("jobSlug") and person["jobSlug"] not in jobs:
            report.error(where, f"people「{person.get('slug')}」の jobSlug「{person['jobSlug']}」が jobs にありません。")
    check_job_items(recruit, report)
    company_ids = {c.get("id") for c in (site or {}).get("companies", []) if isinstance(c, dict)}
    for n, opening in enumerate(recruit.get("openings", []), 1):
        if not isinstance(opening, dict):
            continue
        label = f"{where}（openings の {n} 件目「{opening.get('title', '')}」）"
        if company_ids and opening.get("co") not in company_ids:
            report.error(label, f"co の「{opening.get('co')}」は、site.json の companies にありません。")
        if opening.get("job") not in jobs:
            report.error(label, f"job の「{opening.get('job')}」は、recruit.json の jobs にありません（使えるもの: {'、'.join(jobs)}）。")
        if opening.get("status", STATUS_PUBLIC) not in STATUSES:
            report.error(label, f"status は「公開」「下書き」「非公開」のどれかにしてください（今は「{opening.get('status')}」）。")


def _pairs_ok(value) -> bool:
    """[["左の列", "右の列"], …] の形か（1日の流れ・キャリアの表）"""
    return isinstance(value, list) and bool(value) and all(
        isinstance(row, list) and len(row) == 2 and all(isinstance(c, str) and c.strip() for c in row) for row in value)


def check_job_items(recruit: dict, report: Report) -> None:
    """職種（jobs）と社員紹介（people）の1件ずつに、ページを表示するのに要る項目がそろっているか"""
    where = "site/data/recruit.json"
    for job in recruit.get("jobs", []):
        if not isinstance(job, dict):
            continue
        label = f"{where}（jobs「{job.get('slug', '')}」）"
        for key, name in (("name", "職種名"), ("where", "勤務地"), ("lead", "ひとこと"), ("desc", "仕事内容")):
            if not (isinstance(job.get(key), str) and job[key].strip()):
                report.error(label, f"{key}（{name}）を書いてください。")
        for key, name in (("day", "1日の流れ"), ("career", "キャリアの道筋")):
            if not _pairs_ok(job.get(key)):
                report.error(label, f"{key}（{name}）は [[\"左の列\", \"右の列\"], …] の形で1行以上書いてください。")
        if not job.get("img") and not job.get("imgPlan"):
            report.warn(label, "写真（img）がありません。撮影予定の枠を出すため、imgPlan（どんな写真を撮る予定か）を書いてください。")
    for person in recruit.get("people", []):
        if not isinstance(person, dict):
            continue
        label = f"{where}（people「{person.get('slug', '')}」）"
        for key, name in (("name", "呼び方"), ("jobSlug", "職種"), ("title", "見出し"), ("lead", "ひとこと"), ("where", "勤務地")):
            if not (isinstance(person.get(key), str) and person[key].strip()):
                report.error(label, f"{key}（{name}）を書いてください。")
        qa = person.get("qa")
        ok = isinstance(qa, list) and bool(qa) and all(
            isinstance(x, dict) and (
                (isinstance(x.get("q"), str) and isinstance(x.get("a"), list) and x["a"] and all(isinstance(s, str) for s in x["a"]))
                or (isinstance(x.get("img"), str) and isinstance(x.get("cap", ""), str)))
            for x in qa)
        if not ok:
            report.error(label, 'qa（インタビュー）は {"q": "質問", "a": ["答えの段落", …]} か {"img": "写真", "cap": "説明"} を並べた形にしてください。')


KANJI_NUM = {c: i for i, c in enumerate("〇一二三四五六七八九十")}
JOB_COUNT_RE = re.compile(r"(?<![0-9０-９〇一二三四五六七八九十])(?P<n>[0-9０-９]+|[一二三四五六七八九十]+)(?:つの)?職種"
                          r"|職種(?:は|が)(?P<m>[0-9０-９]+|[一二三四五六七八九十]+)つ")


def _to_int(text: str) -> int | None:
    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if len(text) == 1 and text in KANJI_NUM:
        return KANJI_NUM[text]
    if len(text) == 2 and text[0] == "十" and text[1] in KANJI_NUM:
        return 10 + KANJI_NUM[text[1]]
    return None


def check_job_count_text(recruit, report: Report) -> None:
    """「4つの職種」「職種は4つ」のように職種の数を書いた文が、jobs の数と合っているか（職種を増やしたときの直し忘れを防ぐ）"""
    if not isinstance(recruit, dict):
        return
    count = len([j for j in recruit.get("jobs", []) if isinstance(j, dict)])
    places: list[tuple[str, str]] = []
    for page in sorted(SITE.rglob("*.html")):
        places.append((str(page.relative_to(ROOT)), page.read_text(encoding="utf-8")))
    for name in ("recruit.json", "site.json"):
        path = DATA / name
        if path.is_file():
            places.append((f"site/data/{name}", path.read_text(encoding="utf-8")))
    for label, text in places:
        for m in JOB_COUNT_RE.finditer(text):
            n = _to_int(m.group("n") or m.group("m"))
            if n is not None and n != count:
                report.error(label, f"「{m.group(0)}」と書いてありますが、職種（recruit.json の jobs）は {count} つです。文を直してください。")


def check_paths_in_json(name: str, data, report: Report) -> None:
    for trail, value in walk_strings(data):
        target = site_path(value)
        if target is None:
            continue
        if not value.startswith(("/assets/", "/news/", "/recruit/", "/alliance/", "/contact/", "/data/")) and value != "/":
            continue
        if value.startswith("/assets/") and not target.is_file():
            report.error(f"site/data/{name}（{trail}）", f"「{value}」のファイルが site/ の中に見つかりません。")
        elif not value.startswith("/assets/"):
            page = target if target.suffix else target / "index.html"
            if not page.is_file():
                report.warn(f"site/data/{name}（{trail}）", f"リンク先「{value}」のページが site/ の中に見つかりません。")


HTML_REF_RE = re.compile(r'(?:src|href)="([^"]+)"')


def check_html(report: Report) -> None:
    for page in sorted(SITE.rglob("*.html")):
        text = page.read_text(encoding="utf-8")
        label = str(page.relative_to(ROOT))
        for ref in sorted(set(HTML_REF_RE.findall(text))):
            if re.match(r"^(?:[a-z]+:|//|#|/|\$\{)", ref) or "${" in ref:
                continue
            clean = re.split(r"[?#]", ref, maxsplit=1)[0]
            if not clean:
                continue
            target = (page.parent / clean).resolve()
            if clean.endswith("/") or not target.suffix:
                target = target / "index.html"
            if not target.exists():
                report.error(label, f"「{ref}」のファイル（またはページ）が見つかりません。")


def check_all(today: dt.date) -> tuple[Report, dict]:
    report = Report()
    data = {}
    for path in sorted(DATA.glob("*.json")):
        data[path.stem] = load_json(path, report)
    if data.get("news") is not None:
        counts = check_news(data["news"], report, today)
    else:
        counts = {}
    if data.get("recruit") is not None:
        check_recruit(data["recruit"], data.get("site"), report)
        check_job_count_text(data["recruit"], report)
    for name, value in data.items():
        if value is not None:
            check_paths_in_json(f"{name}.json", value, report)
    check_html(report)
    for svg in sorted((SITE / "assets").rglob("*.svg")):
        for message in check_svg(svg):
            report.error(str(svg.relative_to(ROOT)), message)
    return report, counts


# ---------------------------------------------------------------- dist/ づくり

def short_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


VERSIONED_RE = re.compile(r'((?:src|href)=")([^"?#]+)\?v=[^"#]*(")')


def refresh_versions(page: Path, text: str) -> str:
    """?v= の付いたファイルの番号を、ファイルの中身から作り直す（差し替えたら番号が変わる）。"""

    def fix(m: re.Match) -> str:
        ref = m.group(2)
        if re.match(r"^(?:[a-z]+:|//|/)", ref):
            return m.group(0)
        target = (page.parent / ref).resolve()
        if not target.is_file():
            return m.group(0)
        return f"{m.group(1)}{ref}?v={short_hash(target)}{m.group(3)}"

    return VERSIONED_RE.sub(fix, text)


DATA_SCRIPT_RE = re.compile(r"<script>(?:(?!</script>).)*?\bload\((?:(?!</script>).)*?</script>", re.S)


def prerender_mode(text: str) -> str:
    """そのページの扱い。skip：JSON を読まないので何もしない／strip：書き込んで表示処理を外す／
    mixed：表示処理の中に、見る人の操作に応える処理が混ざっていて、外すと動かなくなる"""
    scripts = DATA_SCRIPT_RE.findall(text)
    if not scripts:
        return "skip"
    joined = " ".join(scripts)
    if "addEventListener" in joined or "location.search" in joined:
        return "mixed"
    return "strip"


def pages_needing_json(pages: list[Path]) -> list[str]:
    """JSON を読む処理が残っているページ（data/ を外すと表示できなくなるページ）"""
    left = []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        inline = re.findall(r"<script>(.*?)</script>", text, re.S)
        if DATA_SCRIPT_RE.search(text) or any("fetch(" in s for s in inline):
            left.append(page.relative_to(DIST).as_posix())
    return left


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def _launch(playwright):
    errors = []
    for options in ({"channel": "chrome"}, {}):
        try:
            return playwright.chromium.launch(**options)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc).splitlines()[0])
    raise RuntimeError(" / ".join(errors))


def prerender(pages: list[Path]) -> tuple[int, list[str]]:
    """dist/ のページをブラウザで開き、表示された中身を HTML に書き込む。書き込んだページ数と、問題の一覧を返す。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return 0, ["ブラウザを動かす部品（playwright）がないため、事前の書き込みをしませんでした（ページはこれまでどおり表示されます）。"]

    handler = functools.partial(_QuietHandler, directory=str(DIST))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    done, problems = 0, []
    try:
        with sync_playwright() as p:
            try:
                browser = _launch(p)
            except RuntimeError as exc:
                return 0, [f"ブラウザを起動できなかったため、事前の書き込みをしませんでした（{exc}）。"]
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            # フォント・外部のページなど、サイトの外への読み込みは待たない
            context.route(re.compile(r"^(?!http://127\.0\.0\.1).*"), lambda route: route.abort())
            for page in pages:
                rel = page.relative_to(DIST).as_posix()
                original = page.read_text(encoding="utf-8")
                mode = prerender_mode(original)
                if mode == "skip":
                    continue
                if mode == "mixed":
                    problems.append(f"{rel}: JSON を読む <script> の中に、クリックなどに応える処理が混ざっているため、書き込めません"
                                    "（ニュース一覧のように、操作の部分を JSON を読まない別の <script> に分けてください）")
                    continue
                tab = context.new_page()
                errors: list[str] = []
                tab.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                tab.on("console", lambda msg, errors=errors: errors.append(msg.text) if msg.type == "error" and "net::" not in msg.text and "Failed to load resource" not in msg.text else None)
                try:
                    tab.goto(base + rel, wait_until="load", timeout=20000)
                    tab.wait_for_load_state("networkidle", timeout=10000)
                    tab.wait_for_timeout(200)
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc).splitlines()[0])
                if errors:
                    problems.append(f"{rel}: 表示の途中で問題があったため、書き込まずに元のままにしました（{errors[0][:100]}）")
                    tab.close()
                    continue
                html_text = tab.evaluate(
                    r"""() => {
                        document.querySelectorAll('script:not([src])').forEach(s => {
                          if (/\bload\(/.test(s.textContent)) s.remove();
                        });
                        return document.documentElement.outerHTML;
                    }"""
                )
                page.write_text("<!DOCTYPE html>\n" + html_text + "\n", encoding="utf-8")
                done += 1
                tab.close()
            browser.close()
    finally:
        server.shutdown()
    return done, problems


def add_social_tags(page: Path, text: str) -> str:
    """SNSで共有されたときに出る情報（タイトル、説明、画像、URL）を、ページごとに書き込む。"""
    if 'property="og:title"' in text:
        return text
    rel = page.relative_to(DIST).as_posix()
    path = rel[: -len("index.html")] if rel.endswith("index.html") else rel
    title_m = re.search(r"<title>(.*?)</title>", text, re.S)
    desc_m = re.search(r'<meta name="description" content="([^"]*)"', text)
    title = title_m.group(1).strip() if title_m else ""
    desc = desc_m.group(1) if desc_m else ""
    image = DEFAULT_OG_IMAGE
    is_article = path.startswith("news/") and path != "news/"
    if is_article:
        body = text[text.find('id="ar-body"'):] if 'id="ar-body"' in text else ""
        m = re.search(r'<img[^>]+src="([^"]+)"', body)
        if m:
            image = (page.parent / m.group(1).split("?")[0]).resolve().relative_to(DIST.resolve()).as_posix()
    tags = [
        f'<meta property="og:type" content="{"article" if is_article else "website"}">',
        f'<meta property="og:title" content="{title}">',
        f'<meta property="og:description" content="{desc}">',
        f'<meta property="og:url" content="{SITE_URL}{path}">',
        f'<meta property="og:image" content="{SITE_URL}{image}">',
        '<meta property="og:site_name" content="ウッドプラスチックテクノロジーポータル">',
        '<meta name="twitter:card" content="summary_large_image">',
    ]
    return text.replace("</head>", "\n".join(tags) + "\n</head>", 1)


def stage(preview: bool, today: dt.date, do_prerender: bool = True) -> dict[str, list[str]]:
    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(SITE, DIST, ignore=IGNORE)
    removed: dict[str, list[str]] = {"news": [], "openings": [], "prerendered": [], "problems": [], "fatal": []}

    # お知らせ：本番では下書き・非公開・公開日が未来の記事を外す。確認用では非公開だけ外し、印を付ける
    news_path = DIST / "data" / "news.json"
    news = json.loads(news_path.read_text(encoding="utf-8"))
    kept = []
    for item in news["items"]:
        state = news_state(item, today)
        if state == "hidden" or (not preview and state in ("draft", "scheduled")):
            removed["news"].append(item["slug"])
            continue
        if preview and state == "draft":
            item = {**item, "t": f"【下書き】{item['t']}"}
        elif preview and state == "scheduled":
            item = {**item, "t": f"【{item['d']} 公開予定】{item['t']}"}
        kept.append(item)
    kept_slugs = {i["slug"] for i in kept}
    for slug in removed["news"]:
        if slug not in kept_slugs:
            shutil.rmtree(DIST / "news" / slug, ignore_errors=True)
    news["items"] = kept
    news_path.write_text(json.dumps(news, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 募集一覧：status が「非公開」（本番では「下書き」も）の求人を外す
    recruit_path = DIST / "data" / "recruit.json"
    if recruit_path.is_file():
        recruit = json.loads(recruit_path.read_text(encoding="utf-8"))
        openings = []
        for o in recruit.get("openings", []):
            status = o.get("status", STATUS_PUBLIC)
            if status == STATUS_HIDDEN or (not preview and status == STATUS_DRAFT):
                removed["openings"].append(o.get("title", ""))
                continue
            if preview and status == STATUS_DRAFT:
                o = {**o, "title": f"【下書き】{o.get('title', '')}"}
            openings.append(o)
        recruit["openings"] = openings
        recruit_path.write_text(json.dumps(recruit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    pages = sorted(DIST.rglob("*.html"))
    for page in pages:
        page.write_text(refresh_versions(page, page.read_text(encoding="utf-8")), encoding="utf-8")
    if do_prerender:
        done, problems = prerender(pages)
        removed["prerendered"] = [str(done)]
        removed["problems"] = problems
        # 本番・確認用のサーバーには JSON を置かない。JSON がないと表示できないページが残っていたら止める
        left = pages_needing_json(pages)
        if left:
            removed["fatal"] = [f"JSON を読む処理が残っているページがあります: {'、'.join(left)}"]
            return removed
        shutil.rmtree(DIST / "data", ignore_errors=True)

    for page in pages:
        text = add_social_tags(page, page.read_text(encoding="utf-8"))
        if preview:
            text = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + PREVIEW_BAR, text, count=1)
            text = re.sub(r'<iframe id="mailform"[^>]*></iframe>', FORM_NOTE, text)
            if 'name="robots"' not in text:
                text = text.replace("</head>", '<meta name="robots" content="noindex,nofollow">\n</head>', 1)
        page.write_text(text, encoding="utf-8")
    if preview:
        (DIST / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="サイトのチェックと dist/ づくり")
    parser.add_argument("--check", action="store_true", help="チェックだけ行う")
    parser.add_argument("--preview", action="store_true", help="確認用として作る")
    parser.add_argument("--today", help="今日の日付を変えて試す（例: 2026-12-01）")
    parser.add_argument("--weekday", nargs="+", metavar="DATE", help="日付の曜日を表示する（例: 2026-12-29）")
    parser.add_argument("--no-prerender", action="store_true", help="事前の書き込みをしない")
    args = parser.parse_args()

    if args.weekday:
        for text in args.weekday:
            day = parse_any_date(text.replace("年", "-").replace("月", "-").replace("日", ""))
            print(f"{text}: {day.year}年{day.month}月{day.day}日（{WEEKDAYS[day.weekday()]}）" if day else f"{text}: 日付が読み取れません（2026-12-29 の形で書いてください）")
        return 0

    today = parse_any_date(args.today) if args.today else today_jst()
    if today is None:
        print("--today の日付が読み取れません（2026-12-01 の形で書いてください）")
        return 1
    print(f"今日の日付（日本時間）: {today.year}年{today.month}月{today.day}日（{WEEKDAYS[today.weekday()]}）")

    report, counts = check_all(today)
    for line in report.errors + report.warnings:
        print(line)
    names = {"published": "公開中", "scheduled": "公開予定", "draft": "下書き", "hidden": "非公開"}
    if counts:
        print("お知らせ: " + "、".join(f"{names[k]} {v}件" for k, v in counts.items()))
    if report.errors:
        print(f"\nエラーが {len(report.errors)} 件あります。上の内容を直してください。")
        return 1
    if args.check:
        print("チェックOK（エラーはありません）")
        return 0

    removed = stage(args.preview, today, not args.no_prerender)
    mode = "確認用" if args.preview else "本番用"
    if removed["fatal"]:
        for line in removed["problems"] + removed["fatal"]:
            print(f"[エラー] {line}")
        print(f"\n{mode}のファイルを作れませんでした。サーバーに JSON を置かない作りのため、"
              "すべてのページに中身を書き込めないと止まります（本番サイトは前の状態のまま変わりません）。上の内容を直してください。")
        return 1
    print(f"{mode}のファイルを dist/ に作りました（基準日: {today.isoformat()}）")
    if removed["news"]:
        print(f"  表示しないお知らせ: {'、'.join(removed['news'])}")
    if removed["openings"]:
        print(f"  表示しない求人: {'、'.join(removed['openings'])}")
    if removed["prerendered"]:
        print(f"  中身を書き込んだページ: {removed['prerendered'][0]} ページ（data/ の JSON は外しました）")
    for line in removed["problems"]:
        print(f"  [注意] {line}")
    if args.no_prerender:
        print("  事前の書き込みをしていないため、data/ を残しました（手元での確認用。このままサーバーには置かない）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
