"""Command-line entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from yt_dlp.utils import DownloadError, parse_bytes

from ltube import get_version
from ltube.backoff import BackoffConfig, parse_range
from ltube.downloader import (
    Event,
    NotAPlaylistError,
    PlaylistDownloader,
    PlaylistStarted,
    RateLimitedError,
    Status,
    Summary,
    TrackFinished,
    TrackMoved,
    TrackRetry,
    TrackStarted,
    YtdlpWarning,
)

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(get_version())
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_print_version, is_eager=True, help="Show the version and exit.")
    ] = False,
) -> None:
    """Download YouTube Music playlists to local storage."""


@app.command()
def download(
    url: Annotated[str, typer.Argument(help="Playlist URL (music.youtube.com or youtube.com).")],
    out: Annotated[
        Path,
        typer.Option("--out", "-o", file_okay=False, resolve_path=True,
                     help="Library folder; tracks go to OUT/Artist/Album/."),
    ],
    cookies: Annotated[
        Path,
        typer.Option("--cookies", "-c", exists=True, dir_okay=False, readable=True, resolve_path=True,
                     help="Netscape-format cookies.txt exported from a signed-in browser."),
    ],
    sleep: Annotated[str, typer.Option(help="Pause between tracks in seconds: MIN-MAX or a single value.")] = "5-15",
    request_sleep: Annotated[float, typer.Option(min=0, help="Pause between yt-dlp's internal requests, seconds.")] = 1.0,
    retries: Annotated[int, typer.Option(min=0, help="Retries per track after the first attempt.")] = 5,
    backoff_base: Annotated[float, typer.Option(min=0.1, help="First retry delay, seconds; doubles each retry.")] = 10.0,
    backoff_max: Annotated[float, typer.Option(min=0.1, help="Upper bound for a retry delay, seconds.")] = 600.0,
    max_consecutive_failures: Annotated[int, typer.Option(min=1, help="Stop the run after this many failed tracks in a row.")] = 3,
    rate_limit: Annotated[str | None, typer.Option(help="Download bandwidth cap, e.g. 2M or 500K (bytes/s).")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show yt-dlp's own output.")] = False,
) -> None:  # fmt: skip
    """Download every track of one playlist, skipping tracks the library already has."""
    try:
        sleep_min, sleep_max = parse_range(sleep)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--sleep") from None

    rate = None
    if rate_limit:
        rate = parse_bytes(rate_limit)
        if rate is None:
            raise typer.BadParameter(f"can't parse {rate_limit!r}", param_hint="--rate-limit")

    try:
        backoff = BackoffConfig(
            sleep_min=sleep_min,
            sleep_max=sleep_max,
            request_sleep=request_sleep,
            retries=retries,
            base=backoff_base,
            cap=backoff_max,
            max_consecutive_failures=max_consecutive_failures,
            rate_limit=rate,
        )
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None

    downloader = PlaylistDownloader(out, cookies, backoff, verbose=verbose, on_event=_print_event)
    try:
        summary = downloader.run(url)
    except KeyboardInterrupt:
        typer.echo("\nInterrupted.", err=True)
        raise typer.Exit(130) from None
    except (NotAPlaylistError, RateLimitedError, DownloadError) as e:
        _fail(str(e).removeprefix("ERROR: "))

    _print_summary(summary)
    ok = summary.aborted is None and summary.count(Status.FAILED) == 0
    raise typer.Exit(0 if ok else 1)


@app.command()
def serve(
    out: Annotated[
        Path,
        typer.Option("--out", "-o", file_okay=False, resolve_path=True, envvar="LTUBE_OUT",
                     help="Initial library folder; change it later in the UI's Settings."),
    ] = Path("~/Music").expanduser(),
    lock_out: Annotated[
        bool,
        typer.Option("--lock-out/--no-lock-out", envvar="LTUBE_LOCK_OUT",
                     help="Keep OUT fixed and read-only in the UI (for Docker, where OUT is a volume mount)."),
    ] = False,
    data: Annotated[
        Path,
        typer.Option(file_okay=False, resolve_path=True, envvar="LTUBE_DATA",
                     help="Where the web UI keeps its state (uploaded cookies)."),
    ] = Path("~/.config/ltube").expanduser(),
    host: Annotated[str, typer.Option(envvar="LTUBE_HOST", help="Interface to bind; the UI has no login.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(envvar="LTUBE_PORT")] = 8000,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the UI in a browser.")] = False,
) -> None:  # fmt: skip
    """Start the web UI."""
    import uvicorn

    from ltube.web.app import create_app

    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, webbrowser.open, [f"http://{host}:{port}/"]).start()
    web_app = create_app(out_dir=out, data_dir=data, lock_out_dir=lock_out)
    # Open event streams would otherwise hold Ctrl-C until the browser disconnects.
    uvicorn.run(web_app, host=host, port=port, timeout_graceful_shutdown=2)


def _print_event(event: Event) -> None:
    match event:
        case PlaylistStarted(title, directory, total):
            typer.echo(f"Playlist: {title} ({total} tracks) -> library {directory}")
        case TrackStarted(index, total, title):
            typer.echo(f"[{index:0{max(2, len(str(total)))}d}/{total}] {title}")
        case TrackFinished(_, result):
            detail = result.path.name if result.path else result.error
            typer.echo(f"    {result.status.value}: {detail}")
        case TrackMoved(old, new):
            typer.echo(f"    moved to its album: {old.name} -> {new.parent.name}/{new.name}")
        case TrackRetry(_, attempt, delay, message):
            typer.echo(f"    attempt {attempt} failed: {message}; retrying in {delay:.0f}s")
        case YtdlpWarning(message):
            typer.echo(f"    warning: {message}")


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(2)


def _print_summary(summary: Summary) -> None:
    typer.echo("")
    typer.echo(f"{summary.playlist_title}: {len(summary.results)}/{summary.total} processed")
    for status in Status:
        if n := summary.count(status):
            typer.echo(f"  {status.value}: {n}")
    failed = [r for r in summary.results if r.status is Status.FAILED]
    for r in failed:
        typer.echo(f"  - {r.error}")
    if summary.aborted:
        typer.secho(f"Stopped early: {summary.aborted}", fg=typer.colors.RED, err=True)
