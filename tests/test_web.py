import threading
import time

import pytest
from fastapi.testclient import TestClient

from ltube.downloader import PlaylistStarted, Status, Summary, TrackFinished, TrackResult, TrackStarted
from ltube.web.app import create_app

COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"


def quick_runner(job, on_event, stop):
    on_event(PlaylistStarted("Mix", job.out_dir / "Mix", 1))
    on_event(TrackStarted(1, 1, "Song"))
    result = TrackResult(Status.DOWNLOADED, job.out_dir / "Mix" / "01 - A - Song.opus", "A - Song")
    on_event(TrackFinished(1, result))
    return Summary("Mix", job.out_dir / "Mix", 1, [result])


def blocking_runner(job, on_event, stop):
    stop.wait(5)
    return Summary("Mix", job.out_dir, 0, aborted="cancelled")


@pytest.fixture
def make_client(tmp_path):
    def make(runner=quick_runner, cookies=True, lock=False):
        data = tmp_path / "data"
        if cookies:
            data.mkdir(exist_ok=True)
            (data / "cookies.txt").write_text(COOKIES)
        return TestClient(create_app(out_dir=tmp_path / "music", data_dir=data, lock_out_dir=lock, runner=runner))

    return make


def wait_for_state(client, job_id, state):
    for _ in range(200):
        job = next(j for j in client.get("/api/jobs").json() if j["id"] == job_id)
        if job["state"] == state:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job stayed {job['state']}, expected {state}")


def test_cookies_upload_is_validated_and_private(make_client, tmp_path):
    client = make_client(cookies=False)
    assert client.get("/api/settings").json()["cookies"]["present"] is False

    assert client.put("/api/settings/cookies", content="hello").status_code == 422
    no_youtube = COOKIES.replace(".youtube.com", ".example.com")
    assert client.put("/api/settings/cookies", content=no_youtube).status_code == 422

    res = client.put("/api/settings/cookies", content=COOKIES)
    assert res.status_code == 200
    assert res.json()["loaded"] == 1
    assert (tmp_path / "data" / "cookies.txt").stat().st_mode & 0o777 == 0o600
    assert client.get("/api/settings").json()["cookies"]["present"] is True

    client.delete("/api/settings/cookies")
    assert client.get("/api/settings").json()["cookies"]["present"] is False


def test_job_needs_cookies(make_client):
    client = make_client(cookies=False)
    assert client.post("/api/jobs", json={"url": "https://x"}).status_code == 409


def test_download_folder_setting_is_saved(make_client, tmp_path):
    client = make_client()
    assert client.get("/api/settings").json()["out_dir"] == str(tmp_path / "music")

    target = tmp_path / "library"
    res = client.put("/api/settings", json={"out_dir": str(target)})
    assert res.status_code == 200
    assert res.json()["out_dir"] == str(target.resolve())
    assert target.is_dir()
    # Survives a server restart.
    assert make_client().get("/api/settings").json()["out_dir"] == str(target.resolve())


@pytest.mark.parametrize("value", ["", "relative/path"])
def test_download_folder_must_be_absolute(make_client, value):
    assert make_client().put("/api/settings", json={"out_dir": value}).status_code == 422


def test_download_folder_rejects_a_file(make_client, tmp_path):
    (tmp_path / "file").touch()
    assert make_client().put("/api/settings", json={"out_dir": str(tmp_path / "file")}).status_code == 422


def test_locked_download_folder_ignores_saved_setting(make_client, tmp_path):
    make_client().put("/api/settings", json={"out_dir": str(tmp_path / "elsewhere")})
    client = make_client(lock=True)
    settings = client.get("/api/settings").json()
    assert settings["out_dir"] == str(tmp_path / "music")
    assert settings["out_dir_locked"] is True
    assert client.put("/api/settings", json={"out_dir": str(tmp_path / "x")}).status_code == 409


def test_job_runs_into_settings_folder(make_client, tmp_path):
    client = make_client()
    client.put("/api/settings", json={"out_dir": str(tmp_path / "library")})
    res = client.post("/api/jobs", json={"url": "https://music.youtube.com/playlist?list=1"})
    assert res.status_code == 201
    assert res.json()["out_dir"] == str((tmp_path / "library").resolve())

    job = wait_for_state(client, res.json()["id"], "done")
    assert job["title"] == "Mix"
    assert job["counts"] == {"downloaded": 1}
    assert job["tracks"][0]["file"] == "01 - A - Song.opus"


def test_invalid_url_is_rejected(make_client):
    assert make_client().post("/api/jobs", json={"url": "ftp://x"}).status_code == 422


def test_pacing_settings_default_validate_and_reach_jobs(make_client):
    seen = []
    client = make_client(runner=lambda job, on_event, stop: seen.append(job.backoff) or quick_runner(job, on_event, stop))
    settings = client.get("/api/settings").json()
    assert (settings["sleep_min"], settings["sleep_max"], settings["retries"], settings["rate_limit"]) == (5, 15, 5, "")
    assert settings["pacing_defaults"]["backoff_max"] == 600

    for bad in [{"sleep_min": 20}, {"backoff_base": 0}, {"retries": -1}, {"max_consecutive_failures": 0},
                {"rate_limit": "fast"}, {"backoff_max": 5}]:  # fmt: skip
        assert client.put("/api/settings", json=bad).status_code == 422, bad

    res = client.put("/api/settings", json={"sleep_min": 20, "sleep_max": 40, "rate_limit": " 2M "})
    assert res.status_code == 200
    assert res.json()["rate_limit"] == "2M"
    job = client.post("/api/jobs", json={"url": "https://x"}).json()
    wait_for_state(client, job["id"], "done")
    assert (seen[0].sleep_min, seen[0].sleep_max, seen[0].rate_limit) == (20, 40, 2 * 1024**2)


def test_cancel_running_and_queued_jobs(make_client):
    client = make_client(runner=blocking_runner)
    first = client.post("/api/jobs", json={"url": "https://a"}).json()["id"]
    second = client.post("/api/jobs", json={"url": "https://b"}).json()["id"]
    wait_for_state(client, first, "running")

    assert client.delete(f"/api/jobs/{second}").json()["state"] == "cancelled"
    client.delete(f"/api/jobs/{first}")
    wait_for_state(client, first, "cancelled")
    assert client.delete("/api/jobs/nope").status_code == 404


def test_downloader_stops_between_tracks(tmp_path, monkeypatch):
    from ltube.backoff import BackoffConfig
    from ltube.downloader import PlaylistDownloader, _Listing, _PlaylistItem

    cookies = tmp_path / "cookies.txt"
    cookies.write_text(COOKIES)
    stop = threading.Event()
    downloader = PlaylistDownloader(tmp_path, cookies, BackoffConfig(sleep_min=0, sleep_max=0, request_sleep=0), stop=stop)
    items = [_PlaylistItem(f"id{i}", f"https://x/{i}", f"Song {i}") for i in range(3)]
    monkeypatch.setattr(downloader, "_list_playlist", lambda url: _Listing("PL1", "Mix", items, False))

    def fake_track(url, number):
        stop.set()  # the user cancels while the first track downloads
        file = tmp_path / "A" / "LP" / "01 - Song.opus"
        file.parent.mkdir(parents=True)
        file.touch()
        downloader._library.add_track(downloader._current_id, file, artist="A", title="Song")
        return TrackResult(Status.DOWNLOADED, file, "A - Song", 10)

    monkeypatch.setattr(downloader, "_download_track", fake_track)
    summary = downloader.run("https://x")

    assert summary.aborted == "cancelled"
    assert len(summary.results) == 1
    assert (tmp_path / "playlists" / "Mix.m3u8").read_text().count(".opus") == 1


def test_downloading_a_playlist_again_reuses_its_record(make_client):
    seen = []
    client = make_client(runner=lambda job, on_event, stop: seen.append(job.playlist_id) or quick_runner(job, on_event, stop))
    job = client.post("/api/jobs", json={"url": "https://music.youtube.com/playlist?list=PL123&si=abc"}).json()
    wait_for_state(client, job["id"], "done")

    [record] = client.get("/api/playlists").json()
    assert (record["key"], record["favourite"]) == ("PL123", False)
    assert (record["title"], record["last_state"]) == ("Mix", "done")
    assert record["last_counts"] == {"downloaded": 1}
    assert "directory" not in record  # tracks live in the shared library, not in a folder per playlist

    # The same playlist under another URL form is the same record.
    again = client.post("/api/jobs", json={"url": "https://www.youtube.com/playlist?list=PL123"}).json()
    assert again["playlist_id"] == record["id"]
    wait_for_state(client, again["id"], "done")
    assert seen == [record["id"], record["id"]]
    assert len(client.get("/api/playlists").json()) == 1


def test_favourites_are_a_flag_on_remembered_playlists(make_client):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "https://x/playlist?list=PL1", "favourite": True}).json()
    wait_for_state(client, job["id"], "done")
    [record] = client.get("/api/playlists").json()
    assert record["favourite"] is True

    sync = client.post(f"/api/playlists/{record['id']}/sync").json()
    wait_for_state(client, sync["id"], "done")

    # Un-favouriting keeps the record for later re-downloads.
    assert client.put(f"/api/playlists/{record['id']}", json={"favourite": False}).json()["favourite"] is False
    assert [p["id"] for p in client.get("/api/playlists").json()] == [record["id"]]
    assert client.put("/api/playlists/nope", json={"favourite": True}).status_code == 404
    assert client.post("/api/playlists/nope/sync").status_code == 404


def test_same_playlist_cannot_run_twice_at_once(make_client):
    client = make_client(runner=blocking_runner)
    job = client.post("/api/jobs", json={"url": "https://x/playlist?list=PL1"}).json()
    assert client.post("/api/jobs", json={"url": "https://x/playlist?list=PL1"}).status_code == 409
    assert client.post(f"/api/playlists/{job['playlist_id']}/sync").status_code == 409
    client.delete(f"/api/jobs/{job['id']}")


def test_legacy_favourites_are_migrated(make_client, tmp_path):
    import json

    data = tmp_path / "data"
    data.mkdir()
    legacy = [{"id": "f1", "key": "PL9", "url": "https://x/playlist?list=PL9", "directory": "/m/Fedor", "title": "Fedor",
               "added_at": 1.0, "last_synced_at": None, "last_state": None, "last_counts": {}, "last_error": None}]  # fmt: skip
    (data / "favourites.json").write_text(json.dumps(legacy))

    [record] = make_client().get("/api/playlists").json()
    assert (record["id"], record["favourite"], record["title"]) == ("f1", True, "Fedor")
    assert not (data / "favourites.json").exists()


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://music.youtube.com/playlist?list=OLAK5uy_abc&si=x", "OLAK5uy_abc"),
        ("https://www.youtube.com/watch?v=xyz&list=PL9", "PL9"),
        ("https://music.youtube.com/browse/MPREb_gTAcphH99wE", "MPREb_gTAcphH99wE"),
    ],
)
def test_playlist_key(url, key):
    from ltube.web.playlists import playlist_key

    assert playlist_key(url) == key


def test_track_pattern_setting(make_client):
    seen = []
    client = make_client(runner=lambda job, on_event, stop: seen.append(job.pattern) or quick_runner(job, on_event, stop))
    assert client.get("/api/settings").json()["track_pattern"] == "{number} - {title} [{id}]"

    for bad in ["{number} - {title}", "{title}", "{id} {genre} {title}", "{artist}/{title} [{id}]", "  "]:
        assert client.put("/api/settings", json={"track_pattern": bad}).status_code == 422, bad
    res = client.put("/api/settings", json={"track_pattern": " {artist} - {title} [{id}] "})
    assert res.json()["track_pattern"] == "{artist} - {title} [{id}]"

    job = client.post("/api/jobs", json={"url": "https://x"}).json()
    wait_for_state(client, job["id"], "done")
    assert seen == ["{artist} - {title} [{id}]"]


def test_locked_folder_still_allows_the_track_pattern(make_client, tmp_path):
    client = make_client(lock=True)
    # The UI sends the unchanged (locked) folder along with the other settings.
    res = client.put("/api/settings", json={"out_dir": str(tmp_path / "music"), "track_pattern": "{title} [{id}]"})
    assert res.status_code == 200
    assert res.json()["track_pattern"] == "{title} [{id}]"


def test_settings_of_the_old_folder_layout_are_ignored(make_client, tmp_path):
    import json

    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps(
        {"use_subfolders": True, "albums_subfolder": "LPs", "album_pattern": "{artist}", "delete_removed": True, "retries": 2}
    ))  # fmt: skip
    settings = make_client().get("/api/settings").json()
    assert settings["retries"] == 2 and settings["track_pattern"] == "{number} - {title} [{id}]"
    assert not {"use_subfolders", "albums_subfolder", "album_pattern", "delete_removed"} & set(settings)
