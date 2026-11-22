# Discord message exporter

Read-only exporter for your own Discord DMs, group DMs and channels, with a terminal menu and a local web viewer.

> Using a personal account token with a third-party client is against Discord's Terms of Service. This tool only reads message history (no sending, reacting or presence changes), but use it at your own risk.

## Setup

Double-click `run.bat` (Windows). The first run creates a `venv` and installs the requirements, then asks for your token and saves it to `config.json`. That file is git-ignored.

Manual setup:

```
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python scraper.py
```

## Using it

1. **Pick a chat.** You get a list of your recent DMs/groups and chats you've exported before. Type a number, search by name, or paste an ID, `<@mention>` or Discord link.
2. **Pick a range.** The default is *new since last export*. You can also pick 24h / 7d / 30d / everything, or a custom range (`7d`, `yesterday`, `5/16/2026 5:32pm`...).
   - `m` / `i` toggle saving **messages** and **images**. Both are on by default.
3. **Open the result.** After the export, `v` opens it in your browser and `o` opens the folder.

Other keys in the chat list: `w` starts the web viewer, `s` opens settings (timezone, images-only vs all attachments), `q` quits.

## Output

Each export goes to `exports/<id>_<name>/`:

| File | What |
|---|---|
| `*.txt` | Transcript for pasting into an LLM. Mentions and emoji are shown as names, and images appear as `[image: media/...]`. Large chats are split into parts. |
| `*.html` | Offline Discord-style viewer with images, replies, markdown, search, and a light/dark toggle. |
| `*.json` | Full data. |
| `media/` | Images, GIFs, stickers and avatars. They're downloaded at export time because Discord CDN links expire, and reused on later runs. |

## Web viewer

Press `w` in the menu, or run `python scraper.py --serve`. It serves http://127.0.0.1:8765/, bound to localhost only.

- **`/`** shows your exported chats and recent DMs with live pfps, display names, usernames and IDs.
- **Export pages** render any export, including older JSON-only ones, filled in with live profile data.
- **Live view** shows the latest messages straight from the API, with paging and nothing saved.

## Command line

```
python scraper.py --list                       # recent chats + IDs
python scraper.py <id|link> --new              # only what's new since the last export
python scraper.py <id> --since 7d --until 1d   # explicit range
python scraper.py <id> --only images           # just download images
python scraper.py <id> --media all             # save every attachment, not just images
python scraper.py --serve                      # web viewer only
```

## Config (`config.json`)

```json
{
  "token": "...",
  "timezone": "America/New_York",
  "output_dir": "exports",
  "media": "images"
}
```
