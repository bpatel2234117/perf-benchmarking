#!/usr/bin/env python3
"""
CLI: authenticate → create/update DS → test connection → create scan profile → trigger scan.

Either pass a custom JSON (same shape as workflow DS templates), or use a built-in template
by supplying only --ds-type and --ds-name (credentials and endpoints from environment; see below).

Usage (built-in template):
  python scripts/bigid_ds_scan_cli.py --base-url https://bigid.example.com \\
    --ds-type s3 --ds-name "My Bucket DS" \\
    --scan-profile-name "My DS Full Scan"

Usage (custom JSON):
  python scripts/bigid_ds_scan_cli.py --base-url https://bigid.example.com \\
    --ds-json ./my_ds.json \\
    --scan-profile-name "My DS Full Scan"

Built-in types: s3, smb, mssql, postgres, mysql (aliases: s3-v2, smb_v2, postgresql, rdb-mssql, …).
Templates live under scripts/ds_templates/<type>.json. Set SCANNER_GROUP, connector-specific
vars (e.g. S3_BUCKET, MSSQL_host), and secrets (same names as workflow / import_bigid_datasources).

Environment URL is passed via --base-url (no DB required).

Auth (same as dashboard):
  - BIGID_AUTH_TOKEN, or
  - BIGID_AUTH_USERNAME + BIGID_AUTH_PASSWORD (optional --username / --password)

Scan template:
  - WORKFLOW_DEFAULT_SCAN_TEMPLATE_ID, or
  - auto-resolve "Data source scan" from /api/v1/scan-templates

Connection test: same sequence as workflow_runner (test_ds_connection_auto → initial wait → wait_for_ds_connection_test_result,
WORKFLOW_DS_TEST_MAX_ATTEMPTS, 2s between retries). Scanner group: same as workflow — --scanner-group or SCANNER_GROUP env, else
resolve_default_scanner_group(engine, --workflow-env-id) from DB (workflow_templates); no BigID API guessing.

Alternate: --async-insights-flow → POST connection test only (no status poll) → create profile → wait --pre-trigger-wait-seconds (15)
→ trigger scan → wait --insights-initial-wait-seconds (default 20s) → fetch_scan_insights --insights-poll-count (10) times;
each response JSON printed to stdout, then --insights-poll-interval-seconds (default 15) before the next GET.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo))

from bigid_auth import Get_auth_token  # noqa: E402
from workflow_runner import (  # noqa: E402
    DEFAULT_SCAN_OWNER_EMAIL,
    create_scan_profile,
    fetch_ds_connection,
    fetch_scan_insights,
    fetch_scan_templates,
    guess_latest_scan_id,
    resolve_default_scanner_group,
    resolve_secret_vars,
    test_ds_connection_auto,
    trigger_scan,
    put_ds_connection,
    wait_for_ds_connection_test_result,
    _apply_runtime_flags,
    _apply_runtime_secrets,
    _ds_kind_initial_wait_seconds,
    _ds_kind_poll_seconds,
    _ds_kind_timeout_seconds,
    _extract_ds_id_from_payload,
    _extract_id,
    _now_utc,
    _render_template,
)

DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "ds_templates"


def _resolve_scanner_group_like_workflow(cli_override: str, workflow_env_id: Optional[int]) -> str:
    """Match workflow_runner: optional override, then env, then resolve_default_scanner_group(engine, env_id)."""
    o = str(cli_override or "").strip()
    if o:
        return o
    explicit = str(os.getenv("SCANNER_GROUP") or os.getenv("WORKFLOW_SCANNER_GROUP") or "").strip()
    if explicit:
        return explicit
    try:
        from sqlalchemy import create_engine  # noqa: WPS433

        from db_config import DB_CONNECTION_STRING  # noqa: E402

        eng = create_engine(DB_CONNECTION_STRING)
        return str(resolve_default_scanner_group(eng, workflow_env_id) or "").strip() or "default"
    except Exception:
        return "default"


def _normalize_ds_kind_for_workflow(dc: Dict[str, Any]) -> str:
    """Map connector type string to workflow step ds_kind (for WORKFLOW_DS_TEST_* env keys)."""
    t = str(dc.get("type") or dc.get("dsType") or "").strip().lower().replace("-", "_")
    if "s3" in t:
        return "s3"
    if "smb" in t:
        return "smb"
    if "mssql" in t or t == "rdb_mssql":
        return "mssql"
    if "postgres" in t:
        return "postgres"
    if "mysql" in t:
        return "mysql"
    return t or "s3"


def _workflow_default_step(ds_kind: str) -> Dict[str, Any]:
    """Same flags as a typical workflow step when ACL/struct are off."""
    return {
        "ds_kind": str(ds_kind or "").strip().lower(),
        "is_acl": False,
        "is_structured_ff": False,
        "is_ocr": False,
    }


def _normalize_ds_type(raw: str) -> str:
    x = str(raw or "").strip().lower().replace("-", "_")
    aliases = {
        "s3_v2": "s3",
        "postgresql": "postgres",
        "rdb_postgresql": "postgres",
        "rdb_mssql": "mssql",
        "rdb_mysql": "mysql",
    }
    x = aliases.get(x, x)
    allowed = {"s3", "smb", "mssql", "postgres", "mysql"}
    if x not in allowed:
        raise SystemExit(
            f"Unknown --ds-type {raw!r} (normalized {x!r}). "
            f"Use one of: {', '.join(sorted(allowed))} (plus common aliases like s3-v2, postgresql)."
        )
    return x


def _build_render_vars(ds_kind: str, ds_name: str, scanner_group: str) -> Dict[str, str]:
    out: Dict[str, str] = {
        "DS_NAME": ds_name,
        "SCANNER_GROUP": scanner_group,
    }
    k = ds_kind.lower()
    if k == "s3":
        out["S3_BUCKET"] = str(os.getenv("S3_BUCKET") or "").strip()
    elif k == "smb":
        out["SMB_resourceAddress"] = str(
            os.getenv("SMB_resourceAddress") or os.getenv("SMB_RESOURCE_ADDRESS") or ""
        ).strip()
    elif k == "mssql":
        out["MSSQL_rdb_url"] = str(os.getenv("MSSQL_rdb_url") or "").strip()
    elif k == "postgres":
        out["Postgres_rdb_url"] = str(os.getenv("Postgres_rdb_url") or "").strip()
    elif k == "mysql":
        out["MySQL_rdb_url"] = str(os.getenv("MySQL_rdb_url") or "").strip()
    return out


def _load_payload_from_builtin(
    ds_kind: str,
    ds_name: str,
    template_dir: Path,
    scanner_group: str,
) -> Dict[str, Any]:
    tpl = template_dir / f"{ds_kind}.json"
    if not tpl.is_file():
        raise SystemExit(f"Template not found: {tpl} (expected built-in {ds_kind}.json)")
    raw = tpl.read_text(encoding="utf-8")
    rendered = _render_template(raw, _build_render_vars(ds_kind, ds_name, scanner_group))
    try:
        data = json.loads(rendered)
    except json.JSONDecodeError as ex:
        raise SystemExit(f"Invalid JSON after template render ({tpl}): {ex}") from ex
    if not isinstance(data, dict):
        raise SystemExit("Template root must be an object")
    data = _ensure_put_shape(data)
    secret_vars = resolve_secret_vars(ds_kind)
    data = _apply_runtime_secrets(ds_kind, data, secret_vars)
    return _apply_runtime_flags(_workflow_default_step(ds_kind), data)


def _load_json(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise SystemExit("JSON root must be an object")
    return data


def _ensure_put_shape(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Match workflow_runner: PUT body with ds_connection + not draft."""
    p = dict(payload)
    if "ds_connection" not in p and isinstance(p.get("type"), str):
        # User passed inner ds_connection only
        p = {"ds_connection": p}
    if isinstance(p.get("ds_connection"), dict):
        dc = dict(p["ds_connection"])
        p["ds_connection"] = dc
        p["ds_connection"]["isSavedAsDraft"] = False
    else:
        p["isSavedAsDraft"] = False
    return p


def _ds_name_from_payload(payload: Dict[str, Any], override: str) -> str:
    if override.strip():
        return override.strip()
    dc = payload.get("ds_connection") if isinstance(payload.get("ds_connection"), dict) else payload
    if isinstance(dc, dict):
        n = str(dc.get("name") or dc.get("id") or "").strip()
        if n:
            return n
    raise SystemExit("Could not determine DS name: pass --ds-name or set ds_connection.name in JSON")


def _resolve_scan_template_id(base_url: str, token: str) -> str:
    env_id = str(os.getenv("WORKFLOW_DEFAULT_SCAN_TEMPLATE_ID", "") or "").strip()
    if env_id:
        items = fetch_scan_templates(base_url, token, limit=400, skip=0)
        for it in items or []:
            tid = str(it.get("_id") or it.get("id") or "").strip()
            if tid == env_id:
                return tid
    items = fetch_scan_templates(base_url, token, limit=200, skip=0)
    for it in items or []:
        nm = str(it.get("name") or "").strip().lower()
        if nm in {"data source scan", "datasource scan"}:
            tid = str(it.get("_id") or it.get("id") or "").strip()
            if tid:
                return tid
    raise SystemExit(
        "Could not resolve scan template id. Set WORKFLOW_DEFAULT_SCAN_TEMPLATE_ID or ensure "
        "a template named 'Data source scan' exists."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="BigID: token → DS → test → scan profile → trigger")
    ap.add_argument("--base-url", required=True, help="BigID base URL, e.g. https://host/bigid or http://elb.amazonaws.com")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--ds-json", type=Path, help="Path to DS PUT JSON (workflow template shape)")
    src.add_argument(
        "--ds-type",
        help="Built-in template: s3, smb, mssql, postgres, mysql (see scripts/ds_templates/)",
    )
    ap.add_argument(
        "--template-dir",
        type=Path,
        default=DEFAULT_TEMPLATE_DIR,
        help=f"Directory for built-in DS JSON templates (default: {DEFAULT_TEMPLATE_DIR})",
    )
    ap.add_argument("--ds-name", default="", help="DS connection name (required with --ds-type; optional override with --ds-json)")
    ap.add_argument(
        "--scanner-group",
        default="",
        help="Override scanner group (else SCANNER_GROUP env, else DB resolve_default_scanner_group like workflow)",
    )
    ap.add_argument(
        "--workflow-env-id",
        type=int,
        default=None,
        help="Optional dashboard env id for DB lookup (config::scanner_group::env=...); ignored if --scanner-group or SCANNER_GROUP is set",
    )
    ap.add_argument("--username", default="", help="Or use BIGID_AUTH_USERNAME")
    ap.add_argument("--password", default="", help="Or use BIGID_AUTH_PASSWORD")
    ap.add_argument("--scan-profile-name", default="", help="Scan profile display name (default: '<ds_name> Full Scan')")
    ap.add_argument("--scan-type", default="dsScan", help="Scan type for profile + trigger (default: dsScan)")
    ap.add_argument("--skip-test", action="store_true", help="Skip connection test (not recommended)")
    ap.add_argument(
        "--async-insights-flow",
        action="store_true",
        help="POST connection test only (no status poll); create profile; wait --pre-trigger-wait-seconds; "
        "trigger scan; wait --insights-initial-wait-seconds; fetch scan insights --insights-poll-count times",
    )
    ap.add_argument(
        "--pre-trigger-wait-seconds",
        type=int,
        default=15,
        help="With --async-insights-flow: sleep after creating scan profile before trigger (default 15)",
    )
    ap.add_argument(
        "--insights-initial-wait-seconds",
        type=int,
        default=20,
        help="With --async-insights-flow: sleep after trigger before first insights GET (default 20 seconds)",
    )
    ap.add_argument(
        "--insights-poll-count",
        type=int,
        default=10,
        help="With --async-insights-flow: number of GET / scan insights calls (default 10)",
    )
    ap.add_argument(
        "--insights-poll-interval-seconds",
        type=int,
        default=15,
        help="Sleep after each insights response before the next GET (default 15; set 0 for back-to-back)",
    )
    ap.add_argument("--skip-trigger", action="store_true", help="Create scan profile only; do not POST /scans")
    ap.add_argument("--print-token", action="store_true", help="Print access token and exit")
    ap.add_argument("--json-out", action="store_true", help="Print last JSON responses as JSON lines")
    args = ap.parse_args()
    if args.async_insights_flow and args.skip_trigger:
        raise SystemExit("--async-insights-flow needs a scan trigger; do not pass --skip-trigger")

    base_url = str(args.base_url).strip().rstrip("/")
    if args.username:
        os.environ["BIGID_AUTH_USERNAME"] = str(args.username).strip()
    if args.password:
        os.environ["BIGID_AUTH_PASSWORD"] = str(args.password).strip()

    env_row = {"base_url": base_url, "env_name": "cli"}
    token = Get_auth_token(base_url, env_row)
    if args.print_token:
        print(token)
        return 0

    print(f"[1/5] Authenticated (token length {len(token)}).", file=sys.stderr)

    wf_env_id: Optional[int] = args.workflow_env_id
    scanner_group = _resolve_scanner_group_like_workflow(str(args.scanner_group or ""), wf_env_id)

    step_ds_kind = ""
    if args.ds_json is not None:
        payload = _ensure_put_shape(_load_json(args.ds_json))
        ds_name = _ds_name_from_payload(payload, args.ds_name)
        dc0 = payload.get("ds_connection") if isinstance(payload.get("ds_connection"), dict) else payload
        wk = _normalize_ds_kind_for_workflow(dc0 if isinstance(dc0, dict) else {})
        step_ds_kind = wk
        payload = _apply_runtime_flags(_workflow_default_step(wk), payload)
    else:
        ds_name = str(args.ds_name or "").strip()
        if not ds_name:
            raise SystemExit("--ds-name is required when using --ds-type")
        kind = _normalize_ds_type(str(args.ds_type or ""))
        step_ds_kind = kind
        payload = _load_payload_from_builtin(kind, ds_name, Path(args.template_dir), scanner_group)
    if isinstance(payload.get("ds_connection"), dict):
        payload["ds_connection"]["name"] = ds_name
        payload["ds_connection"]["id"] = ds_name

    out: Dict[str, Any] = {}
    out["scanner_group"] = scanner_group

    if args.ds_json is not None:
        print(f"[2/5] PUT ds_connections/{ds_name!r} …", file=sys.stderr)
    else:
        print(f"[2/5] PUT ds_connections/{ds_name!r} (scanner_group={scanner_group!r}) …", file=sys.stderr)
    put_out = put_ds_connection(base_url, token, ds_name, payload)
    out["put_ds"] = put_out
    ds_id = _extract_ds_id_from_payload(put_out) or _extract_id(put_out, ["_id", "id", "dsId", "ds_id"])
    try:
        fetched = fetch_ds_connection(base_url, token, ds_name)
        out["get_ds"] = fetched
        if isinstance(fetched, dict):
            ds_id = ds_id or _extract_ds_id_from_payload(fetched)
            dc = fetched.get("ds_connection") if isinstance(fetched.get("ds_connection"), dict) else fetched
            if isinstance(dc, dict):
                payload = fetched
    except Exception as ex:
        print(f"WARN: fetch_ds_connection after PUT: {ex}", file=sys.stderr)

    ds_conn_obj = payload.get("ds_connection") if isinstance(payload.get("ds_connection"), dict) else payload
    if not isinstance(ds_conn_obj, dict):
        raise SystemExit("ds_connection must be a dict after load")

    if not args.skip_test:
        if args.async_insights_flow:
            print("[3/5] Test connection (POST only, no status check) …", file=sys.stderr)
            broadcast = f"{ds_name}-test-connection-complete-{uuid.uuid4()}"[:250]
            test_out = test_ds_connection_auto(
                base_url,
                token,
                ds_name=ds_name,
                ds_id=str(ds_id or ""),
                ds_connection=ds_conn_obj,
                broadcast_event=broadcast,
                is_first_attempt=True,
            )
            out["test_post"] = test_out
            out["test_ok"] = None
            out["test_message"] = "skipped_wait_for_connection_status"
            print("      (not polling connectionStatusTest)", file=sys.stderr)
        else:
            print("[3/5] Test connection …", file=sys.stderr)
            timeout_s = _ds_kind_timeout_seconds(step_ds_kind)
            poll_s = _ds_kind_poll_seconds(step_ds_kind)
            init_wait = _ds_kind_initial_wait_seconds(step_ds_kind)
            try:
                max_attempts = int(str(os.getenv("WORKFLOW_DS_TEST_MAX_ATTEMPTS", "2") or "2").strip())
            except ValueError:
                max_attempts = 2
            max_attempts = max(1, min(5, max_attempts))

            last_msg = ""
            ok = False
            test_out: Dict[str, Any] = {}
            for attempt in range(1, max_attempts + 1):
                broadcast = f"{ds_name}-test-connection-complete-{uuid.uuid4()}"[:250]
                test_started = _now_utc()
                test_out = test_ds_connection_auto(
                    base_url,
                    token,
                    ds_name=ds_name,
                    ds_id=str(ds_id or ""),
                    ds_connection=ds_conn_obj,
                    broadcast_event=broadcast,
                    is_first_attempt=(attempt == 1),
                )
                out["test_post"] = test_out
                out["connection_test_attempt"] = attempt
                out["connection_test_max_attempts"] = max_attempts
                if init_wait > 0:
                    time.sleep(init_wait)
                ok, msg = wait_for_ds_connection_test_result(
                    base_url,
                    token,
                    ds_name,
                    test_started_at=test_started,
                    timeout_seconds=int(timeout_s),
                    poll_seconds=int(poll_s),
                )
                last_msg = msg or last_msg
                if ok:
                    break
                if attempt < max_attempts:
                    time.sleep(2)
            out["test_ok"] = ok
            out["test_message"] = last_msg
            if not ok:
                print(f"ERROR: connection test did not succeed: {last_msg}", file=sys.stderr)
                if args.json_out:
                    print(json.dumps(out, default=str))
                return 2
            print(f"      OK: {last_msg}", file=sys.stderr)
    else:
        print("[3/5] Skipped (--skip-test).", file=sys.stderr)

    scan_profile_name = (args.scan_profile_name or "").strip() or f"{ds_name} Full Scan"
    template_id = _resolve_scan_template_id(base_url, token)
    scan_payload: Dict[str, Any] = {
        "scanType": str(args.scan_type or "dsScan").strip(),
        "dataSourceList": [ds_name],
        "name": scan_profile_name[:240],
        "scanTemplateId": template_id,
        "enabled": True,
    }
    if DEFAULT_SCAN_OWNER_EMAIL:
        scan_payload["owners"] = [DEFAULT_SCAN_OWNER_EMAIL]

    print(f"[4/5] Create scan profile {scan_profile_name!r} …", file=sys.stderr)
    sp_out = create_scan_profile(base_url, token, scan_payload)
    out["scan_profile"] = sp_out
    sp_id = _extract_id(sp_out, ["_id", "id", "scanProfileId", "scan_profile_id"]) or _extract_id(
        sp_out, ["scanProfile", "_id", "id"]
    )
    print(f"      scan_profile_id={sp_id or '?'}", file=sys.stderr)

    if args.skip_trigger:
        print("[5/5] Skipped (--skip-trigger).", file=sys.stderr)
        if args.json_out:
            print(json.dumps(out, default=str))
        else:
            print(json.dumps({"scan_profile_id": sp_id, "scan_profile_name": scan_profile_name, "ds_name": ds_name}, indent=2))
        return 0

    if args.async_insights_flow:
        pre_w = max(0, int(args.pre_trigger_wait_seconds or 0))
        print(f"[5a/5] Waiting {pre_w}s after scan profile before trigger …", file=sys.stderr)
        if pre_w > 0:
            time.sleep(float(pre_w))

        print("[5b/5] Trigger scan …", file=sys.stderr)
        trig = trigger_scan(base_url, token, scan_profile_name, scan_type=str(args.scan_type or "dsScan"))
        out["trigger"] = trig
        scan_id = _extract_id(trig, ["_id", "id", "scanId", "scan_id", "parentScanId", "parent_scan_id"])
        if not scan_id:
            time.sleep(min(15.0, float(os.getenv("WORKFLOW_POST_TRIGGER_SLEEP_SECONDS", "15") or "15")))
            scan_id = guess_latest_scan_id(base_url, token, name_contains=scan_profile_name, limit=80, wf_tag="")
        if not scan_id:
            print("ERROR: could not resolve parent scan id after trigger; cannot fetch insights.", file=sys.stderr)
            out["summary"] = {"parent_scan_id": "", "error": "missing_scan_id_after_trigger"}
            if args.json_out:
                print(json.dumps(out, default=str))
            return 2

        init_insights = max(0, int(args.insights_initial_wait_seconds or 0))
        if init_insights >= 60:
            print(
                f"[5c/5] Waiting {init_insights}s ({init_insights // 60} min) before scan insights …",
                file=sys.stderr,
            )
        else:
            print(f"[5c/5] Waiting {init_insights}s before scan insights …", file=sys.stderr)
        if init_insights > 0:
            time.sleep(float(init_insights))

        n_poll = max(1, min(100, int(args.insights_poll_count or 10)))
        interval = max(0, int(args.insights_poll_interval_seconds or 0))
        insights_rows: list = []
        print(
            f"[5d/5] GET scan insights ×{n_poll} (parent_scan_id={scan_id}); each response printed to stdout, "
            f"then wait {interval}s before next (unless interval=0) …",
            file=sys.stderr,
        )
        for i in range(1, n_poll + 1):
            try:
                ins = fetch_scan_insights(base_url, token, str(scan_id))
                insights_rows.append({"attempt": i, "insights": ins})
                print(f"--- scan_insights {i}/{n_poll} parent_scan_id={scan_id} ---", flush=True)
                print(json.dumps(ins, indent=2, default=str), flush=True)
                print(f"      insights {i}/{n_poll} OK (payload above on stdout)", file=sys.stderr)
            except Exception as ex:
                insights_rows.append({"attempt": i, "error": str(ex)})
                print(f"--- scan_insights {i}/{n_poll} ERROR ---", flush=True)
                print(json.dumps({"error": str(ex)}, indent=2), flush=True)
                print(f"      insights {i}/{n_poll} ERROR: {ex}", file=sys.stderr)
            if i < n_poll and interval > 0:
                print(f"      waiting {interval}s before next insights GET …", file=sys.stderr)
                time.sleep(float(interval))
        out["insights_polls"] = insights_rows

        summary = {
            "ds_name": ds_name,
            "ds_id": ds_id or "",
            "scan_profile_name": scan_profile_name,
            "scan_profile_id": sp_id or "",
            "parent_scan_id": scan_id or "",
            "base_url": base_url,
            "insights_poll_count": n_poll,
        }
        out["summary"] = summary
        if args.json_out:
            print(json.dumps(out, default=str))
        else:
            print(json.dumps(summary, indent=2))
        print("Done.", file=sys.stderr)
        return 0

    print("[5/5] Trigger scan …", file=sys.stderr)
    trig = trigger_scan(base_url, token, scan_profile_name, scan_type=str(args.scan_type or "dsScan"))
    out["trigger"] = trig
    scan_id = _extract_id(trig, ["_id", "id", "scanId", "scan_id", "parentScanId", "parent_scan_id"])
    if not scan_id:
        import time as _t

        _t.sleep(min(15.0, float(os.getenv("WORKFLOW_POST_TRIGGER_SLEEP_SECONDS", "15") or "15")))
        scan_id = guess_latest_scan_id(base_url, token, name_contains=scan_profile_name, limit=80, wf_tag="")

    summary = {
        "ds_name": ds_name,
        "ds_id": ds_id or "",
        "scan_profile_name": scan_profile_name,
        "scan_profile_id": sp_id or "",
        "parent_scan_id": scan_id or "",
        "base_url": base_url,
    }
    out["summary"] = summary

    if args.json_out:
        print(json.dumps(out, default=str))
    else:
        print(json.dumps(summary, indent=2))
    print("Done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
