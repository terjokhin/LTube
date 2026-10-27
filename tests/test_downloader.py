import re

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
    base = tmp_path / "03 - Song 2.0 [aaaaaaaaaaa]"  # a dot in the name is not an extension
    assert _find_existing(base) is None
    (tmp_path / "03 - Song 2.0 [aaaaaaaaaaa].opus").touch()
    assert _find_existing(base) == tmp_path / "03 - Song 2.0 [aaaaaaaaaaa].opus"


class FakeYDL:
    """Stands in for YoutubeDL: runs the pre_process post-processors on `FakeYDL.info`, and
    "downloads" by writing an opus file into the output folder.
    """

    info: dict = {}
    fail: str | None = None
    downloads: list[str] = []
    delay = 0.0
    audio_source = None  # a real audio file to copy instead of writing junk bytes

    def __init__(self, opts):
        self.opts = opts
        self.pps = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add_post_processor(self, pp, when="post_process"):
        self.pps.append((pp, when))

    def add_progress_hook(self, hook):
        pass

    def extract_info(self, url, download=False):
        info = dict(self.info) | {"id": url.rsplit("/", 1)[-1]}
        for pp, when in self.pps:
            if when == "pre_process":
                _, info = pp.run(info)
        return info

    def process_ie_result(self, info, download=True):
        import time
        from pathlib import Path

        from yt_dlp.utils import DownloadError

        self.downloads.append(info["id"])
        out = Path(self.opts["outtmpl"]).parent
        if self.audio_source:
            import shutil

            shutil.copy(self.audio_source, out / f"{info['ltube_name']}.opus")
        else:
            (out / f"{info['ltube_name']}.opus").write_bytes(b"audio")
        (out / f"{info['ltube_name']}.f251.webm.part").touch()
        time.sleep(self.delay)
        if self.fail:
            raise DownloadError(self.fail)


@pytest.fixture
def ydl(monkeypatch):
    from types import SimpleNamespace

    from ltube import downloader as dl

    class Fake(FakeYDL):
        info = {"title": "Song", "artists": ["Artist"], "album": "Album", "duration": 12}
        fail = None
        downloads = []
        audio_source = None

    monkeypatch.setattr(dl, "YoutubeDL", Fake)
    monkeypatch.setattr(dl, "CoverArtPP", lambda ydl: SimpleNamespace())
    return Fake


def make_downloader(tmp_path, monkeypatch, ids, *, is_album=False, title="Mix", playlist_id=None, **kwargs):
    """A downloader over `ids` with the listing faked; YoutubeDL needs the `ydl` fixture."""
    from ltube import downloader as dl
    from ltube.backoff import BackoffConfig

    cookies = tmp_path / "cookies.txt"
    cookies.touch()
    events = []
    d = dl.PlaylistDownloader(
        tmp_path / "lib", cookies, BackoffConfig(sleep_min=0, sleep_max=0, request_sleep=0, retries=0),
        on_event=events.append, **kwargs,
    )  # fmt: skip
    items = [dl._PlaylistItem(i, f"https://x/{i}", f"Title {i}") for i in ids]
    listing = dl._Listing(playlist_id or f"PL-{title}", title, items, is_album)
    monkeypatch.setattr(d, "_list_playlist", lambda url: listing)
    d.events = events
    return d


def test_download_lands_in_artist_album_folder_and_the_index(tmp_path, monkeypatch, ydl):
    from ltube.downloader import Status
    from ltube.library import Library

    d = make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa"])
    summary = d.run("https://x")

    [result] = summary.results
    expected = tmp_path / "lib" / "Artist" / "Album" / "00 - Song [aaaaaaaaaaa].opus"
    assert (result.status, result.path) == (Status.DOWNLOADED, expected)
    assert expected.read_bytes() == b"audio"
    track = Library(tmp_path / "lib").get_track("aaaaaaaaaaa")
    assert (track.path, track.artist, track.title, track.album, track.duration_s) == (expected, "Artist", "Song", "Album", 12)
    # Nothing but the finished track and the index is left behind.
    assert sorted(p.name for p in expected.parent.iterdir()) == [expected.name]
    assert list((tmp_path / "lib" / ".ltube" / "tmp").iterdir()) == []


def test_track_already_in_the_library_is_not_requested_again(tmp_path, monkeypatch, ydl):
    from ltube.downloader import Status

    d = make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa", "bbbbbbbbbbb"])
    d.run("https://x/playlist")
    assert ydl.downloads == ["aaaaaaaaaaa", "bbbbbbbbbbb"]

    # Another playlist (or the same one again) holding one of those tracks and one new one.
    again = make_downloader(tmp_path, monkeypatch, ["bbbbbbbbbbb", "ccccccccccc"], title="Other")
    summary = again.run("https://x/other")

    assert ydl.downloads == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]  # b only once
    assert [r.status for r in summary.results] == [Status.EXISTS, Status.DOWNLOADED]
    assert summary.results[0].label == "Artist - Song"
    files = list((tmp_path / "lib").rglob("*.opus"))
    assert len(files) == 3


def test_second_run_downloads_nothing(tmp_path, monkeypatch, ydl):
    make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa", "bbbbbbbbbbb"]).run("https://x")
    ydl.downloads.clear()
    summary = make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa", "bbbbbbbbbbb"]).run("https://x")
    assert ydl.downloads == []
    assert {r.status.value for r in summary.results} == {"already exists"}


def test_deleted_file_is_downloaded_again(tmp_path, monkeypatch, ydl):
    make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa"]).run("https://x")
    next((tmp_path / "lib").rglob("*.opus")).unlink()
    make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa"]).run("https://x")
    assert ydl.downloads == ["aaaaaaaaaaa", "aaaaaaaaaaa"]


def test_file_missing_from_the_index_is_adopted_not_downloaded(tmp_path, monkeypatch, ydl):
    from ltube.downloader import Status
    from ltube.library import Library

    file = tmp_path / "lib" / "Artist" / "Album" / "00 - Song [aaaaaaaaaaa].opus"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"old")

    summary = make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa"]).run("https://x")

    assert ydl.downloads == []
    assert summary.results[0].status is Status.EXISTS and file.read_bytes() == b"old"
    assert Library(tmp_path / "lib").get_track("aaaaaaaaaaa").path == file


def test_failed_download_leaves_nothing_in_the_library(tmp_path, monkeypatch, ydl):
    from ltube.downloader import Status

    ydl.fail = "Unable to download: connection reset"
    summary = make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa"]).run("https://x")

    assert summary.results[0].status is Status.FAILED
    assert list((tmp_path / "lib").rglob("*.opus")) == []
    assert list((tmp_path / "lib").rglob("*.part")) == []  # the half-written file went too
    from ltube.library import Library

    assert Library(tmp_path / "lib").get_track("aaaaaaaaaaa") is None


def test_album_tracks_are_numbered_by_position_and_get_no_playlist_file(tmp_path, monkeypatch, ydl):
    from ltube.library import Library

    d = make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa", "bbbbbbbbbbb"], is_album=True, title="Album - Album")
    d.run("https://x")

    names = sorted(p.name for p in (tmp_path / "lib" / "Artist" / "Album").iterdir())
    assert names == ["01 - Song [aaaaaaaaaaa].opus", "02 - Song [bbbbbbbbbbb].opus"]
    assert Library(tmp_path / "lib").get_track("bbbbbbbbbbb").track_number == 2
    assert not (tmp_path / "lib" / "playlists").exists()


def test_playlist_file_lists_tracks_from_every_folder(tmp_path, monkeypatch, ydl):
    d = make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa", "bbbbbbbbbbb"])
    d.run("https://x")

    lines = (tmp_path / "lib" / "playlists" / "Mix.m3u8").read_text().splitlines()
    assert [l for l in lines if not l.startswith("#")] == [
        "../Artist/Album/00 - Song [aaaaaaaaaaa].opus",
        "../Artist/Album/00 - Song [bbbbbbbbbbb].opus",
    ]


def test_two_runs_at_once_download_a_shared_track_once(tmp_path, monkeypatch, ydl):
    import threading

    ydl.delay = 0.3
    first = make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa"], title="One")
    second = make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa", "bbbbbbbbbbb"], title="Two")
    errors = []

    def run(d):
        try:
            d.run("https://x")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run, args=(d,)) for d in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert errors == []
    assert sorted(ydl.downloads) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert len(list((tmp_path / "lib").rglob("*.opus"))) == 2
    assert list((tmp_path / "lib").rglob("*.part")) == []


def test_staging_leftovers_of_a_killed_run_are_cleared(tmp_path, monkeypatch, ydl):
    stale = tmp_path / "lib" / ".ltube" / "tmp" / "zzzzzzzzzzz"
    stale.mkdir(parents=True)
    (stale / "half.opus.part").touch()

    make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa"]).run("https://x")

    assert not stale.exists()


def test_empty_listing_fails_the_run_and_keeps_the_playlist_file(tmp_path, monkeypatch, ydl):
    from ltube import downloader as dl

    hidden = "YouTube said: INFO - 53 unavailable videos are hidden"
    d = make_downloader(tmp_path, monkeypatch, [])
    listing = dl._Listing("PL1", "Mix", [], False)
    monkeypatch.setattr(d, "_list_playlist", lambda url: (d._logger.warnings.append(hidden), listing)[1])
    playlist = tmp_path / "lib" / "playlists" / "Mix.m3u8"
    playlist.parent.mkdir(parents=True)
    playlist.write_text("#EXTM3U\nfrom an earlier, fuller run\n")

    summary = d.run("https://x")

    assert summary.aborted.startswith("YouTube listed no tracks") and "hid all of them" in summary.aborted
    assert "fuller run" in playlist.read_text()


def playlist_lines(tmp_path, name="Mix.m3u8"):
    lines = (tmp_path / "lib" / "playlists" / name).read_text().splitlines()
    return [re.search(r"\[(\w+)\]\.opus$", line)[1] for line in lines if not line.startswith("#")]


def remembered(tmp_path, playlist_id="PL1"):
    from ltube.library import Library

    return Library(tmp_path / "lib").playlist_items(playlist_id)


def test_playlist_follows_the_listing_but_files_stay(tmp_path, monkeypatch, ydl):
    make_downloader(tmp_path, monkeypatch, ["a", "b", "c"], playlist_id="PL1").run("https://x")
    assert playlist_lines(tmp_path) == ["a", "b", "c"]

    # b leaves the playlist and the order changes: the playlist file follows, the file b stays.
    make_downloader(tmp_path, monkeypatch, ["c", "a"], playlist_id="PL1").run("https://x")

    assert remembered(tmp_path) == ["c", "a"]
    assert playlist_lines(tmp_path) == ["c", "a"]
    assert len(list((tmp_path / "lib").rglob("*.opus"))) == 3
    assert ydl.downloads == ["a", "b", "c"]


def test_incomplete_listing_does_not_drop_remembered_tracks(tmp_path, monkeypatch, ydl):
    from ltube.downloader import YtdlpWarning

    make_downloader(tmp_path, monkeypatch, ["a", "b", "c"], playlist_id="PL1").run("https://x")
    d = make_downloader(tmp_path, monkeypatch, ["a", "c"], playlist_id="PL1")
    d._logger.warnings.append("YouTube said: INFO - 1 unavailable video is hidden")

    d.run("https://x")

    assert remembered(tmp_path) == ["a", "b", "c"]
    assert playlist_lines(tmp_path) == ["a", "b", "c"]
    assert any("kept 1 tracks" in e.message for e in d.events if isinstance(e, YtdlpWarning))


def test_listing_that_loses_most_of_the_playlist_is_not_believed(tmp_path, monkeypatch, ydl):
    from ltube.downloader import YtdlpWarning

    make_downloader(tmp_path, monkeypatch, ["a", "b", "c", "d"], playlist_id="PL1").run("https://x")
    d = make_downloader(tmp_path, monkeypatch, ["a"], playlist_id="PL1")

    d.run("https://x")

    assert remembered(tmp_path) == ["a", "b", "c", "d"]
    assert any("3 of 4 tracks vanished" in e.message for e in d.events if isinstance(e, YtdlpWarning))


def test_failed_tracks_keep_their_place_in_the_playlist(tmp_path, monkeypatch, ydl):
    make_downloader(tmp_path, monkeypatch, ["a"], playlist_id="PL1").run("https://x")
    ydl.fail = "Unable to download: connection reset"

    make_downloader(tmp_path, monkeypatch, ["a", "b"], playlist_id="PL1").run("https://x")

    assert remembered(tmp_path) == ["a", "b"]
    assert playlist_lines(tmp_path) == ["a"]  # b has no file yet

    ydl.fail = None
    make_downloader(tmp_path, monkeypatch, ["a", "b"], playlist_id="PL1").run("https://x")
    assert playlist_lines(tmp_path) == ["a", "b"]


def test_cancelled_run_still_writes_the_whole_playlist_so_far(tmp_path, monkeypatch, ydl):
    make_downloader(tmp_path, monkeypatch, ["a"], playlist_id="PL1").run("https://x")
    d = make_downloader(tmp_path, monkeypatch, ["a", "b"], playlist_id="PL1")
    d.stop.set()

    assert d.run("https://x").aborted == "cancelled"

    assert playlist_lines(tmp_path) == ["a"]  # the earlier run's track is not lost
    assert remembered(tmp_path) == ["a", "b"]


def test_renamed_playlist_gets_a_new_file_and_loses_the_old_one(tmp_path, monkeypatch, ydl):
    make_downloader(tmp_path, monkeypatch, ["a"], title="Old name", playlist_id="PL1").run("https://x")
    make_downloader(tmp_path, monkeypatch, ["a"], title="New name", playlist_id="PL1").run("https://x")

    assert sorted(p.name for p in (tmp_path / "lib" / "playlists").iterdir()) == ["New name.m3u8"]


def test_playlists_with_the_same_title_get_separate_files(tmp_path, monkeypatch, ydl):
    make_downloader(tmp_path, monkeypatch, ["a"], playlist_id="PL1").run("https://x")
    make_downloader(tmp_path, monkeypatch, ["b"], playlist_id="PL2").run("https://x")

    assert sorted(p.name for p in (tmp_path / "lib" / "playlists").iterdir()) == ["Mix (PL2).m3u8", "Mix.m3u8"]
    assert playlist_lines(tmp_path) == ["a"] and playlist_lines(tmp_path, "Mix (PL2).m3u8") == ["b"]


@pytest.fixture
def real_audio(tmp_path, ydl):
    from conftest import make_track

    source = tmp_path / "template.opus"
    make_track(source, title="Song", artist="Artist")
    ydl.audio_source = source


def file_single_then_album(tmp_path, monkeypatch, ydl, **album_info):
    """A track downloaded on its own (no album), then an album run listing it second."""
    ydl.info = {"title": "Song", "artists": ["Artist"]}
    make_downloader(tmp_path, monkeypatch, ["aaaaaaaaaaa"]).run("https://x")
    ydl.info = {"title": "Other", "artists": ["Artist"], "album": "Disc"}
    album = make_downloader(tmp_path, monkeypatch, ["bbbbbbbbbbb", "aaaaaaaaaaa"], is_album=True, title="Album - Disc")
    info = {"album": "Disc", "artists": ["Artist"], "release_year": 2001} | album_info
    monkeypatch.setattr(album, "_track_info", lambda url: info)
    return album


def test_single_is_filed_under_its_album_when_an_album_run_meets_it(tmp_path, monkeypatch, ydl, real_audio):
    import mutagen

    from ltube.downloader import Status, TrackMoved
    from ltube.library import Library

    album = file_single_then_album(tmp_path, monkeypatch, ydl)
    singles = tmp_path / "lib" / "Artist" / "Singles"
    assert (singles / "00 - Song [aaaaaaaaaaa].opus").exists()

    summary = album.run("https://x")

    moved = tmp_path / "lib" / "Artist" / "Disc" / "02 - Song [aaaaaaaaaaa].opus"
    assert ydl.downloads == ["aaaaaaaaaaa", "bbbbbbbbbbb"]  # the single was not downloaded again
    assert moved.exists() and not singles.exists()  # and the empty Singles folder went
    assert [(r.status, r.path.name) for r in summary.results] == [
        (Status.DOWNLOADED, "01 - Other [bbbbbbbbbbb].opus"),
        (Status.EXISTS, moved.name),
    ]
    assert [(e.old.name, e.new) for e in album.events if isinstance(e, TrackMoved)] == [
        ("00 - Song [aaaaaaaaaaa].opus", moved)
    ]
    tags = mutagen.File(moved).tags
    assert tags["album"] == ["Disc"] and tags["albumartist"] == ["Artist"] and tags["date"] == ["2001"]
    assert (tags["tracknumber"], tags["tracktotal"], tags["title"]) == (["2"], ["2"], ["Song"])
    track = Library(tmp_path / "lib").get_track("aaaaaaaaaaa")
    assert (track.path, track.album, track.album_artist, track.track_number) == (moved, "Disc", "Artist", 2)


def test_single_stays_put_when_its_album_spot_is_taken(tmp_path, monkeypatch, ydl):
    from ltube.downloader import TrackMoved, YtdlpWarning

    album = file_single_then_album(tmp_path, monkeypatch, ydl)
    taken = tmp_path / "lib" / "Artist" / "Disc" / "02 - Song [aaaaaaaaaaa].opus"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"someone else's file")

    album.run("https://x")

    assert (tmp_path / "lib" / "Artist" / "Singles" / "00 - Song [aaaaaaaaaaa].opus").exists()
    assert taken.read_bytes() == b"someone else's file"
    assert not [e for e in album.events if isinstance(e, TrackMoved)]
    assert any("already exists" in e.message for e in album.events if isinstance(e, YtdlpWarning))


def test_single_stays_put_when_it_cannot_be_tagged(tmp_path, monkeypatch, ydl):
    from ltube.downloader import TrackMoved, YtdlpWarning

    album = file_single_then_album(tmp_path, monkeypatch, ydl)  # the fake wrote junk, not audio

    summary = album.run("https://x")

    assert (tmp_path / "lib" / "Artist" / "Singles" / "00 - Song [aaaaaaaaaaa].opus").exists()
    assert summary.results[1].path.parent.name == "Singles"  # still counted as in the library
    assert any("left 00 - Song" in e.message for e in album.events if isinstance(e, YtdlpWarning))
    assert not [e for e in album.events if isinstance(e, TrackMoved)]


def test_album_is_only_looked_up_when_a_track_needs_it(tmp_path, monkeypatch, ydl):
    make_downloader(tmp_path, monkeypatch, ["a"], is_album=True, title="Album - Disc").run("https://x")
    again = make_downloader(tmp_path, monkeypatch, ["a"], is_album=True, title="Album - Disc")
    monkeypatch.setattr(again, "_track_info", lambda url: pytest.fail("every track is filed already"))

    again.run("https://x")


def make_listing_downloader(tmp_path, monkeypatch, signed_in, signed_out):
    """A downloader whose signed-in / signed-out listings return these infos (or raise)."""
    from ltube import downloader as dl
    from ltube.backoff import BackoffConfig

    cookies = tmp_path / "cookies.txt"
    cookies.touch()
    events = []
    d = dl.PlaylistDownloader(tmp_path, cookies, BackoffConfig(), on_event=events.append)
    calls = []

    def fake_extract(url, *, signed_in):
        calls.append(signed_in)
        result = signed_in_info if signed_in else signed_out
        if isinstance(result, Exception):
            raise result
        if signed_in:
            d._logger.warnings.append("YouTube said: INFO - 3 unavailable videos are hidden")
        return result

    signed_in_info = signed_in
    monkeypatch.setattr(d, "_extract_listing", fake_extract)
    return d, events, calls


def entries(*ids, titled=True):
    return [{"id": i, "title": f"T-{i}" if titled else None} for i in ids]


def test_hidden_private_tracks_are_recovered_from_a_signed_out_listing(tmp_path, monkeypatch):
    from ltube.downloader import _HIDDEN_UNAVAILABLE_RE, YtdlpWarning

    signed_in = {"id": "PL1", "title": "Mix", "playlist_count": 3, "entries": entries("b")}
    signed_out = {"id": "PL1", "title": "Mix", "playlist_count": 3, "entries": entries("a", "b", "c", titled=False)}
    d, events, calls = make_listing_downloader(tmp_path, monkeypatch, signed_in, signed_out)

    items = d._list_playlist("https://x").items

    assert calls == [True, False]
    assert [(i.id, i.title) for i in items] == [("a", "a"), ("b", "T-b"), ("c", "c")]
    assert any("hid 2 tracks" in e.message for e in events if isinstance(e, YtdlpWarning))
    # Complete now, so the hidden-videos warning must not block pruning.
    assert not any(_HIDDEN_UNAVAILABLE_RE.search(w) for w in d._logger.warnings)


def test_private_playlist_keeps_signed_in_listing(tmp_path, monkeypatch):
    from yt_dlp.utils import DownloadError

    signed_in = {"id": "PL1", "title": "Mix", "playlist_count": 3, "entries": entries("b")}
    d, _, calls = make_listing_downloader(tmp_path, monkeypatch, signed_in, DownloadError("ERROR: playlist does not exist"))

    items = d._list_playlist("https://x").items

    assert calls == [True, False]
    assert [i.id for i in items] == ["b"]
    assert d._logger.warnings  # still incomplete: pruning stays blocked


def test_no_second_listing_when_nothing_is_hidden(tmp_path, monkeypatch):
    full = {"id": "PL1", "title": "Mix", "playlist_count": 2, "entries": entries("a", "b")}
    d, _, calls = make_listing_downloader(tmp_path, monkeypatch, full, None)

    d._list_playlist("https://x")

    assert calls == [True]


def test_hidden_tracks_come_from_youtube_music_when_available(tmp_path, monkeypatch):
    from ltube import ytmusic

    signed_in = {"id": "PL1", "title": "Mix", "playlist_count": 2, "entries": []}
    d, events, calls = make_listing_downloader(tmp_path, monkeypatch, signed_in, None)
    d._cookie_copy = tmp_path / "cookies.txt"
    music = [ytmusic.MusicTrack("a", "Song A", ["ZAPRAVKA"], "LP"), ytmusic.MusicTrack("b", "Song B", ["ZAPRAVKA"], None)]
    monkeypatch.setattr(ytmusic, "music_playlist", lambda cookiefile, playlist_id: music if playlist_id == "PL1" else None)

    items = d._list_playlist("https://x").items

    assert calls == [True]  # no signed-out listing needed
    assert [(i.id, i.title) for i in items] == [("a", "Song A"), ("b", "Song B")]
    assert d._music["a"].artists == ["ZAPRAVKA"]
