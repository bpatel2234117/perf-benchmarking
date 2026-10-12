"""
Symmetric encryption for BigID environment credentials stored in Postgres.

Key management:
  - Uses CREDENTIALS_ENCRYPT_KEY env var (base64-urlsafe Fernet key).
  - If not set, auto-generates a key on first call and persists it to .env
    in the repo root so subsequent restarts reuse the same key.
"""
import os
import base64
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

_ENV_FILE = Path(__file__).parent.parent / ".env"
_KEY_VAR = "CREDENTIALS_ENCRYPT_KEY"

_fernet: "Fernet | None" = None


def _load_or_create_key() -> bytes:
    # 1) env var takes priority
    key = os.getenv(_KEY_VAR, "").strip()
    if key:
        return key.encode()

    # 2) check .env file
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s.startswith(f"{_KEY_VAR}="):
                val = s.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    os.environ[_KEY_VAR] = val
                    return val.encode()

    # 3) generate, save, and return
    new_key = Fernet.generate_key()
    new_key_str = new_key.decode()
    os.environ[_KEY_VAR] = new_key_str
    _persist_key(new_key_str)
    return new_key


def _persist_key(key_str: str) -> None:
    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if _ENV_FILE.exists():
        existing = _ENV_FILE.read_text(encoding="utf-8", errors="ignore")
    if _KEY_VAR not in existing:
        with _ENV_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\n# Auto-generated credential encryption key\n{_KEY_VAR}={key_str}\n")


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt_password(plain: str) -> str:
    """Encrypt a plaintext password; returns a URL-safe base64 token."""
    if not plain:
        return ""
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_password(token: str) -> str:
    """Decrypt a token produced by encrypt_password; returns plaintext."""
    if not token:
        return ""
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        return ""
