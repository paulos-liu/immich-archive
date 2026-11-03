"""Password-gated admin dashboard. Archive numbers come from the checkpoint, not the disk."""

from __future__ import annotations

import hashlib
import hmac
import html
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from immich_archive.checkpoint import Checkpoint, probe
from immich_archive.config import Config, runtime_settings, save_runtime_settings
from immich_archive.decide import next_cron_time
from immich_archive.runner import run_backup

UNCONFIGURED = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Immich Archive</title></head>
<body>
<p>Admin password is not configured</p>
<p>The dashboard is off until ADMIN_PASSWORD is set.</p>
</body>
</html>
"""

STYLE = """
:root {
  --header: #0b1f33;
  --field: #e4eef3;
  --ink: #14202b;
  --signal: #0f6e82;
  --failure: #8f1d2c;
  --paper: #f4f8fa;
  --line: #b7c9d4;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--field);
  color: var(--ink);
  font-family: "Avenir Next", "Segoe UI", sans-serif;
}
header {
  background: var(--header);
  color: #e7f2f6;
  padding: 28px 7vw 24px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 24px;
}
header h1 {
  margin: 0;
  font-family: "Avenir Next Condensed", "Avenir Next", sans-serif;
  font-weight: 700;
  letter-spacing: 0.14em;
  font-size: 28px;
}
header p { margin: 6px 0 0; color: #b7c9d4; letter-spacing: 0.04em; }
.stamp {
  border: 2px solid #9fd0dc;
  color: #d7eef3;
  font-family: "Avenir Next Condensed", "Avenir Next", sans-serif;
  font-weight: 700;
  letter-spacing: 0.22em;
  padding: 12px 14px;
  transform: rotate(-6deg);
  text-transform: uppercase;
  white-space: nowrap;
}
main { max-width: 980px; margin: 0 auto; padding: 28px 20px 64px; }
.slip {
  background: var(--paper);
  border: 1px solid var(--line);
  padding: 22px;
}
.slip h2 {
  margin: 0 0 14px;
  font-family: "Avenir Next Condensed", "Avenir Next", sans-serif;
  letter-spacing: 0.12em;
  font-size: 18px;
  color: var(--signal);
}
.counts { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.counts div { border-top: 3px solid var(--signal); padding-top: 8px; }
.counts span, .meta span, td { font-family: ui-monospace, "SFMono-Regular", monospace; }
.counts strong {
  display: block;
  font-family: "Avenir Next Condensed", "Avenir Next", sans-serif;
  letter-spacing: 0.08em;
  font-size: 13px;
  font-weight: 600;
}
.counts .bad span { color: var(--failure); }
.meta { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-top: 22px; }
label { display: block; margin: 10px 0; }
input, button {
  font: inherit;
  color: var(--ink);
}
input {
  display: block;
  width: 100%;
  margin-top: 4px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  background: white;
}
button {
  background: var(--signal);
  color: white;
  border: 0;
  padding: 10px 16px;
  letter-spacing: 0.06em;
  cursor: pointer;
}
.actions { display: flex; gap: 10px; margin: 18px 0; }
.actions form { margin: 0; }
button.secondary { background: var(--header); }
.note { color: var(--failure); }
table { width: 100%; border-collapse: collapse; margin-top: 12px; }
th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }
th {
  font-family: "Avenir Next Condensed", "Avenir Next", sans-serif;
  letter-spacing: 0.08em;
  font-size: 13px;
}
a { color: var(--signal); }
.readonly input { background: var(--field); }
@media (max-width: 720px) {
  .counts, .meta { grid-template-columns: 1fr 1fr; }
  header { align-items: flex-start; }
}
"""


def _token(password: str) -> str:
    nonce = secrets.token_urlsafe(24)
    signature = hmac.new(password.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return f"{nonce}.{signature}"


def _token_ok(password: str, token: str) -> bool:
    nonce, separator, signature = token.partition(".")
    if not separator or not nonce or not signature:
        return False
    expected = hmac.new(password.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _password_ok(configured: str, supplied: str) -> bool:
    if not configured or not supplied:
        return False
    left = configured.encode()
    right = supplied.encode()
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _age(uploaded_at: str) -> str:
    try:
        then = datetime.strptime(uploaded_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "unknown"
    seconds = int((datetime.now(timezone.utc) - then).total_seconds())
    if seconds < 90:
        return f"{max(seconds, 0)} seconds"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minutes"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hours"
    return f"{hours // 24} days"


def _view(config: Config) -> dict:
    cron, concurrency = runtime_settings(config)
    zone = ZoneInfo(config.tz)
    upcoming = next_cron_time(cron, datetime.now(zone), config.tz)
    view = {
        "cron": cron,
        "concurrency": concurrency,
        "next_run": upcoming.isoformat(),
        "bucket": config.bucket,
        "region": config.region,
        "access_key": config.access_key,
        "scanned": 0,
        "uploaded": 0,
        "skipped": 0,
        "failed": 0,
        "duration": "0.00s",
        "summary": "No run yet",
        "archive_files": 0,
        "archive_bytes": 0,
        "dump_name": "missing",
        "dump_uploaded_at": "",
        "dump_age": "missing",
        "corrupt": False,
        "files": [],
        "failures": [],
    }
    path = config.sqlite_path()
    if probe(path) != "ok":
        view["corrupt"] = path.exists()
        return view
    checkpoint = Checkpoint.open_readonly(path)
    try:
        count, total = checkpoint.totals()
        view["archive_files"] = count
        view["archive_bytes"] = total
        dump = checkpoint.get_dump()
        if dump is not None:
            view["dump_name"] = str(dump["source_name"])
            view["dump_uploaded_at"] = str(dump["uploaded_at"])
            view["dump_age"] = _age(str(dump["uploaded_at"]))
        last = checkpoint.last_run()
        if last is not None:
            view["scanned"] = int(last["scanned"])
            view["uploaded"] = int(last["uploaded"])
            view["skipped"] = int(last["skipped"])
            view["failed"] = int(last["failed"])
            view["duration"] = f"{float(last['duration_seconds']):.2f}s"
            view["summary"] = str(last["summary"])
        return view
    finally:
        checkpoint.close()


def _search(config: Config, query: str) -> tuple[list[dict], list[dict]]:
    path = config.sqlite_path()
    if probe(path) != "ok":
        return [], []
    checkpoint = Checkpoint.open_readonly(path)
    try:
        return checkpoint.search_files(query), checkpoint.last_failures(query)
    finally:
        checkpoint.close()


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<header>
  <div>
    <h1>IMMICH ARCHIVE</h1>
    <p>Cold-storage vault slip</p>
  </div>
  <div class="stamp">Deep Archive</div>
</header>
<main>{body}</main>
</body>
</html>
"""


def _login_page() -> str:
    body = """
<section class="slip">
  <h2>SIGN IN</h2>
  <form method="post" action="/login">
    <label>Password <input type="password" name="password" autocomplete="current-password"></label>
    <button type="submit">Sign in</button>
  </form>
</section>
"""
    return _page("Sign in", body)


def _overview(config: Config) -> str:
    view = _view(config)
    failed_class = " bad" if view["failed"] else ""
    corrupt = ""
    if view["corrupt"]:
        corrupt = (
            f'<p class="note">Checkpoint unreadable. Delete {html.escape(str(config.sqlite_path()))} '
            "and rerun. The archive was not modified.</p>"
        )
    body = f"""
<section class="slip">
  <h2>VAULT SLIP</h2>
  {corrupt}
  <div class="counts">
    <div><strong>SCANNED</strong><span data-field="scanned">{view["scanned"]}</span></div>
    <div><strong>UPLOADED</strong><span data-field="uploaded">{view["uploaded"]}</span></div>
    <div><strong>SKIPPED</strong><span data-field="skipped">{view["skipped"]}</span></div>
    <div class="{failed_class.strip()}"><strong>FAILED</strong><span data-field="failed">{view["failed"]}</span></div>
  </div>
  <p>Last run <span data-field="duration">{html.escape(str(view["duration"]))}</span>. {html.escape(str(view["summary"]))}</p>
  <div class="meta">
    <div>
      <strong>NEXT RUN</strong>
      <div><span data-field="next-run">{html.escape(view["next_run"])}</span></div>
      <p>{html.escape(config.tz)}</p>
    </div>
    <div>
      <strong>ARCHIVE</strong>
      <div><span data-field="archive-files">{view["archive_files"]}</span> files, <span data-field="archive-bytes">{view["archive_bytes"]}</span> bytes</div>
      <p>Dump <span data-field="dump-name">{html.escape(str(view["dump_name"]))}</span>
         <span data-field="dump-uploaded-at">{html.escape(str(view["dump_uploaded_at"]))}</span>
         ({html.escape(str(view["dump_age"]))})</p>
    </div>
  </div>
  <div class="actions">
    <form method="post" action="/run"><button type="submit">Run now</button></form>
    <form method="post" action="/dry-run"><button class="secondary" type="submit">Dry run</button></form>
    <form method="get" action="/files"><button class="secondary" type="submit">Files</button></form>
  </div>
  <form method="post" action="/settings">
    <label>Cron <input name="cron" value="{html.escape(view["cron"], quote=True)}"></label>
    <label>Concurrency <input name="concurrency" inputmode="numeric" value="{view["concurrency"]}"></label>
    <button type="submit">Save schedule</button>
  </form>
  <div class="readonly">
    <label>Bucket <input value="{html.escape(view["bucket"], quote=True)}" readonly></label>
    <label>Region <input value="{html.escape(view["region"], quote=True)}" readonly></label>
    <label>Access key <input value="{html.escape(view["access_key"], quote=True)}" readonly></label>
    <label>Secret key <input value="hidden" readonly></label>
  </div>
</section>
"""
    return _page("Immich Archive", body)


def _files_page(config: Config, query: str) -> str:
    files, failures = _search(config, query)
    rows = []
    for item in files:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item['relative_path']))}</td>"
            f"<td>{html.escape(str(item['size']))}</td>"
            f"<td>{html.escape(str(item['sha256']))}</td>"
            f"<td>{html.escape(str(item['s3_key']))}</td>"
            f"<td>{html.escape(str(item['uploaded_at']))}</td>"
            "</tr>"
        )
    failure_rows = []
    for item in failures:
        failure_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item['relative_path']))}</td>"
            f"<td>{html.escape(str(item['error']))}</td>"
            "</tr>"
        )
    body = f"""
<section class="slip">
  <h2>FILES</h2>
  <p><a href="/">Back to the vault slip</a></p>
  <form method="get" action="/files">
    <label>Path <input name="q" value="{html.escape(query, quote=True)}"></label>
    <button type="submit">Search</button>
  </form>
  <table>
    <thead><tr><th>PATH</th><th>SIZE</th><th>CHECKSUM</th><th>OBJECT KEY</th><th>UPLOADED</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <h2>LAST RUN FAILURES</h2>
  <table>
    <thead><tr><th>PATH</th><th>ERROR</th></tr></thead>
    <tbody>{''.join(failure_rows)}</tbody>
  </table>
</section>
"""
    return _page("Files", body)


def _result_page(heading: str, summary: str) -> str:
    body = f"""
<section class="slip">
  <h2>{html.escape(heading)}</h2>
  <p data-field="summary">{html.escape(summary)}</p>
  <p><a href="/">Back to the vault slip</a></p>
</section>
"""
    return _page(heading, body)


class ArchiveServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, config: Config):
        self.archive_config = config
        super().__init__((config.host, config.port), Handler)


class Handler(BaseHTTPRequestHandler):
    server: ArchiveServer

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def _route(self, method: str) -> None:
        config = self.server.archive_config
        if not config.admin_password:
            self._send(503, UNCONFIGURED)
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._authed(config):
            if method == "POST" and path == "/login":
                self._login(config)
                return
            self._send(401, _login_page())
            return
        query = parse_qs(parsed.query)
        if method == "GET" and path in ("/", "/overview"):
            self._send(200, _overview(config))
            return
        if method == "GET" and path == "/files":
            text = query.get("q", [""])[0]
            self._send(200, _files_page(config, text))
            return
        if method == "POST" and path == "/settings":
            self._settings(config)
            return
        if method == "POST" and path == "/run":
            self._start(config, dry_run=False)
            return
        if method == "POST" and path == "/dry-run":
            self._start(config, dry_run=True)
            return
        self._send(404, _page("Not found", "<p>Not found</p>"))

    def _authed(self, config: Config) -> bool:
        raw = self.headers.get("Cookie", "")
        token = ""
        for part in raw.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == "session":
                token = value
                break
        return bool(token) and _token_ok(config.admin_password, token)

    def _login(self, config: Config) -> None:
        form = self._form()
        supplied = form.get("password", [""])[0]
        if not _password_ok(config.admin_password, supplied):
            self._send(401, _login_page())
            return
        token = _token(config.admin_password)
        body = b""
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"session={token}; HttpOnly; SameSite=Lax; Path=/")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _settings(self, config: Config) -> None:
        form = self._form()
        cron = form.get("cron", [""])[0]
        raw_concurrency = form.get("concurrency", [""])[0].strip()
        try:
            concurrency = int(raw_concurrency)
            save_runtime_settings(config, cron, concurrency)
        except (ValueError, KeyError):
            self._send(400, _page("Schedule", "<p class=\"note\">Enter a cron expression and a concurrency of at least 1.</p>"))
            return
        self._redirect("/")

    def _start(self, config: Config, *, dry_run: bool) -> None:
        result = run_backup(config, dry_run=dry_run)
        if result.skipped_lock:
            self._send(409, _page("Busy", "<p>A backup is already running.</p>"))
            return
        if dry_run:
            self._send(200, _result_page("DRY RUN", result.summary))
            return
        self._redirect("/")

    def _form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 1_000_000:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        return parse_qs(raw, keep_blank_values=True)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def make_server(config: Config) -> ArchiveServer:
    return ArchiveServer(config)
