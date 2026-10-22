import subprocess

import imageio_ffmpeg


def make_track(path, **tags):
    """A tiny real audio file with tags, made by the bundled ffmpeg."""
    meta = [arg for k, v in tags.items() for arg in ("-metadata", f"{k}={v}")]
    codec = ["-c:a", "aac"] if path.suffix == ".m4a" else ["-c:a", "libopus"]
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-t", "0.2", *codec, *meta, str(path)],
        check=True,
    )  # fmt: skip
