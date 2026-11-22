"""Renders exported message records as a single offline HTML page (media linked from ./media)."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif")
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".m4v")
AUDIO_EXTS = (".mp3", ".ogg", ".wav", ".m4a", ".flac", ".opus")
GROUP_GAP = timedelta(minutes=7)
DISCORD_EPOCH_MS = 1420070400000


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def ext_of(path: str) -> str:
    path = path.split("?", 1)[0].lower()
    return path[path.rfind("."):] if "." in path.rsplit("/", 1)[-1] else ""


# ---- Discord-flavoured markdown (the common subset) ----------------------- #

def markdown(text: str) -> str:
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"```(?:[\w+-]*\n)?(.*?)```", lambda m: keep(f"<pre>{esc(m[1].strip(chr(10)))}</pre>"), text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{esc(m[1])}</code>"), text)
    t = esc(text)
    t = re.sub(r"&lt;(a?):(\w+):(\d+)&gt;", lambda m: keep(
        f'<img class="emoji" alt=":{m[2]}:" title=":{m[2]}:" loading="lazy" '
        f'src="https://cdn.discordapp.com/emojis/{m[3]}.{"gif" if m[1] else "webp"}?size=48">'), t)
    t = re.sub(r"https?://[^\s<]+[^\s<.,:;\"')\]]", lambda m: keep(
        f'<a href="{m[0]}" target="_blank" rel="noopener">{m[0]}</a>'), t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"__(.+?)__", r"<u>\1</u>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", t)
    t = re.sub(r"~~(.+?)~~", r"<s>\1</s>", t)
    t = re.sub(r"\|\|(.+?)\|\|", r'<span class="spoiler">\1</span>', t)
    t = re.sub(r"(?m)^&gt; (.*)$", r'<span class="quote">\1</span>', t)
    t = t.replace("\n", "<br>")
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m[1])], t)


# ---- page ------------------------------------------------------------------ #

def author_color(author_id) -> str:
    return f"hsl({int(author_id) * 47 % 360} 55% 60%)"


def msg_time(r: dict, tz: ZoneInfo) -> datetime:
    """Message time; old exports lack 'timestamp', so fall back to the time encoded in the Discord ID."""
    if r.get("timestamp"):
        return datetime.fromisoformat(r["timestamp"]).astimezone(tz)
    return datetime.fromtimestamp(((int(r["id"]) >> 22) + DISCORD_EPOCH_MS) / 1000, tz)


def display_name(rec: dict, profiles: dict) -> str:
    return rec.get("author_display") or profiles.get(rec["author_id"], {}).get("display") or rec["author"]


def avatar(rec: dict, profiles: dict) -> str:
    src = rec.get("author_avatar") or rec.get("avatar_url") or profiles.get(rec["author_id"], {}).get("avatar")
    if src:
        return f'<img class="avatar" src="{esc(src)}" alt="" loading="lazy">'
    initial = esc(display_name(rec, profiles)[:1].upper())
    return f'<div class="avatar" style="background:{author_color(rec["author_id"])}">{initial}</div>'


def media_html(rec: dict) -> str:
    out = []
    files = rec.get("files", [])
    for f in files:
        e, src = ext_of(f), esc(f)
        if e in IMAGE_EXTS:
            cls = "sticker" if "/sticker_" in f else "img"
            out.append(f'<a href="{src}" target="_blank"><img class="{cls}" src="{src}" loading="lazy" alt=""></a>')
        elif e in VIDEO_EXTS:
            out.append(f'<video class="img" src="{src}" controls preload="metadata"></video>')
        elif e in AUDIO_EXTS:
            out.append(f'<audio src="{src}" controls preload="none"></audio>')
        else:
            out.append(f'<a class="file" href="{src}" target="_blank">{esc(f.split("/")[-1].split("_", 1)[-1])}</a>')
    # live view: fresh CDN links straight from the API
    remote = rec.get("remote_media", [])
    for url in remote:
        out.append(f'<a href="{esc(url)}" target="_blank"><img class="img" src="{esc(url)}" loading="lazy" alt=""></a>')
    # attachments that weren't saved locally (link may have expired)
    saved_ids = {f.split("/")[-1].split("_", 1)[0] for f in files}
    for url in rec.get("attachments", []):
        m = re.search(r"/attachments/\d+/(\d+)/([^?]+)", url)
        if (m and m[1] in saved_ids) or url in remote:
            continue
        name = m[2] if m else url
        out.append(f'<a class="file" href="{esc(url)}" target="_blank">{esc(name)}</a>')
    if not any("/sticker_" in f for f in files) and not remote:
        out += [f'<span class="file">sticker: {esc(s)}</span>' for s in rec.get("stickers", [])]
    return f'<div class="media">{"".join(out)}</div>' if out else ""


def render(records: list[dict], title: str, subtitle: str, tz: ZoneInfo,
           profiles: dict | None = None, nav: str = "", top: str = "") -> str:
    """profiles: {author_id: {"display", "avatar"}} fills gaps in old exports. nav/top: extra HTML (web viewer)."""
    profiles = profiles or {}
    by_id = {r["id"]: r for r in records}
    rows = [top] if top else []
    prev, prev_time, prev_day = None, None, None

    for r in records:
        when = msg_time(r, tz)
        day = when.date()
        if day != prev_day:
            rows.append(f'<div class="day"><span>{when.strftime("%A, %B %d, %Y")}</span></div>')
            prev = None
        prev_day = day
        full_time = when.strftime("%m/%d/%Y %I:%M:%S %p")
        name = display_name(r, profiles)

        if r.get("type"):
            rows.append(f'<div class="msg system" id="m{r["id"]}"><span class="time" title="{full_time}">'
                        f'{when.strftime("%I:%M %p")}</span> {esc(r.get("content") or f"{name}: {r["type"]}")}</div>')
            prev = None
            continue

        reply = ""
        if r.get("reply_to"):
            target = by_id.get(r["reply_to"])
            if target:
                snippet = (target.get("clean_content") or target.get("content") or "[attachment]")[:120]
                reply = (f'<a class="reply" href="#m{target["id"]}"><b>{esc(display_name(target, profiles))}</b> '
                         f'{esc(snippet)}</a>')
            elif r.get("reply_to_author"):
                reply = f'<div class="reply"><b>{esc(r["reply_to_author"])}</b> <i>(not shown here)</i></div>'

        continued = (prev is not None and not reply and prev["author_id"] == r["author_id"]
                     and when - prev_time < GROUP_GAP)
        body = markdown(r.get("clean_content") or r.get("content") or "")
        edited = '<span class="edited">(edited)</span>' if r.get("edited") else ""
        content = f'<div class="text">{body}{edited}</div>' if body or edited else ""

        if continued:
            rows.append(f'<div class="msg cont" id="m{r["id"]}"><span class="time hover" title="{full_time}">'
                        f'{when.strftime("%I:%M")}</span><div class="body">{content}{media_html(r)}</div></div>')
        else:
            rows.append(
                f'<div class="msg" id="m{r["id"]}">{reply}<div class="row">{avatar(r, profiles)}<div class="body">'
                f'<div class="head"><span class="name" style="color:{author_color(r["author_id"])}" '
                f'title="{esc(r["author"])} ({r["author_id"]})">{esc(name)}</span>'
                f'<span class="time" title="{full_time}">{when.strftime("%m/%d/%Y %I:%M %p")}</span></div>'
                f'{content}{media_html(r)}</div></div></div>')
        prev, prev_time = r, when

    return PAGE.format(title=esc(title), subtitle=esc(subtitle), count=f"{len(records):,}",
                       rows="\n".join(rows), nav=nav)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#313338; --bg2:#2b2d31; --fg:#dbdee1; --muted:#949ba4; --line:#3f4147; --hover:#2e3035;
  --link:#00a8fc; --code:#1e1f22; --accent:#5865f2; }}
:root[data-theme="light"] {{ --bg:#ffffff; --bg2:#f2f3f5; --fg:#313338; --muted:#5c5e66; --line:#e3e5e8;
  --hover:#f5f6f7; --link:#006ce7; --code:#ebedef; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.375 "gg sans","Segoe UI",system-ui,sans-serif; }}
header {{ position:sticky; top:0; z-index:2; background:var(--bg2); border-bottom:1px solid var(--line);
  padding:10px 16px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
header h1 {{ font-size:16px; margin:0; }} header .nav {{ color:var(--link); font-size:14px; }} header .sub {{ color:var(--muted); font-size:13px; flex:1; min-width:200px; }}
header input {{ background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:4px; padding:6px 10px; width:220px; max-width:100%; }}
header button {{ background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:4px; padding:6px 10px; cursor:pointer; }}
main {{ padding:8px 0 40px; }}
.day {{ display:flex; align-items:center; margin:18px 16px 6px; color:var(--muted); font-size:12px; font-weight:600; }}
.day::before, .day::after {{ content:""; flex:1; border-top:1px solid var(--line); }} .day span {{ padding:0 8px; }}
.msg {{ padding:2px 16px 2px 72px; position:relative; }} .msg:hover {{ background:var(--hover); }}
.msg:not(.cont):not(.system) {{ margin-top:14px; }}
.msg:target {{ background:color-mix(in srgb, var(--accent) 18%, transparent); }}
.row {{ display:flex; gap:16px; margin-left:-56px; }}
.avatar {{ width:40px; height:40px; border-radius:50%; flex:none; display:flex; align-items:center; justify-content:center;
  color:#fff; font-weight:600; object-fit:cover; }}
.body {{ min-width:0; flex:1; }}
.head {{ display:flex; gap:8px; align-items:baseline; }} .name {{ font-weight:600; }}
.time {{ color:var(--muted); font-size:12px; }}
.cont .time.hover {{ position:absolute; left:16px; width:48px; text-align:right; top:4px; font-size:11px; visibility:hidden; }}
.cont:hover .time.hover {{ visibility:visible; }}
.text {{ white-space:normal; overflow-wrap:anywhere; }}
.edited {{ color:var(--muted); font-size:11px; margin-left:4px; }}
.system {{ color:var(--muted); font-size:14px; padding-top:6px; padding-bottom:6px; }}
.reply {{ display:block; font-size:13px; color:var(--muted); text-decoration:none; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; position:relative; overflow:visible; }}
.reply::before {{ content:""; position:absolute; left:-36px; top:9px; width:30px; height:8px; border:2px solid var(--line);
  border-right:0; border-bottom:0; border-top-left-radius:6px; }}
.reply b {{ color:var(--fg); opacity:.85; }}
a {{ color:var(--link); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
code, pre {{ background:var(--code); border-radius:4px; font:13px Consolas,monospace; }}
code {{ padding:1px 4px; }} pre {{ padding:8px; white-space:pre-wrap; margin:4px 0; }}
.quote {{ border-left:4px solid var(--line); padding-left:8px; display:inline-block; }}
.spoiler {{ background:var(--muted); color:transparent; border-radius:3px; cursor:pointer; }}
.spoiler.shown {{ background:var(--code); color:inherit; }}
.emoji {{ width:22px; height:22px; vertical-align:-5px; object-fit:contain; }}
.media {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:4px; }}
.media .img {{ max-width:min(400px,100%); max-height:320px; border-radius:6px; display:block; }}
.media .sticker {{ width:160px; height:160px; object-fit:contain; }}
.file {{ background:var(--bg2); border:1px solid var(--line); border-radius:6px; padding:8px 12px; font-size:14px; }}
.hidden {{ display:none !important; }}
@media (max-width:600px) {{ .msg {{ padding-left:60px; }} .row {{ margin-left:-48px; gap:10px; }} .avatar {{ width:36px; height:36px; }} }}
</style></head><body>
<header>{nav}<h1>{title}</h1><span class="sub">{subtitle} &middot; {count} messages</span>
<input id="q" type="search" placeholder="Search messages..."><span id="hits" class="sub" style="flex:0"></span>
<button id="theme" type="button">Theme</button></header>
<main>
{rows}
</main>
<script>
(() => {{
  const root = document.documentElement;
  try {{ if (localStorage.getItem("theme") === "light") root.dataset.theme = "light"; }} catch (e) {{}}
  document.getElementById("theme").onclick = () => {{
    root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
    try {{ localStorage.setItem("theme", root.dataset.theme); }} catch (e) {{}}
  }};
  document.addEventListener("click", (ev) => {{
    const sp = ev.target.closest(".spoiler");
    if (sp) sp.classList.add("shown");
  }});
  const msgs = [...document.querySelectorAll(".msg")], hits = document.getElementById("hits");
  let timer;
  document.getElementById("q").oninput = (ev) => {{
    clearTimeout(timer);
    timer = setTimeout(() => {{
      const q = ev.target.value.trim().toLowerCase();
      let n = 0;
      for (const m of msgs) {{
        const show = !q || m.textContent.toLowerCase().includes(q);
        m.classList.toggle("hidden", !show); n += show;
      }}
      document.querySelectorAll(".day").forEach(d => d.classList.toggle("hidden", !!q));
      hits.textContent = q ? n + " found" : "";
    }}, 150);
  }};
}})();
</script>
</body></html>
"""
