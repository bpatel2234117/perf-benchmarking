"""Default workflow step matrix — aligned with dashboard `_seed_default_workflow_steps` + normalize."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _stp(kind, fs=None, acl=False, ols=False, sff=False, notes="", enabled=True):
    ffs = []
    if acl:
        ffs.append("ACL")
    if ols:
        ffs.append("OLS")
    if sff:
        ffs.append("Structured")
    return {
        "ds_kind": kind,
        "scan_kind": "full_scan",
        "file_set_type": fs,
        "is_acl": acl,
        "is_ols": ols,
        "is_lite": False,
        "is_structured_ff": sff,
        "feature_flags": ", ".join(ffs) if ffs else None,
        "notes": notes or None,
        "is_enabled": bool(enabled),
    }


def _raw_seed_steps() -> List[Dict[str, Any]]:
    return [
        # Pre-flight actions first (seq 1–2; off by default). Runner applies each to every
        # free workflow-pool env (is_active + include_in_workflow_runs), not odd/even pinning.
        _stp("action", None, notes="ENABLE DS_LITE MODE", enabled=False),
        _stp("action", None, notes="DISABLE OLS FEATURE FLAGS", enabled=False),
        _stp("mssql", None, notes="MSSQL Full Scan"),
        _stp("postgres", None, notes="Postgres Full Scan"),
        _stp("s3", "Small Files", notes="S3 Small Files (Standard)"),
        _stp("smb", "Small Files", notes="SMB Small Files (Standard)"),
        _stp("s3", "Large Files", notes="S3 Large Files (Standard)"),
        _stp("smb", "Large Files", notes="SMB Large Files (Standard)"),
        _stp("s3", "Small Files", acl=True, notes="S3 Small Files WITH ACL"),
        _stp("smb", "Small Files", acl=True, notes="SMB Small Files WITH ACL"),
        _stp("s3", "Large Files", acl=True, notes="S3 Large Files WITH ACL"),
        _stp("smb", "Large Files", acl=True, notes="SMB Large Files WITH ACL"),
        _stp("s3", "CSV Files", notes="CSV Small Files WITHOUT ACL"),
        _stp("s3", "CSV Files", sff=True, notes="CSV Small Files WITHOUT ACL WITH STRUCTURED DISCOVERY"),
        _stp("s3", "CSV Files", acl=True, notes="CSV Small Files WITH ACL"),
        _stp("s3", "CSV Files", acl=True, sff=True, notes="CSV Small Files WITH ACL WITH STRUCTURED DISCOVERY"),
        # Step 17 — ENABLE OLS (only here, before OLS-tagged scans; not in pre-flight block at steps 1–2)
        _stp("action", None, notes="ENABLE OLS FEATURE FLAGS", enabled=False),
        _stp("s3", "Small Files", ols=True, notes="S3 Small Files WITH OLS"),
        _stp("smb", "Small Files", ols=True, notes="SMB Small Files WITH OLS"),
        _stp("s3", "Large Files", ols=True, notes="S3 Large Files WITH OLS"),
        _stp("smb", "Large Files", ols=True, notes="SMB Large Files WITH OLS"),
        _stp("s3", "Small Files", acl=True, ols=True, notes="S3 Small Files WITH ACL AND OLS"),
        _stp("smb", "Small Files", acl=True, ols=True, notes="SMB Small Files WITH ACL AND OLS"),
        _stp("s3", "Large Files", acl=True, ols=True, notes="S3 Large Files WITH ACL AND OLS"),
        _stp("smb", "Large Files", acl=True, ols=True, notes="SMB Large Files WITH ACL AND OLS"),
        _stp("s3", "CSV Files", ols=True, notes="CSV Small Files WITHOUT ACL WITH OLS"),
        _stp("s3", "CSV Files", sff=True, ols=True, notes="CSV Small Files WITHOUT ACL WITH STRUCTURED DISCOVERY WITH OLS"),
        _stp("s3", "CSV Files", acl=True, ols=True, notes="CSV Small Files WITH ACL WITH OLS"),
        _stp("s3", "CSV Files", acl=True, sff=True, ols=True, notes="CSV Small Files WITH ACL WITH STRUCTURED DISCOVERY WITH OLS"),
        _stp("onedrive-v2", "Small Files", notes="OneDrive Small Files (Standard)"),
        _stp("sharepoint-online-v2", "Small Files", notes="SharePoint Online Small Files (Standard)"),
        _stp("onedrive-v2", "Small Files", acl=True, notes="OneDrive Small Files WITH ACL"),
        _stp("sharepoint-online-v2", "Small Files", acl=True, notes="SharePoint Online Small Files WITH ACL"),
        _stp("onedrive-v2", "Small Files", ols=True, notes="OneDrive Small Files WITH OLS"),
        _stp("sharepoint-online-v2", "Small Files", ols=True, notes="SharePoint Online Small Files WITH OLS"),
        _stp("onedrive-v2", "Small Files", acl=True, ols=True, notes="OneDrive Small Files WITH ACL AND OLS"),
        _stp("sharepoint-online-v2", "Small Files", acl=True, ols=True, notes="SharePoint Online Small Files WITH ACL AND OLS"),
    ]


def _standard_workflow_ds_name(ds_kind: str, file_set_type: Optional[str], is_acl: bool, is_ols: bool, is_structured_ff: bool) -> str:
    k = str(ds_kind or "").strip().lower()
    fs = str(file_set_type or "").strip()
    if k == "postgres":
        return "Postgres"
    if k == "action":
        return "🛠️ Action"
    if k == "mssql":
        return "MSSQL"
    if k == "mysql":
        return "MySQL"
    if fs == "CSV Files":
        nm = "CSV Files"
        nm += " WITH ACL" if bool(is_acl) else " WITHOUT ACL"
        if bool(is_structured_ff):
            nm += " WITH STRUCTURED DISCOVERY"
        if bool(is_ols):
            nm += " WITH OLS"
        return nm
    prefix = ""
    if k == "s3":
        prefix = "S3"
    elif k == "smb":
        prefix = "SMB"
    elif k == "onedrive-v2":
        prefix = "OneDrive"
    elif k == "sharepoint-online-v2":
        prefix = "SharePoint Online"
    else:
        prefix = str(ds_kind or "").strip() or "Unknown"
    nm = f"{prefix} {fs}".strip()
    tags = []
    if bool(is_acl):
        tags.append("ACL")
    if bool(is_ols):
        tags.append("OLS")
    if bool(is_structured_ff):
        tags.append("Structured")
    if tags:
        nm += " With " + " + ".join(tags)
    return nm.strip()


def normalize_workflow_step_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(raw or {})
    ds_kind = str(row.get("ds_kind") or "").strip()
    file_set_type = str(row.get("file_set_type") or "").strip() or None
    is_acl = bool(row.get("is_acl"))
    is_ols = bool(row.get("is_ols"))
    is_structured_ff = bool(row.get("is_structured_ff"))
    feature_flags = []
    if is_acl:
        feature_flags.append("ACL")
    if is_ols:
        feature_flags.append("OLS")
    if is_structured_ff:
        feature_flags.append("Structured")
    ds_name = str(row.get("ds_name") or "").strip()
    if not ds_name:
        ds_name = _standard_workflow_ds_name(ds_kind, file_set_type, is_acl, is_ols, is_structured_ff)
    return {
        "environment_id": None,
        "ds_kind": ds_kind,
        "ds_name": ds_name,
        "scan_kind": str(row.get("scan_kind") or "full_scan").strip() or "full_scan",
        "file_set_type": file_set_type,
        "is_acl": is_acl,
        "is_ols": is_ols,
        "is_lite": bool(row.get("is_lite")),
        "is_structured_ff": is_structured_ff,
        "feature_flags": ", ".join(feature_flags) if feature_flags else None,
        "notes": str(row.get("notes") or "").strip() or None,
        "status": "pending",
        "is_enabled": bool(row.get("is_enabled", True)),
    }


def default_workflow_steps_for_new_run() -> List[Dict[str, Any]]:
    return [normalize_workflow_step_row(dict(x)) for x in _raw_seed_steps()]

