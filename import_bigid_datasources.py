"""
Import BigID datasources export JSON into workflow DS templates.

Input: JSON exported from /api/v1/ds_connections (array of DS objects).
Output: rows in DB table `workflow_templates` keyed by:
  ds::<ds_kind>::<file_set_type>::acl=<0/1>::struct=<0/1>

Templates are stored as JSON strings with placeholders:
  {{DS_NAME}}, {{DS_TYPE}}, {{SCANNER_GROUP}}
  {{SMB_username}}, {{SMB_password}}, {{SMB_password_enc}}
  {{S3_accessKey}}, {{S3_secret_key}}, {{S3_secret_key_enc}}
  {{MSSQL_username}}, {{MSSQL_password}}
  {{Postgres_username}}, {{Postgres_password}}

You will provide real secrets via .env (os.environ).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, text

from db_config import DB_CONNECTION_STRING


def _compact(s: Any) -> str:
    return str(s or "").strip().lower().replace(" ", "")


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _infer_file_set(ds: Dict[str, Any]) -> str:
    name = str(ds.get("name") or "").lower()
    if "csv" in name:
        return "CSV Files"
    if "small file" in name or "small files" in name:
        return "Small Files"
    if "large file" in name or "large files" in name:
        return "Large Files"

    # look at container filters
    try:
        filt = ds.get("containerizedFilter") or {}
        spec = (filt.get("specificNameFilters") or {}) if isinstance(filt, dict) else {}
        sub = spec.get("subContainerUseCase") or {}
        sub_name = str((sub.get("name") or "")).lower()
        if "csv" in sub_name:
            return "CSV Files"
        if "small" in sub_name and "file" in sub_name:
            return "Small Files"
        if "large" in sub_name and "file" in sub_name:
            return "Large Files"
    except Exception:
        pass

    return "default"


def _infer_ds_kind(ds: Dict[str, Any]) -> str:
    t = str(ds.get("type") or "").strip().lower()
    if t in {"smb_v2", "smb"}:
        return "smb"
    if t in {"s3-v2", "s3"}:
        return "s3"
    if t in {"rdb-mssql", "mssql"}:
        return "mssql"
    if t in {"rdb-postgresql", "postgresql", "postgres"}:
        return "postgres"
    return t or "unknown"


def _infer_acl(ds: Dict[str, Any]) -> bool:
    return _as_bool(ds.get("dsAclScanEnabled")) or _as_bool(ds.get("metadataAclScanEnabled"))


def _infer_struct(ds: Dict[str, Any]) -> bool:
    try:
        trp = ds.get("typeRequestProperties") or {}
        if isinstance(trp, dict):
            return _as_bool(trp.get("enableStructuredFilesDiscovery"))
    except Exception:
        pass
    return False


def _clean_ds_for_template(ds: Dict[str, Any], *, ds_kind: str) -> Dict[str, Any]:
    """
    Keep most fields as-is (BigID expects many connector-specific keys),
    but strip volatile/server-managed keys and inject placeholders.
    """
    keep = dict(ds)
    for k in [
        "_id",
        "id",
        "created_at",
        "updated_at",
        "owners_v2",
        "customFields",
        "environments",
        "friendly_name",
        "liteModeNotification",
        "scanWindowName",
    ]:
        keep.pop(k, None)

    keep["name"] = "{{DS_NAME}}"
    if "scanner_group" in keep:
        keep["scanner_group"] = "{{SCANNER_GROUP}}"
    else:
        keep["scanner_group"] = "{{SCANNER_GROUP}}"

    # Secrets placeholders
    if ds_kind == "smb":
        ap = keep.get("authenticationProperties")
        if isinstance(ap, dict):
            ap["username"] = "{{SMB_username}}"
            # allow both enc and plain; user will set one.
            ap.setdefault("password_enc", "{{SMB_password_enc}}")
            ap.setdefault("password", "{{SMB_password}}")
            keep["authenticationProperties"] = ap
    if ds_kind == "s3":
        ap = keep.get("authenticationProperties")
        if isinstance(ap, dict):
            ap["accessKey"] = "{{S3_accessKey}}"
            ap.setdefault("secret_key_enc", "{{S3_secret_key_enc}}")
            ap.setdefault("secret_key", "{{S3_secret_key}}")
            keep["authenticationProperties"] = ap
    if ds_kind == "mssql":
        # rdb-mssql uses flat fields
        keep["username"] = "{{MSSQL_username}}"
        keep["password"] = "{{MSSQL_password}}"
    if ds_kind == "postgres":
        keep["username"] = "{{Postgres_username}}"
        keep["password"] = "{{Postgres_password}}"

    return keep


def ensure_workflow_templates_table(engine) -> None:
    dialect = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
    with engine.connect() as conn:
        if "sqlite" in dialect:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS workflow_templates (
                    k TEXT PRIMARY KEY,
                    template_kind TEXT NULL,
                    payload_json TEXT NULL,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """))
            conn.commit()
            return
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS workflow_templates (
                k VARCHAR(200) PRIMARY KEY,
                template_kind VARCHAR(32) NULL,
                payload_json LONGTEXT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_workflow_templates_kind (template_kind)
            )
        """))
        conn.commit()


def upsert_template(engine, key: str, kind: str, payload_json: str) -> None:
    ensure_workflow_templates_table(engine)
    dialect = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
    with engine.connect() as conn:
        if "sqlite" in dialect:
            conn.execute(
                text("""
                    INSERT INTO workflow_templates (k, template_kind, payload_json, updated_at)
                    VALUES (:k, :t, :p, datetime('now'))
                    ON CONFLICT(k) DO UPDATE SET
                        template_kind=excluded.template_kind,
                        payload_json=excluded.payload_json,
                        updated_at=datetime('now')
                """),
                {"k": key, "t": kind, "p": payload_json},
            )
        else:
            conn.execute(
                text("""
                    INSERT INTO workflow_templates (k, template_kind, payload_json)
                    VALUES (:k, :t, :p)
                    ON DUPLICATE KEY UPDATE
                        template_kind=VALUES(template_kind),
                        payload_json=VALUES(payload_json),
                        updated_at=CURRENT_TIMESTAMP
                """),
                {"k": key, "t": kind, "p": payload_json},
            )
        conn.commit()


def key_for(ds_kind: str, file_set: str, is_acl: bool, is_struct: bool) -> str:
    fs = str(file_set or "").strip() or "default"
    return f"ds::{ds_kind}::{fs}::acl={1 if is_acl else 0}::struct={1 if is_struct else 0}"


def import_file(engine, path: str) -> Tuple[int, int]:
    raw = open(path, "r", encoding="utf-8", errors="ignore").read()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise RuntimeError("Expected a JSON array of datasources")

    seen = 0
    saved = 0
    for ds in data:
        if not isinstance(ds, dict):
            continue
        seen += 1
        ds_kind = _infer_ds_kind(ds)
        file_set = _infer_file_set(ds)
        is_acl = _infer_acl(ds)
        is_struct = _infer_struct(ds)
        if ds_kind not in {"smb", "s3", "mssql", "postgres"}:
            continue
        cleaned = _clean_ds_for_template(ds, ds_kind=ds_kind)
        payload = {"ds_connection": cleaned, "isSavedAsDraft": False}
        k = key_for(ds_kind, file_set, is_acl, is_struct)
        upsert_template(engine, k, "ds", json.dumps(payload, indent=2))
        saved += 1
    return seen, saved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="Paths to BigID datasources export JSON files")
    args = ap.parse_args()

    engine = create_engine(DB_CONNECTION_STRING)
    total_seen = 0
    total_saved = 0
    for p in args.files:
        seen, saved = import_file(engine, p)
        total_seen += seen
        total_saved += saved
        print(f"Imported {saved} DS templates from {p} (items seen={seen})", flush=True)
    print(f"Done. Total templates saved={total_saved}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

