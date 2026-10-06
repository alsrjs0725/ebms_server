from unittest.mock import Mock
from src.ebms_server.db import BlobReader

def test_blob_reader_context_manager():
    mock_con = Mock()
    mock_cur = Mock()
    mock_con.open = True

    with BlobReader(mock_con, mock_cur, "song", 1, 100, "hash") as reader:
        pass

    mock_con.close.assert_called_once()

test_blob_reader_context_manager()
print("Success")
