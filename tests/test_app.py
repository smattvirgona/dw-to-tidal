import datetime
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import dw2tidal_app as app  # noqa: E402

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "embed_playlist.html"


# ------------------------------------------------------------- embed parser

def test_parse_embed_fixture():
    tracks = app.parse_embed_page(FIXTURE.read_text(encoding="utf-8"))
    assert len(tracks) == 30
    first = tracks[0]
    assert set(first) == {"id", "title", "artist", "isrc"}
    assert len(first["id"]) == 22
    assert first["title"] and first["artist"]
    assert first["isrc"] is None
    assert all(t["id"] for t in tracks)


def test_parse_embed_uses_first_artist_only():
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"state":{"data":{"entity":{"trackList":['
            '{"uri":"spotify:track:0123456789abcdefghijkl","title":"T","subtitle":"A, B, C"},'
            '{"uri":"spotify:episode:xyz","title":"skip","subtitle":"pod"}'
            ']}}}}}}</script>')
    tracks = app.parse_embed_page(html)
    assert tracks == [{"id": "0123456789abcdefghijkl", "title": "T", "artist": "A", "isrc": None}]


def test_parse_embed_missing_next_data():
    with pytest.raises(RuntimeError):
        app.parse_embed_page("<html><body>nope</body></html>")


def test_parse_embed_missing_tracklist():
    with pytest.raises(RuntimeError):
        app.parse_embed_page('<script id="__NEXT_DATA__">{"props":{"pageProps":{}}}</script>')


@pytest.mark.parametrize("url", [
    "https://open.spotify.com/playlist/37i9dQZEVXcM8vzTfJi3ej?si=abc",
    "https://open.spotify.com/embed/playlist/37i9dQZEVXcM8vzTfJi3ej",
    "spotify:playlist:37i9dQZEVXcM8vzTfJi3ej",
    "37i9dQZEVXcM8vzTfJi3ej",
])
def test_playlist_id(url):
    assert app.playlist_id(url) == "37i9dQZEVXcM8vzTfJi3ej"


def test_playlist_id_rejects_garbage():
    with pytest.raises(ValueError):
        app.playlist_id("https://open.spotify.com/track/37i9dQZEVXcM8vzTfJi3ej")


# ------------------------------------------------------------- title cleaning

@pytest.mark.parametrize("raw,expected", [
    ("Song (feat. Someone)", "Song"),
    ("Song [feat. Someone]", "Song"),
    ("Song (with Someone)", "Song"),
    ("Song - Remastered 2011", "Song"),
    ("Song - 2011 Remaster", "Song"),
    ("Song (Remastered)", "Song"),
    ("Song - Radio Edit", "Song"),
    ("Song - Live at Wembley", "Song"),
    ("Song (feat. X) - Remaster", "Song"),
    ("Song - feat. X", "Song"),
    ("Song (Part 2)", "Song (Part 2)"),          # not a known suffix: keep
    ("Song - Not A Suffix", "Song - Not A Suffix"),  # unknown dash text: keep
    ("(Remastered)", "(Remastered)"),            # never return empty
    ("Plain Title", "Plain Title"),
])
def test_clean_title(raw, expected):
    assert app.clean_title(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("A feat. B", "A"),
    ("A ft. B", "A"),
    ("A & B", "A"),
    ("A, B", "A"),
    ("A x B", "A"),
    ("Solo Artist", "Solo Artist"),
])
def test_clean_artist(raw, expected):
    assert app.clean_artist(raw) == expected


def test_norm():
    assert app.norm("Björk & The Band!") == "bj rk and the band"


# ------------------------------------------------------------- scheduler

def _cfg(**kw):
    return {"schedule_enabled": True, "weekday": 0, "hour": 8, **kw}


def test_schedule_due_at_hour():
    now = datetime.datetime(2026, 8, 31, 8, 0)  # a Monday
    assert app.schedule_due(_cfg(), now, None, -1e9, 0.0)


def test_schedule_catches_up_later_same_day():
    now = datetime.datetime(2026, 8, 31, 15, 30)
    assert app.schedule_due(_cfg(), now, "2026-08-24", -1e9, 0.0)


def test_schedule_not_before_hour_or_wrong_day():
    assert not app.schedule_due(_cfg(), datetime.datetime(2026, 8, 31, 7, 59), None, -1e9, 0.0)
    assert not app.schedule_due(_cfg(), datetime.datetime(2026, 9, 1, 9, 0), None, -1e9, 0.0)


def test_schedule_not_twice_a_day_and_backs_off_after_failure():
    now = datetime.datetime(2026, 8, 31, 9, 0)
    assert not app.schedule_due(_cfg(), now, "2026-08-31", -1e9, 0.0)
    assert not app.schedule_due(_cfg(), now, None, 100.0, 100.0 + 60)
    assert app.schedule_due(_cfg(), now, None, 100.0, 100.0 + app.RETRY_AFTER_FAILURE)
    assert not app.schedule_due(_cfg(schedule_enabled=False), now, None, -1e9, 0.0)


# ------------------------------------------------------------- folders

class _PL:
    def __init__(self, name, parent=None):
        self.name, self.parent_folder_id, self.trn = name, parent, f"trn:playlist:{name}"


class _Folder:
    name = "Weekly discoveries"
    def __init__(self): self.moved = []
    def add_items(self, trns): self.moved.extend(trns)


def test_tidy_into_folder_moves_only_root_dw_playlists():
    f = _Folder()
    app.tidy_into_folder(None, f, [_PL("Discover Weekly 2026-09-01"), _PL("Discover Weekly 2026-08-25", "fid"), _PL("Other")])
    assert f.moved == ["trn:playlist:Discover Weekly 2026-09-01"]


def test_get_or_create_folder_blank_name_returns_none():
    assert app.get_or_create_folder(None, "  ") is None


def test_get_or_create_folder_matches_case_insensitively(monkeypatch):
    monkeypatch.setattr(app, "list_folders", lambda s: [{"id": "abc", "name": "weekly DISCOVERIES", "trn": "t"}])
    class S:
        def folder(self, fid): return ("folder", fid)
    assert app.get_or_create_folder(S(), "Weekly discoveries") == ("folder", "abc")


# ------------------------------------------------------------- daily discovery

def test_daily_due():
    cfg = {"daily_enabled": True, "daily_hour": 9}
    now = datetime.datetime(2026, 9, 12, 9, 0)
    assert app.daily_due(cfg, now, "2026-09-11", -1e9, 0.0)
    assert app.daily_due(cfg, datetime.datetime(2026, 9, 12, 22, 0), None, -1e9, 0.0)
    assert not app.daily_due(cfg, datetime.datetime(2026, 9, 12, 8, 59), None, -1e9, 0.0)
    assert not app.daily_due(cfg, now, "2026-09-12", -1e9, 0.0)
    assert not app.daily_due(cfg, now, None, 100.0, 160.0)
    assert not app.daily_due({**cfg, "daily_enabled": False}, now, None, -1e9, 0.0)


class _Mix:
    def __init__(self, mix_type, items):
        self.mix_type, self._items = mix_type, items
    def items(self): return self._items


def _track(i):
    t = app.tidalapi.Track.__new__(app.tidalapi.Track)
    t.id = i
    return t


class _FakeSession:
    def __init__(self, existing=()):
        test = self
        self.created, self.added, self.fetched = [], [], []
        self.in_playlist = set()
        class P:
            def add(self, ids):
                new = [i for i in ids if i not in test.in_playlist]
                test.in_playlist.update(new)
                test.added.append(new)
                return new
        self._P = P
        class User:
            def playlists(self): return list(existing)
            def create_playlist(self, name, desc, parent_id="root"):
                test.created.append((name, desc, parent_id))
                return P()
        self.user = User()
    def playlist(self, pid):
        self.fetched.append(pid)
        return self._P()
    def mixes(self):
        MT = app.tidalapi.mix.MixType
        return [_Mix(MT.daily, [_track(1)]), _Mix(MT.discovery, [_track(10), object(), _track(11)])]


class _Day(datetime.date):
    fixed = None
    @classmethod
    def today(cls): return cls.fixed


def _setup_daily(monkeypatch, tmp_path, sess, day):
    _Day.fixed = _Day(day.year, day.month, day.day)
    monkeypatch.setattr(app.datetime, "date", _Day)
    monkeypatch.setattr(app, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(app, "load_config", lambda: dict(app.DEFAULT_CONFIG))
    monkeypatch.setattr(app, "tidal_session", lambda: sess)
    monkeypatch.setattr(app, "save_tidal_session", lambda s: None)
    class F:
        id, name = "fid", "TIDAL Discovery"
    monkeypatch.setattr(app, "get_or_create_folder", lambda s, n: F())
    monkeypatch.setitem(app.STATE, "tidal_connected", True)
    monkeypatch.setitem(app.STATE, "running", False)


def test_week_playlist_name():
    assert app.week_playlist_name(datetime.date(2026, 9, 14)) == "Week 38 (2026)"  # Monday
    assert app.week_playlist_name(datetime.date(2026, 9, 20)) == "Week 38 (2026)"  # Sunday, same week
    assert app.week_playlist_name(datetime.date(2026, 9, 21)) == "Week 39 (2026)"
    assert app.week_playlist_name(datetime.date(2026, 1, 5)) == "Week 02 (2026)"
    assert app.week_playlist_name(datetime.date(2027, 1, 1)) == "Week 53 (2026)"  # ISO year


def test_run_daily_job_starts_week_playlist_in_folder(monkeypatch, tmp_path):
    sess = _FakeSession()
    _setup_daily(monkeypatch, tmp_path, sess, datetime.date(2026, 9, 14))
    app.run_daily_job("manual")
    assert sess.created == [("Week 38 (2026)", "TIDAL My Daily Discovery, 14 Sep – 20 Sep 2026", "fid")]
    assert sess.added == [["10", "11"]]  # tracks only, from the discovery mix
    assert app.STATE["last_daily_date"] == "2026-09-14"
    assert app.DEFAULT_CONFIG["daily_folder_name"] == "TIDAL Discovery"


def test_run_daily_job_adds_to_existing_week_playlist(monkeypatch, tmp_path):
    existing = _PL("Week 38 (2026)", "fid")
    existing.id = "pl38"
    sess = _FakeSession([_PL("Week 37 (2026)", "fid"), existing])
    sess.in_playlist = {"10"}  # already added earlier in the week
    _setup_daily(monkeypatch, tmp_path, sess, datetime.date(2026, 9, 17))
    app.run_daily_job("scheduled")
    assert sess.created == []
    assert sess.fetched == ["pl38"]
    assert sess.added == [["11"]]
    assert "Added 1 new of 2 tracks" in app.STATE["last_daily_result"]


# ------------------------------------------------------------- autostart

def test_launch_agent_plist(monkeypatch, tmp_path):
    import plistlib
    monkeypatch.setattr(app.sys, "platform", "darwin")
    monkeypatch.setattr(app, "LAUNCH_AGENT", tmp_path / "agent.plist")
    monkeypatch.setenv("DW2TIDAL_UV", "/u/bin/uv")
    monkeypatch.setenv("UV_CACHE_DIR", "/u/cache")
    app.set_autostart(True)
    p = plistlib.loads((tmp_path / "agent.plist").read_bytes())
    assert p["ProgramArguments"][:3] == ["/u/bin/uv", "run", "--script"]
    assert p["ProgramArguments"][-1] == "--background"
    assert p["EnvironmentVariables"]["UV_CACHE_DIR"] == "/u/cache"
    assert p["KeepAlive"] == {"SuccessfulExit": False}
    app.set_autostart(False)
    assert not (tmp_path / "agent.plist").exists()


def test_windows_startup_script(monkeypatch, tmp_path):
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setattr(app, "WIN_STARTUP", tmp_path)
    monkeypatch.setattr(app, "WIN_STARTUP_FILE", tmp_path / "DW to TIDAL.vbs")
    monkeypatch.setenv("DW2TIDAL_UV", r"C:\Users\me\.dw2tidal\bin\uv.exe")
    app.set_autostart(True)
    vbs = (tmp_path / "DW to TIDAL.vbs").read_text()
    assert 'sh.Run """C:\\Users\\me\\.dw2tidal\\bin\\uv.exe"" ""run"" ""--script""' in vbs
    assert '""--background""", 0, False' in vbs
