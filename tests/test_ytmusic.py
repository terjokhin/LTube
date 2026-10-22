from ltube import ytmusic


def test_parse_tracks_takes_artists_and_album():
    tracks = ytmusic.parse_tracks(
        [
            {"videoId": "a", "title": "Баблгам", "artists": [{"name": "ZAPRAVKA"}], "album": {"name": "Не трогай"}},
            {"videoId": "b", "title": None, "artists": None, "album": None},
            {"videoId": None, "title": "unplayable"},
        ]
    )
    assert tracks == [
        ytmusic.MusicTrack("a", "Баблгам", ["ZAPRAVKA"], "Не трогай"),
        ytmusic.MusicTrack("b", "b", [], None),
    ]


def test_music_playlist_never_raises(tmp_path, monkeypatch):
    def broken(cookiefile):
        raise RuntimeError("API changed")

    monkeypatch.setattr(ytmusic, "_client", broken)
    assert ytmusic.music_playlist(tmp_path / "cookies.txt", "PL1") is None


def test_client_authenticates_from_cookies_txt(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PAPISID\tsecretsapisid\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsid\n"
        ".google.com\tTRUE\t/\tTRUE\t0\tNID\tother\n"
    )
    client = ytmusic._client(cookies)
    assert client.auth_type.name == "BROWSER"


def test_album_details_number_tracks_in_album_order():
    album = {
        "year": "2022",
        "trackCount": 3,
        "artists": [{"name": "ZAPRAVKA"}],
        "tracks": [{"videoId": "a"}, {"videoId": "b"}, {"videoId": None}],
    }
    details = ytmusic.album_details(album)
    assert details["a"] == {"track_count": 3, "year": 2022, "album_artists": ["ZAPRAVKA"], "track_number": 1}
    assert details["b"]["track_number"] == 2


def test_with_album_details_one_request_per_upload_album(monkeypatch, tmp_path):
    upload = ytmusic.UPLOAD_ALBUM_PREFIX + "x"
    tracks = [
        ytmusic.MusicTrack("a", "A", ["Z"], "LP", upload),
        ytmusic.MusicTrack("b", "B", ["Z"], "LP", upload),
        ytmusic.MusicTrack("c", "C", ["Z"], "Public album", "MPREb_public"),  # not an upload: skipped
    ]
    requested, pauses = [], []

    class Client:
        def get_library_upload_album(self, album_id):
            requested.append(album_id)
            return {"year": 2022, "trackCount": 2, "artists": [{"name": "Z"}], "tracks": [{"videoId": "b"}, {"videoId": "a"}]}

    monkeypatch.setattr(ytmusic, "_client", lambda cookiefile: Client())
    out = ytmusic.with_album_details(tmp_path / "cookies.txt", tracks, pause=lambda: pauses.append(1))

    assert requested == [upload] and len(pauses) == 1
    assert [(t.video_id, t.track_number, t.year) for t in out] == [("a", 2, 2022), ("b", 1, 2022), ("c", None, None)]
