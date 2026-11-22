"""
Local web viewer (http://127.0.0.1:8765/).

    /                       your exported chats + recent DMs, with live pfp / names / IDs from Discord
    /chat/<id>/<export>     an export (any JSON export, old ones too), filled in with live profile data
    /chat/<id>/media/<f>    files saved by exports
    /live/<id>?before=<m>   the latest 100 messages straight from the API (nothing is saved)

Read-only, bound to localhost, and the token never leaves the Python process.
It runs on the Discord client's event loop, so the terminal menu stays usable.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

import discord
from aiohttp import web

import html_view

core = None  # the running scraper module, set by start() (avoids importing a 2nd copy of __main__)

esc = html_view.esc
SAFE_SEGMENT = re.compile(r"^[\w.-]+$")
LIVE_PAGE = 100


class Viewer:
    def __init__(self, client: discord.Client):
        self.client = client
        self.profiles: dict[int, dict] = {}
        self.app = web.Application(middlewares=[self.errors])
        self.app.add_routes([
            web.get("/", self.index),
            web.get("/chat/{id}/media/{name}", self.media),
            web.get("/chat/{id}/{stamp}", self.export_view),
            web.get("/live/{id}", self.live),
        ])

    # ---- helpers ------------------------------------------------------------

    @web.middleware
    async def errors(self, request, handler):
        # Only answer requests that are really for this local server: a foreign Host header means DNS
        # rebinding (another site trying to read your chats), and cross-site fetches are never legitimate.
        host = request.host.rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1") or request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden(text="Forbidden")
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:  # readable pages instead of bare errors
            if type(e) is LookupError or isinstance(e, discord.NotFound):
                title, msg, status = "Not found", str(e) or "No such chat.", 404
            elif isinstance(e, discord.Forbidden):
                title, msg, status = "No access", "Your account cannot read that chat.", 403
            else:
                title, msg, status = "Something went wrong", str(e) or type(e).__name__, 500
            return page(title, f'<div class="empty"><h2>{esc(title)}</h2><p>{esc(msg)}</p>'
                               f'<p><a href="/">Back to all chats</a></p></div>', status=status)

    async def profile(self, uid: int, fetch: bool = True) -> dict:
        """Live user info (cached). Falls back to an API fetch for users not in the client cache."""
        if uid in self.profiles:
            return self.profiles[uid]
        user = self.client.get_user(uid)
        if user is None and fetch:
            try:
                user = await self.client.fetch_user(uid)
            except discord.HTTPException:
                user = None
        p = user_info(user) if user else {"id": uid}
        if user or fetch:
            self.profiles[uid] = p
        return p

    async def profiles_for(self, records: list[dict]) -> dict[int, dict]:
        ids = {int(r["author_id"]) for r in records}
        fetch = sum(1 for i in ids if i not in self.profiles and self.client.get_user(i) is None) <= 25
        results = await asyncio.gather(*(self.profile(i, fetch) for i in ids))
        return {p["id"]: p for p in results}

    async def target_info(self, tid: int, saved=None) -> dict:
        """Header info for a chat: title, subtitle, avatar, type."""
        ch = self.client.get_channel(tid)
        if isinstance(ch, discord.DMChannel):
            ch = None
        if isinstance(ch, discord.GroupChannel):
            return dict(id=tid, kind="Group", title=str(ch), sub=f"{len(ch.recipients) + 1} members",
                        avatar=ch.icon.url if ch.icon else None)
        if ch is not None:
            guild = getattr(ch, "guild", None)
            return dict(id=tid, kind="Channel", title=f"#{getattr(ch, 'name', tid)}", sub=guild.name if guild else "",
                        avatar=guild.icon.url if guild and guild.icon else None)
        if saved and saved.meta.get("kind") in ("group", "channel"):
            return dict(id=tid, kind=saved.meta["kind"].title(), title=saved.name, sub="", avatar=None)
        p = await self.profile(tid)
        if "username" in p:
            return dict(id=tid, kind="DM", title=p["display"], sub=f"@{p['username']}", avatar=p["avatar"])
        return dict(id=tid, kind="", title=saved.name if saved else str(tid), sub="", avatar=None)

    # ---- routes ---------------------------------------------------------------

    async def index(self, request):
        saved = core.scan_exports()
        exported = sorted(saved.values(), key=lambda s: s.last_export, reverse=True)
        infos = await asyncio.gather(*(self.target_info(s.id, s) for s in exported))

        cards = []
        for s, info in zip(exported, infos):
            exports = export_files(s)
            links = "".join(
                f'<a href="/chat/{s.id}/{esc(f.stem)}">{esc(export_label(f))}</a>' for f in exports[:8])
            more = f'<span class="muted">+{len(exports) - 8} older</span>' if len(exports) > 8 else ""
            latest = f'<a class="btn primary" href="/chat/{s.id}/{esc(exports[0].stem)}">Latest export</a>' if exports else ""
            cards.append(
                f'<div class="card" data-search="{esc((info["title"] + " " + info["sub"] + " " + str(s.id)).lower())}">'
                f'{identity_block(info)}<div class="actions">{latest}'
                f'<a class="btn" href="/live/{s.id}">Live view</a></div>'
                f'<div class="exports">{links}{more}</div></div>')

        recent = []
        for pc in sorted(self.client.private_channels, key=lambda c: c.last_message_id or 0, reverse=True):
            tid, _, _ = core.identity(pc)
            if tid in saved:
                continue
            info = channel_info(pc)
            recent.append(
                f'<a class="row-link" href="/live/{tid}" data-search="{esc((info["title"] + " " + info["sub"] + " " + str(tid)).lower())}">'
                f'{identity_block(info, small=True)}</a>')
            if len(recent) >= 60:
                break

        me = user_info(self.client.user)
        body = (
            f'<section><h2>Exported chats <span class="muted">{len(cards)}</span></h2>'
            f'<div class="grid">{"".join(cards) or "<p class=muted>No exports yet.</p>"}</div></section>'
            f'<section><h2>Recent DMs &amp; groups <span class="muted">live view only</span></h2>'
            f'<div class="list">{"".join(recent)}</div></section>')
        header = (f'<div class="me">{avatar_img(me["avatar"], me["display"], 28)}<span>{esc(me["display"])}'
                  f' <span class="muted">@{esc(me["username"])}</span></span></div>'
                  f'<input id="q" type="search" placeholder="Filter by name or ID..." autofocus>')
        return page("Discord exports", body, header=header)

    async def export_view(self, request):
        tid, stamp = parse_id(request), request.match_info["stamp"]
        saved = core.scan_exports().get(tid)
        if not saved or not SAFE_SEGMENT.match(stamp):
            raise web.HTTPNotFound(text="No such export.")
        path = saved.folder / f"{stamp}.json"
        if not path.is_file():
            raise web.HTTPNotFound(text="No such export.")
        records = json.loads(path.read_text(encoding="utf-8"))
        if not records:
            return page(saved.name, '<div class="empty">This export is empty.</div>')

        info, profiles = await asyncio.gather(self.target_info(tid, saved), self.profiles_for(records))
        others = "".join(
            f'<option value="/chat/{tid}/{esc(f.stem)}"{" selected" if f.stem == stamp else ""}>{esc(export_label(f))}</option>'
            for f in export_files(saved))
        top = profile_banner(info, extra=f'<a class="btn" href="/live/{tid}">Live view</a>'
                                          f'<select onchange="location=this.value">{others}</select>')
        subtitle = f"{records[0]['timestamp_est']} -> {records[-1]['timestamp_est']}"
        html = html_view.render(records, info["title"], subtitle, core.TZ, profiles=profiles,
                                nav='<a class="nav" href="/">&larr; All chats</a>', top=top)
        return web.Response(text=html, content_type="text/html")

    async def media(self, request):
        tid, name = parse_id(request), request.match_info["name"]
        saved = core.scan_exports().get(tid)
        if not saved or not SAFE_SEGMENT.match(name):
            raise web.HTTPNotFound()
        path = saved.folder / "media" / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    async def live(self, request):
        tid = parse_id(request)
        before = request.query.get("before", "")
        channel = await core.resolve_target(self.client, tid)
        msgs = [m async for m in channel.history(limit=LIVE_PAGE,
                                                 before=discord.Object(int(before)) if before.isdigit() else None)]
        msgs.reverse()
        records = []
        for m in msgs:
            r = core.message_to_record(m)
            r["avatar_url"] = m.author.display_avatar.with_size(64).url
            r["remote_media"] = live_media(m)
            records.append(r)
            self.profiles.setdefault(m.author.id, user_info(m.author))

        info = await self.target_info(tid, core.scan_exports().get(tid))
        older = (f'<a class="btn" href="/live/{tid}?before={msgs[0].id}">&uarr; Older</a>'
                 if len(msgs) == LIVE_PAGE else "")
        newest = f'<a class="btn" href="/live/{tid}">Newest</a>' if before else ""
        top = profile_banner(info, extra=f'{older}{newest}<span class="muted">Live from Discord, not saved</span>')
        if not records:
            return page(info["title"], top + '<div class="empty">No messages.</div>')
        subtitle = f"live | {records[0]['timestamp_est']} -> {records[-1]['timestamp_est']}"
        html = html_view.render(records, info["title"], subtitle, core.TZ,
                                nav='<a class="nav" href="/">&larr; All chats</a>', top=top)
        if not before:  # start at the newest message, like Discord
            html = html.replace("</body>", "<script>scrollTo(0, document.body.scrollHeight)</script></body>")
        return web.Response(text=html, content_type="text/html")


# ---- data helpers -------------------------------------------------------------

def parse_id(request) -> int:
    raw = request.match_info["id"]
    if not raw.isdigit():
        raise web.HTTPNotFound()
    return int(raw)


def user_info(user) -> dict:
    return {"id": user.id, "username": user.name, "display": user.display_name,
            "avatar": user.display_avatar.with_size(128).url}


def channel_info(pc) -> dict:
    if isinstance(pc, discord.DMChannel):
        u = user_info(pc.recipient)
        return dict(id=u["id"], kind="DM", title=u["display"], sub=f"@{u['username']}", avatar=u["avatar"])
    return dict(id=pc.id, kind="Group", title=str(pc), sub=f"{len(pc.recipients) + 1} members",
                avatar=pc.icon.url if getattr(pc, "icon", None) else None)


def live_media(m: discord.Message) -> list[str]:
    urls = [a.url for a in m.attachments
            if (a.content_type or "").startswith("image/") or core.url_ext(a.filename, "") in core.IMAGE_EXTS]
    for e in m.embeds:
        media = e.thumbnail if e.type in ("image", "gifv") else e.image
        url = getattr(media, "proxy_url", None) or getattr(media, "url", None)
        if url:
            urls.append(url)
    urls += [s.url for s in m.stickers if s.format != discord.StickerFormatType.lottie]
    return urls


def export_files(saved) -> list:
    files = [f for f in saved.folder.glob("*.json") if f.name != ".meta.json"]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def export_label(f) -> str:
    m = re.search(r"(\d{8}_\d{6})", f.stem)
    when = datetime.strptime(m[1], "%Y%m%d_%H%M%S").strftime("%b %d %Y, %I:%M %p") if m else f.stem
    size = f.stat().st_size
    return f"{when} ({size / 1024:,.0f} KB)" if size < 1024 ** 2 else f"{when} ({size / 1024 ** 2:,.1f} MB)"


# ---- HTML bits ------------------------------------------------------------------

def avatar_img(src: str | None, name: str, size: int) -> str:
    if src:
        return f'<img class="pfp" src="{esc(src)}" width="{size}" height="{size}" alt="">'
    return (f'<span class="pfp" style="width:{size}px;height:{size}px;background:{html_view.author_color(len(name) * 7919)}">'
            f'{esc(name[:1].upper())}</span>')


def identity_block(info: dict, small: bool = False) -> str:
    size = 40 if small else 56
    kind = f'<span class="badge">{esc(info["kind"])}</span>' if info.get("kind") else ""
    return (f'<div class="ident">{avatar_img(info.get("avatar"), info["title"], size)}<div class="who">'
            f'<div class="title">{esc(info["title"])} {kind}</div>'
            f'<div class="muted">{esc(info.get("sub", ""))}</div>'
            f'<div class="id" title="Click to copy" data-copy="{info["id"]}">ID {info["id"]}</div></div></div>')


def profile_banner(info: dict, extra: str = "") -> str:
    return (f'<div class="banner">{identity_block(info)}<div class="actions">{extra}</div></div>'
            f'<style>{BANNER_CSS}</style><script>{COPY_JS}</script>')


COPY_JS = """document.addEventListener("click", (e) => { const el = e.target.closest("[data-copy]"); if (!el) return;
  e.preventDefault(); navigator.clipboard?.writeText(el.dataset.copy); const t = el.textContent; el.textContent = "Copied!";
  setTimeout(() => el.textContent = t, 900); });"""

BANNER_CSS = """
.banner { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; margin:12px 16px 4px;
  padding:16px; background:var(--bg2); border:1px solid var(--line); border-radius:8px; }
.ident { display:flex; gap:14px; align-items:center; min-width:0; }
.pfp { border-radius:50%; object-fit:cover; flex:none; display:inline-flex; align-items:center; justify-content:center;
  color:#fff; font-weight:700; }
.who { min-width:0; } .title { font-weight:700; font-size:17px; overflow-wrap:anywhere; }
.muted { color:var(--muted); font-size:13px; }
.id { color:var(--muted); font:12px Consolas,monospace; cursor:pointer; } .id:hover { color:var(--fg); }
.badge { font-size:11px; font-weight:600; padding:1px 6px; border-radius:4px; background:var(--line); color:var(--muted); vertical-align:2px; }
.actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.btn, select { background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:4px; padding:6px 12px;
  font-size:14px; text-decoration:none; cursor:pointer; }
.btn:hover { text-decoration:none; border-color:var(--muted); } .btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
"""

INDEX_CSS = """
:root { --bg:#313338; --bg2:#2b2d31; --fg:#dbdee1; --muted:#949ba4; --line:#3f4147; --link:#00a8fc; --accent:#5865f2; }
:root[data-theme="light"] { --bg:#fff; --bg2:#f2f3f5; --fg:#313338; --muted:#5c5e66; --line:#e3e5e8; --link:#006ce7; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.4 "gg sans","Segoe UI",system-ui,sans-serif; }
a { color:var(--link); text-decoration:none; }
header { position:sticky; top:0; z-index:2; display:flex; gap:16px; align-items:center; justify-content:space-between;
  flex-wrap:wrap; padding:10px 16px; background:var(--bg2); border-bottom:1px solid var(--line); }
header .me { display:flex; gap:10px; align-items:center; font-weight:600; }
header input { background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:4px; padding:7px 10px;
  width:280px; max-width:100%; }
main { max-width:1200px; margin:0 auto; padding:8px 16px 48px; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:24px 0 10px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(340px, 1fr)); gap:12px; }
.card { background:var(--bg2); border:1px solid var(--line); border-radius:8px; padding:16px; display:flex;
  flex-direction:column; gap:12px; min-width:0; }
.exports { display:flex; flex-direction:column; gap:2px; font-size:13px; }
.list { display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:4px; }
.row-link { color:inherit; padding:8px 10px; border-radius:6px; } .row-link:hover { background:var(--bg2); }
.row-link .title { font-size:15px; }
.empty { text-align:center; color:var(--muted); padding:60px 16px; }
.hidden { display:none !important; }
@media (max-width:420px) { .grid { grid-template-columns:1fr; } }
"""

FILTER_JS = """try { if (localStorage.getItem("theme") === "light") document.documentElement.dataset.theme = "light"; } catch (e) {}
const q = document.getElementById("q"); if (q) q.oninput = () => { const v = q.value.trim().toLowerCase();
  document.querySelectorAll("[data-search]").forEach(el => el.classList.toggle("hidden", !!v && !el.dataset.search.includes(v))); };"""


def page(title: str, body: str, header: str = "", status: int = 200) -> web.Response:
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title>'
            f'<style>{INDEX_CSS}{BANNER_CSS}</style></head><body>'
            f'<header>{header or "<a href=/>&larr; All chats</a>"}</header><main>{body}</main>'
            f'<script>{COPY_JS}{FILTER_JS}</script></body></html>')
    return web.Response(text=html, content_type="text/html", status=status)


# ---- start ---------------------------------------------------------------------------

async def start(client: discord.Client, core_module, host: str = "127.0.0.1", port: int = 8765) -> str:
    """Start serving on the client's loop. Tries a few ports if the default is busy. Returns the URL."""
    global core
    core = core_module
    runner = web.AppRunner(Viewer(client).app, access_log=None)
    await runner.setup()
    for p in range(port, port + 20):
        try:
            await web.TCPSite(runner, host, p).start()
            return f"http://{host}:{p}/"
        except OSError:
            continue
    await runner.cleanup()
    raise OSError(f"No free port between {port} and {port + 19}.")
