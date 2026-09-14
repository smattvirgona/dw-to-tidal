# DW to TIDAL

Copies your Spotify **Discover Weekly** into a new, dated playlist on **TIDAL** every week,
inside a "Weekly discoveries" folder. Runs on your own computer; nothing is sent anywhere
except to Spotify and TIDAL.

## Using it

1. **Download** `dw-to-tidal.zip`, unzip it, and keep the folder somewhere handy
   (e.g. Documents). Don't move files out of the folder.
2. **Open it:**
   - macOS: double-click **`Start.command`**. The first time, macOS may say it can't be opened:
     right-click it → **Open** → **Open**. (Once.)
   - Windows: double-click **`Start.bat`**. If SmartScreen appears: **More info** → **Run anyway**.

   The first time only, a black window downloads a small Python runtime (about a minute,
   ~100 MB, kept inside `~/.dw2tidal`; nothing is installed system-wide). Then the black window
   closes itself and the **DW to TIDAL** window opens (an app window if you have Chrome, Edge or
   Brave; otherwise a browser tab at `http://127.0.0.1:8765`).
3. **Setup tab:** press *Connect TIDAL…*, open the link, sign in and approve (once). Paste your
   Discover Weekly link (Spotify: Discover Weekly → `⋯` → Share → Copy link). Changes save by themselves.
4. **Playlists tab:** tick what you want:
   - *Spotify Discover Weekly*: copied every week on the day and hour you pick (it refreshes on Monday).
   - *TIDAL My Daily Discovery*: TIDAL's mix changes every morning; each day's mix is added to one
     playlist per week (Monday–Sunday), named by week of the year, e.g. `Week 38 (2026)`.
     A new playlist starts each Monday. Tracks already in that week's playlist are skipped.
   Each goes into its own TIDAL folder (default *Weekly discoveries* and *TIDAL Discovery*).
5. **Close the window.** The app keeps running hidden and starts again when you log in.
   Double-click `Start.command` / `Start.bat` any time to reopen the window.

Discover Weekly runs create `Discover Weekly YYYY-MM-DD`; daily runs add to `Week NN (YYYY)`. Tracks that
can't be found on TIDAL are listed on the *Log* tab. If the computer was asleep at the scheduled
hour, it runs when it wakes up later that day (a shut-down computer can't run it).

To stop it: **Start → Shut Down DW to TIDAL…** in the window. To stop it starting at login, untick
*Run hidden in the background, and start when I log in* on the Setup tab.

Phones (iOS/Android) aren't supported — the app has to run on a computer.

### Optional: Spotify developer key

Matching is done by artist + title and is right almost every time. A Spotify developer key
(developer.spotify.com/dashboard → Create app; any redirect URI such as `http://127.0.0.1:8765/callback`)
lets the app match by ISRC code instead. **Note:** Spotify currently returns `403 Forbidden` for
this lookup on newly created apps, in which case the app silently falls back to artist + title.

### Where things are stored

Everything lives in `~/.dw2tidal/` (macOS: `/Users/you/.dw2tidal`, Windows: `C:\Users\you\.dw2tidal`):
`config.json`, `tidal-session.json` (your TIDAL login — keep private), `state.json`, `app.log`,
and the downloaded runtime (`bin/`, `uv/`). The start-at-login entry is
`~/Library/LaunchAgents/com.dw2tidal.app.plist` (macOS) or `DW to TIDAL.vbs` in the Startup folder
(Windows). To uninstall: Shut Down, untick start-at-login (or delete that file), then delete the folder.

## Making the zip (for whoever shares it)

```sh
./make_package.sh    # -> dist/dw-to-tidal.zip
```

Run from source instead: `pip install -r requirements.txt && python3 dw2tidal_app.py`
(Python 3.11+). Tests: `pip install -r requirements-dev.txt && pytest`.

## How it works / caveats

- Spotify's Web API no longer exposes Discover Weekly to third-party apps, so the track list is
  read from the public embed page (`open.spotify.com/embed/playlist/…`). This is unofficial and may
  break if Spotify changes the page — the app will say "Spotify's embed page has changed".
- TIDAL access uses the `tidalapi` package (device-link login, like a TV app).
- The launcher uses [uv](https://docs.astral.sh/uv/) to fetch a private Python runtime; it's confined to `~/.dw2tidal`.
