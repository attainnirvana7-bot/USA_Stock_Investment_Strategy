import pytest

from usequity.crypto import KeyError_, decrypt_file, encrypt_file

KEY = "correct horse battery staple"


def test_roundtrip(tmp_path):
    src, enc, out = tmp_path / "a.db", tmp_path / "a.db.enc", tmp_path / "b.db"
    src.write_bytes(b"SQLite format 3\x00" + bytes(range(256)) * 100)
    encrypt_file(src, enc, KEY)
    assert b"SQLite" not in enc.read_bytes()
    decrypt_file(enc, out, KEY)
    assert out.read_bytes() == src.read_bytes()


def test_wrong_or_short_key(tmp_path):
    src, enc = tmp_path / "a.db", tmp_path / "a.db.enc"
    src.write_bytes(b"data")
    encrypt_file(src, enc, KEY)
    with pytest.raises(KeyError_, match="不正確"):
        decrypt_file(enc, tmp_path / "b.db", KEY + "x")
    with pytest.raises(KeyError_, match="16"):
        encrypt_file(src, enc, "short")
