from __future__ import annotations

from io import BytesIO

import pytest

from app.storage import LocalStorageBackend, RangeNotSatisfiableError, parse_http_range


def test_local_storage_range_reads(tmp_path) -> None:
    storage = LocalStorageBackend(tmp_path, chunk_size=4)
    storage.put_object("assignments/1/file.txt", BytesIO(b"abcdef"), content_type="text/plain")

    metadata = storage.head_object("assignments/1/file.txt")
    assert metadata.size == 6
    ranged = storage.get_object_range("assignments/1/file.txt", start=1, end=3)

    assert ranged.size == 6
    assert ranged.range_start == 1
    assert ranged.range_end == 3
    assert b"".join(ranged.body) == b"bcd"


def test_local_storage_rejects_path_traversal(tmp_path) -> None:
    storage = LocalStorageBackend(tmp_path)

    with pytest.raises(Exception):
        storage.put_object("../escape.txt", BytesIO(b"bad"))


def test_parse_http_range_variants() -> None:
    assert parse_http_range("bytes=2-4", object_size=10) == (2, 4)
    assert parse_http_range("bytes=7-", object_size=10) == (7, 9)
    assert parse_http_range("bytes=-3", object_size=10) == (7, 9)

    with pytest.raises(RangeNotSatisfiableError):
        parse_http_range("bytes=11-12", object_size=10)
