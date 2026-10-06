import pytest
import zipfile
import io
from ebms_server.db import zip_entries


def test_zip_entries_empty_zip():
    # Create an empty zip file in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass

    entries = zip_entries(buf.getvalue())
    assert entries == []


def test_zip_entries_invalid_zip():
    with pytest.raises(zipfile.BadZipFile):
        zip_entries(b"not a zip file")


def test_zip_entries_empty_bytes():
    with pytest.raises(zipfile.BadZipFile):
        zip_entries(b"")
