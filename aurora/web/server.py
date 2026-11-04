"""R353: the HTTP side of `aurora --web` — stdlib only.

GET  /                 the Chat page, the default (R377, Design Language flavour 3; R383; no auth: it holds nothing)
GET  /paper            the Paper page (R369)
GET  /v1               the original Desktop page
GET  /api/notes | /api/notes/<id>   plain-text notes (R389); POST /api/notes/create {title, text} | /save {id, title, text} | /delete {id} | /order {ids} (R404) | /type {id, type} (R422; GET ?type=)
GET  /api/models      configured models (R387)
POST /api/model       {i}: switch the active model, remembered for the page only (R387)
GET  /api/models/config   R423: config.yaml's models: list; POST /api/models/config/add|save|delete {i, expect, provider, model, tools, vision, extra}
POST /api/prices/refresh  re-read every configured OpenRouter model's price from the catalog now (R397)
GET  /api/state        busy, model, context, session id, pending approval
GET  /api/sessions?limit=N   recent sessions (short id, when, first message; N ≤ 100)
GET  /api/search?q=   sessions containing the text (R377)
GET  /api/sessions/<id>  one past session as read-only markdown
GET  /api/sessions/<id>/export?level=brief|full   R421: the chat as one self-contained HTML file
GET  /api/stream?after=N   server-sent events (fetch + Authorization header)
GET  /api/diff | /api/undo   last turn's diff | what Undo would revert (R376)
POST /api/send {text, images?, note_ids?, files?, temperature?} | /api/approve {id, answer} | /api/stop | /api/new | /api/auto {on} | /api/command {name} | /api/resume {id} | /api/undo
POST /api/queue/cancel {position, text} | /api/share {id} | /api/share/delete {token} (R418); GET /shared/<token> (no token)
GET  /api/usage        per-model tokens and cost + budgets (R392); POST /api/budget {which, usd} | /api/plan {on} | /api/retry {text?}
GET  /api/files?kind=input|outputs | /api/files/file?kind=&name=&thumb=1|view=1 (R408/R410/R413; R432 was /api/uploads)
POST /api/transcribe   raw audio -> {text} via the SpeechToText whisper server (R392; AURORA_WEB_STT_URL)
GET  /health           no token: version, optional packages, stores readable, model reachable (R392, for Uptime Kuma)
POST /api/chat/delete {id} | /api/chat/rename {id, name} | /api/chat/move {chat, folder|""} | /api/folder/create {name} | /api/folder/rename {id, name}
     | /api/folder/open {id, open} | /api/folder/delete {id}   (R383: web-only; hides chats, never touches the logs)

Every /api/* needs `Authorization: Bearer $AURORA_WEB_TOKEN`; with no token
configured the server refuses to start (fails closed).
"""

import hashlib
import hmac
import json
import os
import shutil
import sys
import subprocess
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import service
from . import attach, export, folders, notes, shares
from .core import MAX_STREAMS, WebSession

WEB = Path(__file__).parent
PAGE = WEB / "index.html"
PAGE_PAPER = WEB / "index_paper.html"
PAGE_V2 = WEB / "index_v2.html"
SCRIPT = WEB / "app.js"
SW = WEB / "sw.js"
MANIFEST = WEB / "manifest.webmanifest"
FAVICON = WEB / "favicon.png"
APP_ICONS = {f"/{n}": n for n in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png")}
MAX_BODY = 1_000_000          # R392 (bug 3): a 100,000-character note fits even as 4-byte UTF-8 / escaped JSON
MAX_AUDIO = 25_000_000
MAX_IMAGES = 4
MAX_SEND_NOTES = 5            # R401
MAX_SEND_FILES = 10           # R405
MAX_SEND_BODY = 24_000_000   # R381: /api/send may carry up to 4 pictures as data URLs
HEARTBEAT_S = 15.0
STT_LANGS = {"en", "pt", "es", "fr", "de", "it"}   # R400: what the page offers; anything else is English
HTML_HEADERS = (("Content-Security-Policy", "frame-ancestors 'none'"), ("X-Frame-Options", "DENY"),
                ("Referrer-Policy", "no-referrer"))


class TooLarge(Exception):
    pass


def _script_version() -> str:
    return hashlib.sha256(SCRIPT.read_bytes()).hexdigest()[:12]


def _temperature(body: dict):
    """R418 (F5): the page's temperature, 0 to 2, or None (the model's own setting)."""
    t = body.get("temperature")
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t <= 2:
        return None
    return round(float(t), 2)


def _shared_page(md: str) -> bytes:
    """R418 (F1): a share link's page: the transcript as escaped text, house fonts, light and dark; no script."""
    import html
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><meta name=robots content=noindex>"
            "<title>Shared chat · Aurora</title><style>"
            ":root{color-scheme:light dark;--bg:#fff;--fg:#1f2328;--line:#d0d7de}"
            "@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--line:#30363d}}"
            "body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,'Noto Sans','DejaVu Sans',sans-serif}"
            "main{max-width:760px;margin:0 auto;padding:32px 16px 64px}"
            "header{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:16px;font-size:14px;opacity:.8}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font:inherit}"
            "</style></head><body><main><header>Shared chat (read only)</header><pre>"
            + html.escape(md) + "</pre></main></body></html>").encode()


def _page(path: Path) -> bytes:
    """The HTML with app.js pinned to its content hash, so the script can be cached for good (P11)."""
    return path.read_bytes().replace(b'src="/app.js"', b'src="/app.js?v=' + _script_version().encode() + b'"')


class Server(ThreadingHTTPServer):
    request_queue_size = 128          # R392 (P8): socketserver's default of 5 turned bursts into +1 s SYN retries
    daemon_threads = True


def make_handler(session: WebSession, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AuroraWeb"

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json", cache: str = "no-store",
                  shared: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            if self.close_connection:
                self.send_header("Connection", "close")
            if shared:        # R418 (F1): nothing runs, nothing loads, never indexed
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
                self.send_header("X-Robots-Tag", "noindex, nofollow")
                self.send_header("Referrer-Policy", "no-referrer")
            elif ctype.startswith("text/html"):
                for k, v in HTML_HEADERS:
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode())

        def _authed(self) -> bool:
            sent = self.headers.get("Authorization", "")
            return hmac.compare_digest(sent.encode(), f"Bearer {token}".encode())

        def _length(self) -> int:
            try:
                return max(0, int(self.headers.get("Content-Length") or 0))
            except ValueError:
                self.close_connection = True
                return 0

        def _body(self, limit: int = MAX_BODY) -> dict:
            """Raises TooLarge for a body over `limit`. R392 (bug 2): a body left unread closes the connection,
            so its bytes are never parsed as the next request on a keep-alive connection."""
            n = self._length()
            if n > limit:
                self.close_connection = True
                if n <= 4 * limit:            # read and drop it, so the client sees the 413 instead of a broken pipe
                    left = n
                    while left > 0:
                        got = self.rfile.read(min(left, 1 << 20))
                        if not got:
                            break
                        left -= len(got)
                raise TooLarge
            if n <= 0:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/v1", "/v1/", "/index.html"):
                self._send(200, _page(PAGE), "text/html; charset=utf-8")
            elif url.path in ("/paper", "/paper/"):
                self._send(200, _page(PAGE_PAPER), "text/html; charset=utf-8")
            elif url.path in ("/",):
                self._send(200, _page(PAGE_V2), "text/html; charset=utf-8")
            elif url.path == "/app.js":
                versioned = parse_qs(url.query).get("v", [""])[0] == _script_version()
                self._send(200, SCRIPT.read_bytes(), "text/javascript; charset=utf-8",
                           "public, max-age=31536000, immutable" if versioned else "no-store")
            elif url.path == "/sw.js":          # R396: offline shell; no-store so a new worker is picked up at once
                self._send(200, SW.read_bytes(), "text/javascript; charset=utf-8")
            elif url.path == "/manifest.webmanifest":
                self._send(200, MANIFEST.read_bytes(), "application/manifest+json", "no-cache")
            elif url.path in ("/favicon.png", "/favicon.ico"):   # R398: the sidebar's logo, as the tab icon
                self._send(200, FAVICON.read_bytes(), "image/png", "public, max-age=86400")
            elif url.path in APP_ICONS:          # R399: the same logo as the home-screen / installed-app icon
                self._send(200, (WEB / APP_ICONS[url.path]).read_bytes(), "image/png", "public, max-age=86400")
            elif url.path.startswith("/shared/"):   # R418 (F1): read-only, no bearer token: the link is the key
                sid = shares.lookup(url.path[len("/shared/"):])
                md = session.session_markdown(sid) if sid else None
                if md is None:
                    self._send(404, b"<!doctype html><title>Not shared</title><p>This link does not exist or was revoked.</p>",
                               "text/html; charset=utf-8")
                else:
                    self._send(200, _shared_page(md), "text/html; charset=utf-8", "no-store", shared=True)
            elif url.path == "/health":
                h = health(session)
                self._json(200 if h["ok"] else 503, h)
            elif not url.path.startswith("/api/"):
                self._json(404, {"error": "not found"})
            elif not self._authed():
                self._json(401, {"error": "missing or bad bearer token"})
            elif url.path == "/api/state":
                self._json(200, session.state())
            elif url.path == "/api/config/files":               # R427: the raw files, secrets hidden
                self._json(200, session.config_files())
            elif url.path == "/api/models/config":              # R423: the models: list as config.yaml has it
                self._json(200, session.model_config())
            elif url.path == "/api/models":
                self._json(200, {"models": session.models()})
            elif url.path == "/api/sessions":
                try:
                    limit = max(1, min(100, int(parse_qs(url.query).get("limit", ["15"])[0])))
                except ValueError:
                    limit = 15
                self._json(200, {"sessions": session.sessions(limit), "folders": session.folders()})
            elif url.path == "/api/search":
                self._json(200, {"sessions": session.search(parse_qs(url.query).get("q", [""])[0])})
            elif url.path.startswith("/api/sessions/") and url.path.endswith("/export"):   # R421
                sid = url.path[len("/api/sessions/"):-len("/export")]
                level = parse_qs(url.query).get("level", ["brief"])[0]
                if level not in export.LEVELS:
                    self._json(400, {"error": "level: brief or full"})
                elif not service.session_exists(sid):
                    self._json(404, {"error": "no such session"})
                else:
                    self._send(200, export.render(sid, level, parse_qs(url.query).get("title", [""])[0][:200]).encode(),
                               "text/html; charset=utf-8")
            elif url.path.startswith("/api/sessions/"):
                sid = url.path[len("/api/sessions/"):]
                md = session.session_markdown(sid)
                if md is None:
                    self._json(404, {"error": "no such session"})
                else:
                    self._json(200, {"id": sid, "markdown": md})
            elif url.path == "/api/files":                      # R408; R413: ?kind=outputs lists the agent's outputs; R432: was /api/uploads
                kind = parse_qs(url.query).get("kind", ["input"])[0]
                if kind not in attach.KINDS:
                    self._json(400, {"error": "kind: input or outputs"})
                else:
                    self._json(200, {"files": attach.files(kind), "kind": kind})
            elif url.path == "/api/files/file":                 # R410: the file itself, or ?thumb=1 for a small preview
                q = parse_qs(url.query)
                f = attach.file_path(q.get("name", [""])[0], q.get("kind", ["input"])[0])
                if f is None:
                    self._json(404, {"error": "no such file"})
                elif q.get("thumb", [""])[0] == "1":
                    t = attach.thumbnail(f)
                    self._send(200, t, "image/png") if t else self._json(404, {"error": "no preview"})
                elif q.get("text", [""])[0] == "1":            # R414: a text preview (markdown, text, code, office)
                    try:
                        self._json(200, attach.preview_text(f))
                    except attach.Unsupported as e:
                        self._json(415, {"error": str(e)})
                elif q.get("view", [""])[0] == "1" and attach._ext(f.name) in attach.VIEW_TYPES:   # R413/R414: shown in the page
                    self._send(200, f.read_bytes(), attach.VIEW_TYPES[attach._ext(f.name)])
                else:
                    self._send(200, f.read_bytes(), "application/octet-stream")
            elif url.path == "/api/notes":
                qs = parse_qs(url.query)                         # R422: ?type= keeps one type of note
                self._json(200, {"notes": notes.listing(qs.get("q", [""])[0], qs.get("type", [""])[0])})
            elif url.path.startswith("/api/notes/"):
                n = notes.get(url.path[len("/api/notes/"):])
                self._json(200, n) if n else self._json(404, {"error": "no such note"})
            elif url.path == "/api/usage":
                self._json(200, session.usage())
            elif url.path == "/api/diff":
                self._json(200, session.diff())
            elif url.path == "/api/undo":
                self._json(200, session.undo_preview())
            elif url.path == "/api/stream":
                q = parse_qs(url.query)
                try:
                    after = int(q.get("after", ["0"])[0])
                except ValueError:
                    after = 0
                self._stream(after, q.get("boot", [None])[0])
            else:
                self._json(404, {"error": "not found"})

        def _stream(self, after: int, boot: str | None = None) -> None:
            with session.cond:
                full = session.streams >= MAX_STREAMS
                if not full:
                    session.streams += 1
            if full:                      # R392 (P12): one OS thread per stream, so they are capped
                self.close_connection = True
                return self._json(429, {"error": f"too many open pages ({MAX_STREAMS})"})
            try:
                self._stream_on(after, boot)
            finally:
                with session.cond:
                    session.streams -= 1

        def _stream_on(self, after: int, boot: str | None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    evs = session.wait_events(after, HEARTBEAT_S, boot)
                    boot = session.boot
                    if not evs:
                        self.wfile.write(b": keep-alive\n\n")
                    for ev in evs:
                        self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
                        after = ev["id"]
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _attach(self):
            """R381: raw file bytes in, text or a picture data URL out (see attach.py)."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            name = unquote(self.headers.get("X-Filename") or "file")
            limit = attach.max_attach(name)       # R420: audio/video may be larger
            if n <= 0 or n > limit:
                self.close_connection = True      # R392 (bug 2): the body is not read
                return self._json(413, {"error": f"{name}: send 1 byte to {limit // 1_000_000} MB"})
            data = self.rfile.read(n)
            try:
                res = attach.process(name, data)
            except attach.Unsupported as e:
                return self._json(415, {"error": f"{name}: {e}"})
            if res["kind"] == "image" and not session.vision(probe=True):
                res["vision"] = False   # R412: still attached and saved; the model gets the file path instead of the picture
            if True:                        # R406 pictures, R409 documents too: the agent gets a real file
                try:
                    res["path"] = attach.save_original(res["name"], data)
                except OSError:
                    if res["kind"] == "media":
                        return self._json(500, {"error": f"{name}: could not be saved"})
            if res["kind"] == "media":            # R420: stored, not read; the model gets the path
                res["text"] = attach.media_text(res["media"], res["name"], len(data), res["path"])
            return self._json(200, res)

        def do_POST(self):
            path = urlparse(self.path).path
            if not path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            if not self._authed():
                return self._json(401, {"error": "missing or bad bearer token"})
            if path == "/api/attach":
                return self._attach()
            if path == "/api/transcribe":
                return self._transcribe()
            if path == "/api/files/delete":                      # R408
                try:
                    body = self._body(MAX_BODY)
                except TooLarge:
                    return self._json(413, {"error": "too large"})
                if attach.delete_file(body.get("name"), body.get("kind") or "input"):
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "no such file"})
            if path == "/api/files/delete_all":                  # R410
                body = (self._body(MAX_BODY) if self._length() else None) or {}
                kind = body.get("kind") or "input"
                if kind not in attach.KINDS:
                    return self._json(400, {"error": "kind: input or outputs"})
                return self._json(200, {"ok": True, "deleted": attach.delete_all_files(kind)})
            try:
                body = self._body(MAX_SEND_BODY if path == "/api/send" else MAX_BODY)
            except TooLarge:              # R392 (bug 3): said as it is, never "empty message" / "no such note"
                lim = MAX_SEND_BODY if path == "/api/send" else MAX_BODY
                return self._json(413, {"error": f"too large: the request is over {lim // 1_000_000 or 1} MB"
                                        + (" (send fewer or smaller pictures)" if path == "/api/send" else "")})
            if path == "/api/send":
                imgs = body.get("images")
                imgs = imgs if isinstance(imgs, list) and all(isinstance(u, str) and u.startswith("data:image/") for u in imgs) else []
                cid = body.get("client_id")
                cid = cid if isinstance(cid, str) and 0 < len(cid) <= 64 else None
                nids = body.get("note_ids") or []                # R401: notes sent as context with this message
                if not isinstance(nids, list) or len(nids) > MAX_SEND_NOTES or not all(isinstance(n, str) for n in nids):
                    return self._json(400, {"error": f"note_ids: a list of at most {MAX_SEND_NOTES} note ids"})
                picked = []
                for nid in dict.fromkeys(nids):
                    n = notes.get(nid) if notes.valid_id(nid) else None
                    if n is None:
                        return self._json(400, {"error": "a note attached to this message no longer exists"})
                    picked.append({"title": n["title"], "text": n["text"]})
                files = body.get("files") or []                  # R405: attached files, their text sent raw
                if not isinstance(files, list) or len(files) > MAX_SEND_FILES or not all(
                        isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("text"), str)
                        and 0 < len(f["name"]) <= 200 and len(f["text"]) <= attach.MAX_TEXT
                        and (f.get("path") is None or (isinstance(f["path"], str) and attach.is_input(f["path"]))) for f in files):
                    return self._json(400, {"error": f"files: at most {MAX_SEND_FILES}, each {{name, text}} of up to {attach.MAX_TEXT} characters"})
                picked += [{"title": " ".join(f["name"].replace("]", ")").split()), "text": f["text"], "kind": "file"} for f in files]
                paths = body.get("image_paths") or []            # R406: where the attached pictures were saved
                if not isinstance(paths, list) or len(paths) > MAX_IMAGES or not all(isinstance(p, str) and attach.is_input(p) for p in paths):
                    return self._json(400, {"error": f"image_paths: at most {MAX_IMAGES} pictures this server saved"})
                text = str(body.get("text", ""))
                fpaths = [f["path"] for f in files if f.get("path")]   # R409
                if paths or fpaths:
                    if not text.strip():
                        text = ("(see the attached picture" + ("s)" if len(paths) > 1 else ")")) if paths else \
                               ("(see the attached file" + ("s)" if len(fpaths) > 1 else ")"))
                    text = text.strip() + "".join(f"\n[Attached picture saved at: {p}]" for p in dict.fromkeys(paths)) \
                        + "".join(f"\n[Attached file saved at: {p}]" for p in dict.fromkeys(fpaths))
                ok, why = session.send(text, imgs[:MAX_IMAGES], cid, picked, _temperature(body))
                return self._json(200 if ok else (409 if "running" in why or "full" in why else 400),
                                  ({"ok": True, "duplicate": True} if why == "duplicate" else {"ok": True}) if ok else {"error": why})
            if path in ("/api/models/config/add", "/api/models/config/save", "/api/models/config/delete"):   # R423
                code, out = session.model_edit(path.rsplit("/", 1)[1], body)
                return self._json(code, out)
            if path in ("/api/config/validate", "/api/config/save"):   # R427
                b = body.get("base")
                code, out = session.config_check(str(body.get("name", "")), body.get("text"), b if isinstance(b, str) else None,
                                                 path.endswith("/save"))
                return self._json(code, out)
            if path == "/api/wake":                # R426
                code, out = session.wake()
                return self._json(code, out)
            if path == "/api/prices/refresh":    # R397
                code, out = session.refresh_prices()
                return self._json(code, out)
            if path == "/api/approve":
                aid, ans = body.get("id"), body.get("answer")
                if not isinstance(aid, int) or isinstance(aid, bool) or not isinstance(ans, str):
                    return self._json(400, {"error": "id (int) and answer (y|n|s) required"})
                ok = session.answer(aid, ans)
                return self._json(200 if ok else 409,
                                  {"ok": True} if ok else {"error": "no such pending approval"})
            if path == "/api/stop":
                session.stop()
                return self._json(200, {"ok": True})
            if path == "/api/auto":
                if not isinstance(body.get("on"), bool):
                    return self._json(400, {"error": "on (bool) required"})
                session.set_auto(body["on"])
                return self._json(200, {"ok": True})
            if path == "/api/plan":
                if not isinstance(body.get("on"), bool):
                    return self._json(400, {"error": "on (bool) required"})
                session.set_plan(body["on"])
                return self._json(200, {"ok": True})
            if path == "/api/budget":
                ok, why = session.set_budget(str(body.get("which", "")), body.get("usd"))
                return self._json(200 if ok else 400, {"ok": True} if ok else {"error": why})
            if path == "/api/retry":
                t = body.get("text")
                ok, why = session.retry(t if isinstance(t, str) else None, _temperature(body))
                return self._json(200 if ok else (409 if "running" in why else 400),
                                  {"ok": True} if ok else {"error": why})
            if path == "/api/queue/cancel":      # R418 (I2)
                ok, why = session.cancel_queued(body.get("position"), body.get("text"))
                return self._json(200 if ok else 409, {"ok": True} if ok else {"error": why})
            if path == "/api/share":             # R418 (F1)
                sid = body.get("id")
                if not isinstance(sid, str) or not service.session_exists(sid):
                    return self._json(400, {"error": "no such session"})
                tok, why = shares.create(sid)
                return self._json(200, {"token": tok, "path": "/shared/" + tok}) if tok else self._json(400, {"error": why})
            if path == "/api/share/delete":
                ok = shares.delete(str(body.get("token", "")))
                return self._json(200 if ok else 404, {"ok": True} if ok else {"error": "no such share link"})
            if path == "/api/model":
                ok, why = session.switch_model(body.get("i"))
                return self._json(200 if ok else (409 if "running" in why else 400),
                                  {"ok": True} if ok else {"error": why})
            if path == "/api/new":
                ok, why = session.new_chat()
                return self._json(200 if ok else 409, {"ok": True} if ok else {"error": why})
            if path == "/api/command":
                ok, why = session.command(str(body.get("name", "")))
                return self._json(200 if ok else (409 if "running" in why else 400),
                                  {"ok": True} if ok else {"error": why})
            if path == "/api/resume":
                ok, why = session.resume(str(body.get("id", "")))
                return self._json(200 if ok else (409 if "running" in why else 400),
                                  {"ok": True} if ok else {"error": why})
            if path.startswith("/api/notes/"):
                return self._notes(path, body)
            if path.startswith("/api/chat/") or path.startswith("/api/folder/"):
                return self._organise(path, body)
            if path == "/api/undo":
                ok, why = session.undo()
                return self._json(200 if ok else 409, {"ok": True, "result": why} if ok else {"error": why})
            return self._json(404, {"error": "not found"})

        def _notes(self, path: str, body: dict) -> None:
            """R389: create / save / delete a plain-text note."""
            if path == "/api/notes/create":
                src = body.get("source_session")
                src = src if isinstance(src, str) and service.session_exists(src) else None
                new, why = notes.create(str(body.get("title", "")), str(body.get("text", "")), src,
                                        str(body.get("type") or "personal"))     # R422
                return self._json(200 if new else 400, {"ok": True, "id": new} if new else {"error": why})
            if path == "/api/notes/save":
                base = body.get("base")
                base = base if isinstance(base, (int, float)) and not isinstance(base, bool) else None
                upd, why = notes.save(str(body.get("id", "")), str(body.get("title", "")), str(body.get("text", "")), base)
                if upd is not None:
                    return self._json(200, {"ok": True, "updated": upd})
                return self._json(409 if why == notes.CONFLICT else (404 if why == "no such note" else 400), {"error": why})
            elif path == "/api/notes/order":                     # R404: drag to reorder
                ok, why = notes.reorder(body.get("ids"))
                return self._json(200 if ok else (404 if why == "no such note" else 400), {"ok": True} if ok else {"error": why})
            elif path == "/api/notes/type":                      # R422: move a note to Personal / Work / Skills
                ok, why = notes.set_type(str(body.get("id", "")), str(body.get("type", "")))
                return self._json(200 if ok else (404 if why == "no such note" else 400), {"ok": True} if ok else {"error": why})
            elif path == "/api/notes/delete":
                ok, why = notes.delete(str(body.get("id", "")))
            else:
                return self._json(404, {"error": "not found"})
            self._json(200 if ok else 400, {"ok": True} if ok else {"error": why})

        def _transcribe(self) -> None:
            """R392 voice input: the page's recording (webm/ogg/mp4/wav) -> 16 kHz mono WAV (ffmpeg) -> whisper /inference."""
            url = os.environ.get("AURORA_WEB_STT_URL", "").rstrip("/")
            n = self._length()
            if not url:
                self.close_connection = True
                return self._json(501, {"error": "voice input is not set up on this server (AURORA_WEB_STT_URL)"})
            if n <= 0 or n > MAX_AUDIO:
                self.close_connection = True
                return self._json(413, {"error": "recording too large or empty"})
            audio = self.rfile.read(n)
            lang = parse_qs(urlparse(self.path).query).get("lang", ["en"])[0].lower()   # R400: the page's Language setting
            lang = lang if lang in STT_LANGS else "en"
            if not shutil.which("ffmpeg"):
                return self._json(501, {"error": "voice input needs ffmpeg on the server"})
            try:
                wav = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
                                     input=audio, capture_output=True, timeout=60, check=True).stdout
            except (subprocess.SubprocessError, OSError):
                return self._json(400, {"error": "the recording could not be read"})
            b = uuid.uuid4().hex
            data = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode() \
                + wav + f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n".encode() \
                + f"--{b}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{lang}\r\n--{b}--\r\n".encode()
            req = urllib.request.Request(url + "/inference", data=data, method="POST",
                                         headers={"Content-Type": f"multipart/form-data; boundary={b}"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    text = str(json.loads(r.read() or b"{}").get("text", "")).strip()
            except Exception as e:
                return self._json(502, {"error": f"speech-to-text failed: {type(e).__name__}"})
            return self._json(200, {"text": text})

        def _organise(self, path: str, body: dict) -> None:
            """R383: delete (hide) chats, create / rename / open / delete folders, move a chat into one."""
            sid, fid = str(body.get("id", "")), str(body.get("folder") or "")
            if path == "/api/chat/delete":
                ok, why = session.delete_chat(sid)
            elif path == "/api/chat/rename":
                ok, why = session.rename_chat(sid, str(body.get("name", "")))
            elif path == "/api/chat/move":
                ok, why = session.move_chat(str(body.get("chat", "")), fid or None)
            elif path == "/api/folder/create":
                new, why = folders.create(str(body.get("name", "")))
                return self._json(200 if new else 400, {"ok": True, "id": new} if new else {"error": why})
            elif path == "/api/folder/rename":
                ok, why = folders.rename(sid, str(body.get("name", "")))
            elif path == "/api/folder/open":
                ok = folders.set_open(sid, bool(body.get("open")))
                why = "" if ok else "no such folder"
            elif path == "/api/folder/delete":
                ok, why = session.delete_folder(sid)
            else:
                return self._json(404, {"error": "not found"})
            self._json(200 if ok else 400, {"ok": True} if ok else {"error": why})

    return Handler


def startup_problems() -> list[str]:
    """R392 (bug 4): optional packages a feature needs, said at start instead of failing on first use."""
    out = []
    for mod, why in (("pypdf", "PDF attachments are refused"), ("PIL", "pictures are not resized or rotated")):
        try:
            __import__(mod)
        except ImportError:
            out.append(f"{mod} is missing: {why} (pip install -e . into this venv)")
    return out


def health(session) -> dict:
    """R392 /health: no token needed, no secrets in it. ok=false (503) when a store is unreadable or a package is missing."""
    from ..service import aurora_home
    pk = {}
    for mod in ("pypdf", "PIL"):
        try:
            __import__(mod)
            pk[mod] = True
        except ImportError:
            pk[mod] = False
    stores = {}
    for name, f in (("notes", lambda: notes.listing()), ("folders", lambda: folders.view())):
        try:
            f()
            stores[name] = True
        except Exception:
            stores[name] = False
    problems = [p for p in (notes.problem, folders.problem) if p]
    model = None
    try:                          # a TCP connect only: an HTTP request would wake a model the idle-manager put to sleep
        import socket
        prov = session.engine._provider_for(session.engine.current)
        u = urlparse(str(getattr(prov, "base_url", "") or ""))
        if u.hostname:
            with socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=2):
                model = True
    except Exception:
        model = False
    ok = all(pk.values()) and all(stores.values())
    return {"ok": ok, "version": _code_version_safe(), "packages": pk, "stores": stores, "problems": problems,
            "model_reachable": model, "busy": session.busy, "streams": session.streams,
            "home_writable": os.access(aurora_home(), os.W_OK)}


def _code_version_safe() -> str:
    try:
        from .. import service
        return service.version()
    except Exception:
        return "?"


def serve(engine, bind: str, port: int, workspace: str) -> None:
    token = os.environ.get("AURORA_WEB_TOKEN", "")
    if not token:
        raise SystemExit("aurora --web: AURORA_WEB_TOKEN is not set — refusing to start without one")
    entry = next((m for m in engine.models
                  if m.get("provider") == "local" and m.get("model") == "local"), None)
    if entry is None:
        raise SystemExit("aurora --web: config.yaml has no `local` model entry (provider: local, model: local)")
    if bind not in ("127.0.0.1", "::1", "localhost"):   # R393: the page can run tools, so a wider bind is said loudly
        sys.stderr.write(f"aurora --web: WARNING: listening on {bind}, not loopback. Anyone who can reach this address and has the "
                         "token can run commands on this machine. Put it behind a TLS proxy on a private network; see SECURITY.md.\n")
    os.chdir(os.path.expanduser(workspace))
    engine.current = entry                  # not switch_model: that would change the TUI's remembered model
    from .. import service                  # R387: the page's own remembered pick wins over the pinned default
    st = service.load_state()
    last = next((m for m in engine.models if m.get("model") == st.get("web_last_model")
                 and m.get("provider") == st.get("web_last_provider") and engine._has_key(m.get("provider"))), None)
    if last is not None:
        engine.current = last
    session = WebSession(engine)
    for problem in startup_problems():
        sys.stderr.write(f"aurora --web: warning: {problem}\n")   # the journal shows it
    httpd = Server((bind, port), make_handler(session, token))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
