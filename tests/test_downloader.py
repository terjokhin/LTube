import pytest

from ltube.downloader import ErrorKind, _find_existing, classify


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot", ErrorKind.RATE_LIMITED),
        ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", ErrorKind.RATE_LIMITED),
        ("This content isn't available, try again later. Your account has been rate-limited", ErrorKind.RATE_LIMITED),
        ("ERROR: [youtube] abc: Video unavailable", ErrorKind.UNAVAILABLE),
        ("ERROR: [youtube] abc: This video is unavailable", ErrorKind.UNAVAILABLE),
        ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", ErrorKind.UNAVAILABLE),
        ("ERROR: [youtube] abc: The uploader has not made this video available in your country", ErrorKind.UNAVAILABLE),
        ("ERROR: [youtube] abc: Video unavailable. This video is not available in your country", ErrorKind.UNAVAILABLE),
        ("ERROR: Unable to download webpage: <urlopen error timed out>", ErrorKind.TRANSIENT),
    ],
)
def test_classify(message, kind):
    assert classify(message) is kind


def test_find_existing_matches_either_container(tmp_path):
    target = tmp_path / "01 - Artist - Song 2.0.webm"
    assert _find_existing(target) is None
    (tmp_path / "01 - Artist - Song 2.0.opus").touch()
    assert _find_existing(target) == tmp_path / "01 - Artist - Song 2.0.opus"


def test_album_gets_artist_year_album_folder_and_no_m3u8(tmp_path, monkeypatch):
    from ltube.backoff import BackoffConfig
    from ltube.downloader import PlaylistDownloader, Status, TrackResult, _PlaylistItem

    cookies = tmp_path / "cookies.txt"
    cookies.touch()
    downloader = PlaylistDownloader(tmp_path, cookies, BackoffConfig(sleep_min=0, sleep_max=0, request_sleep=0))
    items = [_PlaylistItem("id1", "https://x/1", "Track")]
    monkeypatch.setattr(downloader, "_list_playlist", lambda url: ("Album - Discovery", items, True))
    monkeypatch.setattr(
        downloader, "_track_info", lambda url: {"album": "Discovery", "artists": ["Daft Punk", "Romanthony"], "release_year": 2001}
    )
    monkeypatch.setattr(
        downloader, "_download_track", lambda url, d, prefix: TrackResult(Status.DOWNLOADED, d / "01.opus", "A", 1)
    )

    summary = downloader.run("https://x")

    assert summary.playlist_dir == tmp_path / "Daft Punk - 2001 - Discovery"
    assert (summary.playlist_dir / "01.opus") in [r.path for r in summary.results]
    assert not list(summary.playlist_dir.glob("*.m3u8"))


def test_resync_skips_tracks_on_disk_without_requests(tmp_path, monkeypatch):
    from ltube import downloader as dl
    from ltube.backoff import BackoffConfig
    from ltube.metadata import ExistingTrack

    cookies = tmp_path / "cookies.txt"
    cookies.touch()
    downloader = dl.PlaylistDownloader(tmp_path, cookies, BackoffConfig(sleep_min=0, sleep_max=0, request_sleep=0))
    items = [dl._PlaylistItem("new", "https://x/new", "New"), dl._PlaylistItem("old", "https://x/old", "Old")]
    monkeypatch.setattr(downloader, "_list_playlist", lambda url: ("Renamed On YouTube", items, False))
    # Downloaded earlier as track 01; a new track was inserted before it since.
    old_file = tmp_path / "My Mix" / "01 - A - Old.opus"
    monkeypatch.setattr(dl, "scan_existing", lambda d: {"old": ExistingTrack(old_file, "A - Old", 100.0)})
    requested = []

    def fake_track(url, playlist_dir, prefix):
        requested.append((url, prefix))
        return dl.TrackResult(dl.Status.DOWNLOADED, playlist_dir / f"{prefix} - B - New.opus", "B - New", 50)

    monkeypatch.setattr(downloader, "_download_track", fake_track)
    summary = downloader.run("https://x", directory=tmp_path / "My Mix")

    assert requested == [("https://x/new", "01")]
    assert summary.playlist_dir == tmp_path / "My Mix"
    assert [(r.status, r.path.name) for r in summary.results] == [
        (dl.Status.DOWNLOADED, "01 - B - New.opus"),
        (dl.Status.EXISTS, "01 - A - Old.opus"),
    ]


@pytest.mark.parametrize(("is_album", "expected"), [(True, "Albums/A - 2001 - Disc"), (False, "Lists/Mix")])
def test_subfolders_split_albums_and_playlists(tmp_path, monkeypatch, is_album, expected):
    from ltube.backoff import BackoffConfig
    from ltube.downloader import PlaylistDownloader, Status, Subfolders, TrackResult, _PlaylistItem

    cookies = tmp_path / "cookies.txt"
    cookies.touch()
    downloader = PlaylistDownloader(
        tmp_path, cookies, BackoffConfig(sleep_min=0, sleep_max=0, request_sleep=0),
        subfolders=Subfolders(albums="Albums", playlists="Lists"),
    )
    items = [_PlaylistItem("id1", "https://x/1", "Track")]
    monkeypatch.setattr(downloader, "_list_playlist", lambda url: ("Mix", items, is_album))
    monkeypatch.setattr(downloader, "_track_info", lambda url: {"album": "Disc", "artists": ["A"], "release_year": 2001})
    monkeypatch.setattr(
        downloader, "_download_track", lambda url, d, prefix: TrackResult(Status.DOWNLOADED, d / "x.opus", "A", 1)
    )

    assert downloader.run("https://x").playlist_dir == tmp_path / expected


def make_pruning_downloader(tmp_path, monkeypatch, listed_ids, on_disk_ids, warnings=()):
    """A downloader whose playlist lists `listed_ids` and whose folder holds `on_disk_ids`."""
    from ltube import downloader as dl
    from ltube.backoff import BackoffConfig
    from ltube.metadata import ExistingTrack

    cookies = tmp_path / "cookies.txt"
    cookies.touch()
    events = []
    downloader = dl.PlaylistDownloader(
        tmp_path, cookies, BackoffConfig(sleep_min=0, sleep_max=0, request_sleep=0), prune=True, on_event=events.append
    )
    folder = tmp_path / "Mix"
    folder.mkdir()
    files = {vid: folder / f"{vid}.opus" for vid in on_disk_ids}
    for f in files.values():
        f.touch()
    items = [dl._PlaylistItem(vid, f"https://x/{vid}", vid) for vid in listed_ids]

    def fake_list(url):
        downloader._logger.warnings.extend(warnings)
        return "Mix", items, False

    monkeypatch.setattr(downloader, "_list_playlist", fake_list)
    monkeypatch.setattr(dl, "scan_existing", lambda d: {v: ExistingTrack(f, v, 1.0) for v, f in files.items()})
    monkeypatch.setattr(
        downloader, "_download_track", lambda url, d, prefix: dl.TrackResult(dl.Status.DOWNLOADED, d / "new.opus", "N", 1)
    )
    return downloader, files, events


def test_prune_deletes_tracks_that_left_the_playlist(tmp_path, monkeypatch):
    from ltube.downloader import TrackRemoved

    downloader, files, events = make_pruning_downloader(tmp_path, monkeypatch, ["a", "b", "c", "new"], ["a", "b", "c", "gone"])
    summary = downloader.run("https://x", directory=tmp_path / "Mix")

    assert summary.removed == [files["gone"]]
    assert not files["gone"].exists()
    assert all(files[v].exists() for v in "abc")
    assert [e.label for e in events if isinstance(e, TrackRemoved)] == ["gone"]


@pytest.mark.parametrize(
    ("listed", "on_disk", "warnings", "reason"),
    [
        (["a", "b"], ["a", "b", "x"], ["YouTube said: INFO - 1 unavailable video is hidden"], "hid unavailable"),
        (["a"], ["a", "x", "y", "z"], [], "refusing to delete more than half"),
        ([], ["x"], [], "came back empty"),
    ],
)
def test_prune_refuses_when_listing_is_untrustworthy(tmp_path, monkeypatch, listed, on_disk, warnings, reason):
    from ltube.downloader import PruneSkipped

    downloader, files, events = make_pruning_downloader(tmp_path, monkeypatch, listed, on_disk, warnings)
    summary = downloader.run("https://x", directory=tmp_path / "Mix")

    assert summary.removed == []
    assert all(f.exists() for f in files.values())
    assert any(reason in e.reason for e in events if isinstance(e, PruneSkipped))


def test_prune_skipped_when_run_did_not_finish(tmp_path, monkeypatch):
    downloader, files, _ = make_pruning_downloader(tmp_path, monkeypatch, ["a", "new"], ["a", "gone"])
    downloader.stop.set()  # cancelled before the end
    summary = downloader.run("https://x", directory=tmp_path / "Mix")

    assert summary.aborted == "cancelled"
    assert files["gone"].exists()


def test_no_prune_unless_asked(tmp_path, monkeypatch):
    downloader, files, _ = make_pruning_downloader(tmp_path, monkeypatch, ["a"], ["a", "gone"])
    downloader.prune = False
    assert downloader.run("https://x", directory=tmp_path / "Mix").removed == []
    assert files["gone"].exists()


def make_resync(tmp_path, monkeypatch, playlist, pattern):
    """Resync `playlist` (video IDs) into tmp_path/Mix with `pattern`; downloading is forbidden."""
    from ltube import downloader as dl
    from ltube.backoff import BackoffConfig

    cookies = tmp_path / "cookies.txt"
    cookies.touch()
    events = []
    d = dl.PlaylistDownloader(
        tmp_path, cookies, BackoffConfig(sleep_min=0, sleep_max=0, request_sleep=0),
        patterns=dl.NamePatterns(playlist=pattern), on_event=events.append,
    )  # fmt: skip
    items = [dl._PlaylistItem(v, f"https://x/{v}", v) for v in playlist]
    monkeypatch.setattr(d, "_list_playlist", lambda url: ("Mix", items, False))
    monkeypatch.setattr(d, "_download_track", lambda *a: pytest.fail("existing tracks must not be re-downloaded"))
    return d, events


def tagged(folder, name, vid, **tags):
    from conftest import make_track

    folder.mkdir(exist_ok=True)
    make_track(folder / name, purl=f"https://www.youtube.com/watch?v={vid}", **tags)


def purl_of(path):
    import mutagen

    return mutagen.File(path).tags["purl"][0]


def test_resync_renames_to_new_pattern_and_positions(tmp_path, monkeypatch):
    folder = tmp_path / "Mix"
    tagged(folder, "01 - A - Song.opus", "aaaaaaaaaaa", artist="A", title="Song", date="2001")
    tagged(folder, "02 - B - Two.opus", "bbbbbbbbbbb", artist="B", title="Two")
    d, events = make_resync(tmp_path, monkeypatch, ["bbbbbbbbbbb", "aaaaaaaaaaa"], "{artist} - {title} ({year})")

    summary = d.run("https://x", directory=folder)

    assert sorted(p.name for p in folder.glob("*.opus")) == ["A - Song (2001).opus", "B - Two.opus"]
    assert {r.path.name for r in summary.results} == {"A - Song (2001).opus", "B - Two.opus"}
    assert (folder / "Mix.m3u8").read_text().count(".opus") == 2


def test_resync_swaps_numbers_without_clobbering(tmp_path, monkeypatch):
    folder = tmp_path / "Mix"
    tagged(folder, "01 - A - X.opus", "aaaaaaaaaaa", artist="A", title="X")
    tagged(folder, "02 - B - Y.opus", "bbbbbbbbbbb", artist="B", title="Y")
    d, _ = make_resync(tmp_path, monkeypatch, ["bbbbbbbbbbb", "aaaaaaaaaaa"], "{number} - {artist} - {title}")

    d.run("https://x", directory=folder)

    assert purl_of(folder / "01 - B - Y.opus").endswith("bbbbbbbbbbb")
    assert purl_of(folder / "02 - A - X.opus").endswith("aaaaaaaaaaa")
    assert not list(folder.glob(".ltube-rename-*"))


def test_resync_never_overwrites_a_file_it_does_not_manage(tmp_path, monkeypatch):
    from ltube.downloader import YtdlpWarning

    folder = tmp_path / "Mix"
    tagged(folder, "01 - A - X.opus", "aaaaaaaaaaa", artist="A", title="X")
    (folder / "A - X.opus").write_bytes(b"someone else's file")
    d, events = make_resync(tmp_path, monkeypatch, ["aaaaaaaaaaa"], "{artist} - {title}")

    d.run("https://x", directory=folder)

    assert (folder / "A - X.opus").read_bytes() == b"someone else's file"
    assert (folder / "01 - A - X.opus").exists()
    assert any("already exists" in e.message for e in events if isinstance(e, YtdlpWarning))


def test_resync_with_unchanged_pattern_renames_nothing(tmp_path, monkeypatch):
    from ltube.downloader import TrackRenamed

    folder = tmp_path / "Mix"
    tagged(folder, "01 - A - X.opus", "aaaaaaaaaaa", artist="A", title="X")
    d, events = make_resync(tmp_path, monkeypatch, ["aaaaaaaaaaa"], "{number} - {artist} - {title}")
    d.run("https://x", directory=folder)
    assert not [e for e in events if isinstance(e, TrackRenamed)]
