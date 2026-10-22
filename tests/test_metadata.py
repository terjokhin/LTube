from pathlib import Path

import pytest

from conftest import make_track
from ltube.metadata import (
    NormalizeMetadataPP,
    album_folder_name,
    render_name,
    validate_name_pattern,
    write_m3u8,
)


def normalize(info):
    _, out = NormalizeMetadataPP().run(info)
    return out


def test_topic_channel_becomes_artist():
    info = normalize({"title": "Song", "uploader": "Daft Punk - Topic"})
    assert info["artist"] == "Daft Punk"
    assert info["artists"] == ["Daft Punk"]


def test_extracted_artists_win_over_uploader():
    info = normalize({"artists": ["A", "B"], "uploader": "Some Channel"})
    assert info["artist"] == "A, B"


def test_duplicate_artists_are_dropped():
    info = normalize({"artists": ["Reveriesss", "Johnny C.", "Johnny C."]})
    assert info["artist"] == "Reveriesss, Johnny C."


def test_no_artist_information_leaves_fields_unset():
    assert "artist" not in normalize({"title": "Song"})


def test_write_m3u8(tmp_path):
    playlist = tmp_path / "Mix" / "Mix.m3u8"
    playlist.parent.mkdir()
    write_m3u8(
        playlist,
        [
            (playlist.parent / "01 - A - One.opus", 201.4, "A - One"),
            (playlist.parent / "02 - B - Two.m4a", None, "B - Two"),
        ],
    )
    assert playlist.read_text(encoding="utf-8").splitlines() == [
        "#EXTM3U",
        "#EXTINF:201,A - One",
        "01 - A - One.opus",
        "#EXTINF:-1,B - Two",
        "02 - B - Two.m4a",
    ]


def test_album_folder_name():
    assert album_folder_name("Daft Punk", 2001, "Discovery") == "Daft Punk - 2001 - Discovery"
    assert album_folder_name("Daft Punk", None, "Discovery") == "Daft Punk - Discovery"
    assert album_folder_name(None, 2001, "Discovery") == "Unknown Artist - 2001 - Discovery"


def test_scan_existing_finds_video_ids_in_tags(tmp_path):
    from ltube.metadata import scan_existing

    url = "https://www.youtube.com/watch?v=P8X9ESwV0b0"
    make_track(tmp_path / "07 - Russian Circles - Borehole.opus", purl=url, artist="Russian Circles", title="Borehole")
    make_track(tmp_path / "02 - B - Two.m4a", comment="https://www.youtube.com/watch?v=abcdefghijk", title="Two")
    make_track(tmp_path / "03 - untagged.opus")

    found = scan_existing(tmp_path)

    assert set(found) == {"P8X9ESwV0b0", "abcdefghijk"}
    assert found["P8X9ESwV0b0"].path.name == "07 - Russian Circles - Borehole.opus"
    assert found["P8X9ESwV0b0"].label == "Russian Circles - Borehole"
    assert found["abcdefghijk"].label == "Unknown Artist - Two"


@pytest.mark.parametrize(
    ("pattern", "values", "expected"),
    [
        ("{number} - {artist} - {title}", {"number": "01", "artist": "A", "title": "T"}, "01 - A - T"),
        ("{artist} - {title}", {"number": "01", "artist": "A", "title": "T"}, "A - T"),
        ("{number} - {year} - {title}", {"number": "01", "title": "T"}, "01 - T"),
        ("{year} - {title}", {"title": "T"}, "T"),
        ("{title} ({year})", {"title": "T"}, "T"),
        ("{title} ({year})", {"title": "T", "year": "2001"}, "T (2001)"),
        ("{number}. {title} [{album}]", {"number": "3", "title": "T"}, "3. T"),
    ],
)
def test_render_name(pattern, values, expected):
    assert render_name(pattern, values) == expected


@pytest.mark.parametrize("pattern", ["{artist}", "{title} {genre}", "{artist}/{title}", "  "])
def test_validate_name_pattern_rejects(pattern):
    with pytest.raises(ValueError):
        validate_name_pattern(pattern)


def test_track_name_goes_through_yt_dlp_output_template(tmp_path):
    from yt_dlp import YoutubeDL

    from ltube.metadata import TrackNamePP

    info = {"id": "abc", "title": "AC/DC Cover", "uploader": "Band - Topic", "ext": "opus"}
    _, info = NormalizeMetadataPP().run(info)
    _, info = TrackNamePP("{number} - {artist} - {title} ({year})", "07").run(info)
    with YoutubeDL({"outtmpl": f"{tmp_path}/%(ltube_name)s.%(ext)s"}) as ydl:
        path = Path(ydl.prepare_filename(info))

    assert path.parent == tmp_path
    assert path.name.startswith("07 - Band - AC") and path.name.endswith(" Cover.opus")


def test_track_file_name_sanitizes_like_yt_dlp_and_fits_the_filesystem():
    from yt_dlp.utils import sanitize_filename

    from ltube.metadata import track_file_name

    name = track_file_name("{artist} - {title}", {"artist": "AC/DC", "title": "What?"})
    assert name == sanitize_filename("AC/DC - What?") and "/" not in name
    assert sanitize_filename(name) == name  # yt-dlp sanitizes ltube_name again; must not change it
    long = track_file_name("{title}", {"title": "Ж" * 300})  # 2 bytes per letter
    assert len(long.encode()) <= 230 and long == "Ж" * 115


def test_release_year_is_tagged_instead_of_upload_date():
    _, info = NormalizeMetadataPP().run({"title": "T", "release_year": 1997, "upload_date": "20150101"})
    assert info["meta_date"] == "1997"
    _, info = NormalizeMetadataPP().run({"title": "T", "upload_date": "20150101"})
    assert "meta_date" not in info


def test_scan_existing_reads_name_fields(tmp_path):
    from ltube.metadata import scan_existing

    url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    make_track(tmp_path / "x.opus", purl=url, artist="A", title="Song", album="LP", date="1997")
    make_track(tmp_path / "y.opus", purl=url.replace("a", "b"), title="Other", date="20150101")
    found = scan_existing(tmp_path)
    assert found["aaaaaaaaaaa"].fields == {"artist": "A", "title": "Song", "album": "LP", "year": "1997"}
    assert found["bbbbbbbbbbb"].fields == {"artist": "Unknown Artist", "title": "Other", "album": None, "year": None}


def test_uploads_channel_is_not_an_artist():
    from ltube.metadata import UPLOADS_CHANNEL

    info = normalize({"title": "Баблгам", "uploader": UPLOADS_CHANNEL, "artists": [UPLOADS_CHANNEL]})
    assert "artist" not in info and "artists" not in info
    assert info["meta_artist"] == ""  # stops FFmpegMetadata tagging the uploads channel


def test_private_videos_only_try_signed_thumbnails_preferring_the_upload_cover():
    from ltube.metadata import usable_thumbnails

    thumbs = [
        {"url": "https://i.ytimg.com/vi/x/maxresdefault.jpg", "width": 1280},
        {"url": "https://i9.ytimg.com/vi/x/hqdefault.jpg?sqp=a", "width": 480},
        {"url": "https://i9.ytimg.com/vi_locker/x/locker.png?sqp=b", "width": 544},
        {"url": "https://i9.ytimg.com/vi_locker/x/locker.png?sqp=c", "width": 226},
    ]
    picked = usable_thumbnails({"availability": "private", "thumbnails": thumbs})
    assert [t["width"] for t in picked] == [226, 544]  # yt-dlp tries the last (largest) first
    no_locker = usable_thumbnails({"availability": "private", "thumbnails": thumbs[:2]})
    assert [t["width"] for t in no_locker] == [480]
    assert usable_thumbnails({"availability": "public", "thumbnails": thumbs}) == thumbs


def run_cover_pp(thumb_path):
    import imageio_ffmpeg
    from yt_dlp import YoutubeDL

    from ltube.metadata import CoverArtPP

    with YoutubeDL({"ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(), "quiet": True}) as ydl:
        _, info = CoverArtPP(ydl).run({"thumbnails": [{"url": "x", "filepath": str(thumb_path)}]})
    return info["thumbnails"][0]


def test_cover_is_converted_by_content_even_when_misnamed(tmp_path):
    import subprocess

    import imageio_ffmpeg

    misnamed = tmp_path / "01 - A - T.png"
    jpeg = tmp_path / "src.jpg"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "color=red:s=320x180",
                    "-frames:v", "1", str(jpeg)], check=True)  # fmt: skip
    jpeg.rename(misnamed)  # JPEG data under a .png name, as YouTube serves for uploads

    thumb = run_cover_pp(misnamed)

    cover = tmp_path / "01 - A - T.jpg"
    assert thumb["filepath"] == str(cover) and cover.exists() and not misnamed.exists()
    probe = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(cover)], capture_output=True, text=True)
    assert "180x180" in probe.stderr  # cropped square


def test_unconvertible_cover_is_dropped_not_fatal(tmp_path):
    broken = tmp_path / "01 - A - T.webp"
    broken.write_bytes(b"not an image")

    thumb = run_cover_pp(broken)

    assert "filepath" not in thumb
    assert list(tmp_path.iterdir()) == []


def test_youtube_music_data_fills_artist_and_album_for_uploads():
    from ltube.metadata import UPLOADS_CHANNEL
    from ltube.ytmusic import MusicTrack

    music = MusicTrack("rbSlQPXdBvg", "Баблгам", ["ZAPRAVKA"], "Не трогай, это на Новый Год")
    _, info = NormalizeMetadataPP(music).run({"title": "Баблгам", "uploader": UPLOADS_CHANNEL})
    assert (info["artist"], info["album"]) == ("ZAPRAVKA", "Не трогай, это на Новый Год")
    assert "meta_artist" not in info

    # yt-dlp's own music metadata wins where it exists.
    _, info = NormalizeMetadataPP(music).run({"title": "T", "artists": ["Real"], "album": "Real LP"})
    assert (info["artist"], info["album"]) == ("Real", "Real LP")


def test_upload_album_details_become_tags():
    from ltube.ytmusic import MusicTrack

    music = MusicTrack("v", "Ништяк", ["ZAPRAVKA"], "LP", "id", track_number=2, track_count=3, year=2022, album_artists=["ZAPRAVKA"])
    _, info = NormalizeMetadataPP(music).run({"title": "Ништяк", "uploader": "Music Library Uploads"})
    assert info["meta_track"] == "2/3" and info["track_number"] == 2
    assert info["album_artists"] == ["ZAPRAVKA"]
    assert info["release_year"] == 2022 and info["meta_date"] == "2022"
