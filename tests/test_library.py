import sqlite3
import threading

import pytest

from ltube.library import META_DIR, SCHEMA_VERSION, Library


def audio(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    return path


def test_creates_index_folder_that_media_servers_ignore(tmp_path):
    Library(tmp_path)
    assert (tmp_path / META_DIR / "library.db").exists()
    assert (tmp_path / META_DIR / ".ndignore").exists()


def test_unknown_track_is_none(tmp_path):
    assert Library(tmp_path).get_track("nope") is None


def test_added_track_is_found_with_absolute_path_and_tags(tmp_path):
    lib = Library(tmp_path)
    file = audio(tmp_path, "Artist/Album/01 - Song [aaa].m4a")
    lib.add_track("aaa", file, artist="Artist", title="Song", album="Album", track_number=1, duration_s=12.5)
    track = lib.get_track("aaa")
    assert track.path == file
    assert (track.artist, track.title, track.album, track.track_number, track.duration_s) == (
        "Artist", "Song", "Album", 1, 12.5,
    )  # fmt: skip
    assert track.album_artist is None


def test_row_is_dropped_when_its_file_is_gone(tmp_path):
    lib = Library(tmp_path)
    file = audio(tmp_path, "a.m4a")
    lib.add_track("aaa", file)
    file.unlink()
    assert lib.get_track("aaa") is None
    file.write_bytes(b"back")
    assert lib.get_track("aaa") is None  # forgotten for good, not just hidden while missing


def test_adding_a_known_id_replaces_the_file_and_keeps_added_at(tmp_path):
    lib = Library(tmp_path)
    lib.add_track("aaa", audio(tmp_path, "old.m4a"), title="Old")
    before = lib.get_track("aaa").added_at
    new = audio(tmp_path, "new.m4a")
    lib.add_track("aaa", new, title="New")
    track = lib.get_track("aaa")
    assert (track.path, track.title, track.added_at) == (new, "New", before)


def test_path_outside_the_library_is_rejected(tmp_path):
    lib = Library(tmp_path / "lib")
    with pytest.raises(ValueError):
        lib.add_track("aaa", tmp_path / "elsewhere.m4a")


def test_unknown_tag_is_rejected(tmp_path):
    with pytest.raises(TypeError):
        Library(tmp_path).add_track("aaa", tmp_path / "a.m4a", genre="x")


def test_move_updates_path_and_only_the_given_tags(tmp_path):
    lib = Library(tmp_path)
    lib.add_track("aaa", audio(tmp_path, "Artist/Singles/00 - Song [aaa].m4a"), artist="Artist", title="Song")
    moved = audio(tmp_path, "Artist/Album/03 - Song [aaa].m4a")
    lib.move_track("aaa", moved, album="Album", track_number=3)
    track = lib.get_track("aaa")
    assert (track.path, track.album, track.track_number, track.title) == (moved, "Album", 3, "Song")


def test_moving_an_unknown_track_fails(tmp_path):
    with pytest.raises(KeyError):
        Library(tmp_path).move_track("nope", tmp_path / "a.m4a")


def test_playlist_keeps_its_order(tmp_path):
    lib = Library(tmp_path)
    for vid in "abc":
        lib.add_track(vid, audio(tmp_path, f"{vid}.m4a"))
    lib.set_playlist_items("PL1", "Mix", ["c", "a", "b"], m3u_name="Mix.m3u8")
    assert lib.playlist_items("PL1") == ["c", "a", "b"]
    assert [t.video_id for t in lib.playlist_tracks("PL1")] == ["c", "a", "b"]
    playlist = lib.get_playlist("PL1")
    assert (playlist.title, playlist.m3u_name) == ("Mix", "Mix.m3u8")
    assert playlist.last_synced_at is not None


def test_set_replaces_items_and_keeps_the_m3u_name_unless_given(tmp_path):
    lib = Library(tmp_path)
    lib.set_playlist_items("PL1", "Mix", ["a", "b"], m3u_name="Mix.m3u8")
    lib.set_playlist_items("PL1", "Renamed", ["b"])
    assert lib.playlist_items("PL1") == ["b"]
    playlist = lib.get_playlist("PL1")
    assert (playlist.title, playlist.m3u_name) == ("Renamed", "Mix.m3u8")


def test_playlist_tracks_skip_tracks_not_downloaded_or_gone(tmp_path):
    lib = Library(tmp_path)
    kept = audio(tmp_path, "a.m4a")
    lib.add_track("a", kept)
    lib.add_track("b", audio(tmp_path, "b.m4a"))
    (tmp_path / "b.m4a").unlink()
    lib.set_playlist_items("PL1", "Mix", ["a", "b", "never"])
    assert [t.path for t in lib.playlist_tracks("PL1")] == [kept]
    assert lib.playlist_items("PL1") == ["a", "b", "never"]  # the order is remembered regardless


def test_same_track_twice_in_a_playlist(tmp_path):
    lib = Library(tmp_path)
    lib.add_track("a", audio(tmp_path, "a.m4a"))
    lib.set_playlist_items("PL1", "Mix", ["a", "a"])
    assert [t.video_id for t in lib.playlist_tracks("PL1")] == ["a", "a"]


@pytest.mark.parametrize(
    "old, listed, expected",
    [
        (["a", "b", "c"], ["a", "c"], ["a", "b", "c"]),  # b hidden: stays between its neighbours
        (["a", "b", "c"], ["b", "c"], ["a", "b", "c"]),  # the first one hidden
        (["a", "b", "c"], ["a", "b"], ["a", "b", "c"]),  # the last one hidden
        (["a", "b", "c"], ["c", "a", "x"], ["c", "a", "b", "x"]),  # reordered: b follows a, as before
        (["a", "b", "c"], [], ["a", "b", "c"]),
        ([], ["a"], ["a"]),
    ],
)
def test_merge_keeps_old_items_missing_from_the_listing(tmp_path, old, listed, expected):
    lib = Library(tmp_path)
    lib.set_playlist_items("PL1", "Mix", old)
    lib.merge_playlist_items("PL1", "Mix", listed)
    assert lib.playlist_items("PL1") == expected


def test_playlists_do_not_mix(tmp_path):
    lib = Library(tmp_path)
    lib.set_playlist_items("PL1", "One", ["a"])
    lib.set_playlist_items("PL2", "Two", ["b", "c"])
    lib.set_playlist_items("PL1", "One", ["a", "d"])
    assert lib.playlist_items("PL2") == ["b", "c"]


def test_a_newer_index_is_not_touched(tmp_path):
    Library(tmp_path)
    db = sqlite3.connect(tmp_path / META_DIR / "library.db")
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="newer ltube"):
        Library(tmp_path)


def test_reopening_keeps_the_data(tmp_path):
    Library(tmp_path).add_track("aaa", audio(tmp_path, "a.m4a"), title="Song")
    assert Library(tmp_path).get_track("aaa").title == "Song"


def test_lock_makes_a_second_run_wait(tmp_path):
    order = []
    holding, release = threading.Event(), threading.Event()

    def first():
        with Library(tmp_path).lock():
            order.append("first in")
            holding.set()
            release.wait(5)
            order.append("first out")

    def second():
        holding.wait(5)
        with Library(tmp_path).lock():  # a separate Library, as a separate process would have
            order.append("second in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    holding.wait(5)
    threading.Event().wait(0.3)  # give the second run time to (wrongly) get in
    assert order == ["first in"]
    release.set()
    for thread in threads:
        thread.join(5)
    assert order == ["first in", "first out", "second in"]


def test_opening_a_new_library_from_two_threads_at_once(tmp_path):
    errors = []
    for attempt in range(20):
        root = tmp_path / str(attempt)
        barrier = threading.Barrier(2)

        def open_library():
            barrier.wait(5)
            try:
                Library(root)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=open_library) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
    assert errors == []


def test_playlist_is_found_by_its_m3u_name(tmp_path):
    lib = Library(tmp_path)
    lib.set_playlist_items("PL1", "Mix", ["a"], m3u_name="Mix.m3u8")
    assert lib.playlist_with_m3u_name("Mix.m3u8").playlist_id == "PL1"
    assert lib.playlist_with_m3u_name("Other.m3u8") is None
