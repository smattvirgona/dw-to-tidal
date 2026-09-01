#!/usr/bin/env python3
"""
Discover Weekly → TIDAL  (local app)

Runs on your own computer. Opens a page in your browser where you paste your
Discover Weekly link, connect TIDAL once, and either press "Copy this week's
playlist" or switch on the weekly schedule.

Install (once):
    pip3 install requests tidalapi

Run:
    python3 dw2tidal_app.py

The page opens at http://127.0.0.1:8765. Settings and the TIDAL login are
stored in ~/.dw2tidal/. Nothing leaves your computer except the calls to
Spotify and TIDAL.

Optional: a Spotify client ID/secret (developer.spotify.com/dashboard → Create
app) makes matching exact via ISRC codes. Without them the app matches by
artist + title, which is right most of the time.

The schedule only fires while this app is open.
"""

import datetime
import json
import os
import pathlib
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    import requests
    import tidalapi
except ImportError:
    sys.exit("Missing packages. Run:  pip3 install requests tidalapi")

PORT = 8765
CONFIG_DIR = pathlib.Path.home() / ".dw2tidal"
CONFIG_FILE = CONFIG_DIR / "config.json"
TIDAL_SESSION_FILE = CONFIG_DIR / "tidal-session.json"
STATE_FILE = CONFIG_DIR / "state.json"
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DEFAULT_CONFIG = {
    "dw_url": "",
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "schedule_enabled": False,
    "weekday": 0,
    "hour": 8,
}

LOCK = threading.Lock()
STATE = {
    "log": [],
    "running": False,
    "tidal_login_url": None,
    "tidal_connected": False,
    "tidal_user": "",
    "last_run_date": None,
    "last_result": "",
}
SESSION: "tidalapi.Session | None" = None


# ---------------------------------------------------------------- storage

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except json.JSONDecodeError:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def load_state() -> None:
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text())
            STATE["last_run_date"] = saved.get("last_run_date")
            STATE["last_result"] = saved.get("last_result", "")
        except json.JSONDecodeError:
            pass


def save_state() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "last_run_date": STATE["last_run_date"],
        "last_result": STATE["last_result"],
    }))


def log(msg: str) -> None:
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    with LOCK:
        STATE["log"].append(f"{stamp}  {msg}")
        STATE["log"] = STATE["log"][-300:]
    print(msg, flush=True)


# ---------------------------------------------------------------- spotify

def fetch_discover_weekly(url: str) -> list[dict]:
    """Track list from the public embed page. Returns [{id, title, artist}]."""
    m = re.search(r"playlist/([A-Za-z0-9]+)", url)
    if not m:
        raise ValueError("That doesn't look like a Spotify playlist link.")
    pid = m.group(1)
    r = requests.get(
        f"https://open.spotify.com/embed/playlist/{pid}",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("Spotify's embed page has changed; track list not found.")
    data = json.loads(m.group(1))
    try:
        items = data["props"]["pageProps"]["state"]["data"]["entity"]["trackList"]
    except KeyError:
        raise RuntimeError("Spotify's embed page has changed; track list not found.")
    out = []
    for i in items:
        uri = i.get("uri", "")
        if not uri.startswith("spotify:track:"):
            continue
        out.append({
            "id": uri.rsplit(":", 1)[-1],
            "title": i.get("title", ""),
            "artist": (i.get("subtitle") or "").split(",")[0].strip(),
            "isrc": None,
        })
    return out


def add_isrcs(tracks: list[dict], client_id: str, client_secret: str) -> None:
    """Fill in ISRC codes via Spotify's catalog API (client-credentials)."""
    tok = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=30,
    )
    tok.raise_for_status()
    headers = {"Authorization": f"Bearer {tok.json()['access_token']}"}
    by_id = {t["id"]: t for t in tracks}
    ids = list(by_id)
    for i in range(0, len(ids), 50):
        r = requests.get(
            "https://api.spotify.com/v1/tracks",
            params={"ids": ",".join(ids[i:i + 50])},
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        for t in r.json().get("tracks", []):
            if t and t["id"] in by_id:
                by_id[t["id"]]["isrc"] = (t.get("external_ids") or {}).get("isrc")
                by_id[t["id"]]["title"] = t["name"]
                by_id[t["id"]]["artist"] = t["artists"][0]["name"]


# ---------------------------------------------------------------- tidal

def tidal_session() -> "tidalapi.Session":
    global SESSION
    if SESSION is None:
        SESSION = tidalapi.Session()
        if TIDAL_SESSION_FILE.exists():
            try:
                SESSION.load_session_from_file(TIDAL_SESSION_FILE)
            except Exception:
                pass
        refresh_tidal_status()
    return SESSION


def refresh_tidal_status() -> None:
    s = SESSION
    ok = False
    name = ""
    if s is not None:
        try:
            ok = bool(s.check_login())
            if ok:
                name = getattr(s.user, "username", "") or getattr(s.user, "email", "") or ""
        except Exception:
            ok = False
    with LOCK:
        STATE["tidal_connected"] = ok
        STATE["tidal_user"] = name


def start_tidal_login() -> None:
    def worker():
        s = tidal_session()
        try:
            login, future = s.login_oauth()
            url = login.verification_uri_complete
            if not url.startswith("http"):
                url = "https://" + url
            with LOCK:
                STATE["tidal_login_url"] = url
            log("Open the TIDAL link on the page and approve this app.")
            future.result()
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            s.save_session_to_file(TIDAL_SESSION_FILE)
            refresh_tidal_status()
            log("TIDAL connected.")
        except Exception as e:
            log(f"TIDAL login failed: {e}")
        finally:
            with LOCK:
                STATE["tidal_login_url"] = None

    threading.Thread(target=worker, daemon=True).start()


def match_on_tidal(s: "tidalapi.Session", tracks: list[dict]) -> tuple[list[int], list[str]]:
    ids, missed = [], []
    for t in tracks:
        hit = None
        if t.get("isrc"):
            try:
                r = s.get_tracks_by_isrc(t["isrc"])
                hit = r[0] if r else None
            except Exception:
                hit = None
        if hit is None and (t["artist"] or t["title"]):
            try:
                r = s.search(f'{t["artist"]} {t["title"]}', models=[tidalapi.Track], limit=1)["tracks"]
                hit = r[0] if r else None
            except Exception:
                hit = None
        if hit:
            ids.append(hit.id)
        else:
            missed.append(f'{t["artist"]} – {t["title"]}')
    return ids, missed


# ---------------------------------------------------------------- the job

def run_job(trigger: str) -> None:
    with LOCK:
        if STATE["running"]:
            return
        STATE["running"] = True
    try:
        cfg = load_config()
        if not cfg["dw_url"]:
            log("No Discover Weekly link saved yet.")
            return
        s = tidal_session()
        refresh_tidal_status()
        if not STATE["tidal_connected"]:
            log("TIDAL isn't connected. Press 'Connect TIDAL' first.")
            return

        name = f"Discover Weekly {datetime.date.today():%Y-%m-%d}"
        if any(p.name == name for p in s.user.playlists()):
            log(f"'{name}' already exists in TIDAL. Nothing to do.")
            STATE["last_run_date"] = str(datetime.date.today())
            save_state()
            return

        log(f"Reading Discover Weekly ({trigger})…")
        tracks = fetch_discover_weekly(cfg["dw_url"])
        if not tracks:
            log("Discover Weekly came back empty.")
            return
        log(f"Found {len(tracks)} tracks.")

        if cfg["spotify_client_id"] and cfg["spotify_client_secret"]:
            try:
                add_isrcs(tracks, cfg["spotify_client_id"], cfg["spotify_client_secret"])
                log("Fetched ISRC codes from Spotify.")
            except Exception as e:
                log(f"Spotify ISRC lookup failed ({e}); matching by artist + title instead.")

        log("Matching on TIDAL…")
        tidal_ids, missed = match_on_tidal(s, tracks)
        if not tidal_ids:
            log("No matches found on TIDAL. Playlist not created.")
            return

        pl = s.user.create_playlist(name, "Mirrored from Spotify Discover Weekly")
        pl.add(tidal_ids)
        result = f"Created '{name}' with {len(tidal_ids)} of {len(tracks)} tracks."
        log(result)
        for m in missed:
            log(f"   not found: {m}")
        STATE["last_run_date"] = str(datetime.date.today())
        STATE["last_result"] = result
        save_state()
    except Exception as e:
        log(f"Stopped: {e}")
    finally:
        with LOCK:
            STATE["running"] = False


def scheduler() -> None:
    while True:
        try:
            cfg = load_config()
            now = datetime.datetime.now()
            if (
                cfg["schedule_enabled"]
                and now.weekday() == int(cfg["weekday"])
                and now.hour == int(cfg["hour"])
                and STATE["last_run_date"] != str(now.date())
            ):
                run_job("scheduled")
        except Exception as e:
            log(f"Scheduler error: {e}")
        time.sleep(60)


# ---------------------------------------------------------------- web ui

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Discover Weekly → TIDAL</title>
<style>
  :root {
    --bg: #E9ECEF;
    --panel: #FFFFFF;
    --ink: #17202A;
    --muted: #5B6672;
    --line: #CDD3D9;
    --accent: #2B4C7E;
    --accent-ink: #FFFFFF;
    --ok: #1F7A4D;
    --warn: #A64B2A;
    --log-bg: #17202A;
    --log-ink: #DCE3EA;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #12171C; --panel: #1B2229; --ink: #E6EBF0; --muted: #98A4B0;
      --line: #2E3944; --accent: #7FA6E0; --accent-ink: #0F1720;
      --ok: #6CCB94; --warn: #E48A63; --log-bg: #0C1014; --log-ink: #C7D0D9;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  main { max-width: 980px; margin: 0 auto; padding: 32px 20px 48px; }
  h1 { font-size: 30px; font-weight: 600; letter-spacing: -0.01em; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 28px; max-width: 60ch; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 760px) { .grid { grid-template-columns: 1fr; } }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 20px; }
  h2 { font-size: 17px; font-weight: 600; margin: 0 0 14px; }
  label { display: block; font-size: 14px; color: var(--muted); margin: 12px 0 4px; }
  input[type=text], input[type=password], select {
    width: 100%; padding: 9px 10px; font: inherit; color: var(--ink);
    background: var(--bg); border: 1px solid var(--line); border-radius: 6px;
  }
  input:focus, select:focus, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .row > * { flex: 1; }
  button {
    font: inherit; font-weight: 600; padding: 10px 16px; border-radius: 6px;
    border: 1px solid var(--accent); background: var(--accent); color: var(--accent-ink); cursor: pointer;
  }
  button.quiet { background: transparent; color: var(--accent); }
  button:disabled { opacity: .5; cursor: default; }
  .actions { margin-top: 16px; display: flex; gap: 10px; flex-wrap: wrap; }
  .status { margin: 0 0 14px; }
  .status b { font-weight: 600; }
  .ok { color: var(--ok); } .warn { color: var(--warn); }
  .toggle { display: flex; align-items: center; gap: 10px; margin-top: 14px; }
  .toggle input { width: 18px; height: 18px; }
  .note { font-size: 13px; color: var(--muted); margin: 6px 0 0; }
  details { margin-top: 16px; } summary { cursor: pointer; color: var(--muted); font-size: 14px; }
  pre {
    margin: 12px 0 0; background: var(--log-bg); color: var(--log-ink); padding: 14px;
    border-radius: 8px; font: 13px/1.5 ui-monospace, Menlo, Consolas, monospace;
    max-height: 420px; overflow: auto; white-space: pre-wrap;
  }
  .login a { color: var(--accent); font-weight: 600; word-break: break-all; }
  .saved { font-size: 14px; color: var(--ok); margin-left: 8px; }
</style>
</head>
<body>
<main>
  <h1>Discover Weekly → TIDAL</h1>
  <p class="sub">Copies this week's Spotify Discover Weekly into a new, dated TIDAL playlist. Runs on this computer only.</p>

  <div class="grid">
    <section>
      <h2>Setup</h2>
      <label for="dw">Discover Weekly link</label>
      <input id="dw" type="text" placeholder="https://open.spotify.com/playlist/37i9dQZEVX…" autocomplete="off">
      <p class="note">In Spotify: open Discover Weekly → ⋯ → Share → Copy link. It stays the same every week.</p>

      <details>
        <summary>Optional: exact matching with a Spotify developer key</summary>
        <label for="cid">Client ID</label>
        <input id="cid" type="text" autocomplete="off">
        <label for="csec">Client secret</label>
        <input id="csec" type="password" autocomplete="off">
        <p class="note">From developer.spotify.com/dashboard → Create app. Lets the app match by ISRC code instead of artist + title.</p>
      </details>

      <div class="actions"><button id="save">Save settings</button><span id="savedmsg"></span></div>
    </section>

    <section>
      <h2>TIDAL</h2>
      <p class="status" id="tidal-status">Checking…</p>
      <div class="login" id="login-box" hidden>
        <p>Open this link, sign in, and approve. Then come back here.</p>
        <p><a id="login-link" href="#" target="_blank" rel="noopener"></a></p>
      </div>
      <div class="actions"><button id="connect" class="quiet">Connect TIDAL</button></div>

      <h2 style="margin-top:24px">Weekly schedule</h2>
      <div class="row">
        <select id="weekday"></select>
        <select id="hour"></select>
      </div>
      <div class="toggle"><input id="enabled" type="checkbox"><label for="enabled" style="margin:0;color:var(--ink)">Run every week at this time</label></div>
      <p class="note">Discover Weekly refreshes on Monday. The schedule only fires while this app is open.</p>
      <div class="actions"><button id="save2">Save schedule</button><span id="savedmsg2"></span></div>
    </section>
  </div>

  <section style="margin-top:20px">
    <h2>Run</h2>
    <p class="status" id="last"></p>
    <div class="actions"><button id="run">Copy this week's playlist to TIDAL</button></div>
    <pre id="log">Waiting.</pre>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
const days = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
days.forEach((d,i) => $("weekday").add(new Option(d, i)));
for (let h = 0; h < 24; h++) $("hour").add(new Option(String(h).padStart(2,"0") + ":00", h));

async function api(path, body) {
  const r = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)} : {});
  return r.json();
}
function flash(id) { $(id).textContent = "Saved"; setTimeout(() => $(id).textContent = "", 1800); }

async function loadConfig() {
  const c = await api("/config");
  $("dw").value = c.dw_url; $("cid").value = c.spotify_client_id; $("csec").value = c.spotify_client_secret;
  $("weekday").value = c.weekday; $("hour").value = c.hour; $("enabled").checked = c.schedule_enabled;
}
function gather() {
  return { dw_url: $("dw").value.trim(), spotify_client_id: $("cid").value.trim(), spotify_client_secret: $("csec").value.trim(),
           weekday: +$("weekday").value, hour: +$("hour").value, schedule_enabled: $("enabled").checked };
}
$("save").onclick = async () => { await api("/config", gather()); flash("savedmsg"); };
$("save2").onclick = async () => { await api("/config", gather()); flash("savedmsg2"); };
$("connect").onclick = async () => { await api("/tidal/login", {}); };
$("run").onclick = async () => { await api("/run", {}); };

async function poll() {
  const s = await api("/status");
  const ts = $("tidal-status");
  if (s.tidal_connected) { ts.innerHTML = '<b class="ok">Connected</b>' + (s.tidal_user ? " as " + s.tidal_user : ""); $("connect").textContent = "Reconnect"; }
  else { ts.innerHTML = '<b class="warn">Not connected</b>'; $("connect").textContent = "Connect TIDAL"; }
  if (s.tidal_login_url) { $("login-box").hidden = false; $("login-link").href = s.tidal_login_url; $("login-link").textContent = s.tidal_login_url; }
  else { $("login-box").hidden = true; }
  $("run").disabled = s.running;
  $("run").textContent = s.running ? "Working…" : "Copy this week's playlist to TIDAL";
  $("last").textContent = s.last_result ? "Last result: " + s.last_result : "No playlist copied yet.";
  const pre = $("log"); const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
  pre.textContent = s.log.length ? s.log.join("\n") : "Waiting.";
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}
loadConfig(); poll(); setInterval(poll, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence request logging
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/config":
            self._json(load_config())
        elif path == "/status":
            with LOCK:
                snap = dict(STATE)
                snap["log"] = list(STATE["log"])
            self._json(snap)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/config":
            cfg = load_config()
            incoming = self._body()
            for k in DEFAULT_CONFIG:
                if k in incoming:
                    cfg[k] = incoming[k]
            save_config(cfg)
            self._json({"ok": True})
        elif path == "/tidal/login":
            start_tidal_login()
            self._json({"ok": True})
        elif path == "/run":
            threading.Thread(target=run_job, args=("manual",), daemon=True).start()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def main() -> None:
    load_state()
    tidal_session()
    threading.Thread(target=scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Discover Weekly → TIDAL is running at {url}  (Ctrl-C to quit)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
