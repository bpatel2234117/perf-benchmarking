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


# Closed set of data-source types (matches the data_source_types reference table).
# normalize_ds_name() maps any input onto one of these and NEVER returns an unknown
# string — otherwise resolve_ds_id()/the save path would insert it as a brand-new
# "type", which is how scan labels (e.g. "244-10 CSV", "Postgres10K") leaked into the
# Benchmark Data dropdown. Unrecognized values collapse to "Other (custom)".
CANONICAL_DS_TYPES = (
    "S3", "SMB", "Postgres", "MSSQL", "MySQL",
    "OneDrive", "Sharepoint Online", "Oracle", "NFS", "Other (custom)",
)


def normalize_ds_name(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    # Exact canonical name (case-insensitive) wins.
    for c in CANONICAL_DS_TYPES:
        if low == c.lower():
            return c
    # Common aliases.
    if low in {"amazon s3", "aws s3"}:
        return "S3"
    if low in {"postgresql", "postgres sql"}:
        return "Postgres"
    if low in {"ms sql", "sql server", "microsoft sql server", "msql"}:
        return "MSSQL"
    if low in {"my sql"}:
        return "MySQL"
    if low in {"other custom", "other"}:
        return "Other (custom)"
    # Substring inference for labels that embed the source (e.g. "bharat-s3-1m...",
    # "Postgres10K insurance", "loadmillOnedriveV2..."). MySQL/MSSQL checked before the
    # bare "sql" forms; CSV scans default to S3 (matches add_benchmark_math).
    if "mysql" in low or "my sql" in low:
        return "MySQL"
    if "mssql" in low or "ms sql" in low or "msql" in low or "sql server" in low:
        return "MSSQL"
    if "s3" in low or "amazon s3" in low or "csv" in low:
        return "S3"
    if "postgres" in low:
        return "Postgres"
    if "onedrive" in low:
        return "OneDrive"
    if "sharepoint" in low:
        return "Sharepoint Online"
    if "smb" in low:
        return "SMB"
    if "oracle" in low:
        return "Oracle"
    if re.search(r"\bnfs\b", low):
        return "NFS"
    return "Other (custom)"


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


def canonical_combined_file_set(ds_value: Any, file_set_value: Any) -> Optional[str]:
    """Canonical, data-source-prefixed file-set name (single source of truth for scenarios).

    Returns one of: "S3 Small Files", "S3 Large Files", "S3 CSV Files", "SMB Small Files",
    "SMB Large Files", "Postgres Tables", "MSSQL Tables", "MySQL Tables" (or "<DS> CSV Files"
    for an unusual SMB CSV row). Returns None when the data source can't be confidently
    classified, so callers should leave such rows untouched rather than corrupt them.
    """
    ds = normalize_ds_name(ds_value)
    fs_upper = normalize_file_set(file_set_value).upper()
    # Relational sources collapse to "<DS> Tables" regardless of the original file-set label.
    if ds in ("Postgres", "MSSQL", "MySQL"):
        return f"{ds} Tables"
    if ds in ("S3", "SMB"):
        if "CSV" in fs_upper:
            return f"{ds} CSV Files"
        if "LARGE" in fs_upper:
            return f"{ds} Large Files"
        if "SMALL" in fs_upper:
            return f"{ds} Small Files"
        # Unknown size for a known unstructured source → don't guess.
        return None
    return None


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
    if "WITHOUT ACL" in upper or "NO ACL" in upper or "WITHOUT ACI" in upper or "NO ACI" in upper:
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
    # Preserve DB scan_type before canonical overrides (dashboard trend charts use DB values).
    if "scan_type" in out.columns:
        out["scan_type_db"] = out["scan_type"].astype(str).str.strip()
    records = out.to_dict("records")
    canon = [canonical_benchmark_fields(r) for r in records]
    cdf = pd.DataFrame(canon, index=out.index)
    for col in ["data_source_type", "file_set_type", "scan_type", "is_acl", "is_feature_flags", "is_ols"]:
        if col in out.columns and col in cdf.columns:
            out[col] = cdf[col]
        elif col in cdf.columns:
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
