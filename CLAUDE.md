# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ltube` downloads YouTube Music playlists and albums into one shared library, `OUT/Album Artist/Album/`, one audio file per track however many playlists contain it. Playlists are `OUT/playlists/<name>.m3u8` files pointing at the shared files. A SQLite index (`OUT/.ltube/library.db`) records which video ID is where. It is deliberately slow and polite to avoid YouTube rate limiting, and requires a signed-in browser's exported `cookies.txt`. Two front ends share one downloader: a Typer CLI (`ltube download`) and a web UI (`ltube serve`: FastAPI backend + SvelteKit frontend in `web/`), which also ships as a Docker image.

## Commands

Managed with `uv` (Python 3.13 pinned in `.python-version`; `requires-python >= 3.12`).

```sh
uv sync                                   # install deps + dev group into .venv
uv run pytest                             # all tests (fast, no network)
uv run pytest tests/test_backoff.py::test_parse_range   # single test
uv run ltube download URL -o OUT -c cookies.txt         # run the CLI (also: python -m ltube)
uv run ltube serve [-o INITIAL_OUT] [--open]            # web UI on 127.0.0.1:8000

cd web && npm ci                  # frontend deps (Node 24 via nvm)
cd web && npm run dev             # Vite dev server; proxies /api to `ltube serve` on :8000
cd web && npm run check           # svelte-check / TypeScript
cd web && npm run build           # writes the static UI into src/ltube/web/static/ (gitignored)

MUSIC_DIR=~/Music docker compose up --build   # image: UI on 127.0.0.1:8000, host MUSIC_DIR mounted at /music
```

No linter/formatter is configured; code uses `# fmt: skip` markers in places, suggesting black/ruff-format style, but nothing enforces it.

## External binaries come from Python packages

No system installs are needed: **ffmpeg** comes from the `imageio-ffmpeg` wheel (passed explicitly as `ffmpeg_location`; override with `IMAGEIO_FFMPEG_EXE`), and **Deno** (yt-dlp's JS runtime for YouTube challenges) comes from the `deno` PyPI package, which yt-dlp discovers in the venv on its own.

## Architecture (`src/ltube/`)

- `cli.py` — Typer app. `download` parses/validates options into a `BackoffConfig`, runs `PlaylistDownloader`, and prints its events (`_print_event`) and a summary. `serve` starts uvicorn; its options also read `LTUBE_OUT/LOCK_OUT/DATA/HOST/PORT` env vars (used by the Docker image). Exit codes: 0 all good, 1 any track failed or run aborted, 2 fatal error (not a playlist / rate-limited during listing / DownloadError), 130 Ctrl-C.
- `library.py` — `Library(root)`: the index and the run lock.
  - SQLite in `root/.ltube/library.db` (tables `tracks`, `playlists`, `playlist_items`; paths relative to the root; `PRAGMA user_version` schema version, a newer one is refused). Schema creation runs in one `BEGIN IMMEDIATE` transaction: two processes opening a new library at once must not both create the tables. One short-lived connection per call, so any thread can use a `Library`.
  - `get_track` returns None, and deletes the row, if the file is gone: the track is then downloaded again. The index is the source of truth for "what do we have", but the disk wins.
  - `set_playlist_items` replaces a playlist's items; `merge_playlist_items` keeps old items missing from the new list (each right after the item that preceded it) for listings that can't be trusted. `playlist_tracks` returns the tracks in order, leaving out missing files.
  - `lock()` is an `flock` on `.ltube/lock`, held for a whole run: a second run (another job, the CLI) blocks until the first is done, then finds its tracks in the index. Not cancellable while waiting; the web UI never waits, it runs one job at a time.
  - `tmp_dir` (`.ltube/tmp`) is the staging area; `.ltube/.ndignore` (`*`) keeps Navidrome out of the index and staging.
- `downloader.py` — the core. `PlaylistDownloader.run(url)`:
  1. Copies the cookie file into a temp dir (yt-dlp rewrites its cookie file; the user's file must stay untouched), opens the `Library`, takes its lock, and clears leftover staging folders of a killed run.
  2. Lists the playlist flat (`extract_flat="in_playlist"`) into a `_Listing(id, title, items, is_album)`; the ID is the playlist ID that keys it in the index.
  3. For a playlist (not an album), `_record_playlist` stores the tracks and their order **before any download**, so a cancelled or failed run still leaves a correct playlist file. If the listing can't be trusted it only adds to what is remembered (`merge_playlist_items`, plus a `YtdlpWarning`): when yt-dlp warned that YouTube hid unavailable videos, or when more than 2 and more than half of the remembered tracks vanished at once (`_untrusted_listing`). A track missing from such a listing may merely be unplayable.
  4. Per track: `library.get_track(id)` — known tracks are EXISTS with **no request and no pause**, whichever playlist or album fetched them. Otherwise: pause → `_download_track(url, number)`; `number` is the track's position for album runs (`None` for playlists).
  5. `_attempt`, with a **fresh `YoutubeDL` per attempt**: builds the track in `.ltube/tmp/<id>/`, runs `NormalizeMetadataPP`, `LibraryNamePP` (both pre_process) and `CoverArtPP` (before_dl), extracts the info, and computes the target `OUT/<ltube_dir>/<ltube_name>.<ext>`. If that file already exists but the index doesn't know it (e.g. the index was lost), it is adopted: EXISTS, no download. Otherwise it downloads, `os.replace`s the finished file into the library (same filesystem, so atomic) and `add_track`s it. The staging folder is removed however the attempt ends, so a failed or half-processed track is never in the library for a retry or a later run to mistake for a finished one.
  6. **Singles → album**: during an album run, a known track whose index row has no album (it came in on its own, so it sits in `Artist/Singles/`) is retagged with `retag_album` (album, album artist, track number/total, year; mutagen, m4a and opus) and moved to its album folder, with no download (`_file_under_album`, event `TrackMoved`). The album's name, artist and year come from one lookup on the album's first track (`_album_of`, cached per run, made only when some track needs it). It never overwrites a file and leaves the track where it is, with a warning, if the target name is taken or the tags can't be written.
  7. Playlists get `playlists/<title>.m3u8` written in a `finally` from the index (`_write_playlist_file`), so partial runs still produce a complete one, including tracks earlier runs fetched. Albums get none. A renamed playlist gets a new file and loses the old (`m3u_name` in the index); two playlists with one title get `Title (<id>).m3u8` for the second.
  - ltube never deletes library files (no pruning). A track removed from a playlist only leaves the playlist's items and `.m3u8`.
  - Listing: if the signed-in listing returns fewer entries than `playlist_count` and yt-dlp warned that unavailable videos are hidden, `_recover_hidden` first asks **YouTube Music** (`ltube/ytmusic.py`, via `ytmusicapi`, signed in from the same cookies.txt). That gives every track with its artist and album; for uploaded albums, `with_album_details` fetches each album page once (paced with `_sleep`) for track number/count, year and album artist. All of it is kept in `self._music` and applied by `NormalizeMetadataPP(music)` only where yt-dlp has no music metadata. If YouTube Music fails (`music_playlist` never raises, returns None), it lists the playlist again **signed out** (`_extract_listing(signed_in=False)` drops `cookiefile`) and merges the IDs in, with no artist info. Signed in, YouTube leaves out private videos even when asked to show unavailable ones, e.g. songs uploaded to YouTube Music (private videos on the "Music Library Uploads" channel). The tracks themselves download fine with cookies. If the merged list is complete, the hidden-videos warning is dropped so the listing counts as trusted. An empty listing aborts the run with an explanation (and leaves the playlist file alone) instead of finishing "done" with 0 tracks. `_Logger` is always installed (it prints only in verbose mode) so listing warnings can be checked.
  - Uploads and covers: `NormalizeMetadataPP` treats `UPLOADS_CHANNEL` as no artist and sets `meta_artist=""` so FFmpegMetadata doesn't tag the channel. `usable_thumbnails` narrows a private video's thumbnails to signed (`sqp=`) URLs, preferring the square `vi_locker` upload art; otherwise yt-dlp tries ~40 that fail, one request each. `CoverArtPP` (before_dl) replaces FFmpegThumbnailsConvertor: it lets ffmpeg detect the image format from content (YouTube serves JPEGs named .png for uploads, which the convertor's forced `-f image2` decodes by extension and fails on), crops to a square JPEG, and drops an unconvertible cover with a warning instead of failing the track.
  - Progress is reported only through typed events (`PlaylistStarted`, `TrackStarted`, `TrackProgress`, `TrackRetry`, `TrackFinished`, `TrackMoved`, `Waiting`, `YtdlpWarning`) passed to `on_event`; it never prints. New progress info means a new event, handled in both `cli._print_event` and `web.jobs.Job.apply`.
  - Cancellation: a `threading.Event` `stop`. All pauses go through `_sleep` (wakes early), and the yt-dlp progress hook raises `Cancelled`, which subclasses yt-dlp's `DownloadCancelled` so yt-dlp re-raises it instead of reporting it as a download error. Never use `time.sleep` in the downloader.
- Error handling hinges on `classify()`, which string-matches yt-dlp error messages into `RATE_LIMITED` (abort the whole run via `RateLimitedError`), `UNAVAILABLE` (skip track, no retry), or `TRANSIENT` (retry with backoff). Rate-limit markers are checked first and win. When adding markers, add a case to `tests/test_downloader.py`.
- Run also aborts after `max_consecutive_failures` FAILED tracks in a row.
- `backoff.py` — `BackoffConfig` (track pauses, exponential retry with "equal jitter", bandwidth cap) plus `inner_retry_sleep` for yt-dlp's own low-level retries. Two retry layers exist on purpose: yt-dlp's short internal retries (HTTP/fragment/extractor), and ltube's whole-track retries with the user's backoff. Between tracks, a full random pause follows real downloads/failures; only a short `request_sleep` follows cheap "already in the library"/unavailable lookups.
- `metadata.py` — naming, tags and playlist files.
  - Track names come from one pattern (`DEFAULT_TRACK_PATTERN`, `{number} - {title} [{id}]`). `render_name(pattern, values)` fills `{number} {artist} {title} {album} {year} {id}` and drops an empty field together with its separator (or its surrounding brackets). `validate_track_pattern` requires `{title}` and `{id}`: the ID in the name is what lets the index be rebuilt from the files.
  - `library_location(pattern, info, number)` returns (folder relative to the library root, file name): `Album Artist/Album` (album artist falls back to the first artist, then `Unknown Artist`; no album → `Singles`) and the rendered name. Folder levels go through `folder_name`: yt-dlp's `sanitize_filename`, a 230-byte limit, and leading dots stripped (`sanitize_filename` leaves `..` alone and a leading dot would hide the folder). A name that is too long loses the end of the **title**, never the end of the name, so `[id]` survives (`_fit_name`). Don't use yt-dlp's `trim_file_name`: it trims the whole path.
  - `LibraryNamePP` (pre_process, after `NormalizeMetadataPP`) stores the folder in `info["ltube_dir"]` and the name in `info["ltube_name"]` separately: yt-dlp sanitizes a `/` inside one field value (`AC/DC` → `AC⧸DC`), so the folder can't ride in the name. Staging uses `%(ltube_name)s.%(ext)s`; the downloader moves the result to `ltube_dir`. For album runs it also sets `info["track_number"]` to the album position, which FFmpegMetadata tags as `track` and media servers order an album by.
  - `web/src/lib/pattern.ts` is a TypeScript copy of `render_name` and the pattern checks, used for live previews; keep the two in sync (placeholders included).
  - `NormalizeMetadataPP`, a yt-dlp `PostProcessor` registered `when="pre_process"` so the artist is filled in (from uploader, stripping `" - Topic"`, deduped) *before* the output filename is computed and before `FFmpegMetadata` writes tags. It tags `date` with the release year (`meta_date`) instead of the upload date.
  - `retag_album` (mutagen) puts a file on an album in place; `write_m3u8` writes an extended M3U atomically (temp file + `os.replace`), paths relative to the playlist file.

## Web UI (`src/ltube/web/` + `web/`)

- `web/jobs.py` — `JobManager`: in-memory jobs, run strictly one at a time by a single worker thread (deliberate, for rate limiting). The worker turns downloader events into job state under one lock and pushes messages to asyncio queues via `loop.call_soon_threadsafe`. `subscribe()` takes the snapshot and registers the listener under the same lock, so the stream has no gaps. History is lost on restart. A `Job` carries `playlist_id` and `pattern` (copied from Settings when it is submitted).
- `web/app.py` — `create_app(out_dir, data_dir, *, lock_out_dir=False, runner=None)`; tests inject a fake `runner` instead of the real downloader. API: `GET|PUT /api/settings`, `PUT|DELETE /api/settings/cookies` (raw text body, validated with `MozillaCookieJar` and must contain youtube.com cookies, stored as `DATA/cookies.txt` with mode 0600), `GET|POST /api/jobs`, `DELETE /api/jobs/{id}` (cancel), and `GET /api/events` (SSE stream: a `snapshot` message, then `job`/`track`/`removed`/`playlists` messages).
- Settings (`DATA/settings.json`, every field optional in `PUT`):
  - `out_dir`: the library folder. Must be absolute, `~` allowed; created and write-checked; read when each job is submitted. `--out` is only the initial value. With `lock_out_dir` (`--lock-out` / `LTUBE_LOCK_OUT=1`, set in the Dockerfile) it is always `--out` and `PUT /api/settings` returns 409 for a different one, because in Docker it's the `/music` volume mount and any other path would land inside the container.
  - `track_pattern` (`validate_track_pattern`); editable even when `out_dir` is locked.
  - Pacing (`PACING_FIELDS`, defaults taken from `BackoffConfig()`; a `PUT` is validated by building a `BackoffConfig`; `rate_limit` is a string like `2M`, empty for unlimited). `GET` also returns `pacing_defaults` for the UI's reset button.
  - Keys of the earlier folder layout (`use_subfolders`, `*_pattern`, `delete_removed`, ...) may linger in an old `settings.json`; they are ignored.
- `web/playlists.py` — `PlaylistStore` (`DATA/playlists.json`; on first load it migrates the older `favourites.json`) remembers **every** playlist ever submitted, deduplicated by `playlist_key` (the `list=` ID or album browse ID). Favourites are just records with `favourite: true`. Pasting a URL again, or a favourite's Resync, finds the record and runs it again; there is no per-playlist folder (the `directory` of older records is unused). The `JobManager` `on_finish` hook records title, counts and state on the record and broadcasts a `playlists` SSE message. Endpoints: `GET /api/playlists`, `POST /api/playlists/{id}/sync`, `PUT /api/playlists/{id}` `{favourite}` (un-favouriting keeps the record). A playlist can't be queued twice at once (409). The frontend's `playlistKey` in `api.ts` mirrors `playlist_key`.
- Static UI is served from `src/ltube/web/static/` if it exists. Pages are prerendered with `trailingSlash = 'always'` so `/settings/` maps to `settings/index.html` for Starlette's `StaticFiles`. The folder is gitignored but listed as a hatch `artifacts` entry so wheels include it.
- `web/` is SvelteKit 3 + Svelte 5 runes, TypeScript, `adapter-static`. SvelteKit 3 differences: config lives in `vite.config.ts` (no `svelte.config.js`), and `#lib/...` imports need the file extension (`#lib/api.ts`). Live state is in `src/lib/state.svelte.ts` (an EventSource that applies the server messages) and API types are in `src/lib/api.ts`; keep them in sync with `Job.public()`.
- The UI has no authentication; `serve` binds 127.0.0.1 by default, and compose publishes on 127.0.0.1 only.

## Output conventions

- Library: `OUT/<Album Artist>/<Album>/<name>.<ext>`, or `OUT/<Artist>/Singles/` for a track with no album. The same video is never stored twice, whichever playlists or albums contain it. Playlists are `OUT/playlists/<title>.m3u8` (relative paths into the library); albums get no playlist file. `OUT/.ltube/` holds the index, lock and staging.
- An album is detected by its playlist ID prefix `OLAK5uy_`; `music.youtube.com/browse/MPREb_…` links resolve to one. Its tracks are numbered by position in the album.
- Track names come from the one pattern (default `{number} - {title} [{id}]`). `{number}` is the zero-padded track number (width 2): the album position for album runs, otherwise YouTube's own track number, `00` if none. Changing the pattern is safe: tracks already in the library are found by ID and keep their names; nothing is renamed.
- There is no migration from the earlier layout (a folder per playlist, ID read from tags): delete the old library and download again.
- Audio is never re-encoded: AAC stays `.m4a`; Opus is remuxed from webm to `.opus`. Thumbnail is converted to JPEG, center-cropped to square (`COVER_FFMPEG_ARGS`), and embedded.

## Versions, CI and releases

- The version comes from **git tags**, not Python. `pyproject.toml` has a fixed placeholder, `version = "0.0.0"`; don't bump it. `ltube.get_version()` returns `LTUBE_VERSION` (set in the image from the `VERSION` build arg, default `0.0.0-SNAPSHOT`) or the package version. `ltube --version` prints it, and FastAPI reports it.
- `.github/workflows/ci.yml` runs on pushes to `main` and on PRs. It runs backend tests (`uv sync --locked` + pytest), the frontend `npm ci`/`check`/`build`, and a Docker build without pushing.
- `.github/workflows/release.yml` runs on tags `X.Y.Z*` or `vX.Y.Z*`. It doesn't re-run CI (tags are cut from `main` commits that already passed); it builds amd64+arm64 and pushes `ghcr.io/<owner>/ltube:{version, major.minor, latest}` (metadata-action semver), and verifies the image with `imagetools inspect` and `ltube --version`.
- Actions are pinned to major tags, except `astral-sh/setup-uv`, which publishes no major tag and is pinned to an exact version. Lint workflows with `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:<version>`.

## Docker

`Dockerfile`: a Node stage (`--platform=$BUILDPLATFORM`, since its output is architecture-independent static files) builds the UI. Then a `python:3.13-slim` stage uses uv and runs as uid 1000 (`ltube`). `ARG VERSION`, the OCI labels and `LTUBE_VERSION` come last, so a new version only rebuilds those layers. Volumes: `/music` (the host's music folder, from `MUSIC_DIR` in `compose.yaml`, default `./music`; the library, including its hidden `.ltube/` index and staging folder) and `/data` (cookies, settings, yt-dlp cache). The `.dockerignore` keeps cookie files out of the image.

## Secrets

The web UI stores the uploaded `cookies.txt` (a live browser session) as `DATA/cookies.txt`, by default `~/.config/ltube/cookies.txt`. Never read it into output or log it. The repo no longer contains one; `.gitignore` and `.dockerignore` still exclude `cookies*.txt`.
