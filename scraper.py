"""
Discord message exporter (read-only).

Exports message history from a DM, group DM, or channel you already have
access to. Each export writes three files into exports/<id>_<name>/:

    .txt   plain transcript, ready to paste into an LLM (auto-split if huge)
    .html  offline Discord-style viewer with images, replies, search
    .json  full data

Images, stickers and avatars are saved to media/ next to them (Discord's CDN
links expire, so they're grabbed at export time and reused on later runs).

    python scraper.py                    interactive: pick a chat from a list
    python scraper.py <id|link> [opts]   one-shot export, no prompts
    python scraper.py --list             show recent chats and exit
    python scraper.py --serve            local web viewer (also: press w in the menu)

The web viewer (http://127.0.0.1:8765/) lists your exports and recent DMs with
live pfps / names / IDs, renders any export (old ones too), and can show a
chat live from the API without saving anything.

This script only reads message history. It does not send messages, react,
change presence, or otherwise act as your account. It uses your own account
token to call the same read APIs the official client uses when you scroll.

Note: using a personal account token with a third-party client is against
Discord's Terms of Service. That's your call as the account owner; this
script sticks to read-only history fetches to keep the footprint small.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import html_view
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.table import Table

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
TOKEN_PLACEHOLDER = "YOUR_DISCORD_TOKEN_HERE"

# Overridable from config.json ("timezone", "output_dir", "media").
TZ = ZoneInfo("America/New_York")
OUTPUT_DIR = ROOT / "exports"
MEDIA_MODE = "images"   # what the "images" toggle grabs: "images" or "all" (any attachment)
SAVE_MESSAGES = True    # write .txt/.html/.json transcripts
SAVE_IMAGES = True      # download images into media/

console = Console(highlight=False)

PAGE_SIZE = 15


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_config(interactive: bool) -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    token = (cfg.get("token") or "").strip()
    if not token or token == TOKEN_PLACEHOLDER:
        if not interactive:
            sys.exit(f"No token set. Put your token in {CONFIG_PATH} or run without arguments to set it up.")
        cfg["token"] = ask_token("No Discord token found.")
        save_config(cfg)

    global TZ, OUTPUT_DIR, MEDIA_MODE, SAVE_MESSAGES, SAVE_IMAGES
    TZ = ZoneInfo(cfg.get("timezone", "America/New_York"))
    OUTPUT_DIR = (ROOT / cfg.get("output_dir", "exports")).resolve()
    MEDIA_MODE = "all" if cfg.get("media") == "all" else "images"
    SAVE_MESSAGES = bool(cfg.get("save_messages", True))
    SAVE_IMAGES = bool(cfg.get("save_images", cfg.get("media") != "none"))
    if not (SAVE_MESSAGES or SAVE_IMAGES):
        SAVE_MESSAGES = True
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def ask_token(reason: str) -> str:
    console.print(f"[yellow]{reason}[/] Paste your token below (input is hidden). It is saved to config.json.")
    token = getpass.getpass("Token: ").strip().strip('"')
    if not token:
        sys.exit("No token entered.")
    return token


# --------------------------------------------------------------------------- #
# Time parsing
# --------------------------------------------------------------------------- #

DATE_FORMATS = [
    "%m/%d/%Y %I:%M%p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%m/%d/%y %I:%M%p", "%m/%d/%y %I:%M %p", "%m/%d/%y %H:%M", "%m/%d/%y",
    "%Y-%m-%d %H:%M", "%Y-%m-%d",
]
YEARLESS_FORMATS = ["%m/%d %I:%M%p", "%m/%d %I:%M %p", "%m/%d"]
RELATIVE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(mo|min|m|hr|h|d|w|y)[a-z]*$")
RELATIVE_UNITS = {
    "min": timedelta(minutes=1), "m": timedelta(minutes=1),
    "hr": timedelta(hours=1), "h": timedelta(hours=1),
    "d": timedelta(days=1), "w": timedelta(weeks=1),
    "mo": timedelta(days=30), "y": timedelta(days=365),
}
WHEN_HELP = "7d, 12h, 2w, yesterday, 5/16/2026 5:32pm, 2026-05-16"


def parse_when(raw: str) -> datetime:
    """Parse '7d', 'yesterday', '5/16/2026 5:32pm', etc. into an aware datetime in TZ."""
    s = raw.strip().lower()
    now = datetime.now(TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if s in ("", "now"):
        return now
    if s == "today":
        return midnight
    if s == "yesterday":
        return midnight - timedelta(days=1)

    m = RELATIVE_RE.match(s)
    if m:
        return now - float(m.group(1)) * RELATIVE_UNITS[m.group(2)]

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError:
            pass
    for fmt in YEARLESS_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(year=now.year, tzinfo=TZ)
        except ValueError:
            pass
    raise ValueError(f"Couldn't understand '{raw}'. Try: {WHEN_HELP}")


def fmt_time(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%m/%d/%Y %I:%M:%S %p")


def fmt_short(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%b %d %Y, %I:%M %p")


# --------------------------------------------------------------------------- #
# Export folders
# --------------------------------------------------------------------------- #

def safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)


@dataclass
class Saved:
    """A previously exported target (exports/<id>_<name>/.meta.json)."""
    id: int
    name: str
    folder: Path
    meta: dict

    @property
    def last_export(self) -> float:
        return self.meta.get("last_export") or self.folder.stat().st_mtime

    def last_message_id(self) -> int | None:
        """Newest exported message ID. Old folders lack it in meta, so scan their JSON once."""
        if self.meta.get("last_message_id"):
            return int(self.meta["last_message_id"])
        if self.meta.get("_scanned"):
            return None
        best = None
        for f in self.folder.glob("*.json"):
            if f.name == ".meta.json":
                continue
            try:
                ids = [int(m["id"]) for m in json.loads(f.read_text(encoding="utf-8"))]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if ids:
                best = max(best or 0, max(ids))
        if best:
            self.meta["last_message_id"] = str(best)
        else:
            self.meta["_scanned"] = True
        return best


def scan_exports() -> dict[int, Saved]:
    found = {}
    if not OUTPUT_DIR.is_dir():
        return found
    for folder in OUTPUT_DIR.iterdir():
        meta_path = folder / ".meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            tid = int(meta["id"])
        except (OSError, ValueError, KeyError):
            continue
        found[tid] = Saved(tid, meta.get("username") or str(tid), folder, meta)
    return found


def target_dir(target_id: int, name: str) -> tuple[Path, dict]:
    """
    Each target gets exports/<id>_<name>/. Folders are matched by ID via
    .meta.json, so if the name changes the folder is renamed in place.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    desired = OUTPUT_DIR / f"{target_id}_{safe_name(name)}"
    existing = scan_exports().get(target_id)
    meta = {}
    if existing:
        existing.last_message_id()  # backfills meta["last_message_id"] for old folders
        meta = existing.meta
        meta.pop("_scanned", None)
        if existing.folder != desired:
            existing.folder.rename(desired)
            console.print(f"[dim]Renamed folder {existing.folder.name} -> {desired.name}[/]")
    desired.mkdir(exist_ok=True)
    return desired, meta


# --------------------------------------------------------------------------- #
# Messages -> records -> files
# --------------------------------------------------------------------------- #

CUSTOM_EMOJI_RE = re.compile(r"<a?(:\w+:)\d+>")
ATTACHMENT_ID_RE = re.compile(r"/attachments/\d+/(\d+)/")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".heic"}
MEDIA_MODES = ("images", "all")
MAX_FILE_BYTES = 25 * 1024 * 1024  # bigger attachments keep their link instead
TXT_SPLIT_CHARS = 350_000  # ~90k tokens per .txt part, so each part fits an LLM context window


def message_to_record(msg: discord.Message) -> dict:
    is_system = msg.type not in (discord.MessageType.default, discord.MessageType.reply)
    content = msg.content
    clean = msg.clean_content
    if is_system:
        try:
            content = clean = msg.system_content or ""
        except Exception:
            pass

    rec = {
        "id": msg.id,
        "timestamp_est": fmt_time(msg.created_at),  # local time in the configured timezone
        "timestamp": msg.created_at.isoformat(),
        "author": str(msg.author),
        "author_id": msg.author.id,
        "author_display": msg.author.display_name,
        "content": content,
        "clean_content": clean,  # mentions resolved to names
        "attachments": [a.url for a in msg.attachments],
        "edited": msg.edited_at is not None,
    }
    if is_system:
        rec["type"] = msg.type.name
    if msg.reference and msg.reference.message_id and msg.type == discord.MessageType.reply:
        rec["reply_to"] = msg.reference.message_id
        author = getattr(msg.reference.resolved, "author", None)
        if author:
            rec["reply_to_author"] = str(author)
    if msg.stickers:
        rec["stickers"] = [s.name for s in msg.stickers]
    embeds = [e.url or e.title for e in msg.embeds if e.url or e.title]
    if embeds:
        rec["embeds"] = embeds
    return rec


# ---- media ---------------------------------------------------------------- #

def safe_filename(name: str, limit: int = 80) -> str:
    stem, ext = os.path.splitext(name)
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)[: limit - len(ext)]
    ext = "".join(c for c in ext if c.isalnum() or c == ".")[:8]
    return (stem or "file") + ext.lower()


def url_ext(url: str, default: str = ".png") -> str:
    ext = os.path.splitext(url.split("?", 1)[0])[1].lower()
    return ext if 1 < len(ext) <= 6 else default


def media_jobs(msg: discord.Message, rec: dict, mode: str) -> list[tuple[dict, str, str]]:
    """(record, url, filename) for every file worth saving locally from this message."""
    jobs = []  # computed even with images off, so the HTML can still show files saved by earlier runs
    if SAVE_MESSAGES:  # avatars only matter for the HTML view
        avatar = msg.author.display_avatar.with_size(64)
        avatar_name = f"avatar_{msg.author.id}_{avatar.key}{url_ext(avatar.url)}"
        rec["author_avatar"] = f"media/{avatar_name}"
        jobs.append((None, avatar.url, avatar_name))
    for a in msg.attachments:
        is_image = (a.content_type or "").startswith("image/") or url_ext(a.filename, "") in IMAGE_EXTS
        if (mode == "all" or is_image) and a.size <= MAX_FILE_BYTES:
            jobs.append((rec, a.url, f"{a.id}_{safe_filename(a.filename)}"))
    for i, e in enumerate(msg.embeds):
        # gifs / direct image links / rich embeds with a picture; skip link-preview thumbnails
        media = e.thumbnail if e.type in ("image", "gifv") else e.image
        url = getattr(media, "proxy_url", None) or getattr(media, "url", None)
        if url:
            jobs.append((rec, url, f"{msg.id}_embed{i}{url_ext(url)}"))
    for st in msg.stickers if SAVE_MESSAGES else ():
        if st.format != discord.StickerFormatType.lottie:
            jobs.append((rec, st.url, f"sticker_{st.id}{url_ext(st.url)}"))
    return jobs


def unique_jobs(jobs) -> dict[str, tuple[str, datetime | None]]:
    """filename -> (url, message time). Files are named by Discord IDs, so they're stable across runs."""
    unique = {}
    for rec, url, name in jobs:
        if name not in unique:
            unique[name] = (url, datetime.fromisoformat(rec["timestamp"]) if rec else None)
    return unique


async def download_media(client: discord.Client, media_dir: Path, jobs, done: set, on_progress) -> int:
    """Download each unique file once; files from earlier exports are reused. Returns the failure count."""
    media_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(4)
    failed = 0

    async def fetch(url: str) -> bytes:
        for attempt in range(3):
            try:
                return await client.http.get_from_cdn(url)
            except (discord.NotFound, discord.Forbidden):
                raise  # expired or deleted: retrying won't help
            except discord.HTTPException:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)

    async def one(name: str, url: str, when: datetime | None) -> None:
        nonlocal failed
        path = media_dir / name
        if not path.exists():
            async with sem:
                try:
                    data = await fetch(url)
                except Exception:
                    failed += 1
                    on_progress()
                    return
            part = path.with_name(path.name + ".part")  # never leave a truncated file behind
            part.write_bytes(data)
            part.replace(path)
            if when:
                os.utime(path, (when.timestamp(), when.timestamp()))
        done.add(name)
        on_progress()

    await asyncio.gather(*(one(n, u, t) for n, (u, t) in unique_jobs(jobs).items()))
    return failed


def attach_files(records: list[dict], jobs, done: set) -> None:
    for rec, _, name in jobs:
        if rec is not None and name in done and f"media/{name}" not in rec.setdefault("files", []):
            rec["files"].append(f"media/{name}")
    for rec in records:
        if rec.get("author_avatar", "").removeprefix("media/") not in done:
            rec.pop("author_avatar", None)


# ---- output files --------------------------------------------------------- #

def record_to_line(r: dict) -> str:
    who = r["author"]
    if r.get("reply_to_author"):
        who += f" (replying to {r['reply_to_author']})"
    local = {f.split("/")[-1].split("_", 1)[0]: f for f in r.get("files", [])}
    parts = [CUSTOM_EMOJI_RE.sub(r"\1", r.get("clean_content", r["content"]))]
    parts += [f"[sticker: {s}]" for s in r.get("stickers", [])]
    for url in r["attachments"]:
        m = ATTACHMENT_ID_RE.search(url)
        path = local.get(m.group(1)) if m else None
        if path:
            kind = "image" if url_ext(path, "") in IMAGE_EXTS else "file"
            parts.append(f"[{kind}: {path}]")
        else:
            parts.append(url)
    if r["edited"]:
        parts.append("(edited)")
    text = " ".join(p for p in parts if p)
    if r.get("type"):
        return f"[{r['timestamp_est']}] * {text or r['type']}"  # system text already names the author
    return f"[{r['timestamp_est']}] {who}: {text}"


def split_lines(lines: list[str], max_chars: int) -> list[list[str]]:
    chunks, cur, size = [], [], 0
    for line in lines:
        if cur and size + len(line) > max_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    return chunks + [cur] if cur else chunks


def write_outputs(records: list[dict], folder: Path, name: str) -> dict[str, Path]:
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    paths = {ext: folder / f"{stamp}.{ext}" for ext in ("txt", "html", "json")}
    span = f"{records[0]['timestamp_est']} -> {records[-1]['timestamp_est']} ({TZ.key})"

    paths["json"].write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    header = f"# {name} | {span} | {len(records)} messages\n"
    chunks = split_lines([record_to_line(r) for r in records], TXT_SPLIT_CHARS)
    for i, chunk in enumerate(chunks, 1):
        part = f" | part {i} of {len(chunks)}" if len(chunks) > 1 else ""
        path = paths["txt"] if i == 1 else folder / f"{stamp}_part{i}.txt"
        path.write_text(header.rstrip("\n") + part + "\n" + "\n".join(chunk) + "\n", encoding="utf-8")
    if len(chunks) > 1:
        paths["txt"] = folder / f"{stamp}.txt  (+{len(chunks) - 1} more parts)"
    paths["html"].write_text(html_view.render(records, title=name, subtitle=span, tz=TZ), encoding="utf-8")
    return paths


def update_meta(folder: Path, meta: dict, target_id: int, name: str, kind: str, newest: int, media_complete: bool) -> None:
    """Remember how far we got, separately for transcripts and images, so 'new since last' works for each."""
    def bump(key):
        meta[key] = str(max(newest, int(meta.get(key) or 0)))
    if SAVE_MESSAGES:
        bump("last_message_id")
    if SAVE_IMAGES and media_complete:
        bump("last_media_id")
    meta.update(id=str(target_id), username=name, kind=kind, last_export=time.time())
    (folder / ".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Discord session (runs on a background thread so prompts stay responsive)
# --------------------------------------------------------------------------- #

class Session:
    def __init__(self, token: str):
        self.token = token
        self.client = discord.Client()
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None

        @self.client.event
        async def on_ready():
            self._ready.set()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.client.start(self.token))
        except BaseException as e:  # surfaced to the main thread by connect()
            self._error = e
        finally:
            self._ready.set()

    def connect(self, timeout: float = 90) -> None:
        threading.Thread(target=self._run, daemon=True).start()
        deadline = time.monotonic() + timeout
        while not self._ready.wait(0.1):
            if time.monotonic() > deadline:
                raise TimeoutError("Timed out connecting to Discord.")
        if self._error:
            raise self._error

    def call(self, coro):
        """Run a coroutine on the client loop and wait for it (Ctrl+C friendly)."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            while not fut.done():
                time.sleep(0.05)
        except KeyboardInterrupt:
            fut.cancel()
            raise
        return fut.result()

    def close(self) -> None:
        if self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.client.close(), self.loop).result(timeout=5)
            except Exception:
                pass


def identity(ch) -> tuple[int, str, str]:
    """(folder id, name, kind). DMs are keyed by the other user's ID."""
    if isinstance(ch, discord.DMChannel):
        return ch.recipient.id, str(ch.recipient), "dm"
    if isinstance(ch, discord.GroupChannel):
        return ch.id, str(ch), "group"
    return ch.id, getattr(ch, "name", None) or f"channel_{ch.id}", "channel"


def label_for(ch) -> str:
    if isinstance(ch, discord.DMChannel):
        u = ch.recipient
        return u.display_name if u.display_name == u.name else f"{u.display_name} ({u.name})"
    if isinstance(ch, discord.GroupChannel):
        return str(ch)
    guild = getattr(ch, "guild", None)
    return f"#{ch.name} ({guild.name})" if guild else f"#{getattr(ch, 'name', ch.id)}"


async def resolve_target(client: discord.Client, tid: int):
    """Turn a user ID or channel ID into something with .history()."""
    ch = client.get_channel(tid)
    if ch:
        return ch
    for pc in client.private_channels:
        if isinstance(pc, discord.DMChannel) and pc.recipient.id == tid:
            return pc
    user = client.get_user(tid)
    if user is None:
        try:
            return await client.fetch_channel(tid)
        except discord.HTTPException:
            pass
        try:
            user = await client.fetch_user(tid)
        except discord.NotFound:
            raise LookupError(f"No channel or user found with ID {tid} (or you can't access it).")
    return user.dm_channel or await user.create_dm()


async def fetch_history(channel, after, before, sink: list, jobs: list, on_progress) -> None:
    async for msg in channel.history(after=after, before=before, limit=None, oldest_first=True):
        rec = message_to_record(msg)
        sink.append(rec)
        jobs += media_jobs(msg, rec, MEDIA_MODE)
        on_progress(len(sink), msg.created_at)


# --------------------------------------------------------------------------- #
# Picking a target
# --------------------------------------------------------------------------- #

@dataclass
class Entry:
    id: int
    label: str
    kind: str
    channel: object = None
    saved: Saved | None = None


def build_entries(client: discord.Client, saved: dict[int, Saved]) -> list[Entry]:
    """Previously exported chats first (most recent export first), then other DMs by activity."""
    by_id: dict[int, Entry] = {}
    for pc in sorted(client.private_channels, key=lambda c: c.last_message_id or 0, reverse=True):
        tid, _, kind = identity(pc)
        by_id[tid] = Entry(tid, label_for(pc), kind, pc, saved.get(tid))
    for s in saved.values():
        if s.id not in by_id:
            ch = client.get_channel(s.id)
            label = label_for(ch) if ch else s.name
            by_id[s.id] = Entry(s.id, label, s.meta.get("kind") or ("channel" if ch else "?"), ch, s)
    exported = sorted((e for e in by_id.values() if e.saved), key=lambda e: e.saved.last_export, reverse=True)
    others = [e for e in by_id.values() if not e.saved]
    return exported + others


def parse_target(raw: str) -> int | None:
    """Accepts a raw ID, <@mention>, <#channel>, or a discord.com/channels/... link."""
    raw = raw.strip()
    m = re.search(r"channels/(?:@me|\d+)/(\d+)", raw)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"<(?:@!?|#)(\d+)>|(\d{15,21})", raw)
    if m:
        return int(m.group(1) or m.group(2))
    return None


KIND_LABEL = {"dm": "DM", "group": "Group", "channel": "Channel", "?": ""}


def show_entries(entries: list[Entry], query: str) -> None:
    title = f"Chats matching '{query}'" if query else "Recent chats"
    t = Table(title=title, title_justify="left", header_style="bold", box=None, padding=(0, 2))
    t.add_column("#", justify="right", style="cyan")
    t.add_column("Name")
    t.add_column("Type", style="dim")
    t.add_column("Last export", style="green")
    for i, e in enumerate(entries, 1):
        last = datetime.fromtimestamp(e.saved.last_export, TZ).strftime("%b %d %Y") if e.saved else ""
        t.add_row(str(i), e.label, KIND_LABEL.get(e.kind, e.kind), last)
    console.print(t)


def pick_target(entries: list[Entry], saved: dict[int, Saved]) -> Entry | None:
    query = ""
    while True:
        shown = [e for e in entries if query.lower() in e.label.lower()] if query else entries
        shown = shown[:PAGE_SIZE * (2 if query else 1)]
        console.print()
        if shown:
            show_entries(shown, query)
        else:
            console.print(f"[yellow]Nothing matches '{query}'.[/]")
        ans = Prompt.ask(
            "\n[bold]Pick a number[/], type to search, or paste an ID / link  [dim](w = web viewer, s = settings, q = quit)[/]",
            default="", show_default=False,
        ).strip()

        if ans.lower() in ("q", "quit", "exit"):
            return None
        if ans.lower() == "s":
            return "settings"
        if ans.lower() == "w":
            return "web"
        if ans.isdigit() and len(ans) <= 4:
            if 1 <= int(ans) <= len(shown):
                return shown[int(ans) - 1]
            console.print(f"[red]Pick 1-{len(shown)}.[/]")
            continue
        tid = parse_target(ans)
        if tid:
            match = next((e for e in entries if e.id == tid), None)
            return match or Entry(tid, str(tid), "?", None, saved.get(tid))
        query = ans


# --------------------------------------------------------------------------- #
# Picking a time range
# --------------------------------------------------------------------------- #

@dataclass
class Range:
    after: object  # datetime | discord.Object | None
    before: datetime | None
    label: str


def what_label() -> str:
    images = "images & files" if MEDIA_MODE == "all" else "images"
    return {(True, True): f"messages + {images}", (True, False): "messages", (False, True): f"{images} only"}[
        (SAVE_MESSAGES, SAVE_IMAGES)]


def range_since_last(saved: Saved | None) -> Range | None:
    """Continue from the last export. Images-only runs track their own position."""
    if not saved:
        return None
    last_id = saved.last_message_id() if SAVE_MESSAGES else int(saved.meta.get("last_media_id") or 0)
    if not last_id:
        return None
    when = discord.utils.snowflake_time(last_id)
    return Range(discord.Object(last_id), None, f"new since last export ({fmt_short(when)})")


def toggle_save(what: str, cfg: dict) -> None:
    global SAVE_MESSAGES, SAVE_IMAGES
    if what == "m":
        SAVE_MESSAGES = not SAVE_MESSAGES
    else:
        SAVE_IMAGES = not SAVE_IMAGES
    if not (SAVE_MESSAGES or SAVE_IMAGES):  # need at least one; flip the other on
        SAVE_MESSAGES, SAVE_IMAGES = what != "m", what == "m"
    cfg.update(save_messages=SAVE_MESSAGES, save_images=SAVE_IMAGES)
    save_config(cfg)


def pick_range(entry: Entry, cfg: dict) -> Range | None:
    while True:
        now = datetime.now(TZ)
        options: list[tuple[str, Range | None]] = []
        incremental = range_since_last(entry.saved)
        if incremental:
            options.append(("New since last export", incremental))
        options += [
            ("Last 24 hours", Range(now - timedelta(days=1), None, "last 24 hours")),
            ("Last 7 days", Range(now - timedelta(days=7), None, "last 7 days")),
            ("Last 30 days", Range(now - timedelta(days=30), None, "last 30 days")),
            ("Everything", Range(None, None, "all history")),
            ("Custom range...", None),
        ]

        box = lambda on: "[green]\\[x][/]" if on else "[dim]\\[ ][/]"
        console.print(f"\n[bold]{entry.label}[/] - what time range?")
        for i, (text, _) in enumerate(options, 1):
            console.print(f"  [cyan]{i}[/]) {text}")
        console.print(f"  Save: {box(SAVE_MESSAGES)} messages [cyan](m)[/]   {box(SAVE_IMAGES)} "
                      f"{'images & files' if MEDIA_MODE == 'all' else 'images'} [cyan](i)[/]")
        choice = Prompt.ask("Choice [dim](m/i to toggle, b = back)[/]",
                            choices=[str(i) for i in range(1, len(options) + 1)] + ["m", "i", "b"],
                            default="1", show_choices=False)
        if choice in ("m", "i"):
            toggle_save(choice, cfg)
            continue
        if choice == "b":
            return None
        rng = options[int(choice) - 1][1]
        return rng or ask_custom_range()


def ask_custom_range() -> Range:
    console.print(f"[dim]Examples: {WHEN_HELP}. Times are {TZ.key}.[/]")
    while True:
        try:
            start = parse_when(Prompt.ask("From"))
            end = parse_when(Prompt.ask("To", default="now"))
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            continue
        if end <= start:
            console.print("[red]'To' must be after 'From'.[/]")
            continue
        return Range(start, end, f"{fmt_short(start)} -> {fmt_short(end)}")


# --------------------------------------------------------------------------- #
# Running an export
# --------------------------------------------------------------------------- #

def run_export(session: Session, entry: Entry, rng: Range) -> tuple[Path, Path | None] | None:
    """Returns (folder to open, html view or None), or None if nothing was found."""
    channel = entry.channel
    if channel is None:
        with console.status("Looking up target..."):
            channel = session.call(resolve_target(session.client, entry.id))
    tid, name, kind = identity(channel)
    label = label_for(channel)

    records: list[dict] = []
    jobs: list = []
    done: set = set()
    failed = 0
    interrupted = False
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), TimeElapsedColumn(),
                  console=console, transient=True) as progress:
        task = progress.add_task(f"Fetching {label} ({rng.label}, {what_label()})...")

        def on_fetch(n, created):
            progress.update(task, description=f"Fetching {label}: [bold]{n:,}[/] messages, at {fmt_short(created)}")

        try:
            session.call(fetch_history(channel, rng.after, rng.before, records, jobs, on_fetch))
        except KeyboardInterrupt:
            interrupted = True

        if not records:
            console.print(f"[yellow]No messages in {label} for {rng.label}.[/]")
            return None

        folder, meta = target_dir(tid, name)
        media_complete = True
        if jobs and not SAVE_IMAGES:  # no downloads, just reuse what's already on disk
            done.update(n for n in unique_jobs(jobs) if (folder / "media" / n).exists())
        elif jobs:  # still save images found before a Ctrl+C; a second Ctrl+C stops the download
            n_files = len(unique_jobs(jobs))
            progress.update(task, description=f"Saving {n_files:,} files to media/", total=n_files, completed=0)
            try:
                failed = session.call(download_media(session.client, folder / "media", jobs, done,
                                                     lambda: progress.advance(task)))
            except KeyboardInterrupt:
                interrupted, media_complete = True, False

    attach_files(records, jobs, done)
    update_meta(folder, meta, tid, name, kind, max(int(r["id"]) for r in records), media_complete)
    paths = write_outputs(records, folder, name) if SAVE_MESSAGES else {}
    if SAVE_IMAGES and (folder / "media").is_dir():
        paths["media"] = folder / "media"
    n_media = sum(1 for r in records for f in r.get("files", []) if "/sticker_" not in f)

    if not SAVE_MESSAGES and not n_media:
        console.print(f"[yellow]No images in {label} for {rng.label} ({len(records):,} messages checked).[/]")
        return None
    show_summary(label, records, paths, n_media, failed, interrupted)
    return (paths["media"] if not SAVE_MESSAGES else folder), paths.get("html")


def show_summary(label: str, records: list[dict], paths: dict[str, Path], n_media: int, failed: int,
                 interrupted: bool) -> None:
    if SAVE_MESSAGES:
        authors = Counter(r["author"] for r in records)
        headline = f"[bold]{len(records):,}[/] messages from [bold]{label}[/]"
    else:
        authors = Counter(r["author"] for r in records for f in r.get("files", []))
        headline = f"[bold]{n_media:,}[/] images from [bold]{label}[/] [dim]({len(records):,} messages checked)[/]"
    lines = [
        headline,
        f"{records[0]['timestamp_est']}  ->  {records[-1]['timestamp_est']}",
        "",
        *(f"  {a}: {n:,}" for a, n in authors.most_common(6)),
        "",
    ]
    if n_media or failed:
        lines.append(f"[dim]{n_media:,} images/files in media/[/]" + (f", [yellow]{failed} couldn't be downloaded[/]" if failed else ""))
    lines += [f"[dim]{k.upper():<5}[/] {v}" for k, v in paths.items()]
    if interrupted:
        lines.insert(0, "[yellow]Stopped early - saved what was fetched so far.[/]\n")
    console.print(Panel("\n".join(lines), title="Export saved", border_style="green", expand=False))


def open_web(session: Session) -> str:
    """Start the local web viewer (once) on the client's loop and open it in the browser."""
    if not getattr(session, "web_url", None):
        import web
        session.web_url = session.call(web.start(session.client, sys.modules[__name__]))
    console.print(f"[green]Web viewer running at[/] [bold]{session.web_url}[/] [dim](stays up while this window is open)[/]")
    webbrowser.open(session.web_url)
    return session.web_url


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path])
    else:
        subprocess.run(["xdg-open", path])


def settings_menu(cfg: dict) -> None:
    global MEDIA_MODE, TZ
    while True:
        console.print(f"\n[bold]Settings[/] [dim](saved to config.json)[/]\n"
                      f"  [cyan]1[/]) 'Images' grabs: [bold]{'images only' if MEDIA_MODE == 'images' else 'all attachments (videos, docs, ...)'}[/]\n"
                      f"  [cyan]2[/]) Timezone:       [bold]{TZ.key}[/]")
        choice = Prompt.ask("Change which? [dim](Enter = back)[/]", choices=["1", "2", ""],
                            default="", show_choices=False)
        if choice == "1":
            MEDIA_MODE = MEDIA_MODES[(MEDIA_MODES.index(MEDIA_MODE) + 1) % len(MEDIA_MODES)]
            cfg["media"] = MEDIA_MODE
        elif choice == "2":
            raw = Prompt.ask("Timezone (e.g. America/Chicago, Europe/London, UTC)", default=TZ.key)
            try:
                TZ = ZoneInfo(raw.strip())
                cfg["timezone"] = TZ.key
            except Exception:
                console.print(f"[red]Unknown timezone '{raw}'.[/]")
                continue
        else:
            return
        save_config(cfg)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def interactive(session: Session, cfg: dict) -> None:
    while True:
        saved = scan_exports()
        entry = pick_target(build_entries(session.client, saved), saved)
        if entry is None:
            return
        if entry == "settings":
            settings_menu(cfg)
            continue
        if entry == "web":
            open_web(session)
            continue
        rng = pick_range(entry, cfg)
        if rng is None:
            continue
        try:
            result = run_export(session, entry, rng)
        except (LookupError, discord.HTTPException) as e:
            console.print(f"[red]{e}[/]")
            continue
        if not result:
            continue
        folder, html_path = result

        view = "[cyan]v[/] = view in browser, " if html_path else ""
        while True:
            ans = Prompt.ask(f"\n{view}[cyan]o[/] = open folder, [dim]Enter[/] = export another, [cyan]q[/] = quit",
                             default="", show_default=False).strip().lower()
            if ans == "v" and html_path:
                open_path(html_path)
            elif ans == "o":
                open_path(folder)
            elif ans == "q":
                return
            else:
                break


def one_shot(session: Session, args) -> int:
    tid = parse_target(args.target)
    if tid is None:
        console.print(f"[red]'{args.target}' isn't an ID, mention, or Discord link.[/]")
        return 2
    saved = scan_exports()
    entry = Entry(tid, str(tid), "?", None, saved.get(tid))

    try:
        if args.new:
            rng = range_since_last(entry.saved)
            if not rng:
                console.print("[red]No previous export to continue from.[/]")
                return 2
        elif args.since or args.until:
            start = parse_when(args.since) if args.since else None
            end = parse_when(args.until) if args.until else None
            rng = Range(start, end, f"{args.since or 'start'} -> {args.until or 'now'}")
        else:
            rng = range_since_last(entry.saved) or Range(None, None, "all history")
        run_export(session, entry, rng)
    except (ValueError, LookupError, discord.HTTPException) as e:
        console.print(f"[red]{e}[/]")
        return 1
    return 0


def list_chats(session: Session) -> None:
    entries = build_entries(session.client, scan_exports())
    t = Table(header_style="bold", box=None, padding=(0, 2))
    for col in ("ID", "Name", "Type", "Last export"):
        t.add_column(col)
    for e in entries[:50]:
        last = datetime.fromtimestamp(e.saved.last_export, TZ).strftime("%b %d %Y") if e.saved else ""
        t.add_row(str(e.id), e.label, KIND_LABEL.get(e.kind, e.kind), last)
    console.print(t)


def parse_args():
    p = argparse.ArgumentParser(description="Read-only Discord message exporter. Run with no arguments for the interactive menu.")
    p.add_argument("target", nargs="?", help="user ID, channel ID, <@mention>, or discord.com/channels/... link")
    p.add_argument("--since", help=f"start of range ({WHEN_HELP})")
    p.add_argument("--until", help="end of range (default: now)")
    p.add_argument("--new", action="store_true", help="only what's new since the last export of this target")
    p.add_argument("--only", choices=("messages", "images"), help="save just transcripts, or just images (default: both)")
    p.add_argument("--media", choices=("images", "all", "none"), help="images = pictures only, all = any attachment, none = skip downloads")
    p.add_argument("--list", action="store_true", help="list recent chats with their IDs and exit")
    p.add_argument("--serve", action="store_true", help="just run the local web viewer (http://127.0.0.1:8765)")
    return p.parse_args()


def connect(cfg: dict, interactive_mode: bool) -> Session:
    while True:
        session = Session(cfg["token"])
        try:
            with console.status("Logging in to Discord..."):
                session.connect()
            return session
        except discord.LoginFailure:
            if not interactive_mode:
                sys.exit("Discord rejected the token in config.json.")
            cfg["token"] = ask_token("Discord rejected that token.")
            save_config(cfg)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # don't crash on emoji names when output is piped
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = parse_args()
    is_interactive = not (args.target or args.list or args.serve)
    cfg = load_config(interactive=is_interactive)
    global MEDIA_MODE, SAVE_MESSAGES, SAVE_IMAGES
    if not is_interactive:  # one-shot runs default to both, independent of the menu toggles
        SAVE_MESSAGES = SAVE_IMAGES = True
    if args.media:
        SAVE_IMAGES = args.media != "none"
        MEDIA_MODE = "all" if args.media == "all" else "images"
    if args.only:
        SAVE_MESSAGES, SAVE_IMAGES = args.only == "messages", args.only == "images"
    session = None
    try:
        session = connect(cfg, is_interactive)
        console.print(f"Logged in as [bold]{session.client.user}[/]  [dim]| times in {TZ.key} | saving to {OUTPUT_DIR}[/]")
        if args.list:
            list_chats(session)
        elif args.serve:
            open_web(session)
            console.print("[dim]Ctrl+C to stop.[/]")
            while True:
                time.sleep(1)
        elif args.target:
            return one_shot(session, args)
        else:
            interactive(session, cfg)
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/]")
    except (TimeoutError, OSError) as e:
        console.print(f"[red]{e}[/]")
        return 1
    finally:
        if session:
            session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
