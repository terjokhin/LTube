"""Download YouTube Music playlists to local storage."""

import os
from importlib.metadata import PackageNotFoundError, version


def get_version() -> str:
    """The release version: LTUBE_VERSION (set by the Docker build from the git tag), else the
    package's placeholder version.
    """
    if env := os.environ.get("LTUBE_VERSION"):
        return env
    try:
        return version("ltube")
    except PackageNotFoundError:
        return "0.0.0"
