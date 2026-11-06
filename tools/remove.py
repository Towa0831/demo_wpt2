"""ニュース・求人・職種・社員紹介を消す（取り下げる）。

項目を JSON から消し、ページのフォルダと、その項目だけで使っていた画像・資料も一緒に消す。
ほかの場所でも使っている画像は残す。消したものは変更の記録に残るので、
tools/history.py で探して、見たり戻したりできる。

使い方:
  python tools/remove.py news 20251210     ニュースの記事を消す
  python tools/remove.py openings          求人の一覧（番号付き）を見る
  python tools/remove.py openings 3        3番の求人を消す
  python tools/remove.py people 04         社員紹介を消す
  python tools/remove.py jobs food         職種を消す（その職種の社員紹介・求人が残っているときは止まる）

消したあとは python tools/build.py --check を実行してから保存する。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import history  # noqa: E402
from history import DATA, KINDS, ROOT, SITE, asset_paths, items_of, label_of, load_json, opening_id, save_json  # noqa: E402

SLUG_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def delete_path(path: Path) -> None:
    """git の記録からも外して消す（保存するときに、消したことも一緒に保存されるように）"""
    rel = path.relative_to(ROOT).as_posix()
    subprocess.run(["git", "rm", "-r", "-q", "--ignore-unmatch", "--", rel], cwd=ROOT, capture_output=True)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def used_elsewhere(asset: str) -> list[str]:
    """その画像・資料を、いまも使っているファイル"""
    needle = asset.lstrip("/")  # JSON では /assets/…、HTML や CSS では ../assets/… のように書かれている
    users = []
    for p in sorted(SITE.rglob("*")):
        if p.is_file() and p.suffix in (".json", ".html", ".css", ".js") and needle in p.read_text(encoding="utf-8"):
            users.append(p.relative_to(ROOT).as_posix())
    return users


def links_to(page: str) -> list[str]:
    """ほかの場所から、そのページへのリンク"""
    users = []
    for p in sorted(SITE.rglob("*")):
        if p.is_file() and p.suffix in (".json", ".html"):
            text = p.read_text(encoding="utf-8")
            if f"/{page}" in text or f"/{page.rstrip('/')}\"" in text:
                users.append(p.relative_to(ROOT).as_posix())
    return users


def list_openings() -> None:
    rec = load_json(DATA / "recruit.json")
    print("求人の一覧（消すときは python tools/remove.py openings 番号）")
    for n, o in enumerate(items_of(rec, "openings"), 1):
        status = f"［{o['status']}］" if o.get("status") else ""
        print(f"  {n:>2}. {label_of(o, 'openings')} {o.get('area', '')}{status}")


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in KINDS:
        print(__doc__)
        return 1
    kind = args[0]
    if kind == "openings" and len(args) == 1:
        list_openings()
        return 0
    if len(args) < 2:
        print(f"使い方: python tools/remove.py {kind} 対象")
        return 1
    target = args[1]
    spec = KINDS[kind]
    path = DATA / spec["file"]
    data = load_json(path)
    items = data.get(spec["list"], [])

    # 消す項目を探す
    if kind == "openings":
        if not target.isdigit() or not 1 <= int(target) <= len(items):
            print(f"求人の番号「{target}」がありません。python tools/remove.py openings で番号を確かめてください。")
            return 1
        index = int(target) - 1
    else:
        if not target or set(target) - SLUG_OK:
            print(f"「{target}」は、フォルダ名（半角の英小文字・数字・ハイフン）で指定してください。")
            return 1
        index = next((i for i, x in enumerate(items) if isinstance(x, dict) and x.get("slug") == target), None)

    folder = SITE / spec["folder"] / target if spec["folder"] else None
    if index is None:
        # 項目はもうないが、フォルダだけ残っているとき（差し戻しのあとなど）は、フォルダと画像を片付ける
        if folder and folder.is_dir():
            past = history.find(kind, target)
            item = past["item"] if past else {}
            delete_path(folder)
            print(f"{KINDS[kind]['label']}「{target}」の項目はすでにないため、残っていたフォルダ {folder.relative_to(ROOT)}/ を消しました。")
            cleanup_assets(item)
            return 0
        print(f"{KINDS[kind]['label']}「{target}」が見つかりません。")
        return 1
    item = items[index]

    # 職種は、その職種の社員紹介・求人が残っていると消せない
    if kind == "jobs":
        rec = data
        people = [p for p in items_of(rec, "people") if p.get("jobSlug") == target]
        openings = [(n, o) for n, o in enumerate(items_of(rec, "openings"), 1) if o.get("job") == target]
        if people or openings:
            print(f"職種「{item.get('name', target)}」には、まだ次のものがあります。先に消すか、ほかの職種に変えてから、もう一度実行してください。")
            for p in people:
                print(f"  社員紹介 {p.get('slug')}: {label_of(p, 'people')}  → python tools/remove.py people {p.get('slug')}")
            for n, o in openings:
                print(f"  求人 {n}番: {label_of(o, 'openings')}  → python tools/remove.py openings {n}")
            print("  （求人を続けて消すときは、番号がずれるので、1件消すたびに python tools/remove.py openings で番号を確かめる）")
            return 1

    # JSON から消す
    del items[index]
    save_json(path, data)
    print(f"{KINDS[kind]['label']}「{label_of(item, kind)}」を site/data/{spec['file']} から消しました。")

    # ページのフォルダ
    if folder and folder.is_dir():
        delete_path(folder)
        print(f"ページのフォルダ {folder.relative_to(ROOT)}/ を消しました。")

    cleanup_assets(item)

    # ほかの場所からのリンク
    if folder:
        page = f"{spec['folder']}/{target}/"
        refs = links_to(page)
        if refs:
            print(f"[注意] ほかの場所に、このページ（/{page}）へのリンクがあります: {'、'.join(refs)}")
            print("       リンクを外すか、別のページに変えるかを担当者に確認してください（このままだと --check がエラーになります）。")
    if kind == "jobs":
        print("[注意] 「4つの職種」のように職種の数を書いた文を直してください（--check が場所を教えてくれます）。")
        print("[注意] エントリーフォームの「ご希望の職種」の選択肢から外す作業は、管理者に頼むよう担当者に伝えてください。")

    key = opening_id(item) if kind == "openings" else target
    print(f"\nあとで戻すとき: python tools/history.py restore {kind} {key}（保存したあとでも戻せます）")
    print("次に python tools/build.py --check を実行してください。")
    return 0


def cleanup_assets(item: dict) -> None:
    for asset in asset_paths(item):
        file = SITE / asset.lstrip("/")
        if not file.is_file():
            continue
        users = used_elsewhere(asset)
        if users:
            print(f"画像・資料 {asset} は、ほかでも使っているので残しました（{'、'.join(users)}）。")
        else:
            delete_path(file)
            print(f"この項目だけで使っていた {asset} を消しました。")


if __name__ == "__main__":
    sys.exit(main())
