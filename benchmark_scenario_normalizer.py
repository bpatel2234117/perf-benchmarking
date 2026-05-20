from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Tuple

try:
    import pandas as pd
except Exception:  # pragma: no cover - helpers still work without pandas for row dicts.
    pd = None


SCENARIO_ORDER = [
    "Base (Full Scan)",
    "ACL On",
    "Structured FF",
    "ACL + Structured FF",
    "OLS Scan",
    "ACL + OLS Scan",
    "OLS + Structured FF",
    "ACL + OLS + Structured FF",
]


def truthy_flag(v: Any) -> bool:
    try:
        if pd is not None and pd.isna(v):
            return False
    except Exception:
        pass
    if isinstance(v, str):
        return v.strip().lower() not in {"", "0", "false", "no", "none", "nan"}
    return bool(v)


def row_text(row: Mapping[str, Any], *cols: str) -> str:
    return " ".join(str(row.get(c) or "") for c in cols).strip()


def normalize_ds_name(value: Any) -> str:
    raw = str(value or "").strip()
    low = raw.lower()
    if low in {"s3", "amazon s3"}:
        return "S3"
    if low in {"smb"}:
        return "SMB"
    if low in {"mssql", "ms sql", "sql server", "microsoft sql server"}:
        return "MSSQL"
    if low in {"postgres", "postgresql"}:
        return "Postgres"
    if low in {"file-system", "filesystem", "fs"}:
        return raw
    if low in {"other (custom)", "other custom", "other"}:
        return raw
    return raw


def normalize_file_set(value: Any) -> str:
    text = str(value or "").strip()
    upper = text.upper()
    if "CSV" in upper:
        return "CSV Files"
    if "SMALL FILE" in upper:
        return "Small Files"
    if "LARGE FILE" in upper:
        return "Large Files"
    if "MIXED TABLE" in upper:
        return "Mixed Tables"
    if "MIXED" in upper:
        return "Mixed Files"
    return text


def scenario_flags(row: Mapping[str, Any]) -> Tuple[bool, bool, bool]:
    text = row_text(
        row,
        "scan_type",
        "Scan_Type",
        "scenario_type",
        "Scenario_Type",
        "feature_flags",
        "comment",
        "file_set_type",
        "scan_name",
        "ds_name",
    )
    upper = text.upper()

    is_acl = truthy_flag(row.get("is_acl"))
    if "WITHOUT ACL" in upper or "NO ACL" in upper:
        is_acl = False
    elif re.search(r"\bACL\b|\bACI\b", upper):
        is_acl = True

    is_ff = truthy_flag(row.get("is_feature_flags")) or truthy_flag(row.get("is_structured_ff"))
    if (
        "STRUCTURED DISCOVERY" in upper
        or "STRUCTURE DISCOVERY" in upper
        or "STRUCT DISC" in upper
        or "STRUCTURED FILE" in upper
    ):
        is_ff = True

    has_ols = truthy_flag(row.get("is_ols"))
    if "OLS" in upper and "NO OLS" not in upper and "WITHOUT OLS" not in upper:
        has_ols = True
    return is_acl, is_ff, has_ols


def classify_scenario(row: Mapping[str, Any]) -> str:
    is_acl, is_ff, has_ols = scenario_flags(row)
    if has_ols and is_acl and is_ff:
        return "ACL + OLS + Structured FF"
    if has_ols and is_acl:
        return "ACL + OLS Scan"
    if has_ols and is_ff:
        return "OLS + Structured FF"
    if has_ols:
        return "OLS Scan"
    if is_acl and is_ff:
        return "ACL + Structured FF"
    if is_acl:
        return "ACL On"
    if is_ff:
        return "Structured FF"
    return "Base (Full Scan)"


def matrix_group_values(row: Mapping[str, Any]) -> Tuple[str, str]:
    ds_type = normalize_ds_name(row.get("data_source_type") or row.get("Data_Source_Type"))
    file_set = normalize_file_set(row.get("file_set_type"))
    text = row_text(
        row,
        "data_source_type",
        "Data_Source_Type",
        "file_set_type",
        "scenario_type",
        "Scenario_Type",
        "scan_name",
        "ds_name",
    ).upper()
    if "CSV" in text:
        return "S3", "CSV Files"
    return ds_type, file_set


def canonical_benchmark_fields(row: Mapping[str, Any]) -> Dict[str, Any]:
    is_acl, is_ff, has_ols = scenario_flags(row)
    ds_type, matrix_file_set = matrix_group_values(row)
    file_set = str(row.get("file_set_type") or "").strip()
    if "CSV" in row_text(row, "file_set_type", "scan_name", "scenario_type", "Scenario_Type").upper():
        file_set = "S3 CSV Files"
    elif not file_set:
        file_set = matrix_file_set

    return {
        "data_source_type": ds_type,
        "file_set_type": file_set,
        "matrix_data_source_type": ds_type,
        "matrix_file_set_type": matrix_file_set,
        "scan_type": "OLS Scan" if has_ols else "Full Scan",
        "scenario_label": classify_scenario(row),
        "is_acl": is_acl,
        "is_feature_flags": is_ff,
        "is_structured_ff": is_ff,
        "is_ols": has_ols,
    }


def normalize_benchmark_dataframe(df: Any) -> Any:
    if pd is None or df is None or getattr(df, "empty", True):
        return df
    out = df.copy()
    records = out.to_dict("records")
    canon = [canonical_benchmark_fields(r) for r in records]
    cdf = pd.DataFrame(canon, index=out.index)
    for col in ["data_source_type", "file_set_type", "scan_type", "is_acl", "is_feature_flags"]:
        if col in out.columns and col in cdf.columns:
            out[col] = cdf[col]
    rename_map = {
        "Data_Source_Type": "data_source_type",
        "Scan_Type": "scan_type",
    }
    for display_col, canon_col in rename_map.items():
        if display_col in out.columns and canon_col in cdf.columns:
            out[display_col] = cdf[canon_col]
    out["matrix_data_source_type"] = cdf["matrix_data_source_type"]
    out["matrix_file_set_type"] = cdf["matrix_file_set_type"]
    out["scenario_label"] = cdf["scenario_label"]
    return out
