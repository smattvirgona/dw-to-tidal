#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests==2.34.2",
#     "tidalapi==0.8.11",
# ]
# ///
"""
Discover Weekly → TIDAL  (local app)

Runs hidden on your own computer. Saves Spotify's Discover Weekly (weekly) and
TIDAL's My Daily Discovery mix (added daily to one playlist per week, named
'Week 38 (2026)') as TIDAL playlists, each in its own folder. The settings window is a small page at http://127.0.0.1:8765; closing
it leaves the app running in the background.

Install (once):
    pip3 install requests tidalapi

Run:
    python3 dw2tidal_app.py              # run and open the window
    python3 dw2tidal_app.py --background # run hidden
    python3 dw2tidal_app.py --show       # start hidden if needed (and at login), open the window

Settings and the TIDAL login are stored in ~/.dw2tidal/. Nothing leaves your
computer except the calls to Spotify and TIDAL.

Optional: a Spotify client ID/secret (developer.spotify.com/dashboard → Create
app) makes matching exact via ISRC codes. Without them the app matches by
artist + title, which is right most of the time.
"""

import argparse
import datetime
import json
import logging
import logging.handlers
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
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
    "daily_enabled": False,
    "daily_hour": 9,
    "daily_folder_name": "TIDAL Discovery",
    "autostart": True,
}
PLAYLIST_PREFIX = "Discover Weekly "
APP_LABEL = "com.dw2tidal.app"

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
    "last_daily_date": None,
    "last_daily_result": "",
}
SESSION: "tidalapi.Session | None" = None
SESSION_LOCK = threading.Lock()
LAST_SCHEDULED_ATTEMPT = 0.0
LAST_DAILY_ATTEMPT = 0.0

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
            STATE["last_daily_date"] = saved.get("last_daily_date")
            STATE["last_daily_result"] = saved.get("last_daily_result", "")
        except json.JSONDecodeError:
            pass


def save_state() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        k: STATE[k] for k in ("last_run_date", "last_result", "last_daily_date", "last_daily_result")
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


def tidy_into_folder(s: "tidalapi.Session", folder, playlists, prefix: str = PLAYLIST_PREFIX) -> None:
    """Move earlier '<prefix>…' playlists that sit at the root into the folder."""
    stray = [p for p in playlists if p.name.startswith(prefix) and not getattr(p, "parent_folder_id", None)]
    if not stray:
        return
    try:
        folder.add_items([p.trn for p in stray])
        log(f"Moved {len(stray)} earlier playlist(s) into '{folder.name}'.")
    except Exception as e:
        logger.warning("Could not move playlists into folder: %s", e)


# ---------------------------------------------------------------- the job

def _guarded(job) -> None:
    """Run one job at a time; turn network and other errors into log lines."""
    with LOCK:
        if STATE["running"]:
            log("Already working on something; try again in a moment.")
            return
        STATE["running"] = True
    ok = False
    try:
        ok = bool(job())
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


def _connected_session():
    s = tidal_session()
    if not STATE["tidal_connected"]:
        log("TIDAL isn't connected (or the login expired). Press 'Connect TIDAL' first.")
        return None
    return s


def _open_folder(s, name: str):
    try:
        return get_or_create_folder(s, name)
    except Exception as e:
        log(f"Couldn't open the TIDAL folder ({e}); playlist will go to the top level.")
        return None


def _finish(s, folder, existing, prefix: str, date_key: str, result_key: str, result: str | None) -> None:
    if folder is not None:
        tidy_into_folder(s, folder, existing, prefix)
    STATE[date_key] = str(datetime.date.today())
    if result:
        STATE[result_key] = result
    save_state()
    try:
        save_tidal_session(s)  # persist any refreshed token
    except Exception as e:
        logger.warning("Could not save TIDAL session: %s", e)


def run_job(trigger: str) -> None:
    """Copy Spotify Discover Weekly into a dated TIDAL playlist."""
    def job():
        cfg = load_config()
        if not cfg["dw_url"]:
            log("No Discover Weekly link saved yet.")
            return False
        s = _connected_session()
        if s is None:
            return False

        name = f"{PLAYLIST_PREFIX}{datetime.date.today():%Y-%m-%d}"
        existing = s.user.playlists()
        folder = _open_folder(s, cfg.get("folder_name", ""))
        if any(p.name == name for p in existing):
            log(f"'{name}' already exists in TIDAL. Nothing to do.")
            _finish(s, folder, existing, PLAYLIST_PREFIX, "last_run_date", "last_result", None)
            return True

        log(f"Reading Discover Weekly ({trigger})…")
        tracks = fetch_discover_weekly(cfg["dw_url"])
        if not tracks:
            log("Discover Weekly came back empty.")
            return False
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
            return False

        pl = s.user.create_playlist(name, "Mirrored from Spotify Discover Weekly",
                                    parent_id=folder.id if folder is not None else "root")
        pl.add([str(i) for i in tidal_ids])
        where = f" in folder '{folder.name}'" if folder is not None else ""
        result = f"Created '{name}'{where} with {len(tidal_ids)} of {len(tracks)} tracks."
        log(result)
        if missed:
            log("Not found on TIDAL: " + "; ".join(missed))
        _finish(s, folder, existing, PLAYLIST_PREFIX, "last_run_date", "last_result", result)
        return True

    _guarded(job)


def find_daily_discovery(s: "tidalapi.Session"):
    """The user's 'My Daily Discovery' mix, or None."""
    for m in s.mixes():
        if getattr(m, "mix_type", None) == tidalapi.mix.MixType.discovery:
            return m
    return None


def week_playlist_name(day: datetime.date) -> str:
    """'Week 38 (2026)': ISO week, Monday to Sunday."""
    year, week, _ = day.isocalendar()
    return f"Week {week:02d} ({year})"


def run_daily_job(trigger: str) -> None:
    """Add today's TIDAL 'My Daily Discovery' mix to this week's playlist (a new one each Monday).
    Tracks already in the playlist are skipped, so running twice in a day is harmless."""
    def job():
        cfg = load_config()
        s = _connected_session()
        if s is None:
            return False

        today = datetime.date.today()
        name = week_playlist_name(today)
        log(f"Reading TIDAL My Daily Discovery ({trigger})…")
        mix = find_daily_discovery(s)
        if mix is None:
            log("TIDAL didn't return a 'My Daily Discovery' mix for this account.")
            return False
        ids = [str(t.id) for t in mix.items() if isinstance(t, tidalapi.Track)]
        if not ids:
            log("My Daily Discovery came back empty.")
            return False

        folder = _open_folder(s, cfg.get("daily_folder_name", ""))
        match = next((p for p in s.user.playlists() if p.name == name), None)
        if match is None:
            monday = today - datetime.timedelta(days=today.weekday())
            sunday = monday + datetime.timedelta(days=6)
            log(f"Starting this week's playlist '{name}'.")
            desc = f"TIDAL My Daily Discovery, {monday.day} {monday:%b} – {sunday.day} {sunday:%b %Y}"
            pl = s.user.create_playlist(name, desc, parent_id=folder.id if folder is not None else "root")
        else:
            pl = s.playlist(match.id)  # fresh copy: add() needs the current track count and etag
        added = pl.add(ids)
        where = f" in folder '{folder.name}'" if folder is not None else ""
        result = f"Added {len(added)} new of {len(ids)} tracks from {today:%a %d %b} to '{name}'{where}."
        log(result)
        _finish(s, None, [], "", "last_daily_date", "last_daily_result", result)
        return True

    _guarded(job)


def daily_due(cfg: dict, now: datetime.datetime, last_daily_date, last_attempt: float, mono: float) -> bool:
    return (
        bool(cfg.get("daily_enabled"))
        and now.hour >= int(cfg.get("daily_hour", 9))
        and last_daily_date != str(now.date())
        and (mono - last_attempt) >= RETRY_AFTER_FAILURE
    )


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
    global LAST_SCHEDULED_ATTEMPT, LAST_DAILY_ATTEMPT
    while True:
        try:
            cfg = load_config()
            now = datetime.datetime.now()
            with LOCK:
                running = STATE["running"]
                last = STATE["last_run_date"]
                last_daily = STATE["last_daily_date"]
            if not running and schedule_due(cfg, now, last, LAST_SCHEDULED_ATTEMPT, time.monotonic()):
                LAST_SCHEDULED_ATTEMPT = time.monotonic()
                run_job("scheduled")
            elif not running and daily_due(cfg, now, last_daily, LAST_DAILY_ATTEMPT, time.monotonic()):
                LAST_DAILY_ATTEMPT = time.monotonic()
                run_daily_job("scheduled")
        except Exception as e:
            log(f"Scheduler error: {e}")
        time.sleep(30)


# ---------------------------------------------------------------- background + window

LAUNCH_AGENT = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{APP_LABEL}.plist"
WIN_STARTUP = pathlib.Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
WIN_STARTUP_FILE = WIN_STARTUP / "DW to TIDAL.vbs"


def background_command() -> list[str]:
    """How to start this app hidden: through the launcher's uv if it started us, else this Python."""
    script = str(pathlib.Path(__file__).resolve())
    uv = os.environ.get("DW2TIDAL_UV")
    if uv:
        return [uv, "run", "--script", script, "--background"]
    return [sys.executable, script, "--background"]


def _uv_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if (k.startswith("UV_") or k == "DW2TIDAL_UV") and k != "UV_RUN_RECURSION_DEPTH"}


def autostart_supported() -> bool:
    return sys.platform in ("darwin", "win32")


def set_autostart(enabled: bool) -> None:
    """Start hidden at login (macOS LaunchAgent / Windows Startup folder). Never stops a running instance."""
    try:
        if sys.platform == "darwin":
            if not enabled:
                LAUNCH_AGENT.unlink(missing_ok=True)
                return
            LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
            LAUNCH_AGENT.write_bytes(plistlib.dumps({
                "Label": APP_LABEL,
                "ProgramArguments": background_command(),
                "EnvironmentVariables": _uv_env(),
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after Quit
                "StandardOutPath": str(CONFIG_DIR / "background.log"),
                "StandardErrorPath": str(CONFIG_DIR / "background.log"),
            }))
        elif sys.platform == "win32":
            if not enabled:
                WIN_STARTUP_FILE.unlink(missing_ok=True)
                return
            q = lambda s: '"' + s.replace('"', '""') + '"'  # VBScript string literal
            cmd = " ".join('""' + a.replace('"', '') + '""' for a in background_command())
            lines = ['Set sh = CreateObject("WScript.Shell")', 'Set env = sh.Environment("PROCESS")']
            lines += [f"env({q(k)}) = {q(v)}" for k, v in _uv_env().items()]
            lines.append(f'sh.Run "{cmd}", 0, False')
            WIN_STARTUP.mkdir(parents=True, exist_ok=True)
            WIN_STARTUP_FILE.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    except Exception as e:
        log(f"Couldn't change the start-at-login setting: {e}")


def is_running() -> bool:
    try:
        return requests.get(f"http://127.0.0.1:{PORT}/status", timeout=2).ok
    except requests.RequestException:
        return False


def start_background() -> None:
    if sys.platform == "darwin" and LAUNCH_AGENT.exists():
        domain = f"gui/{os.getuid()}"
        loaded = subprocess.run(["launchctl", "print", f"{domain}/{APP_LABEL}"], capture_output=True).returncode == 0
        if loaded:
            subprocess.run(["launchctl", "kickstart", f"{domain}/{APP_LABEL}"], capture_output=True)
        else:
            subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCH_AGENT)], capture_output=True)
        return
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(background_command(), **kwargs)


def open_window(url: str) -> None:
    """A chromeless app window if Chrome/Edge/Brave is installed, else the default browser."""
    args = [f"--app={url}", "--window-size=620,700"]
    try:
        if sys.platform == "darwin":
            for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
                if pathlib.Path(f"/Applications/{app}.app").exists():
                    subprocess.Popen(["open", "-na", app, "--args", *args])
                    return
        elif sys.platform == "win32":
            for base in (os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("PROGRAMFILES", ""), os.environ.get("LOCALAPPDATA", "")):
                for exe in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
                    p = pathlib.Path(base) / exe
                    if base and p.exists():
                        subprocess.Popen([str(p), *args])
                        return
        else:
            for exe in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
                if shutil.which(exe):
                    subprocess.Popen([exe, *args])
                    return
    except OSError as e:
        logger.info("App window failed (%s); using default browser", e)
    webbrowser.open(url)


def show() -> None:
    """Launcher entry: make sure the background app is running (and starts at login), then open the window."""
    setup_logging()
    if autostart_supported():
        set_autostart(bool(load_config().get("autostart", True)))
    if not is_running():
        print("Starting DW to TIDAL in the background…")
        start_background()
        for _ in range(180):
            if is_running():
                break
            time.sleep(0.5)
        else:
            sys.exit(f"DW to TIDAL didn't start. See {LOG_FILE} and {CONFIG_DIR / 'background.log'}.")
    open_window(f"http://127.0.0.1:{PORT}")


# ---------------------------------------------------------------- web ui

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DW to TIDAL</title>
<style>
  :root {
    --desk: #008080; --face: #c0c0c0; --hi: #ffffff; --lite: #dfdfdf; --shade: #808080; --dark: #0a0a0a;
    --title: #000080; --title-ink: #ffffff; --ink: #000000; --field: #ffffff; --sel: #000080;
    --raised: inset -1px -1px var(--dark), inset 1px 1px var(--lite), inset -2px -2px var(--shade), inset 2px 2px var(--hi);
    --button: inset -1px -1px var(--dark), inset 1px 1px var(--hi), inset -2px -2px var(--shade), inset 2px 2px var(--lite);
    --pressed: inset -1px -1px var(--hi), inset 1px 1px var(--dark), inset -2px -2px var(--lite), inset 2px 2px var(--shade);
    --sunken: inset -1px -1px var(--hi), inset 1px 1px var(--shade), inset -2px -2px var(--lite), inset 2px 2px var(--dark);
    --thin: inset -1px -1px var(--hi), inset 1px 1px var(--shade);
    color-scheme: light;
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--desk); color: var(--ink);
    font: 12px/1.35 "Pixelated MS Sans Serif", "MS Sans Serif", "Microsoft Sans Serif", Tahoma, Geneva, Verdana, sans-serif;
    -webkit-font-smoothing: none; font-smooth: never;
    padding: 16px 16px 48px;
  }
  button, input, select { font: inherit; color: inherit; }

  /* window */
  .window { background: var(--face); box-shadow: var(--raised); padding: 3px; width: 100%; max-width: 560px; margin: 0 auto; }
  .titlebar { background: var(--title); color: var(--title-ink); display: flex; align-items: center; gap: 4px; padding: 2px 2px 2px 3px; font-weight: bold; user-select: none; }
  .titlebar .name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .titlebar svg { flex: none; }
  .tb { width: 16px; height: 14px; padding: 0; border: 0; background: var(--face); box-shadow: var(--button); display: grid; place-items: center; cursor: default; }
  .tb:active { box-shadow: var(--pressed); }
  .tb + .tb { margin-left: 2px; }
  .body { padding: 8px 6px 6px; }

  /* tabs */
  .tabs { display: flex; padding-left: 2px; position: relative; z-index: 1; }
  .tab { border: 0; background: var(--face); padding: 3px 10px 2px; margin-top: 2px; cursor: default;
         box-shadow: inset 1px 1px var(--hi), inset -1px 0 var(--dark), inset -2px 0 var(--shade), inset 0 2px var(--lite); border-radius: 3px 3px 0 0; }
  .tab[aria-selected="true"] { margin: 0 -2px -1px; padding: 4px 12px 5px; position: relative; z-index: 2; }
  .tab:focus-visible span { outline: 1px dotted var(--ink); }
  .sheet { box-shadow: var(--raised); padding: 12px 10px 10px; min-height: 360px; }
  [role="tabpanel"][hidden] { display: none; }

  /* groups and controls */
  fieldset { margin: 0 0 12px; padding: 8px 10px 10px; border: 1px solid var(--shade); box-shadow: 1px 1px var(--hi), inset 1px 1px var(--hi); }
  legend { padding: 0 3px; margin-left: -2px; }
  p { margin: 0 0 8px; }
  .note { color: #333; }
  label { cursor: default; }
  .row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin: 6px 0; }
  .field-label { display: block; margin: 8px 0 3px; }

  input[type="text"], input[type="password"], select {
    background-color: var(--field); border: 0; box-shadow: var(--sunken); padding: 3px 4px; height: 22px; border-radius: 0; outline: none;
  }
  input[type="text"], input[type="password"] { width: 100%; }
  input[type="text"]:focus, input[type="password"]:focus { background-color: #ffffe8; }
  select { appearance: none; -webkit-appearance: none; padding-right: 20px; min-width: 76px;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='18' shape-rendering='crispEdges'%3E%3Cpath fill='%23c0c0c0' d='M0 0h16v18H0z'/%3E%3Cpath fill='%23fff' d='M1 1h13v1H1zM1 1h1v15H1z'/%3E%3Cpath fill='%230a0a0a' d='M15 0h1v18h-1zM0 17h16v1H0z'/%3E%3Cpath fill='%23808080' d='M14 1h1v16h-1zM1 16h14v1H1z'/%3E%3Cpath d='M4 7h7v1H4zM5 8h5v1H5zM6 9h3v1H6zM7 10h1v1H7z'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 2px center; }
  select:focus { outline: 1px dotted var(--ink); outline-offset: -4px; }
  :disabled, .dim { color: var(--shade) !important; text-shadow: 1px 1px var(--hi); }
  input:disabled, select:disabled { background-color: var(--face); }

  input[type="checkbox"] { appearance: none; -webkit-appearance: none; width: 13px; height: 13px; margin: 0 4px 0 0; flex: none;
    background: var(--field); box-shadow: var(--sunken); vertical-align: -2px; }
  input[type="checkbox"]:checked { background: var(--field) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='7' height='7' shape-rendering='crispEdges'%3E%3Cpath d='M6 0h1v3H6zM5 1h1v3H5zM4 2h1v3H4zM3 3h1v3H3zM2 4h1v3H2zM1 3h1v3H1zM0 2h1v3H0z'/%3E%3C/svg%3E") no-repeat 3px 3px; }
  input[type="checkbox"]:focus-visible + label { outline: 1px dotted var(--ink); }
  .check { display: flex; align-items: flex-start; margin: 6px 0; }

  .btn { min-width: 75px; min-height: 23px; padding: 3px 10px; border: 0; background: var(--face); box-shadow: var(--button); cursor: default; }
  .btn:active:not(:disabled), .btn.down { box-shadow: var(--pressed); padding: 4px 9px 2px 11px; }
  .btn:focus-visible { outline: 1px dotted var(--ink); outline-offset: -5px; }
  .btn.default { box-shadow: inset -1px -1px var(--dark), inset 1px 1px var(--dark), inset -2px -2px var(--dark), inset 2px 2px var(--hi), inset -3px -3px var(--shade), inset 3px 3px var(--lite); }
  a.btn { display: inline-block; color: var(--ink); text-decoration: none; }
  .buttons { display: flex; gap: 6px; justify-content: flex-end; flex-wrap: wrap; margin-top: 10px; }
  .linkbtn { border: 0; background: none; padding: 0; color: #0000ee; text-decoration: underline; cursor: pointer; }

  .status { display: flex; align-items: center; gap: 8px; margin: 2px 0 8px; }
  .login { background: #ffffe1; box-shadow: var(--thin); padding: 8px; margin: 8px 0 0; }
  .login a { color: #0000ee; word-break: break-all; }
  .result { color: #333; margin: 8px 0 0; min-height: 16px; }

  pre#log { margin: 0; height: 290px; overflow: auto; background: var(--field); box-shadow: var(--sunken); padding: 6px;
    font: 12px/1.35 "Fixedsys", "Lucida Console", Menlo, Consolas, monospace; white-space: pre-wrap; word-break: break-word; }

  .statusbar { display: flex; gap: 2px; margin-top: 4px; }
  .cell { box-shadow: var(--thin); padding: 2px 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 0; }
  .cell.grow { flex: 2; }
  .progress { height: 14px; box-shadow: var(--thin); padding: 2px; flex: 1; min-width: 60px; }
  .progress i { display: block; height: 100%; width: 100%;
    background: repeating-linear-gradient(90deg, var(--sel) 0 8px, transparent 8px 10px); background-size: 200% 100%;
    animation: march 1.2s steps(10) infinite; }
  @keyframes march { to { background-position: -100px 0; } }

  /* taskbar + start menu */
  .taskbar { position: fixed; left: 0; right: 0; bottom: 0; height: 30px; background: var(--face); box-shadow: inset 0 1px var(--lite), inset 0 2px var(--hi);
    display: flex; align-items: center; gap: 4px; padding: 2px 2px 0; z-index: 10; }
  .start { font-weight: bold; min-width: 0; padding: 2px 6px; display: flex; gap: 4px; align-items: center; }
  .task { flex: 0 1 160px; box-shadow: var(--pressed); padding: 3px 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    background: repeating-conic-gradient(var(--face) 0 25%, var(--hi) 0 50%) 0 0 / 2px 2px; font-weight: bold; }
  .spacer { flex: 1; }
  .tray { box-shadow: var(--thin); padding: 4px 10px; white-space: nowrap; }
  .menu { position: fixed; left: 2px; bottom: 30px; background: var(--face); box-shadow: var(--raised); padding: 3px 3px 3px 26px; min-width: 220px; z-index: 11; }
  .menu::before { content: "DW to TIDAL"; position: absolute; left: 3px; top: 3px; bottom: 3px; width: 21px; background: var(--shade); color: var(--face);
    writing-mode: vertical-rl; transform: rotate(180deg); font-weight: bold; font-size: 15px; padding-top: 6px; text-align: left; }
  .menu button { display: flex; align-items: center; gap: 8px; width: 100%; border: 0; background: none; text-align: left; padding: 6px 8px; cursor: default; }
  .menu button:hover, .menu button:focus-visible { background: var(--sel); color: var(--hi); outline: none; }
  .menu hr { border: 0; border-top: 1px solid var(--shade); border-bottom: 1px solid var(--hi); margin: 3px 2px; }

  /* message box */
  .overlay { position: fixed; inset: 0; display: grid; place-items: center; padding: 16px; z-index: 20; }
  .msg { max-width: 380px; width: 100%; }
  .msg .content { display: flex; gap: 12px; align-items: flex-start; padding: 12px 8px 4px; }
  .msg .content svg, .status svg { flex: none; }
  .msg .buttons { justify-content: center; margin: 12px 0 6px; }

  .safe { position: fixed; inset: 0; background: #000; color: #ff8000; display: grid; place-items: center; text-align: center;
    font: bold 22px/1.5 "Fixedsys", "Lucida Console", Menlo, monospace; padding: 16px; z-index: 30; }

  @media (max-width: 440px) { .tab { padding: 3px 6px 2px; } .cell.hide-narrow { display: none; } }
</style>
</head>
<body>

<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <defs>
    <symbol id="i-disc" viewBox="0 0 16 16" shape-rendering="crispEdges"><circle cx="8" cy="8" r="7" fill="#c0c0c0" stroke="#000"/><circle cx="8" cy="8" r="5" fill="#dfdfdf"/><path d="M4 6h3v1H4zM3 8h2v1H3z" fill="#fff"/><circle cx="8" cy="8" r="2" fill="#fff" stroke="#000"/></symbol>
    <symbol id="i-ok" viewBox="0 0 32 32" shape-rendering="crispEdges"><circle cx="16" cy="16" r="14" fill="#008000" stroke="#000"/><path d="M9 16l5 5 9-10" fill="none" stroke="#fff" stroke-width="3"/></symbol>
    <symbol id="i-err" viewBox="0 0 32 32" shape-rendering="crispEdges"><circle cx="16" cy="16" r="14" fill="#ff0000" stroke="#000"/><path d="M10 10l12 12M22 10L10 22" stroke="#fff" stroke-width="3"/></symbol>
    <symbol id="i-info" viewBox="0 0 32 32" shape-rendering="crispEdges"><circle cx="16" cy="16" r="14" fill="#fff" stroke="#000"/><path d="M14 7h4v4h-4zM14 13h4v12h-4z" fill="#000080"/></symbol>
    <symbol id="i-q" viewBox="0 0 32 32" shape-rendering="crispEdges"><circle cx="16" cy="16" r="14" fill="#fff" stroke="#000"/><path d="M11 12c0-3 2-5 5-5s5 2 5 5-3 4-3 6v1h-4v-2c0-3 3-3 3-5 0-1-1-2-1-2s-2 1-2 2z M14 21h4v4h-4z" fill="#000080"/></symbol>
    <symbol id="i-note" viewBox="0 0 16 16" shape-rendering="crispEdges"><path d="M6 2h8v2H8v8a3 3 0 1 1-2-2.8z" fill="#000"/><path d="M6 2h8v2H6z" fill="#000080"/></symbol>
  </defs>
</svg>

<div class="window" role="application" aria-labelledby="wtitle">
  <div class="titlebar">
    <svg width="16" height="16"><use href="#i-disc"/></svg>
    <span class="name" id="wtitle">DW to TIDAL</span>
    <button class="tb" id="help" aria-label="Help"><svg width="8" height="9" shape-rendering="crispEdges"><path d="M1 1h1v2H1zM2 0h4v1H2zM6 1h1v2H6zM5 3h1v1H5zM4 4h1v2H3V4zM3 7h2v2H3z"/></svg></button>
    <button class="tb" id="close-x" aria-label="Close window"><svg width="8" height="7" shape-rendering="crispEdges"><path d="M0 0h2v1h1v1h2V1h1V0h2v1H7v1H6v1H5v1h1v1h1v1h1v1H6V6H5V5H3v1H2v1H0V6h1V5h1V4h1V3H2V2H1V1H0z"/></svg></button>
  </div>

  <div class="body">
    <div class="tabs" role="tablist" aria-label="Sections">
      <button class="tab" role="tab" id="t-setup" aria-controls="p-setup" aria-selected="true"><span>Setup</span></button>
      <button class="tab" role="tab" id="t-lists" aria-controls="p-lists" aria-selected="false" tabindex="-1"><span>Playlists</span></button>
      <button class="tab" role="tab" id="t-log" aria-controls="p-log" aria-selected="false" tabindex="-1"><span>Log</span></button>
    </div>
    <div class="sheet">

      <section role="tabpanel" id="p-setup" aria-labelledby="t-setup">
        <fieldset>
          <legend>Step 1: Connect TIDAL</legend>
          <div class="status"><svg width="24" height="24" id="tidal-icon"><use href="#i-info"/></svg><span id="tidal-status">Checking…</span></div>
          <div class="row"><button class="btn" id="connect">Connect TIDAL…</button></div>
          <div class="login" id="login-box" hidden>
            <p>Open this link, sign in, and press <b>Yes, continue</b>. This window updates by itself.</p>
            <a id="login-link" href="#" target="_blank" rel="noopener"></a>
          </div>
        </fieldset>

        <fieldset>
          <legend>Step 2: Your Spotify Discover Weekly link</legend>
          <input id="dw" type="text" placeholder="https://open.spotify.com/playlist/37i9dQZEVX…" autocomplete="off" aria-label="Discover Weekly link">
          <p class="note" style="margin:6px 0 0">In Spotify: Discover Weekly → ⋯ → Share → Copy link. Skip this if you only want TIDAL's daily mix.</p>
        </fieldset>

        <fieldset>
          <legend>Step 3: Pick your playlists</legend>
          <p>Choose what to save on the <button class="linkbtn" data-goto="t-lists">Playlists</button> tab.</p>
          <div class="check" id="autostart-row"><input type="checkbox" id="autostart"><label for="autostart">Run hidden in the background, and start when I log in</label></div>
          <p class="note" style="margin:0">You can close this window. Playlists keep being made.</p>
        </fieldset>

        <div class="row"><button class="btn" id="adv-toggle" aria-expanded="false">Advanced…</button></div>
        <fieldset id="adv" hidden>
          <legend>Spotify developer key (optional)</legend>
          <p class="note">Lets the app match Discover Weekly by ISRC code instead of artist + title. From developer.spotify.com/dashboard → Create app.</p>
          <label class="field-label" for="cid">Client ID:</label>
          <input id="cid" type="text" autocomplete="off">
          <label class="field-label" for="csec">Client secret:</label>
          <input id="csec" type="password" autocomplete="off">
        </fieldset>
      </section>

      <section role="tabpanel" id="p-lists" aria-labelledby="t-lists" hidden>
        <fieldset>
          <legend>Spotify Discover Weekly</legend>
          <div class="check"><input type="checkbox" id="enabled"><label for="enabled">Copy it to TIDAL every week</label></div>
          <div class="row" data-dep="enabled">
            <label for="weekday">On</label><select id="weekday"></select>
            <label for="hour">at</label><select id="hour"></select>
          </div>
          <label class="field-label" for="folder">Put them in this TIDAL folder:</label>
          <input id="folder" type="text" placeholder="(top level)" autocomplete="off">
          <p class="result" id="last-weekly"></p>
          <div class="buttons"><button class="btn run" id="run">Copy now</button></div>
        </fieldset>

        <fieldset>
          <legend>TIDAL My Daily Discovery</legend>
          <div class="check"><input type="checkbox" id="daily"><label for="daily">Add each day's mix to a playlist for the week</label></div>
          <div class="row" data-dep="daily">
            <label for="dhour">At</label><select id="dhour"></select>
          </div>
          <label class="field-label" for="dfolder">Put them in this TIDAL folder:</label>
          <input id="dfolder" type="text" placeholder="(top level)" autocomplete="off">
          <p class="note" style="margin:6px 0 0">One playlist per week, Monday to Sunday, named like “Week 38 (2026)”.</p>
          <p class="result" id="last-daily"></p>
          <div class="buttons"><button class="btn run" id="run-daily">Add today's mix now</button></div>
        </fieldset>
        <p class="note">If the computer was asleep at that time, it catches up when it wakes the same day.</p>
      </section>

      <section role="tabpanel" id="p-log" aria-labelledby="t-log" hidden>
        <pre id="log" tabindex="0" aria-live="polite">Nothing yet.</pre>
      </section>
    </div>

    <div class="buttons"><button class="btn default" id="close">Close</button></div>
  </div>

  <div class="statusbar" role="status">
    <div class="cell grow" id="sb-main">Ready</div>
    <div class="cell hide-narrow" id="sb-weekly"></div>
    <div class="cell hide-narrow" id="sb-daily"></div>
  </div>
</div>

<div class="taskbar">
  <button class="btn start" id="start" aria-haspopup="menu" aria-expanded="false"><svg width="16" height="16"><use href="#i-note"/></svg>Start</button>
  <div class="task">DW to TIDAL</div>
  <div class="spacer"></div>
  <div class="tray" id="clock"></div>
</div>
<div class="menu" id="menu" role="menu" hidden>
  <button role="menuitem" data-act="run"><svg width="16" height="16"><use href="#i-disc"/></svg>Copy Discover Weekly now</button>
  <button role="menuitem" data-act="run-daily"><svg width="16" height="16"><use href="#i-disc"/></svg>Add today's Daily Discovery now</button>
  <button role="menuitem" data-act="log"><svg width="16" height="16"><use href="#i-note"/></svg>Show log</button>
  <hr>
  <button role="menuitem" data-act="quit"><svg width="16" height="16"><use href="#i-err"/></svg>Shut Down DW to TIDAL…</button>
</div>

<div class="overlay" id="overlay" hidden>
  <div class="window msg" role="alertdialog" aria-modal="true" aria-labelledby="msg-title" aria-describedby="msg-text">
    <div class="titlebar"><span class="name" id="msg-title">DW to TIDAL</span></div>
    <div class="content"><svg width="32" height="32" id="msg-icon"><use href="#i-info"/></svg><p id="msg-text"></p></div>
    <div class="buttons" id="msg-buttons"></div>
  </div>
</div>

<div class="safe" id="safe" hidden><div>It's now safe to close this window.<br><small style="font-size:14px;color:#aaa">Open DW to TIDAL again to restart it.</small></div></div>

<script>
const $ = id => document.getElementById(id);
const DAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
const hh = h => String(h).padStart(2, "0") + ":00";
DAYS.forEach((d, i) => $("weekday").add(new Option(d, i)));
for (let h = 0; h < 24; h++) { $("hour").add(new Option(hh(h), h)); $("dhour").add(new Option(hh(h), h)); }

async function api(path, body) {
  const r = await fetch(path, body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {});
  return r.json();
}

/* message box */
function msgbox(text, {icon = "info", buttons = ["OK"], title = "DW to TIDAL"} = {}) {
  return new Promise(resolve => {
    $("msg-title").textContent = title; $("msg-text").textContent = text;
    $("msg-icon").innerHTML = `<use href="#i-${icon}"/>`;
    const box = $("msg-buttons"); box.innerHTML = "";
    buttons.forEach((b, i) => {
      const el = document.createElement("button"); el.className = "btn" + (i === 0 ? " default" : ""); el.textContent = b;
      el.onclick = () => { $("overlay").hidden = true; resolve(b); };
      box.append(el);
    });
    $("overlay").hidden = false; box.firstChild.focus();
  });
}
document.addEventListener("keydown", e => { if (e.key === "Escape") { if (!$("overlay").hidden) $("msg-buttons").lastChild.click(); closeMenu(); } });

/* tabs */
const tabs = [...document.querySelectorAll(".tab")];
function selectTab(tab) {
  tabs.forEach(t => { const on = t === tab; t.setAttribute("aria-selected", on); t.tabIndex = on ? 0 : -1; $(t.getAttribute("aria-controls")).hidden = !on; });
  tab.focus();
}
tabs.forEach((t, i) => {
  t.onclick = () => selectTab(t);
  t.onkeydown = e => { if (e.key === "ArrowRight" || e.key === "ArrowLeft") selectTab(tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length]); };
});
document.querySelectorAll("[data-goto]").forEach(b => b.onclick = () => selectTab($(b.dataset.goto)));

/* settings, saved as you go */
const F = {dw_url: "dw", spotify_client_id: "cid", spotify_client_secret: "csec", folder_name: "folder", daily_folder_name: "dfolder",
           weekday: "weekday", hour: "hour", daily_hour: "dhour", schedule_enabled: "enabled", daily_enabled: "daily", autostart: "autostart"};
let saveTimer = null, loaded = false;
function gather() {
  const out = {};
  for (const [k, id] of Object.entries(F)) {
    const el = $(id);
    out[k] = el.type === "checkbox" ? el.checked : el.tagName === "SELECT" ? +el.value : el.value.trim();
  }
  return out;
}
function syncDisabled() {
  document.querySelectorAll("[data-dep]").forEach(row => {
    const on = $(row.dataset.dep).checked;
    row.querySelectorAll("select").forEach(s => s.disabled = !on);
    row.querySelectorAll("label").forEach(l => l.classList.toggle("dim", !on));
  });
  renderSchedule();
}
async function saveNow() {
  clearTimeout(saveTimer);
  if (!loaded) return;
  try {
    const r = await api("/config", gather());
    if (r.ok) flash("Settings saved."); else msgbox(r.error || "Couldn't save.", {icon: "err"});
  } catch { flash("Couldn't reach DW to TIDAL."); }
}
for (const id of Object.values(F)) {
  const el = $(id);
  if (el.type === "text" || el.type === "password") { el.addEventListener("input", () => { clearTimeout(saveTimer); saveTimer = setTimeout(saveNow, 700); }); el.addEventListener("blur", () => saveTimer && saveNow()); }
  else el.addEventListener("change", () => { syncDisabled(); saveNow(); });
}
async function loadConfig() {
  const c = await api("/config");
  for (const [k, id] of Object.entries(F)) { const el = $(id); if (el.type === "checkbox") el.checked = !!c[k]; else el.value = c[k] ?? ""; }
  if (c.spotify_client_id) toggleAdvanced(true);
  if (!c.autostart_supported) $("autostart-row").hidden = true;
  loaded = true; syncDisabled();
}
function toggleAdvanced(force) {
  const open = force ?? $("adv").hidden;
  $("adv").hidden = !open; $("adv-toggle").setAttribute("aria-expanded", open); $("adv-toggle").textContent = open ? "Advanced «" : "Advanced…";
}
$("adv-toggle").onclick = () => toggleAdvanced();

let flashTimer = null, lastStatus = null;
function flash(text) { $("sb-main").textContent = text; clearTimeout(flashTimer); flashTimer = setTimeout(() => { flashTimer = null; renderStatus(); }, 2000); }

function renderSchedule() {
  $("sb-weekly").textContent = "Weekly: " + ($("enabled").checked ? DAYS[+$("weekday").value].slice(0, 3) + " " + hh(+$("hour").value) : "off");
  $("sb-daily").textContent = "Daily: " + ($("daily").checked ? hh(+$("dhour").value) : "off");
}

/* actions */
async function act(what) {
  closeMenu();
  if (what === "log") return selectTab($("t-log"));
  if (what === "quit") {
    const b = await msgbox("Shut down DW to TIDAL? No playlists will be made until you open it again.", {icon: "q", buttons: ["Yes", "No"]});
    if (b !== "Yes") return;
    try { await api("/quit", {}); } catch {}
    $("safe").hidden = false; return;
  }
  await api("/" + what, {}); selectTab($("t-log")); poll();
}
$("run").onclick = () => act("run");
$("run-daily").onclick = () => act("run-daily");
$("connect").onclick = async () => { await api("/tidal/login", {}); flash("Getting a TIDAL sign-in link…"); poll(); };

function closeWindow() {
  window.close();
  setTimeout(() => msgbox("DW to TIDAL keeps running in the background. You can close this browser tab.", {icon: "info"}), 250);
}
$("close").onclick = closeWindow; $("close-x").onclick = closeWindow;
$("help").onclick = () => msgbox("DW to TIDAL copies Spotify's Discover Weekly to TIDAL each week, and adds TIDAL's My Daily Discovery to a playlist for each week of the year. It runs hidden on this computer; close this window any time. To stop it completely, use Start → Shut Down.");

/* start menu */
function closeMenu() { $("menu").hidden = true; $("start").classList.remove("down"); $("start").setAttribute("aria-expanded", false); }
$("start").onclick = e => { e.stopPropagation(); const open = $("menu").hidden; closeMenu(); if (open) { $("menu").hidden = false; $("start").classList.add("down"); $("start").setAttribute("aria-expanded", true); $("menu").querySelector("button").focus(); } };
document.addEventListener("click", e => { if (!$("menu").contains(e.target)) closeMenu(); });
$("menu").querySelectorAll("button").forEach(b => b.onclick = () => act(b.dataset.act));

function tick() { $("clock").textContent = new Date().toLocaleTimeString([], {hour: "numeric", minute: "2-digit"}); }
tick(); setInterval(tick, 15000);

/* status */
function renderStatus() {
  const s = lastStatus; if (!s || flashTimer) return;
  const main = $("sb-main");
  if (s.running) main.innerHTML = '<div style="display:flex;gap:6px;align-items:center">Working… <div class="progress"><i></i></div></div>';
  else main.textContent = s.tidal_connected ? "Running in the background" : "Not connected to TIDAL";
}
async function poll() {
  let s;
  try { s = await api("/status"); } catch { $("sb-main").textContent = "DW to TIDAL isn't running."; return; }
  lastStatus = s;
  $("tidal-icon").innerHTML = `<use href="#i-${s.tidal_connected ? "ok" : "err"}"/>`;
  $("tidal-status").textContent = s.tidal_connected ? "Connected" + (s.tidal_user ? " as " + s.tidal_user : "") + "." : "Not connected.";
  $("connect").textContent = s.tidal_connected ? "Reconnect…" : "Connect TIDAL…";
  $("login-box").hidden = !s.tidal_login_url;
  if (s.tidal_login_url) { $("login-link").href = s.tidal_login_url; $("login-link").textContent = s.tidal_login_url; }
  document.querySelectorAll(".run").forEach(b => b.disabled = s.running);
  $("last-weekly").textContent = s.last_result ? "Last: " + s.last_result : "";
  $("last-daily").textContent = s.last_daily_result ? "Last: " + s.last_daily_result : "";
  const pre = $("log"), atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
  pre.textContent = s.log.length ? s.log.join("\n") : "Nothing yet.";
  if (atBottom) pre.scrollTop = pre.scrollHeight;
  renderStatus();
}
loadConfig().then(poll); setInterval(poll, 2000);
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
            self._json({**load_config(), "autostart_supported": autostart_supported()})
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
            before = cfg.get("autostart")
            incoming = self._body()
            for k, default in DEFAULT_CONFIG.items():
                if k in incoming and isinstance(incoming[k], type(default)):
                    cfg[k] = incoming[k]
            cfg["weekday"] = min(max(int(cfg["weekday"]), 0), 6)
            cfg["hour"] = min(max(int(cfg["hour"]), 0), 23)
            cfg["daily_hour"] = min(max(int(cfg["daily_hour"]), 0), 23)
            if cfg["dw_url"]:
                try:
                    playlist_id(cfg["dw_url"])
                except ValueError as e:
                    self._json({"ok": False, "error": str(e)}, 400)
                    return
            save_config(cfg)
            if autostart_supported() and cfg["autostart"] != before:
                set_autostart(cfg["autostart"])
            self._json({"ok": True})
        elif path == "/tidal/login":
            start_tidal_login()
            self._json({"ok": True})
        elif path == "/run":
            threading.Thread(target=run_job, args=("manual",), daemon=True).start()
            self._json({"ok": True})
        elif path == "/run-daily":
            threading.Thread(target=run_daily_job, args=("manual",), daemon=True).start()
            self._json({"ok": True})
        elif path == "/quit":
            self._json({"ok": True})
            log("Shutting down.")
            threading.Timer(0.3, lambda: os._exit(0)).start()
        else:
            self._json({"error": "not found"}, 404)


def serve(open_ui: bool) -> None:
    setup_logging()
    url = f"http://127.0.0.1:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # Already running (or port taken).
        logger.info("Port %s busy; another instance is running", PORT)
        if open_ui:
            open_window(url)
        return
    load_state()
    threading.Thread(target=tidal_session, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    logger.info("DW to TIDAL is running at %s", url)
    if open_ui:
        threading.Timer(0.8, lambda: open_window(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Discover Weekly → TIDAL")
    ap.add_argument("--background", action="store_true", help="run hidden, without opening a window")
    ap.add_argument("--show", action="store_true", help="start in the background if needed, then open the window")
    a = ap.parse_args()
    if a.show:
        show()
    else:
        serve(open_ui=not a.background)


if __name__ == "__main__":
    main()
