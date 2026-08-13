"""Upload storage (services/storage.py): the local-disk backend."""

import pytest

from app.config import UPLOADS_DIR
from app.services.storage import LocalStorage


def test_roundtrip(tmp_path):
    s = LocalStorage(root=tmp_path)
    s.save("a.pdf", b"bytes")
    assert s.read("a.pdf") == b"bytes"


def test_missing_key_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        LocalStorage(root=tmp_path).read("nope.pdf")


def test_delete_is_idempotent(tmp_path):
    s = LocalStorage(root=tmp_path)
    s.save("a.pdf", b"x")
    s.delete("a.pdf")
    s.delete("a.pdf")  # deleting what's already gone is not an error
    with pytest.raises(FileNotFoundError):
        s.read("a.pdf")


def test_keys_cannot_escape_the_uploads_directory(tmp_path):
    """Keys are app-generated, but a traversal key must still stay contained."""
    s = LocalStorage(root=tmp_path)
    s.save("../../escaped.txt", b"x")
    assert (tmp_path / "escaped.txt").exists()
    assert not (tmp_path.parent.parent / "escaped.txt").exists()


def test_directory_is_created_on_demand(tmp_path):
    s = LocalStorage(root=tmp_path / "deep" / "nested")
    s.save("a.txt", b"x")
    assert s.read("a.txt") == b"x"


def test_default_backend_points_at_the_data_directory():
    from app.services.storage import get_storage

    assert get_storage().root == UPLOADS_DIR
