"""Apply shared BigID license on env save. Token: BIGID_LICENSE_TOKEN in .env (your curl body)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from bigid_auth import Get_auth_token, _build_url, _read_dotenv_map

logger = logging.getLogger(__name__)

LICENSE_DIR = Path(__file__).resolve().parent.parent / "data" / "environment_licenses"
INSTALL_TOKEN_FILE = LICENSE_DIR / "install_token"


def license_file_path(environment_id: int) -> Path:
    LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    return LICENSE_DIR / f"env_{int(environment_id)}.json"


def _persist_install_token(token: str) -> None:
    t = (token or "").strip()
    if not t:
        return
    LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    if INSTALL_TOKEN_FILE.is_file() and INSTALL_TOKEN_FILE.read_text(encoding="utf-8").strip() == t:
        return
    INSTALL_TOKEN_FILE.write_text(t, encoding="utf-8")


def get_license_install_token() -> str:
    dotenv_map = _read_dotenv_map()
    from_env = (os.getenv("BIGID_LICENSE_TOKEN") or dotenv_map.get("BIGID_LICENSE_TOKEN") or "").strip()
    if from_env:
        _persist_install_token(from_env)
        return from_env
    if INSTALL_TOKEN_FILE.is_file():
        return INSTALL_TOKEN_FILE.read_text(encoding="utf-8").strip()
    return ""


def _extract_license_summary(payload: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(payload, dict):
        return out

    def _walk(obj: Any, depth: int = 0) -> None:
        if depth > 4 or not isinstance(obj, dict):
            return
        for key, val in obj.items():
            lk = str(key).lower()
            if lk in ("licensename", "license_name", "name") and isinstance(val, str) and val.strip():
                out.setdefault("license_name", val.strip())
            if lk in ("expirationdate", "expiration_date", "expires", "expiry") and isinstance(val, str) and val.strip():
                out.setdefault("license_expires", val.strip())
            if isinstance(val, dict):
                _walk(val, depth + 1)

    _walk(payload)
    for outer in ("data", "result", "license"):
        nested = payload.get(outer)
        if isinstance(nested, dict):
            _walk(nested, 0)
    return out


def fetch_and_store_environment_license(env_row: Dict[str, Any]) -> Dict[str, Any]:
    env_id = int(env_row.get("id") or 0)
    from bigid_auth import normalize_bigid_base_url, _get_ssl_verify_for_url
    base_url = normalize_bigid_base_url(str(env_row.get("base_url") or ""))
    env_name = str(env_row.get("env_name") or "").strip()

    if not env_id or not base_url:
        return {"ok": False, "environment_id": env_id, "error": "missing environment id or base_url"}

    install_token = get_license_install_token()
    if not install_token:
        return {
            "ok": False,
            "environment_id": env_id,
            "error": "BIGID_LICENSE_TOKEN missing in .env",
        }

    try:
        auth_token = Get_auth_token(base_url, env_row)
    except Exception as ex:
        return {"ok": False, "environment_id": env_id, "error": f"auth failed: {ex}"}

    url = _build_url(base_url, os.getenv("BIGID_LICENSE_API_PATH", "/api/v1/license").strip() or "/api/v1/license")
    ssl_verify = _get_ssl_verify_for_url(url)
    http_status = 0
    license_payload: Any = {}
    try:
        resp = requests.post(
            url,
            json={"token": install_token},
            headers={
                "authorization": auth_token,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            },
            timeout=int(os.getenv("BIGID_LICENSE_TIMEOUT_SEC", "60") or "60"),
            verify=ssl_verify,
        )
        http_status = int(resp.status_code)
        try:
            license_payload = resp.json()
        except Exception:
            license_payload = {"raw_text": (resp.text or "")[:8000]}
    except Exception as ex:
        return {"ok": False, "environment_id": env_id, "error": f"license request failed: {ex}"}

    summary = _extract_license_summary(license_payload if isinstance(license_payload, dict) else {})
    record = {
        "environment_id": env_id,
        "env_name": env_name,
        "base_url": base_url,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "http_status": http_status,
        "license_summary": summary,
        "license_response": license_payload,
    }
    try:
        license_file_path(env_id).write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    except Exception as ex:
        return {"ok": False, "environment_id": env_id, "http_status": http_status, "error": f"failed to write license file: {ex}"}

    ok = 200 <= http_status < 300
    return {
        "ok": ok,
        "environment_id": env_id,
        "http_status": http_status,
        "license_summary": summary,
        "error": None if ok else f"license API returned HTTP {http_status}",
    }


def load_environment_row_for_license(engine, environment_id: int) -> Optional[Dict[str, Any]]:
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, env_name, base_url, auth_module_path, auth_function_name,
                       bigid_username, bigid_password_enc
                FROM test_environments WHERE id = :eid LIMIT 1
                """
            ),
            {"eid": int(environment_id)},
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "env_name": row[1],
        "base_url": row[2],
        "auth_module_path": row[3],
        "auth_function_name": row[4],
        "bigid_username": row[5],
        "bigid_password_enc": row[6],
    }


def sync_environment_license(engine, environment_id: int) -> Dict[str, Any]:
    env_row = load_environment_row_for_license(engine, environment_id)
    if not env_row:
        return {"ok": False, "environment_id": int(environment_id), "error": "environment not found"}
    result = fetch_and_store_environment_license(env_row)
    if not result.get("ok"):
        logger.warning("license apply env %s: %s", environment_id, result.get("error"))
    return result
