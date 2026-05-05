"""
BigID HTTP → Postgres helpers for v2 FastAPI (scan_parents list re-sync + scan_insights sync).
Mirrors Streamlit Scan Status "Manual Re-sync" and per-row "Sync" behaviour without importing dashboard.py.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy import text
from sqlalchemy.engine import Engine

from bigid_auth import Get_auth_token

PARENT_SCANS_PATH = os.getenv("BIGID_PARENT_SCANS_API_PATH", "/api/v1/scans/parent-scans").strip() or "/api/v1/scans/parent-scans"
INSIGHTS_PATH = os.getenv("BIGID_PARENT_SCAN_INSIGHTS_API_PATH", "/api/v1/scans/insights/parent-scans").strip() or "/api/v1/scans/insights/parent-scans"

RUNNING_STATES_FETCH = [
    "Started",
    "Awaiting",
    "In Progress",
    "InProgress",
    "Running",
    "Queued",
    "Pending",
    "Stop Requested",
    "StopRequested",
    "Stopping",
]
COMPLETED_STATES_FETCH = ["Failed", "CompletedWithFailures", "Completed", "Stopped", "Aborted", "Cancelled", "Canceled"]

TERMINAL_STATES = ["Failed", "CompletedWithFailures", "Completed", "Stopped", "Aborted", "Cancelled", "Canceled"]


def _compact_state(s: Any) -> str:
    return str(s or "").strip().lower().replace(" ", "")


def _is_terminal_state(s: Any) -> bool:
    return _compact_state(s) in {_compact_state(x) for x in TERMINAL_STATES}


def normalize_base_url(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def build_api_url(base_url: str, api_path: str) -> str:
    base = normalize_base_url(base_url)
    p = str(api_path or "").strip()
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if not p.startswith("/"):
        p = "/" + p
    return f"{base}{p}"


def _request_get(url: str, token: str, *, params: Optional[Dict[str, Any]] = None, timeout: float = 45.0) -> requests.Response:
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(url, params=params or {}, headers=headers_raw, timeout=timeout)
    if resp.status_code in (401, 403) and token:
        resp = requests.get(url, params=params or {}, headers=headers_bearer, timeout=timeout)
    resp.raise_for_status()
    return resp


def merge_parent_scan_items(running_items: List[Dict[str, Any]], completed_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged_map: Dict[str, Dict[str, Any]] = {}
    seq = 0
    for item in running_items or []:
        if not isinstance(item, dict):
            continue
        scan_id = str(item.get("_id") or item.get("id") or "").strip()
        key = scan_id or f"running::{seq}"
        merged_map[key] = dict(item)
        seq += 1
    for item in completed_items or []:
        if not isinstance(item, dict):
            continue
        scan_id = str(item.get("_id") or item.get("id") or "").strip()
        key = scan_id or f"completed::{seq}"
        if key in merged_map:
            merged_map[key] = {**merged_map[key], **item}
        else:
            merged_map[key] = dict(item)
        seq += 1
    return list(merged_map.values())


def fetch_parent_scans_list(base_url: str, token: str, states: Optional[List[str]], limit: int, skip: int) -> List[Dict[str, Any]]:
    url = build_api_url(base_url, PARENT_SCANS_PATH)
    filter_items = []
    cleaned_states = [str(s).strip() for s in (states or []) if str(s).strip()]
    if cleaned_states:
        filter_items.append({"field": "state", "operator": "in", "value": cleaned_states})
    params: Dict[str, Any] = {
        "skip": int(skip),
        "limit": int(limit),
        "requireTotalCount": "true",
        "sort": json.dumps([{"field": "_id", "order": "desc"}], separators=(",", ":")),
    }
    if filter_items:
        params["filter"] = json.dumps(filter_items, separators=(",", ":"))
    resp = _request_get(url, token, params=params)
    payload = resp.json() if resp.content else {}
    items: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        for block in [payload.get("data"), payload]:
            if isinstance(block, dict):
                for key in ["scanParents", "parentScans", "scans", "results", "items"]:
                    if isinstance(block.get(key), list):
                        items = block[key]
                        break
            if items:
                break
        if not items:
            for key in ["scanParents", "parentScans", "scans", "results", "items"]:
                if isinstance(payload.get(key), list):
                    items = payload[key]
                    break
    return items if isinstance(items, list) else []


def resolve_scan_status_value(scan: Dict[str, Any]) -> str:
    if not isinstance(scan, dict):
        return ""
    top = str(scan.get("state") or scan.get("status") or "").strip()
    if top:
        return top
    progress = scan.get("progress")
    if isinstance(progress, dict):
        return str(progress.get("state") or "").strip()
    return ""


def _normalize_scan_type_value(raw_type: Any, template_name: Any = None) -> str:
    t = str(raw_type or "").strip()
    ct = _compact_state(t)
    tmpl = str(template_name or "").strip().lower()
    if ct in {"dsscan"}:
        return "full_scan"
    if ct in {"full_scan", "fullscan"}:
        return "full_scan"
    if "data source scan" in tmpl and ct in {"", "dsscan"}:
        return "full_scan"
    return t


def upsert_scan_parent_pg(conn, env_id: Any, scan: Dict[str, Any], status: str) -> None:
    scan_id = str(scan.get("_id") or scan.get("id") or "").strip()
    if not scan_id:
        return
    name = str(scan.get("name") or scan.get("scanName") or scan.get("displayName") or "").strip() or None
    template_name = str(scan.get("scanTemplateName") or "").strip() or None
    scan_type = _normalize_scan_type_value(scan.get("type") or "", template_name) or None
    started_at = str(scan.get("startedAt") or scan.get("startTime") or scan.get("createdAt") or "").strip() or None
    completed_at = str(scan.get("completedAt") or scan.get("endTime") or scan.get("endedAt") or "").strip() or None
    status_val = str(status or "").strip() or None
    if status_val and not _is_terminal_state(status_val):
        completed_at = None
    try:
        env_id_int = int(env_id) if env_id is not None and str(env_id).strip() != "" else None
    except Exception:
        env_id_int = None

    conn.execute(
        text(
            """
            INSERT INTO scan_parents (
                scan_id, environment_id, scan_name, scan_type, started_at, completed_at,
                template_name, scan_status, is_active, last_seen_at
            ) VALUES (
                :sid, :eid, :sn, :st, :sa, :ca, :tn, :ss, TRUE, CURRENT_TIMESTAMP
            )
            ON CONFLICT (scan_id) DO UPDATE SET
                environment_id = EXCLUDED.environment_id,
                scan_name = EXCLUDED.scan_name,
                scan_type = EXCLUDED.scan_type,
                started_at = EXCLUDED.started_at,
                completed_at = EXCLUDED.completed_at,
                template_name = EXCLUDED.template_name,
                scan_status = EXCLUDED.scan_status,
                last_seen_at = CURRENT_TIMESTAMP
            """
        ),
        {
            "sid": scan_id,
            "eid": env_id_int,
            "sn": name,
            "st": scan_type,
            "sa": started_at,
            "ca": completed_at,
            "tn": template_name,
            "ss": status_val,
        },
    )


def load_environment_row(engine: Engine, environment_id: int) -> Optional[Dict[str, Any]]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, env_name, base_url, auth_module_path, auth_function_name
                FROM test_environments
                WHERE id = :eid
                LIMIT 1
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
    }


def load_environment_for_scan_id(engine: Engine, scan_id: str) -> Optional[Dict[str, Any]]:
    sid = str(scan_id or "").strip()
    if not sid:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT te.id, te.env_name, te.base_url, te.auth_module_path, te.auth_function_name
                FROM scan_parents sp
                JOIN test_environments te ON te.id = sp.environment_id
                WHERE sp.scan_id = :sid
                LIMIT 1
                """
            ),
            {"sid": sid},
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "env_name": row[1],
        "base_url": row[2],
        "auth_module_path": row[3],
        "auth_function_name": row[4],
    }


def ensure_scan_insights_table(engine: Engine) -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS scan_insights (
        scan_id VARCHAR(128) PRIMARY KEY,
        distinct_classification_attributes BIGINT NULL,
        classifier_attribute_names TEXT NULL,
        data_source_names TEXT NULL,
        objects_with_pi BIGINT NULL,
        objects_scanned BIGINT NULL,
        objects_enumerated BIGINT NULL,
        total_classification BIGINT NULL,
        new_attributes_count BIGINT NULL,
        removed_attributes_count BIGINT NULL,
        total_size_gb REAL NULL,
        insight_payload TEXT NULL,
        attributes_payload TEXT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
    with engine.connect() as conn:
        conn.execute(text(ddl))
        conn.commit()


def fetch_parent_scan_insight_http(base_url: str, scan_id: str, token: str) -> Dict[str, Any]:
    base_path = INSIGHTS_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{scan_id}")
    resp = _request_get(url, token)
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def fetch_parent_scan_attributes_http(base_url: str, scan_id: str, token: str, *, skip: int = 0, limit: int = 200) -> Dict[str, Any]:
    base_path = INSIGHTS_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{scan_id}/attributes")
    params = {
        "skip": int(skip),
        "limit": int(limit),
        "requireTotalCount": "true",
        "sort": json.dumps([{"field": "new", "order": "desc"}], separators=(",", ":")),
        "type": "classifier",
    }
    resp = _request_get(url, token, params=params)
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def extract_scan_attributes_payload(payload: Dict[str, Any]) -> Tuple[List[Any], Optional[int]]:
    """Returns (items, total_count) — aligned with dashboard."""
    items: List[Any] = []
    total_count: Optional[int] = None
    data_block = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(payload, dict):
        if isinstance(data_block, dict):
            for key in [
                "attributes",
                "items",
                "results",
                "dataSourcesInsights",
                "classifierAttributes",
                "classificationAttributes",
                "classificationsAttributes",
                "scanAttributes",
                "classification_insights",
                "attribute_counts",
                "findings",
            ]:
                if isinstance(data_block.get(key), list):
                    items = data_block.get(key)
                    break
            if not items:
                for key in ["attributes", "items", "results"]:
                    if isinstance(payload.get(key), list):
                        items = payload.get(key)
                        break
        elif isinstance(data_block, list):
            items = data_block
        if isinstance(data_block, dict) and data_block.get("totalCount") is not None:
            try:
                total_count = int(data_block.get("totalCount"))
            except Exception:
                total_count = None
        if not items:
            for key in [
                "attributes",
                "items",
                "results",
                "dataSourcesInsights",
                "classifierAttributes",
                "classificationAttributes",
                "classificationsAttributes",
                "scanAttributes",
            ]:
                if isinstance(payload.get(key), list):
                    items = payload.get(key)
                    break
        if total_count is None and payload.get("totalCount") is not None:
            try:
                total_count = int(payload.get("totalCount"))
            except Exception:
                total_count = None
    return (items if isinstance(items, list) else []), total_count


def extract_new_removed_counts_from_attributes(payload: Dict[str, Any]) -> Tuple[int, int]:
    root = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if not isinstance(root, dict):
        root = {}
    for n_key, r_key in [
        ("newCount", "removedCount"),
        ("newlyAddedCount", "removedCount"),
        ("new", "removed"),
    ]:
        if root.get(n_key) is not None and root.get(r_key) is not None:
            try:
                return int(root.get(n_key)), int(root.get(r_key))
            except Exception:
                pass

    items, _ = extract_scan_attributes_payload(payload)
    new_count = 0
    removed_count = 0

    def to_int_or_none(val: Any) -> Optional[int]:
        try:
            if val is None or val == "":
                return None
            return int(val)
        except Exception:
            return None

    for item in items:
        if not isinstance(item, dict):
            continue
        raw_new = item.get("new")
        raw_removed = item.get("removed")
        new_val = to_int_or_none(raw_new)
        removed_val = to_int_or_none(raw_removed)
        is_new = bool(raw_new is True)
        is_removed = bool(raw_removed is True)

        change_type = str(
            item.get("changeType") or item.get("change") or item.get("status") or item.get("state") or ""
        ).strip().lower()
        if change_type in {"new", "added", "newlyadded"}:
            is_new = True
        if change_type in {"removed", "deleted"}:
            is_removed = True

        if new_val is not None and new_val >= 0:
            new_count += new_val
        elif is_new:
            new_count += 1

        if removed_val is not None and removed_val >= 0:
            removed_count += removed_val
        elif is_removed:
            removed_count += 1
    return new_count, removed_count


def extract_scan_insight_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Subset aligned with dashboard.extract_scan_insight_metrics."""

    def deep_find(obj: Any, target_keys: List[str]) -> Any:
        keys = {str(k).lower() for k in target_keys}
        stack = [obj]
        seen = set()
        while stack:
            cur = stack.pop()
            obj_id = id(cur)
            if obj_id in seen:
                continue
            seen.add(obj_id)
            if isinstance(cur, dict):
                for k, v in cur.items():
                    if str(k).lower() in keys and v is not None:
                        return v
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                for item in cur:
                    if isinstance(item, (dict, list)):
                        stack.append(item)
        return None

    root = payload if isinstance(payload, dict) else {}
    data_block = root.get("data")
    if isinstance(data_block, dict):
        root = data_block
    si_inner = root.get("scanInsights") if isinstance(root.get("scanInsights"), dict) else None
    if si_inner:
        root = si_inner

    classifications = deep_find(root, ["classifications"])
    if not isinstance(classifications, dict):
        classifications = {}

    newly_added = classifications.get("newlyAdded")
    if newly_added is None:
        newly_added = classifications.get("new")
    if newly_added is None:
        newly_added = classifications.get("added")
    if newly_added is None:
        newly_added = deep_find(root, ["newlyAdded", "new", "added"])

    classifier_attribute_names = deep_find(
        root,
        ["classifierAttributeNames", "classifier_attribute_names", "classifier_attributes", "attribute_names"],
    )
    if classifier_attribute_names is None:
        classifier_attribute_names = classifications.get("classifierAttributeNames") or classifications.get("attributeNames")

    if not classifier_attribute_names and classifications:
        keys = [k for k in classifications.keys() if k not in ["newlyAdded", "new", "total", "added", "removed", "falseRemoved"]]
        if keys:
            classifier_attribute_names = ",".join(keys)

    if isinstance(newly_added, list):
        distinct_classification_attributes = len({str(x).strip() for x in newly_added if str(x).strip()})
    elif newly_added is None:
        if isinstance(classifier_attribute_names, list):
            distinct_classification_attributes = len({str(x).strip() for x in classifier_attribute_names if str(x).strip()})
        else:
            distinct_classification_attributes = ""
    else:
        try:
            distinct_classification_attributes = int(newly_added)
        except Exception:
            distinct_classification_attributes = str(newly_added)

    if isinstance(classifier_attribute_names, list):
        classifier_attribute_names_text = ", ".join(sorted({str(x).strip() for x in classifier_attribute_names if str(x).strip()}))
    elif classifier_attribute_names is None:
        classifier_attribute_names_text = ""
    else:
        classifier_attribute_names_text = str(classifier_attribute_names)

    total_classification = classifications.get("total")
    if total_classification is None:
        total_classification = classifications.get("totalCount")
    if total_classification is None:
        total_classification = deep_find(root, ["total", "totalCount"])

    data_sources = deep_find(root, ["dataSources", "data_sources"])
    data_source_names: List[str] = []
    if isinstance(data_sources, dict):
        dsn = data_sources.get("dataSourceNames")
        if isinstance(dsn, list):
            data_source_names = [str(x).strip() for x in dsn if str(x).strip()]
    if not data_source_names:
        dsn2 = deep_find(root, ["dataSourceNames"])
        if isinstance(dsn2, list):
            data_source_names = [str(x).strip() for x in dsn2 if str(x).strip()]
    data_source_names_text = ", ".join(sorted(set(data_source_names))) if data_source_names else ""

    return {
        "distinct_classification_attributes": distinct_classification_attributes,
        "classifier_attribute_names": classifier_attribute_names_text,
        "data_source_names": data_source_names_text,
        "objects_with_pi": deep_find(root, ["objectsWithPi", "objects_with_pi", "objectsWithPI"]) or "",
        "objects_scanned": deep_find(root, ["objectsScanned", "scannedObjects", "objects_scanned"]) or "",
        "objects_enumerated": deep_find(root, ["objectsEnumerated", "enumeratedObjects", "objects_enumerated"]) or "",
        "total_classification": total_classification if total_classification is not None else "",
        "total_size_gb": deep_find(root, ["totalSize", "bytesScanned", "total_size_gb", "total_size"]) or "",
    }


def _metric_int(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        if isinstance(val, bool):
            return None
        return int(val)
    except Exception:
        return None


def _metric_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except Exception:
        return None


def upsert_scan_insights_pg(
    engine: Engine,
    scan_id: str,
    metrics: Dict[str, Any],
    insight_payload: Optional[Dict[str, Any]],
    attributes_payload: Optional[Dict[str, Any]],
) -> None:
    dca = metrics.get("distinct_classification_attributes")
    dca_sql: Optional[int]
    if isinstance(dca, int):
        dca_sql = dca
    else:
        dca_sql = _metric_int(dca)

    ins_json = json.dumps(insight_payload, ensure_ascii=False) if insight_payload is not None else None
    attr_json = json.dumps(attributes_payload, ensure_ascii=False) if attributes_payload is not None else None

    with engine.connect() as conn:
        conn.execute(
            text(
                """
                INSERT INTO scan_insights (
                    scan_id,
                    distinct_classification_attributes,
                    classifier_attribute_names,
                    data_source_names,
                    objects_with_pi,
                    objects_scanned,
                    objects_enumerated,
                    total_classification,
                    new_attributes_count,
                    removed_attributes_count,
                    total_size_gb,
                    insight_payload,
                    attributes_payload,
                    updated_at
                ) VALUES (
                    :sid, :dca, :can, :dsn, :owp, :osc, :oen, :tcl, :nac, :rac, :tgb, :ip, :ap, CURRENT_TIMESTAMP
                )
                ON CONFLICT (scan_id) DO UPDATE SET
                    distinct_classification_attributes = EXCLUDED.distinct_classification_attributes,
                    classifier_attribute_names = EXCLUDED.classifier_attribute_names,
                    data_source_names = EXCLUDED.data_source_names,
                    objects_with_pi = EXCLUDED.objects_with_pi,
                    objects_scanned = EXCLUDED.objects_scanned,
                    objects_enumerated = EXCLUDED.objects_enumerated,
                    total_classification = EXCLUDED.total_classification,
                    new_attributes_count = EXCLUDED.new_attributes_count,
                    removed_attributes_count = EXCLUDED.removed_attributes_count,
                    total_size_gb = EXCLUDED.total_size_gb,
                    insight_payload = EXCLUDED.insight_payload,
                    attributes_payload = EXCLUDED.attributes_payload,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "sid": str(scan_id),
                "dca": dca_sql,
                "can": str(metrics.get("classifier_attribute_names") or "") or None,
                "dsn": str(metrics.get("data_source_names") or "") or None,
                "owp": _metric_int(metrics.get("objects_with_pi")),
                "osc": _metric_int(metrics.get("objects_scanned")),
                "oen": _metric_int(metrics.get("objects_enumerated")),
                "tcl": _metric_int(metrics.get("total_classification")),
                "nac": _metric_int(metrics.get("new_attributes_count")),
                "rac": _metric_int(metrics.get("removed_attributes_count")),
                "tgb": _metric_float(metrics.get("total_size_gb")),
                "ip": ins_json,
                "ap": attr_json,
            },
        )
        conn.commit()


def sync_scan_insights(engine: Engine, scan_id: str) -> str:
    sid = str(scan_id or "").strip()
    if not sid:
        raise ValueError("scan_id is required")

    ensure_scan_insights_table(engine)
    env_row = load_environment_for_scan_id(engine, sid)
    if not env_row:
        raise RuntimeError(f"No environment linked to scan_id={sid} in scan_parents (cannot pick BigID base URL).")

    base_url = str(env_row.get("base_url") or "").strip()
    if not base_url:
        raise RuntimeError("test_environments.base_url is empty for this scan.")

    token = Get_auth_token(base_url, env_row)
    insight_payload = fetch_parent_scan_insight_http(base_url, sid, token)
    metrics = extract_scan_insight_metrics(insight_payload)

    attributes_payload: Optional[Dict[str, Any]] = None
    try:
        attributes_payload = fetch_parent_scan_attributes_http(base_url, sid, token, skip=0, limit=200)
        new_c, rem_c = extract_new_removed_counts_from_attributes(attributes_payload)
        metrics["new_attributes_count"] = new_c
        metrics["removed_attributes_count"] = rem_c
    except Exception:
        pass

    upsert_scan_insights_pg(engine, sid, metrics, insight_payload, attributes_payload)
    return f"Synced scan_insights for {sid}"


def resync_scan_parents_for_environment(engine: Engine, environment_id: int, limit: int = 200) -> Dict[str, Any]:
    env_row = load_environment_row(engine, environment_id)
    if not env_row:
        raise RuntimeError(f"environment_id={environment_id} not found")

    base_url = str(env_row.get("base_url") or "").strip()
    if not base_url:
        raise RuntimeError("test_environments.base_url is empty")

    token = Get_auth_token(base_url, env_row)
    lim = max(10, min(int(limit), 1000))
    running_items = fetch_parent_scans_list(base_url, token, RUNNING_STATES_FETCH, lim, 0)
    completed_items = fetch_parent_scans_list(base_url, token, COMPLETED_STATES_FETCH, lim, 0)
    merged = merge_parent_scan_items(running_items, completed_items)

    upserted = 0
    with engine.connect() as conn:
        for scan in merged:
            if not isinstance(scan, dict):
                continue
            st = resolve_scan_status_value(scan)
            upsert_scan_parent_pg(conn, env_row.get("id"), scan, st)
            upserted += 1
        conn.commit()

    return {
        "upserted": upserted,
        "running_total": len(running_items),
        "completed_total": len(completed_items),
        "merged_total": len(merged),
    }
