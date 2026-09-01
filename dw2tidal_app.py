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
import logging
import logging.handlers
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
LOG_FILE = CONFIG_DIR / "app.log"
HTTP_TIMEOUT = 30          # seconds, for every Spotify and TIDAL request
RETRY_AFTER_FAILURE = 30 * 60  # scheduler waits this long before retrying a failed run
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DEFAULT_CONFIG = {
    "dw_url": "",
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "schedule_enabled": False,
    "weekday": 0,
    "hour": 8,
    "folder_name": "Weekly discoveries",
}
PLAYLIST_PREFIX = "Discover Weekly "

LOCK = threading.Lock()
STATE = {
    "log": [],
    "running": False,
    "tidal_login_url": None,
    "tidal_connected": False,
    "tidal_user": "",
    "last_run_date": None,
    "last_result": "",
    "last_run_ok": None,
}
SESSION: "tidalapi.Session | None" = None
SESSION_LOCK = threading.Lock()
LAST_SCHEDULED_ATTEMPT = 0.0

logger = logging.getLogger("dw2tidal")


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


def setup_logging() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stdout is not None and not getattr(sys, "frozen", False):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(sh)
    logger.setLevel(logging.INFO)


def log(msg: str) -> None:
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    with LOCK:
        STATE["log"].append(f"{stamp}  {msg}")
        STATE["log"] = STATE["log"][-300:]
    logger.info(msg)


# ---------------------------------------------------------------- spotify

def playlist_id(url: str) -> str:
    """Accepts open.spotify.com/playlist/<id>, /embed/playlist/<id>, spotify:playlist:<id> or a bare id."""
    m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", url) or re.fullmatch(r"\s*([A-Za-z0-9]{22})\s*", url)
    if not m:
        raise ValueError("That doesn't look like a Spotify playlist link.")
    return m.group(1)


def parse_embed_page(html: str) -> list[dict]:
    """Parse the __NEXT_DATA__ blob of an embed page. Returns [{id, title, artist, isrc}]."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("Spotify's embed page has changed; track list not found.")
    data = json.loads(m.group(1))
    try:
        items = data["props"]["pageProps"]["state"]["data"]["entity"]["trackList"]
    except (KeyError, TypeError):
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


def fetch_discover_weekly(url: str) -> list[dict]:
    """Track list from the public embed page."""
    pid = playlist_id(url)
    r = requests.get(
        f"https://open.spotify.com/embed/playlist/{pid}",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return parse_embed_page(r.text)


def add_isrcs(tracks: list[dict], client_id: str, client_secret: str) -> None:
    """Fill in ISRC codes via Spotify's catalog API (client-credentials)."""
    tok = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=HTTP_TIMEOUT,
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
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        for t in r.json().get("tracks", []):
            if t and t["id"] in by_id:
                by_id[t["id"]]["isrc"] = (t.get("external_ids") or {}).get("isrc")
                by_id[t["id"]]["title"] = t["name"]
                by_id[t["id"]]["artist"] = t["artists"][0]["name"]


# ---------------------------------------------------------------- tidal

class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """tidalapi issues requests without a timeout; give every one a default."""

    def send(self, request, **kwargs):
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        return super().send(request, **kwargs)


def new_tidal_session() -> "tidalapi.Session":
    s = tidalapi.Session()
    s.request_session.mount("https://", _TimeoutAdapter())
    s.request_session.mount("http://", _TimeoutAdapter())
    return s


def save_tidal_session(s: "tidalapi.Session") -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    s.save_session_to_file(TIDAL_SESSION_FILE)
    try:
        TIDAL_SESSION_FILE.chmod(0o600)
    except OSError:
        pass


def tidal_session() -> "tidalapi.Session":
    global SESSION
    with SESSION_LOCK:
        if SESSION is None:
            SESSION = new_tidal_session()
            if TIDAL_SESSION_FILE.exists():
                try:
                    SESSION.load_session_from_file(TIDAL_SESSION_FILE)
                except Exception as e:
                    logger.warning("Could not load saved TIDAL session: %s", e)
    refresh_tidal_status()
    return SESSION


def ensure_tidal_login(s: "tidalapi.Session") -> bool:
    """True if the session works. Tries a token refresh first; on failure the user must reconnect."""
    try:
        if s.check_login():
            return True
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach TIDAL: {e}")
    if not s.refresh_token:
        return False
    try:
        log("TIDAL token expired; refreshing…")
        if s.token_refresh(s.refresh_token) and s.load_oauth_session(
            s.token_type, s.access_token, s.refresh_token, s.expiry_time, s.is_pkce
        ):
            save_tidal_session(s)
            return bool(s.check_login())
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach TIDAL: {e}")
    except Exception as e:
        logger.warning("TIDAL token refresh failed: %s", e)
    return False


def refresh_tidal_status() -> None:
    s = SESSION
    ok = False
    name = ""
    if s is not None:
        try:
            ok = ensure_tidal_login(s)
            if ok:
                name = getattr(s.user, "username", "") or getattr(s.user, "email", "") or ""
        except Exception as e:
            logger.warning("TIDAL status check failed: %s", e)
            ok = False
    with LOCK:
        STATE["tidal_connected"] = ok
        STATE["tidal_user"] = name


def start_tidal_login() -> None:
    def worker():
        global SESSION
        with LOCK:
            if STATE["tidal_login_url"]:
                return  # a login link is already waiting
        s = new_tidal_session()
        try:
            login, future = s.login_oauth()
            url = login.verification_uri_complete
            if not url.startswith("http"):
                url = "https://" + url
            with LOCK:
                STATE["tidal_login_url"] = url
            log("Open the TIDAL link on the page and approve this app.")
            future.result(timeout=login.expires_in + 10)
            with SESSION_LOCK:
                SESSION = s
            save_tidal_session(s)
            refresh_tidal_status()
            log("TIDAL connected.")
        except Exception as e:
            log(f"TIDAL login failed: {e}")
        finally:
            with LOCK:
                STATE["tidal_login_url"] = None

    threading.Thread(target=worker, daemon=True).start()


_FEAT = r"(?:feat\.?|ft\.?|featuring|with)\s"
_SUFFIX_WORDS = r"(?:remaster(?:ed)?|remix|mix|edit|version|live|mono|stereo|deluxe|bonus|acoustic|instrumental|demo|single|explicit|clean|radio|edition|anniversary|re-?recorded|sped up|slowed)"


def clean_title(title: str) -> str:
    """Strip '(feat. X)', '- 2011 Remaster', '[Live]' style decorations for a looser search."""
    t = title
    t = re.sub(r"\s*[\(\[]" + _FEAT + r"[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s*[\(\[][^\)\]]*\b" + _SUFFIX_WORDS + r"\b[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s+[-–—]\s+" + _FEAT + r".*$", "", t, flags=re.I)
    t = re.sub(r"\s+[-–—]\s+[^-–—]*\b" + _SUFFIX_WORDS + r"\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—")
    return t or title.strip()


def clean_artist(artist: str) -> str:
    """'A feat. B' / 'A & B' → 'A'."""
    a = re.split(r"\s+" + _FEAT + r"|\s*[,&]\s*|\s+x\s+", artist, maxsplit=1, flags=re.I)[0]
    return a.strip() or artist.strip()


def norm(s: str) -> str:
    s = s.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def _track_artists(tr) -> list[str]:
    names = [getattr(a, "name", "") for a in (getattr(tr, "artists", None) or [])]
    if not names and getattr(tr, "artist", None):
        names = [tr.artist.name]
    return names


def _artist_matches(want: str, tr) -> bool:
    w = norm(clean_artist(want))
    if not w:
        return True
    for n in _track_artists(tr):
        n = norm(n)
        if n == w or w in n or n in w:
            return True
    return False


def _pick(results, artist: str, title: str):
    """Prefer a result whose artist matches and whose title matches (cleaned); else artist match; else None."""
    want_t = norm(clean_title(title))
    best = None
    for tr in results:
        if not _artist_matches(artist, tr):
            continue
        got_t = norm(clean_title(getattr(tr, "name", "") or ""))
        if got_t == want_t or (want_t and (want_t in got_t or got_t in want_t)):
            return tr
        best = best or tr
    return best


def match_on_tidal(s: "tidalapi.Session", tracks: list[dict]) -> tuple[list[int], list[str]]:
    """Returns (tidal_ids, missed). Logs how each track was matched."""
    ids, missed = [], []
    for t in tracks:
        hit, how = None, ""
        label = f'{t["artist"]} – {t["title"]}'
        if t.get("isrc"):
            try:
                r = s.get_tracks_by_isrc(t["isrc"])
                if r:
                    hit, how = r[0], "isrc"
            except Exception as e:
                logger.info("ISRC lookup failed for %s: %s", label, e)
        if hit is None and (t["artist"] or t["title"]):
            queries = [("search", f'{t["artist"]} {t["title"]}')]
            ct, ca = clean_title(t["title"]), clean_artist(t["artist"])
            if (ct, ca) != (t["title"], t["artist"]):
                queries.append(("search-cleaned", f"{ca} {ct}"))
            fallback = None
            for method, q in queries:
                try:
                    r = s.search(q, models=[tidalapi.Track], limit=10)["tracks"]
                except Exception as e:
                    logger.info("TIDAL search failed for %r: %s", q, e)
                    continue
                if not r:
                    continue
                fallback = fallback or (r[0], method + "-first")
                picked = _pick(r, t["artist"], t["title"])
                if picked is not None:
                    hit, how = picked, method
                    break
            if hit is None and fallback:
                hit, how = fallback
        if hit:
            ids.append(hit.id)
            log(f"   ✓ {label}  →  {', '.join(_track_artists(hit))} – {hit.name}  [{how}]")
        else:
            log(f"   ✗ {label}  (no match)")
            missed.append(label)
    return ids, missed


def list_folders(s: "tidalapi.Session") -> list[dict]:
    """[{id, name, trn}] of the user's top-level playlist folders (v2 collection API)."""
    # The endpoint ignores `offset` when filtered to folders, so ask for one big page.
    r = s.request.request(
        "GET", "my-collection/playlists/folders",
        params={"folderId": "root", "includeOnly": "FOLDER", "limit": 50},
        base_url=s.config.api_v2_location,
    )
    out = []
    for it in r.json().get("items") or []:
        fid = (it.get("data") or {}).get("id")
        if it.get("itemType") == "FOLDER" and fid:
            out.append({"id": fid, "name": it.get("name", ""), "trn": it.get("trn")})
    return out


def get_or_create_folder(s: "tidalapi.Session", name: str):
    """Returns a tidalapi Folder (or None if name is blank). Matches existing folders case-insensitively."""
    name = (name or "").strip()
    if not name:
        return None
    for f in list_folders(s):
        if f["name"].strip().lower() == name.lower():
            return s.folder(f["id"])
    log(f"Creating TIDAL folder '{name}'.")
    return s.user.create_folder(name)


def tidy_into_folder(s: "tidalapi.Session", folder, playlists) -> None:
    """Move earlier 'Discover Weekly …' playlists that sit at the root into the folder."""
    stray = [p for p in playlists if p.name.startswith(PLAYLIST_PREFIX) and not getattr(p, "parent_folder_id", None)]
    if not stray:
        return
    try:
        folder.add_items([p.trn for p in stray])
        log(f"Moved {len(stray)} earlier playlist(s) into '{folder.name}'.")
    except Exception as e:
        logger.warning("Could not move playlists into folder: %s", e)


# ---------------------------------------------------------------- the job

def run_job(trigger: str) -> None:
    with LOCK:
        if STATE["running"]:
            return
        STATE["running"] = True
    ok = False
    try:
        cfg = load_config()
        if not cfg["dw_url"]:
            log("No Discover Weekly link saved yet.")
            return
        s = tidal_session()
        if not STATE["tidal_connected"]:
            log("TIDAL isn't connected (or the login expired). Press 'Connect TIDAL' first.")
            return

        name = f"{PLAYLIST_PREFIX}{datetime.date.today():%Y-%m-%d}"
        existing = s.user.playlists()
        folder = None
        try:
            folder = get_or_create_folder(s, cfg.get("folder_name", ""))
        except Exception as e:
            log(f"Couldn't open the TIDAL folder ({e}); playlist will go to the top level.")
        if any(p.name == name for p in existing):
            log(f"'{name}' already exists in TIDAL. Nothing to do.")
            if folder is not None:
                tidy_into_folder(s, folder, existing)
            STATE["last_run_date"] = str(datetime.date.today())
            save_state()
            ok = True
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

        pl = s.user.create_playlist(name, "Mirrored from Spotify Discover Weekly",
                                    parent_id=folder.id if folder is not None else "root")
        pl.add([str(i) for i in tidal_ids])
        if folder is not None:
            tidy_into_folder(s, folder, existing)
        where = f" in folder '{folder.name}'" if folder is not None else ""
        result = f"Created '{name}'{where} with {len(tidal_ids)} of {len(tracks)} tracks."
        log(result)
        if missed:
            log("Not found on TIDAL: " + "; ".join(missed))
        STATE["last_run_date"] = str(datetime.date.today())
        STATE["last_result"] = result
        save_state()
        ok = True
        try:
            save_tidal_session(s)  # persist any refreshed token
        except Exception as e:
            logger.warning("Could not save TIDAL session: %s", e)
    except requests.exceptions.Timeout:
        log("Stopped: a network request timed out. Check your connection and try again.")
    except requests.exceptions.ConnectionError:
        log("Stopped: no network connection.")
    except Exception as e:
        logger.exception("Run failed")
        log(f"Stopped: {e}")
    finally:
        with LOCK:
            STATE["running"] = False
            STATE["last_run_ok"] = ok


def schedule_due(cfg: dict, now: datetime.datetime, last_run_date, last_attempt: float, mono: float) -> bool:
    """Due if it's the scheduled weekday, the scheduled hour has started or passed (catch-up after
    sleep), nothing ran today, and we haven't just failed."""
    return (
        bool(cfg.get("schedule_enabled"))
        and now.weekday() == int(cfg.get("weekday", 0))
        and now.hour >= int(cfg.get("hour", 8))
        and last_run_date != str(now.date())
        and (mono - last_attempt) >= RETRY_AFTER_FAILURE
    )


def scheduler() -> None:
    global LAST_SCHEDULED_ATTEMPT
    while True:
        try:
            cfg = load_config()
            now = datetime.datetime.now()
            with LOCK:
                running = STATE["running"]
                last = STATE["last_run_date"]
            if not running and schedule_due(cfg, now, last, LAST_SCHEDULED_ATTEMPT, time.monotonic()):
                LAST_SCHEDULED_ATTEMPT = time.monotonic()
                run_job("scheduled")
        except Exception as e:
            log(f"Scheduler error: {e}")
        time.sleep(30)


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
      <label for="folder">TIDAL folder for the weekly playlists</label>
      <input id="folder" type="text" placeholder="Weekly discoveries" autocomplete="off">
      <p class="note">Leave blank to put playlists at the top level.</p>

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
      <p class="note">Discover Weekly refreshes on Monday. The schedule only fires while this app is open; if the computer was asleep at that hour, it runs when it wakes later that day.</p>
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
  $("folder").value = c.folder_name; $("weekday").value = c.weekday; $("hour").value = c.hour; $("enabled").checked = c.schedule_enabled;
}
function gather() {
  return { dw_url: $("dw").value.trim(), spotify_client_id: $("cid").value.trim(), spotify_client_secret: $("csec").value.trim(), folder_name: $("folder").value.trim(),
           weekday: +$("weekday").value, hour: +$("hour").value, schedule_enabled: $("enabled").checked };
}
async function save(id) { const r = await api("/config", gather()); if (r.ok) flash(id); else alert(r.error || "Could not save."); }
$("save").onclick = () => save("savedmsg");
$("save2").onclick = () => save("savedmsg2");
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
        n = min(int(self.headers.get("Content-Length") or 0), 65536)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _local(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def do_GET(self):
        if not self._local():
            self._json({"error": "forbidden"}, 403)
            return
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
        if not self._local():
            self._json({"error": "forbidden"}, 403)
            return
        path = urlparse(self.path).path
        if path == "/config":
            cfg = load_config()
            incoming = self._body()
            for k, default in DEFAULT_CONFIG.items():
                if k in incoming and isinstance(incoming[k], type(default)):
                    cfg[k] = incoming[k]
            cfg["weekday"] = min(max(int(cfg["weekday"]), 0), 6)
            cfg["hour"] = min(max(int(cfg["hour"]), 0), 23)
            if cfg["dw_url"]:
                try:
                    playlist_id(cfg["dw_url"])
                except ValueError as e:
                    self._json({"ok": False, "error": str(e)}, 400)
                    return
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
    setup_logging()
    url = f"http://127.0.0.1:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # Already running (or port taken): just show the existing window.
        logger.info("Port %s busy; opening existing instance", PORT)
        webbrowser.open(url)
        return
    load_state()
    threading.Thread(target=tidal_session, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    logger.info("Discover Weekly → TIDAL is running at %s  (Ctrl-C to quit)", url)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
