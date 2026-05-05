"""
Benchmark prefill + throughput math aligned with dashboard.page_add_benchmark().

Does not import Streamlit or dashboard.py — keeps v2_api lightweight.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Match dashboard.py defaults for throughput breakdown preview.
DEFAULT_SCANNER_COUNT = int(os.getenv("BENCHMARK_STANDARD_SCANNER_COUNT", "5"))
DEFAULT_THREAD_COUNT = int(os.getenv("BENCHMARK_STANDARD_TOTAL_THREADS", "50"))


def parse_iso_datetime(dt_value: Any) -> Optional[datetime]:
    s = str(dt_value or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def parse_build_version_from_text(text_value: Any) -> str:
    s = str(text_value or "")
    m = re.search(r"\b(2\d{2})[\s\-\.]{1,2}(\d{1,3})\b", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m_dot3 = re.search(r"\b(2\d{2})\.(\d{1,3})\.(\d{1,3})\b", s)
    if m_dot3:
        return f"{m_dot3.group(1)}.{m_dot3.group(2)}"
    return ""


def infer_add_benchmark_defaults_from_scan_name(scan_name: Any) -> Dict[str, Any]:
    """Same rules as dashboard.infer_add_benchmark_defaults_from_scan_name."""
    name = str(scan_name or "")
    upper_name = name.upper()

    has_small_file = "SMALL FILES" in upper_name or "SMALL FILE" in upper_name
    has_large_file = "LARGE FILES" in upper_name or "LARGE FILE" in upper_name
    has_csv_file = "CSV FILES" in upper_name or "CSV FILE" in upper_name
    has_with_aci = ("WITH ACI" in upper_name or "ACL" in upper_name) and "NO ACL" not in upper_name
    has_struct_disc = (
        "STRUCT DISC" in upper_name
        or "STRUCTURE DISCOVERY" in upper_name
        or "STRUCTURED FILES DISCOVERY" in upper_name
        or "STRUCTURED FILE DISCOVERY" in upper_name
    )

    build_version = parse_build_version_from_text(name)

    data_source_type = ""
    if re.search(r"\bMSSQL\b|\bMS SQL\b", upper_name):
        data_source_type = "MSSQL"
    elif re.search(r"\bMYSQL\b|\bMY SQL\b", upper_name):
        data_source_type = "MySQL"
    elif re.search(r"\bPOSTGRESQL\b|\bPOSTGRES\b", upper_name):
        data_source_type = "Postgres"
    elif re.search(r"\bSMB\b", upper_name):
        data_source_type = "SMB"
    elif re.search(r"\bS3\b", upper_name):
        data_source_type = "S3"
    elif re.search(r"\bORACLE\b", upper_name):
        data_source_type = "Oracle"
    elif re.search(r"\bNFS\b", upper_name):
        data_source_type = "NFS"

    scenario_type = ""
    if has_small_file:
        scenario_type = "Small Files"
    elif has_large_file:
        scenario_type = "Large Files"
    elif has_csv_file:
        scenario_type = "CSV Files"

    file_set_type = ""
    if has_small_file:
        file_set_type = "Small Files"
    elif has_large_file:
        file_set_type = "Large Files"
    elif has_csv_file:
        file_set_type = "CSV Files"

    return {
        "build_version": build_version,
        "scenario_type": scenario_type,
        "file_set_type": file_set_type,
        "is_acl": has_with_aci,
        "scan_type": "OLS Scan" if "OLS" in upper_name and "NO OLS" not in upper_name else "Full Scan",
        "is_ff": has_struct_disc,
        "is_ocr": False,
        "data_source_type": data_source_type,
    }


def infer_scan_benchmark_shape(scan_name: Any, data_source_type: Any) -> Dict[str, str]:
    """Same rules as dashboard.infer_scan_benchmark_shape (run metrics / file set label)."""
    name = str(scan_name or "")
    upper_name = name.upper()
    ds = str(data_source_type or "").strip()
    ds_upper = ds.upper()

    if "CSV" in upper_name:
        file_kind = "CSV Files"
        data_type = "Unstructured"
        scenario = "CSV File"
    elif "LARGE FILE" in upper_name or "LARGE FILES" in upper_name or re.search(r"\bLARGE\b", upper_name):
        file_kind = "Large Files"
        data_type = "Unstructured"
        scenario = "Large File"
    elif "SMALL FILE" in upper_name or "SMALL FILES" in upper_name:
        file_kind = "Small Files"
        data_type = "Unstructured"
        scenario = "Small File"
    elif "MIXED" in upper_name:
        if ds_upper in {"MSSQL", "MYSQL", "POSTGRES", "ORACLE"}:
            file_kind = "Mixed Tables"
            data_type = "Structured"
        else:
            file_kind = "Mixed Files"
            data_type = "Mixed"
        scenario = "Mixed File"
    else:
        if ds_upper in {"MSSQL", "MYSQL", "POSTGRES", "ORACLE"}:
            file_kind = "Mixed Tables"
            data_type = "Structured"
        else:
            file_kind = "Mixed Files"
            data_type = "Mixed"
        scenario = "Mixed File"

    file_set_type = f"{ds} {file_kind}".strip() if ds else file_kind
    return {
        "data_type": data_type,
        "scenario_type": scenario,
        "file_set_type": file_set_type,
    }


def split_iso_for_display(iso_val: Any) -> Dict[str, Optional[str]]:
    """Split DB/ISO timestamp into date + time strings like Streamlit date/time inputs."""
    dt = parse_iso_datetime(iso_val)
    if not dt:
        return {"date": None, "time": None}
    return {"date": dt.date().isoformat(), "time": dt.strftime("%H:%M:%S")}


def duration_hours_between(started_at: Any, completed_at: Any) -> Optional[float]:
    start_dt = parse_iso_datetime(started_at)
    end_dt = parse_iso_datetime(completed_at)
    if not start_dt or not end_dt:
        return None
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    seconds = (end_dt - start_dt).total_seconds()
    if seconds <= 0:
        return None
    return seconds / 3600.0


def compute_throughputs(
    *,
    total_gb: float,
    total_files: float,
    duration_hours: float,
    scanner_count: int,
    thread_count: int,
) -> Dict[str, float]:
    """dashboard.page_add_benchmark Save Benchmark throughput block."""
    if duration_hours <= 0:
        return {
            "throughput_cluster_gb_hr": 0.0,
            "throughput_cluster_files_hr": 0.0,
            "throughput_scanner_gb_hr": 0.0,
            "throughput_scanner_files_hr": 0.0,
            "throughput_thread_gb_hr": 0.0,
            "throughput_thread_files_hr": 0.0,
        }
    sc = max(int(scanner_count), 1)
    tc = max(int(thread_count), 1)
    tp_cluster_gb = float(total_gb) / duration_hours
    tp_cluster_files = float(total_files) / duration_hours
    return {
        "throughput_cluster_gb_hr": tp_cluster_gb,
        "throughput_cluster_files_hr": tp_cluster_files,
        "throughput_scanner_gb_hr": tp_cluster_gb / sc,
        "throughput_scanner_files_hr": tp_cluster_files / sc,
        "throughput_thread_gb_hr": tp_cluster_gb / tc,
        "throughput_thread_files_hr": tp_cluster_files / tc,
    }
