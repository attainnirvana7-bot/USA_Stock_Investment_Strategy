"""資料庫加密：公開分支上只放加密檔，Streamlit 以 DB_KEY 解密。

DB_KEY 是自訂的長密語（至少 16 字元），以 scrypt 導出 Fernet 金鑰，
同一組密語需同時設在 GitHub Secrets 與 Streamlit Secrets。

  python -m usequity.crypto encrypt data/usequity.db data/usequity.db.enc
  python -m usequity.crypto decrypt data/usequity.db.enc data/usequity.db
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys
import zlib
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

MIN_KEY_LEN = 16
_SALT = b"usequity-db-v1"


class KeyError_(ValueError):
    pass


def _fernet(passphrase: str) -> Fernet:
    if not passphrase or len(passphrase) < MIN_KEY_LEN:
        raise KeyError_(f"DB_KEY 至少需要 {MIN_KEY_LEN} 個字元")
    raw = hashlib.scrypt(passphrase.encode(), salt=_SALT, n=2**14, r=8, p=1, dklen=32)
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_file(src: str | Path, dst: str | Path, passphrase: str) -> None:
    # 先壓縮再加密（SQLite 壓縮率高；加密後的資料就無法再壓縮）
    Path(dst).write_bytes(_fernet(passphrase).encrypt(zlib.compress(Path(src).read_bytes(), 6)))


def decrypt_file(src: str | Path, dst: str | Path, passphrase: str) -> None:
    try:
        data = zlib.decompress(_fernet(passphrase).decrypt(Path(src).read_bytes()))
    except InvalidToken:
        raise KeyError_("DB_KEY 不正確，無法解密資料庫") from None
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(dst).with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dst)   # 原子替換，避免讀到寫一半的檔案


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("encrypt", "decrypt"):
        print(__doc__)
        return 2
    key = os.environ.get("DB_KEY") or ""
    try:
        (encrypt_file if argv[0] == "encrypt" else decrypt_file)(argv[1], argv[2], key)
    except KeyError_ as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
