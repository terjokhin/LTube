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
    from ltube.downloader import PlaylistDownloader, _PlaylistItem

    cookies = tmp_path / "cookies.txt"
    cookies.write_text(COOKIES)
    stop = threading.Event()
    downloader = PlaylistDownloader(tmp_path, cookies, BackoffConfig(sleep_min=0, sleep_max=0, request_sleep=0), stop=stop)
    items = [_PlaylistItem(f"id{i}", f"https://x/{i}", f"Song {i}") for i in range(3)]
    monkeypatch.setattr(downloader, "_list_playlist", lambda url: ("Mix", items, False))

    def fake_track(url, playlist_dir, prefix):
        stop.set()  # the user cancels while the first track downloads
        return TrackResult(Status.DOWNLOADED, playlist_dir / f"{prefix}.opus", "A - Song", 10)

    monkeypatch.setattr(downloader, "_download_track", fake_track)
    summary = downloader.run("https://x")

    assert summary.aborted == "cancelled"
    assert len(summary.results) == 1
    assert (tmp_path / "Mix" / "Mix.m3u8").read_text().count(".opus") == 1


def test_redownload_acts_as_resync_into_the_first_folder(make_client, tmp_path):
    seen = []

    def runner(job, on_event, stop):
        seen.append((job.target_dir, job.prune))
        return quick_runner(job, on_event, stop)

    client = make_client(runner=runner)
    client.put("/api/settings", json={"delete_removed": True})
    job = client.post("/api/jobs", json={"url": "https://music.youtube.com/playlist?list=PL123&si=abc"}).json()
    wait_for_state(client, job["id"], "done")

    [record] = client.get("/api/playlists").json()
    assert (record["key"], record["favourite"]) == ("PL123", False)
    assert (record["title"], record["directory"], record["last_state"]) == ("Mix", str(tmp_path / "music" / "Mix"), "done")
    assert record["last_counts"] == {"downloaded": 1}

    # Settings change later; downloading the same playlist (any URL form) again resyncs the first folder.
    client.put("/api/settings", json={"out_dir": str(tmp_path / "elsewhere"), "use_subfolders": True})
    again = client.post("/api/jobs", json={"url": "https://www.youtube.com/playlist?list=PL123"}).json()
    assert again["playlist_id"] == record["id"]
    wait_for_state(client, again["id"], "done")
    # The first run has nothing to prune; the re-download does.
    assert seen == [(None, False), (tmp_path / "music" / "Mix", True)]
    assert len(client.get("/api/playlists").json()) == 1


def test_favourites_are_a_flag_on_remembered_playlists(make_client, tmp_path):
    seen = []
    client = make_client(runner=lambda job, on_event, stop: seen.append(job.target_dir) or quick_runner(job, on_event, stop))
    job = client.post("/api/jobs", json={"url": "https://x/playlist?list=PL1", "favourite": True}).json()
    wait_for_state(client, job["id"], "done")
    [record] = client.get("/api/playlists").json()
    assert record["favourite"] is True

    sync = client.post(f"/api/playlists/{record['id']}/sync").json()
    wait_for_state(client, sync["id"], "done")
    assert seen == [None, tmp_path / "music" / "Mix"]

    # Un-favouriting keeps the record (and its folder) for later re-downloads.
    assert client.put(f"/api/playlists/{record['id']}", json={"favourite": False}).json()["favourite"] is False
    assert client.get("/api/playlists").json()[0]["directory"] == str(tmp_path / "music" / "Mix")
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
    assert (record["id"], record["favourite"], record["directory"]) == ("f1", True, "/m/Fedor")
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


def test_subfolder_settings(make_client):
    client = make_client()
    settings = client.get("/api/settings").json()
    assert (settings["use_subfolders"], settings["albums_subfolder"], settings["playlists_subfolder"]) == (
        False,
        "Albums",
        "Playlists",
    )

    res = client.put("/api/settings", json={"use_subfolders": True, "albums_subfolder": " Music/LPs/ "})
    assert res.json()["use_subfolders"] is True
    assert res.json()["albums_subfolder"] == "Music/LPs"

    for bad in ["", "../outside", "/abs", "a/../b"]:
        assert client.put("/api/settings", json={"playlists_subfolder": bad}).status_code == 422


def test_jobs_get_subfolders_only_when_enabled(make_client):
    from ltube.downloader import Subfolders

    seen = []
    client = make_client(runner=lambda job, on_event, stop: seen.append(job.subfolders) or quick_runner(job, on_event, stop))
    for enabled in (False, True):
        client.put("/api/settings", json={"use_subfolders": enabled, "albums_subfolder": "LPs"})
        job = client.post("/api/jobs", json={"url": f"https://x/{enabled}"}).json()
        wait_for_state(client, job["id"], "done")
    assert seen == [None, Subfolders(albums="LPs", playlists="Playlists")]


def test_locked_folder_still_allows_subfolders(make_client, tmp_path):
    client = make_client(lock=True)
    # The UI sends the unchanged (locked) folder along with the other settings.
    res = client.put("/api/settings", json={"out_dir": str(tmp_path / "music"), "use_subfolders": True})
    assert res.status_code == 200
    assert res.json()["use_subfolders"] is True


def test_name_pattern_settings_reach_jobs(make_client):
    seen = []
    client = make_client(runner=lambda job, on_event, stop: seen.append(job.patterns) or quick_runner(job, on_event, stop))
    settings = client.get("/api/settings").json()
    assert settings["playlist_pattern"] == settings["album_pattern"] == "{number} - {artist} - {title}"

    assert client.put("/api/settings", json={"playlist_pattern": "{artist}"}).status_code == 422
    client.put("/api/settings", json={"playlist_pattern": "{artist} - {title}"})
    job = client.post("/api/jobs", json={"url": "https://x"}).json()
    wait_for_state(client, job["id"], "done")
    assert seen[0].playlist == "{artist} - {title}"
    assert seen[0].album == "{number} - {artist} - {title}"


def test_prune_only_on_resync_and_only_when_enabled(make_client):
    seen = []
    client = make_client(runner=lambda job, on_event, stop: seen.append(job.prune) or quick_runner(job, on_event, stop))
    assert client.get("/api/settings").json()["delete_removed"] is False

    first = client.post("/api/jobs", json={"url": "https://x/playlist?list=P2"}).json()
    wait_for_state(client, first["id"], "done")
    resync = client.post(f"/api/playlists/{first['playlist_id']}/sync").json()
    wait_for_state(client, resync["id"], "done")
    client.put("/api/settings", json={"delete_removed": True})
    resync = client.post(f"/api/playlists/{first['playlist_id']}/sync").json()
    wait_for_state(client, resync["id"], "done")

    assert seen == [False, False, True]


def test_removed_tracks_are_counted_on_job_and_playlist(make_client):
    from ltube.downloader import TrackRemoved

    def pruning_runner(job, on_event, stop):
        summary = quick_runner(job, on_event, stop)
        on_event(TrackRemoved(job.out_dir / "Mix" / "02 - Old.opus", "A - Old"))
        return summary

    client = make_client(runner=pruning_runner)
    job = client.post("/api/jobs", json={"url": "https://x/playlist?list=P3"}).json()
    done = wait_for_state(client, job["id"], "done")
    assert done["counts"] == {"downloaded": 1, "removed": 1}
    assert any("02 - Old.opus" in m for m in done["messages"])
    [record] = client.get("/api/playlists").json()
    assert record["last_counts"]["removed"] == 1
