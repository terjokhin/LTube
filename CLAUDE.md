# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ltube` downloads a YouTube Music playlist into `OUT/<playlist name>/`, one audio file per track, plus an `.m3u8` playlist. It is deliberately slow and polite to avoid YouTube rate limiting, and requires a signed-in browser's exported `cookies.txt`. Two front ends share one downloader: a Typer CLI (`ltube download`) and a web UI (`ltube serve`: FastAPI backend + SvelteKit frontend in `web/`), which also ships as a Docker image.

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
- `downloader.py` — the core. `PlaylistDownloader.run()`:
  1. Copies the cookie file into a temp dir (yt-dlp rewrites its cookie file; the user's file must stay untouched).
  2. Lists the playlist flat (`extract_flat="in_playlist"`), then downloads each track with a **fresh `YoutubeDL` instance per attempt**.
  3. Before the loop, `metadata.scan_existing` indexes the folder's audio files by the YouTube video ID in their tags (`purl`/`comment`, written by FFmpegMetadata). Tracks found there are reported as EXISTS with **no request and no pause**, whatever their number prefix, because positions shift when a playlist changes. Otherwise: pause → extract info → compute target filename → skip if that file exists (the fallback for untagged files; `_find_existing` checks both `.m4a` and `.opus`, since the final container isn't known before download) → download. New tracks get their current position number, so after a playlist reorder, number prefixes can repeat; the `.m3u8` order is still correct.
  - Rename: before the loop, `_rename_existing` renames ID-matched files to what the current pattern and playlist positions would name them, using their own tags (no requests). It works in two phases via `.ltube-rename-<id>` temp names, so swaps work, and it never overwrites a file it isn't moving. Downloads and renames must both go through `metadata.track_file_name` (render, then yt-dlp's `sanitize_filename`, then a 230-byte limit; yt-dlp's `trim_file_name` is not used because it trims the whole path). `NormalizeMetadataPP` tags `date` with the release year (`meta_date`) instead of the upload date, and `scan_existing` only treats a bare 4-digit `date` as `{year}`.
  - Prune (`prune=True`, only for resyncs (runs into a pinned directory), when the opt-in `delete_removed` setting is on; it's off by default): after a run that finished without aborting, files from the initial `scan_existing` whose video ID is no longer listed are deleted (`TrackRemoved` events, `Summary.removed`). It's skipped with a `PruneSkipped` event if the listing was empty, if yt-dlp warned that YouTube hid unavailable videos (its reload of hidden entries is non-fatal, so the list may be short), or if more than half of the folder's tracks would go. Files without an ID tag are never candidates. `_Logger` is always installed (it prints only in verbose mode) so listing warnings can be checked.
  - `run(url, directory=None)`: `directory` pins the exact folder (resyncs) instead of deriving `OUT[/<subfolder>]/<name>` from the playlist/album, which also skips the album lookup. `Subfolders` (albums/playlists) and `NamePatterns` (album/playlist track file name patterns) are constructor options; the pattern is chosen once the listing tells whether it's an album.
  4. Writes the `.m3u8` (non-album playlists only) in a `finally`, so partial/aborted runs still get a playlist.
  - Progress is reported only through typed events (`PlaylistStarted`, `TrackStarted`, `TrackProgress`, `TrackRetry`, `TrackFinished`, `Waiting`, `YtdlpWarning`) passed to `on_event`; it never prints. New progress info means a new event, handled in both `cli._print_event` and `web.jobs.Job.apply`.
  - Cancellation: a `threading.Event` `stop`. All pauses go through `_sleep` (wakes early), and the yt-dlp progress hook raises `Cancelled`, which subclasses yt-dlp's `DownloadCancelled` so yt-dlp re-raises it instead of reporting it as a download error. Never use `time.sleep` in the downloader.
- Error handling hinges on `classify()`, which string-matches yt-dlp error messages into `RATE_LIMITED` (abort the whole run via `RateLimitedError`), `UNAVAILABLE` (skip track, no retry), or `TRANSIENT` (retry with backoff). Rate-limit markers are checked first and win. When adding markers, add a case to `tests/test_downloader.py`.
- Run also aborts after `max_consecutive_failures` FAILED tracks in a row.
- `backoff.py` — `BackoffConfig` (track pauses, exponential retry with "equal jitter", bandwidth cap) plus `inner_retry_sleep` for yt-dlp's own low-level retries. Two retry layers exist on purpose: yt-dlp's short internal retries (HTTP/fragment/extractor), and ltube's whole-track retries with the user's backoff. Between tracks, a full random pause follows real downloads/failures; only a short `request_sleep` follows cheap "already exists"/unavailable lookups.
- `metadata.py` — `scan_existing` (see above; uses mutagen). Track file names: `render_name(pattern, values)` fills `{number} {artist} {title} {album} {year}` and drops an empty field together with its separator (or its surrounding brackets); `TrackNamePP` (pre_process, after `NormalizeMetadataPP`) stores the result in `info["ltube_name"]`, and the output template is just `%(ltube_name)s.%(ext)s`, so yt-dlp still sanitizes it. `web/src/lib/pattern.ts` is a TypeScript copy used for live previews; keep the two in sync. `NormalizeMetadataPP`, a yt-dlp `PostProcessor` registered `when="pre_process"` so the artist is filled in (from uploader, stripping `" - Topic"`, deduped) *before* the output filename is computed and before `FFmpegMetadata` writes tags. Also `write_m3u8` (paths relative to the playlist file).

## Web UI (`src/ltube/web/` + `web/`)

- `web/jobs.py` — `JobManager`: in-memory jobs, run strictly one at a time by a single worker thread (deliberate, for rate limiting). The worker turns downloader events into job state under one lock and pushes messages to asyncio queues via `loop.call_soon_threadsafe`. `subscribe()` takes the snapshot and registers the listener under the same lock, so the stream has no gaps. History is lost on restart.
- `web/app.py` — `create_app(out_dir, data_dir, *, lock_out_dir=False, runner=None)`; tests inject a fake `runner` instead of the real downloader. API: `GET|PUT /api/settings` (the download folder), `PUT|DELETE /api/settings/cookies` (raw text body, validated with `MozillaCookieJar` and must contain youtube.com cookies, stored as `DATA/cookies.txt` with mode 0600), `GET|POST /api/jobs`, `DELETE /api/jobs/{id}` (cancel), and `GET /api/events` (SSE stream: a `snapshot` message, then `job`/`track`/`removed` messages).
- Download folder: set in the UI's Settings (must be absolute, `~` allowed; created and write-checked), persisted in `DATA/settings.json`, and read when each job is submitted (jobs don't take a folder). `--out` is only the initial value. With `lock_out_dir` (`--lock-out` / `LTUBE_LOCK_OUT=1`, set in the Dockerfile) the folder is always `--out` and `PUT /api/settings` returns 409, because in Docker it's the `/music` volume mount and any other path would land inside the container.
- Settings (`DATA/settings.json`, every field optional in `PUT`): `out_dir`; `use_subfolders` with `albums_subfolder`/`playlists_subfolder` (relative paths, no `..`; editable even when `out_dir` is locked, since they stay inside the mount); `album_pattern`/`playlist_pattern` (`validate_name_pattern`: must contain `{title}`, known placeholders only, no slashes); and pacing (`PACING_FIELDS`, defaults taken from `BackoffConfig()`; a `PUT` is validated by building a `BackoffConfig`; `rate_limit` is a string like `2M`, empty for unlimited). `GET` also returns `pacing_defaults` for the UI's reset button. Each job takes a copy of subfolders, patterns and pacing when it's submitted; `POST /api/jobs` takes only `url` and `favourite`.
- `web/playlists.py` — `PlaylistStore` (`DATA/playlists.json`; on first load it migrates the older `favourites.json`) remembers **every** playlist ever submitted, deduplicated by `playlist_key` (the `list=` ID or album browse ID). Favourites are just records with `favourite: true`. After its first run a record pins the exact `directory` it downloaded into. So any later run of the same playlist (a Resync, or pasting the URL again) is a resync: it fills that folder even if Settings changed or the playlist was renamed, and prunes if `delete_removed` is on. A pinned directory outside the locked Docker root is ignored. Jobs carry `playlist_id`/`target_dir`/`prune`. The `JobManager` `on_finish` hook records title, directory, counts and state on the record and broadcasts a `playlists` SSE message. Endpoints: `GET /api/playlists`, `POST /api/playlists/{id}/sync`, `PUT /api/playlists/{id}` `{favourite}` (un-favouriting keeps the record and its folder). A playlist can't be queued twice at once (409). The frontend's `playlistKey` in `api.ts` mirrors `playlist_key`.
- Static UI is served from `src/ltube/web/static/` if it exists. Pages are prerendered with `trailingSlash = 'always'` so `/settings/` maps to `settings/index.html` for Starlette's `StaticFiles`. The folder is gitignored but listed as a hatch `artifacts` entry so wheels include it.
- `web/` is SvelteKit 3 + Svelte 5 runes, TypeScript, `adapter-static`. SvelteKit 3 differences: config lives in `vite.config.ts` (no `svelte.config.js`), and `#lib/...` imports need the file extension (`#lib/api.ts`). Live state is in `src/lib/state.svelte.ts` (an EventSource that applies the server messages) and API types are in `src/lib/api.ts`; keep them in sync with `Job.public()`.
- The UI has no authentication; `serve` binds 127.0.0.1 by default, and compose publishes on 127.0.0.1 only.

## Output conventions

- Folder: `OUT/[<albums or playlists subfolder>/]<name>`. The name is the playlist title for ordinary playlists, which also get a `<title>.m3u8` (written in a `finally`, so partial runs get one too). Albums get no `.m3u8`. For YouTube Music albums it is `Artist - Year - Album` (year omitted if unknown). An album is detected by its playlist ID prefix `OLAK5uy_`; `music.youtube.com/browse/MPREb_…` links resolve to one. The playlist itself carries no artist or year, and its "<X> - Topic" uploader channel can differ from the real artist, so `_album_name` makes one extra metadata lookup on the first track (`album`, `artists[0]`, `release_year`).
- Track file names come from the album/playlist pattern (default `{number} - {artist} - {title}`). `{number}` is the zero-padded playlist position (width ≥ 2) and `{artist}` falls back to `Unknown Artist`. Changing a pattern is safe for existing libraries: the ID-based scan finds old files under their old names. Only untagged files rely on the filename fallback.
- Audio is never re-encoded: AAC stays `.m4a`; Opus is remuxed from webm to `.opus`. Thumbnail is converted to JPEG, center-cropped to square (`COVER_FFMPEG_ARGS`), and embedded.

## Versions, CI and releases

- The version comes from **git tags**, not Python. `pyproject.toml` has a fixed placeholder, `version = "0.0.0"`; don't bump it. `ltube.get_version()` returns `LTUBE_VERSION` (set in the image from the `VERSION` build arg, default `0.0.0-SNAPSHOT`) or the package version. `ltube --version` prints it, and FastAPI reports it.
- `.github/workflows/ci.yml` runs on pushes to `main` and on PRs. It runs backend tests (`uv sync --locked` + pytest), the frontend `npm ci`/`check`/`build`, and a Docker build without pushing.
- `.github/workflows/release.yml` runs on tags `X.Y.Z*` or `vX.Y.Z*`. It doesn't re-run CI (tags are cut from `main` commits that already passed); it builds amd64+arm64 and pushes `ghcr.io/<owner>/ltube:{version, major.minor, latest}` (metadata-action semver), and verifies the image with `imagetools inspect` and `ltube --version`.
- Actions are pinned to major tags, except `astral-sh/setup-uv`, which publishes no major tag and is pinned to an exact version. Lint workflows with `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:<version>`.

## Docker

`Dockerfile`: a Node stage (`--platform=$BUILDPLATFORM`, since its output is architecture-independent static files) builds the UI. Then a `python:3.13-slim` stage uses uv and runs as uid 1000 (`ltube`). `ARG VERSION`, the OCI labels and `LTUBE_VERSION` come last, so a new version only rebuilds those layers. Volumes: `/music` (the host's music folder, from `MUSIC_DIR` in `compose.yaml`, default `./music`) and `/data` (cookies, settings, yt-dlp cache). The `.dockerignore` keeps cookie files out of the image.

## Secrets

The web UI stores the uploaded `cookies.txt` (a live browser session) as `DATA/cookies.txt`, by default `~/.config/ltube/cookies.txt`. Never read it into output or log it. The repo no longer contains one; `.gitignore` and `.dockerignore` still exclude `cookies*.txt`.
