import pathlib
from ebms_server.db import chart_arcname


def test_chart_arcname():
    sha = "dummyhash"

    # Normal lowercase extension
    assert chart_arcname(sha, pathlib.PurePath("foo.bms")) == f"{sha}.bms"

    # Uppercase extension - should be preserved according to the current code
    assert chart_arcname(sha, pathlib.PurePath("foo.BME")) == f"{sha}.BME"

    # Path with directories
    assert (
        chart_arcname(sha, pathlib.PurePath("some/path/to/chart.bml")) == f"{sha}.bml"
    )

    # No extension
    assert chart_arcname(sha, pathlib.PurePath("foo")) == f"{sha}"

    # Empty extension
    assert chart_arcname(sha, pathlib.PurePath("foo.")) == f"{sha}"

    # Dotfile without extension
    assert chart_arcname(sha, pathlib.PurePath(".bms")) == f"{sha}"

    # Multiple extensions
    assert chart_arcname(sha, pathlib.PurePath("foo.tar.gz")) == f"{sha}.gz"
