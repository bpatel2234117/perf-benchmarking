#!/usr/bin/env python3
"""Test DS connection (PUT + test) for MSSQL and S3.

Create DS with any name and trigger test connection; once test is triggered,
the DS will not stay in draft status.

Needs: .env with BIGID_BASE_URL, auth, MSSQL_*/S3_* credentials.
DB: workflow_templates (ds::mssql::..., ds::s3::...). Import via import_bigid_datasources.py if missing.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_ENV_FILE = _PROJECT_ROOT / ".env"
if _ENV_FILE.exists():
    try:
        raw = _ENV_FILE.read_text(encoding="utf-8", errors="ignore")
        for line in raw.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            key = k.strip()
            if key and key not in os.environ:
                os.environ[key] = v.strip().strip("\"").strip("'")
    except Exception:
        pass

from sqlalchemy import create_engine
from db_config import DB_CONNECTION_STRING
from bigid_auth import Get_auth_token
from workflow_runner import (
    get_template,
    put_ds_connection,
    fetch_ds_connection,
    test_ds_connection,
    wait_for_ds_connection_test_result,
    _render_template,
    _apply_runtime_secrets,
    _apply_runtime_flags,
    _workflow_ds_key_candidates,
    _unresolved_placeholders,
    resolve_secret_vars,
    resolve_default_scanner_group,
    _ds_kind_timeout_seconds,
    _ds_kind_poll_seconds,
    _ds_kind_initial_wait_seconds,
)


def _now_utc():
    return datetime.now(timezone.utc)


def run_one(base_url, token, engine, ds_kind, file_set_type, ds_name):
    step_dict = {"ds_kind": ds_kind, "ds_name": ds_name, "file_set_type": file_set_type, "is_acl": False, "is_structured_ff": False, "is_ols": False}
    candidates = _workflow_ds_key_candidates(step_dict)
    ds_tpl = ""
    for k in candidates:
        ds_tpl = get_template(engine, k)
        if ds_tpl:
            break
    if not ds_tpl:
        return False, "No DS template for %s (tried %s...)" % (ds_kind, candidates[:3])
    secret_vars = resolve_secret_vars(ds_kind)
    scanner_group = resolve_default_scanner_group(engine, None)
    ds_payload_raw = _render_template(ds_tpl, {
        "DS_NAME": ds_name, "DS_TYPE": ds_kind, "SCANNER_GROUP": scanner_group,
        "ENV_ID": "", "FILE_SET_TYPE": file_set_type, "IS_ACL": "0", "IS_OLS": "0", "IS_STRUCTURED_FF": "0",
        "FEATURE_FLAGS": "", "NOTES": "", "BUILD_VERSION": "", "BUILD_PREFIX": "",
        **{k: v for k, v in secret_vars.items() if v},
    })
    unresolved = _unresolved_placeholders(ds_payload_raw)
    if unresolved:
        return False, "Unresolved placeholders: %s" % ", ".join(unresolved[:10])
    try:
        ds_payload = json.loads(ds_payload_raw)
    except Exception as e:
        return False, "Invalid template JSON: %s" % e
    ds_payload = _apply_runtime_secrets(ds_kind, ds_payload, secret_vars)
    ds_payload = _apply_runtime_flags(step_dict, ds_payload)
    if isinstance(ds_payload, dict):
        ds_payload = dict(ds_payload)
        ds_payload["isSavedAsDraft"] = False
    try:
        put_ds_connection(base_url, token, ds_name, ds_payload)
    except Exception as e:
        return False, "PUT failed: %s" % e
    # Trigger test connection: once triggered, DS will not be in draft status.
    try:
        fetched = fetch_ds_connection(base_url, token, ds_name, timeout_seconds=10.0)
        if isinstance(fetched, dict) and fetched:
            ds_payload = fetched
    except Exception:
        pass
    ds_conn_obj = ds_payload.get("ds_connection") if isinstance(ds_payload, dict) else ds_payload
    if not isinstance(ds_conn_obj, dict):
        ds_conn_obj = ds_payload if isinstance(ds_payload, dict) else {}
    ds_conn_obj = dict(ds_conn_obj)
    ds_conn_obj["name"] = ds_name
    ds_conn_obj["id"] = ds_name
    broadcast = ("%s-test-connection-complete-%s" % (ds_name, uuid.uuid4()))[:250]
    test_started_at = _now_utc()
    try:
        test_ds_connection(base_url, token, ds_connection=ds_conn_obj, broadcast_event=broadcast, is_first_attempt=True)
    except Exception as e:
        return False, "Test trigger failed: %s" % e
    timeout_s = _ds_kind_timeout_seconds(ds_kind)
    # MSSQL often takes 5–10 min; ensure we wait long enough (test script + workflow both use 600s default for MSSQL).
    if str(ds_kind or "").strip().lower() == "mssql":
        timeout_s = max(int(timeout_s), 600)
    poll_s = _ds_kind_poll_seconds(ds_kind)
    initial_wait_s = _ds_kind_initial_wait_seconds(ds_kind)
    if initial_wait_s > 0:
        import time
        time.sleep(initial_wait_s)
    ok, msg = wait_for_ds_connection_test_result(base_url, token, ds_name, test_started_at=test_started_at, timeout_seconds=int(timeout_s), poll_seconds=int(poll_s))
    return ok, msg or "Unknown result"


def main():
    ap = argparse.ArgumentParser(description="Create DS with any name and test connection (triggering test ensures DS is not draft).")
    ap.add_argument("--mssql-only", action="store_true")
    ap.add_argument("--s3-only", action="store_true")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--name", default="", help="DS name (default: auto-generated per type, e.g. Test-MSSQL-<uuid>)")
    ap.add_argument("--name-mssql", default="", help="MSSQL DS name (overrides --name for MSSQL)")
    ap.add_argument("--name-s3", default="", help="S3 DS name (overrides --name for S3)")
    args = ap.parse_args()
    base_url = (args.base_url or os.getenv("BIGID_BASE_URL", "")).strip()
    if not base_url:
        print("Set BIGID_BASE_URL in .env or pass --base-url")
        sys.exit(1)
    try:
        token = Get_auth_token(base_url, None)
    except Exception as e:
        print("Auth failed: %s" % e)
        sys.exit(1)
    if not token:
        print("No BigID token (BIGID_AUTH_TOKEN or BIGID_AUTH_USERNAME/BIGID_AUTH_PASSWORD)")
        sys.exit(1)
    try:
        engine = create_engine(DB_CONNECTION_STRING)
    except Exception as e:
        print("DB connect failed: %s" % e)
        sys.exit(1)

    def _ds_name(kind, default_prefix):
        n = args.name_mssql if kind == "mssql" else (args.name_s3 if kind == "s3" else args.name)
        if (n or "").strip():
            return n.strip()
        return "%s-%s" % (default_prefix, uuid.uuid4().hex[:8])

    if not args.s3_only:
        name_mssql = _ds_name("mssql", "Test-MSSQL")
        print("--- MSSQL [%s] ---" % name_mssql)
        ok, msg = run_one(base_url, token, engine, "mssql", "default", name_mssql)
        print("OK" if ok else "FAIL", msg)
        if not ok:
            sys.exit(2)
    if not args.mssql_only:
        name_s3 = _ds_name("s3", "Test-S3")
        print("--- S3 [%s] ---" % name_s3)
        ok, msg = run_one(base_url, token, engine, "s3", "Small Files", name_s3)
        print("OK" if ok else "FAIL", msg)
        if not ok:
            sys.exit(2)
    print("Done. Both DS connection tests passed.")


if __name__ == "__main__":
    main()
