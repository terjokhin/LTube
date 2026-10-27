from pathlib import Path

import pytest

from ltube.metadata import (
    DEFAULT_TRACK_PATTERN,
    MAX_NAME_BYTES,
    LibraryNamePP,
    NormalizeMetadataPP,
    library_location,
    render_name,
    validate_name_pattern,
    validate_track_pattern,
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


def test_write_m3u8_points_across_folders_and_creates_its_own(tmp_path):
    playlist = tmp_path / "playlists" / "Mix.m3u8"
    write_m3u8(
        playlist,
        [
            (tmp_path / "A" / "LP" / "01 - One.opus", 201.4, "A - One"),
            (tmp_path / "B" / "Singles" / "00 - Two.m4a", None, "B - Two"),
        ],
    )
    assert playlist.read_text(encoding="utf-8").splitlines() == [
        "#EXTM3U",
        "#EXTINF:201,A - One",
        "../A/LP/01 - One.opus",
        "#EXTINF:-1,B - Two",
        "../B/Singles/00 - Two.m4a",
    ]
    assert [p.name for p in playlist.parent.iterdir()] == ["Mix.m3u8"]  # no temp file left


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


def test_library_names_go_through_yt_dlp_output_template_unchanged(tmp_path):
    from yt_dlp import YoutubeDL

    info = {"id": "aaaaaaaaaaa", "title": "AC/DC Cover?", "uploader": "Band - Topic", "ext": "opus"}
    _, info = NormalizeMetadataPP().run(info)
    _, info = LibraryNamePP(DEFAULT_TRACK_PATTERN).run(info)
    with YoutubeDL({"outtmpl": f"{tmp_path}/%(ltube_name)s.%(ext)s"}) as ydl:
        path = Path(ydl.prepare_filename(info))

    assert path.parent == tmp_path
    assert path.stem == info["ltube_name"] == "00 - AC⧸DC Cover？ [aaaaaaaaaaa]"  # yt-dlp must not change it


def test_release_year_is_tagged_instead_of_upload_date():
    _, info = NormalizeMetadataPP().run({"title": "T", "release_year": 1997, "upload_date": "20150101"})
    assert info["meta_date"] == "1997"
    _, info = NormalizeMetadataPP().run({"title": "T", "upload_date": "20150101"})
    assert "meta_date" not in info


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


def test_track_pattern_must_keep_the_video_id():
    assert validate_track_pattern(DEFAULT_TRACK_PATTERN) == DEFAULT_TRACK_PATTERN
    assert validate_track_pattern(" {title} [{id}] ") == "{title} [{id}]"
    with pytest.raises(ValueError, match=r"\{id\}"):
        validate_track_pattern("{number} - {title}")
    with pytest.raises(ValueError, match=r"\{title\}"):
        validate_track_pattern("{id}")


def location(info, number=None, pattern=DEFAULT_TRACK_PATTERN):
    folder, name = library_location(pattern, {"id": "aaaaaaaaaaa", "title": "Song"} | info, number)
    return folder.as_posix(), name


def test_album_track_goes_to_album_artist_and_album_folder():
    info = {"album": "Nine", "artists": ["Russian Circles", "Guest"], "track_number": 3}
    assert location(info) == ("Russian Circles/Nine", "03 - Song [aaaaaaaaaaa]")
    # The album artist beats the track artist (compilations), and the album position beats the tag.
    assert location(info | {"album_artists": ["Various Artists"]}, number=7) == (
        "Various Artists/Nine", "07 - Song [aaaaaaaaaaa]",
    )  # fmt: skip


def test_track_without_album_goes_to_singles_with_number_zero():
    assert location({"artists": ["Artist"]}) == ("Artist/Singles", "00 - Song [aaaaaaaaaaa]")


def test_track_without_artist_goes_to_unknown_artist():
    assert location({"album": "LP"}) == ("Unknown Artist/LP", "00 - Song [aaaaaaaaaaa]")


def test_pattern_can_use_other_fields():
    info = {"artist": "A", "artists": ["A"], "album": "LP", "release_year": 2001, "track_number": 2}
    assert location(info, pattern="{artist} - {title} ({year}) [{id}]")[1] == "A - Song (2001) [aaaaaaaaaaa]"


@pytest.mark.parametrize("hostile", ["AC/DC", "..", ".", "...Baby", "  ", "a\\b", "What?"])
def test_folder_names_cannot_leave_or_hide_the_folder(hostile):
    folder, _ = library_location(DEFAULT_TRACK_PATTERN, {"id": "x", "title": "T", "artists": [hostile], "album": hostile})
    assert len(folder.parts) == 2
    assert all(part and not part.startswith(".") and "/" not in part for part in folder.parts)


def test_long_title_is_cut_but_keeps_the_id():
    folder, name = library_location(
        DEFAULT_TRACK_PATTERN, {"id": "aaaaaaaaaaa", "title": "я" * 200, "artists": ["A"], "album": "L" * 400}
    )
    assert len(name.encode()) <= MAX_NAME_BYTES
    assert name.startswith("00 - яя") and name.endswith(" [aaaaaaaaaaa]")
    assert all(len(part.encode()) <= MAX_NAME_BYTES for part in folder.parts)


def test_album_position_becomes_the_track_number_tag():
    _, info = LibraryNamePP(DEFAULT_TRACK_PATTERN, number=4).run({"id": "x", "title": "T", "artists": ["A"]})
    assert info["track_number"] == 4  # FFmpegMetadata tags `track` from it
    _, info = LibraryNamePP(DEFAULT_TRACK_PATTERN).run({"id": "x", "title": "T", "artists": ["A"]})
    assert "track_number" not in info


def test_library_name_pp_sets_folder_and_name_apart():
    _, info = LibraryNamePP(DEFAULT_TRACK_PATTERN, number=4).run(
        {"id": "aaaaaaaaaaa", "title": "Song", "artists": ["Artist"], "album": "AC/DC"}
    )
    assert info["ltube_dir"] == "Artist/AC⧸DC"  # a slash in the album name is not a folder level
    assert info["ltube_name"] == "04 - Song [aaaaaaaaaaa]"


def test_retag_puts_an_opus_track_on_an_album(tmp_path):
    import mutagen

    from conftest import make_track
    from ltube.metadata import retag_album

    track = tmp_path / "x.opus"
    make_track(track, title="Song", artist="A")
    retag_album(track, album="Disc", album_artist="Band", track_number=2, track_total=9, year=2001)
    tags = mutagen.File(track).tags
    assert (tags["album"], tags["albumartist"], tags["tracknumber"], tags["tracktotal"], tags["date"]) == (
        ["Disc"], ["Band"], ["2"], ["9"], ["2001"],
    )  # fmt: skip
    assert (tags["title"], tags["artist"]) == (["Song"], ["A"])


def test_retag_puts_an_m4a_track_on_an_album(tmp_path):
    import mutagen

    from conftest import make_track
    from ltube.metadata import retag_album

    track = tmp_path / "x.m4a"
    make_track(track, title="Song", artist="A")
    retag_album(track, album="Disc", album_artist="Band", track_number=2, track_total=None, year=None)
    tags = mutagen.File(track).tags
    assert (tags["\xa9alb"], tags["aART"], tags["trkn"]) == (["Disc"], ["Band"], [(2, 0)])
    assert "\xa9day" not in tags  # an unknown year is not invented
    assert (tags["\xa9nam"], tags["\xa9ART"]) == (["Song"], ["A"])


def test_retag_refuses_what_is_not_audio(tmp_path):
    from ltube.metadata import retag_album

    junk = tmp_path / "x.opus"
    junk.write_bytes(b"not audio")
    with pytest.raises(ValueError):
        retag_album(junk, album="Disc", album_artist=None, track_number=1, track_total=None, year=None)
