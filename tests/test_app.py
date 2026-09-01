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
