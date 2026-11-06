"""消したニュース・求人・職種・社員紹介を、変更の記録（git の履歴）から探して、見る・戻す。

ニュースや求人を取り下げるときは、tools/remove.py で消す。消したものは記録に残っているので、
あとでもう一度使いたくなったら、ここから探して中身を見たり、そのまま戻したりできる。

使い方:
  python tools/history.py list                  消したものの一覧（ニュース・求人・職種・社員紹介）
  python tools/history.py list openings         種類をしぼる（news / openings / jobs / people）
  python tools/history.py show news 20251210    消す直前の中身を見る（新しい記事や求人の参考にするとき）
  python tools/history.py show openings o-1a2b3c
  python tools/history.py restore news 20251210 そのまま戻す（中身・ページのフォルダ・画像）

求人には決まった名前がないので、list に出る「o-」で始まる記号で指定する（list の番号でもよい）。
戻したら python tools/build.py --check を実行してから保存する。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DATA = SITE / "data"
JST = dt.timezone(dt.timedelta(hours=9))

KINDS = {
    "news": {"file": "news.json", "list": "items", "folder": "news", "label": "ニュース"},
    "openings": {"file": "recruit.json", "list": "openings", "folder": None, "label": "求人"},
    "jobs": {"file": "recruit.json", "list": "jobs", "folder": "recruit/jobs", "label": "職種"},
    "people": {"file": "recruit.json", "list": "people", "folder": "recruit/people", "label": "社員紹介"},
}
WORKTREE = {"commit": None, "date": "まだ保存していない変更", "subject": "今の作業"}


# ---------------------------------------------------------------- 共通

def git(*args: str, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"git {' '.join(args)} が失敗しました")
    return res.stdout


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    # site/data の JSON は、この形（字下げ2つ、最後に改行）で書かれている
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def items_of(data, kind: str) -> list[dict]:
    if not isinstance(data, dict):
        return []
    return [x for x in data.get(KINDS[kind]["list"], []) if isinstance(x, dict)]


def opening_key(o: dict) -> tuple:
    return tuple(str(o.get(k, "")) for k in ("co", "job", "site", "emp", "title"))


def opening_group(o: dict) -> tuple:
    return opening_key(o)[:4]


def key_of(item: dict, kind: str):
    return opening_key(item) if kind == "openings" else str(item.get("slug", ""))


def opening_id(o: dict) -> str:
    return "o-" + hashlib.sha1("|".join(opening_key(o)).encode("utf-8")).hexdigest()[:6]


def label_of(item: dict, kind: str) -> str:
    if kind == "news":
        return f"{item.get('d', '')}「{item.get('t', '')}」"
    if kind == "openings":
        return f"{item.get('title', '')}（{item.get('co', '')}・{item.get('site', '')}・{item.get('emp', '')}）"
    if kind == "jobs":
        return str(item.get("name", ""))
    return f"{item.get('name', '')}（{item.get('role', '')}）"


def asset_paths(item) -> list[str]:
    """項目の中で使っている画像や資料（/assets/ から始まるもの）"""
    found: list[str] = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str) and v.startswith("/assets/"):
            p = v.split("?")[0].split("#")[0]
            if p not in found:
                found.append(p)

    walk(item)
    return found


def jst(iso: str) -> str:
    try:
        return dt.datetime.fromisoformat(iso).astimezone(JST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def is_shallow() -> bool:
    return git("rev-parse", "--is-shallow-repository", check=False).strip() == "true"


# ---------------------------------------------------------------- 記録から探す

def snapshots(kind: str) -> list[tuple[dict, list[dict]]]:
    """そのデータファイルの、古い順の版（記録）と、いまの作業中の版"""
    rel = f"site/data/{KINDS[kind]['file']}"
    log = git("log", "--format=%H%x09%cI%x09%s", "--", rel, check=False)
    result: list[tuple[dict, list[dict]]] = []
    for line in reversed([l for l in log.splitlines() if l.strip()]):
        commit, date, subject = (line.split("\t", 2) + ["", ""])[:3]
        text = git("show", f"{commit}:{rel}", check=False)
        try:
            data = json.loads(text)
        except ValueError:
            continue  # 書き方の誤りがあった版は飛ばす
        result.append(({"commit": commit, "date": jst(date), "subject": subject}, items_of(data, kind)))
    current = DATA / KINDS[kind]["file"]
    result.append((WORKTREE, items_of(load_json(current), kind) if current.is_file() else []))
    return result


def deleted(kind: str) -> list[dict]:
    """いまはないが、以前はあったもの。新しく消したものから順に並べる"""
    snaps = snapshots(kind)
    events: dict = {}
    for (old_meta, old), (new_meta, new) in zip(snaps, snaps[1:]):
        new_keys = {key_of(x, kind) for x in new}
        old_keys = {key_of(x, kind) for x in old}
        # 求人は名前がないので、同じ会社・職種・勤務地・雇用形態で、同時に新しい求人が増えていれば「直した」とみなす
        added_groups = {k[:4] for k in new_keys - old_keys} if kind == "openings" else set()
        for x in old:
            k = key_of(x, kind)
            if not k or k in new_keys:
                continue
            if kind == "openings" and k[:4] in added_groups:
                continue
            i = old.index(x)
            events[k] = {"kind": kind, "item": x, "index": i, "existed": old_meta, "deleted": new_meta,
                         # 元の並びに戻せるよう、前後にあった項目を覚えておく
                         "prev": key_of(old[i - 1], kind) if i > 0 else None,
                         "next": key_of(old[i + 1], kind) if i + 1 < len(old) else None}
    current = {key_of(x, kind) for x in snaps[-1][1]}
    found = [e for k, e in events.items() if k not in current]
    found.sort(key=lambda e: e["deleted"]["date"] if e["deleted"]["commit"] else "9999", reverse=True)
    for n, e in enumerate(found, 1):
        e["no"] = n
        e["id"] = opening_id(e["item"]) if kind == "openings" else str(e["item"].get("slug", ""))
    return found


def find(kind: str, target: str) -> dict | None:
    for e in deleted(kind):
        if target in (e["id"], str(e["no"])):
            return e
    return None


def exists_at(commit: str, rel: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{commit}:{rel}"], cwd=ROOT, capture_output=True).returncode == 0


def latest_source(rel: str, fallback: str | None) -> str | None:
    """そのファイル（フォルダ）の、消す直前のいちばん新しい版がある記録"""
    if exists_at("HEAD", rel):
        return "HEAD"  # まだ保存していない作業で消したとき
    last = git("rev-list", "-n", "1", "HEAD", "--", rel, check=False).strip()  # 消した記録
    if last and exists_at(last + "^", rel):
        return last + "^"
    if fallback and exists_at(fallback, rel):
        return fallback
    return None


def files_of(e: dict) -> list[tuple[str, str]]:
    """一緒に戻せる、その項目のページのフォルダと画像・資料（場所, 戻す元の記録）"""
    kind, item = e["kind"], e["item"]
    rels: list[str] = []
    folder = KINDS[kind]["folder"]
    if folder and item.get("slug"):
        rels.append(f"site/{folder}/{item['slug']}")
    rels += ["site" + p for p in asset_paths(item)]
    found = []
    for rel in rels:
        source = latest_source(rel, e["existed"]["commit"])
        if source:
            found.append((rel, source))
    return found


# ---------------------------------------------------------------- 戻す

def insert_position(kind: str, items: list[dict], e: dict) -> int:
    item = e["item"]
    keys = [key_of(x, kind) for x in items]
    if e.get("next") in keys:
        return keys.index(e["next"])  # 元のすぐ後ろにあった項目の前に
    if e.get("prev") in keys:
        return keys.index(e["prev"]) + 1  # 元のすぐ前にあった項目の後ろに
    if kind == "news":
        # 新しい日付が上になるように並んでいる
        for i, x in enumerate(items):
            if str(x.get("d", "")) < str(item.get("d", "")):
                return i
        return len(items)
    if kind == "openings":
        for match in (("co", "job"), ("co",)):
            same = [i for i, x in enumerate(items) if all(x.get(k) == item.get(k) for k in match)]
            if same:
                return same[-1] + 1
        return len(items)
    return min(e["index"], len(items))


def restore(e: dict) -> list[str]:
    kind, item = e["kind"], e["item"]
    path = DATA / KINDS[kind]["file"]
    data = load_json(path)
    items = data.setdefault(KINDS[kind]["list"], [])
    items.insert(insert_position(kind, items, e), item)
    save_json(path, data)
    done = [f"site/data/{KINDS[kind]['file']} に「{label_of(item, kind)}」を戻しました"]
    for rel, source in files_of(e):
        target = ROOT / rel
        if target.is_file():
            continue  # 画像がいまもあるなら、そのまま使う
        git("checkout", source, "--", rel)
        done.append(f"{rel}{'/' if target.is_dir() else ''} を戻しました")
    return done


# ---------------------------------------------------------------- 画面に出す

def print_list(kinds: list[str]) -> None:
    if is_shallow():
        print("※ 記録の一部しか取り出されていません。`git fetch --unshallow origin` を実行してから、もう一度実行してください。\n")
    for kind in kinds:
        found = deleted(kind)
        print(f"【{KINDS[kind]['label']}】消したもの {len(found)} 件")
        for e in found:
            when = e["deleted"]["date"]
            why = e["deleted"]["subject"]
            print(f"  {e['no']:>2}. {e['id']}  {label_of(e['item'], kind)}  ／ 消した日: {when}（{why}）")
        print()
    print("中身を見る: python tools/history.py show 種類 記号　　戻す: python tools/history.py restore 種類 記号")


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("list", "show", "restore"):
        print(__doc__)
        return 1
    if args[0] == "list":
        kinds = [args[1]] if len(args) > 1 else list(KINDS)
        if any(k not in KINDS for k in kinds):
            print(f"種類は {' / '.join(KINDS)} のどれかにしてください。")
            return 1
        print_list(kinds)
        return 0
    if len(args) < 3 or args[1] not in KINDS:
        print(f"使い方: python tools/history.py {args[0]} 種類（{' / '.join(KINDS)}） 記号")
        return 1
    kind, target = args[1], args[2]
    e = find(kind, target)
    if e is None:
        current = [key_of(x, kind) for x in items_of(load_json(DATA / KINDS[kind]["file"]), kind)]
        if kind != "openings" and target in current:
            print(f"「{target}」はいまもあります（消されていません）。")
        else:
            print(f"消した{KINDS[kind]['label']}に「{target}」が見つかりません。python tools/history.py list {kind} で一覧を確かめてください。")
        return 1
    item = e["item"]
    if args[0] == "show":
        print(f"{KINDS[kind]['label']}: {label_of(item, kind)}")
        print(f"消した日: {e['deleted']['date']}（{e['deleted']['subject']}）")
        print(f"この中身が最後にあった版: {e['existed']['date']}（{e['existed']['subject']}）")
        files = files_of(e)
        if files:
            print("一緒に戻せるファイル: " + "、".join(rel for rel, _ in files))
        print("\n" + json.dumps(item, ensure_ascii=False, indent=2))
        return 0
    for line in restore(e):
        print(line)
    if kind == "jobs":
        print("※ 職種を戻したら、「4つの職種」のように職種の数を書いた文も直してください（--check が教えてくれます）。"
              "エントリーフォームの「ご希望の職種」の選択肢は、管理者に足してもらうよう担当者に伝えてください。")
        print("※ この職種の社員紹介や求人も戻すときは、python tools/history.py list で探して戻してください。")
    print("\n次に python tools/build.py --check を実行してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
