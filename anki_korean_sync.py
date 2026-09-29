#!/usr/bin/env python3
"""Korean cards to go over, from AnkiDroid, into a Google Doc for a Claude project.

Copies AnkiDroid's collection off the phone with adb (read-only; the phone
is not changed), reads Anki's review log for the chosen decks, and replaces
the content of one Google Doc with: the cards answered Again/Hard lately,
and the cards first studied lately. A Claude project with that Doc in its
knowledge sees the current list.

    anki_korean_sync.py decks            # list decks, to pick the Korean ones
    anki_korean_sync.py run [--dry-run]  # copy, write the Doc (or just print it)
    anki_korean_sync.py watch [--every M] # run whenever the collection changed, checking every M minutes

adb must be connected to the phone (e.g. `adb tcpip 5555` once per boot,
then `adb connect 127.0.0.1:5555` from Termux, or the phone's IP from a PC).
Only the Python standard library is used, so it runs in a Termux proot.

Settings live in ~/.config/anki-korean-sync/: config.json
({"decks": [...], "days": 14, "serial": "127.0.0.1:5555"}) and doc-id (the
Doc, made on the first run). Google access comes from an rclone Drive remote
(env ANKI_KOREAN_DRIVE_REMOTE, default "moneo"); scope drive.file is enough.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anki-korean-sync"
CONFIG = CONFIG_DIR / "config.json"
DOC_ID = CONFIG_DIR / "doc-id"
LAST_SEEN = CONFIG_DIR / "last-mtime"
REMOTE = os.environ.get("ANKI_KOREAN_DRIVE_REMOTE", "moneo")
DOC_TITLE = "Anki Korean study words"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
ANKI_DIR = "/sdcard/Android/data/com.ichi2.anki/files/AnkiDroid1"
MAX_PER_SECTION = 60
SEP = "\x1f"  # Anki's separator for fields and deck-name levels


def load_config() -> dict:
    cfg = {"decks": [], "days": 14, "serial": None}
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text()))
    return cfg


def adb(cfg: dict, *args: str, **kw) -> subprocess.CompletedProcess:
    serial = ["-s", cfg["serial"]] if cfg.get("serial") else []
    return subprocess.run(["adb", *serial, *args], check=True, **kw)


def collection_mtime(cfg: dict) -> str:
    out = adb(cfg, "shell", f"stat -c %Y {ANKI_DIR}/collection.anki2 {ANKI_DIR}/collection.anki2-wal 2>/dev/null",
              capture_output=True, text=True).stdout
    return out.strip().replace("\n", ",")


def copy_collection(cfg: dict, tmp: str) -> sqlite3.Connection:
    """A private copy of the collection (with its WAL, so recent answers count)."""
    for name in ("collection.anki2", "collection.anki2-wal"):
        with open(Path(tmp) / name, "wb") as f:
            adb(cfg, "exec-out", f"cat {ANKI_DIR}/{name} 2>/dev/null", stdout=f)
    path = Path(tmp) / "collection.anki2"
    if path.stat().st_size < 1024:
        sys.exit("Couldn't copy AnkiDroid's collection: is adb connected?")
    return sqlite3.connect(path)


def deck_names(db: sqlite3.Connection) -> dict[int, str]:
    return {did: name.replace(SEP, "::") for did, name in db.execute("select id, name from decks")}


def deck_ids(db: sqlite3.Connection, wanted: list[str]) -> list[int]:
    """Ids of the named decks and their subdecks."""
    return sorted(did for did, name in deck_names(db).items()
                  if any(name == w or name.startswith(w + "::") for w in wanted))


def plain(field: str) -> str:
    field = re.sub(r"\[sound:[^\]]*\]", "", field)
    field = re.sub(r"<br\s*/?>|</div>|</p>", " ", field, flags=re.I)
    field = re.sub(r"<[^>]+>", "", field)
    return re.sub(r"\s+", " ", html.unescape(field)).strip()


def collect(db: sqlite3.Connection, dids: list[int], days: int) -> tuple[list[dict], list[dict]]:
    since_ms = int((time.time() - days * 86400) * 1000)
    ids = ",".join(map(str, dids))
    in_decks = f"(c.did in ({ids}) or c.odid in ({ids}))"
    # revlog.ease: 1 Again, 2 Hard, 3 Good, 4 Easy; 0 = manual reschedule.
    struggling = db.execute(f"""
        select c.id, c.nid, sum(r.ease = 1), sum(r.ease = 2), max(r.id), c.lapses, c.type
        from revlog r join cards c on c.id = r.cid
        where r.id >= {since_ms} and r.ease in (1, 2) and {in_decks}
        group by c.id order by sum(r.ease = 1) * 2 + sum(r.ease = 2) desc, max(r.id) desc
    """).fetchall()
    missed = {row[0] for row in struggling}
    # First studied lately: the card's first answer falls in the window.
    learned = db.execute(f"""
        select c.id, c.nid, 0, 0, max(r.id), c.lapses, c.type
        from revlog r join cards c on c.id = r.cid
        where r.ease > 0 and {in_decks}
        group by c.id having min(r.id) >= {since_ms}
        order by max(r.id) desc
    """).fetchall()

    def describe(rows, skip=frozenset()):
        out, seen_notes = [], set()
        for cid, nid, again, hard, last_ms, lapses, ctype in rows:
            if cid in skip or nid in seen_notes:
                continue  # one line per note, even with several card templates
            seen_notes.add(nid)
            flds = db.execute("select flds from notes where id = ?", (nid,)).fetchone()[0]
            fields = [plain(f) for f in flds.split(SEP)]
            out.append({
                "fields": [f for f in fields[:3] if f and not f.startswith("http")],
                "again": again, "hard": hard, "lapses": lapses,
                "last": datetime.fromtimestamp(last_ms / 1000),
                "status": {0: "new", 1: "learning", 2: "review", 3: "relearning"}.get(ctype, "?"),
            })
        return out[:MAX_PER_SECTION]

    return describe(struggling), describe(learned, skip=missed)


def markdown(struggling: list[dict], learned: list[dict], decks: list[str], days: int) -> str:
    def line(w: dict, note: str | None) -> str:
        parts = [" — ".join(w["fields"])]
        if note:
            parts.append(note)
        parts.append(f"Last {w['last']:%b %-d}, {w['status']}")
        # "…for me?" + ". Again ×1" → "…for me? Again ×1", not "?."
        return "- " + "".join(p if i == 0 else (" " if parts[i - 1][-1] in ".?!" else ". ") + p
                              for i, p in enumerate(parts)) + "."

    out = [
        "# Anki Korean study words", "",
        f"Korean cards from the player's Anki decks ({', '.join(decks)}), last {days} days. "
        f"Updated {datetime.now():%Y-%m-%d %H:%M}. Each line is the card's Korean, then its English.", "",
        "## Struggling (answered Again or Hard)", "",
    ]
    out += [line(w, ", ".join(p for p in (
        f"Again ×{w['again']}" if w["again"] else "",
        f"Hard ×{w['hard']}" if w["hard"] else "",
        f"lapsed {w['lapses']}× overall" if w["lapses"] else "",
    ) if p)) for w in struggling] or ["None lately."]
    out += ["", f"## Recently learned (first studied in the last {days} days)", ""]
    out += [line(w, None) for w in learned] or ["None lately."]
    return "\n".join(out) + "\n"


def drive_token() -> str:
    # Any call through the remote refreshes an expired token and saves it.
    subprocess.run(["rclone", "lsf", f"{REMOTE}:", "--max-depth", "1"], check=True, stdout=subprocess.DEVNULL)
    dump = json.loads(subprocess.run(["rclone", "config", "dump"], check=True, capture_output=True, text=True).stdout)
    return json.loads(dump[REMOTE]["token"])["access_token"]


def upload(text: str) -> str:
    token = drive_token()
    body = text.encode()
    if DOC_ID.exists():
        doc = DOC_ID.read_text().strip()
        req = urllib.request.Request(f"{UPLOAD_API}/{doc}?uploadType=media", data=body, method="PATCH", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "text/markdown; charset=UTF-8"})
        try:
            urllib.request.urlopen(req).read()
            return doc
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            print("The Doc is gone; making a new one.", file=sys.stderr)
    boundary = f"anki{int(time.time())}"
    meta = json.dumps({"name": DOC_TITLE, "mimeType": "application/vnd.google-apps.document"}).encode()
    multipart = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + meta +
        f"\r\n--{boundary}\r\nContent-Type: text/markdown; charset=UTF-8\r\n\r\n".encode() + body +
        f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(f"{UPLOAD_API}?uploadType=multipart&fields=id", data=multipart, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}"})
    doc = json.loads(urllib.request.urlopen(req).read())["id"]
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DOC_ID.write_text(doc)
    print(f"Created Google Doc: https://docs.google.com/document/d/{doc}")
    return doc


def run(cfg: dict, dry_run: bool) -> None:
    if not cfg["decks"]:
        sys.exit(f"No decks chosen: put {{\"decks\": [\"Deck name\"]}} in {CONFIG}.")
    with tempfile.TemporaryDirectory() as tmp:
        db = copy_collection(cfg, tmp)
        try:
            dids = deck_ids(db, cfg["decks"])
            if not dids:
                sys.exit(f"None of {cfg['decks']} exist; see `decks`.")
            struggling, learned = collect(db, dids, cfg["days"])
        finally:
            db.close()
    text = markdown(struggling, learned, cfg["decks"], cfg["days"])
    if dry_run:
        print(text)
        return
    doc = upload(text)
    print(f"[{datetime.now():%H:%M}] {len(struggling)} struggling, {len(learned)} new → "
          f"https://docs.google.com/document/d/{doc}", flush=True)


def watch(cfg: dict, every_min: int) -> None:
    """Run whenever AnkiDroid's collection changed since the last upload."""
    while True:
        try:
            mtime = collection_mtime(cfg)
            if mtime and (not LAST_SEEN.exists() or LAST_SEEN.read_text() != mtime):
                run(cfg, dry_run=False)
                LAST_SEEN.write_text(mtime)
        except (subprocess.CalledProcessError, urllib.error.URLError, SystemExit) as e:
            print(f"[{datetime.now():%H:%M}] skipped: {e}", file=sys.stderr, flush=True)
        time.sleep(every_min * 60)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("decks")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="print the Doc text instead of uploading")
    w = sub.add_parser("watch")
    w.add_argument("--every", type=int, default=15, help="minutes between checks (default 15)")
    args = p.parse_args()
    cfg = load_config()

    if args.cmd == "decks":
        with tempfile.TemporaryDirectory() as tmp:
            db = copy_collection(cfg, tmp)
            for did, name in sorted(deck_names(db).items(), key=lambda kv: kv[1]):
                n = db.execute("select count() from cards where did = ? or odid = ?", (did, did)).fetchone()[0]
                if n:
                    print(f"{n:6d}  {name}")
            db.close()
    elif args.cmd == "run":
        run(cfg, args.dry_run)
    else:
        watch(cfg, args.every)


if __name__ == "__main__":
    main()
