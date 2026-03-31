import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import altair as alt
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta, timezone
import hashlib
import base64
import numpy as np
import requests
import os
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from textwrap import dedent
import html
import re
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# db_config may only define DB_CONNECTION_STRING (e.g. on Windows); DB_CONFIG is optional.
try:
    from db_config import DB_CONFIG, DB_CONNECTION_STRING
except ImportError:
    try:
        from db_config import DB_CONNECTION_STRING
        DB_CONFIG = None
    except ImportError:
        DB_CONNECTION_STRING = os.getenv("DB_CONNECTION_STRING") or ""
        DB_CONFIG = None

from bigid_auth import Get_auth_token, get_saved_environment_credentials, save_environment_credentials
from scanner_performance import ensure_scanner_performance_tables, capture_scanner_performance_for_env
from dashboard_auth import ensure_rbac_tables, login_user, logout_user, check_access, get_user_permissions, hash_password, verify_password
try:
    # Optional: auto-start tracker when available (same folder as dashboard.py).
    from tracker_autostart import ensure_tracker_running  # type: ignore
except Exception:
    ensure_tracker_running = None

try:
    # Optional: auto-start workflow runner when workflows are running.
    from workflow_autostart import ensure_workflow_runner_running  # type: ignore
except Exception:
    ensure_workflow_runner_running = None

# --- Configuration & Styling ---
st.set_page_config(
    page_title="BigID Scan Benchmark Dashboard",
    page_icon="🟣",
    layout="wide",
    initial_sidebar_state="expanded"
)

# BigID Brand Colors
PRIMARY_COLOR = "#8B5CF6"  # Purple
SECONDARY_COLOR = "#6366F1"  # Indigo
ACCENT_COLOR = "#A78BFA"  # Light Purple
SUCCESS_COLOR = "#10B981"  # Green
TEXT_COLOR = "#1F2937"  # Dark Gray
LOGO_CANDIDATES = [
    Path(__file__).parent / "assets" / "BigID-next-1-5f6e8592-1d9f-4476-a5e4-b4c145ebf06e.png",
    Path("/Users/bpatel/.cursor/projects/Users-bpatel-AI-Scan-Performance-Benchmark/assets/BigID-next-1-5f6e8592-1d9f-4476-a5e4-b4c145ebf06e.png")
]
DEFAULT_CLASSIFIERS_API_PATH = (
    "/api/v1/all-classifications?skip=0&limit=600&requireTotalCount=true&"
    "filter=%5B%7B%22field%22%3A%22enabled%22%2C%22value%22%3A%5Btrue%2Cnull%5D%2C%22operator%22%3A%22in%22%7D%2C"
    "%7B%22field%22%3A%22type%22%2C%22value%22%3A%5B%22data%22%2C%22columnName%22%5D%2C%22operator%22%3A%22in%22%7D%5D"
)
INTERNAL_CLASSIFIERS_API_PATH = os.getenv("BIGID_CLASSIFIERS_API_PATH", DEFAULT_CLASSIFIERS_API_PATH).strip() or DEFAULT_CLASSIFIERS_API_PATH
# Base path for fetching a single classifier by id: GET /api/v1/all-classifications/{id}
ALL_CLASSIFICATIONS_DETAIL_BASE = os.getenv("BIGID_ALL_CLASSIFICATIONS_DETAIL_BASE", "/api/v1/all-classifications").strip() or "/api/v1/all-classifications"
INTERNAL_ABOUT_API_PATH = os.getenv("BIGID_ABOUT_API_PATH", "/api/v1/about").strip() or "/api/v1/about"
INTERNAL_PARENT_SCANS_API_PATH = os.getenv("BIGID_PARENT_SCANS_API_PATH", "/api/v1/scans/parent-scans").strip() or "/api/v1/scans/parent-scans"
INTERNAL_PARENT_SCAN_INSIGHTS_API_PATH = os.getenv("BIGID_PARENT_SCAN_INSIGHTS_API_PATH", "/api/v1/scans/insights/parent-scans").strip() or "/api/v1/scans/insights/parent-scans"
INTERNAL_SCANNER_GROUPS_API_PATH = os.getenv("BIGID_SCANNER_GROUPS_API_PATH", "/api/v1/scanner-groups").strip() or "/api/v1/scanner-groups"
INTERNAL_DS_CONNECTIONS_API_PATH = os.getenv("BIGID_DS_CONNECTIONS_API_PATH", "/api/v1/ds_connections").strip() or "/api/v1/ds_connections"
INTERNAL_DS_CONNECTION_TEST_API_PATH = os.getenv("BIGID_DS_CONNECTION_TEST_API_PATH", "/api/v1/ds-connection-test/client").strip() or "/api/v1/ds-connection-test/client"
INTERNAL_SCAN_TEMPLATES_API_PATH = os.getenv("BIGID_SCAN_TEMPLATES_API_PATH", "/api/v1/scan-templates").strip() or "/api/v1/scan-templates"

# Local LLM (optional): Ollama or OpenAI-compatible server
LOCAL_LLM_PROVIDER = os.getenv("LOCAL_LLM_PROVIDER", "").strip().lower()  # "ollama" | "openai"
LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "").strip()          # e.g. http://localhost:11434 or http://localhost:1234
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "").strip()                # e.g. llama3.1:8b
LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "").strip()            # only for OpenAI-compatible servers if needed

def get_logo_base64():
    for path in LOGO_CANDIDATES:
        try:
            if path.exists():
                with open(path, "rb") as f:
                    return base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            continue
    return None

st.markdown(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    @import url('https://fonts.googleapis.com/icon?family=Material+Icons');
    @import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined');
    
    * {{
        font-family: 'Inter', sans-serif;
    }}
    
    .reportview-container .main .block-container{{ 
        padding-top: 0 !important; 
        margin-top: 0 !important;
        max-width: 1400px;
    }}
    section.main, .main, [data-testid="stAppViewContainer"] {{
        padding-top: 0 !important;
        margin-top: 0 !important;
    }}
    [data-testid="stAppViewContainer"] > .main {{
        padding-top: 0 !important;
        margin-top: 0 !important;
    }}
    
    h1, h2, h3 {{ 
        color: {TEXT_COLOR}; 
        font-weight: 600;
        margin-top: 0 !important;
        margin-bottom: 0.5rem;
    }}
    
    h1 {{ font-size: 1.3rem; }}
    h2 {{ font-size: 1.1rem; }}
    h3 {{ font-size: 1.0rem; }}
    
    .stButton>button,
    .stFormSubmitButton>button,
    button[kind="primary"] {{
        background: {PRIMARY_COLOR} !important;
        color: white; 
        border: none;
        border-radius: 8px;
        padding: 0.5rem 1.5rem;
        font-weight: 500;
        transition: all 0.3s ease;
    }}
    .stButton>button:hover,
    .stFormSubmitButton>button:hover,
    button[kind="primary"]:hover {{
        background: {SECONDARY_COLOR} !important;
        color: white !important;
    }}
    .stButton>button {{
        width: auto !important;
        min-width: 180px;
    }}
    
    .stButton>button:hover {{
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(139, 92, 246, 0.4);
    }}
    
    div[data-testid="stMetricValue"] {{ 
        color: {PRIMARY_COLOR}; 
        font-size: 1.5rem;
        font-weight: 700;
    }}
    
    div[data-testid="stMetricLabel"] {{
        color: {TEXT_COLOR};
        font-weight: 500;
    }}
    
    .stSelectbox, .stMultiSelect, .stTextInput, .stNumberInput {{
        border-radius: 8px;
    }}
    
    [data-baseweb="select"] {{
        border-radius: 8px;
    }}
    
    .stTabs [data-baseweb="tab-list"] {{
        gap: 8px;
    }}
    
    .stTabs [data-baseweb="tab"] {{
        border-radius: 8px 8px 0 0;
        color: {TEXT_COLOR};
        font-weight: 500;
    }}
    
    .stTabs [aria-selected="true"] {{
        background-color: {PRIMARY_COLOR};
        color: white;
    }}
    
    /* Sidebar styling */
    [data-testid="stSidebar"] {{
        background: linear-gradient(180deg, {PRIMARY_COLOR} 0%, {SECONDARY_COLOR} 100%);
    }}
    
    [data-testid="stSidebar"] * {{
        color: white !important;
    }}
    
    /* Keep sidebar labels white but make input text readable */
    [data-testid="stSidebar"] input,
    [data-testid="stSidebar"] textarea,
    [data-testid="stSidebar"] [data-baseweb="select"] span,
    [data-testid="stSidebar"] [data-baseweb="select"] div,
    [data-testid="stSidebar"] [data-baseweb="select"] input {{
        color: {TEXT_COLOR} !important;
    }}
    
    [data-testid="stSidebar"] [data-baseweb="select"] > div {{
        background-color: #FFFFFF;
    }}
    
    [data-testid="stSidebar"] .stRadio > label {{
        color: white !important;
    }}
    
    /* Hide Streamlit sidebar collapse icon text */
    [data-testid="stSidebarCollapseButton"] {{
        display: none !important;
    }}
    [data-testid="stSidebarCollapseButton"] * {{
        display: none !important;
    }}
    
        [data-testid="stSidebar"] .block-container {{
            padding-top: 0.5rem;
            padding-bottom: 0.5rem;
        }}
    
    [data-testid="stSidebar"] h1, 
    [data-testid="stSidebar"] h2, 
    [data-testid="stSidebar"] h3 {{
        margin-bottom: 0.5rem;
    }}
    
    [data-testid="stSidebar"] .stRadio label {{
        font-size: 0.95rem;
    }}
    
    /* Header with logo */
    .bigid-header {{
        background: linear-gradient(135deg, {PRIMARY_COLOR} 0%, {SECONDARY_COLOR} 100%);
        padding: 1rem;
        border-radius: 12px;
        margin-bottom: 1.5rem;
        color: white;
        text-align: center;
    }}
    
    .bigid-logo {{
        font-size: 1.4rem;
        font-weight: 700;
        letter-spacing: -0.5px;
    }}
    
    .bigid-logo-img {{
        height: 28px;
        vertical-align: middle;
    }}
    </style>
""", unsafe_allow_html=True)

# --- Database ---
@st.cache_resource
def get_engine():
    try:
        # pool_recycle=3600: Reconnect every hour to avoid MySQL "gone away"
        # pool_pre_ping=True: Verify connection health before usage
        # pool_size=10, max_overflow=20: Standard pooling to avoid port exhaustion
        engine = create_engine(
            DB_CONNECTION_STRING,
            pool_recycle=3600,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20
        )
        with engine.connect() as conn: pass
        return engine
    except Exception as e:
        return None

engine = get_engine()

# --- Tracker / Runner Handlers ---

# Best-effort: auto-start tracker when Streamlit runs (no manual commands).
# Can be disabled via DISABLE_TRACKER_AUTOSTART=1.
try:
    if engine:
        if callable(ensure_tracker_running):
            ensure_tracker_running(engine)
except Exception:
    pass

# Best-effort: auto-start workflow runner if any workflow is running.
# Can be disabled via DISABLE_WORKFLOW_AUTOSTART=1.
try:
    if engine:
        if callable(ensure_workflow_runner_running):
            ensure_workflow_runner_running(engine)
except Exception:
    pass


@st.cache_data(ttl=600)
def _db_dialect_name() -> str:
    try:
        return str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
    except Exception:
        return ""


@st.cache_data(ttl=600)
def _db_has_column(table_name: str, column_name: str) -> bool:
    """
    Safe schema check (cached). Never raises.
    Supports MySQL and SQLite.
    """
    if not engine or not table_name or not column_name:
        return False
    try:
        dialect = _db_dialect_name()
        with engine.connect() as conn:
            if "mysql" in dialect:
                row = conn.execute(
                    text(f"SHOW COLUMNS FROM {table_name} LIKE :c"),
                    {"c": str(column_name)},
                ).fetchone()
                return bool(row)
            if "sqlite" in dialect:
                rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
                return any(str(r[1]) == str(column_name) for r in (rows or []) if r and len(r) > 1)
    except Exception:
        return False
    return False


def ensure_perf_indexes():
    """
    Best-effort performance indexes.
    Safe: ignored if index exists / no permissions / unsupported.
    """
    if not engine:
        return
    try:
        if st.session_state.get("_perf_indexes_done"):
            return
        st.session_state["_perf_indexes_done"] = True
    except Exception:
        # No session available; still safe to run once.
        pass

    try:
        dialect = _db_dialect_name()
        with engine.connect() as conn:
            # scan_parents: often filtered by env + status, ordered by last_seen_at.
            try:
                if "sqlite" in dialect:
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_scan_parents_env_status ON scan_parents(environment_id, scan_status)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_scan_parents_last_seen ON scan_parents(last_seen_at)"))
                else:
                    conn.execute(text("CREATE INDEX idx_scan_parents_env_status ON scan_parents(environment_id, scan_status)"))
                    conn.execute(text("CREATE INDEX idx_scan_parents_last_seen ON scan_parents(last_seen_at)"))
            except Exception:
                pass

            # scan_insights: joins on scan_id (unique), but we also sort/filter by updated_at.
            try:
                if "sqlite" in dialect:
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_scan_insights_updated ON scan_insights(updated_at)"))
                else:
                    conn.execute(text("CREATE INDEX idx_scan_insights_updated ON scan_insights(updated_at)"))
            except Exception:
                pass

            # benchmarks: common filters for trend pages.
            try:
                if "sqlite" in dialect:
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_benchmarks_active ON benchmarks(is_active)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_benchmarks_filters ON benchmarks(file_set_type, scan_type, data_source_type)"))
                else:
                    conn.execute(text("CREATE INDEX idx_benchmarks_active ON benchmarks(is_active)"))
                    conn.execute(text("CREATE INDEX idx_benchmarks_filters ON benchmarks(file_set_type, scan_type, data_source_type)"))
            except Exception:
                pass

            conn.commit()
    except Exception:
        pass


def _ensure_throughput_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure throughput columns exist and are filled when possible.
    Non-breaking: if required inputs are missing, leaves values as-is.
    """
    if df is None or df.empty:
        return df
    out = df
    try:
        if "total_scan_hours" not in out.columns:
            return out
        hours = pd.to_numeric(out["total_scan_hours"], errors="coerce")
        # Prefer Total_* columns from the flattened dataset.
        size_gb = pd.to_numeric(out["Total_Size_GB"], errors="coerce") if "Total_Size_GB" in out.columns else pd.Series([pd.NA] * len(out))
        files = pd.to_numeric(out["Total_Files"], errors="coerce") if "Total_Files" in out.columns else pd.Series([pd.NA] * len(out))

        safe_hours = hours.where((hours.notna()) & (hours > 0))
        calc_gb = size_gb / safe_hours
        calc_files = files / safe_hours

        if "throughput_cluster_gb_hr" not in out.columns:
            out["throughput_cluster_gb_hr"] = calc_gb
        else:
            existing = pd.to_numeric(out["throughput_cluster_gb_hr"], errors="coerce")
            out["throughput_cluster_gb_hr"] = existing.fillna(calc_gb)

        if "throughput_cluster_files_hr" not in out.columns:
            out["throughput_cluster_files_hr"] = calc_files
        else:
            existing2 = pd.to_numeric(out["throughput_cluster_files_hr"], errors="coerce")
            out["throughput_cluster_files_hr"] = existing2.fillna(calc_files)
    except Exception:
        return out
    return out

@st.cache_data(ttl=600)
def load_data(include_inactive=False):
    if not engine: return pd.DataFrame()
    ensure_perf_indexes()
    # Some historical rows may have NULL is_active; treat NULL as active.
    where_clause = "" if include_inactive else "WHERE COALESCE(b.is_active, 1) = 1"
    # Ensure optional columns exist (non-breaking for existing DBs)
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE benchmarks ADD COLUMN scenario_type VARCHAR(100)"))
            # Classification metrics (manual benchmark meaning):
            # - total_catalog_items is used as "New Attributes count" in your table
            # Add missing fields so Trend chart can be complete.
            try:
                conn.execute(text("ALTER TABLE benchmarks ADD COLUMN total_classification BIGINT NULL"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE benchmarks ADD COLUMN removed_attributes BIGINT NULL"))
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass
    # Join Tables for Flat View
    # Backward-compatible: if the DB user cannot ALTER, or columns don't exist yet,
    # select NULL placeholders instead of failing.
    has_total_classification = False
    has_removed_attributes = False
    try:
        has_total_classification = _db_has_column("benchmarks", "total_classification")
        has_removed_attributes = _db_has_column("benchmarks", "removed_attributes")
    except Exception:
        has_total_classification = False
        has_removed_attributes = False

    # Classification columns: prefer non-null scan_insights (tracker/sync) over benchmark copies.
    # Reason: "Save Benchmark Run" often stores total_catalog_items=0 when the form default was 0;
    # COALESCE(b, si) also wrongly keeps benchmark 0 when si has the real counts after sync.
    total_class_expr = (
        "CASE WHEN si.total_classification IS NOT NULL THEN si.total_classification ELSE b.total_classification END as Total_Classification"
        if has_total_classification
        else "si.total_classification as Total_Classification"
    )
    removed_attr_expr = (
        "CASE WHEN si.removed_attributes_count IS NOT NULL THEN si.removed_attributes_count ELSE b.removed_attributes END as Removed_Attributes"
        if has_removed_attributes
        else "si.removed_attributes_count as Removed_Attributes"
    )
    # New attributes: benchmarks.total_catalog_items is the manual field; overlay si when populated.
    new_attr_expr = (
        "CASE WHEN si.new_attributes_count IS NOT NULL THEN si.new_attributes_count ELSE b.total_catalog_items END as New_Attributes"
    )

    query = f"""
        SELECT 
            b.id,
            bu.version as Build_Version,
            COALESCE(NULLIF(b.classifier_list, ''), c.name) as Classifier_Name,
            conf.id as config_id,
            conf.scanner_count, conf.thread_count, conf.is_acl, conf.is_ocr, conf.is_feature_flags, conf.feature_flags, conf.comment,
            b.run_date as Date,
            b.scenario_type as Scenario_Type,
            b.scan_type as Scan_Type,
            b.data_source_type as Data_Source_Type,
            b.data_type as Data_Type,
            b.file_set_type as file_set_type,
            b.total_files as Total_Files,
            b.total_folders as Total_Folders,
            b.total_size_gb as Total_Size_GB,
            {new_attr_expr},
            {total_class_expr},
            {removed_attr_expr},
            b.throughput_cluster_gb_hr,
            b.throughput_scanner_gb_hr,
            b.throughput_thread_gb_hr,
            b.throughput_cluster_files_hr,
            b.throughput_scanner_files_hr,
            b.throughput_thread_files_hr,
            b.scan_start_time,
            b.scan_end_time,
            b.total_scan_hours,
            b.avg_file_size,
            b.is_active,
            b.scan_id,
            COALESCE(sp.scan_status, 'Completed') as Scan_Status,
            COALESCE(si.objects_scanned, 0) as objects_scanned
        FROM benchmarks b
        JOIN builds bu ON b.build_id = bu.id
        JOIN configurations conf ON b.config_id = conf.id
        LEFT JOIN classifiers c ON b.classifier_id = c.id
        LEFT JOIN scan_parents sp ON sp.scan_id = b.scan_id
        LEFT JOIN scan_insights si ON si.scan_id = b.scan_id
        {where_clause}
    """
    try:
        df = pd.read_sql(query, engine)

        # Normalize classification metric columns (avoid mixed str/int like '' that breaks Arrow)
        for _c in ["New_Attributes", "Total_Classification", "Removed_Attributes"]:
            if _c in df.columns:
                try:
                    df[_c] = pd.to_numeric(df[_c], errors="coerce").astype("Int64")
                except Exception:
                    pass

        # Recalculate per-scanner and per-thread throughput for display consistency
        if not df.empty:
            if 'scan_start_time' in df.columns and 'scan_end_time' in df.columns:
                start = pd.to_datetime(df['scan_start_time'], errors='coerce')
                end = pd.to_datetime(df['scan_end_time'], errors='coerce')
                calc_hours = (end - start).dt.total_seconds() / 3600
                df['total_scan_hours'] = calc_hours.combine_first(df.get('total_scan_hours'))
            try:
                sc = pd.to_numeric(df.get("scanner_count"), errors="coerce")
                tc = pd.to_numeric(df.get("thread_count"), errors="coerce")
                c_gb = pd.to_numeric(df.get("throughput_cluster_gb_hr"), errors="coerce")
                c_f = pd.to_numeric(df.get("throughput_cluster_files_hr"), errors="coerce")

                m_sc = c_gb.notna() & sc.notna() & (sc > 0)
                m_tc = c_gb.notna() & tc.notna() & (tc > 0)
                df.loc[m_sc, "throughput_scanner_gb_hr"] = (c_gb[m_sc] / sc[m_sc]).astype(float)
                df.loc[m_tc, "throughput_thread_gb_hr"] = (c_gb[m_tc] / tc[m_tc]).astype(float)

                m_sc2 = c_f.notna() & sc.notna() & (sc > 0)
                m_tc2 = c_f.notna() & tc.notna() & (tc > 0)
                df.loc[m_sc2, "throughput_scanner_files_hr"] = (c_f[m_sc2] / sc[m_sc2]).astype(float)
                df.loc[m_tc2, "throughput_thread_files_hr"] = (c_f[m_tc2] / tc[m_tc2]).astype(float)
            except Exception:
                pass
        return df
    except Exception as e:
        st.error(f"Error loading data: {e}")
        return pd.DataFrame()

# --- Pages ---

@st.fragment
def page_overview(df):
    st.markdown("## 📊 Strategic Overview")
    if df.empty:
        st.warning("No data found.")
        return

    # Sidebar Filters
    st.sidebar.header("Filters")
    
    # Version
    all_versions = sorted(df['Build_Version'].astype(str).unique())
    selected_versions = st.sidebar.multiselect("Build Version", all_versions, default=all_versions[-3:] if all_versions else [])
    
    # Data Source
    all_ds = sorted(df['Data_Source_Type'].astype(str).unique())
    selected_ds = st.sidebar.multiselect("Data Source", all_ds, default=all_ds)

    # Advanced Flags
    st.sidebar.subheader("Configuration Flags")
    show_acl = st.sidebar.checkbox("Show ACL Scans Only", False)
    show_ocr = st.sidebar.checkbox("Show OCR Scans Only", False)
    show_ff = st.sidebar.checkbox("Show Structured Discovery FF Only", False)

    # Filter Logic
    mask = (df['Build_Version'].astype(str).isin(selected_versions)) & \
           (df['Data_Source_Type'].astype(str).isin(selected_ds))
    
    if show_acl: mask = mask & (df['is_acl'] == 1)
    if show_ocr: mask = mask & (df['is_ocr'] == 1)
    if show_ff: mask = mask & (df['is_feature_flags'] == 1)

    filtered_df = df[mask]

    # Handle duplicate runs for the same build/datasource: Pick the one with minimum scan time
    def select_best_run(group):
        # 1. Try to pick fastest run by completion time
        if 'total_scan_hours' in group.columns and group['total_scan_hours'].notna().any():
            valid_time = group[group['total_scan_hours'].notna()]
            return valid_time.loc[valid_time['total_scan_hours'].idxmin()]
        
        # 2. Fallback: Pick run with highest throughput (usually the 'best' run)
        if 'throughput_cluster_gb_hr' in group.columns and group['throughput_cluster_gb_hr'].notna().any():
            valid_tp = group[group['throughput_cluster_gb_hr'].notna()]
            return valid_tp.loc[valid_tp['throughput_cluster_gb_hr'].idxmax()]
            
        return group.iloc[0]
    
    if not filtered_df.empty:
        gb = filtered_df.groupby(["Build_Version", "Data_Source_Type"], group_keys=False, sort=False)
        try:
            # Pandas 2.x: avoid deprecated behavior of including grouping columns in apply().
            filtered_df = gb.apply(select_best_run, include_groups=False).reset_index(drop=True)
        except TypeError:
            # Older Pandas: no include_groups arg.
            filtered_df = gb.apply(select_best_run).reset_index(drop=True)

    # KPIs
    st.markdown("### Key Metrics")
    k1, k2, k3, k4 = st.columns(4)
    avg_tp = filtered_df['throughput_cluster_gb_hr'].mean()
    max_tp = filtered_df['throughput_cluster_gb_hr'].max()
    avg_file_tp = filtered_df['throughput_cluster_files_hr'].mean()
    
    k1.metric("Avg Data Throughput (GB/hr)", f"{avg_tp:.2f}" if pd.notnull(avg_tp) else "0")
    k2.metric("Max Data Throughput (GB/hr)", f"{max_tp:.2f}" if pd.notnull(max_tp) else "0")
    k3.metric("Avg File Throughput (Files/hr)", f"{avg_file_tp:,.0f}" if pd.notnull(avg_file_tp) else "0")
    k4.metric("Scans Analyzed", len(filtered_df))

    st.divider()

    # Charts
    c1, c2 = st.columns(2)
    
    with c1: 
        st.subheader("Data Throughput Trends")
        if not filtered_df.empty:
            df_sorted = filtered_df.sort_values('Build_Version')
            fig = px.line(df_sorted, x='Build_Version', y='throughput_cluster_gb_hr', color='Data_Source_Type',
                          markers=True, title="Throughput (GB/hr) by Version")
            st.plotly_chart(fig, width="stretch")

    with c2:
        st.subheader("Throughput vs Content")
        if not filtered_df.empty:
            # Handle NaN values for scatter plot parameters
            plot_df = filtered_df.copy()
            plot_df['Total_Files'] = plot_df['Total_Files'].fillna(0)
            plot_df['Total_Size_GB'] = plot_df['Total_Size_GB'].fillna(0)
            
            fig2 = px.scatter(plot_df, x='Total_Files', y='throughput_cluster_gb_hr', 
                              size='Total_Size_GB', color='Data_Source_Type',
                              hover_data=['Build_Version', 'Classifier_Name'],
                              title="Throughput vs Total Files (Size = Total GB)")
            st.plotly_chart(fig2, width="stretch")

import hashlib

def get_or_create_id(conn, table, col, val):
    """Helper to get ID of existing item or insert new one."""
    if not val: return None
    # Check exist
    query = text(f"SELECT id FROM {table} WHERE {col} = :v")
    res = conn.execute(query, {'v': val}).fetchone()
    if res: return res[0]
    # Insert
    ins = text(f"INSERT INTO {table} ({col}) VALUES (:v)")
    conn.execute(ins, {'v': val})
    conn.commit()
    # Return new
    res = conn.execute(query, {'v': val}).fetchone()
    return res[0]

def get_data_source_types():
    if not engine:
        return []
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS data_source_types (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(50) UNIQUE
                )
            """))
            conn.commit()
            rows = conn.execute(text("SELECT name FROM data_source_types ORDER BY name")).fetchall()
            return [r[0] for r in rows]
    except Exception:
        return []

def ensure_build_templates_table():
    if not engine:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS build_templates (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    build_id INT,
                    template_name VARCHAR(100),
                    classifier_set TEXT,
                    UNIQUE KEY uniq_build_template (build_id, template_name),
                    FOREIGN KEY (build_id) REFERENCES builds(id)
                )
            """))
            conn.commit()
    except Exception:
        pass

def get_file_set_types(data_type=None):
    if not engine:
        return []
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS file_set_types (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) UNIQUE,
                    data_type VARCHAR(50),
                    total_files BIGINT,
                    total_folders BIGINT,
                    total_size_gb FLOAT
                )
            """))
            try:
                conn.execute(text("ALTER TABLE file_set_types ADD COLUMN data_type VARCHAR(50)"))
                conn.commit()
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE file_set_types ADD COLUMN total_files BIGINT"))
                conn.execute(text("ALTER TABLE file_set_types ADD COLUMN total_folders BIGINT"))
                conn.execute(text("ALTER TABLE file_set_types ADD COLUMN total_size_gb FLOAT"))
                conn.commit()
            except Exception:
                pass
            # One-time best-effort migration for existing rows
            try:
                conn.execute(text("""
                    UPDATE file_set_types
                    SET data_type = CASE
                        WHEN LOWER(name) LIKE '%table%' THEN 'Structured'
                        WHEN LOWER(name) LIKE '%csv%' THEN 'Unstructured'
                        WHEN LOWER(name) LIKE '%file%' THEN 'Unstructured'
                        ELSE data_type
                    END
                    WHERE data_type IS NULL OR data_type = ''
                """))
                conn.commit()
            except Exception:
                pass
            conn.commit()
            if data_type:
                rows = conn.execute(
                    text("SELECT name, total_files, total_folders, total_size_gb FROM file_set_types WHERE data_type = :dt ORDER BY name"),
                    {"dt": data_type}
                ).fetchall()
            else:
                rows = conn.execute(text("SELECT name, total_files, total_folders, total_size_gb FROM file_set_types ORDER BY name")).fetchall()
            names = [r[0] for r in rows]
            details = {r[0]: {"total_files": r[1], "total_folders": r[2], "total_size_gb": r[3]} for r in rows}
            if data_type == "Unstructured":
                names = [n for n in names if "table" not in n.lower()]
            if data_type == "Structured":
                names = [n for n in names if "file" not in n.lower() and "csv" not in n.lower()]
            return names, details
    except Exception:
        return [], {}

def get_classifier_names():
    if not engine:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT name FROM classifiers ORDER BY name")).fetchall()
            return [r[0] for r in rows]
    except Exception:
        return []


def get_classifier_names_for_build(build_version):
    """
    Return classifier names scoped to the provided build version only.
    Priority:
    1) Normalized mappings (build_classifier_mappings -> classifiers)
    2) build_templates.classifier_set (comma-separated fallback)
    """
    build_version = str(build_version or "").strip()
    if not engine or not build_version:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT c.name
                FROM builds b
                JOIN build_classifier_mappings bcm ON bcm.build_id = b.id
                JOIN classifiers c ON c.id = bcm.classifier_id
                WHERE b.version = :v
                ORDER BY c.name
            """), {"v": build_version}).fetchall()
            names = [str(r[0]).strip() for r in rows if r and r[0]]
            if names:
                return names

            # Fallback for builds saved via template-based classifier sets
            tmpl_rows = conn.execute(text("""
                SELECT bt.classifier_set
                FROM builds b
                JOIN build_templates bt ON bt.build_id = b.id
                WHERE b.version = :v
            """), {"v": build_version}).fetchall()

            fallback = set()
            for (classifier_set,) in tmpl_rows:
                raw = str(classifier_set or "").strip()
                if not raw:
                    continue
                for name in raw.split(","):
                    n = name.strip()
                    if n:
                        fallback.add(n)
            return sorted(fallback)
    except Exception:
        return []


def ensure_test_environments_table(_eng):
    if not _eng:
        return
    try:
        with _eng.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS test_environments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    env_name VARCHAR(100) UNIQUE,
                    base_url VARCHAR(500),
                    version VARCHAR(100),
                    auth_module_path VARCHAR(500),
                    auth_function_name VARCHAR(100) DEFAULT 'Get_auth_token',
                    classifiers_api_path VARCHAR(1200),
                    is_active BOOLEAN DEFAULT TRUE
                )
            """))
            try:
                conn.execute(text("ALTER TABLE test_environments ADD COLUMN auth_function_name VARCHAR(100) DEFAULT 'Get_auth_token'"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE test_environments ADD COLUMN classifiers_api_path VARCHAR(1200)"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE test_environments MODIFY COLUMN classifiers_api_path VARCHAR(1200)"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE test_environments ADD COLUMN is_busy BOOLEAN DEFAULT FALSE"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE test_environments ADD COLUMN busy_by_run_id INT NULL"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE test_environments ADD COLUMN busy_since DATETIME NULL"))
            except Exception:
                pass
            try:
                conn.execute(
                    text(
                        "ALTER TABLE test_environments ADD COLUMN include_in_workflow_runs BOOLEAN DEFAULT TRUE"
                    )
                )
            except Exception:
                pass

            # Supporting tables for version history + build-wise classifier mappings.
            # These are used by Version Comparison and environment history views.
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS builds (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    version VARCHAR(50) UNIQUE
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS classifiers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) UNIQUE
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS test_environment_version_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    environment_id INT NOT NULL,
                    version_value VARCHAR(100),
                    classifier_set_id INT NULL,
                    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    change_note VARCHAR(255),
                    FOREIGN KEY (environment_id) REFERENCES test_environments(id)
                )
            """))
            try:
                conn.execute(text("ALTER TABLE test_environment_version_history ADD COLUMN classifier_set_id INT NULL"))
            except Exception:
                pass
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS classifier_sets (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    set_hash VARCHAR(64) UNIQUE,
                    classifier_count INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS classifier_set_members (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    set_id INT NOT NULL,
                    classifier_id INT NOT NULL,
                    UNIQUE KEY uniq_set_classifier (set_id, classifier_id),
                    FOREIGN KEY (set_id) REFERENCES classifier_sets(id),
                    FOREIGN KEY (classifier_id) REFERENCES classifiers(id)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS build_classifier_mappings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    build_id INT NOT NULL,
                    classifier_id INT NOT NULL,
                    environment_id INT NULL,
                    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_build_classifier_env (build_id, classifier_id, environment_id),
                    FOREIGN KEY (build_id) REFERENCES builds(id),
                    FOREIGN KEY (classifier_id) REFERENCES classifiers(id),
                    FOREIGN KEY (environment_id) REFERENCES test_environments(id)
                )
            """))
            try:
                conn.execute(text("ALTER TABLE build_classifier_mappings DROP INDEX uniq_build_classifier"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE build_classifier_mappings ADD UNIQUE KEY uniq_build_classifier_env (build_id, classifier_id, environment_id)"))
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass

if engine:
    @st.cache_resource
    def initialize_app_tables(_eng):
        """Run expensive DDL checks only once per application instance."""
        ensure_rbac_tables(_eng)
        ensure_test_environments_table(_eng)
        # Other table checks will run if they don't exist during first loader hit,
        # but these core ones are ensured at startup.
        return True
    
    initialize_app_tables(engine)


def ensure_classifier_details_snapshots_table():
    """Table to store classifier detail snapshots per build for comparison."""
    if not engine:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS classifier_details_snapshots (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    environment_id INT NULL,
                    build_id INT NOT NULL,
                    bigid_classifier_id VARCHAR(64) NOT NULL,
                    classifier_name VARCHAR(500) NOT NULL,
                    snapshot_json LONGTEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_env_build_classifier (environment_id, build_id, bigid_classifier_id),
                    FOREIGN KEY (build_id) REFERENCES builds(id),
                    FOREIGN KEY (environment_id) REFERENCES test_environments(id)
                )
            """))
            conn.commit()
    except Exception:
        pass


def save_classifier_details_snapshot(environment_id, build_id, bigid_classifier_id, classifier_name, detail_dict):
    """Store a classifier detail snapshot for (env, build, classifier id)."""
    if not engine or not detail_dict:
        return
    import json
    snapshot = {k: detail_dict.get(k) for k in CLASSIFIER_DETAIL_KEYS if k in detail_dict}
    snapshot_json = json.dumps(snapshot, default=str)
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO classifier_details_snapshots (environment_id, build_id, bigid_classifier_id, classifier_name, snapshot_json)
                VALUES (:eid, :bid, :cid, :cname, :json)
                ON DUPLICATE KEY UPDATE classifier_name = VALUES(classifier_name), snapshot_json = VALUES(snapshot_json), created_at = CURRENT_TIMESTAMP
            """), {"eid": environment_id, "bid": int(build_id), "cid": str(bigid_classifier_id)[:64], "cname": str(classifier_name)[:500], "json": snapshot_json})
            conn.commit()
    except Exception:
        pass


def load_classifier_snapshots_for_build(environment_id, build_id):
    """Return list of {bigid_classifier_id, classifier_name, snapshot_dict} for given env and build."""
    if not engine:
        return []
    import json
    try:
        with engine.connect() as conn:
            # If environment_id is None (All environments), we take the latest snapshot for this build from ANY environment.
            if environment_id is None:
                query = """
                    SELECT bigid_classifier_id, classifier_name, snapshot_json
                    FROM classifier_details_snapshots
                    WHERE build_id = :bid
                    GROUP BY bigid_classifier_id
                    ORDER BY classifier_name
                """
                params = {"bid": int(build_id)}
            else:
                query = """
                    SELECT bigid_classifier_id, classifier_name, snapshot_json
                    FROM classifier_details_snapshots
                    WHERE build_id = :bid AND environment_id = :eid
                    ORDER BY classifier_name
                """
                params = {"bid": int(build_id), "eid": int(environment_id)}
            
            rows = conn.execute(text(query), params).fetchall()
            out = []
            for r in (rows or []):
                try:
                    snap = json.loads(r[2]) if r[2] else {}
                except Exception:
                    snap = {}
                out.append({"bigid_classifier_id": r[0], "classifier_name": r[1], "snapshot": snap})
            return out
    except Exception:
        return []


def load_classifier_detail_snapshot_builds_summary(environment_id: Optional[int]) -> pd.DataFrame:
    """
    Build versions that have at least one row in classifier_details_snapshots (metadata for Deep-Dive).
    When environment_id is None, counts across all environments.
    """
    if not engine:
        return pd.DataFrame(columns=["build_version", "snapshot_rows"])
    try:
        with engine.connect() as conn:
            if environment_id is None:
                rows = conn.execute(
                    text(
                        """
                        SELECT b.version, COUNT(*) AS snapshot_rows
                        FROM classifier_details_snapshots cds
                        INNER JOIN builds b ON b.id = cds.build_id
                        WHERE TRIM(COALESCE(b.version, '')) <> ''
                        GROUP BY cds.build_id, b.version
                        """
                    )
                ).fetchall()
            else:
                rows = conn.execute(
                    text(
                        """
                        SELECT b.version, COUNT(*) AS snapshot_rows
                        FROM classifier_details_snapshots cds
                        INNER JOIN builds b ON b.id = cds.build_id
                        WHERE cds.environment_id = :eid
                          AND TRIM(COALESCE(b.version, '')) <> ''
                        GROUP BY cds.build_id, b.version
                        """
                    ),
                    {"eid": int(environment_id)},
                ).fetchall()
        if not rows:
            return pd.DataFrame(columns=["build_version", "snapshot_rows"])
        df = pd.DataFrame(rows, columns=["build_version", "snapshot_rows"])
        df["build_version"] = df["build_version"].astype(str).str.strip()

        def _bk(v: str):
            try:
                return float(v)
            except Exception:
                return float("inf")

        df["_k"] = df["build_version"].map(lambda x: _bk(str(x)))
        df = df.sort_values("_k").drop(columns=["_k"]).reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame(columns=["build_version", "snapshot_rows"])


def get_test_environment_by_id(env_id):
    """
    Returns the environment record (dict) for a given environment ID.
    """
    if not engine or env_id is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT id, env_name, base_url, version, auth_module_path, auth_function_name, classifiers_api_path
                FROM test_environments WHERE id = :eid
            """), {"eid": int(env_id)}).fetchone()
            if row:
                return {
                    "id": row[0],
                    "env_name": row[1],
                    "base_url": row[2],
                    "version": row[3],
                    "auth_module_path": row[4],
                    "auth_function_name": row[5],
                    "classifiers_api_path": row[6]
                }
    except Exception:
        pass
    return None


@st.cache_data(ttl=600)
def load_test_environments():
    if not engine:
        return pd.DataFrame()
    ensure_test_environments_table(engine)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, env_name, base_url, version, auth_module_path, auth_function_name, classifiers_api_path, is_active,
                       COALESCE(include_in_workflow_runs, 1) AS include_in_workflow_runs
                FROM test_environments
                ORDER BY is_active DESC, env_name ASC
            """)).fetchall()
            cols = [
                "id",
                "env_name",
                "base_url",
                "version",
                "auth_module_path",
                "auth_function_name",
                "classifiers_api_path",
                "is_active",
                "include_in_workflow_runs",
            ]
            return pd.DataFrame(rows, columns=cols)
    except Exception:
        return pd.DataFrame()


def normalize_base_url(base_url):
    if not base_url:
        return ""
    return str(base_url).strip().rstrip("/")


def build_api_url(base_url, api_path):
    base = normalize_base_url(base_url)
    path = (api_path or "").strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


def fetch_about(base_url: str, token: str):
    """GET /api/v1/about from environment."""
    url = build_api_url(base_url, "/api/v1/about")
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = requests.get(url, headers=headers_raw, timeout=20)
        if resp.status_code in (401, 403) and token:
            resp = requests.get(url, headers=headers_bearer, timeout=20)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}


def extract_token_from_result(result):
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, (list, tuple)) and result:
        first = result[0]
        if isinstance(first, str):
            return first.strip()
    if isinstance(result, dict):
        for key in ["access_token", "token", "auth_token", "jwt", "id_token"]:
            if key in result and result[key]:
                return str(result[key]).strip()
    return ""


def extract_token_from_payload(payload):
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""

    # Direct token keys
    for key in ["access_token", "token", "auth_token", "jwt", "id_token", "authorization"]:
        if payload.get(key):
            return str(payload.get(key)).strip()

    # Nested structures often used by APIs
    for outer_key in ["data", "result", "response"]:
        obj = payload.get(outer_key)
        if isinstance(obj, dict):
            for key in ["access_token", "token", "auth_token", "jwt", "id_token", "authorization"]:
                if obj.get(key):
                    return str(obj.get(key)).strip()
        elif isinstance(obj, str) and obj.strip():
            return obj.strip()
    return ""


def fetch_auth_token_for_env(env_row):
    if "auth_token_cache" not in st.session_state:
        st.session_state["auth_token_cache"] = {}

    base_url = normalize_base_url((env_row or {}).get("base_url"))
    if not base_url:
        raise RuntimeError("Base URL is required for auth token fetch.")
    cached = st.session_state["auth_token_cache"].get(base_url)
    if cached:
        return cached

    attempts = [
        lambda: Get_auth_token(base_url, env_row),
        lambda: Get_auth_token(base_url),
        lambda: Get_auth_token()
    ]
    last_err = None
    for call in attempts:
        try:
            token = extract_token_from_result(call())
            if token:
                st.session_state["auth_token_cache"][base_url] = token
                return token
        except TypeError as te:
            last_err = te
            continue
        except Exception as ex:
            last_err = ex
            break

    raise RuntimeError(f"Auth token fetch failed using project-local Get_auth_token: {last_err}")


def extract_classifier_payload(payload):
    """
    Returns (classifier_names, total_count_from_api).
    Supports the BigID response shape:
    {"data": {"classifications": [...], "totalCount": 72}}
    """
    items = []
    total_count = None

    if isinstance(payload, dict):
        data_block = payload.get("data")
        if isinstance(data_block, dict):
            if isinstance(data_block.get("classifications"), list):
                items = data_block.get("classifications")
            if data_block.get("totalCount") is not None:
                try:
                    total_count = int(data_block.get("totalCount"))
                except Exception:
                    total_count = None

        if not items:
            for key in ["results", "items", "classifiers"]:
                if isinstance(payload.get(key), list):
                    items = payload.get(key)
                    break

    names = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                n = item.strip()
                if n:
                    names.append(n)
                continue
            if isinstance(item, dict):
                n = (
                    item.get("classification_name")
                    or item.get("name")
                    or item.get("classifierName")
                    or item.get("classifier_name")
                    or item.get("title")
                )
                if n:
                    names.append(str(n).strip())

    unique = []
    seen = set()
    for n in names:
        key = n.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(n)

    return unique, total_count


def fetch_classifiers_from_api(base_url, api_path, token):
    url = build_api_url(base_url, api_path)
    # BigID sample uses raw token in "authorization" header (not Bearer).
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}

    resp = requests.get(url, headers=headers_raw, timeout=30)
    if resp.status_code in (401, 403) and token:
        resp = requests.get(url, headers=headers_bearer, timeout=30)

    if resp.status_code in (401, 403):
        if not token:
            raise RuntimeError(
                "Classifiers API requires authentication token. "
                "Set BIGID_AUTH_TOKEN or BIGID_AUTH_USERNAME/BIGID_AUTH_PASSWORD."
            )
        raise RuntimeError("Authentication token is invalid or expired for classifiers API.")

    resp.raise_for_status()
    payload = resp.json()
    names, total_count = extract_classifier_payload(payload)
    return names, total_count, url


def fetch_classifier_list_with_ids_from_api(base_url, api_path, token):
    """Same as list API but returns list of {id, name} for detail fetch."""
    url = build_api_url(base_url, api_path)
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(url, headers=headers_raw, timeout=30)
    if resp.status_code in (401, 403) and token:
        resp = requests.get(url, headers=headers_bearer, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Classifiers list API returned {resp.status_code}")
    return extract_classifier_list_with_ids(resp.json())


def extract_classifier_list_with_ids(payload):
    """Return list of {id, name} from list API response (data.classifications or results)."""
    items = []
    if isinstance(payload, dict):
        data_block = payload.get("data")
        if isinstance(data_block, dict) and isinstance(data_block.get("classifications"), list):
            items = data_block.get("classifications")
        if not items:
            for key in ["results", "items", "classifiers"]:
                if isinstance(payload.get(key), list):
                    items = payload.get(key)
                    break
    out = []
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        bid = item.get("_id") or item.get("id")
        name = (item.get("classification_name") or item.get("name") or item.get("classifier_name") or item.get("classifier_name") or "").strip()
        if bid and name:
            out.append({"id": str(bid).strip(), "name": name})
    return out


# Keys from classifier detail API we store for build-by-build comparison (may change between builds)
CLASSIFIER_DETAIL_KEYS = [
    "classification_name", "category", "classifierType", "dataType", "original_name",
    "detection_sensitivity", "enabled", "min_length", "max_length",
    "proximity_after", "proximity_before", "nst_proximity_after", "nst_proximity_before",
    "description",
]


def fetch_classifier_detail_from_api(base_url, classifier_id, token):
    """GET /api/v1/all-classifications/{id} OR /api/v1/classifications/{id} -> detail data dict or None."""
    paths = [
        f"{ALL_CLASSIFICATIONS_DETAIL_BASE.rstrip('/')}/{classifier_id}",
        f"/api/v1/classifications/{classifier_id}"
    ]
    
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
    
    for path in paths:
        url = build_api_url(base_url, path)
        try:
            resp = requests.get(url, headers=headers_raw, timeout=20)
            if resp.status_code in (401, 403) and token:
                resp = requests.get(url, headers=headers_bearer, timeout=20)
            
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("data"), dict):
                    return data.get("data")
                return data if isinstance(data, dict) else None
        except Exception:
            continue
    return None


def fetch_classifiers_with_auth_retry(env_row):
    """
    Fetch classifiers in a smooth flow:
    1) Try without auth (same pattern as version API).
    2) If unauthorized, fetch token and retry.
    3) If token seems expired, refresh once and retry.
    """
    env_row = env_row or {}
    base_url = env_row.get("base_url")
    api_path = env_row.get("classifiers_api_path") or INTERNAL_CLASSIFIERS_API_PATH

    try:
        return fetch_classifiers_from_api(base_url, api_path, "")
    except Exception as ex:
        msg = str(ex).lower()
        if "requires authentication token" in msg or "unauthorized" in msg:
            token = fetch_auth_token_for_env(env_row)
            return fetch_classifiers_from_api(base_url, api_path, token)

        if "invalid or expired" in msg and "auth_token_cache" in st.session_state:
            # Clear cached token and retry once with a fresh token
            try:
                key = normalize_base_url(base_url)
                st.session_state.get("auth_token_cache", {}).pop(key, None)
            except Exception:
                pass
            fresh = fetch_auth_token_for_env(env_row)
            return fetch_classifiers_from_api(base_url, api_path, fresh)
        raise


def extract_parent_scans_payload(payload):
    """
    Returns (scan_items, total_count_from_api) for parent scans response.
    """
    items = []
    total_count = None

    if isinstance(payload, dict):
        data_block = payload.get("data")
        if isinstance(data_block, dict):
            for key in ["scanParents", "parentScans", "scans", "results", "items"]:
                if isinstance(data_block.get(key), list):
                    items = data_block.get(key)
                    break
            if data_block.get("totalCount") is not None:
                try:
                    total_count = int(data_block.get("totalCount"))
                except Exception:
                    total_count = None

        if not items:
            for key in ["scanParents", "parentScans", "scans", "results", "items"]:
                if isinstance(payload.get(key), list):
                    items = payload.get(key)
                    break
        if total_count is None and payload.get("totalCount") is not None:
            try:
                total_count = int(payload.get("totalCount"))
            except Exception:
                total_count = None

    return items if isinstance(items, list) else [], total_count


def fetch_parent_scans_from_api(base_url, token, states=None, skip=0, limit=200):
    """
    Fetch parent scans with optional state filter.
    """
    url = build_api_url(base_url, INTERNAL_PARENT_SCANS_API_PATH)
    # Do NOT over-filter by initiatedSource; it can hide real scans in some envs.
    # Keep optional gating via BIGID_PARENT_SCANS_FILTER_INITIATED_SOURCE=1.
    filter_items = []
    try:
        gate = str(os.getenv("BIGID_PARENT_SCANS_FILTER_INITIATED_SOURCE", "0") or "0").strip().lower() in {"1", "true", "yes", "y"}
    except Exception:
        gate = False
    if gate:
        filter_items.append({"field": "initiatedSource", "value": ["scans", "system", "scheduler", None], "operator": "in"})
    cleaned_states = [str(s).strip() for s in (states or []) if str(s).strip()]
    if cleaned_states:
        filter_items.insert(0, {"field": "state", "operator": "in", "value": cleaned_states})

    params = {
        "skip": int(skip),
        "limit": int(limit),
        "requireTotalCount": "true",
        "filter": json.dumps(filter_items, separators=(",", ":")),
        "sort": json.dumps([{"field": "_id", "order": "desc"}], separators=(",", ":"))
    }
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}

    resp = requests.get(url, params=params, headers=headers_raw, timeout=45)
    if resp.status_code in (401, 403) and token:
        resp = requests.get(url, params=params, headers=headers_bearer, timeout=45)

    if resp.status_code in (401, 403):
        if not token:
            raise RuntimeError(
                "Scans API requires authentication token. "
                "Set BIGID_AUTH_TOKEN or BIGID_AUTH_USERNAME/BIGID_AUTH_PASSWORD."
            )
        raise RuntimeError("Authentication token is invalid or expired for scans API.")

    resp.raise_for_status()
    payload = resp.json()
    items, total_count = extract_parent_scans_payload(payload)
    return items, total_count, getattr(resp, "url", url)


def fetch_parent_scans_with_auth_retry(env_row, states=None, skip=0, limit=200):
    env_row = env_row or {}
    base_url = env_row.get("base_url")

    try:
        return fetch_parent_scans_from_api(base_url, "", states=states, skip=skip, limit=limit)
    except Exception as ex:
        msg = str(ex).lower()
        if "requires authentication token" in msg or "unauthorized" in msg:
            token = fetch_auth_token_for_env(env_row)
            return fetch_parent_scans_from_api(base_url, token, states=states, skip=skip, limit=limit)

        if "invalid or expired" in msg and "auth_token_cache" in st.session_state:
            try:
                key = normalize_base_url(base_url)
                st.session_state.get("auth_token_cache", {}).pop(key, None)
            except Exception:
                pass
            fresh = fetch_auth_token_for_env(env_row)
            return fetch_parent_scans_from_api(base_url, fresh, states=states, skip=skip, limit=limit)

        token = fetch_auth_token_for_env(env_row)
        return fetch_parent_scans_from_api(base_url, token, states=states, skip=skip, limit=limit)


def merge_parent_scan_items(running_items, completed_items):
    """
    Merge running + completed parent scans by scan id.
    Completed payload wins for overlapping keys.
    """
    merged_map = {}
    seq = 0
    for item in (running_items or []):
        if not isinstance(item, dict):
            continue
        scan_id = str(item.get("_id") or item.get("id") or "").strip()
        key = scan_id or f"running::{seq}"
        merged_map[key] = dict(item)
        seq += 1
    for item in (completed_items or []):
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


def fetch_combined_parent_scans_for_env(env_row, running_states, completed_states, limit=200):
    """
    Fetch running+completed scans for one environment and return merged list.
    """
    running_items, running_total, _ = fetch_parent_scans_with_auth_retry(
        env_row, states=running_states, skip=0, limit=int(limit)
    )
    completed_items, completed_total, _ = fetch_parent_scans_with_auth_retry(
        env_row, states=completed_states, skip=0, limit=int(limit)
    )
    merged_items = merge_parent_scan_items(running_items, completed_items)
    return {
        "items": merged_items,
        "running_total": running_total,
        "completed_total": completed_total,
    }


def fetch_parent_scan_insight_from_api(base_url, scan_id, token):
    """
    Fetch insights for a specific parent scan id.
    """
    if not scan_id:
        raise RuntimeError("scan_id is required for scan insights API.")
    base_path = INTERNAL_PARENT_SCAN_INSIGHTS_API_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{scan_id}")
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}

    resp = requests.get(url, headers=headers_raw, timeout=45)
    if resp.status_code in (401, 403) and token:
        resp = requests.get(url, headers=headers_bearer, timeout=45)

    if resp.status_code in (401, 403):
        if not token:
            raise RuntimeError(
                "Scan insights API requires authentication token. "
                "Set BIGID_AUTH_TOKEN or BIGID_AUTH_USERNAME/BIGID_AUTH_PASSWORD."
            )
        raise RuntimeError("Authentication token is invalid or expired for scan insights API.")

    resp.raise_for_status()
    payload = resp.json()
    return payload, getattr(resp, "url", url)


def fetch_parent_scan_insight_with_auth_retry(env_row, scan_id):
    env_row = env_row or {}
    base_url = env_row.get("base_url")
    try:
        return fetch_parent_scan_insight_from_api(base_url, scan_id, "")
    except Exception as ex:
        msg = str(ex).lower()
        if "requires authentication token" in msg or "unauthorized" in msg:
            token = fetch_auth_token_for_env(env_row)
            return fetch_parent_scan_insight_from_api(base_url, scan_id, token)

        if "invalid or expired" in msg and "auth_token_cache" in st.session_state:
            try:
                key = normalize_base_url(base_url)
                st.session_state.get("auth_token_cache", {}).pop(key, None)
            except Exception:
                pass
            fresh = fetch_auth_token_for_env(env_row)
            return fetch_parent_scan_insight_from_api(base_url, scan_id, fresh)

        token = fetch_auth_token_for_env(env_row)
        return fetch_parent_scan_insight_from_api(base_url, scan_id, token)

def capture_scan_progress_snapshot(env_row, scan_item, force=False):
    """
    Fetches latest insights and saves a progress record if cooldown passed or forced.
    """
    if not engine:
        return
    scan_id = str(scan_item.get("_id") or scan_item.get("id") or "").strip()
    if not scan_id:
        return
    
    # Check cooldown (10 minutes = 600 seconds).
    if not force:
        last_ts = get_last_progress_timestamp(scan_id)
        if last_ts:
            try:
                # Skip cooldown if the last snapshot we captured was all zeros (bug recovery)
                is_zero = check_last_snapshot_is_zero(scan_id)
                if not is_zero:
                    now = datetime.now()
                    # If last_ts is a naive datetime, make sure comparison is valid
                    diff = (now - last_ts).total_seconds()
                    if diff < 600:
                        return 
            except Exception:
                pass

    try:
        payload_tuple = fetch_parent_scan_insight_with_auth_retry(env_row, scan_id)
        if payload_tuple and isinstance(payload_tuple, tuple) and payload_tuple[0]:
            insight_payload = payload_tuple[0]
            scan_name = str(scan_item.get("name") or scan_item.get("scanName") or scan_item.get("displayName") or "").strip()
            upsert_scan_progress_record(env_row.get("id"), scan_id, scan_name, insight_payload)
    except Exception:
        pass


def extract_scan_attributes_payload(payload):
    """
    Returns (items, total_count) for scan attributes response.
    """
    items = []
    total_count = None
    if isinstance(payload, dict):
        data_block = payload.get("data")
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
            ]:
                if isinstance(data_block.get(key), list):
                    items = data_block.get(key)
                    break
            if data_block.get("totalCount") is not None:
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
    return items if isinstance(items, list) else [], total_count


def extract_new_removed_counts_from_attributes(payload):
    """
    Count new/removed classifier attributes from attributes payload.
    """
    root = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if not isinstance(root, dict):
        root = {}

    # Prefer server-provided aggregate counts when available.
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

    def to_int_or_none(val):
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
            item.get("changeType")
            or item.get("change")
            or item.get("status")
            or item.get("state")
            or ""
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


def fetch_parent_scan_attributes_from_api(base_url, scan_id, token, skip=0, limit=200):
    """
    Fetch per-scan classifier attributes delta page.
    """
    if not scan_id:
        raise RuntimeError("scan_id is required for scan attributes API.")
    base_path = INTERNAL_PARENT_SCAN_INSIGHTS_API_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{scan_id}/attributes")
    params = {
        "skip": int(skip),
        "limit": int(limit),
        "requireTotalCount": "true",
        "sort": json.dumps([{"field": "new", "order": "desc"}], separators=(",", ":")),
        "type": "classifier",
    }
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}

    resp = requests.get(url, params=params, headers=headers_raw, timeout=45)
    if resp.status_code in (401, 403) and token:
        resp = requests.get(url, params=params, headers=headers_bearer, timeout=45)

    if resp.status_code in (401, 403):
        if not token:
            raise RuntimeError(
                "Scan attributes API requires authentication token. "
                "Set BIGID_AUTH_TOKEN or BIGID_AUTH_USERNAME/BIGID_AUTH_PASSWORD."
            )
        raise RuntimeError("Authentication token is invalid or expired for scan attributes API.")

    resp.raise_for_status()
    payload = resp.json()
    return payload, getattr(resp, "url", url)


def fetch_parent_scan_attribute_counts_with_auth_retry(env_row, scan_id, limit=200):
    """
    Fetch attribute pages and return aggregate new/removed counts.
    """
    env_row = env_row or {}
    base_url = env_row.get("base_url")

    def collect_with_token(token):
        skip = 0
        all_items = []
        total_count = None
        sample_url = ""
        # Hard cap pages to keep UI responsive.
        for _ in range(20):
            payload, url = fetch_parent_scan_attributes_from_api(
                base_url, scan_id, token, skip=skip, limit=limit
            )
            sample_url = sample_url or url
            items, page_total = extract_scan_attributes_payload(payload)
            if total_count is None and page_total is not None:
                total_count = int(page_total)
            if items:
                all_items.extend(items)
            if not items:
                break
            skip += limit
            if total_count is not None and skip >= total_count:
                break
        synthetic_payload = {"data": {"attributes": all_items}}
        new_count, removed_count = extract_new_removed_counts_from_attributes(synthetic_payload)
        return new_count, removed_count, synthetic_payload, sample_url

    try:
        return collect_with_token("")
    except Exception as ex:
        msg = str(ex).lower()
        if "requires authentication token" in msg or "unauthorized" in msg:
            token = fetch_auth_token_for_env(env_row)
            return collect_with_token(token)
        if "invalid or expired" in msg and "auth_token_cache" in st.session_state:
            try:
                key = normalize_base_url(base_url)
                st.session_state.get("auth_token_cache", {}).pop(key, None)
            except Exception:
                pass
            fresh = fetch_auth_token_for_env(env_row)
            return collect_with_token(fresh)
        token = fetch_auth_token_for_env(env_row)
        return collect_with_token(token)


def _redact_secrets(obj):
    """
    Recursively redacts secret-like keys in dict/list payloads so we can safely display API responses.
    """
    secret_keys = {
        "authorization",
        "token",
        "access_token",
        "id_token",
        "password",
        "password_enc",
        "secret",
        "client_secret",
    }
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in secret_keys:
                out[k] = "***redacted***"
            else:
                out[k] = _redact_secrets(v)
        return out
    if isinstance(obj, list):
        return [_redact_secrets(x) for x in obj]
    return obj


def _local_llm_is_configured() -> bool:
    if LOCAL_LLM_PROVIDER in {"ollama", "openai"} and LOCAL_LLM_BASE_URL and LOCAL_LLM_MODEL:
        return True
    return False


def generate_local_llm_analysis(system_prompt: str, user_prompt: str, timeout_s: int = 45) -> str:
    """
    Best-effort local LLM call.
    Supports:
    - Ollama: POST {base}/api/chat
    - OpenAI-compatible: POST {base}/v1/chat/completions
    Returns markdown-ish text or "" on failure.
    Never raises.
    """
    try:
        provider = (LOCAL_LLM_PROVIDER or "").strip().lower()
        base = (LOCAL_LLM_BASE_URL or "").strip().rstrip("/")
        model = (LOCAL_LLM_MODEL or "").strip()
        if not provider or not base or not model:
            return ""

        if provider == "ollama":
            url = f"{base}/api/chat"
            payload = {
                "model": model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": str(system_prompt or "")},
                    {"role": "user", "content": str(user_prompt or "")},
                ],
            }
            resp = requests.post(url, json=payload, timeout=int(timeout_s))
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            msg = (data.get("message") or {}) if isinstance(data, dict) else {}
            return str(msg.get("content") or "").strip()

        if provider == "openai":
            url = f"{base}/v1/chat/completions"
            headers = {"Content-Type": "application/json"}
            if LOCAL_LLM_API_KEY:
                headers["Authorization"] = f"Bearer {LOCAL_LLM_API_KEY}"
            payload = {
                "model": model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": str(system_prompt or "")},
                    {"role": "user", "content": str(user_prompt or "")},
                ],
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=int(timeout_s))
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            choices = data.get("choices") if isinstance(data, dict) else None
            if isinstance(choices, list) and choices:
                msg = (choices[0] or {}).get("message") or {}
                return str(msg.get("content") or "").strip()
            return ""

        return ""
    except Exception:
        return ""


def extract_scanner_groups_payload(payload):
    """
    Returns (items, total_count) for scanner groups response.
    """
    items = []
    total_count = None
    if isinstance(payload, dict):
        data_block = payload.get("data")
        root = data_block if isinstance(data_block, dict) else payload
        for key in ["scannerGroups", "scanner_groups", "groups", "items", "results"]:
            val = root.get(key)
            if isinstance(val, list):
                items = [x for x in val if isinstance(x, dict)]
                break
        if root.get("totalCount") is not None:
            try:
                total_count = int(root.get("totalCount"))
            except Exception:
                total_count = None
    return items, total_count


def fetch_scanner_groups_from_api(base_url, token):
    url = build_api_url(base_url, INTERNAL_SCANNER_GROUPS_API_PATH)
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(url, headers=headers_raw, timeout=45)
    if resp.status_code in (401, 403) and token:
        resp = requests.get(url, headers=headers_bearer, timeout=45)
    if resp.status_code in (401, 403):
        if not token:
            raise RuntimeError(
                "Scanner groups API requires authentication token. "
                "Set BIGID_AUTH_TOKEN or BIGID_AUTH_USERNAME/BIGID_AUTH_PASSWORD."
            )
        raise RuntimeError("Authentication token is invalid or expired for scanner groups API.")
    resp.raise_for_status()
    payload = resp.json()
    items, total_count = extract_scanner_groups_payload(payload)
    return items, total_count, getattr(resp, "url", url)


def fetch_scanner_groups_with_auth_retry(env_row):
    env_row = env_row or {}
    base_url = env_row.get("base_url")
    try:
        return fetch_scanner_groups_from_api(base_url, "")
    except Exception as ex:
        msg = str(ex).lower()
        if "requires authentication token" in msg or "unauthorized" in msg:
            token = fetch_auth_token_for_env(env_row)
            return fetch_scanner_groups_from_api(base_url, token)
        if "invalid or expired" in msg and "auth_token_cache" in st.session_state:
            try:
                key = normalize_base_url(base_url)
                st.session_state.get("auth_token_cache", {}).pop(key, None)
            except Exception:
                pass
            fresh = fetch_auth_token_for_env(env_row)
            return fetch_scanner_groups_from_api(base_url, fresh)
        token = fetch_auth_token_for_env(env_row)
        return fetch_scanner_groups_from_api(base_url, token)


def extract_scan_templates_payload(payload):
    """
    Returns (items, total_count) for scan templates response.
    """
    items = []
    total_count = None
    if isinstance(payload, dict):
        data_block = payload.get("data")
        root = data_block if isinstance(data_block, dict) else payload
        for key in ["scanTemplates", "scan_templates", "templates", "items", "results"]:
            val = root.get(key)
            if isinstance(val, list):
                items = [x for x in val if isinstance(x, dict)]
                break
        if root.get("totalCount") is not None:
            try:
                total_count = int(root.get("totalCount"))
            except Exception:
                total_count = None
    return items, total_count


def fetch_scan_templates_from_api(base_url, token, *, limit: int = 200, skip: int = 0):
    url = build_api_url(base_url, INTERNAL_SCAN_TEMPLATES_API_PATH)
    params = {
        "skip": int(skip),
        "limit": int(limit),
        "requireTotalCount": "true",
        # Match UI request shape: no-op filter + updated_at desc, name asc
        "filter": json.dumps([{"field": "_id", "value": [], "operator": "notIn"}], separators=(",", ":")),
        "sort": json.dumps(
            [{"field": "updated_at", "order": "desc"}, {"field": "name", "order": "asc"}],
            separators=(",", ":"),
        ),
    }
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(url, params=params, headers=headers_raw, timeout=45)
    if resp.status_code in (401, 403) and token:
        resp = requests.get(url, params=params, headers=headers_bearer, timeout=45)
    if resp.status_code in (401, 403):
        if not token:
            raise RuntimeError(
                "Scan templates API requires authentication token. "
                "Set BIGID_AUTH_TOKEN or BIGID_AUTH_USERNAME/BIGID_AUTH_PASSWORD."
            )
        raise RuntimeError("Authentication token is invalid or expired for scan templates API.")
    resp.raise_for_status()
    payload = resp.json()
    items, total_count = extract_scan_templates_payload(payload)
    return items, total_count, getattr(resp, "url", url)


def fetch_scan_templates_with_auth_retry(env_row, *, limit: int = 200, skip: int = 0):
    env_row = env_row or {}
    base_url = env_row.get("base_url")
    try:
        return fetch_scan_templates_from_api(base_url, "", limit=limit, skip=skip)
    except Exception as ex:
        msg = str(ex).lower()
        if "requires authentication token" in msg or "unauthorized" in msg:
            token = fetch_auth_token_for_env(env_row)
            return fetch_scan_templates_from_api(base_url, token, limit=limit, skip=skip)
        if "invalid or expired" in msg and "auth_token_cache" in st.session_state:
            try:
                key = normalize_base_url(base_url)
                st.session_state.get("auth_token_cache", {}).pop(key, None)
            except Exception:
                pass
            fresh = fetch_auth_token_for_env(env_row)
            return fetch_scan_templates_from_api(base_url, fresh, limit=limit, skip=skip)
        token = fetch_auth_token_for_env(env_row)
        return fetch_scan_templates_from_api(base_url, token, limit=limit, skip=skip)


def _normalize_template_name(s: Any) -> str:
    return str(s or "").strip().lower()


def ensure_env_default_scan_template_id(env_row: Dict[str, Any]) -> str:
    """
    Automatic: if config::scan_template_id::env=<id> is missing, fetch /scan-templates and save
    the template id for "Data source scan".
    """
    try:
        env_id = int(env_row.get("id")) if env_row and env_row.get("id") is not None else None
    except Exception:
        env_id = None
    if env_id is None:
        return ""
    key_env = f"config::scan_template_id::env={int(env_id)}"
    try:
        existing = str(get_workflow_template_payload(key_env) or "").strip()
        if existing:
            return existing
    except Exception:
        existing = ""

    # Avoid repeated API calls in one session
    cache_key = f"_auto_scan_template_done::{int(env_id)}"
    try:
        if st.session_state.get(cache_key):
            return ""
    except Exception:
        pass

    try:
        items, _total, _url = fetch_scan_templates_with_auth_retry(env_row, limit=200, skip=0)
        for it in items or []:
            if not isinstance(it, dict):
                continue
            nm = _normalize_template_name(it.get("name"))
            if nm in {"data source scan", "data source scan "}:
                tid = str(it.get("_id") or it.get("id") or "").strip()
                if tid:
                    upsert_workflow_template(key_env, "config", tid)
                    try:
                        st.session_state[cache_key] = True
                    except Exception:
                        pass
                    return tid
        try:
            st.session_state[cache_key] = True
        except Exception:
            pass
        return ""
    except Exception:
        try:
            st.session_state[cache_key] = True
        except Exception:
            pass
        return ""


def put_ds_connection_from_api(base_url, ds_name, token, payload):
    from urllib.parse import quote

    name = str(ds_name or "").strip()
    if not name:
        raise RuntimeError("ds_name is required")
    base_path = INTERNAL_DS_CONNECTIONS_API_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{quote(name, safe='')}")
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.put(url, headers=headers_raw, json=payload, timeout=60)
    if resp.status_code in (401, 403) and token:
        resp = requests.put(url, headers=headers_bearer, json=payload, timeout=60)
    if resp.status_code in (401, 403):
        if not token:
            raise RuntimeError(
                "DS connections API requires authentication token. "
                "Set BIGID_AUTH_TOKEN or BIGID_AUTH_USERNAME/BIGID_AUTH_PASSWORD."
            )
        raise RuntimeError("Authentication token is invalid or expired for DS connections API.")
    resp.raise_for_status()
    out = resp.json()
    return out, getattr(resp, "url", url)


def put_ds_connection_with_auth_retry(env_row, ds_name, payload):
    env_row = env_row or {}
    base_url = env_row.get("base_url")
    try:
        return put_ds_connection_from_api(base_url, ds_name, "", payload)
    except Exception as ex:
        msg = str(ex).lower()
        if "requires authentication token" in msg or "unauthorized" in msg:
            token = fetch_auth_token_for_env(env_row)
            return put_ds_connection_from_api(base_url, ds_name, token, payload)
        if "invalid or expired" in msg and "auth_token_cache" in st.session_state:
            try:
                key = normalize_base_url(base_url)
                st.session_state.get("auth_token_cache", {}).pop(key, None)
            except Exception:
                pass
            fresh = fetch_auth_token_for_env(env_row)
            return put_ds_connection_from_api(base_url, ds_name, fresh, payload)
        token = fetch_auth_token_for_env(env_row)
        return put_ds_connection_from_api(base_url, ds_name, token, payload)


def post_ds_connection_test_from_api(base_url, broadcast_event, token, payload):
    url = build_api_url(base_url, INTERNAL_DS_CONNECTION_TEST_API_PATH)
    params = {"broadcastEvent": str(broadcast_event or "").strip()} if broadcast_event else {}
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.post(url, params=params, headers=headers_raw, json=payload, timeout=120)
    if resp.status_code in (401, 403) and token:
        resp = requests.post(url, params=params, headers=headers_bearer, json=payload, timeout=120)
    if resp.status_code in (401, 403):
        if not token:
            raise RuntimeError(
                "DS connection test API requires authentication token. "
                "Set BIGID_AUTH_TOKEN or BIGID_AUTH_USERNAME/BIGID_AUTH_PASSWORD."
            )
        raise RuntimeError("Authentication token is invalid or expired for DS connection test API.")
    resp.raise_for_status()
    out = resp.json()
    return out, getattr(resp, "url", url)


def post_ds_connection_test_with_auth_retry(env_row, broadcast_event, payload):
    env_row = env_row or {}
    base_url = env_row.get("base_url")
    try:
        return post_ds_connection_test_from_api(base_url, broadcast_event, "", payload)
    except Exception as ex:
        msg = str(ex).lower()
        if "requires authentication token" in msg or "unauthorized" in msg:
            token = fetch_auth_token_for_env(env_row)
            return post_ds_connection_test_from_api(base_url, broadcast_event, token, payload)
        if "invalid or expired" in msg and "auth_token_cache" in st.session_state:
            try:
                key = normalize_base_url(base_url)
                st.session_state.get("auth_token_cache", {}).pop(key, None)
            except Exception:
                pass
            fresh = fetch_auth_token_for_env(env_row)
            return post_ds_connection_test_from_api(base_url, broadcast_event, fresh, payload)
        token = fetch_auth_token_for_env(env_row)
        return post_ds_connection_test_from_api(base_url, broadcast_event, token, payload)


def resolve_scan_status_value(scan):
    if not isinstance(scan, dict):
        return ""
    # Prefer parent-scan top-level state/status; progress.state is sub-progress and can read "Completed" early.
    top = str(scan.get("state") or scan.get("status") or "").strip()
    if top:
        return top
    progress = scan.get("progress")
    if isinstance(progress, dict):
        return str(progress.get("state") or "").strip()
    return ""


def parse_iso_datetime(dt_value):
    s = str(dt_value or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def format_scan_duration(started_at, completed_at, is_running=False):
    start_dt = parse_iso_datetime(started_at)
    if not start_dt:
        return ""
    end_dt = parse_iso_datetime(completed_at)
    if not end_dt:
        if is_running:
            end_dt = datetime.now(timezone.utc)
        else:
            return ""
    
    # Ensure both are offset-aware for comparison
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)

    seconds = int((end_dt - start_dt).total_seconds())
    if seconds < 0:
        return "00:00:00"
    hh = seconds // 3600
    mm = (seconds % 3600) // 60
    ss = seconds % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def extract_scan_insight_metrics(payload):
    """
    Extract requested fields from scan insight payload.
    """
    def deep_find(obj, target_keys):
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
    # BigID parent-scan payloads nest metrics under data.scanInsights (see tracker.upsert_scan_progress_record).
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

    classifier_attribute_names = deep_find(root, ["classifierAttributeNames", "classifier_attribute_names"])
    if classifier_attribute_names is None:
        classifier_attribute_names = classifications.get("classifierAttributeNames")

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
    data_source_names = []
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


def fetch_core_version_from_api(base_url, token, about_api_path=None):
    path = about_api_path or INTERNAL_ABOUT_API_PATH
    url = build_api_url(base_url, path)
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}

    resp = requests.get(url, headers=headers_raw, timeout=30)
    if resp.status_code in (401, 403) and token:
        resp = requests.get(url, headers=headers_bearer, timeout=30)
    resp.raise_for_status()

    payload = resp.json()
    raw_version = ""
    if isinstance(payload, dict):
        # Support multiple response shapes:
        # 1) {"releaseVersion": "..."}
        # 2) {"data": {"releaseVersion": "..."}}
        # 3) {"data": {"about": {"releaseVersion": "..."}}}  <-- current BigID shape
        raw_version = str(payload.get("releaseVersion") or "").strip()
        if not raw_version:
            data_block = payload.get("data")
            if isinstance(data_block, dict):
                raw_version = str(data_block.get("releaseVersion") or "").strip()
                if not raw_version:
                    about_block = data_block.get("about")
                    if isinstance(about_block, dict):
                        raw_version = str(about_block.get("releaseVersion") or "").strip()

    clean_version = raw_version
    if clean_version.lower().startswith("release-"):
        clean_version = clean_version[len("release-"):].strip()

    return clean_version, raw_version, url


def record_environment_version(environment_id, env_name, new_version, classifier_set_id=None):
    """
    Save environment version history only when changed from latest value.
    Also updates current version in test_environments table.
    Returns: (changed, previous_version)
    """
    if not engine or not environment_id:
        return False, None

    previous = None
    previous_set_id = None
    with engine.connect() as conn:
        last = conn.execute(
            text("""
                SELECT version_value, classifier_set_id
                FROM test_environment_version_history
                WHERE environment_id = :eid
                ORDER BY changed_at DESC, id DESC
                LIMIT 1
            """),
            {"eid": int(environment_id)}
        ).fetchone()
        if last:
            if last[0]:
                previous = str(last[0])
            previous_set_id = int(last[1]) if last[1] is not None else None

        conn.execute(
            text("UPDATE test_environments SET version = :v WHERE id = :eid"),
            {"v": new_version or None, "eid": int(environment_id)}
        )

        new_set_id = int(classifier_set_id) if classifier_set_id is not None else None
        version_changed = (previous != (new_version or None))
        set_changed = (previous_set_id != new_set_id)
        changed = version_changed or set_changed
        if changed:
            if version_changed:
                note = f"{previous or 'None'} -> {new_version or 'None'}"
            elif set_changed:
                note = f"Classifier set updated (version {new_version or 'None'})"
            else:
                note = "No change"
            conn.execute(
                text("""
                    INSERT INTO test_environment_version_history (environment_id, version_value, classifier_set_id, change_note)
                    VALUES (:eid, :vv, :sid, :note)
                """),
                {"eid": int(environment_id), "vv": new_version or None, "sid": new_set_id, "note": note[:255]}
            )
        conn.commit()
    return changed, previous


def load_environment_version_history(environment_id):
    if not engine or not environment_id:
        return pd.DataFrame()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT changed_at, version_value, change_note
                    FROM test_environment_version_history
                    WHERE environment_id = :eid
                    ORDER BY changed_at DESC, id DESC
                """),
                {"eid": int(environment_id)}
            ).fetchall()
        return pd.DataFrame(rows, columns=["Changed At", "Version", "Change"])
    except Exception:
        return pd.DataFrame()


def load_all_environment_version_history():
    if not engine:
        return pd.DataFrame()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT
                        te.id AS environment_id,
                        te.env_name,
                        h.changed_at,
                        h.version_value,
                        h.change_note,
                        COALESCE(
                            cs.classifier_count,
                            (
                                SELECT COUNT(DISTINCT bcm.classifier_id)
                                FROM build_classifier_mappings bcm
                                JOIN builds b ON b.id = bcm.build_id
                                WHERE b.version = h.version_value
                            ),
                            0
                        ) AS classifier_count,
                        (
                            SELECT COALESCE(
                                (
                                    SELECT GROUP_CONCAT(DISTINCT c.name ORDER BY c.name SEPARATOR ', ')
                                    FROM classifier_set_members csm
                                    JOIN classifiers c ON c.id = csm.classifier_id
                                    WHERE csm.set_id = h.classifier_set_id
                                ),
                                (
                                    SELECT GROUP_CONCAT(DISTINCT c2.name ORDER BY c2.name SEPARATOR ', ')
                                    FROM build_classifier_mappings bcm2
                                    JOIN builds b2 ON b2.id = bcm2.build_id
                                    JOIN classifiers c2 ON c2.id = bcm2.classifier_id
                                    WHERE b2.version = h.version_value
                                )
                            )
                        ) AS classifier_list
                    FROM test_environment_version_history h
                    JOIN test_environments te ON te.id = h.environment_id
                    LEFT JOIN classifier_sets cs ON cs.id = h.classifier_set_id
                    ORDER BY h.changed_at DESC, h.id DESC
                """)
            ).fetchall()
        return pd.DataFrame(
            rows,
            columns=[
                "environment_id",
                "Environment",
                "Changed At",
                "Version",
                "Change",
                "Classifier Count",
                "Classifiers"
            ]
        )
    except Exception:
        return pd.DataFrame()


def get_environment_current_version(environment_id):
    if not engine or environment_id is None:
        return ""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT version FROM test_environments WHERE id = :eid LIMIT 1"),
                {"eid": int(environment_id)}
            ).fetchone()
        return str(row[0] or "").strip() if row else ""
    except Exception:
        return ""


def load_build_version_classifier_stats():
    """
    Build-wise classifier counts per environment, based on normalized mappings.
    """
    if not engine:
        return pd.DataFrame()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    te.id AS environment_id,
                    te.env_name,
                    b.version AS build_version,
                    COUNT(DISTINCT bcm.classifier_id) AS classifier_count,
                    MAX(bcm.last_synced_at) AS last_synced_at
                FROM build_classifier_mappings bcm
                JOIN builds b ON b.id = bcm.build_id
                JOIN test_environments te ON te.id = bcm.environment_id
                GROUP BY te.id, te.env_name, b.version
                ORDER BY te.env_name, MAX(bcm.last_synced_at), b.version
            """)).fetchall()
        return pd.DataFrame(
            rows,
            columns=["environment_id", "env_name", "build_version", "classifier_count", "last_synced_at"]
        )
    except Exception:
        return pd.DataFrame()


def load_environment_build_change_paths():
    """
    Returns environment_id -> build version path string.
    Example: None -> 244.49 -> 245.4
    """
    if not engine:
        return {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT environment_id, version_value, changed_at, id
                FROM test_environment_version_history
                ORDER BY environment_id, changed_at, id
            """)).fetchall()
    except Exception:
        return {}

    by_env = {}
    for env_id, ver, _ts, _id in rows:
        if env_id not in by_env:
            by_env[env_id] = []
        v = str(ver or "").strip()
        if not v:
            continue
        if not by_env[env_id] or by_env[env_id][-1] != v:
            by_env[env_id].append(v)

    paths = {}
    for env_id, versions in by_env.items():
        if versions:
            paths[env_id] = "None -> " + " -> ".join(versions)
        else:
            paths[env_id] = "None"
    return paths


def load_current_environment_classifier_status():
    """
    Current (non-history) classifier status per environment at its current version.
    """
    if not engine:
        return pd.DataFrame()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    te.id AS environment_id,
                    te.env_name,
                    te.base_url,
                    te.version,
                    te.is_active,
                    COUNT(DISTINCT bcm.classifier_id) AS classifier_count,
                    GROUP_CONCAT(DISTINCT c.name ORDER BY c.name SEPARATOR ', ') AS classifier_list
                FROM test_environments te
                LEFT JOIN builds b
                    ON b.version = te.version
                LEFT JOIN build_classifier_mappings bcm
                    ON bcm.build_id = b.id
                LEFT JOIN classifiers c
                    ON c.id = bcm.classifier_id
                GROUP BY te.id, te.env_name, te.base_url, te.version, te.is_active
                ORDER BY te.env_name
            """)).fetchall()
        return pd.DataFrame(
            rows,
            columns=[
                "environment_id",
                "env_name",
                "base_url",
                "version",
                "is_active",
                "classifier_count",
                "classifier_list"
            ]
        )
    except Exception:
        return pd.DataFrame()


def get_classifier_names_for_set(set_id):
    if not engine or set_id is None:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT c.name
                FROM classifier_set_members csm
                JOIN classifiers c ON c.id = csm.classifier_id
                WHERE csm.set_id = :sid
                ORDER BY c.name
            """), {"sid": int(set_id)}).fetchall()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def get_classifier_names_for_env_version(environment_id, version_value):
    if not engine or environment_id is None or not version_value:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT c.name
                FROM build_classifier_mappings bcm
                JOIN builds b ON b.id = bcm.build_id
                JOIN classifiers c ON c.id = bcm.classifier_id
                WHERE bcm.environment_id = :eid
                  AND b.version = :ver
                ORDER BY c.name
            """), {"eid": int(environment_id), "ver": str(version_value)}).fetchall()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def save_classifiers_to_db(classifier_names):
    if not engine:
        return 0
    cleaned = []
    seen = set()
    for name in classifier_names or []:
        clean = str(name).strip()
        if not clean or re.match(r"^\d+\s+Classifiers\s+from\s+Global\s+list$", clean, flags=re.IGNORECASE):
            continue
        k = clean.lower()
        if k in seen:
            continue
        seen.add(k)
        cleaned.append(clean)
    if not cleaned:
        return 0

    def _in_params(values, prefix="v"):
        vals = list(values or [])
        params = {f"{prefix}{i}": v for i, v in enumerate(vals)}
        clause = "(" + ",".join([f":{prefix}{i}" for i in range(len(vals))]) + ")"
        return clause, params

    dialect = getattr(getattr(engine, "dialect", None), "name", "") or ""
    insert_ignore = "INSERT OR IGNORE" if dialect.lower() == "sqlite" else "INSERT IGNORE"

    with engine.connect() as conn:
        clause, params = _in_params(cleaned, prefix="n")
        existing = conn.execute(
            text(f"SELECT name FROM classifiers WHERE name IN {clause}"),
            params,
        ).fetchall()
        existing_names = {str(r[0]) for r in existing if r and r[0]}
        missing = [n for n in cleaned if n not in existing_names]
        if missing:
            conn.execute(
                text(f"{insert_ignore} INTO classifiers (name) VALUES (:n)"),
                [{"n": n} for n in missing],
            )
        conn.commit()
    return len(missing)


def create_or_get_classifier_set(classifier_names):
    """
    Normalized classifier set storage:
    - classifier_sets: unique set hash
    - classifier_set_members: members for each set
    Returns (set_id, classifier_count, new_classifiers_added)
    """
    if not engine:
        return None, 0, 0

    cleaned = []
    seen = set()
    for n in classifier_names or []:
        c = str(n or "").strip()
        if not c or re.match(r"^\d+\s+Classifiers\s+from\s+Global\s+list$", c, flags=re.IGNORECASE):
            continue
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(c)

    if not cleaned:
        return None, 0, 0

    def _in_params(values, prefix="v"):
        vals = list(values or [])
        params = {f"{prefix}{i}": v for i, v in enumerate(vals)}
        clause = "(" + ",".join([f":{prefix}{i}" for i in range(len(vals))]) + ")"
        return clause, params

    dialect = getattr(getattr(engine, "dialect", None), "name", "") or ""
    insert_ignore = "INSERT OR IGNORE" if dialect.lower() == "sqlite" else "INSERT IGNORE"

    new_classifiers = 0
    with engine.connect() as conn:
        ordered = sorted(cleaned, key=lambda x: x.lower())
        clause, params = _in_params(ordered, prefix="n")
        rows = conn.execute(
            text(f"SELECT id, name FROM classifiers WHERE name IN {clause}"),
            params,
        ).fetchall()
        name_to_id = {str(r[1]): int(r[0]) for r in rows if r and r[0] and r[1]}
        missing = [n for n in ordered if n not in name_to_id]
        if missing:
            # Bulk insert missing classifiers then re-select ids once.
            conn.execute(
                text(f"{insert_ignore} INTO classifiers (name) VALUES (:n)"),
                [{"n": n} for n in missing],
            )
            new_classifiers = len(missing)
            rows2 = conn.execute(
                text(f"SELECT id, name FROM classifiers WHERE name IN {clause}"),
                params,
            ).fetchall()
            name_to_id = {str(r[1]): int(r[0]) for r in rows2 if r and r[0] and r[1]}

        classifier_ids = sorted({int(name_to_id[n]) for n in name_to_id})
        set_hash = hashlib.md5(",".join(str(i) for i in classifier_ids).encode()).hexdigest()

        set_row = conn.execute(
            text("SELECT id FROM classifier_sets WHERE set_hash = :h"),
            {"h": set_hash},
        ).fetchone()
        if set_row:
            set_id = int(set_row[0])
        else:
            conn.execute(
                text(f"{insert_ignore} INTO classifier_sets (set_hash, classifier_count) VALUES (:h, :c)"),
                {"h": set_hash, "c": len(classifier_ids)},
            )
            set_id = int(conn.execute(
                text("SELECT id FROM classifier_sets WHERE set_hash = :h"),
                {"h": set_hash},
            ).fetchone()[0])

        # Bulk insert members (ignore duplicates).
        conn.execute(
            text(f"{insert_ignore} INTO classifier_set_members (set_id, classifier_id) VALUES (:s, :c)"),
            [{"s": set_id, "c": int(cid)} for cid in classifier_ids],
        )
        conn.execute(
            text("UPDATE classifier_sets SET classifier_count = :c WHERE id = :s"),
            {"c": len(classifier_ids), "s": set_id},
        )
        conn.commit()
    return set_id, len(cleaned), new_classifiers


def sync_build_classifier_mappings(build_version, classifier_names, environment_id=None):
    """
    Normalize classifier storage:
    - classifiers table keeps unique classifier names
    - build_classifier_mappings keeps unique (build, classifier) links
    """
    if not engine:
        return {"new_classifiers": 0, "new_links": 0, "total_links_for_build": 0, "build_id": None}

    build_version = str(build_version or "").strip()
    if not build_version:
        return {"new_classifiers": 0, "new_links": 0, "total_links_for_build": 0, "build_id": None}

    cleaned = []
    seen = set()
    for n in classifier_names or []:
        c = str(n).strip()
        if not c or re.match(r"^\d+\s+Classifiers\s+from\s+Global\s+list$", c, flags=re.IGNORECASE):
            continue
        k = c.lower()
        if k in seen:
            continue
        seen.add(k)
        cleaned.append(c)

    if not cleaned:
        return {"new_classifiers": 0, "new_links": 0, "total_links_for_build": 0, "build_id": None}

    def _in_params(values, prefix="v"):
        vals = list(values or [])
        params = {f"{prefix}{i}": v for i, v in enumerate(vals)}
        clause = "(" + ",".join([f":{prefix}{i}" for i in range(len(vals))]) + ")"
        return clause, params

    dialect = getattr(getattr(engine, "dialect", None), "name", "") or ""
    insert_ignore = "INSERT OR IGNORE" if dialect.lower() == "sqlite" else "INSERT IGNORE"

    new_classifiers = 0
    new_links = 0
    with engine.connect() as conn:
        build_id = get_or_create_id(conn, "builds", "version", build_version)
        env_id = int(environment_id) if environment_id is not None else None

        # 1) Ensure all classifiers exist (bulk), then fetch ids (single select).
        clause, params = _in_params(cleaned, prefix="n")
        rows = conn.execute(
            text(f"SELECT id, name FROM classifiers WHERE name IN {clause}"),
            params,
        ).fetchall()
        name_to_id = {str(r[1]): int(r[0]) for r in rows if r and r[0] and r[1]}
        missing = [n for n in cleaned if n not in name_to_id]
        if missing:
            conn.execute(
                text(f"{insert_ignore} INTO classifiers (name) VALUES (:n)"),
                [{"n": n} for n in missing],
            )
            new_classifiers = len(missing)
            rows2 = conn.execute(
                text(f"SELECT id, name FROM classifiers WHERE name IN {clause}"),
                params,
            ).fetchall()
            name_to_id = {str(r[1]): int(r[0]) for r in rows2 if r and r[0] and r[1]}

        classifier_ids = sorted({int(name_to_id[n]) for n in name_to_id})
        if not classifier_ids:
            return {"new_classifiers": new_classifiers, "new_links": 0, "total_links_for_build": 0, "build_id": build_id}

        # 2) Find existing links (single select) and bulk insert missing links.
        cid_clause, cid_params = _in_params(classifier_ids, prefix="c")
        link_params = {"b": int(build_id), **cid_params}
        if env_id is None:
            existing_links = conn.execute(
                text(f"""
                    SELECT DISTINCT classifier_id
                    FROM build_classifier_mappings
                    WHERE build_id = :b
                      AND environment_id IS NULL
                      AND classifier_id IN {cid_clause}
                """),
                link_params,
            ).fetchall()
        else:
            link_params["e"] = int(env_id)
            existing_links = conn.execute(
                text(f"""
                    SELECT DISTINCT classifier_id
                    FROM build_classifier_mappings
                    WHERE build_id = :b
                      AND environment_id = :e
                      AND classifier_id IN {cid_clause}
                """),
                link_params,
            ).fetchall()
        existing_ids = {int(r[0]) for r in existing_links if r and r[0] is not None}
        missing_ids = [cid for cid in classifier_ids if cid not in existing_ids]
        if missing_ids:
            conn.execute(
                text(f"{insert_ignore} INTO build_classifier_mappings (build_id, classifier_id, environment_id) VALUES (:b, :c, :e)"),
                [{"b": int(build_id), "c": int(cid), "e": (int(env_id) if env_id is not None else None)} for cid in missing_ids],
            )
            new_links = len(missing_ids)

        # 3) Touch last_synced_at once for this build+env.
        if env_id is None:
            conn.execute(
                text("""
                    UPDATE build_classifier_mappings
                    SET last_synced_at = CURRENT_TIMESTAMP
                    WHERE build_id = :b AND environment_id IS NULL
                """),
                {"b": int(build_id)},
            )
            total_links = int(conn.execute(
                text("SELECT COUNT(*) FROM build_classifier_mappings WHERE build_id = :b AND environment_id IS NULL"),
                {"b": int(build_id)},
            ).fetchone()[0])
        else:
            conn.execute(
                text("""
                    UPDATE build_classifier_mappings
                    SET last_synced_at = CURRENT_TIMESTAMP
                    WHERE build_id = :b AND environment_id = :e
                """),
                {"b": int(build_id), "e": int(env_id)},
            )
            total_links = int(conn.execute(
                text("SELECT COUNT(*) FROM build_classifier_mappings WHERE build_id = :b AND environment_id = :e"),
                {"b": int(build_id), "e": int(env_id)},
            ).fetchone()[0])

        conn.commit()

    return {
        "new_classifiers": new_classifiers,
        "new_links": new_links,
        "total_links_for_build": total_links,
        "build_id": build_id
    }


def clone_build_mappings_to_environment(environment_id, build_version):
    """
    For same build version across different environments, clone existing build mappings
    into this environment without calling classifier API.
    """
    if not engine or environment_id is None or not build_version:
        return 0
    dialect = getattr(getattr(engine, "dialect", None), "name", "") or ""
    insert_ignore = "INSERT OR IGNORE" if dialect.lower() == "sqlite" else "INSERT IGNORE"
    with engine.connect() as conn:
        b_row = conn.execute(text("SELECT id FROM builds WHERE version = :v"), {"v": str(build_version)}).fetchone()
        if not b_row:
            return 0
        build_id = int(b_row[0])

        # One-shot clone: insert any missing (build_id, classifier_id, environment_id) rows.
        result = conn.execute(
            text(f"""
                {insert_ignore} INTO build_classifier_mappings (build_id, classifier_id, environment_id)
                SELECT :b, bcm.classifier_id, :e
                FROM build_classifier_mappings bcm
                WHERE bcm.build_id = :b
                  AND bcm.classifier_id IS NOT NULL
                GROUP BY bcm.classifier_id
            """),
            {"b": int(build_id), "e": int(environment_id)},
        )
        try:
            inserted = int(getattr(result, "rowcount", 0) or 0)
        except Exception:
            inserted = 0

        # Touch last_synced_at once.
        conn.execute(
            text("""
                UPDATE build_classifier_mappings
                SET last_synced_at = CURRENT_TIMESTAMP
                WHERE build_id = :b AND environment_id = :e
            """),
            {"b": int(build_id), "e": int(environment_id)},
        )
        conn.commit()
    return inserted


def ensure_scan_tracking_tables():
    if not engine:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scan_parents (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    scan_id VARCHAR(128) NOT NULL UNIQUE,
                    environment_id INT NULL,
                    scan_name VARCHAR(1000) NULL,
                    scan_type VARCHAR(120) NULL,
                    started_at VARCHAR(80) NULL,
                    completed_at VARCHAR(80) NULL,
                    template_name VARCHAR(255) NULL,
                    scan_status VARCHAR(120) NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (environment_id) REFERENCES test_environments(id)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scan_insights (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    scan_id VARCHAR(128) NOT NULL UNIQUE,
                    distinct_classification_attributes BIGINT NULL,
                    classifier_attribute_names LONGTEXT NULL,
                    data_source_names LONGTEXT NULL,
                    objects_with_pi BIGINT NULL,
                    objects_scanned BIGINT NULL,
                    objects_enumerated BIGINT NULL,
                    total_classification BIGINT NULL,
                    new_attributes_count BIGINT NULL,
                    removed_attributes_count BIGINT NULL,
                    insight_payload LONGTEXT NULL,
                    attributes_payload LONGTEXT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
            """))
            try:
                conn.execute(text("ALTER TABLE scan_insights ADD COLUMN total_size_gb FLOAT NULL"))
                conn.commit()
            except Exception:
                pass
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scan_data_sources (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(500) NOT NULL UNIQUE
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scan_insight_data_source_map (
                    scan_id VARCHAR(128) NOT NULL,
                    data_source_id INT NOT NULL,
                    PRIMARY KEY (scan_id, data_source_id),
                    FOREIGN KEY (scan_id) REFERENCES scan_parents(scan_id),
                    FOREIGN KEY (data_source_id) REFERENCES scan_data_sources(id)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scan_classifier_attributes (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(500) NOT NULL UNIQUE
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scan_insight_attribute_delta_map (
                    scan_id VARCHAR(128) NOT NULL,
                    attribute_id INT NOT NULL,
                    is_new BOOLEAN DEFAULT FALSE,
                    is_removed BOOLEAN DEFAULT FALSE,
                    finding_count BIGINT NULL,
                    attribute_value VARCHAR(500) NULL,
                    new_count_value BIGINT NULL,
                    removed_count_value BIGINT NULL,
                    PRIMARY KEY (scan_id, attribute_id),
                    FOREIGN KEY (scan_id) REFERENCES scan_parents(scan_id),
                    FOREIGN KEY (attribute_id) REFERENCES scan_classifier_attributes(id)
                )
            """))
            try:
                conn.execute(text("ALTER TABLE scan_insight_attribute_delta_map ADD COLUMN finding_count BIGINT NULL"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE scan_insight_attribute_delta_map ADD COLUMN attribute_value VARCHAR(500) NULL"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE scan_insight_attribute_delta_map ADD COLUMN new_count_value BIGINT NULL"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE scan_insight_attribute_delta_map ADD COLUMN removed_count_value BIGINT NULL"))
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass


def ensure_benchmark_findings_extended_columns():
    if not engine:
        return
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE benchmark_findings ADD COLUMN classifier_attribute VARCHAR(500) NULL"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE benchmark_findings ADD COLUMN attribute_value VARCHAR(500) NULL"))
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass


def ensure_auto_scan_benchmarks_table():
    if not engine:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS auto_scan_benchmarks (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    scan_id VARCHAR(128) NOT NULL UNIQUE,
                    environment_id INT NULL,
                    build_version VARCHAR(100) NULL,
                    run_date DATE NULL,
                    scenario_type VARCHAR(100) NULL,
                    scan_type VARCHAR(100) NULL,
                    data_source_type VARCHAR(100) NULL,
                    data_type VARCHAR(50) NULL,
                    file_set_type VARCHAR(150) NULL,
                    scan_start_time DATETIME NULL,
                    scan_end_time DATETIME NULL,
                    total_scan_hours FLOAT NULL,
                    objects_with_pi BIGINT NULL,
                    objects_scanned BIGINT NULL,
                    objects_enumerated BIGINT NULL,
                    total_classification BIGINT NULL,
                    new_attributes BIGINT NULL,
                    removed_attributes BIGINT NULL,
                    classifier_attribute_names LONGTEXT NULL,
                    scan_name VARCHAR(1000) NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (environment_id) REFERENCES test_environments(id)
                )
            """))
            try:
                conn.execute(text("ALTER TABLE auto_scan_benchmarks ADD COLUMN total_size_gb FLOAT NULL"))
                conn.execute(text("ALTER TABLE auto_scan_benchmarks ADD COLUMN total_files BIGINT NULL"))
                conn.execute(text("ALTER TABLE auto_scan_benchmarks ADD COLUMN total_folders BIGINT NULL"))
                conn.execute(text("ALTER TABLE auto_scan_benchmarks ADD COLUMN throughput_cluster_gb_hr FLOAT NULL"))
                conn.execute(text("ALTER TABLE auto_scan_benchmarks ADD COLUMN throughput_cluster_files_hr FLOAT NULL"))
                conn.commit()
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass

def ensure_scan_progress_table():
    if not engine:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scan_progress (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    scan_id VARCHAR(128) NOT NULL,
                    scan_name VARCHAR(1000) NULL,
                    environment_id INT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    captured_at_utc DATETIME NULL,
                    scan_state VARCHAR(64) NULL,
                    completed_sub_scans BIGINT DEFAULT 0,
                    failed_sub_scans BIGINT DEFAULT 0,
                    total_sub_scans BIGINT DEFAULT 0,
                    completed_parts BIGINT DEFAULT 0,
                    failed_parts BIGINT DEFAULT 0,
                    total_parts_scanned BIGINT DEFAULT 0,
                    total_classifications BIGINT DEFAULT 0,
                    newly_added_classifications BIGINT DEFAULT 0,
                    false_removed_classifications BIGINT DEFAULT 0,
                    objects_scanned BIGINT DEFAULT 0,
                    objects_failed BIGINT DEFAULT 0,
                    objects_with_pi BIGINT DEFAULT 0,
                    INDEX idx_scan_id (scan_id),
                    INDEX idx_timestamp (timestamp),
                    INDEX idx_captured_at_utc (captured_at_utc),
                    INDEX idx_state (scan_state)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scanner_group_performance (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    environment_id INT NOT NULL,
                    captured_at_utc DATETIME NOT NULL,
                    group_name VARCHAR(255) NULL,
                    running_scanners INT DEFAULT 0,
                    running_threads INT DEFAULT 0,
                    max_threads INT DEFAULT 0,
                    scanner_threads INT DEFAULT 0,
                    physical_memory_mb BIGINT DEFAULT 0,
                    free_memory_mb BIGINT DEFAULT 0,
                    avg_scan_speed FLOAT DEFAULT 0,
                    update_date_bigid DATETIME NULL,
                    INDEX idx_env_time_group (environment_id, captured_at_utc)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scanner_individual_performance (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    environment_id INT NOT NULL,
                    captured_at_utc DATETIME NOT NULL,
                    scanner_id VARCHAR(255) NULL,
                    name VARCHAR(255) NULL,
                    status VARCHAR(64) NULL,
                    group_name VARCHAR(255) NULL,
                    running_threads INT DEFAULT 0,
                    total_num_of_threads INT DEFAULT 0,
                    system_load_average FLOAT DEFAULT 0,
                    physical_memory_mb BIGINT DEFAULT 0,
                    free_memory_mb BIGINT DEFAULT 0,
                    scanner_threads INT DEFAULT 0,
                    update_date_bigid DATETIME NULL,
                    INDEX idx_env_time_scanner (environment_id, captured_at_utc, scanner_id)
                )
            """))
            # Non-breaking schema upgrades for existing DBs
            try:
                conn.execute(text("ALTER TABLE scan_progress MODIFY COLUMN `timestamp` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE scan_progress ADD COLUMN scan_state VARCHAR(64) NULL"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE scan_progress ADD COLUMN captured_at_utc DATETIME NULL"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE scan_progress ADD COLUMN false_removed_classifications BIGINT DEFAULT 0"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE scan_progress ADD COLUMN objects_failed BIGINT DEFAULT 0"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE scan_progress ADD COLUMN objects_with_pi BIGINT DEFAULT 0"))
            except Exception:
                pass
            # Best-effort: de-dupe and enforce unique slot per (scan_id, captured_at_utc)
            try:
                conn.execute(text("""
                    DELETE sp1 FROM scan_progress sp1
                    JOIN scan_progress sp2
                      ON sp1.scan_id = sp2.scan_id
                     AND sp1.captured_at_utc IS NOT NULL
                     AND sp2.captured_at_utc IS NOT NULL
                     AND sp1.captured_at_utc = sp2.captured_at_utc
                     AND sp1.id < sp2.id
                """))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE scan_progress ADD UNIQUE KEY uniq_scan_slot (scan_id, captured_at_utc)"))
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass
    ensure_scanner_performance_tables(engine)


def ensure_scanner_pod_metrics_table():
    """Time-series metrics per scanner pod (CPU, memory, load). Ingest via INSERT or your collector."""
    if not engine:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scanner_pod_metrics (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    environment_id INT NULL,
                    captured_at_utc DATETIME NOT NULL,
                    scanner_id VARCHAR(128) NOT NULL,
                    scanner_name VARCHAR(255) NULL,
                    scanner_version VARCHAR(100) NULL,
                    bigid_version VARCHAR(100) NULL,
                    status VARCHAR(64) NULL,
                    load_avg DOUBLE NULL,
                    memory_pct DOUBLE NULL,
                    cpu_pct DOUBLE NULL,
                    network_mbps DOUBLE NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_spm_env_time (environment_id, captured_at_utc),
                    INDEX idx_spm_scanner (scanner_id, captured_at_utc),
                    INDEX idx_spm_bigid_ver (bigid_version)
                )
            """))
            conn.commit()
    except Exception:
        pass


def load_scanner_pod_metrics_df(environment_id: Optional[int] = None) -> pd.DataFrame:
    if not engine:
        return pd.DataFrame()
    ensure_scanner_pod_metrics_table()
    try:
        q = """
            SELECT environment_id, captured_at_utc, scanner_id, scanner_name, scanner_version,
                   bigid_version, status, load_avg, memory_pct, cpu_pct, network_mbps
            FROM scanner_pod_metrics
            WHERE 1=1
        """
        params: Dict[str, Any] = {}
        if environment_id is not None:
            q += " AND environment_id = :eid"
            params["eid"] = int(environment_id)
        q += " ORDER BY captured_at_utc ASC, scanner_id ASC"
        with engine.connect() as conn:
            df = pd.read_sql(text(q), conn, params=params)
        if df.empty:
            return df
        df["captured_at_utc"] = pd.to_datetime(df["captured_at_utc"], errors="coerce", utc=True)
        return df
    except Exception:
        return pd.DataFrame()


def _scanner_status_inactive(status_val: Any) -> bool:
    """BigID scanner row status — hide these from default charts when user only wants healthy pods."""
    s = str(status_val or "").strip().lower()
    if not s:
        return False
    if s in {"failed", "inactive", "error", "stopped", "dead", "offline", "unknown", "aborted", "cancelled", "canceled"}:
        return True
    if "fail" in s:
        return True
    return False


def _spm_normalize_version(v: Any) -> str:
    return str(v or "").strip()


def _spm_faded_line_color(base_c: str, alpha: float = 0.35) -> str:
    """Plotly palette may be #hex or rgb(...); fade inactive traces."""
    s = str(base_c or "").strip()
    try:
        if s.startswith("#") and len(s) >= 7:
            h = s.lstrip("#")[:6]
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return f"rgba({r},{g},{b},{alpha})"
        if s.startswith("rgb") and "(" in s:
            inner = s.split("(", 1)[1].split(")", 1)[0]
            parts = [p.strip() for p in inner.split(",")]
            if len(parts) >= 3:
                r, g, b = int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))
                return f"rgba({r},{g},{b},{alpha})"
    except Exception:
        pass
    return f"rgba(180,180,180,{alpha})"


def page_scanner_infrastructure():
    """
    Primary charts use scanner_* tables filled by tracker / Scan Progress Sync (BigID APIs).
    Legacy `scanner_pod_metrics` section at bottom — tracker does NOT write there by default.
    """
    st.title("🖥️ Scanner infrastructure")
    st.caption(
        "**BigID snapshots:** `scanner_individual_performance` + `scanner_group_performance` (same as **Scan Progress → Sync** / **tracker**). "
        "Left chart = **CPU %** (BigID UI scale, often **>100%**). Right = **memory %**. "
        "This is **not** the old `scanner_pod_metrics` load-only view unless you scroll to **Legacy**."
    )
    if not engine:
        st.error("Database not configured.")
        return
    ensure_scanner_performance_tables(engine)
    try:
        env_df = load_test_environments()
    except Exception:
        env_df = pd.DataFrame()
    if env_df.empty:
        st.warning("No test environments. Add one under Manage Test Environments.")
        return
    if "is_active" in env_df.columns:
        active = env_df[pd.to_numeric(env_df["is_active"], errors="coerce").fillna(0).astype(int) != 0]
    else:
        active = env_df
    labels = [f"{r.get('env_name', r.get('id'))} (id={r.get('id')})" for _, r in active.iterrows()]
    ids = [int(r["id"]) for _, r in active.iterrows()]
    c1, c2, c3 = st.columns([1.2, 1, 1])
    with c1:
        idx = st.selectbox("Environment", range(len(labels)), format_func=lambda i: labels[i] if labels else "")
    env_id = ids[idx] if ids else None
    with c2:
        days_back = st.slider("History (days)", 1, 30, 7, key="si_hist_days")
    with c3:
        show_failed_si = st.checkbox("Show failed/inactive pods", value=False, key="si_show_failed")
        show_all_si = st.checkbox("Show all pods (ignore running set)", value=False, key="si_show_all")

    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=int(days_back))

    df_perf_g = load_scanner_group_performance(env_id, start_dt, end_dt) if env_id is not None else pd.DataFrame()
    df_perf_i = load_scanner_individual_performance(env_id, start_dt, end_dt) if env_id is not None else pd.DataFrame()
    if not df_perf_g.empty:
        df_perf_g = df_perf_g.copy()
        df_perf_g["captured_at_utc"] = pd.to_datetime(df_perf_g["captured_at_utc"], errors="coerce")
    if not df_perf_i.empty:
        df_perf_i = df_perf_i.copy()
        df_perf_i["captured_at_utc"] = pd.to_datetime(df_perf_i["captured_at_utc"], errors="coerce")

    df_chart = df_perf_i
    eff_ids: set = set()
    eff_src = ""
    if not df_perf_i.empty:
        try:
            _ord = df_perf_i.sort_values("captured_at_utc")
            _last_st = _ord.groupby("display_name", dropna=False)["status"].last()
            _active_names = {
                str(nm) for nm, st in _last_st.items() if not _scanner_status_inactive(st)
            }
            if not _active_names:
                _active_names = set(df_perf_i["display_name"].astype(str).unique())
        except Exception:
            _active_names = set(df_perf_i["display_name"].astype(str).unique())
        if not show_failed_si:
            df_chart = df_perf_i[df_perf_i["display_name"].astype(str).isin(_active_names)].copy()
            if df_chart.empty:
                df_chart = df_perf_i.copy()
        else:
            df_chart = df_perf_i.copy()

        eff_ids, eff_src = spm_effective_running_scanner_id_set(df_perf_g, df_perf_i)
        if not show_all_si and eff_ids:
            _tg = df_chart[df_chart["scanner_id"].astype(str).isin(eff_ids)].copy()
            if not _tg.empty:
                df_chart = _tg

    si_layout = dict(
        template="plotly_white",
        hovermode="x unified",
        height=400,
        margin=dict(l=48, r=36, t=44, b=96),
        legend=dict(orientation="h", yanchor="top", y=-0.32, xanchor="left", x=0),
    )

    if df_perf_i.empty and df_perf_g.empty:
        st.warning(
            f"No rows in **`scanner_individual_performance` / `scanner_group_performance`** for env **#{env_id}** in the last **{days_back}** day(s). "
            "Open **Scan Progress** for a running scan and click **Sync now**, or run **tracker.py**."
        )
    else:
        if eff_ids:
            st.success(f"**{len(eff_ids)} pod(s)** in chart filter — **{eff_src}**. (Uncheck filters above to widen.)")
        elif not df_perf_i.empty:
            st.info("No **running-set** IDs found (sync **scanner-groups** to store `scannersIDs`, or enable **Show all pods**).")

        col_cpu, col_mem = st.columns(2)
        with col_cpu:
            st.subheader("CPU % per scanner")
            _cs = pd.to_numeric(df_chart.get("cpu_pct"), errors="coerce") if not df_chart.empty else pd.Series(dtype=float)
            if not df_chart.empty and _cs.notna().any():
                _mx = float(_cs.max() or 0)
                if _mx <= 0:
                    _top = 100.0
                elif _mx < 100:
                    _top = max(100.0, _mx * 1.25 + 10.0)
                else:
                    _top = max(700.0, _mx * 1.15 + 50.0)
                fig_c = px.line(
                    df_chart.dropna(subset=["cpu_pct"]),
                    x="captured_at_utc",
                    y="cpu_pct",
                    color="display_name",
                    markers=True,
                )
                fig_c.update_traces(mode="lines+markers")
                fig_c.update_layout(**si_layout, yaxis=dict(title="CPU % (BigID)", range=[0, _top]))
                spm_apply_plotly_ticks(fig_c, df_chart["captured_at_utc"])
                st.plotly_chart(fig_c, width="stretch")
                st.caption(
                    f"Y-axis to **~{_top:.0f}%** (series max **{_mx:.1f}%**). "
                    "**Not** Linux load average — that is a different number (see **Legacy** or **Scan Progress → expander**)."
                )
            else:
                st.info("No **cpu_pct** in DB for this window — capture from **services-status** (tracker / **Sync now**).")
        with col_mem:
            st.subheader("Memory usage % per scanner")
            if not df_chart.empty:
                fig_m = px.line(
                    df_chart,
                    x="captured_at_utc",
                    y="memory_usage_pct",
                    color="display_name",
                    markers=True,
                )
                fig_m.update_traces(mode="lines+markers")
                fig_m.update_layout(**si_layout, yaxis=dict(title="Memory %", range=[0, 100]))
                spm_apply_plotly_ticks(fig_m, df_chart["captured_at_utc"])
                st.plotly_chart(fig_m, width="stretch")
                st.caption("Host **memory %** from physical/free MB in **services-status** (stays 0–100%).")
            else:
                st.caption("—")

        with st.expander("Optional: **system load average** (raw API — not CPU %)"):
            if not df_chart.empty:
                fig_l = px.line(
                    df_chart,
                    x="captured_at_utc",
                    y="system_load_average",
                    color="display_name",
                    markers=True,
                )
                fig_l.update_traces(mode="lines+markers")
                fig_l.update_layout(**si_layout, yaxis=dict(title="Load average", rangemode="tozero"))
                spm_apply_plotly_ticks(fig_l, df_chart["captured_at_utc"])
                st.plotly_chart(fig_l, width="stretch")

    st.divider()
    st.subheader("Legacy: `scanner_pod_metrics`")
    st.caption(
        "Separate table — **not** populated by default tracker. Only appears if you INSERT rows yourself "
        "(load_avg / memory_pct / cpu_pct columns)."
    )
    ensure_scanner_pod_metrics_table()
    raw = load_scanner_pod_metrics_df(environment_id=env_id)
    if raw.empty:
        st.caption("No rows in `scanner_pod_metrics`.")
    else:
        st.dataframe(raw.sort_values(["captured_at_utc", "scanner_id"]).tail(500), width="stretch")


def ensure_workflow_tables():
    """
    DB-backed workflow to orchestrate DS + scan creation/runs.
    UI-first: this just stores the plan + state so a background runner can execute it.
    """
    if not engine:
        return
    try:
        dialect = _db_dialect_name()
    except Exception:
        dialect = ""
    try:
        with engine.connect() as conn:
            if "sqlite" in dialect:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS workflow_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NULL,
                        status TEXT NULL,
                        env1_id INTEGER NULL,
                        env2_id INTEGER NULL,
                        cooldown_minutes INTEGER DEFAULT 15,
                        next_eligible_at TEXT NULL,
                        last_error TEXT NULL,
                        started_at TEXT NULL,
                        finished_at TEXT NULL,
                        created_at TEXT DEFAULT (datetime('now')),
                        updated_at TEXT DEFAULT (datetime('now'))
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS workflow_steps (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        seq INTEGER NOT NULL,
                        environment_id INTEGER NULL,
                        ds_kind TEXT NULL,
                        ds_name TEXT NULL,
                        scan_kind TEXT NULL,
                        file_set_type TEXT NULL,
                        is_enabled INTEGER DEFAULT 1,
                        is_acl INTEGER DEFAULT 0,
                        is_ocr INTEGER DEFAULT 0,
                        is_structured_ff INTEGER DEFAULT 0,
                        is_ols INTEGER DEFAULT 0,
                        feature_flags TEXT NULL,
                        notes TEXT NULL,
                        ds_id TEXT NULL,
                        scan_profile_id TEXT NULL,
                        scan_profile_name TEXT NULL,
                        build_version TEXT NULL,
                        build_prefix TEXT NULL,
                        scan_id TEXT NULL,
                        status TEXT NULL,
                        last_error TEXT NULL,
                        started_at TEXT NULL,
                        finished_at TEXT NULL,
                        created_at TEXT DEFAULT (datetime('now')),
                        updated_at TEXT DEFAULT (datetime('now')),
                        UNIQUE(run_id, seq)
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS workflow_templates (
                        k TEXT PRIMARY KEY,
                        template_kind TEXT NULL,
                        payload_json TEXT NULL,
                        updated_at TEXT DEFAULT (datetime('now'))
                    )
                """))
                # Non-breaking upgrades for existing sqlite DBs
                for stmt in [
                    "ALTER TABLE workflow_steps ADD COLUMN is_enabled INTEGER DEFAULT 1",
                    "ALTER TABLE workflow_steps ADD COLUMN scan_profile_id TEXT NULL",
                    "ALTER TABLE workflow_steps ADD COLUMN scan_profile_name TEXT NULL",
                    "ALTER TABLE workflow_steps ADD COLUMN build_version TEXT NULL",
                    "ALTER TABLE workflow_steps ADD COLUMN build_prefix TEXT NULL",
                    "ALTER TABLE workflow_templates ADD COLUMN template_kind TEXT NULL",
                    "ALTER TABLE workflow_templates ADD COLUMN payload_json TEXT NULL",
                    "ALTER TABLE workflow_runs ADD COLUMN scan_template_id TEXT NULL",
                ]:
                    try:
                        conn.execute(text(stmt))
                    except Exception:
                        pass
                try:
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_workflow_steps_run_seq ON workflow_steps(run_id, seq)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_workflow_steps_status ON workflow_steps(status)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_workflow_templates_kind ON workflow_templates(template_kind)"))
                except Exception:
                    pass
                conn.commit()
                return

            # MySQL / others
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) NULL,
                    status VARCHAR(32) NULL,
                    env1_id INT NULL,
                    env2_id INT NULL,
                    cooldown_minutes INT DEFAULT 15,
                    next_eligible_at DATETIME NULL,
                    last_error TEXT NULL,
                    started_at DATETIME NULL,
                    finished_at DATETIME NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_workflow_runs_status (status)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    run_id INT NOT NULL,
                    seq INT NOT NULL,
                    environment_id INT NULL,
                    ds_kind VARCHAR(64) NULL,
                    ds_name VARCHAR(500) NULL,
                    scan_kind VARCHAR(64) NULL,
                    file_set_type VARCHAR(150) NULL,
                    is_enabled BOOLEAN DEFAULT TRUE,
                    is_acl BOOLEAN DEFAULT FALSE,
                    is_ocr BOOLEAN DEFAULT FALSE,
                    is_structured_ff BOOLEAN DEFAULT FALSE,
                    is_ols BOOLEAN DEFAULT FALSE,
                    feature_flags VARCHAR(500) NULL,
                    notes VARCHAR(1000) NULL,
                    ds_id VARCHAR(128) NULL,
                    scan_profile_id VARCHAR(128) NULL,
                    scan_profile_name VARCHAR(500) NULL,
                    build_version VARCHAR(50) NULL,
                    build_prefix VARCHAR(20) NULL,
                    scan_id VARCHAR(128) NULL,
                    status VARCHAR(32) NULL,
                    last_error TEXT NULL,
                    started_at DATETIME NULL,
                    finished_at DATETIME NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_workflow_step (run_id, seq),
                    INDEX idx_workflow_steps_run_seq (run_id, seq),
                    INDEX idx_workflow_steps_status (status)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS workflow_templates (
                    k VARCHAR(200) PRIMARY KEY,
                    template_kind VARCHAR(32) NULL,
                    payload_json LONGTEXT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_workflow_templates_kind (template_kind)
                )
            """))
            # Non-breaking upgrades for existing MySQL DBs
            for stmt in [
                "ALTER TABLE workflow_steps ADD COLUMN is_enabled BOOLEAN DEFAULT TRUE",
                "ALTER TABLE workflow_steps ADD COLUMN scan_profile_id VARCHAR(128) NULL",
                "ALTER TABLE workflow_steps ADD COLUMN scan_profile_name VARCHAR(500) NULL",
                "ALTER TABLE workflow_steps ADD COLUMN build_version VARCHAR(50) NULL",
                "ALTER TABLE workflow_steps ADD COLUMN build_prefix VARCHAR(20) NULL",
                "ALTER TABLE workflow_templates ADD COLUMN template_kind VARCHAR(32) NULL",
                "ALTER TABLE workflow_templates ADD COLUMN payload_json LONGTEXT NULL",
                "ALTER TABLE workflow_runs ADD COLUMN scan_template_id VARCHAR(128) NULL",
            ]:
                try:
                    conn.execute(text(stmt))
                    conn.commit()
                except Exception:
                    pass
            conn.commit()
    except Exception:
        pass


def _workflow_now_utc() -> datetime:
    return datetime.utcnow()


def create_workflow_run(
    name: str,
    env1_id: Optional[int],
    env2_id: Optional[int],
    steps: List[Dict[str, Any]],
    cooldown_minutes: int = 15,
) -> Optional[int]:
    if not engine:
        return None
    ensure_workflow_tables()
    nm = str(name or "").strip() or f"workflow {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    try:
        cd = int(cooldown_minutes)
    except Exception:
        cd = 15

    try:
        dialect = _db_dialect_name()
    except Exception:
        dialect = ""

    with engine.connect() as conn:
        if "sqlite" in dialect:
            conn.execute(
                text("""
                    INSERT INTO workflow_runs (name, status, env1_id, env2_id, cooldown_minutes, created_at, updated_at)
                    VALUES (:n, :s, :e1, :e2, :cd, datetime('now'), datetime('now'))
                """),
                {"n": nm, "s": "created", "e1": env1_id, "e2": env2_id, "cd": cd},
            )
            run_id = conn.execute(text("SELECT last_insert_rowid()")).fetchone()[0]
        else:
            conn.execute(
                text("""
                    INSERT INTO workflow_runs (name, status, env1_id, env2_id, cooldown_minutes)
                    VALUES (:n, :s, :e1, :e2, :cd)
                """),
                {"n": nm, "s": "created", "e1": env1_id, "e2": env2_id, "cd": cd},
            )
            run_id = conn.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]

        # Insert steps (seq starts at 1)
        seq_rows = []
        for i, stp in enumerate(steps or [], start=1):
            if not isinstance(stp, dict):
                continue
            enabled = bool(stp.get("is_enabled", True))
            seq_rows.append({
                "rid": int(run_id),
                "seq": int(i),
                "eid": None,  # Set by runner when step actually starts; no pre-allocation so UI shows env only for running steps
                "dk": str(stp.get("ds_kind") or "").strip() or None,
                "dn": str(stp.get("ds_name") or "").strip() or None,
                "sk": str(stp.get("scan_kind") or "").strip() or None,
                "fs": str(stp.get("file_set_type") or "").strip() or None,
                "en": 1 if enabled else 0,
                "acl": 1 if bool(stp.get("is_acl")) else 0,
                "ocr": 1 if bool(stp.get("is_ocr")) else 0,
                "sff": 1 if bool(stp.get("is_structured_ff")) else 0,
                "ols": 1 if bool(stp.get("is_ols")) else 0,
                "ff": str(stp.get("feature_flags") or "").strip() or None,
                "notes": str(stp.get("notes") or "").strip() or None,
                "status": ("pending" if enabled else "disabled"),
            })
        if seq_rows:
            conn.execute(
                text("""
                    INSERT INTO workflow_steps (
                        run_id, seq, environment_id,
                        ds_kind, ds_name, scan_kind, file_set_type,
                        is_enabled,
                        is_acl, is_ocr, is_structured_ff, is_ols,
                        feature_flags, notes, status
                    )
                    VALUES (
                        :rid, :seq, :eid,
                        :dk, :dn, :sk, :fs,
                        :en,
                        :acl, :ocr, :sff, :ols,
                        :ff, :notes, :status
                    )
                """),
                seq_rows,
            )
        conn.commit()
    try:
        return int(run_id)
    except Exception:
        return None


@st.cache_data(ttl=300)
def load_workflow_runs(limit: int = 30) -> pd.DataFrame:
    if not engine:
        return pd.DataFrame()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT
                        wr.id,
                        wr.name,
                        wr.status,
                        wr.env1_id,
                        wr.env2_id,
                        wr.cooldown_minutes,
                        wr.next_eligible_at,
                        wr.started_at,
                        wr.finished_at,
                        wr.updated_at,
                        wr.created_at,
                        wr.last_error,
                        wr.scan_template_id
                    FROM workflow_runs wr
                    ORDER BY wr.id DESC
                    LIMIT :lim
                """),
                {"lim": int(limit)},
            ).fetchall()
        cols = [
            "id","name","status","env1_id","env2_id","cooldown_minutes",
            "next_eligible_at","started_at","finished_at","updated_at","created_at","last_error",
            "scan_template_id",
        ]
        return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    except Exception:
        return pd.DataFrame()


# Align with workflow_runner.WORKFLOW_ENV_OCCUPYING_STATUSES: show env as busy from step state, not only te.is_busy (API sync lags).
_WORKFLOW_UI_ENV_BUSY_STATUSES = frozenset({
    "running",
    "pre_action_done",
    "ds_created",
    "ds_test_ok",
    "scan_created",
    "scan_running",
    "scan_waiting",
})


def _workflow_norm_step_status(raw: Any) -> str:
    """Avoid pandas NaN / literal 'nan' string breaking status checks (wrong 🟢 busy dot)."""
    if raw is None:
        return ""
    try:
        if pd.isna(raw):
            return ""
    except Exception:
        pass
    s = str(raw).strip().lower()
    if s in ("nan", "nat", "none"):
        return ""
    return s


def _workflow_is_transient_step_message(msg: str) -> bool:
    """Waiting/retry messages are informational, not hard workflow errors."""
    t = str(msg or "").strip().lower()
    if not t:
        return False
    needles = (
        "will retry",
        "waiting for it to clear",
        "no free env this tick",
        "env already has a running scan",
        "env has a running scan",
        "reserved by another workflow run",
        "safety check failed",
    )
    return any(n in t for n in needles)


def _workflow_step_implies_env_busy(rd: dict) -> bool:
    """
    Per-row env dot + derived env strip: busy if DB says so, step is in DS/scan pipeline,
    or scan_id is set and step is not completed/disabled (covers error/pending + live BigID scan).
    """
    c = _workflow_norm_step_status(rd.get("status"))
    if c == "disabled":
        return False
    try:
        if rd.get("is_enabled") is not None and not bool(int(rd.get("is_enabled"))):
            return False
    except Exception:
        if not bool(rd.get("is_enabled", True)):
            return False
    ei = rd.get("env_is_busy")
    try:
        db_busy = bool(ei) if ei is not None and not pd.isna(ei) else False
    except Exception:
        db_busy = bool(ei)
    if db_busy:
        return True
    if c in _WORKFLOW_UI_ENV_BUSY_STATUSES:
        return True
    sid = str(rd.get("scan_id") or "").strip()
    if sid and c not in ("completed", "disabled"):
        return True
    return False


def load_workflow_steps_batch(run_ids: list[int]) -> pd.DataFrame:
    if not engine or not run_ids:
        return pd.DataFrame()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT
                        ws.id,
                        ws.run_id,
                        ws.seq,
                        ws.environment_id,
                        te.env_name,
                        te.is_busy AS env_is_busy,
                        ws.ds_kind,
                        ws.ds_name,
                        ws.scan_kind,
                        ws.file_set_type,
                        ws.is_enabled,
                        ws.is_acl,
                        ws.is_ocr,
                        ws.is_structured_ff,
                        ws.is_ols,
                        ws.feature_flags,
                        ws.notes,
                        ws.ds_id,
                        ws.scan_profile_id,
                        ws.scan_profile_name,
                        ws.build_version,
                        ws.build_prefix,
                        ws.scan_id,
                        ws.status,
                        ws.started_at,
                        ws.finished_at,
                        ws.updated_at,
                        ws.last_error
                    FROM workflow_steps ws
                    LEFT JOIN test_environments te ON te.id = ws.environment_id
                    WHERE ws.run_id IN :rids
                    ORDER BY ws.run_id DESC, ws.seq ASC
                """),
                {"rids": tuple(run_ids)},
            ).fetchall()
        cols = [
            "id","run_id","seq","environment_id","environment","env_is_busy",
            "ds_kind","ds_name","scan_kind","file_set_type",
            "is_enabled","is_acl","is_ocr","is_structured_ff","is_ols",
            "feature_flags","notes","ds_id","scan_profile_id","scan_profile_name","build_version","build_prefix","scan_id",
            "status","started_at","finished_at","updated_at","last_error",
        ]
        return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    except Exception:
        return pd.DataFrame()


def load_workflow_steps(run_id: int) -> pd.DataFrame:
    if not engine or run_id is None:
        return pd.DataFrame()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT
                        ws.id,
                        ws.run_id,
                        ws.seq,
                        ws.environment_id,
                        te.env_name,
                        ws.ds_kind,
                        ws.ds_name,
                        ws.scan_kind,
                        ws.file_set_type,
                        ws.is_enabled,
                        ws.is_acl,
                        ws.is_ocr,
                        ws.is_structured_ff,
                        ws.is_ols,
                        ws.feature_flags,
                        ws.notes,
                        ws.ds_id,
                    ws.scan_profile_id,
                    ws.scan_profile_name,
                    ws.build_version,
                    ws.build_prefix,
                        ws.scan_id,
                        ws.status,
                        ws.started_at,
                        ws.finished_at,
                        ws.updated_at,
                        ws.last_error
                    FROM workflow_steps ws
                    LEFT JOIN test_environments te ON te.id = ws.environment_id
                    WHERE ws.run_id = :rid
                    ORDER BY ws.seq ASC
                """),
                {"rid": int(run_id)},
            ).fetchall()
        cols = [
            "id","run_id","seq","environment_id","environment",
            "ds_kind","ds_name","scan_kind","file_set_type",
            "is_enabled","is_acl","is_ocr","is_structured_ff","is_ols",
            "feature_flags","notes","ds_id","scan_profile_id","scan_profile_name","build_version","build_prefix","scan_id",
            "status","started_at","finished_at","updated_at","last_error",
        ]
        return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    except Exception:
        return pd.DataFrame()


def set_workflow_run_status(run_id: int, status: str, *, last_error: Optional[str] = None) -> None:
    if not engine or run_id is None:
        return
    ensure_workflow_tables()
    stv = str(status or "").strip()
    if not stv:
        return
    try:
        with engine.connect() as conn:
            # When starting/resuming, clear next_eligible_at so the runner can pick steps immediately (no stale cooldown).
            if stv == "running":
                conn.execute(
                    text("""
                        UPDATE workflow_runs
                        SET status = :s, last_error = COALESCE(:e, last_error), next_eligible_at = NULL
                        WHERE id = :rid
                    """),
                    {"s": stv, "e": (str(last_error)[:2000] if last_error else None), "rid": int(run_id)},
                )
            else:
                conn.execute(
                    text("""
                        UPDATE workflow_runs
                        SET status = :s, last_error = COALESCE(:e, last_error)
                        WHERE id = :rid
                    """),
                    {"s": stv, "e": (str(last_error)[:2000] if last_error else None), "rid": int(run_id)},
                )
            conn.commit()
    except Exception:
        pass


def release_workflow_run_envs(eng, run_id: int) -> None:
    """Release all environments reserved by this workflow run (is_busy=1, busy_by_run_id=run_id)."""
    if not eng or run_id is None:
        return
    try:
        with eng.connect() as conn:
            conn.execute(
                text("""
                    UPDATE test_environments
                    SET is_busy = 0, busy_by_run_id = NULL, busy_since = NULL
                    WHERE busy_by_run_id = :rid
                """),
                {"rid": int(run_id)},
            )
            conn.commit()
    except Exception:
        pass


def purge_workflow_linked_scan_rows(conn, scan_ids: List[str]) -> None:
    """
    Delete local DB rows tied to BigID parent scan ids (insights, progress snapshots,
    scan_parents, auto benchmarks, manual benchmarks). Safe order for FK-heavy MySQL schemas.
    Best-effort for optional tables (e.g. benchmarks) if missing on this engine.
    """
    ids = list(dict.fromkeys(str(x).strip() for x in (scan_ids or []) if str(x).strip()))
    if not ids:
        return
    tup = tuple(ids)

    def _exec(sql: str, params: dict) -> None:
        try:
            conn.execute(text(sql), params)
        except Exception:
            pass

    _exec("DELETE FROM scan_insight_attribute_delta_map WHERE scan_id IN :ids", {"ids": tup})
    _exec("DELETE FROM scan_insight_data_source_map WHERE scan_id IN :ids", {"ids": tup})
    _exec("DELETE FROM scan_insights WHERE scan_id IN :ids", {"ids": tup})
    _exec("DELETE FROM scan_progress WHERE scan_id IN :ids", {"ids": tup})

    try:
        rows = conn.execute(
            text("SELECT id FROM benchmarks WHERE scan_id IN :ids"),
            {"ids": tup},
        ).fetchall()
        bids = tuple(int(r[0]) for r in rows if r and r[0] is not None)
        if bids:
            _exec("DELETE FROM benchmark_findings WHERE benchmark_id IN :bids", {"bids": bids})
        _exec("DELETE FROM benchmarks WHERE scan_id IN :ids", {"ids": tup})
    except Exception:
        pass

    _exec("DELETE FROM auto_scan_benchmarks WHERE scan_id IN :ids", {"ids": tup})
    _exec("DELETE FROM scan_parents WHERE scan_id IN :ids", {"ids": tup})


def delete_workflow_run_and_purge_scans(eng, run_id: int) -> List[str]:
    """
    Remove workflow run + steps and purge local DB rows for every scan_id ever recorded on those steps.
    Releases env reservations for this run. Returns the list of distinct scan ids purged.
    """
    if not eng or run_id is None:
        return []
    rid = int(run_id)
    with eng.begin() as conn:
        conn.execute(
            text("""
                UPDATE test_environments
                SET is_busy = 0, busy_by_run_id = NULL, busy_since = NULL
                WHERE busy_by_run_id = :rid
            """),
            {"rid": rid},
        )
        rows = conn.execute(
            text("""
                SELECT DISTINCT TRIM(scan_id) AS sid
                FROM workflow_steps
                WHERE run_id = :rid
                  AND scan_id IS NOT NULL
                  AND TRIM(scan_id) <> ''
            """),
            {"rid": rid},
        ).fetchall()
        scan_ids = [str(r[0]).strip() for r in rows if r and r[0]]
        purge_workflow_linked_scan_rows(conn, scan_ids)
        conn.execute(text("DELETE FROM workflow_steps WHERE run_id=:rid"), {"rid": rid})
        conn.execute(text("DELETE FROM workflow_runs WHERE id=:rid"), {"rid": rid})
    return scan_ids


def _workflow_ds_template_key(step: Dict[str, Any]) -> str:
    ds_kind = str(step.get("ds_kind") or "").strip().lower()
    fs = str(step.get("file_set_type") or "").strip() or "default"
    acl = 1 if bool(step.get("is_acl")) else 0
    struct = 1 if bool(step.get("is_structured_ff")) else 0
    return f"ds::{ds_kind}::{fs}::acl={acl}::struct={struct}"


def _workflow_scan_template_key(step: Dict[str, Any]) -> str:
    ds_kind = str(step.get("ds_kind") or "").strip().lower()
    fs = str(step.get("file_set_type") or "").strip() or "default"
    acl = 1 if bool(step.get("is_acl")) else 0
    ols = 1 if bool(step.get("is_ols")) else 0
    struct = 1 if bool(step.get("is_structured_ff")) else 0
    return f"scan::{ds_kind}::{fs}::acl={acl}::ols={ols}::struct={struct}"


def upsert_workflow_template(key: str, template_kind: str, payload_json: str) -> None:
    ensure_workflow_tables()
    if not engine:
        return
    k = str(key or "").strip()
    if not k:
        return
    tk = str(template_kind or "").strip() or None
    pj = str(payload_json or "").strip() or None
    try:
        dialect = _db_dialect_name()
    except Exception:
        dialect = ""
    try:
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
                    {"k": k, "t": tk, "p": pj},
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
                    {"k": k, "t": tk, "p": pj},
                )
            conn.commit()
    except Exception:
        pass


def get_workflow_template_payload(key: str) -> str:
    ensure_workflow_tables()
    if not engine:
        return ""
    k = str(key or "").strip()
    if not k:
        return ""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT payload_json FROM workflow_templates WHERE k = :k LIMIT 1"),
                {"k": k},
            ).fetchone()
        return str(row[0] or "").strip() if row else ""
    except Exception:
        return ""


def _is_terminal_state_anywhere(state: Any) -> bool:
    s = str(state or "").strip().lower().replace(" ", "")
    return s in {"failed","completedwithfailures","completed","stopped","aborted","cancelled","canceled"}


def _is_running_state_anywhere(state: Any) -> bool:
    s = str(state or "").strip().lower().replace(" ", "")
    return s in {"started","awaiting","inprogress","inprogress","running","queued","pending","stoprequested","pauserequested"}


def reconcile_workflow_run(run_id: int) -> Dict[str, Any]:
    """
    Best-effort reconciliation using DB scan status.
    - Marks workflow step completed if its scan_id is terminal in scan_parents or scan_progress
    - Advances run.next_eligible_at after a step completes
    """
    out = {"updated_steps": 0, "completed_run": False}
    if not engine or run_id is None:
        return out
    ensure_workflow_tables()
    try:
        steps_df = load_workflow_steps(int(run_id))
        if steps_df.empty:
            return out
    except Exception:
        return out

    # Find steps that have scan_id and are not completed yet.
    try:
        pending = steps_df[steps_df["status"].astype(str).isin(["pending", "running", "created", "scan_running", "ds_created", "scan_created", "error"])]
    except Exception:
        pending = steps_df
    scan_ids = [str(x).strip() for x in pending.get("scan_id", pd.Series(dtype=str)).tolist() if str(x).strip()]

    # Resolve latest statuses for scan_ids (if any)
    status_map: Dict[str, str] = {}
    if scan_ids:
        try:
            from sqlalchemy import bindparam
            with engine.connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT scan_id, scan_status
                        FROM scan_parents
                        WHERE scan_id IN :ids
                    """).bindparams(bindparam("ids", expanding=True)),
                    {"ids": scan_ids},
                ).fetchall()
            for r in rows or []:
                sid = str(r[0] or "").strip()
                if sid:
                    status_map[sid] = str(r[1] or "").strip()
        except Exception:
            pass
        try:
            p_map = load_latest_scan_progress_state_map(scan_ids)
            for sid, stv in (p_map or {}).items():
                if sid and sid not in status_map:
                    status_map[str(sid)] = str(stv or "").strip()
        except Exception:
            pass

    updated = 0
    now = _workflow_now_utc()
    try:
        cooldown = None
        with engine.connect() as conn:
            r = conn.execute(text("SELECT cooldown_minutes FROM workflow_runs WHERE id = :rid"), {"rid": int(run_id)}).fetchone()
            if r:
                try:
                    cooldown = int(r[0]) if r[0] is not None else 15
                except Exception:
                    cooldown = 15
    except Exception:
        cooldown = 15
    if cooldown is None:
        cooldown = 15

    if scan_ids:
        try:
            with engine.connect() as conn:
                for row in pending.itertuples():
                    sid = str(getattr(row, "scan_id", "") or "").strip()
                    if not sid:
                        continue
                    stv = status_map.get(sid, "")
                    if not _is_terminal_state_anywhere(stv):
                        # If scan is still running, ensure step reflects that (even if it was previously marked error).
                        if _is_running_state_anywhere(stv):
                            conn.execute(
                                text("""
                                    UPDATE workflow_steps
                                    SET status = 'scan_running', updated_at = CURRENT_TIMESTAMP
                                    WHERE id = :id
                                """),
                                {"id": int(getattr(row, "id"))},
                            )
                            updated += 1
                        continue
                    # Mark step completed
                    conn.execute(
                        text("""
                            UPDATE workflow_steps
                            SET status = 'completed', finished_at = COALESCE(finished_at, :t), updated_at = CURRENT_TIMESTAMP
                            WHERE id = :id
                        """),
                        {"id": int(getattr(row, "id")), "t": now},
                    )
                    updated += 1
                if updated:
                    # Set run cooldown timer
                    next_t = now + timedelta(minutes=float(cooldown))
                    conn.execute(
                        text("""
                            UPDATE workflow_runs
                            SET next_eligible_at = :t, updated_at = CURRENT_TIMESTAMP
                            WHERE id = :rid
                        """),
                        {"t": next_t, "rid": int(run_id)},
                    )
                conn.commit()
        except Exception:
            pass

    out["updated_steps"] = updated
    try:
        steps_df2 = load_workflow_steps(int(run_id))
        if not steps_df2.empty:
            stv2 = steps_df2["status"].fillna("").astype(str).str.strip().str.lower()
            enabled2 = steps_df2.get("is_enabled", pd.Series([1] * len(steps_df2))).fillna(1)
            try:
                enabled2 = enabled2.astype(int)
            except Exception:
                enabled2 = enabled2.apply(lambda x: 1 if bool(x) else 0)
            # Treat completed/disabled/error/scan_created as terminal for run completion.
            term = {"completed", "disabled", "error", "scan_created"}
            remaining = steps_df2[(enabled2.astype(int) == 1) & (~stv2.isin(list(term)))].copy()
            if remaining.empty:
                set_workflow_run_status(int(run_id), "completed")
                out["completed_run"] = True
    except Exception:
        pass
    return out


def _safe_int(val):
    try:
        if val is None or val == "":
            return None
        return int(val)
    except Exception:
        return None


def _to_float_or_none(val):
    try:
        if val is None or val == "":
            return None
        return float(val)
    except Exception:
        return None


def _safe_float(val):
    try:
        if val is None or val == "":
            return None
        return float(val)
    except Exception:
        return None


def parse_build_version_from_text(text_value):
    """
    Parse build version from scan text (e.g. 244-59 -> 244.59, 246.2 -> 246.2).
    Returns empty string when no reliable build pattern is found.
    """
    s = str(text_value or "")
    # Support 244-5, 244.5, 2 244-5, etc.
    m = re.search(r"\b(2\d{2})[\s\-\.]{1,2}(\d{1,3})\b", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    # Support 244.59.0
    m_dot3 = re.search(r"\b(2\d{2})\.(\d{1,3})\.(\d{1,3})\b", s)
    if m_dot3:
        return f"{m_dot3.group(1)}.{m_dot3.group(2)}"
    return ""


def infer_scan_benchmark_shape(scan_name, data_source_type):
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


def upsert_auto_scan_benchmark(scan_row, env_row, insight_metrics):
    if not engine or not isinstance(scan_row, dict):
        return
    ensure_auto_scan_benchmarks_table()

    scan_id = str(scan_row.get("_id") or scan_row.get("id") or "").strip()
    if not scan_id:
        return
    scan_name = str(scan_row.get("name") or scan_row.get("scanName") or scan_row.get("displayName") or "").strip()
    defaults = infer_add_benchmark_defaults_from_scan_name(scan_name)
    shape = infer_scan_benchmark_shape(scan_name, defaults.get("data_source_type"))

    start_raw = scan_row.get("startedAt") or scan_row.get("startTime") or scan_row.get("createdAt")
    end_raw = scan_row.get("completedAt") or scan_row.get("endTime") or scan_row.get("endedAt")
    start_dt = parse_iso_datetime(start_raw)
    end_dt = parse_iso_datetime(end_raw)

    total_scan_hours = None
    if start_dt and end_dt:
        delta_seconds = (end_dt - start_dt).total_seconds()
        if delta_seconds >= 0:
            total_scan_hours = float(delta_seconds / 3600.0)

    run_date = start_dt.date() if start_dt else (end_dt.date() if end_dt else None)
    raw_scan_type = str(scan_row.get("type") or "").strip().lower()
    if raw_scan_type in {"incremental", "ols_scan", "ols", "incremental_scan"}:
        scan_type = "OLS Scan"
    elif raw_scan_type in {"full_scan", "full", "fullscan"}:
        scan_type = "Full Scan"
    elif str(defaults.get("scan_type") or "").strip() in {"OLS Scan", "Full Scan"}:
        scan_type = str(defaults.get("scan_type"))
    else:
        scan_type = "OLS Scan"

    try:
        # Build version must come from scan identity, not from current env version.
        # Falling back to env version collapses historical scans into one build.
        resolved_build_version = str(defaults.get("build_version") or "").strip()
        if not resolved_build_version:
            resolved_build_version = parse_build_version_from_text(scan_name)

        resolved_data_source_type = str(defaults.get("data_source_type") or "").strip()
        if not resolved_data_source_type:
            ds_names = str((insight_metrics or {}).get("data_source_names") or "").strip()
            if ds_names:
                resolved_data_source_type = ds_names.split(",")[0].strip()
        if not resolved_data_source_type:
            resolved_data_source_type = "Unknown"

        resolved_scenario = str(shape.get("scenario_type") or defaults.get("scenario_type") or "").strip() or "Mixed File"
        resolved_data_type = str(shape.get("data_type") or "").strip() or "Mixed"
        resolved_file_set = str(shape.get("file_set_type") or "").strip()
        if not resolved_file_set:
            if str(resolved_data_source_type).upper() in {"MSSQL", "MYSQL", "POSTGRES", "ORACLE"}:
                resolved_file_set = f"{resolved_data_source_type} Mixed Tables".strip()
            else:
                resolved_file_set = f"{resolved_data_source_type} Mixed Files".strip()

        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO auto_scan_benchmarks (
                    scan_id, environment_id, build_version, run_date, scenario_type, scan_type,
                    data_source_type, data_type, file_set_type, scan_start_time, scan_end_time, total_scan_hours,
                    total_size_gb, total_files, total_folders,
                    objects_with_pi, objects_scanned, objects_enumerated, total_classification, new_attributes, removed_attributes,
                    classifier_attribute_names, scan_name, is_active
                ) VALUES (
                    :sid, :eid, :bv, :rd, :scn, :st, :dst, :dt, :fst, :ss, :se, :tsh,
                    :tgb, :tf, :tfo,
                    :owp, :osc, :oen, :tcl, :nac, :rac, :can, :sn, TRUE
                )
                ON DUPLICATE KEY UPDATE
                    environment_id = VALUES(environment_id),
                    build_version = VALUES(build_version),
                    run_date = VALUES(run_date),
                    scenario_type = VALUES(scenario_type),
                    scan_type = VALUES(scan_type),
                    data_source_type = VALUES(data_source_type),
                    data_type = VALUES(data_type),
                    file_set_type = VALUES(file_set_type),
                    scan_start_time = VALUES(scan_start_time),
                    scan_end_time = VALUES(scan_end_time),
                    total_scan_hours = VALUES(total_scan_hours),
                    objects_with_pi = VALUES(objects_with_pi),
                    objects_scanned = VALUES(objects_scanned),
                    objects_enumerated = VALUES(objects_enumerated),
                    total_classification = VALUES(total_classification),
                    new_attributes = VALUES(new_attributes),
                    removed_attributes = VALUES(removed_attributes),
                    total_size_gb = VALUES(total_size_gb),
                    total_files = VALUES(total_files),
                    total_folders = VALUES(total_folders),
                    classifier_attribute_names = VALUES(classifier_attribute_names),
                    scan_name = VALUES(scan_name),
                    updated_at = CURRENT_TIMESTAMP
            """), {
                "sid": scan_id,
                "eid": _safe_int((env_row or {}).get("id")),
                "bv": resolved_build_version or None,
                "rd": run_date,
                "scn": resolved_scenario or None,
                "st": str(scan_type or "") or None,
                "dst": resolved_data_source_type or None,
                "dt": resolved_data_type or None,
                "fst": resolved_file_set or None,
                "ss": start_dt,
                "se": end_dt,
                "tsh": total_scan_hours,
                "tgb": _to_float_or_none((insight_metrics or {}).get("total_size_gb")),
                "tf": _safe_int((insight_metrics or {}).get("total_files")),
                "tfo": _safe_int((insight_metrics or {}).get("total_folders")),
                "owp": _safe_int((insight_metrics or {}).get("objects_with_pi")),
                "osc": _safe_int((insight_metrics or {}).get("objects_scanned")),
                "oen": _safe_int((insight_metrics or {}).get("objects_enumerated")),
                "tcl": _safe_int((insight_metrics or {}).get("total_classification")),
                "nac": _safe_int((insight_metrics or {}).get("new_attributes_count")),
                "rac": _safe_int((insight_metrics or {}).get("removed_attributes_count")),
                "can": str((insight_metrics or {}).get("classifier_attribute_names") or "") or None,
                "sn": scan_name or None,
            })
            conn.commit()
    except Exception:
        pass


def load_auto_scan_benchmarks_preview(environment_id, limit=200):
    if not engine or environment_id is None:
        return pd.DataFrame()
    ensure_auto_scan_benchmarks_table()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    ab.scan_id,
                    COALESCE(ab.build_version, '') AS build_version,
                    ab.run_date,
                    COALESCE(ab.scenario_type, '') AS scenario_type,
                    COALESCE(ab.scan_type, '') AS scan_type,
                    COALESCE(ab.data_source_type, '') AS data_source_type,
                    COALESCE(ab.data_type, '') AS data_type,
                    COALESCE(ab.file_set_type, '') AS file_set_type,
                    ab.total_scan_hours,
                    ab.objects_scanned,
                    ab.total_classification,
                    ab.new_attributes,
                    ab.removed_attributes,
                    ab.updated_at
                FROM auto_scan_benchmarks ab
                LEFT JOIN scan_parents sp ON sp.scan_id = ab.scan_id
                WHERE ab.environment_id = :eid
                  AND LOWER(COALESCE(sp.scan_status, '')) NOT IN ('stopped', 'aborted')
                  AND LOWER(COALESCE(sp.scan_name, '')) NOT LIKE 'instant scan data%'
                  AND LOWER(COALESCE(ab.scan_name, '')) NOT LIKE 'instant scan data%'
                ORDER BY ab.updated_at DESC
                LIMIT :lim
            """), {"eid": int(environment_id), "lim": int(limit)}).fetchall()
        df = pd.DataFrame(
            rows,
            columns=[
                "scan_id", "build_version", "run_date", "scenario_type", "scan_type",
                "data_source_type", "data_type", "file_set_type", "total_scan_hours",
                "objects_scanned", "total_classification", "new_attributes", "removed_attributes", "updated_at"
            ]
        )
        # Arrow-safe dtypes: don't mix ints with '' strings.
        numeric_cols = ["objects_scanned", "total_classification", "new_attributes", "removed_attributes"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
        # Fill non-numeric columns for nicer display
        for col in df.columns:
            if col in numeric_cols:
                continue
            df[col] = df[col].fillna("")
        return df
    except Exception:
        return pd.DataFrame()


def load_auto_scan_benchmark_row(scan_id):
    """
    Load one auto trend row by scan id.
    """
    if not engine or not scan_id:
        return {}
    ensure_auto_scan_benchmarks_table()
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT
                    scan_id,
                    COALESCE(build_version, '') AS build_version,
                    run_date,
                    COALESCE(scenario_type, '') AS scenario_type,
                    COALESCE(scan_type, '') AS scan_type,
                    COALESCE(data_source_type, '') AS data_source_type,
                    COALESCE(data_type, '') AS data_type,
                    COALESCE(file_set_type, '') AS file_set_type,
                    total_scan_hours,
                    objects_scanned,
                    total_classification,
                    new_attributes,
                    removed_attributes,
                    updated_at
                FROM auto_scan_benchmarks
                WHERE scan_id = :sid
                LIMIT 1
            """), {"sid": str(scan_id)}).fetchone()
        if not row:
            return {}
        result = {
            "scan_id": row[0],
            "build_version": row[1],
            "run_date": row[2],
            "scenario_type": row[3],
            "scan_type": row[4],
            "data_source_type": row[5],
            "data_type": row[6],
            "file_set_type": row[7],
            "total_scan_hours": row[8],
            "objects_scanned": row[9],
            "total_classification": row[10],
            "new_attributes": row[11],
            "removed_attributes": row[12],
            "updated_at": row[13],
        }
        for k in ["objects_scanned", "total_classification", "new_attributes", "removed_attributes"]:
            try:
                if result.get(k) is not None and result.get(k) != "":
                    result[k] = int(result.get(k))
            except Exception:
                pass
        return result
    except Exception:
        return {}

def upsert_scan_progress_record(env_id, scan_id, scan_name, insight_payload, *, align_10min: bool = True):
    """
    Saves a snapshot of current scan progress to the scan_progress table.
    """
    if not engine or not scan_id:
        return {"ok": False, "error": "missing_engine_or_scan_id"}
    
    # Extract metrics from payload. BigID response is usually { "data": { "scanInsights": { ... } } }
    si = insight_payload.get("data", {}).get("scanInsights", {}) if isinstance(insight_payload, dict) else {}
    if not si:
        # Fallback if payload is already the scanInsights block
        si = insight_payload if isinstance(insight_payload, dict) else {}

    def _to_int(val, default=0):
        try:
            if val is None or val == "":
                return int(default)
            return int(val)
        except Exception:
            return int(default)

    subscans = si.get("subScans") if isinstance(si.get("subScans"), dict) else {}
    data_sources = si.get("dataSources") if isinstance(si.get("dataSources"), dict) else {}
    classifications = data_sources.get("classifications") if isinstance(data_sources.get("classifications"), dict) else {}
    progress = si.get("scanProfile", {}).get("progress", {}) if isinstance(si.get("scanProfile"), dict) else {}
    # Store API scan lifecycle on scanInsights.state only (not progress.state — see tracker._choose_best_state).
    scan_state = str(si.get("state") or "").strip() or None
    
    completed_sub = _to_int(subscans.get("completedSubScans", 0))
    failed_sub = _to_int(subscans.get("failedSubScans", 0))
    total_sub = _to_int(subscans.get("totalSubScans", 0))
    
    # Fallback to scanProfile.progress for completed/failed/total parts
    completed_parts = _to_int(subscans.get("completedParts") or progress.get("complete") or 0)
    failed_parts = _to_int(subscans.get("failedParts") or progress.get("failed") or 0)
    total_parts = _to_int(subscans.get("totalPartsScanned") or progress.get("total") or 0)
    
    total_class = _to_int(classifications.get("total", 0))
    new_class = _to_int(classifications.get("newlyAdded", 0))
    false_removed = _to_int(classifications.get("falseRemoved", 0))
    
    # In current BigID shape, objectsScanned sits under dataSources.
    objects_scanned = _to_int(data_sources.get("objectsScanned") or si.get("objectsScanned") or 0)
    objects_failed = _to_int(data_sources.get("objectsFailed") or 0)
    objects_with_pi = _to_int(data_sources.get("objectsWithPi") or 0)

    try:
        # Use explicit UTC time.
        # - Auto tracker snapshots: align to 10-minute marks (11:10, 11:20, ...)
        # - Manual snapshots: record current time (not slot-aligned)
        now_utc = datetime.utcnow()
        if bool(align_10min):
            captured_at_utc = now_utc.replace(
                minute=(now_utc.minute // 10) * 10,
                second=0,
                microsecond=0,
            )
        else:
            captured_at_utc = now_utc.replace(microsecond=0)
        with engine.connect() as conn:
            # start-marker row (scan start time): write the real BigID startedAt as the first DB row for this scan
            try:
                sp0 = si.get("scanProfile") if isinstance(si.get("scanProfile"), dict) else {}
                started_raw = sp0.get("startedAt") or sp0.get("startTime") or sp0.get("createdAt")
                started_dt = pd.to_datetime(started_raw, errors="coerce", utc=True) if started_raw else None
                if started_dt is not None and pd.notnull(started_dt):
                    row = conn.execute(text("SELECT 1 FROM scan_progress WHERE scan_id=:sid LIMIT 1"), {"sid": str(scan_id)}).fetchone()
                    if not row:
                        from datetime import timedelta
                        cau = started_dt.to_pydatetime().replace(tzinfo=None, microsecond=0)
                        # Avoid colliding with slot-aligned snapshots (seconds=0 at 10-min boundaries)
                        if (cau.second == 0) and (cau.minute % 10 == 0):
                            cau = cau + timedelta(seconds=1)
                        ts0 = started_dt.to_pydatetime().replace(tzinfo=None, microsecond=0)
                        conn.execute(text("""
                            INSERT IGNORE INTO scan_progress (
                                captured_at_utc, `timestamp`,
                                scan_id, scan_name, environment_id,
                                scan_state,
                                completed_sub_scans, failed_sub_scans, total_sub_scans,
                                completed_parts, failed_parts, total_parts_scanned,
                                total_classifications, newly_added_classifications, false_removed_classifications,
                                objects_scanned, objects_failed, objects_with_pi
                            ) VALUES (
                                :cau, :ts,
                                :sid, :sname, :eid,
                                :sst,
                                0,0,0,
                                0,0,0,
                                0,0,0,
                                0,0,0
                            )
                        """), {
                            "cau": cau,
                            "ts": ts0,
                            "sid": str(scan_id),
                            "sname": str(scan_name or ""),
                            "eid": env_id,
                            "sst": "Started",
                        })
            except Exception:
                pass

            conn.execute(text("""
                INSERT INTO scan_progress (
                    captured_at_utc,
                    scan_id, scan_name, environment_id,
                    scan_state,
                    completed_sub_scans, failed_sub_scans, total_sub_scans,
                    completed_parts, failed_parts, total_parts_scanned,
                    total_classifications, newly_added_classifications,
                    false_removed_classifications,
                    objects_scanned, objects_failed, objects_with_pi
                ) VALUES (
                    :cau, :sid, :sname, :eid, :sst, :cs, :fs, :tsc, :cp, :fp, :tp, :tc, :nc, :fr, :os, :of, :owp
                )
                ON DUPLICATE KEY UPDATE
                    scan_name = VALUES(scan_name),
                    environment_id = VALUES(environment_id),
                    scan_state = VALUES(scan_state),
                    completed_sub_scans = VALUES(completed_sub_scans),
                    failed_sub_scans = VALUES(failed_sub_scans),
                    total_sub_scans = VALUES(total_sub_scans),
                    completed_parts = VALUES(completed_parts),
                    failed_parts = VALUES(failed_parts),
                    total_parts_scanned = VALUES(total_parts_scanned),
                    total_classifications = VALUES(total_classifications),
                    newly_added_classifications = VALUES(newly_added_classifications),
                    false_removed_classifications = VALUES(false_removed_classifications),
                    objects_scanned = VALUES(objects_scanned),
                    objects_failed = VALUES(objects_failed),
                    objects_with_pi = VALUES(objects_with_pi),
                    `timestamp` = CURRENT_TIMESTAMP
            """), {
                "cau": captured_at_utc,
                "sid": scan_id, "sname": scan_name, "eid": env_id,
                "sst": scan_state,
                "cs": completed_sub, "fs": failed_sub, "tsc": total_sub,
                "cp": completed_parts, "fp": failed_parts, "tp": total_parts,
                "tc": total_class, "nc": new_class, "fr": false_removed,
                "os": objects_scanned, "of": objects_failed, "owp": objects_with_pi
            })
            conn.commit()
        # Final terminal snapshot logic
        try:
            terminal_states = {
                "completed", "completedwithfailures", "completed with failures",
                "failed", "stopped", "stop", "aborted", "cancelled", "canceled",
            }
            is_terminal = str(scan_state).strip().lower().replace(" ", "") in {x.replace(" ", "") for x in terminal_states}
            if is_terminal:
                try:
                    # Get actual completedAt from BigID if available
                    si = insight_payload.get("data", {}).get("scanInsights", {}) if isinstance(insight_payload, dict) else {}
                    if not si:
                        si = insight_payload if isinstance(insight_payload, dict) else {}
                    sp = si.get("scanProfile") if isinstance(si.get("scanProfile"), dict) else {}
                    cat = sp.get("completedAt")
                    if cat:
                        cat_dt = None
                        try:
                            clean_time = str(cat).replace("Z", "+00:00")
                            cat_dt = datetime.fromisoformat(clean_time)
                            if cat_dt.tzinfo:
                                cat_dt = cat_dt.astimezone(timedelta(0)).replace(tzinfo=None)
                        except Exception:
                            pass
                        
                        if cat_dt:
                            with engine.connect() as conn:
                                existing = conn.execute(text("""
                                    SELECT id FROM scan_progress 
                                    WHERE scan_id = :sid AND scan_state = :st AND captured_at_utc = :cat
                                """), {"sid": scan_id, "st": scan_state, "cat": cat_dt}).fetchone()
                                
                                if not existing:
                                    conn.execute(text("""
                                        INSERT INTO scan_progress (
                                            scan_id, scan_name, environment_id, scan_state,
                                            captured_at_utc, timestamp,
                                            completed_parts, failed_parts, total_parts_scanned,
                                            objects_scanned, objects_failed, objects_with_pi,
                                            total_classifications, newly_added_classifications, false_removed_classifications
                                        ) VALUES (
                                            :sid, :sn, :eid, :st, :cat, :cat,
                                            :cp, :fp, :tp, :os, :of, :opi, :tc, :nac, :frc
                                        )
                                    """), {
                                        "sid": scan_id, "sn": scan_name, "eid": env_id, "st": scan_state, "cat": cat_dt,
                                        "cp": completed_parts, "fp": failed_parts, "tp": total_parts,
                                        "os": objects_scanned, "of": objects_failed, "opi": objects_with_pi,
                                        "tc": total_class, "nac": new_class, "frc": false_removed
                                    })
                                    conn.commit()
                except Exception:
                    pass
        except Exception:
            pass

        return {"ok": True, "captured_at_utc": captured_at_utc, "scan_state": scan_state}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


def _strip_mb_to_int(val_str):
    if val_str is None: return 0
    s = str(val_str).strip().upper()
    if not s: return 0
    s = s.replace("MB", "").replace("M", "").replace(",", "").strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0

def get_scanner_group_performance(base_url, token):
    if not base_url or not token: return []
    url = f"{base_url.rstrip('/')}/api/v1/scanner-groups"
    try:
        resp = requests.get(url, headers={"authorization": token}, verify=False, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            groups = []
            for item in data.get("data", []):
                for sg in item.get("scannerGroups", []):
                    groups.append({
                        "name": sg.get("name"),
                        "runningScanners": sg.get("runningScanners", 0),
                        "runningThreads": sg.get("runningThreads", 0),
                        "maxThreads": sg.get("maxThreads", 0),
                        "scannerThreads": sg.get("scannerThreads", 0),
                        "avgScanSpeed": sg.get("avgScanSpeed", 0),
                        "physicalMemory": sg.get("physicalMemory"),
                        "freeMemory": sg.get("freeMemory"),
                        "updateDate": sg.get("updateDate")
                    })
            return groups
    except Exception:
        pass
    return []

def get_individual_scanner_performance(base_url, token):
    if not base_url or not token: return []
    url = f"{base_url.rstrip('/')}/api/v1/health-checks/services-status"
    try:
        resp = requests.get(url, headers={"authorization": token}, verify=False, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            scanners = []
            for item in data:
                if item.get("type") == "scanner":
                    scanners.append({
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "status": item.get("status"),
                        "group": item.get("groupName"),
                        "runningThreads": item.get("runningThreads", 0),
                        "totalNumOfThreads": item.get("totalNumOfThreads", 0),
                        "systemLoadAverage": item.get("systemLoadAverage", 0),
                        "physicalMemory": item.get("physicalMemory"),
                        "freeMemory": item.get("freeMemory"),
                        "scannerThreads": item.get("scannerThreads", 0),
                        "updateDate": item.get("updateDate")
                    })
            return scanners
    except Exception:
        pass
    return []

def _now_utc_naive():
    return datetime.utcnow()

def _strip_mb_to_int(v):
    try:
        if not v: return 0
        s = str(v).upper().replace("MB", "").replace(" ", "").strip()
        return int(float(s))
    except Exception:
        return 0

def upsert_scanner_performance(engine, env_id, group_metrics, individual_metrics, captured_at=None):
    if not engine or env_id is None: return
    now = captured_at if captured_at else _now_utc_naive()
    now = now.replace(microsecond=0)
    try:
        with engine.connect() as conn:
            for g in group_metrics:
                update_date = _parse_iso_dt(str(g.get("updateDate") or ""))
                conn.execute(text("""
                    INSERT IGNORE INTO scanner_group_performance (
                        environment_id, captured_at_utc, group_name,
                        running_scanners, running_threads, max_threads, scanner_threads,
                        physical_memory_mb, free_memory_mb, avg_scan_speed, update_date_bigid
                    ) VALUES (:eid, :cau, :gn, :rs, :rt, :mt, :st, :pm, :fm, :as, :ud)
                """), {
                    "eid": env_id, "cau": now, "gn": str(g.get("name") or ""),
                    "rs": int(g.get("runningScanners") or 0), "rt": int(g.get("runningThreads") or 0),
                    "mt": int(g.get("maxThreads") or 0), "st": int(g.get("scannerThreads") or 0),
                    "pm": _strip_mb_to_int(g.get("physicalMemory")), "fm": _strip_mb_to_int(g.get("freeMemory")),
                    "as": float(g.get("avgScanSpeed") or 0), "ud": update_date
                })
            for i in individual_metrics:
                update_date = _parse_iso_dt(str(i.get("updateDate") or ""))
                sid = str(i.get("id") or "")
                s_name = str(i.get("name") or "")
                # If multiple scanners have the same name, we suffix with ID to separate lines in charts
                display_name = f"{s_name} [{sid[:4]}]" if sid else s_name

                conn.execute(text("""
                    INSERT IGNORE INTO scanner_individual_performance (
                        environment_id, captured_at_utc, scanner_id, name, status, group_name,
                        running_threads, total_num_of_threads, system_load_average,
                        physical_memory_mb, free_memory_mb, scanner_threads, update_date_bigid
                    ) VALUES (
                        :eid, :cau, :sid, :name, :status, :gn,
                        :rt, :tt, :la, :pm, :fm, :st, :ud
                    )
                """), {
                    "eid": env_id, "cau": now, "sid": sid,
                    "name": display_name, "status": str(i.get("status") or ""),
                    "gn": str(i.get("group") or ""),
                    "rt": int(i.get("runningThreads") or 0),
                    "tt": int(i.get("totalNumOfThreads") or 0),
                    "la": float(i.get("systemLoadAverage") or 0),
                    "pm": _strip_mb_to_int(i.get("physicalMemory")), 
                    "fm": _strip_mb_to_int(i.get("freeMemory")),
                    "st": int(i.get("scannerThreads") or 0), "ud": update_date
                })
            conn.commit()
    except Exception:
        pass

def _parse_iso_dt(val):
    if not val: return None
    try:
        clean = str(val).replace("Z", "+00:00")
        return datetime.fromisoformat(clean).replace(tzinfo=None)
    except Exception:
        return None

def capture_scanner_performance_for_env(env_row, captured_at=None):
    if not env_row or not engine: return {"ok": False}
    eid = env_row.get("id")
    base_url = env_row.get("base_url")
    token = None
    try:
        from bigid_auth import Get_auth_token
        token = Get_auth_token(env_row)
    except Exception:
        pass
    if not token: return {"ok": False}
    try:
        g = get_scanner_group_performance(base_url, token)
        i = get_individual_scanner_performance(base_url, token)
        if g or i:
            upsert_scanner_performance(engine, eid, g, i, captured_at=captured_at)
            return {"ok": True, "groups": len(g), "individuals": len(i)}
    except Exception:
        pass
    return {"ok": False}


def _resolve_env_row_for_scan_progress(scan_id):
    """
    Best-effort environment resolution for Scan Progress page:
    1) Use scan_parents.environment_id (if Scan Status was loaded at least once)
    2) Fallback: try each active environment until the insights API succeeds
    Returns: (env_row_dict_or_None, live_payload_or_None)
    """
    scan_id = str(scan_id or "").strip()
    if not scan_id:
        return None, None

    # 1) DB lookup from scan_parents
    try:
        if engine:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT environment_id FROM scan_parents WHERE scan_id = :sid ORDER BY last_seen_at DESC, id DESC LIMIT 1"),
                    {"sid": scan_id},
                ).fetchone()
            env_id = row[0] if row else None
            if env_id is not None:
                env_row = get_test_environment_by_id(env_id)
                if env_row:
                    try:
                        payload, _url = fetch_parent_scan_insight_with_auth_retry(env_row, scan_id)
                        return env_row, payload
                    except Exception:
                        # If env exists but API fails, fall back to brute-force below
                        pass
    except Exception:
        pass

    # 2) Brute force: try all active environments
    try:
        env_df = load_test_environments()
        if not env_df.empty and "is_active" in env_df.columns:
            env_df = env_df[env_df["is_active"].apply(lambda x: bool(x))]
        env_rows = env_df.to_dict(orient="records") if not env_df.empty else []
    except Exception:
        env_rows = []

    for env_row in env_rows:
        try:
            payload, _url = fetch_parent_scan_insight_with_auth_retry(env_row, scan_id)
            return env_row, payload
        except Exception:
            continue

    return None, None


def _fetch_live_progress_for_scan_id(scan_id):
    """
    Best-effort live fetch for Scan Progress page, with debug output.
    Returns: (env_row_or_None, live_payload_or_None, attempts[list[str]])
    """
    attempts = []
    scan_id = str(scan_id or "").strip()
    if not scan_id:
        return None, None, attempts

    # 1) Try environment from DB scan_parents mapping first.
    try:
        if engine:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT environment_id FROM scan_parents WHERE scan_id = :sid ORDER BY last_seen_at DESC, id DESC LIMIT 1"),
                    {"sid": scan_id},
                ).fetchone()
            env_id = row[0] if row else None
            if env_id is not None:
                env_row = get_test_environment_by_id(env_id)
                if env_row:
                    try:
                        payload, _url = fetch_parent_scan_insight_with_auth_retry(env_row, scan_id)
                        return env_row, payload, attempts
                    except Exception as ex:
                        attempts.append(f"env_id={env_id} live fetch failed: {ex}")
    except Exception as ex:
        attempts.append(f"db env lookup failed: {ex}")

    # 2) Try all active environments until one succeeds.
    env_rows = []
    try:
        env_df = load_test_environments()
        if not env_df.empty and "is_active" in env_df.columns:
            env_df = env_df[env_df["is_active"].apply(lambda x: bool(x))]
        env_rows = env_df.to_dict(orient="records") if not env_df.empty else []
    except Exception as ex:
        attempts.append(f"load_test_environments failed: {ex}")

    for env_row in env_rows:
        env_name = str(env_row.get("env_name") or env_row.get("id") or "env").strip()
        try:
            payload, _url = fetch_parent_scan_insight_with_auth_retry(env_row, scan_id)
            return env_row, payload, attempts
        except Exception as ex:
            attempts.append(f"{env_name}: {ex}")
            continue

    return None, None, attempts

def get_last_progress_timestamp(scan_id):
    if not engine or not scan_id:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT MAX(timestamp) FROM scan_progress WHERE scan_id = :sid"), {"sid": scan_id}).fetchone()
            return row[0] if row else None
    except Exception:
        return None

def check_last_snapshot_is_zero(scan_id):
    """
    Checks if the most recent snapshot for this scan has all zero metrics.
    Used to force a re-capture if the previous one failed due to a bug.
    """
    if not engine or not scan_id:
        return False
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT total_sub_scans, total_parts_scanned, total_classifications, objects_scanned 
                FROM scan_progress 
                WHERE scan_id = :sid 
                ORDER BY timestamp DESC LIMIT 1
            """), {"sid": scan_id}).fetchone()
            if not row:
                return True # No data yet, allow capture
            # If all major metrics are 0, return True (is zero)
            return all(float(val or 0) == 0 for val in row)
    except Exception:
        return False

@st.cache_data(ttl=60)
def get_scans_with_progress_ids():
    """
    Returns a set of scan_ids that have at least one snapshot in scan_progress.
    """
    if not engine:
        return set()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT DISTINCT scan_id FROM scan_progress")).fetchall()
            return {str(r[0]).strip() for r in rows if r[0]}
    except Exception:
        return set()

def upsert_scan_parent_record(env_id, scan_row, resolved_status):
    if not engine or not scan_row:
        return
    scan_id = str(scan_row.get("_id") or scan_row.get("id") or "").strip()
    if not scan_id:
        return
    name = str(scan_row.get("name") or scan_row.get("scanName") or scan_row.get("displayName") or "").strip()
    template_name = str(scan_row.get("scanTemplateName") or "").strip()
    scan_type_raw = str(scan_row.get("type") or "").strip()
    _ct = scan_type_raw.strip().lower().replace(" ", "")
    _tmpl = template_name.strip().lower()
    if _ct in {"dsscan", "fullscan", "full_scan"} or ("data source scan" in _tmpl and _ct in {"", "dsscan"}):
        scan_type = "full_scan"
    else:
        scan_type = scan_type_raw
    started_at = str(scan_row.get("startedAt") or scan_row.get("startTime") or scan_row.get("createdAt") or "").strip()
    completed_at = str(scan_row.get("completedAt") or scan_row.get("endTime") or scan_row.get("endedAt") or "").strip()
    status = str(resolved_status or "").strip()
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO scan_parents (
                    scan_id, environment_id, scan_name, scan_type, started_at, completed_at, template_name, scan_status, is_active
                ) VALUES (:sid, :eid, :sn, :st, :sa, :ca, :tn, :ss, TRUE)
                ON DUPLICATE KEY UPDATE
                    environment_id = VALUES(environment_id),
                    scan_name = VALUES(scan_name),
                    scan_type = VALUES(scan_type),
                    started_at = VALUES(started_at),
                    completed_at = VALUES(completed_at),
                    template_name = VALUES(template_name),
                    scan_status = VALUES(scan_status),
                    last_seen_at = CURRENT_TIMESTAMP
            """), {
                "sid": scan_id,
                "eid": int(env_id) if env_id is not None else None,
                "sn": name or None,
                "st": scan_type or None,
                "sa": started_at or None,
                "ca": completed_at or None,
                "tn": template_name or None,
                "ss": status or None,
            })
            conn.commit()
    except Exception:
        pass


def is_ignored_scan_name(scan_name):
    name = str(scan_name or "").strip().lower()
    if not name:
        return False
    return bool(re.match(r"^instant\s+scan\s+data", name))


@st.cache_data(ttl=60)
def get_scan_parent_active_map(scan_ids):
    active_map = {}
    if not engine:
        return active_map
    cleaned_ids = []
    seen = set()
    for scan_id in (scan_ids or []):
        sid = str(scan_id or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        cleaned_ids.append(sid)
    if not cleaned_ids:
        return active_map
    try:
        from sqlalchemy import bindparam
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT scan_id, is_active FROM scan_parents WHERE scan_id IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": cleaned_ids},
            ).fetchall()
            for row in rows:
                sid = str(row[0] or "").strip()
                if sid:
                    active_map[sid] = bool(row[1])
    except Exception:
        pass
    return active_map

def get_scan_parent_details(scan_id):
    """
    Returns the scan_parent record (dict) for a given scan_id.
    """
    if not engine or not scan_id:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT scan_id, scan_name, environment_id, scan_type, scan_status, is_active, started_at, completed_at
                FROM scan_parents WHERE scan_id = :sid
            """), {"sid": scan_id}).fetchone()
            if row:
                return {
                    "scan_id": row[0],
                    "name": row[1],
                    "environment_id": row[2],
                    "type": row[3],
                    "scan_status": row[4],
                    "is_active": row[5],
                    "started_at": str(row[6] or "").strip(),
                    "completed_at": str(row[7] or "").strip(),
                }
    except Exception:
        pass
    return None


def load_scan_parents_from_db(environment_id, status_view="All", limit=200):
    if not engine or environment_id is None:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    scan_id,
                    scan_name,
                    scan_type,
                    started_at,
                    completed_at,
                    template_name,
                    scan_status
                FROM scan_parents
                WHERE environment_id = :eid
                  AND LOWER(COALESCE(scan_name, '')) NOT LIKE 'instant scan data%'
                ORDER BY updated_at DESC
                LIMIT :lim
            """), {"eid": int(environment_id), "lim": int(limit)}).fetchall()
    except Exception:
        return []

    running_states = {"Started", "Awaiting", "In Progress", "InProgress", "Running"}
    completed_states = {"Failed", "CompletedWithFailures", "Completed"}
    excluded_states = {"Stopped", "Aborted"}
    items = []
    for r in rows:
        state = str(r[6] or "").strip()
        if state in excluded_states:
            continue
        if status_view == "In Progress" and state not in running_states:
            continue
        if status_view == "Completed" and state not in completed_states:
            continue
        items.append({
            "_id": str(r[0] or "").strip(),
            "name": str(r[1] or "").strip(),
            "type": str(r[2] or "").strip(),
            "startedAt": str(r[3] or "").strip(),
            "completedAt": str(r[4] or "").strip(),
            "scanTemplateName": str(r[5] or "").strip(),
            "state": state,
        })
    return items


@st.cache_data(ttl=300)
def load_scan_parents_for_environments(environment_ids, status_view="All", limit=500):
    if not engine:
        return []
    cleaned_ids = []
    seen = set()
    for env_id in (environment_ids or []):
        try:
            eid = int(env_id)
        except Exception:
            continue
        if eid in seen:
            continue
        seen.add(eid)
        cleaned_ids.append(eid)
    if not cleaned_ids:
        return []

    try:
        from sqlalchemy import bindparam
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    sp.scan_id,
                    sp.scan_name,
                    sp.scan_type,
                    sp.started_at,
                    sp.completed_at,
                    sp.template_name,
                    sp.scan_status,
                    sp.environment_id,
                    te.env_name
                FROM scan_parents sp
                LEFT JOIN test_environments te ON te.id = sp.environment_id
                WHERE sp.environment_id IN :env_ids
                  AND LOWER(COALESCE(sp.scan_name, '')) NOT LIKE 'instant scan data%'
                ORDER BY
                    CASE
                        WHEN sp.scan_status IN ('Started','Awaiting','In Progress','InProgress','Running','Stop Requested','StopRequested','Stopping') THEN 0
                        ELSE 1
                    END,
                    sp.updated_at DESC
                LIMIT :lim
            """).bindparams(bindparam("env_ids", expanding=True)), {"env_ids": cleaned_ids, "lim": int(limit)}).fetchall()
    except Exception:
        return []

    running_states = {"Started", "Awaiting", "In Progress", "InProgress", "Running"}
    completed_states = {"Failed", "CompletedWithFailures", "Completed"}
    excluded_states = {"Stopped", "Aborted"}
    items = []
    for r in rows:
        state = str(r[6] or "").strip()
        if state in excluded_states:
            continue
        if status_view == "In Progress" and state not in running_states:
            continue
        if status_view == "Completed" and state not in completed_states:
            continue
        items.append({
            "_id": str(r[0] or "").strip(),
            "name": str(r[1] or "").strip(),
            "type": str(r[2] or "").strip(),
            "startedAt": str(r[3] or "").strip(),
            "completedAt": str(r[4] or "").strip(),
            "scanTemplateName": str(r[5] or "").strip(),
            "state": state,
            "environment_id": r[7],
            "environment_name": str(r[8] or "").strip(),
        })
    return items


def set_scan_parent_active(scan_id, is_active):
    if not engine or not scan_id:
        return
    try:
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE scan_parents SET is_active = :a WHERE scan_id = :sid"),
                {"a": bool(is_active), "sid": str(scan_id)}
            )
            conn.commit()
    except Exception:
        pass


@st.cache_data(ttl=60)
def get_saved_benchmark_scan_ids():
    """
    Returns a set of scan_ids that have already been converted to manual benchmarks.
    """
    if not engine:
        return set()
    saved_ids = set()
    try:
        with engine.connect() as conn:
            # Check benchmarks (manual) - scan_id column should exist now
            try:
                col_check = conn.execute(text("SHOW COLUMNS FROM benchmarks LIKE 'scan_id'")).fetchone()
                if col_check:
                    rows_manual = conn.execute(text("SELECT DISTINCT scan_id FROM benchmarks WHERE scan_id IS NOT NULL")).fetchall()
                    for r in rows_manual:
                        if r[0]:
                            saved_ids.add(str(r[0]).strip().lower())
            except Exception:
                pass
    except Exception:
        pass
    return saved_ids


def load_scan_insight_from_db(scan_id):
    if not engine or not scan_id:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT
                    distinct_classification_attributes,
                    objects_with_pi,
                    objects_scanned,
                    objects_enumerated,
                    total_classification,
                    new_attributes_count,
                    removed_attributes_count,
                    total_size_gb
                FROM scan_insights
                WHERE scan_id = :sid
                LIMIT 1
            """), {"sid": str(scan_id)}).fetchone()
            if not row:
                return None

            ds_rows = conn.execute(text("""
                SELECT d.name
                FROM scan_insight_data_source_map m
                JOIN scan_data_sources d ON d.id = m.data_source_id
                WHERE m.scan_id = :sid
                ORDER BY d.name
            """), {"sid": str(scan_id)}).fetchall()
            data_source_names = ", ".join(str(r[0]) for r in ds_rows if r and r[0])

            attr_rows = conn.execute(text("""
                SELECT a.name
                FROM scan_insight_attribute_delta_map m
                JOIN scan_classifier_attributes a ON a.id = m.attribute_id
                WHERE m.scan_id = :sid
                ORDER BY a.name
            """), {"sid": str(scan_id)}).fetchall()
            classifier_attribute_names = ", ".join(str(r[0]) for r in attr_rows if r and r[0])

            delta_counts = conn.execute(text("""
                SELECT
                    COALESCE(SUM(CASE
                        WHEN new_count_value IS NOT NULL THEN new_count_value
                        WHEN is_new = TRUE THEN 1 ELSE 0 END), 0) AS new_count,
                    COALESCE(SUM(CASE
                        WHEN removed_count_value IS NOT NULL THEN removed_count_value
                        WHEN is_removed = TRUE THEN 1 ELSE 0 END), 0) AS removed_count
                FROM scan_insight_attribute_delta_map
                WHERE scan_id = :sid
            """), {"sid": str(scan_id)}).fetchone()
            map_new_count = int(delta_counts[0]) if delta_counts and delta_counts[0] is not None else 0
            map_removed_count = int(delta_counts[1]) if delta_counts and delta_counts[1] is not None else 0

            return {
                "distinct_classification_attributes": int(row[0]) if row[0] is not None and str(row[0]).strip() != "" else None,
                "classifier_attribute_names": classifier_attribute_names,
                "data_source_names": data_source_names,
                "objects_with_pi": int(row[1]) if row[1] is not None and str(row[1]).strip() != "" else None,
                "objects_scanned": int(row[2]) if row[2] is not None and str(row[2]).strip() != "" else None,
                "objects_enumerated": int(row[3]) if row[3] is not None and str(row[3]).strip() != "" else None,
                "total_classification": int(row[4]) if row[4] is not None and str(row[4]).strip() != "" else None,
                "new_attributes_count": int(map_new_count) if int(map_new_count or 0) != 0 else (int(row[5]) if row[5] is not None and str(row[5]).strip() != "" else None),
                "removed_attributes_count": int(map_removed_count) if int(map_removed_count or 0) != 0 else (int(row[6]) if row[6] is not None and str(row[6]).strip() != "" else None),
            }
    except Exception:
        return None


def load_scan_insight_classifier_hits_df(scan_id: str) -> Tuple[pd.DataFrame, str]:
    """
    Build a table of classifier / attribute hits from `scan_insights.attributes_payload`
    (and, if empty, from nested shapes inside `insight_payload`). Populated when the
    tracker or **Sync now** runs `sync_single_scan_insight` (attributes API + upsert).
    """
    empty = (pd.DataFrame(), "")
    if not engine or not str(scan_id or "").strip():
        return empty
    sid = str(scan_id).strip()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT attributes_payload, insight_payload
                    FROM scan_insights
                    WHERE scan_id = :sid
                    LIMIT 1
                    """
                ),
                {"sid": sid},
            ).fetchone()
        if not row:
            return (
                pd.DataFrame(),
                "No `scan_insights` row yet for this scan. Use **Sync now (manual)** or wait for the tracker.",
            )
        ap_raw, ip_raw = row[0], row[1]

        def _loads(x):
            if x is None:
                return None
            if isinstance(x, dict):
                return x
            try:
                return json.loads(str(x))
            except Exception:
                return None

        items: List[Any] = []
        total_hint: Optional[int] = None
        source = ""

        ap_obj = _loads(ap_raw)
        if isinstance(ap_obj, dict):
            items, total_hint = extract_scan_attributes_payload(ap_obj)
            if items:
                source = "attributes API snapshot"

        if not items:
            ip_obj = _loads(ip_raw)
            if isinstance(ip_obj, dict):
                items, total_hint = extract_scan_attributes_payload(ip_obj)
                if items:
                    source = "parent-scan insight payload"
            if not items and isinstance(ip_obj, dict):
                data = ip_obj.get("data")
                if isinstance(data, dict):
                    si = data.get("scanInsights")
                    if isinstance(si, dict):
                        items, total_hint = extract_scan_attributes_payload(si)
                        if items:
                            source = "insight.scanInsights"

        if not items:
            return (
                pd.DataFrame(),
                "`attributes_payload` is empty for this scan. Run **Sync now** to fetch the attributes "
                "breakdown from BigID (same call as benchmark prefill).",
            )

        rows_out: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = (
                item.get("_id")
                or item.get("id")
                or item.get("name")
                or item.get("attributeName")
                or item.get("classifierAttribute")
            )
            if raw_id is None:
                continue
            label = str(raw_id).replace("classifier.", "").strip()
            if not label:
                continue

            def _cnt(v: Any) -> int:
                if v is True:
                    return 1
                if v in (False, None, ""):
                    return 0
                try:
                    return int(v)
                except Exception:
                    return 0

            n_new = _cnt(item.get("new"))
            n_rem = _cnt(item.get("removed"))
            rows_out.append(
                {
                    "Classifier / attribute": label,
                    "New findings": n_new,
                    "Removed": n_rem,
                }
            )

        if not rows_out:
            return (
                pd.DataFrame(),
                "Attributes payload exists but no rows with identifiers were parsed.",
            )

        df = pd.DataFrame(rows_out)
        df = df.sort_values(["New findings", "Classifier / attribute"], ascending=[False, True])
        note = f"Source: **{source}** · **{len(df)}** row(s) in DB"
        if total_hint is not None and int(total_hint) > len(df):
            note += f" (API reported totalCount **{int(total_hint)}** — increase attributes fetch limit in sync if you need all rows)."
        return df, note
    except Exception:
        return (
            pd.DataFrame(),
            "Could not load classifier hit table (database or JSON parse error).",
        )


def load_scan_insight_classifier_total_count_hint(scan_id: str) -> Optional[int]:
    """
    Best-effort totalCount hint from stored `scan_insights.attributes_payload`.
    Falls back to parsed item count when totalCount is absent.
    """
    if not engine or not str(scan_id or "").strip():
        return None
    sid = str(scan_id).strip()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT attributes_payload FROM scan_insights WHERE scan_id = :sid LIMIT 1"),
                {"sid": sid},
            ).fetchone()
        if not row or not row[0]:
            return None
        raw = row[0]
        obj = raw if isinstance(raw, dict) else json.loads(str(raw))
        if not isinstance(obj, dict):
            return None
        items, total_hint = extract_scan_attributes_payload(obj)
        if total_hint is not None:
            try:
                return int(total_hint)
            except Exception:
                pass
        return len(items) if isinstance(items, list) and items else None
    except Exception:
        return None


def fetch_live_scan_classifier_hits_df(scan_id: str, env_row: Optional[Dict[str, Any]]) -> Tuple[pd.DataFrame, str, Optional[int]]:
    """
    Live one-shot fetch from BigID attributes API (same endpoint as Manual Sync).
    Returns (df, note, total_count_hint).
    """
    empty = (pd.DataFrame(), "No live classifier hit rows.", None)
    if not str(scan_id or "").strip() or not isinstance(env_row, dict):
        return empty
    base_url = str(env_row.get("base_url") or "").strip()
    if not base_url:
        return empty

    try:
        token = fetch_auth_token_for_env(env_row)
        payload, api_url = fetch_parent_scan_attributes_from_api(base_url, str(scan_id).strip(), token, skip=0, limit=200)
    except Exception as ex:
        return pd.DataFrame(), f"Live attributes API fetch failed: {ex}", None

    try:
        items, total_hint = extract_scan_attributes_payload(payload if isinstance(payload, dict) else {})
    except Exception:
        items, total_hint = [], None
    if not isinstance(items, list) or not items:
        return pd.DataFrame(), "Live attributes API returned no rows.", total_hint

    rows_out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("_id") or item.get("id") or item.get("name")
        if raw_id is None:
            continue
        label = str(raw_id).replace("classifier.", "").strip()
        if not label:
            continue
        try:
            n_new = int(item.get("new") or 0)
        except Exception:
            n_new = 0
        try:
            n_rem = int(item.get("removed") or 0)
        except Exception:
            n_rem = 0
        rows_out.append(
            {
                "Classifier / attribute": label,
                "New findings": n_new,
                "Removed": n_rem,
            }
        )
    if not rows_out:
        return pd.DataFrame(), "Live attributes payload parsed but no classifier rows found.", total_hint

    df = pd.DataFrame(rows_out).sort_values(["New findings", "Classifier / attribute"], ascending=[False, True])
    note = f"Source: **live attributes API** · **{len(df)}** row(s)"
    if total_hint is not None:
        note += f" · totalCount={int(total_hint)}"
    note += f" · `{api_url}`"
    return df, note, total_hint


def load_scan_classifier_hits_map(scan_id: str) -> Dict[str, int]:
    """
    Return classifier->new_findings map for a scan.
    Uses DB snapshot first; falls back to a one-shot live attributes API call.
    """
    out: Dict[str, int] = {}
    sid = str(scan_id or "").strip()
    if not sid:
        return out
    try:
        df_hits, _note = load_scan_insight_classifier_hits_df(sid)
    except Exception:
        df_hits = pd.DataFrame()

    if (df_hits is None or df_hits.empty):
        try:
            env_row = get_environment_row_for_scan_id(sid)
            if env_row:
                live_df, _live_note, _live_total = fetch_live_scan_classifier_hits_df(sid, env_row)
                if live_df is not None and not live_df.empty:
                    df_hits = live_df
        except Exception:
            pass

    try:
        if df_hits is None or df_hits.empty:
            return out
        for _r in df_hits.to_dict(orient="records"):
            nm = str(_r.get("Classifier / attribute") or "").strip()
            if not nm:
                continue
            try:
                cnt = int(_r.get("New findings") or 0)
            except Exception:
                cnt = 0
            out[nm] = cnt
    except Exception:
        return {}
    return out


def load_workflow_context_for_scan_id(sid: str) -> Dict[str, Any]:
    """Latest workflow_steps row for a scan (scenario + build context)."""
    if not engine or not str(sid or "").strip():
        return {}
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT ds_kind, file_set_type, is_acl, is_ols, is_structured_ff, build_version, environment_id\n"
                    "FROM workflow_steps\n"
                    "WHERE scan_id = :sid\n"
                    "ORDER BY updated_at DESC, id DESC\n"
                    "LIMIT 1"
                ),
                {"sid": str(sid).strip()},
            ).fetchone()
        if not row:
            return {}
        return {
            "ds_kind": str(row[0] or "").strip(),
            "file_set_type": (str(row[1]).strip() if row[1] is not None else None),
            "is_acl": int(row[2] or 0),
            "is_ols": int(row[3] or 0),
            "is_structured_ff": int(row[4] or 0),
            "build_version": str(row[5] or "").strip(),
            "environment_id": (int(row[6]) if row[6] is not None else None),
        }
    except Exception:
        return {}


def _scan_progress_peak_row_for_report(scan_id: str) -> Dict[str, Any]:
    """Peak / latest-style metrics from scan_progress for reporting."""
    out: Dict[str, Any] = {}
    if not engine or not str(scan_id or "").strip():
        return out
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT "
                    "MAX(CAST(COALESCE(objects_scanned, 0) AS SIGNED)), "
                    "MAX(CAST(COALESCE(total_classifications, 0) AS SIGNED)), "
                    "MAX(CAST(COALESCE(newly_added_classifications, 0) AS SIGNED)), "
                    "MAX(CAST(COALESCE(false_removed_classifications, 0) AS SIGNED)), "
                    "MAX(CAST(COALESCE(objects_with_pi, 0) AS SIGNED)), "
                    "MAX(CAST(COALESCE(completed_parts, 0) AS SIGNED)), "
                    "MAX(CAST(COALESCE(total_parts_scanned, 0) AS SIGNED)), "
                    "MAX(CAST(COALESCE(failed_parts, 0) AS SIGNED)), "
                    "MAX(CAST(COALESCE(objects_failed, 0) AS SIGNED)) "
                    "FROM scan_progress WHERE scan_id = :sid"
                ),
                {"sid": str(scan_id).strip()},
            ).fetchone()
            if not row:
                return out
            out["objects_scanned_max"] = int(row[0] or 0)
            out["total_classifications_max"] = int(row[1] or 0)
            out["newly_added_max"] = int(row[2] or 0)
            out["false_removed_max"] = int(row[3] or 0)
            out["objects_with_pi_max"] = int(row[4] or 0)
            out["completed_parts_max"] = int(row[5] or 0)
            out["total_parts_max"] = int(row[6] or 0)
            out["failed_parts_max"] = int(row[7] or 0)
            out["objects_failed_max"] = int(row[8] or 0)
            r2 = conn.execute(
                text(
                    "SELECT scan_state, captured_at_utc FROM scan_progress "
                    "WHERE scan_id = :sid ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
                ),
                {"sid": str(scan_id).strip()},
            ).fetchone()
            if r2:
                out["latest_scan_state"] = str(r2[0] or "").strip()
                out["latest_captured_at_utc"] = str(r2[1] or "").strip()
    except Exception:
        return {}
    return out


def find_peer_scans_same_build(cur_scan_id: str, build_version: str, *, limit: int = 12) -> List[Dict[str, Any]]:
    """
    Other scans on the same BigID build (workflow_steps), e.g. S3 vs SMB small-files runs.
    """
    if not engine:
        return []
    bv = str(build_version or "").strip()
    cur = str(cur_scan_id or "").strip()
    if not bv or not cur:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT scan_id, ds_kind, file_set_type, build_version, MAX(updated_at) AS mx "
                    "FROM workflow_steps "
                    "WHERE scan_id IS NOT NULL AND TRIM(scan_id) <> '' "
                    "  AND scan_id <> :cur "
                    "  AND build_version = :bv "
                    "GROUP BY scan_id, ds_kind, file_set_type, build_version "
                    "ORDER BY mx DESC "
                    "LIMIT :lim"
                ),
                {"cur": cur, "bv": bv, "lim": int(limit)},
            ).fetchall()
        peers: List[Dict[str, Any]] = []
        for r in rows or []:
            peers.append(
                {
                    "scan_id": str(r[0] or "").strip(),
                    "ds_kind": str(r[1] or "").strip(),
                    "file_set_type": (str(r[2]).strip() if r[2] is not None else ""),
                    "build_version": str(r[3] or "").strip(),
                }
            )
        return peers
    except Exception:
        return []


def build_scan_progress_manager_reports(
    *,
    scan_id: str,
    display_name: str,
    db_started_at: str,
    db_completed_at: str,
    cur_state: str,
    cur_objects: int,
    cur_parts_done: int,
    cur_parts_total: int,
    cur_failed_parts: int,
    cur_objects_failed: int,
    pct_parts: Optional[float],
    files_per_min: Optional[float],
    expected_total_files: Optional[Any],
    lookup_key: str,
    wf_ctx: Dict[str, Any],
    cls_vs_prev: Dict[str, Any],
    cls_new_removed_vs_prev: Dict[str, Any],
    classifier_hits_df: Any,
    classifier_hits_note: str,
    narrative: str,
    flags: List[str],
    completion_lag_summary: str,
    regression_warnings: List[str],
    observations: List[str],
) -> Tuple[str, str]:
    """Return (markdown_report, csv_report) for manager sharing."""
    import csv
    from io import StringIO

    sid = str(scan_id or "").strip()
    now_s = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    title_name = (display_name or "").strip() or sid

    def _fmt_num(v: Any) -> str:
        try:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return ""
            return f"{int(v):,}"
        except Exception:
            return str(v or "")

    lines: List[str] = []
    lines.append(f"# Scan status report — {title_name}")
    lines.append("")
    lines.append(f"- **Generated:** {now_s}")
    lines.append(f"- **Scan ID:** `{sid}`")
    lines.append(f"- **DB started:** `{db_started_at or '—'}`")
    lines.append(f"- **DB completed:** `{db_completed_at or '—'}`")
    lines.append(f"- **Latest state (snapshots):** `{cur_state or '—'}`")
    lines.append("")

    lines.append("## Scenario context (workflow)")
    lines.append("")
    if wf_ctx:
        lines.append(f"- **ds_kind:** `{wf_ctx.get('ds_kind') or '—'}`")
        lines.append(f"- **file_set_type:** `{wf_ctx.get('file_set_type') or '—'}`")
        lines.append(f"- **build_version:** `{wf_ctx.get('build_version') or '—'}`")
        lines.append(
            f"- **flags:** acl={wf_ctx.get('is_acl', 0)}, ols={wf_ctx.get('is_ols', 0)}, structured_ff={wf_ctx.get('is_structured_ff', 0)}"
        )
        if wf_ctx.get("environment_id") is not None:
            lines.append(f"- **environment_id:** `{wf_ctx.get('environment_id')}`")
    else:
        lines.append("_No workflow_steps row for this scan (scenario context unknown)._")
    lines.append("")

    lines.append("## Progress & throughput")
    lines.append("")
    lines.append(f"- **Objects scanned (latest):** {_fmt_num(cur_objects)}")
    if expected_total_files is not None and str(expected_total_files).strip() != "":
        try:
            exp = int(expected_total_files)
            short = max(0, exp - int(cur_objects or 0))
            lk = str(lookup_key or "").strip()
            lines.append(f"- **Expected objects:** {_fmt_num(exp)}" + (f" (`{lk}`)" if lk else ""))
            lines.append(f"- **Object shortfall:** {_fmt_num(short)}")
        except Exception:
            pass
    if pct_parts is not None:
        lines.append(
            f"- **Parts:** {_fmt_num(cur_parts_done)} / {_fmt_num(cur_parts_total)} ({pct_parts:.1f}%)"
        )
    else:
        lines.append(f"- **Parts:** {_fmt_num(cur_parts_done)} / {_fmt_num(cur_parts_total)}")
    lines.append(f"- **Failed parts:** {_fmt_num(cur_failed_parts)}")
    lines.append(f"- **Objects failed:** {_fmt_num(cur_objects_failed)}")
    if files_per_min is not None and files_per_min >= 0:
        lines.append(f"- **Recent throughput (approx):** {files_per_min:,.1f} objects/min")
    lines.append("")

    lines.append("## Classification vs previous same scenario")
    lines.append("")
    if cls_vs_prev:
        pv = cls_vs_prev.get("prev_sid", "")
        lines.append(
            f"- **Total classifications:** current **{_fmt_num(cls_vs_prev.get('cur'))}** vs previous **{_fmt_num(cls_vs_prev.get('prev'))}** "
            f"(Δ {int(cls_vs_prev.get('delta') or 0):+,}, {float(cls_vs_prev.get('pct') or 0):.1f}% vs previous)"
        )
        lines.append(f"- **Previous scan:** `{pv}` (build `{cls_vs_prev.get('prev_build') or 'unknown'}`)")
        miss = list(cls_vs_prev.get("missing") or [])
        new_c = list(cls_vs_prev.get("new") or [])
        if miss:
            lines.append(f"- **Missing in current (were in previous):** {', '.join(miss)}")
        if new_c:
            lines.append(f"- **Only in current:** {', '.join(new_c)}")
        top = cls_vs_prev.get("top_delta_rows") or []
        if top:
            lines.append("- **Top classifier deltas (prev → current):**")
            for t in top:
                lines.append(f"  - {t}")
    else:
        lines.append("_No flagged total-classifications delta vs previous (threshold not met or no previous scan)._")
    lines.append("")

    lines.append("## Newly added / removed classifications vs previous")
    lines.append("")
    if cls_new_removed_vs_prev:
        cn = int(cls_new_removed_vs_prev.get("cur_new", 0) or 0)
        pn = int(cls_new_removed_vs_prev.get("prev_new", 0) or 0)
        dn = int(cls_new_removed_vs_prev.get("d_new", 0) or 0)
        cr = int(cls_new_removed_vs_prev.get("cur_removed", 0) or 0)
        pr = int(cls_new_removed_vs_prev.get("prev_removed", 0) or 0)
        dr = int(cls_new_removed_vs_prev.get("d_removed", 0) or 0)
        lines.append(
            f"- **Newly added:** {cn:,} vs {pn:,} (Δ {dn:+,}) — previous scan `{cls_new_removed_vs_prev.get('prev_sid', '')}`"
        )
        lines.append(f"- **Removed (false removed):** {cr:,} vs {pr:,} (Δ {dr:+,})")
    else:
        lines.append("_Not available (no previous same-scenario scan or metrics missing)._")
    lines.append("")

    lines.append("## Classifier / attribute hits (snapshot)")
    lines.append("")
    if classifier_hits_note:
        lines.append(classifier_hits_note.replace("**", ""))
        lines.append("")
    try:
        dfh = classifier_hits_df
        if dfh is not None and not getattr(dfh, "empty", True):
            show = dfh.copy()
            if "New findings" in show.columns and "Removed" in show.columns:
                show = show[(pd.to_numeric(show["New findings"], errors="coerce").fillna(0) > 0) | (pd.to_numeric(show["Removed"], errors="coerce").fillna(0) > 0)]
            show = show.head(25)
            lines.append("| Classifier | New | Removed |")
            lines.append("| --- | ---: | ---: |")
            for rec in show.to_dict(orient="records"):
                nm = str(rec.get("Classifier / attribute") or "").replace("|", "\\|")
                nw = _fmt_num(rec.get("New findings"))
                rm = _fmt_num(rec.get("Removed"))
                lines.append(f"| {nm} | {nw} | {rm} |")
        else:
            lines.append("_No classifier hit rows in DB; use Sync now on Scan Progress to refresh attributes._")
    except Exception:
        lines.append("_Could not render classifier hit table._")
    lines.append("")

    lines.append("## Same-build peer scans (e.g. S3 vs SMB)")
    lines.append("")
    bv = str((wf_ctx or {}).get("build_version") or "").strip()
    peers = find_peer_scans_same_build(sid, bv, limit=12) if bv else []
    peer_rows_csv: List[List[Any]] = []
    if bv:
        self_agg = _scan_progress_peak_row_for_report(sid)
        lines.append("### This scan (reference)")
        lines.append("")
        lines.append("| Scan ID | ds_kind | file_set_type | objects (max) | classifications (max) | latest state |")
        lines.append("| --- | --- | --- | ---: | ---: | --- |")
        lines.append(
            f"| `{sid}` | {(wf_ctx or {}).get('ds_kind') or '—'} | {(wf_ctx or {}).get('file_set_type') or '—'} | "
            f"{_fmt_num(self_agg.get('objects_scanned_max'))} | {_fmt_num(self_agg.get('total_classifications_max'))} | "
            f"`{self_agg.get('latest_scan_state') or cur_state or '—'}` |"
        )
        lines.append("")
    if peers:
        lines.append("### Other scans (same `build_version`)")
        lines.append("")
        lines.append("| Scan ID | ds_kind | file_set_type | objects (max) | classifications (max) | latest state |")
        lines.append("| --- | --- | --- | ---: | ---: | --- |")
        for p in peers:
            pk = str(p.get("scan_id") or "").strip()
            if not pk:
                continue
            agg = _scan_progress_peak_row_for_report(pk)
            lines.append(
                f"| `{pk}` | {p.get('ds_kind') or '—'} | {p.get('file_set_type') or '—'} | "
                f"{_fmt_num(agg.get('objects_scanned_max'))} | {_fmt_num(agg.get('total_classifications_max'))} | "
                f"`{agg.get('latest_scan_state') or '—'}` |"
            )
            peer_rows_csv.append(
                [
                    pk,
                    p.get("ds_kind"),
                    p.get("file_set_type"),
                    agg.get("objects_scanned_max"),
                    agg.get("total_classifications_max"),
                    agg.get("newly_added_max"),
                    agg.get("false_removed_max"),
                    agg.get("latest_scan_state"),
                ]
            )
    elif bv:
        lines.append("_No other scans found for this `build_version` in workflow_steps (only this scan listed above)._")
    else:
        lines.append("_Build version unknown — cannot list peer runs for cross-scenario comparison._")
    lines.append("")

    lines.append("## Risks & notes")
    lines.append("")
    lines.append(narrative or "_—_")
    lines.append("")
    if completion_lag_summary:
        lines.append(f"- **Completion lag:** {completion_lag_summary}")
    if flags:
        lines.append("- **Flags:**")
        for fl in flags:
            lines.append(f"  - {fl}")
    if regression_warnings:
        lines.append("- **Regression warnings:**")
        for rw in regression_warnings:
            lines.append(f"  - {rw}")
    if observations:
        lines.append("- **Other observations:**")
        for ob in observations[:40]:
            lines.append(f"  - {ob}")
    lines.append("")
    lines.append("---")
    lines.append("_End of report_")

    md = "\n".join(lines)

    buf = StringIO()
    cw = csv.writer(buf)
    cur_agg = _scan_progress_peak_row_for_report(sid)
    cw.writerow(["PRIMARY_SCAN"])
    cw.writerow(
        [
            "scan_id",
            "display_name",
            "ds_kind",
            "file_set_type",
            "build_version",
            "latest_state",
            "objects_scanned_latest",
            "expected_objects",
            "object_shortfall",
            "parts_done",
            "parts_total",
            "failed_parts",
            "objects_failed",
            "throughput_objects_per_min",
            "total_classifications_max",
            "newly_added_max",
            "false_removed_max",
            "objects_with_pi_max",
            "prev_scan_id",
            "prev_total_classifications",
            "delta_total_classifications",
            "missing_classifiers",
            "only_in_current_classifiers",
            "vs_prev_newly_added_cur",
            "vs_prev_newly_added_prev",
            "vs_prev_newly_added_delta",
            "vs_prev_removed_cur",
            "vs_prev_removed_prev",
            "vs_prev_removed_delta",
            "vs_prev_new_removed_prev_scan_id",
        ]
    )
    try:
        exp_o = int(expected_total_files) if expected_total_files is not None and str(expected_total_files).strip() != "" else ""
        shf = max(0, int(exp_o) - int(cur_objects or 0)) if exp_o != "" else ""
    except Exception:
        exp_o, shf = "", ""
    _nr = cls_new_removed_vs_prev or {}
    cw.writerow(
        [
            sid,
            title_name,
            (wf_ctx or {}).get("ds_kind"),
            (wf_ctx or {}).get("file_set_type"),
            (wf_ctx or {}).get("build_version"),
            cur_state,
            cur_objects,
            exp_o,
            shf,
            cur_parts_done,
            cur_parts_total,
            cur_failed_parts,
            cur_objects_failed,
            f"{files_per_min:.4f}" if files_per_min is not None else "",
            cur_agg.get("total_classifications_max"),
            cur_agg.get("newly_added_max"),
            cur_agg.get("false_removed_max"),
            cur_agg.get("objects_with_pi_max"),
            (cls_vs_prev or {}).get("prev_sid"),
            (cls_vs_prev or {}).get("prev"),
            (cls_vs_prev or {}).get("delta"),
            ";".join(cls_vs_prev.get("missing") or []) if cls_vs_prev else "",
            ";".join(cls_vs_prev.get("new") or []) if cls_vs_prev else "",
            _nr.get("cur_new"),
            _nr.get("prev_new"),
            _nr.get("d_new"),
            _nr.get("cur_removed"),
            _nr.get("prev_removed"),
            _nr.get("d_removed"),
            _nr.get("prev_sid"),
        ]
    )
    cw.writerow([])
    cw.writerow(["SAME_BUILD_PEER_SCANS"])
    cw.writerow(
        [
            "scan_id",
            "ds_kind",
            "file_set_type",
            "objects_scanned_max",
            "total_classifications_max",
            "newly_added_max",
            "false_removed_max",
            "latest_scan_state",
        ]
    )
    for pr in peer_rows_csv:
        cw.writerow(pr)
    csv_text = buf.getvalue()

    return md, csv_text


def _enrich_scan_insight_row_from_payload(row: Dict[str, Any], raw_payload: Any) -> Dict[str, Any]:
    """When denormalized scan_insights columns are empty, fill from stored insight JSON (tracker/sync)."""
    out = dict(row)
    if not raw_payload:
        return out
    try:
        if isinstance(raw_payload, (bytes, bytearray)):
            raw_payload = raw_payload.decode("utf-8", "ignore")
        if isinstance(raw_payload, str):
            s = raw_payload.strip()
            if not s:
                return out
            ip_obj = json.loads(s)
        elif isinstance(raw_payload, dict):
            ip_obj = raw_payload
        else:
            return out
    except Exception:
        return out
    try:
        mx = extract_scan_insight_metrics(ip_obj)
    except Exception:
        return out

    def _blank_txt(v: Any) -> bool:
        return v is None or str(v).strip() == ""

    if _blank_txt(out.get("classifier_attribute_names")):
        out["classifier_attribute_names"] = str(mx.get("classifier_attribute_names") or "").strip()
    if _blank_txt(out.get("data_source_names")):
        out["data_source_names"] = str(mx.get("data_source_names") or "").strip()
    if out.get("distinct_classification_attributes") is None:
        dca = mx.get("distinct_classification_attributes")
        if dca is not None and str(dca).strip() != "":
            try:
                out["distinct_classification_attributes"] = int(dca)
            except Exception:
                pass
    for kdb, kmx in (
        ("objects_with_pi", "objects_with_pi"),
        ("objects_scanned", "objects_scanned"),
        ("objects_enumerated", "objects_enumerated"),
        ("total_classification", "total_classification"),
    ):
        if out.get(kdb) is None:
            v = mx.get(kmx)
            if v is not None and str(v).strip() != "":
                try:
                    out[kdb] = int(float(v))
                except Exception:
                    pass
    return out


def _merge_scan_progress_into_insight_row(row: Dict[str, Any], prog: Dict[str, Any]) -> Dict[str, Any]:
    """Fill missing counters from latest scan_progress (tracker snapshots)."""
    out = dict(row)

    def _to_int(v: Any) -> Optional[int]:
        if v is None or (isinstance(v, str) and str(v).strip() == ""):
            return None
        try:
            return int(v)
        except Exception:
            return None

    if out.get("objects_scanned") is None:
        v = _to_int(prog.get("objects_scanned"))
        if v is not None:
            out["objects_scanned"] = v
    if out.get("objects_with_pi") is None:
        v = _to_int(prog.get("objects_with_pi"))
        if v is not None:
            out["objects_with_pi"] = v
    if out.get("total_classification") is None:
        v = _to_int(prog.get("total_classifications"))
        if v is not None:
            out["total_classification"] = v
    if out.get("new_attributes_count") is None:
        v = _to_int(prog.get("newly_added_classifications"))
        if v is not None:
            out["new_attributes_count"] = v
    if out.get("removed_attributes_count") is None:
        v = _to_int(prog.get("false_removed_classifications"))
        if v is not None:
            out["removed_attributes_count"] = v
    return out


@st.cache_data(ttl=300)
def load_scan_insight_map_for_scan_ids(scan_ids):
    """
    Batch-load scan insight metrics for many scan ids in one query.
    """
    result = {}
    cleaned_ids = []
    seen = set()
    for scan_id in (scan_ids or []):
        sid = str(scan_id or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        cleaned_ids.append(sid)
    if not engine or not cleaned_ids:
        return result
    try:
        from sqlalchemy import bindparam
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
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
                    insight_payload
                FROM scan_insights
                WHERE scan_id IN :ids
            """).bindparams(bindparam("ids", expanding=True)), {"ids": cleaned_ids}).fetchall()

            # Prefer attribute-delta sums when available (this matches Scan Progress page logic).
            # This avoids showing 0/blank in Scan Status when scan_insights.*_count isn't populated yet.
            delta_rows = conn.execute(text("""
                SELECT
                    scan_id,
                    COALESCE(SUM(CASE
                        WHEN new_count_value IS NOT NULL THEN new_count_value
                        WHEN is_new = TRUE THEN 1 ELSE 0 END), 0) AS new_count,
                    COALESCE(SUM(CASE
                        WHEN removed_count_value IS NOT NULL THEN removed_count_value
                        WHEN is_removed = TRUE THEN 1 ELSE 0 END), 0) AS removed_count
                FROM scan_insight_attribute_delta_map
                WHERE scan_id IN :ids
                GROUP BY scan_id
            """).bindparams(bindparam("ids", expanding=True)), {"ids": cleaned_ids}).fetchall()
            delta_map = {}
            for dr in (delta_rows or []):
                sid2 = str(dr[0] or "").strip()
                if not sid2:
                    continue
                try:
                    delta_map[sid2] = (int(dr[1] or 0), int(dr[2] or 0))
                except Exception:
                    delta_map[sid2] = (0, 0)

            # Final fallback: latest scan_progress snapshot counters (newly added / false removed).
            # Some envs populate scan_progress sooner than attribute-delta tables.
            prog_rows = conn.execute(text("""
                SELECT
                    scan_id,
                    newly_added_classifications,
                    false_removed_classifications
                FROM scan_progress
                WHERE scan_id IN :ids
                ORDER BY COALESCE(captured_at_utc, timestamp) DESC, id DESC
            """).bindparams(bindparam("ids", expanding=True)), {"ids": cleaned_ids}).fetchall()
            progress_map = {}
            for pr in (prog_rows or []):
                sid3 = str(pr[0] or "").strip()
                if not sid3 or sid3 in progress_map:
                    continue
                try:
                    progress_map[sid3] = (int(pr[1] or 0), int(pr[2] or 0))
                except Exception:
                    progress_map[sid3] = (0, 0)

            ds_by_scan: Dict[str, List[str]] = {}
            attr_by_scan: Dict[str, List[str]] = {}
            try:
                ds_rows = conn.execute(
                    text("""
                        SELECT m.scan_id, d.name
                        FROM scan_insight_data_source_map m
                        JOIN scan_data_sources d ON d.id = m.data_source_id
                        WHERE m.scan_id IN :ids
                        ORDER BY m.scan_id ASC, d.name ASC
                    """).bindparams(bindparam("ids", expanding=True)),
                    {"ids": cleaned_ids},
                ).fetchall()
                for dr in ds_rows or []:
                    sid_ds = str(dr[0] or "").strip()
                    nm_ds = str(dr[1] or "").strip()
                    if sid_ds and nm_ds:
                        ds_by_scan.setdefault(sid_ds, []).append(nm_ds)
            except Exception:
                pass
            try:
                attr_rows = conn.execute(
                    text("""
                        SELECT m.scan_id, a.name
                        FROM scan_insight_attribute_delta_map m
                        JOIN scan_classifier_attributes a ON a.id = m.attribute_id
                        WHERE m.scan_id IN :ids
                        ORDER BY m.scan_id ASC, a.name ASC
                    """).bindparams(bindparam("ids", expanding=True)),
                    {"ids": cleaned_ids},
                ).fetchall()
                for ar in attr_rows or []:
                    sid_at = str(ar[0] or "").strip()
                    nm_at = str(ar[1] or "").strip()
                    if sid_at and nm_at:
                        attr_by_scan.setdefault(sid_at, []).append(nm_at)
            except Exception:
                pass

        def _backfill_insight_maps(sid_key: str, row: Dict[str, Any]) -> Dict[str, Any]:
            out = dict(row)
            if not str(out.get("data_source_names") or "").strip():
                names = ds_by_scan.get(sid_key) or []
                if names:
                    out["data_source_names"] = ", ".join(sorted(set(names)))
            if not str(out.get("classifier_attribute_names") or "").strip():
                anames = attr_by_scan.get(sid_key) or []
                if anames:
                    out["classifier_attribute_names"] = ", ".join(sorted(set(anames)))
            if out.get("distinct_classification_attributes") is None:
                anames2 = attr_by_scan.get(sid_key) or []
                if anames2:
                    try:
                        out["distinct_classification_attributes"] = len(set(anames2))
                    except Exception:
                        pass
            return out

        for r in rows:
            sid = str(r[0] or "").strip()
            if not sid:
                continue
            d_new, d_removed = (delta_map.get(sid) or (0, 0))
            p_new, p_removed = (progress_map.get(sid) or (0, 0))
            # scan_insights may store 0/NULL while progress has non-zero (or vice versa).
            si_new = r[8]
            si_removed = r[9]
            try:
                si_new_i = int(si_new) if si_new is not None and str(si_new).strip() != "" else None
            except Exception:
                si_new_i = None
            try:
                si_removed_i = int(si_removed) if si_removed is not None and str(si_removed).strip() != "" else None
            except Exception:
                si_removed_i = None

            def _pick_count(delta_val: int, si_val: Optional[int], prog_val: int):
                if int(delta_val or 0) > 0:
                    return int(delta_val)
                if si_val is not None and int(si_val) > 0:
                    return int(si_val)
                if int(prog_val or 0) > 0:
                    return int(prog_val)
                # If everything is zero and we have no explicit value, return None (keeps Arrow types stable).
                if si_val is not None:
                    return int(si_val)
                return None

            result[sid] = {
                "distinct_classification_attributes": int(r[1]) if r[1] is not None and str(r[1]).strip() != "" else None,
                "classifier_attribute_names": str(r[2] or "").strip(),
                "data_source_names": str(r[3] or "").strip(),
                "objects_with_pi": int(r[4]) if r[4] is not None and str(r[4]).strip() != "" else None,
                "objects_scanned": int(r[5]) if r[5] is not None and str(r[5]).strip() != "" else None,
                "objects_enumerated": int(r[6]) if r[6] is not None and str(r[6]).strip() != "" else None,
                "total_classification": int(r[7]) if r[7] is not None and str(r[7]).strip() != "" else None,
                "new_attributes_count": _pick_count(d_new, si_new_i, p_new),
                "removed_attributes_count": _pick_count(d_removed, si_removed_i, p_removed),
                "total_size_gb": float(r[10]) if r[10] is not None and str(r[10]).strip() != "" else None,
            }
            result[sid] = _enrich_scan_insight_row_from_payload(result[sid], r[11] if len(r) > 11 else None)
            result[sid] = _backfill_insight_maps(sid, result[sid])

        try:
            prog_full = load_latest_scan_progress_map_for_scan_ids(cleaned_ids)
        except Exception:
            prog_full = {}
        for sid in cleaned_ids:
            if sid not in prog_full:
                continue
            if sid not in result:
                result[sid] = {
                    "distinct_classification_attributes": None,
                    "classifier_attribute_names": "",
                    "data_source_names": "",
                    "objects_with_pi": None,
                    "objects_scanned": None,
                    "objects_enumerated": None,
                    "total_classification": None,
                    "new_attributes_count": None,
                    "removed_attributes_count": None,
                    "total_size_gb": None,
                }
                d_new2, d_removed2 = (delta_map.get(sid) or (0, 0))
                p_new2, p_removed2 = (progress_map.get(sid) or (0, 0))

                def _pick_count2(delta_val: int, prog_val: int):
                    if int(delta_val or 0) > 0:
                        return int(delta_val)
                    if int(prog_val or 0) > 0:
                        return int(prog_val)
                    return None

                result[sid]["new_attributes_count"] = _pick_count2(d_new2, p_new2)
                result[sid]["removed_attributes_count"] = _pick_count2(d_removed2, p_removed2)
            result[sid] = _merge_scan_progress_into_insight_row(result[sid], prog_full[sid])
            result[sid] = _backfill_insight_maps(sid, result[sid])
    except Exception:
        return {}
    return result


def load_scan_detail_completeness_map(scan_ids):
    """
    Return {scan_id: bool} indicating whether both insight and attributes are persisted.
    """
    completeness = {}
    cleaned_ids = []
    seen = set()
    for scan_id in (scan_ids or []):
        sid = str(scan_id or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        cleaned_ids.append(sid)
        completeness[sid] = False

    if not engine or not cleaned_ids:
        return completeness

    try:
        from sqlalchemy import bindparam
        with engine.connect() as conn:
            insight_rows = conn.execute(text("""
                SELECT
                    scan_id,
                    distinct_classification_attributes,
                    objects_with_pi,
                    objects_scanned,
                    objects_enumerated,
                    total_classification,
                    new_attributes_count,
                    removed_attributes_count,
                    insight_payload,
                    attributes_payload
                FROM scan_insights
                WHERE scan_id IN :ids
            """).bindparams(bindparam("ids", expanding=True)), {"ids": cleaned_ids}).fetchall()

            attr_rows = conn.execute(text("""
                SELECT
                    scan_id,
                    COUNT(*) AS cnt,
                    SUM(CASE
                        WHEN COALESCE(new_count_value, 0) > 0
                          OR COALESCE(removed_count_value, 0) > 0
                          OR COALESCE(finding_count, 0) > 0
                          OR COALESCE(attribute_value, '') <> ''
                        THEN 1 ELSE 0 END) AS signal_cnt
                FROM scan_insight_attribute_delta_map
                WHERE scan_id IN :ids
                GROUP BY scan_id
            """).bindparams(bindparam("ids", expanding=True)), {"ids": cleaned_ids}).fetchall()
            attr_count_map = {str(r[0]): int(r[1] or 0) for r in attr_rows if r and r[0]}
            attr_signal_map = {str(r[0]): int(r[2] or 0) for r in attr_rows if r and r[0]}

            def has_real_attributes_payload(raw_payload):
                txt = str(raw_payload or "").strip()
                if not txt:
                    return False
                try:
                    obj = json.loads(txt)
                except Exception:
                    return False
                items, total_count = extract_scan_attributes_payload(obj if isinstance(obj, dict) else {})
                if items:
                    return True
                try:
                    return int(total_count or 0) > 0
                except Exception:
                    return False

            def has_real_insight_payload(raw_payload):
                txt = str(raw_payload or "").strip()
                if not txt:
                    return False
                try:
                    obj = json.loads(txt)
                except Exception:
                    return False
                if not isinstance(obj, dict):
                    return False
                # Any non-empty object is considered real insight payload.
                return bool(obj)

            for r in insight_rows:
                sid = str(r[0] or "").strip()
                if not sid:
                    continue
                metric_values = [r[1], r[2], r[3], r[4], r[5], r[6], r[7]]
                has_metric_data = any(v is not None for v in metric_values)
                has_insight_payload = has_real_insight_payload(r[8])
                has_attributes_payload = has_real_attributes_payload(r[9])
                has_attr_rows = int(attr_count_map.get(sid, 0)) > 0
                has_attr_signal = int(attr_signal_map.get(sid, 0)) > 0
                has_insight = has_metric_data or has_insight_payload
                has_attributes = has_attributes_payload or has_attr_signal
                completeness[sid] = bool(has_insight and has_attributes)
    except Exception:
        return completeness

    return completeness


def upsert_scan_insight_record(scan_id, metrics, insight_payload=None, attributes_payload=None):
    if not engine or not scan_id:
        return

    def to_int_or_none(v):
        if v is None or v == "":
            return None
        try:
            return int(v)
        except Exception:
            return None

    try:
        with engine.connect() as conn:
            conn.execute(text("""
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
                    attributes_payload
                ) VALUES (
                    :sid, :dca, :can, :dsn, :owp, :osc, :oen, :tcl, :nac, :rac, :tgb, :ip, :ap
                )
                ON DUPLICATE KEY UPDATE
                    distinct_classification_attributes = VALUES(distinct_classification_attributes),
                    classifier_attribute_names = VALUES(classifier_attribute_names),
                    data_source_names = VALUES(data_source_names),
                    objects_with_pi = VALUES(objects_with_pi),
                    objects_scanned = VALUES(objects_scanned),
                    objects_enumerated = VALUES(objects_enumerated),
                    total_classification = VALUES(total_classification),
                    new_attributes_count = VALUES(new_attributes_count),
                    removed_attributes_count = VALUES(removed_attributes_count),
                    total_size_gb = VALUES(total_size_gb),
                    insight_payload = VALUES(insight_payload),
                    attributes_payload = VALUES(attributes_payload),
                    updated_at = CURRENT_TIMESTAMP
            """), {
                "sid": str(scan_id),
                "dca": to_int_or_none(metrics.get("distinct_classification_attributes")),
                "can": str(metrics.get("classifier_attribute_names") or "") or None,
                "dsn": str(metrics.get("data_source_names") or "") or None,
                "owp": to_int_or_none(metrics.get("objects_with_pi")),
                "osc": to_int_or_none(metrics.get("objects_scanned")),
                "oen": to_int_or_none(metrics.get("objects_enumerated")),
                "tcl": to_int_or_none(metrics.get("total_classification")),
                "nac": to_int_or_none(metrics.get("new_attributes_count")),
                "rac": to_int_or_none(metrics.get("removed_attributes_count")),
                "ip": json.dumps(insight_payload, ensure_ascii=False) if insight_payload is not None else None,
                "ap": json.dumps(attributes_payload, ensure_ascii=False) if attributes_payload is not None else None,
            })

            # Normalize data sources into dictionary + map tables.
            ds_text = str(metrics.get("data_source_names") or "").strip()
            data_source_names = sorted({x.strip() for x in ds_text.split(",") if x and x.strip()})
            conn.execute(text("DELETE FROM scan_insight_data_source_map WHERE scan_id = :sid"), {"sid": str(scan_id)})
            for name in data_source_names:
                conn.execute(text("INSERT IGNORE INTO scan_data_sources (name) VALUES (:n)"), {"n": name})
                ds_row = conn.execute(text("SELECT id FROM scan_data_sources WHERE name = :n LIMIT 1"), {"n": name}).fetchone()
                if ds_row:
                    conn.execute(text("""
                        INSERT IGNORE INTO scan_insight_data_source_map (scan_id, data_source_id)
                        VALUES (:sid, :did)
                    """), {"sid": str(scan_id), "did": int(ds_row[0])})

            # Normalize classifier attributes + delta flags into dictionary + map tables.
            attr_map = {}

            attrs_payload = attributes_payload if isinstance(attributes_payload, dict) else {}
            attr_items, _ = extract_scan_attributes_payload(attrs_payload)

            def try_int(val):
                try:
                    if val is None or val == "":
                        return None
                    return int(val)
                except Exception:
                    return None

            for item in attr_items:
                if not isinstance(item, dict):
                    continue
                attr_name = str(
                    item.get("name")
                    or item.get("_id")
                    or item.get("attributeName")
                    or item.get("classifierAttributeName")
                    or item.get("classifier_name")
                    or item.get("title")
                    or ""
                ).strip()
                if not attr_name:
                    continue
                current = attr_map.get(attr_name, {
                    "is_new": False,
                    "is_removed": False,
                    "finding_count": None,
                    "attribute_value": "",
                    "new_count_value": 0,
                    "removed_count_value": 0,
                })
                raw_new = item.get("new")
                raw_removed = item.get("removed")
                is_new = bool(raw_new is True)
                is_removed = bool(raw_removed is True)
                new_cnt_val = try_int(raw_new)
                removed_cnt_val = try_int(raw_removed)
                if new_cnt_val is not None and new_cnt_val > 0:
                    is_new = True
                if removed_cnt_val is not None and removed_cnt_val > 0:
                    is_removed = True
                finding_count = (
                    try_int(item.get("findingCount"))
                    or try_int(item.get("findings"))
                    or try_int(item.get("count"))
                    or try_int(item.get("total"))
                    or try_int(item.get("totalCount"))
                    or try_int(item.get("objectsWithPi"))
                    or try_int(item.get("objectsCount"))
                )
                attribute_value = str(
                    item.get("value")
                    or item.get("attributeValue")
                    or item.get("deltaValue")
                    or item.get("changeValue")
                    or item.get("displayValue")
                    or ""
                ).strip()
                change_type = str(
                    item.get("changeType")
                    or item.get("change")
                    or item.get("status")
                    or item.get("state")
                    or ""
                ).strip().lower()
                if change_type in {"new", "added", "newlyadded"}:
                    is_new = True
                if change_type in {"removed", "deleted"}:
                    is_removed = True
                current["is_new"] = bool(current.get("is_new") or is_new)
                current["is_removed"] = bool(current.get("is_removed") or is_removed)
                if new_cnt_val is not None:
                    current["new_count_value"] = max(int(current.get("new_count_value") or 0), int(new_cnt_val))
                elif is_new and int(current.get("new_count_value") or 0) == 0:
                    current["new_count_value"] = 1
                if removed_cnt_val is not None:
                    current["removed_count_value"] = max(int(current.get("removed_count_value") or 0), int(removed_cnt_val))
                elif is_removed and int(current.get("removed_count_value") or 0) == 0:
                    current["removed_count_value"] = 1
                if finding_count is not None:
                    prev_fc = current.get("finding_count")
                    current["finding_count"] = finding_count if prev_fc is None else max(int(prev_fc), int(finding_count))
                if attribute_value:
                    current["attribute_value"] = attribute_value
                if not attribute_value:
                    if int(current.get("new_count_value") or 0) > 0 and int(current.get("removed_count_value") or 0) > 0:
                        current["attribute_value"] = "New, Removed"
                    elif int(current.get("new_count_value") or 0) > 0:
                        current["attribute_value"] = "New"
                    elif int(current.get("removed_count_value") or 0) > 0:
                        current["attribute_value"] = "Removed"
                if current.get("finding_count") is None:
                    current["finding_count"] = int(current.get("new_count_value") or 0) + int(current.get("removed_count_value") or 0)
                attr_map[attr_name] = current

            conn.execute(text("DELETE FROM scan_insight_attribute_delta_map WHERE scan_id = :sid"), {"sid": str(scan_id)})
            for attr_name, flags in attr_map.items():
                conn.execute(text("INSERT IGNORE INTO scan_classifier_attributes (name) VALUES (:n)"), {"n": attr_name})
                attr_row = conn.execute(text("SELECT id FROM scan_classifier_attributes WHERE name = :n LIMIT 1"), {"n": attr_name}).fetchone()
                if attr_row:
                    conn.execute(text("""
                        INSERT INTO scan_insight_attribute_delta_map
                        (scan_id, attribute_id, is_new, is_removed, finding_count, attribute_value, new_count_value, removed_count_value)
                        VALUES (:sid, :aid, :is_new, :is_removed, :fc, :av, :ncv, :rcv)
                        ON DUPLICATE KEY UPDATE
                            is_new = VALUES(is_new),
                            is_removed = VALUES(is_removed),
                            finding_count = VALUES(finding_count),
                            attribute_value = VALUES(attribute_value),
                            new_count_value = VALUES(new_count_value),
                            removed_count_value = VALUES(removed_count_value)
                    """), {
                        "sid": str(scan_id),
                        "aid": int(attr_row[0]),
                        "is_new": bool(flags.get("is_new")),
                        "is_removed": bool(flags.get("is_removed")),
                        "fc": flags.get("finding_count"),
                        "av": str(flags.get("attribute_value") or "") or None,
                        "ncv": int(flags.get("new_count_value") or 0),
                        "rcv": int(flags.get("removed_count_value") or 0),
                    })
            conn.commit()
    except Exception:
        pass


def infer_add_benchmark_defaults_from_scan_name(scan_name):
    name = str(scan_name or "")
    upper_name = name.upper()

    has_small_file = "SMALL FILES" in upper_name or "SMALL FILE" in upper_name
    has_large_file = "LARGE FILES" in upper_name or "LARGE FILE" in upper_name
    has_csv_file = "CSV FILES" in upper_name or "CSV FILE" in upper_name
    has_with_aci = ("WITH ACI" in upper_name or "ACL" in upper_name) and "NO ACL" not in upper_name
    has_no_ols = "NO OLS" in upper_name
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
    if has_small_file: scenario_type = "Small Files"
    elif has_large_file: scenario_type = "Large Files"
    elif has_csv_file: scenario_type = "CSV Files"

    file_set_type = ""
    if has_small_file: file_set_type = "Small Files"
    elif has_large_file: file_set_type = "Large Files"
    elif has_csv_file: file_set_type = "CSV Files"

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


def resolve_expected_totals_for_scan(scan_name: str) -> dict:
    """
    Uses file_set_types table (folder/bucket ground truth) to estimate expected totals for a scan.
    Returns:
      {
        "data_source_type": "S3"/"SMB"/...,
        "file_set_type": "Small Files"/"Large Files"/...,
        "lookup_key": "S3 Large Files" (if matched) or "Large Files"/...,
        "expected_total_files": int|None,
        "expected_total_folders": int|None,
        "expected_total_size_gb": float|None,
      }
    """
    inferred = infer_add_benchmark_defaults_from_scan_name(scan_name)
    ds = str((inferred or {}).get("data_source_type") or "").strip()
    fs = str((inferred or {}).get("file_set_type") or "").strip()

    names, details = get_file_set_types(data_type=None)
    details = details or {}

    candidates = []
    if ds and fs:
        candidates.append(f"{ds} {fs}")
    if fs:
        candidates.append(fs)

    chosen = ""
    for k in candidates:
        if k in details:
            chosen = k
            break

    d = details.get(chosen) if chosen else None
    exp_files = d.get("total_files") if isinstance(d, dict) else None
    exp_folders = d.get("total_folders") if isinstance(d, dict) else None
    exp_size = d.get("total_size_gb") if isinstance(d, dict) else None

    try:
        exp_files = int(exp_files) if exp_files is not None else None
    except Exception:
        exp_files = None
    try:
        exp_folders = int(exp_folders) if exp_folders is not None else None
    except Exception:
        exp_folders = None
    try:
        exp_size = float(exp_size) if exp_size is not None else None
    except Exception:
        exp_size = None

    return {
        "data_source_type": ds,
        "file_set_type": fs,
        "lookup_key": chosen,
        "expected_total_files": exp_files,
        "expected_total_folders": exp_folders,
        "expected_total_size_gb": exp_size,
    }


def get_scan_name_from_db(scan_id):
    if not engine or not scan_id:
        return ""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT scan_name FROM scan_parents WHERE scan_id = :sid LIMIT 1"),
                {"sid": str(scan_id)}
            ).fetchone()
            return str(row[0] or "").strip() if row else ""
    except Exception:
        return ""


def get_scan_parent_details_from_db(scan_id):
    if not engine or not scan_id:
        return {}
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT started_at, completed_at, scan_name
                FROM scan_parents
                WHERE scan_id = :sid
                LIMIT 1
            """), {"sid": str(scan_id)}).fetchone()
            if not row:
                return {}
            return {
                "started_at": str(row[0] or "").strip(),
                "completed_at": str(row[1] or "").strip(),
                "scan_name": str(row[2] or "").strip(),
            }
    except Exception:
        return {}


def get_environment_row_for_scan_id(scan_id):
    if not engine or not scan_id:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT te.id, te.env_name, te.base_url, te.version, te.classifiers_api_path, te.is_active
                FROM scan_parents sp
                JOIN test_environments te ON te.id = sp.environment_id
                WHERE sp.scan_id = :sid
                LIMIT 1
            """), {"sid": str(scan_id)}).fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "env_name": row[1],
                "base_url": row[2],
                "version": row[3],
                "classifiers_api_path": row[4],
                "is_active": row[5],
            }
    except Exception:
        return None


def sync_single_scan_insight(scan_id, env_row):
    """
    Force-sync one scan insight + attributes and upsert normalized records.
    """
    if not scan_id or not env_row:
        return False
    try:
        insight_payload, _ = fetch_parent_scan_insight_with_auth_retry(env_row, scan_id)
        new_cnt, removed_cnt, attrs_payload, _ = fetch_parent_scan_attribute_counts_with_auth_retry(
            env_row, scan_id, limit=200
        )
        metrics = extract_scan_insight_metrics(insight_payload if isinstance(insight_payload, dict) else {})
        metrics["new_attributes_count"] = new_cnt
        metrics["removed_attributes_count"] = removed_cnt
        upsert_scan_insight_record(
            scan_id,
            metrics,
            insight_payload=insight_payload if isinstance(insight_payload, dict) else None,
            attributes_payload=attrs_payload if isinstance(attrs_payload, dict) else None,
        )
        return True
    except Exception:
        return False


def auto_sync_scan_items(env_row, scan_items, max_workers=6, only_missing_details=False, max_to_sync=None):
    """
    Auto-sync insights/attributes for all eligible scans in parallel.
    Excludes scans in states Stopped/Aborted.
    """
    if not scan_items:
        return {"attempted": 0, "synced": 0, "failed": 0, "skipped_existing": 0, "pending_after_batch": 0}

    excluded = {"stopped", "aborted"}
    candidates = []
    candidate_state_map = {}
    seen = set()
    for scan in scan_items:
        if not isinstance(scan, dict):
            continue
        sid = str(scan.get("_id") or scan.get("id") or "").strip()
        if not sid or sid in seen:
            continue
        scan_name = str(scan.get("name") or scan.get("scanName") or scan.get("displayName") or "").strip()
        if is_ignored_scan_name(scan_name):
            continue
        state = resolve_scan_status_value(scan).strip().lower()
        if state in excluded:
            continue
        seen.add(sid)
        candidates.append(sid)
        candidate_state_map[sid] = state

    if not candidates:
        return {"attempted": 0, "synced": 0, "failed": 0, "skipped_existing": 0, "pending_after_batch": 0}

    to_sync = []
    skipped_existing = 0
    pending_after_batch = 0
    if only_missing_details:
        detail_map = load_scan_detail_completeness_map(candidates)
        all_missing = []
        running_like_states = {"started", "awaiting", "in progress", "inprogress"}
        for sid in candidates:
            state_norm = str(candidate_state_map.get(sid) or "").strip().lower()
            # Keep running scans fresh even when detail rows already exist.
            if state_norm in running_like_states:
                all_missing.append(sid)
                continue
            if detail_map.get(sid):
                skipped_existing += 1
            else:
                all_missing.append(sid)
        if max_to_sync is not None:
            try:
                batch_size = max(1, int(max_to_sync))
            except Exception:
                batch_size = len(all_missing)
            to_sync = all_missing[:batch_size]
            pending_after_batch = max(0, len(all_missing) - len(to_sync))
        else:
            to_sync = all_missing
    else:
        to_sync = list(candidates)

    if not to_sync:
        return {
            "attempted": 0,
            "synced": 0,
            "failed": 0,
            "skipped_existing": skipped_existing,
            "pending_after_batch": pending_after_batch,
        }

    synced = 0
    failed = 0
    workers = max(1, min(int(max_workers), 10))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(sync_single_scan_insight, sid, env_row): sid for sid in to_sync}
        for fut in as_completed(futures):
            try:
                ok = bool(fut.result())
                if ok:
                    synced += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
    return {
        "attempted": len(to_sync),
        "synced": synced,
        "failed": failed,
        "skipped_existing": skipped_existing,
        "pending_after_batch": pending_after_batch,
    }


def upsert_auto_trend_rows_from_scans(env_row, scan_items):
    """
    Update auto trend table from already-stored DB insight rows.
    This runs during sync flow, not during table rendering.
    """
    if not scan_items:
        return 0
    updated = 0
    for scan in (scan_items or []):
        if not isinstance(scan, dict):
            continue
        scan_name = str(scan.get("name") or scan.get("scanName") or scan.get("displayName") or "").strip()
        if is_ignored_scan_name(scan_name):
            continue
        state = resolve_scan_status_value(scan).strip().lower()
        if state in {"stopped", "aborted"}:
            continue
        sid = str(scan.get("_id") or scan.get("id") or "").strip()
        if not sid:
            continue
        metrics = {
            "distinct_classification_attributes": "",
            "classifier_attribute_names": "",
            "data_source_names": "",
            "objects_with_pi": "",
            "objects_scanned": "",
            "objects_enumerated": "",
            "total_classification": "",
            "new_attributes_count": "",
            "removed_attributes_count": "",
        }
        db_metrics = load_scan_insight_from_db(sid)
        if db_metrics:
            metrics.update(db_metrics)
        upsert_auto_scan_benchmark(scan, env_row, metrics)
        updated += 1
    return updated


def get_scan_attribute_findings_prefill(scan_id):
    """
    Build findings-row prefill from normalized scan attribute delta tables.
    """
    if not engine or not scan_id:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT a.name, m.is_new, m.is_removed, m.finding_count, m.attribute_value, m.new_count_value, m.removed_count_value
                FROM scan_insight_attribute_delta_map m
                JOIN scan_classifier_attributes a ON a.id = m.attribute_id
                WHERE m.scan_id = :sid
                ORDER BY a.name
            """), {"sid": str(scan_id)}).fetchall()
    except Exception:
        return []

    # If DB rows are missing or only zero values, force one-shot API sync and retry.
    has_non_zero = any(
        ((r[5] or 0) and int(r[5]) > 0) or ((r[6] or 0) and int(r[6]) > 0) or ((r[3] or 0) and int(r[3]) > 0)
        for r in rows
        if r is not None
    )
    if (not rows or not has_non_zero) and scan_id:
        env_row = get_environment_row_for_scan_id(scan_id)
        if env_row and sync_single_scan_insight(scan_id, env_row):
            try:
                with engine.connect() as conn:
                    rows = conn.execute(text("""
                        SELECT a.name, m.is_new, m.is_removed, m.finding_count, m.attribute_value, m.new_count_value, m.removed_count_value
                        FROM scan_insight_attribute_delta_map m
                        JOIN scan_classifier_attributes a ON a.id = m.attribute_id
                        WHERE m.scan_id = :sid
                        ORDER BY a.name
                    """), {"sid": str(scan_id)}).fetchall()
            except Exception:
                rows = rows or []

    prefill_rows = []
    for attr_name, is_new, is_removed, finding_count, stored_value, new_count_value, removed_count_value in rows:
        label = ""
        if bool(is_new) and bool(is_removed):
            label = "New, Removed"
        elif bool(is_new):
            label = "New"
        elif bool(is_removed):
            label = "Removed"
        if str(stored_value or "").strip():
            label = str(stored_value or "").strip()
        try:
            new_cnt = int(new_count_value) if new_count_value is not None else 0
        except Exception:
            new_cnt = 0
        try:
            rem_cnt = int(removed_count_value) if removed_count_value is not None else 0
        except Exception:
            rem_cnt = 0
        if new_cnt > 0:
            prefill_rows.append({
                "Classifier Attribute": str(attr_name or "").strip(),
                "Attribute Value": "New",
                "Findings": new_cnt
            })
        if rem_cnt > 0:
            prefill_rows.append({
                "Classifier Attribute": str(attr_name or "").strip(),
                "Attribute Value": "Removed",
                "Findings": rem_cnt
            })
        if new_cnt == 0 and rem_cnt == 0:
            try:
                findings_count_val = int(finding_count) if finding_count is not None else 0
            except Exception:
                findings_count_val = 0
            prefill_rows.append({
                "Classifier Attribute": str(attr_name or "").strip(),
                "Attribute Value": label,
                "Findings": findings_count_val
            })
    return prefill_rows


def page_add_benchmark():
    st.title("➕ Add New Benchmark Run")
    st.markdown("Enter details below. This will populate the **normalized** tables (`builds`, `configs`, `classifiers`, `benchmarks`).")
    if st.session_state.get("benchmark_saved"):
        st.success("✅ Benchmark saved successfully.")
        st.session_state["benchmark_saved"] = False

    # Optional prefill from Scan Status page
    prefill = {
        "build_version": "",
        "scenario_type": "",
        "file_set_type": "",
        "is_acl": False,
        "scan_type": "OLS Scan",
        "is_ff": False,
        "is_ocr": False,
    }
    scan_prefill_id = ""
    scan_prefill_name = ""
    try:
        qp = st.query_params
        scan_prefill_id = str(qp.get("scan_id", "") or "").strip()
        if scan_prefill_id:
            scan_prefill_name = get_scan_name_from_db(scan_prefill_id)
            if scan_prefill_name:
                prefill = infer_add_benchmark_defaults_from_scan_name(scan_prefill_name)
    except Exception:
        pass
    if scan_prefill_id and st.session_state.get("_last_scan_prefill_id") != scan_prefill_id:
        parent_meta = get_scan_parent_details_from_db(scan_prefill_id)
        started_dt = parse_iso_datetime(parent_meta.get("started_at"))
        completed_dt = parse_iso_datetime(parent_meta.get("completed_at"))
        prefill_findings = get_scan_attribute_findings_prefill(scan_prefill_id)

        st.session_state["add_build_ver"] = str(prefill.get("build_version") or "")
        st.session_state["add_scenario_type"] = str(prefill.get("scenario_type") or "")
        st.session_state["add_scan_type"] = str(prefill.get("scan_type") or "OLS Scan")
        st.session_state["add_is_acl"] = bool(prefill.get("is_acl"))
        st.session_state["add_is_ff"] = bool(prefill.get("is_ff"))
        ds_prefill = str(prefill.get("data_source_type") or "").strip()
        ds_options_prefill = get_data_source_types() or ["S3", "SMB", "MSSQL", "Oracle", "NFS", "Unknown"]
        if ds_prefill and ds_prefill in ds_options_prefill:
            st.session_state["add_ds_type_choice"] = ds_prefill
            st.session_state["add_custom_ds_type"] = ""
        elif ds_prefill:
            st.session_state["add_ds_type_choice"] = "Other (custom)"
            st.session_state["add_custom_ds_type"] = ds_prefill
        if str(prefill.get("file_set_type") or "").strip().lower() == "small file":
            st.session_state["data_type"] = "Unstructured"
            st.session_state["file_set_unstructured"] = "Small Files"
            st.session_state["custom_file_set_type"] = "Small File"
        if started_dt:
            st.session_state["scan_start_date"] = started_dt.date()
            st.session_state["scan_start_time_str"] = started_dt.strftime("%H:%M:%S")
        if completed_dt:
            st.session_state["scan_end_date"] = completed_dt.date()
            st.session_state["scan_end_time_str"] = completed_dt.strftime("%H:%M:%S")
            st.session_state["enum_end_date"] = completed_dt.date()
            st.session_state["enum_end_time_str"] = completed_dt.strftime("%H:%M:%S")
        st.session_state["findings_prefill_rows"] = prefill_findings
        if "findings_editor" in st.session_state:
            del st.session_state["findings_editor"]
        st.session_state["_last_scan_prefill_id"] = scan_prefill_id
    if scan_prefill_id:
        st.caption(f"Prefill source scan id: `{scan_prefill_id}`" + (f" | name: `{scan_prefill_name}`" if scan_prefill_name else ""))

    # 1. Build Info
    st.subheader("1. Build Information")
    c1, c2 = st.columns(2)
    build_ver = c1.text_input("Build Version (e.g. 218.117)", help="Required", key="add_build_ver")
    
    # 2. Classifier Info
    st.subheader("2. Classifiers")
    cl_c1, cl_c2 = st.columns(2)
    template_name = cl_c1.selectbox(
        "Template Name",
        ["Data Source Scan", "Smart Scan", "Labeling Scan"],
        index=0
    )
    classifier_input = cl_c2.text_input("Classifiers (comma-separated)", help="Example: PII, PCI, GDPR")
    classifier_set = classifier_input
    
    # 3. Configuration & Flags
    st.subheader("3. Configuration & Flags")
    c3, c4, c5, c6 = st.columns(4)
    scan_count = c3.number_input("Scanner Count", min_value=1, value=5)
    thread_count = c4.number_input("Threads per Scanner", min_value=1, value=50)
    
    is_acl = c5.checkbox("ACL Enabled", key="add_is_acl")
    is_ff = c6.checkbox("Structured Discovery FF", key="add_is_ff")
    
    feature_flags = st.text_input("Additional Feature Flags (comma-separated)")
    comment = st.text_area("Configuration Comment (Optional)")

    # 4. Run Metrics (dynamic)
    st.subheader("4. Run Metrics")
    r1_c1, r1_c2, r1_c3 = st.columns(3)
    run_date = r1_c1.date_input("Run Date")
    scenario_type = r1_c2.text_input("Scenario Type", help="Describe the scenario being tested", key="add_scenario_type")
    scan_type_options = ["Full Scan", "Differential Scan", "OLS Scan", "Incremental Scan", "Other"]
    if st.session_state.get("add_scan_type") not in scan_type_options:
        st.session_state["add_scan_type"] = "OLS Scan"
    scan_type = r1_c3.selectbox("Scan Type", scan_type_options, key="add_scan_type")
    ds_options = get_data_source_types()
    if not ds_options:
        ds_options = ["S3", "SMB", "MSSQL", "Oracle", "NFS", "Unknown"]
    def reset_file_fields():
        for k in ["file_set_structured", "file_set_unstructured", "total_rows", "total_columns", "total_files", "total_folders"]:
            if k in st.session_state:
                del st.session_state[k]

    c_ds, c_dt, c_fs = st.columns(3)
    with c_ds:
        ds_all_options = ds_options + ["Other (custom)"]
        if "add_ds_type_choice" not in st.session_state or st.session_state.get("add_ds_type_choice") not in ds_all_options:
            st.session_state["add_ds_type_choice"] = ds_all_options[0]
        ds_type_choice = st.selectbox("Data Source Type", ds_all_options, key="add_ds_type_choice")
        custom_ds_type = None
        if ds_type_choice == "Other (custom)":
            custom_ds_type = st.text_input("Custom Data Source Type", key="add_custom_ds_type")
        ds_type = custom_ds_type.strip() if custom_ds_type else ds_type_choice
        ds_clean = str(ds_type or "").strip()
    with c_dt:
        prefill_fs_raw = str(prefill.get("file_set_type") or "").strip().lower()
        if prefill_fs_raw in ["small file", "large file", "csv file", "small files", "large files", "csv files"]:
            default_data_type = "Unstructured"
        elif ds_clean.upper() in ["SMB", "S3", "NFS"]:
            default_data_type = "Unstructured"
        else:
            default_data_type = "Structured"
        
        default_data_type_idx = ["Structured", "Unstructured", "Mixed"].index(default_data_type) if default_data_type in ["Structured", "Unstructured", "Mixed"] else 0
        data_type = st.selectbox(
            "Data Type",
            ["Structured", "Unstructured", "Mixed"],
            index=default_data_type_idx,
            key="data_type",
            on_change=reset_file_fields
        )
    fs_names, fs_details = get_file_set_types(data_type)
    with c_fs:
        if data_type == "Structured":
            fs_defaults = ["Small Tables", "Large Tables", "Mixed Tables"]
            fs_raw_options = sorted(set(fs_names + fs_defaults))
            fs_display_options = [
                (raw if ds_clean and raw.lower().startswith(ds_clean.lower() + " ") else (f"{ds_clean} {raw}".strip() if ds_clean else raw))
                for raw in fs_raw_options
            ] + ["Other (custom)"]
            
            # Stable indexing for Structured
            current_val = st.session_state.get("file_set_structured")
            if current_val in fs_display_options:
                default_fs_idx = fs_display_options.index(current_val)
            else:
                default_fs_idx = 0
            
            file_set_choice = st.selectbox("File Set Type", fs_display_options, index=default_fs_idx, key="file_set_structured")
        else:
            fs_defaults = ["Small Files", "Large Files", "Mixed Files", "CSV Files"]
            fs_raw_options = sorted(set(fs_names + fs_defaults))
            fs_display_options = [
                (raw if ds_clean and raw.lower().startswith(ds_clean.lower() + " ") else (f"{ds_clean} {raw}".strip() if ds_clean else raw))
                for raw in fs_raw_options
            ] + ["Other (custom)"]
            prefill_fs = str(prefill.get("file_set_type") or "").strip().lower()
            
            # Map prefill keywords to display names
            keyword_map = {
                "small file": "Small Files",
                "large file": "Large Files",
                "csv file": "CSV Files",
                "small files": "Small Files",
                "large files": "Large Files",
                "csv files": "CSV Files"
            }
            target_raw = keyword_map.get(prefill_fs)
            preferred_target = f"{ds_clean} {target_raw}".strip() if ds_clean and target_raw else (target_raw if target_raw else None)
            
            # 1. Check if user already has a selection in session state
            current_val = st.session_state.get("file_set_unstructured")
            if current_val in fs_display_options:
                default_fs_idx = fs_display_options.index(current_val)
            # 2. Otherwise use prefill
            elif preferred_target and preferred_target in fs_display_options:
                default_fs_idx = fs_display_options.index(preferred_target)
            elif preferred_target and "Other (custom)" in fs_display_options:
                default_fs_idx = fs_display_options.index("Other (custom)")
            else:
                default_fs_idx = 0

            file_set_choice = st.selectbox("File Set Type", fs_display_options, index=default_fs_idx, key="file_set_unstructured")
    custom_fs_default = "Small File" if str(prefill.get("file_set_type") or "").strip().lower() == "small file" else ""
    custom_fs = st.text_input("Custom File Set Type", key="custom_file_set_type", value=custom_fs_default) if file_set_choice == "Other (custom)" else ""
    file_set_type_raw = custom_fs.strip() if custom_fs.strip() else file_set_choice
    if file_set_choice != "Other (custom)" and ds_clean and file_set_type_raw.lower().startswith(ds_clean.lower() + " "):
        file_set_type_raw = file_set_type_raw[len(ds_clean):].strip()
    if ds_clean and file_set_type_raw:
        if file_set_type_raw.lower().startswith(ds_clean.lower() + " "):
            file_set_type = file_set_type_raw
        else:
            file_set_type = f"{ds_clean} {file_set_type_raw}"
    else:
        file_set_type = file_set_type_raw

    def resolve_file_set_preset(details_map, ds_name, raw_name, display_name, combined_name):
        if not isinstance(details_map, dict) or not details_map:
            return {}

        def strip_ds(v):
            s = str(v or "").strip()
            if ds_name and s.lower().startswith(ds_name.lower() + " "):
                s = s[len(ds_name):].strip()
            return s

        raw_name = str(raw_name or "").strip()
        display_name = str(display_name or "").strip()
        combined_name = str(combined_name or "").strip()
        ds_name = str(ds_name or "").strip()

        candidates = [
            raw_name,
            display_name,
            combined_name,
            strip_ds(raw_name),
            strip_ds(display_name),
            strip_ds(combined_name),
        ]
        if ds_name and raw_name:
            candidates.append(f"{ds_name} {raw_name}")

        # 1) Exact match candidates
        for c in candidates:
            if c in details_map:
                return details_map.get(c) or {}

        # 2) Case-insensitive key match
        lowered = {str(k).strip().lower(): k for k in details_map.keys()}
        for c in candidates:
            lk = str(c).strip().lower()
            if lk in lowered:
                return details_map.get(lowered[lk]) or {}

        # 3) Normalized match after removing datasource prefix on both sides
        for c in candidates:
            c_norm = strip_ds(c).lower()
            if not c_norm:
                continue
            for k in details_map.keys():
                if strip_ds(k).lower() == c_norm:
                    return details_map.get(k) or {}

        return {}

    preset = resolve_file_set_preset(
        fs_details,
        ds_clean,
        file_set_type_raw,
        file_set_choice,
        file_set_type,
    )
    avg_size = str(preset.get("avg_file_size") or "")
    st.caption("File Set Type Details (auto-filled)")
    if data_type == "Structured":
        details_row = {
            "File Set Type": file_set_type,
            "Total Rows": preset.get("total_files"),
            "Total Columns": preset.get("total_folders"),
            "Total Size (GB)": preset.get("total_size_gb"),
            "Avg Row Size": avg_size
        }
    else:
        details_row = {
            "File Set Type": file_set_type,
            "Total Files": preset.get("total_files"),
            "Total Folders": preset.get("total_folders"),
            "Total Size (GB)": preset.get("total_size_gb"),
            "Avg File Size": avg_size
        }
    st.table(pd.DataFrame([details_row]))

    # Use values from selected file set details (no duplicate fields in UI)
    total_files = int(preset.get("total_files")) if preset.get("total_files") is not None else 0
    total_folders = int(preset.get("total_folders")) if preset.get("total_folders") is not None else 0
    total_gb = float(preset.get("total_size_gb")) if preset.get("total_size_gb") is not None else 0.0
    # New Attributes: this is the value you historically stored in benchmarks.total_catalog_items
    # and want used for trends. Prefer scan_insights.new_attributes_count when scan_id is provided.
    total_catalog_items = 0
    try:
        if scan_prefill_id:
            m = load_scan_insight_map_for_scan_ids([scan_prefill_id]).get(str(scan_prefill_id), {}) or {}
            nac = m.get("new_attributes_count")
            if nac is not None and str(nac).strip() != "":
                total_catalog_items = int(nac)
                st.caption(f"Prefilled from scan_insights: New Attributes = `{total_catalog_items:,}`")
    except Exception:
        pass

    with st.form("add_benchmark_form"):
        st.markdown("#### Scan Timing")
        st.caption("Enter start and end times - throughput will be calculated automatically")
        
        t1_c1, t1_c2, t1_c3, t1_c4 = st.columns(4)
        scan_start_date = t1_c1.date_input("Scan Start Date", key="scan_start_date")
        scan_start_time_str = t1_c1.text_input("Scan Start Time (HH:MM[:SS])", key="scan_start_time_str", placeholder="2:13:17")
        scan_end_date = t1_c2.date_input("Scan End Date", key="scan_end_date")
        scan_end_time_str = t1_c2.text_input("Scan End Time (HH:MM[:SS])", key="scan_end_time_str", placeholder="2:29:34")
        t1_c3.text_input("Enum Start Date", value=str(scan_start_date), disabled=True)
        t1_c3.text_input("Enum Start Time (HH:MM[:SS])", value=scan_start_time_str, disabled=True)
        enum_end_date = t1_c4.date_input("Enum End Date", key="enum_end_date")
        enum_end_time_str = t1_c4.text_input("Enum End Time (HH:MM[:SS])", key="enum_end_time_str", placeholder="2:29:00")

        def parse_time_value(val):
            val = (val or "").strip()
            for fmt in ("%H:%M:%S", "%H:%M", "%H.%M", "%H%M", "%I:%M:%S%p", "%I:%M:%S %p", "%I:%M%p", "%I:%M %p"):
                try:
                    return datetime.strptime(val, fmt).time()
                except Exception:
                    continue
            return None

        scan_start_time = parse_time_value(scan_start_time_str)
        scan_end_time = parse_time_value(scan_end_time_str)
        enum_start_time = parse_time_value(scan_start_time_str)
        enum_end_time = parse_time_value(enum_end_time_str)

        scan_start = datetime.combine(scan_start_date, scan_start_time) if scan_start_date and scan_start_time else None
        scan_end = datetime.combine(scan_end_date, scan_end_time) if scan_end_date and scan_end_time else None
        enum_start = datetime.combine(scan_start_date, enum_start_time) if scan_start_date and enum_start_time else None
        enum_end = datetime.combine(enum_end_date, enum_end_time) if enum_end_date and enum_end_time else None

        # Preview calculated scan duration
        if scan_start and scan_end:
            duration_seconds = (scan_end - scan_start).total_seconds()
            if duration_seconds > 0:
                hours = duration_seconds / 3600
                hh = int(duration_seconds // 3600)
                mm = int((duration_seconds % 3600) // 60)
                ss = int(duration_seconds % 60)
                st.info(f"Calculated Scan Time: {hh:02d}:{mm:02d}:{ss:02d} ({hours:.2f} hours)")
            else:
                st.warning("Scan End Time must be after Scan Start Time.")

        st.subheader("5. Findings Files")
        st.caption("Add classifier attribute/value and findings count. Add rows as needed.")
        # Findings rows are classifier *attribute names* (e.g. "classifier.Email"),
        # which come from scan insights. Build-wise classifier mappings are not required here.
        attr_options = []
        try:
            if scan_prefill_id:
                m = load_scan_insight_map_for_scan_ids([scan_prefill_id]).get(str(scan_prefill_id), {}) or {}
                raw = str(m.get("classifier_attribute_names") or "").strip()
                if raw:
                    attr_options = [x.strip() for x in raw.split(",") if x.strip()]
        except Exception:
            attr_options = []
        # IMPORTANT: Do not call BigID APIs from this page. Tracker is the only updater.
        # Fallback: derive from prefilled rows (if any)
        try:
            if not attr_options:
                pre_rows = st.session_state.get("findings_prefill_rows") or []
                derived = []
                for r in pre_rows:
                    if isinstance(r, dict):
                        v = str(r.get("Classifier Attribute") or "").strip()
                        if v:
                            derived.append(v)
                attr_options = sorted(set(derived))
        except Exception:
            pass
        if scan_prefill_id and not attr_options:
            st.caption("No classifier attribute names found yet for this scan. Run/let `tracker.py` populate scan insights, or type manually.")
        default_findings_rows = st.session_state.get("findings_prefill_rows")
        if not isinstance(default_findings_rows, list) or not default_findings_rows:
            default_findings_rows = [{
                "Classifier Attribute": "",
                "Attribute Value": "",
                "Findings": 0
            }]
        classifier_attr_col = (
            st.column_config.SelectboxColumn("Classifier Attribute", options=attr_options)
            if attr_options else
            st.column_config.TextColumn("Classifier Attribute")
        )
        findings_df = st.data_editor(
            pd.DataFrame(default_findings_rows),
            num_rows="dynamic",
            width="stretch",
            key="findings_editor",
            column_config={
                "Classifier Attribute": classifier_attr_col,
                "Attribute Value": st.column_config.TextColumn("Attribute Value"),
                "Findings": st.column_config.NumberColumn("Findings", min_value=0, step=1)
            }
        )
        findings = []
        for _, row in findings_df.iterrows():
            attr_name = str(row.get("Classifier Attribute", "")).strip()
            attr_value = str(row.get("Attribute Value", "")).strip()
            count = row.get("Findings", None)
            if attr_name or attr_value:
                try:
                    count_val = int(count) if count is not None else 0
                except Exception:
                    count_val = 0
                findings.append((attr_name, attr_value, count_val))

        submitted = st.form_submit_button("💾 Save Benchmark Run", type="primary")
        
        if submitted:
            if not build_ver:
                st.error("Build Version is required.")
                return
            if not scan_start or not scan_end:
                st.error("Scan Start and End times are required for throughput calculation.")
                return
            if not engine:
                st.error("Database not connected.")
                return
                
            try:
                is_ocr = False
                # Calculate durations in hours
                scan_duration = (scan_end - scan_start).total_seconds() / 3600
                enum_duration = (enum_end - enum_start).total_seconds() / 3600 if enum_start and enum_end else 0
                
                if scan_duration <= 0:
                    st.error("Scan End Time must be after Scan Start Time.")
                    return
                
                # Calculate all throughput metrics
                # Cluster (total)
                tp_cluster_gb = total_gb / scan_duration if scan_duration > 0 else 0
                tp_cluster_files = total_files / scan_duration if scan_duration > 0 else 0
                
                # Per Scanner
                tp_scanner_gb = tp_cluster_gb / scan_count if scan_count > 0 else 0
                tp_scanner_files = tp_cluster_files / scan_count if scan_count > 0 else 0
                
                # Per Thread (threads per scanner)
                tp_thread_gb = tp_cluster_gb / thread_count if thread_count > 0 else 0
                tp_thread_files = tp_cluster_files / thread_count if thread_count > 0 else 0
                
                with engine.connect() as conn:
                    # A. Insert Build
                    b_id = get_or_create_id(conn, "builds", "version", build_ver.strip())

                    # Build template & classifier set (optional)
                    ensure_build_templates_table()
                    if template_name.strip() or classifier_set.strip():
                        conn.execute(text("""
                            INSERT INTO build_templates (build_id, template_name, classifier_set)
                            VALUES (:bid, :tn, :cs)
                            ON DUPLICATE KEY UPDATE classifier_set = VALUES(classifier_set)
                        """), {
                            "bid": b_id,
                            "tn": template_name.strip() if template_name.strip() else "Default",
                            "cs": classifier_set.strip()
                        })
                        conn.commit()
                    
                    # B. Insert Classifier(s)
                    classifier_list = [c.strip() for c in (classifier_input or "").split(",") if c.strip()]
                    classifier_list_str = ", ".join(classifier_list)
                    primary_classifier = classifier_list[0] if classifier_list else ""
                    c_id = get_or_create_id(conn, "classifiers", "name", primary_classifier) if primary_classifier else None
                    for cl in classifier_list[1:]:
                        get_or_create_id(conn, "classifiers", "name", cl)
                    
                    # C. Insert Config
                    conf_str = f"{scan_count}-{thread_count}-{is_acl}-{is_ocr}-{is_ff}-{feature_flags}-{comment}"
                    conf_hash = hashlib.md5(conf_str.encode()).hexdigest()
                    
                    chk_conf = text("SELECT id FROM configurations WHERE hash = :h")
                    res_conf = conn.execute(chk_conf, {'h': conf_hash}).fetchone()
                    
                    if res_conf:
                        conf_id = res_conf[0]
                    else:
                        ins_conf = text("""
                            INSERT INTO configurations 
                            (scanner_count, thread_count, is_acl, is_ocr, is_feature_flags, feature_flags, comment, hash)
                            VALUES (:sc, :tc, :acl, :ocr, :ff, :fflags, :cm, :h)
                        """)
                        conn.execute(ins_conf, {
                            'sc': scan_count, 'tc': thread_count,
                            'acl': is_acl, 'ocr': is_ocr, 'ff': is_ff,
                            'fflags': feature_flags, 'cm': comment, 'h': conf_hash
                        })
                        conn.commit()
                        conf_id = conn.execute(chk_conf, {'h': conf_hash}).fetchone()[0]
                    
                    # D. Insert Benchmark with calculated throughputs
                    # Ensure Data Source Type exists in lookup table
                    if ds_type:
                        get_or_create_id(conn, "data_source_types", "name", ds_type)
                    if file_set_type:
                        fs_id = get_or_create_id(conn, "file_set_types", "name", file_set_type)
                        try:
                            conn.execute(
                                text("UPDATE file_set_types SET data_type = :dt WHERE id = :id AND (data_type IS NULL OR data_type = '')"),
                                {"dt": data_type, "id": fs_id}
                            )
                        except Exception:
                            pass

                    # Attempt to check if scan_id column exists for INSERT
                    has_scan_id_col = False
                    try:
                        has_scan_id_col = _db_has_column("benchmarks", "scan_id")
                    except Exception:
                        pass

                    cols_list = [
                        "build_id", "config_id", "classifier_id", "classifier_list", "run_date", "scenario_type", "scan_type", "data_source_type", "data_type", "file_set_type",
                        "total_files", "total_folders", "total_size_gb", "total_catalog_items", "avg_file_size", 
                        "scan_start_time", "scan_end_time", "enum_start_time", "enum_end_time",
                        "total_scan_hours", "enumeration_hours",
                        "throughput_cluster_gb_hr", "throughput_scanner_gb_hr", "throughput_thread_gb_hr",
                        "throughput_cluster_files_hr", "throughput_scanner_files_hr", "throughput_thread_files_hr"
                    ]
                    vals_placeholders = [
                        ":bid", ":cid", ":clid", ":clist", ":dt", ":scn", ":st", ":dst", ":dtype", ":fstype",
                        ":tf", ":tfo", ":tgb", ":tci", ":asz",
                        ":ss", ":se", ":es", ":ee", ":tsh", ":eh",
                        ":tpcgb", ":tpsgb", ":tptgb", ":tpcf", ":tpsf", ":tptf"
                    ]
                    params_dict = {
                        'bid': b_id, 'cid': conf_id, 'clid': c_id,
                        'clist': classifier_list_str,
                        'dt': str(run_date), 'scn': scenario_type, 'st': scan_type, 'dst': ds_type, 'dtype': data_type, 'fstype': file_set_type,
                        'tf': total_files, 'tfo': total_folders, 'tgb': total_gb, 'tci': total_catalog_items, 'asz': avg_size,
                        'ss': scan_start, 'se': scan_end, 
                        'es': enum_start, 'ee': enum_end,
                        'tsh': scan_duration, 'eh': enum_duration,
                        'tpcgb': tp_cluster_gb, 'tpsgb': tp_scanner_gb, 'tptgb': tp_thread_gb,
                        'tpcf': tp_cluster_files, 'tpsf': tp_scanner_files, 'tptf': tp_thread_files
                    }

                    # If scan_id is provided, pull classification metrics from scan_insights into benchmarks (manual trend table)
                    try:
                        if scan_prefill_id:
                            si = conn.execute(
                                text("""
                                    SELECT total_classification, new_attributes_count, removed_attributes_count
                                    FROM scan_insights
                                    WHERE scan_id = :sid
                                    LIMIT 1
                                """),
                                {"sid": str(scan_prefill_id).strip()},
                            ).fetchone()
                            if si:
                                params_dict["tclass"] = si[0]
                                # keep New Attributes in total_catalog_items
                                try:
                                    if si[1] is not None:
                                        params_dict["tci"] = int(si[1])
                                except Exception:
                                    pass
                                params_dict["trem"] = si[2]
                    except Exception:
                        pass

                    if has_scan_id_col:
                        cols_list.append("scan_id")
                        vals_placeholders.append(":sid")
                        params_dict['sid'] = scan_prefill_id if scan_prefill_id else None
                        # Also insert missing classification columns if present in schema
                        try:
                            if _db_has_column("benchmarks", "total_classification"):
                                cols_list.append("total_classification")
                                vals_placeholders.append(":tclass")
                                params_dict.setdefault("tclass", None)
                        except Exception:
                            pass
                        try:
                            if _db_has_column("benchmarks", "removed_attributes"):
                                cols_list.append("removed_attributes")
                                vals_placeholders.append(":trem")
                                params_dict.setdefault("trem", None)
                        except Exception:
                            pass

                    ins_bench = text(f"""
                        INSERT INTO benchmarks 
                        ({', '.join(cols_list)})
                        VALUES ({', '.join(vals_placeholders)})
                    """)
                    
                    result = conn.execute(ins_bench, params_dict)
                    bench_id = result.lastrowid
                    
                    if findings and bench_id:
                        ensure_benchmark_findings_extended_columns()
                        ins_findings = text("""
                            INSERT INTO benchmark_findings (benchmark_id, classifier_name, classifier_attribute, attribute_value, finding_count)
                            VALUES (:bid, :cname, :attr, :aval, :cnt)
                        """)
                        try:
                            conn.execute(
                                ins_findings,
                                [{
                                    "bid": bench_id,
                                    "cname": attr or "",
                                    "attr": attr or None,
                                    "aval": aval or None,
                                    "cnt": cnt
                                } for attr, aval, cnt in findings],
                            )
                        except Exception:
                            for attr, aval, cnt in findings:
                                conn.execute(ins_findings, {
                                    'bid': bench_id,
                                    'cname': attr or "",
                                    'attr': attr or None,
                                    'aval': aval or None,
                                    'cnt': cnt
                                })
                    conn.commit()
                    
                    st.success(f"✅ Successfully added Benchmark run for Build {build_ver}!")
                    st.info(f"📊 **Calculated Throughputs:**\n\n"
                           f"- Cluster: {tp_cluster_gb:.2f} GB/hr, {tp_cluster_files:,.0f} files/hr\n"
                           f"- Per Scanner: {tp_scanner_gb:.2f} GB/hr, {tp_scanner_files:,.0f} files/hr\n"
                           f"- Per Thread: {tp_thread_gb:.2f} GB/hr, {tp_thread_files:,.0f} files/hr")
                    st.toast("Data Saved!")
                    st.session_state["benchmark_saved"] = True
                    st.rerun()
                    
            except Exception as e:
                st.error(f"Error saving to DB: {e}")

def page_manage_classifiers():
    st.title("🧩 Build Template Classifiers")
    st.markdown("Manage build-wise template and comma-separated classifier sets.")

    if not engine:
        st.error("Database not connected.")
        return

    ensure_build_templates_table()

    with engine.connect() as conn:
        build_versions = [r[0] for r in conn.execute(text("SELECT version FROM builds ORDER BY version DESC")).fetchall()]

    c1, c2 = st.columns(2)
    build_version = c1.selectbox("Build Version", build_versions + ["Other (new build)"])
    if build_version == "Other (new build)":
        build_version = c1.text_input("New Build Version")
    template_name = c2.selectbox("Template Name", ["Data Source Scan", "Smart Scan", "Labeling Scan", "Other"])
    if template_name == "Other":
        template_name = c2.text_input("Custom Template Name")

    existing_set = ""
    if build_version and template_name:
        try:
            with engine.connect() as conn:
                row = conn.execute(text("""
                    SELECT bt.classifier_set
                    FROM build_templates bt
                    JOIN builds b ON bt.build_id = b.id
                    WHERE b.version = :v AND bt.template_name = :t
                    LIMIT 1
                """), {"v": build_version.strip(), "t": template_name.strip()}).fetchone()
                if row and row[0]:
                    existing_set = row[0]
        except Exception:
            existing_set = ""

    classifier_set = st.text_area("Classifiers (comma-separated)", value=existing_set, height=120)
    submitted = st.button("Save Build Template Classifiers", type="primary")
    if submitted:
        if not build_version or not build_version.strip():
            st.error("Build Version is required.")
            return
        if not template_name or not template_name.strip():
            st.error("Template Name is required.")
            return
        try:
            classifiers = [c.strip() for c in (classifier_set or "").split(",") if c.strip()]
            with engine.connect() as conn:
                b_id = get_or_create_id(conn, "builds", "version", build_version.strip())
                ensure_build_templates_table()
                conn.execute(text("""
                    INSERT INTO build_templates (build_id, template_name, classifier_set)
                    VALUES (:bid, :tn, :cs)
                    ON DUPLICATE KEY UPDATE classifier_set = VALUES(classifier_set)
                """), {"bid": b_id, "tn": template_name.strip(), "cs": ", ".join(classifiers)})
                for cl in classifiers:
                    get_or_create_id(conn, "classifiers", "name", cl)
                conn.commit()
            st.success("Build template classifiers saved.")
        except Exception as e:
            st.error(f"Error saving build template classifiers: {e}")


def page_manage_test_environments():
    st.title("🌐 Test Environment APIs")
    st.markdown("Manage test environments and load classifiers from API.")
    st.caption(
        "Saving an environment, using **Get Classifiers**, or bulk **Sync current versions** updates classifier **names/mappings** "
        "and refreshes **metadata snapshots** (per-classifier JSON for Deep-Dive). The tracker also polls active envs on a schedule."
    )
    if st.session_state.get("env_action_msg"):
        lvl = st.session_state.get("env_action_level", "info")
        msg = st.session_state.get("env_action_msg")
        if lvl == "success":
            st.success(msg)
        elif lvl == "warning":
            st.warning(msg)
        else:
            st.info(msg)
        # Keep message visible until next action overrides it.

    if not engine:
        st.error("Database not connected.")
        return

    ensure_test_environments_table(engine)
    env_df = load_test_environments()

    # Bulk: sync current versions for all active envs + global enabled classifiers (no manual follow-up steps).
    st.subheader("0. Bulk Actions")
    st.caption(
        "Bulk sync runs for **every active** environment. **Use for workflow runs** only affects the workflow runner, not sync or tracker."
    )
    if st.button("Sync current versions (API) for all active envs", type="primary"):
        if env_df.empty:
            st.warning("No environments found.")
        else:
            active_rows = env_df.copy()
            try:
                if "is_active" in active_rows.columns:
                    active_rows["is_active"] = active_rows["is_active"].apply(lambda x: bool(x))
                    active_rows = active_rows[active_rows["is_active"] == True]
            except Exception:
                pass

            if active_rows.empty:
                st.warning("No active environments to sync.")
            else:
                updated = 0
                failed = 0
                classifiers_ok = 0
                classifiers_fail = 0
                classifiers_skipped = 0
                details = []

                def _has_classifier_mappings(build_version: str, env_id_val: int) -> bool:
                    try:
                        with engine.connect() as conn:
                            row = conn.execute(
                                text("""
                                    SELECT COUNT(DISTINCT bcm.classifier_id)
                                    FROM build_classifier_mappings bcm
                                    JOIN builds b ON b.id = bcm.build_id
                                    WHERE b.version = :v AND bcm.environment_id = :e
                                """),
                                {"v": str(build_version).strip(), "e": int(env_id_val)},
                            ).fetchone()
                        return bool(row and int(row[0] or 0) > 0)
                    except Exception:
                        return False

                def _sync_one(env_row: dict) -> dict:
                    t0 = time.monotonic()
                    env_id = env_row.get("id")
                    env_name_val = str(env_row.get("env_name") or "").strip() or str(env_id or "")
                    base = env_row.get("base_url")
                    token = ""
                    try:
                        token = fetch_auth_token_for_env(env_row)
                    except Exception:
                        token = ""

                    res = {
                        "env": env_name_val,
                        "version_ok": False,
                        "version": "",
                        "version_raw": "",
                        "classifiers": "none",  # ok|fail|skipped|none
                        "classifiers_n": 0,
                        "classifiers_total": None,
                        "error": "",
                        "t_version_s": None,
                        "t_classifiers_api_s": None,
                        "t_db_sync_s": None,
                        "t_total_s": None,
                    }

                    clean_version = ""
                    raw_version = ""
                    try:
                        tv0 = time.monotonic()
                        clean_version, raw_version, _about_url = fetch_core_version_from_api(base, token, INTERNAL_ABOUT_API_PATH)
                        if clean_version and env_id is not None:
                            with engine.connect() as conn:
                                conn.execute(
                                    text("UPDATE test_environments SET version = :v WHERE id = :id"),
                                    {"v": str(clean_version), "id": int(env_id)},
                                )
                                conn.commit()
                            res["version_ok"] = True
                            res["version"] = str(clean_version)
                            res["version_raw"] = str(raw_version)
                        res["t_version_s"] = round(time.monotonic() - tv0, 3)
                    except Exception as ex:
                        res["error"] = f"version_sync_failed: {ex}"
                        try:
                            res["t_version_s"] = round(time.monotonic() - tv0, 3)
                        except Exception:
                            pass

                    effective_version = str(clean_version or env_row.get("version") or "").strip()
                    if not effective_version or env_id is None:
                        res["classifiers"] = "fail"
                        if not res["error"]:
                            res["error"] = "no_build_or_env_id"
                        res["t_total_s"] = round(time.monotonic() - t0, 3)
                        return res

                    # Skip heavy classifier fetch if mappings already exist for build+env.
                    if _has_classifier_mappings(effective_version, int(env_id)):
                        res["classifiers"] = "skipped"
                        try:
                            from tracker import sync_classifier_details_for_env_build

                            _er = {
                                "id": int(env_id),
                                "env_name": env_row.get("env_name"),
                                "base_url": base,
                                "classifiers_api_path": env_row.get("classifiers_api_path") or INTERNAL_CLASSIFIERS_API_PATH,
                            }
                            if token and effective_version:
                                sync_classifier_details_for_env_build(
                                    engine, _er, token, str(effective_version), force=False
                                )
                        except Exception:
                            pass
                        res["t_total_s"] = round(time.monotonic() - t0, 3)
                        return res

                    try:
                        # If this build already has mappings in DB (from any environment),
                        # clone them to this environment to avoid calling the classifiers API.
                        try:
                            has_any = False
                            names_from_db = []
                            with engine.connect() as conn:
                                any_row = conn.execute(
                                    text("""
                                        SELECT COUNT(DISTINCT bcm.classifier_id)
                                        FROM build_classifier_mappings bcm
                                        JOIN builds b ON b.id = bcm.build_id
                                        WHERE b.version = :v
                                    """),
                                    {"v": str(effective_version).strip()},
                                ).fetchone()
                                has_any = bool(any_row and int(any_row[0] or 0) > 0)
                                if has_any:
                                    names_from_db = [
                                        str(r[0]).strip()
                                        for r in conn.execute(
                                            text("""
                                                SELECT DISTINCT c.name
                                                FROM build_classifier_mappings bcm
                                                JOIN builds b ON b.id = bcm.build_id
                                                JOIN classifiers c ON c.id = bcm.classifier_id
                                                WHERE b.version = :v
                                                ORDER BY c.name
                                            """),
                                            {"v": str(effective_version).strip()},
                                        ).fetchall()
                                        if r and r[0]
                                    ]
                            if has_any and names_from_db:
                                td0 = time.monotonic()
                                clone_build_mappings_to_environment(int(env_id), effective_version)
                                set_id, _set_size, _new_cls_from_set = create_or_get_classifier_set(names_from_db)
                                record_environment_version(
                                    int(env_id),
                                    env_name_val,
                                    effective_version,
                                    classifier_set_id=set_id
                                )
                                res["classifiers_total"] = None
                                res["classifiers_n"] = len(names_from_db)
                                res["classifiers"] = "ok"
                                res["t_classifiers_api_s"] = 0.0
                                res["t_db_sync_s"] = round(time.monotonic() - td0, 3)
                                try:
                                    from tracker import sync_classifier_details_for_env_build

                                    _er = {
                                        "id": int(env_id),
                                        "env_name": env_row.get("env_name"),
                                        "base_url": base,
                                        "classifiers_api_path": env_row.get("classifiers_api_path") or INTERNAL_CLASSIFIERS_API_PATH,
                                    }
                                    if token and effective_version:
                                        sync_classifier_details_for_env_build(
                                            engine, _er, token, str(effective_version), force=True
                                        )
                                except Exception:
                                    pass
                                res["t_total_s"] = round(time.monotonic() - t0, 3)
                                return res
                        except Exception:
                            pass

                        ta0 = time.monotonic()
                        classifier_names, total_count, _ = fetch_classifiers_with_auth_retry(env_row)
                        res["t_classifiers_api_s"] = round(time.monotonic() - ta0, 3)
                        res["classifiers_total"] = total_count
                        res["classifiers_n"] = len(classifier_names or [])
                        if classifier_names:
                            td0 = time.monotonic()
                            set_id, _set_size, _new_cls_from_set = create_or_get_classifier_set(classifier_names)
                            record_environment_version(
                                int(env_id),
                                env_name_val,
                                effective_version,
                                classifier_set_id=set_id
                            )
                            sync_build_classifier_mappings(
                                effective_version,
                                classifier_names,
                                environment_id=int(env_id)
                            )
                            res["t_db_sync_s"] = round(time.monotonic() - td0, 3)
                            res["classifiers"] = "ok"
                            try:
                                from tracker import sync_classifier_details_for_env_build

                                _er = {
                                    "id": int(env_id),
                                    "env_name": env_row.get("env_name"),
                                    "base_url": base,
                                    "classifiers_api_path": env_row.get("classifiers_api_path") or INTERNAL_CLASSIFIERS_API_PATH,
                                }
                                if token and effective_version:
                                    sync_classifier_details_for_env_build(
                                        engine, _er, token, str(effective_version), force=True
                                    )
                            except Exception:
                                pass
                        else:
                            res["classifiers"] = "fail"
                            res["error"] = "classifiers_empty"
                    except Exception as ex:
                        res["classifiers"] = "fail"
                        res["error"] = f"classifier_sync_failed: {ex}"
                    res["t_total_s"] = round(time.monotonic() - t0, 3)
                    return res

                with st.spinner("Syncing versions + global classifiers (parallel)..."):
                    rows_list = active_rows.to_dict(orient="records")
                    max_workers = min(4, max(1, len(rows_list)))
                    with ThreadPoolExecutor(max_workers=max_workers) as ex:
                        futs = [ex.submit(_sync_one, r) for r in rows_list]
                        for f in as_completed(futs):
                            r = f.result()
                            if r.get("version_ok"):
                                updated += 1
                                details.append(f"[{r.get('env')}] version={r.get('version')} (raw: {r.get('version_raw')})")
                            else:
                                failed += 1
                                details.append(f"[{r.get('env')}] version sync failed")

                            cst = r.get("classifiers")
                            if cst == "ok":
                                classifiers_ok += 1
                                tc = r.get("classifiers_total")
                                tc_suffix = f" (totalCount={tc})" if tc is not None else ""
                                details.append(f"[{r.get('env')}] classifiers={r.get('classifiers_n')}{tc_suffix}")
                            elif cst == "skipped":
                                classifiers_skipped += 1
                                details.append(f"[{r.get('env')}] classifiers skipped (already mapped)")
                            elif cst == "fail":
                                classifiers_fail += 1
                                details.append(f"[{r.get('env')}] classifier sync failed")

                            try:
                                tv = r.get("t_version_s")
                                ta = r.get("t_classifiers_api_s")
                                td = r.get("t_db_sync_s")
                                tt = r.get("t_total_s")
                                parts = []
                                if tv is not None:
                                    parts.append(f"ver={tv}s")
                                if ta is not None:
                                    parts.append(f"api={ta}s")
                                if td is not None:
                                    parts.append(f"db={td}s")
                                if tt is not None:
                                    parts.append(f"total={tt}s")
                                if parts:
                                    details.append(f"[{r.get('env')}] timing " + " ".join(parts))
                            except Exception:
                                pass

                st.success(
                    f"Bulk sync complete. Versions updated={updated}, version failures={failed}. "
                    f"Classifiers synced={classifiers_ok}, skipped={classifiers_skipped}, failures={classifiers_fail}."
                )
                if details:
                    st.caption(" | ".join(details[:6]) + (" ..." if len(details) > 6 else ""))
                try:
                    st.cache_data.clear()
                except Exception:
                    pass
                st.rerun()

    st.subheader("1. Environment Details")
    env_names = env_df["env_name"].tolist() if not env_df.empty else []
    selected_env_name = st.selectbox("Existing Environment", ["Add New Environment"] + env_names)
    selected_row = {}
    if selected_env_name != "Add New Environment" and not env_df.empty:
        selected = env_df[env_df["env_name"] == selected_env_name]
        if not selected.empty:
            selected_row = selected.iloc[0].to_dict()

    # Initialize/edit form state per selected environment
    selection_marker = f"{selected_env_name}|{selected_row.get('id', 'new')}"
    if st.session_state.get("_env_form_selected_marker") != selection_marker:
        st.session_state["_env_form_selected_marker"] = selection_marker
        st.session_state["env_name_input"] = str(selected_row.get("env_name") or "")
        st.session_state["env_version_input"] = str(selected_row.get("version") or "")
        st.session_state["env_base_url_input"] = str(selected_row.get("base_url") or "")
        _cred_env_name = str(selected_row.get("env_name") or "")
        _saved_user, _saved_pass = get_saved_environment_credentials(_cred_env_name)
        st.session_state["env_auth_username_input"] = _saved_user
        st.session_state["env_auth_password_input"] = _saved_pass
        st.session_state["_prev_base_url_for_version_sync"] = normalize_base_url(selected_row.get("base_url") or "")
        st.session_state.pop("_auto_version_status", None)
        _iwf = selected_row.get("include_in_workflow_runs")
        try:
            if _iwf is None or (isinstance(_iwf, float) and pd.isna(_iwf)):
                _iwf = True
            else:
                _iwf = bool(int(_iwf))
        except Exception:
            _iwf = bool(_iwf) if _iwf is not None else True
        st.session_state["env_include_workflow_input"] = _iwf

    col1, col2, col3 = st.columns(3)
    env_name = col1.text_input("Environment Name", key="env_name_input")
    base_url = col2.text_input("Base URL", key="env_base_url_input", placeholder="https://test-env.example.com")

    # Auto-fetch version when base URL changes (especially for first-time/new env entry)
    normalized_base_url = normalize_base_url(base_url)
    prev_base_url = st.session_state.get("_prev_base_url_for_version_sync", "")
    if normalized_base_url != prev_base_url:
        st.session_state["_prev_base_url_for_version_sync"] = normalized_base_url
        if normalized_base_url:
            temp_env = {"base_url": normalized_base_url, "env_name": env_name}
            try:
                token = ""
                try:
                    token = fetch_auth_token_for_env(temp_env)
                except Exception:
                    # /api/v1/about can work without auth in some envs.
                    token = ""
                clean_version, raw_version, _ = fetch_core_version_from_api(
                    normalized_base_url, token, INTERNAL_ABOUT_API_PATH
                )
                if clean_version:
                    st.session_state["env_version_input"] = clean_version
                    st.session_state["_auto_version_status"] = (
                        "success",
                        f"Auto-detected core version: {clean_version} (raw: {raw_version})"
                    )
                else:
                    st.session_state["_auto_version_status"] = (
                        "warn",
                        "Base URL changed, but releaseVersion was not found in /api/v1/about response."
                    )
            except Exception as ex:
                st.session_state["_auto_version_status"] = (
                    "warn",
                    f"Base URL changed, but version auto-fetch failed: {ex}"
                )

    status = st.session_state.get("_auto_version_status")
    if status:
        level, msg = status
        if level == "success":
            st.success(msg)
        else:
            st.warning(msg)

    version = col3.text_input("Version", key="env_version_input")

    col4, col5, col6 = st.columns(3)
    auth_username = col4.text_input("Username (optional)", key="env_auth_username_input", help="If blank, defaults to bigid")
    auth_password = col5.text_input("Password (optional)", key="env_auth_password_input", type="password", help="If blank, defaults to bigid111")
    with col6:
        st.markdown("<div style='margin-top: 32px;'></div>", unsafe_allow_html=True)
        is_active = st.checkbox("Active", value=bool(selected_row.get("is_active", True)))
    st.checkbox(
        "Use for workflow runs",
        key="env_include_workflow_input",
        help="When enabled, the workflow runner may assign DS/scan steps to this environment. "
        "Turn off to keep the env active for version/classifier sync and tracking only.",
    )

    if st.button("Save Environment", type="primary"):
        if not env_name.strip():
            st.error("Environment Name is required.")
        elif not base_url.strip():
            st.error("Base URL is required.")
        else:
            try:
                save_environment_credentials(env_name.strip(), auth_username, auth_password)
                resolved_auth_module_path = str(selected_row.get("auth_module_path") or "").strip()
                resolved_auth_function = str(selected_row.get("auth_function_name") or "Get_auth_token").strip()
                resolved_api_path = str(selected_row.get("classifiers_api_path") or INTERNAL_CLASSIFIERS_API_PATH).strip()

                with engine.connect() as conn:
                    conn.execute(text("""
                        INSERT INTO test_environments (
                            env_name, base_url, version, auth_module_path, auth_function_name,
                            classifiers_api_path, is_active, include_in_workflow_runs
                        )
                        VALUES (:n, :u, :v, :ap, :af, :cp, :ia, :iwf)
                        ON DUPLICATE KEY UPDATE
                            base_url = VALUES(base_url),
                            version = VALUES(version),
                            auth_module_path = VALUES(auth_module_path),
                            auth_function_name = VALUES(auth_function_name),
                            classifiers_api_path = VALUES(classifiers_api_path),
                            is_active = VALUES(is_active),
                            include_in_workflow_runs = VALUES(include_in_workflow_runs)
                    """), {
                        "n": env_name.strip(),
                        "u": normalize_base_url(base_url),
                        "v": version.strip() if version else None,
                        "ap": resolved_auth_module_path or None,
                        "af": resolved_auth_function or "Get_auth_token",
                        "cp": resolved_api_path or DEFAULT_CLASSIFIERS_API_PATH,
                        "ia": 1 if is_active else 0,
                        "iwf": 1 if st.session_state.get("env_include_workflow_input", True) else 0,
                    })
                    conn.commit()

                    saved = conn.execute(text("""
                        SELECT id, env_name, base_url, version, classifiers_api_path
                        FROM test_environments
                        WHERE env_name = :n
                        LIMIT 1
                    """), {"n": env_name.strip()}).fetchone()

                st.success("Environment saved.")

                # Auto-sync classifiers to normalized build mapping on save
                if saved:
                    saved_row = {
                        "id": saved[0],
                        "env_name": saved[1],
                        "base_url": saved[2],
                        "version": saved[3],
                        "classifiers_api_path": saved[4] or INTERNAL_CLASSIFIERS_API_PATH
                    }
                    token = ""
                    try:
                        token = fetch_auth_token_for_env(saved_row)
                    except Exception:
                        token = ""

                    effective_version = str(saved_row.get("version") or "").strip()
                    try:
                        core_version, raw_version, _ = fetch_core_version_from_api(saved_row.get("base_url"), token, INTERNAL_ABOUT_API_PATH)
                        if core_version:
                            effective_version = core_version
                            st.info(f"Core version detected: {core_version} (raw: {raw_version})")
                    except Exception:
                        pass

                    try:
                        classifier_names, total_count, _ = fetch_classifiers_with_auth_retry(saved_row)
                        if classifier_names:
                            set_id, set_size, _new_cls_from_set = create_or_get_classifier_set(classifier_names)
                            record_environment_version(
                                saved_row.get("id"),
                                saved_row.get("env_name"),
                                effective_version,
                                classifier_set_id=set_id
                            )
                            sync_stats = sync_build_classifier_mappings(
                                effective_version,
                                classifier_names,
                                environment_id=saved_row.get("id")
                            )
                            st.success(
                                f"Classifier sync complete for build {effective_version or 'N/A'}: "
                                f"{len(classifier_names)} fetched"
                                f"{f' (totalCount={total_count})' if total_count is not None else ''}, "
                                f"set size {set_size}, "
                                f"{sync_stats['total_links_for_build']} mapped for this environment."
                            )
                            try:
                                from tracker import sync_classifier_details_for_env_build

                                meta_row = {
                                    "id": saved_row.get("id"),
                                    "env_name": saved_row.get("env_name"),
                                    "base_url": saved_row.get("base_url"),
                                    "classifiers_api_path": saved_row.get("classifiers_api_path"),
                                }
                                if token and effective_version:
                                    sync_classifier_details_for_env_build(
                                        engine, meta_row, token, str(effective_version), force=True
                                    )
                                    st.caption("Classifier **metadata snapshots** refreshed for Deep-Dive / build diffs.")
                            except Exception as mex:
                                st.caption(f"Metadata snapshot refresh failed (list still synced): {mex}")
                        else:
                            st.warning("Environment saved, but classifier API returned no names.")
                    except Exception as ex:
                        st.warning(f"Environment saved, but classifier sync failed: {ex}")
                
                # Clear the cache so the updated active state renders immediately 
                load_test_environments.clear()
                st.rerun()
            except Exception as e:
                st.error(f"Error saving environment: {e}")

    if not env_df.empty:
        show_df = env_df.copy()
        show_df["is_active"] = show_df["is_active"].apply(lambda x: bool(x))
        if "include_in_workflow_runs" not in show_df.columns:
            show_df["include_in_workflow_runs"] = True
        else:
            show_df["include_in_workflow_runs"] = pd.to_numeric(
                show_df["include_in_workflow_runs"], errors="coerce"
            ).fillna(1).astype(int).ne(0)
        build_paths = load_environment_build_change_paths()
        def run_env_actions(fetch_row, do_sync=False, do_fetch=False):
            env_name_val = str(fetch_row.get("env_name") or "").strip()
            effective_version = str(fetch_row.get("version") or "").strip()
            try:
                # Ensure env-wise credentials exist in .env (defaults if not provided)
                saved_u, saved_p = get_saved_environment_credentials(env_name_val)
                save_environment_credentials(env_name_val, saved_u, saved_p)

                data_changed = False
                token = ""
                try:
                    token = fetch_auth_token_for_env(fetch_row)
                except Exception:
                    token = ""

                if do_sync:
                    clean_version, raw_version, about_url = fetch_core_version_from_api(
                        fetch_row.get("base_url"), token, INTERNAL_ABOUT_API_PATH
                    )
                    if clean_version:
                        # Hard guard: Sync must not mutate data when version is unchanged.
                        previous_version = get_environment_current_version(fetch_row.get("id")) or str(fetch_row.get("version") or "").strip()
                        effective_version = clean_version

                        if clean_version != previous_version:
                            st.success(f"[{env_name_val}] Core version changed: {previous_version or 'None'} -> {clean_version} (raw: {raw_version})")
                            # Only fetch classifiers on sync when version changed.
                            do_fetch = True
                        else:
                            copied = clone_build_mappings_to_environment(fetch_row.get("id"), clean_version)
                            if copied > 0:
                                st.success(f"[{env_name_val}] Version unchanged ({clean_version}). Reused {copied} existing classifier mappings for this environment.")
                            else:
                                st.session_state["env_action_level"] = "info"
                                st.session_state["env_action_msg"] = f"[{env_name_val}] No version change ({clean_version}). Classifier sync skipped."
                                st.info(st.session_state["env_action_msg"])
                            try:
                                from tracker import sync_classifier_details_for_env_build

                                meta_row = {
                                    "id": fetch_row.get("id"),
                                    "env_name": fetch_row.get("env_name"),
                                    "base_url": fetch_row.get("base_url"),
                                    "classifiers_api_path": fetch_row.get("classifiers_api_path"),
                                }
                                if token and clean_version:
                                    sync_classifier_details_for_env_build(
                                        engine, meta_row, token, str(clean_version), force=False
                                    )
                            except Exception as mex:
                                st.caption(f"[{env_name_val}] Metadata snapshot check failed: {mex}")
                            if copied > 0:
                                st.cache_data.clear()
                            return
                    else:
                        st.warning(f"[{env_name_val}] Could not read releaseVersion from API response. URL: {about_url}")

                if do_fetch:
                    classifier_names, total_count, api_url = fetch_classifiers_with_auth_retry(fetch_row)
                    if not classifier_names:
                        st.warning(f"[{env_name_val}] API call succeeded but no classifier names were found. URL: {api_url}")
                    else:
                        set_id, set_size, _new_cls_from_set = create_or_get_classifier_set(classifier_names)
                        changed, previous = record_environment_version(
                            fetch_row.get("id"),
                            fetch_row.get("env_name"),
                            effective_version,
                            classifier_set_id=set_id
                        )
                        if changed:
                            if str(previous or "") == str(effective_version or ""):
                                st.info(f"[{env_name_val}] Classifier set linked for build {effective_version}.")
                            else:
                                st.info(f"[{env_name_val}] Build version updated: {previous or 'None'} -> {effective_version}")
                        sync_stats = sync_build_classifier_mappings(
                            effective_version,
                            classifier_names,
                            environment_id=fetch_row.get("id")
                        )
                        data_changed = data_changed or changed or sync_stats["new_links"] > 0 or sync_stats["new_classifiers"] > 0
                        st.success(
                            f"[{env_name_val}] Classifier sync for build {effective_version or 'N/A'}: "
                            f"{len(classifier_names)} fetched"
                            f"{f' (totalCount={total_count})' if total_count is not None else ''}, "
                            f"set size {set_size}, "
                            f"{sync_stats['total_links_for_build']} mapped for this environment."
                        )
                        try:
                            from tracker import sync_classifier_details_for_env_build

                            meta_row = {
                                "id": fetch_row.get("id"),
                                "env_name": fetch_row.get("env_name"),
                                "base_url": fetch_row.get("base_url"),
                                "classifiers_api_path": fetch_row.get("classifiers_api_path"),
                            }
                            if token and effective_version:
                                sync_classifier_details_for_env_build(
                                    engine, meta_row, token, str(effective_version), force=True
                                )
                                st.caption(
                                    f"[{env_name_val}] Classifier **metadata snapshots** refreshed (compare builds under Classifier Changes)."
                                )
                        except Exception as mex:
                            st.caption(f"[{env_name_val}] Metadata snapshots failed: {mex}")
                        st.session_state["env_action_level"] = "success"
                        st.session_state["env_action_msg"] = (
                            f"[{env_name_val}] Sync done for version {effective_version}: "
                            f"{len(classifier_names)} classifiers mapped for this environment."
                        )
                if data_changed:
                    st.cache_data.clear()
            except Exception as ex:
                st.error(f"[{env_name_val}] Action failed: {ex}")
                st.session_state["env_action_level"] = "warning"
                st.session_state["env_action_msg"] = f"[{env_name_val}] Action failed: {ex}"

        # Render a compact inline table-like layout with native buttons (no full page refresh)
        h1, h2, h3, h4, h5, h6, h7, h8 = st.columns([1.2, 2.9, 0.75, 0.55, 0.55, 2.0, 1.05, 1.05])
        h1.markdown("**env_name**")
        h2.markdown("**base_url**")
        h3.markdown("**version**")
        h4.markdown("**active**")
        h5.markdown("**WF**")
        h6.markdown("**Build path**")
        h7.markdown("**Sync ver**")
        h8.markdown("**Classifiers**")
        st.divider()

        for _, r in show_df.iterrows():
            row_dict = r.to_dict()
            is_env_active = bool(row_dict.get("is_active"))
            opacity = "1.0" if is_env_active else "0.4"
            row_style = f'opacity: {opacity};'
            if not is_env_active:
                row_style += ' filter: grayscale(80%); transition: opacity 0.3s;'
            
            row_dict["classifiers_api_path"] = str(row_dict.get("classifiers_api_path") or INTERNAL_CLASSIFIERS_API_PATH).strip()
            
            # Use a container for styling if possible, but columns in Streamlit are tricky with CSS.
            # We'll apply styling to markdown where we can, or just keep it simple with text color if needed.
            # For now, we'll prefix names to indicate status and use opacity in the columns.
            
            c1, c2, c3, c4, c5, c6, c7, c8 = st.columns([1.2, 2.9, 0.75, 0.55, 0.55, 2.0, 1.05, 1.05])
            
            name_display = str(row_dict.get("env_name") or "")
            if not is_env_active:
                name_display = f"~~{name_display}~~ (Inactive)"
            
            c1.markdown(f'<div style="{row_style}">{name_display}</div>', unsafe_allow_html=True)
            c2.markdown(f'<div style="{row_style} font-size: 0.85rem;">{str(row_dict.get("base_url") or "")}</div>', unsafe_allow_html=True)
            c3.markdown(f'<div style="{row_style}">{str(row_dict.get("version") or "")}</div>', unsafe_allow_html=True)
            c4.markdown(f'<div style="{row_style}">{"✅" if is_env_active else "❌"}</div>', unsafe_allow_html=True)
            _iwf_show = bool(row_dict.get("include_in_workflow_runs", True))
            c5.markdown(
                f'<div style="{row_style}" title="Use for workflow runs">{"✅" if _iwf_show else "—"}</div>',
                unsafe_allow_html=True,
            )
            
            env_id = row_dict.get("id")
            path_text = build_paths.get(int(env_id), f"None -> {str(row_dict.get('version') or '').strip()}") if env_id is not None else f"None -> {str(row_dict.get('version') or '').strip()}"
            c6.markdown(f'<div style="{row_style} font-size: 0.8rem; color: #64748b;">{path_text}</div>', unsafe_allow_html=True)
            c7.markdown(
                """
                <style>
                div[data-testid="stButton"] > button[kind="secondary"] {
                    min-width: 120px !important;
                    padding: 0.35rem 0.75rem !important;
                }
                </style>
                """,
                unsafe_allow_html=True
            )
            if c7.button("Sync Core Ver", key=f"sync_btn_{int(row_dict.get('id'))}"):
                run_env_actions(row_dict, do_sync=True, do_fetch=False)
            c8.markdown(
                """
                <style>
                div[data-testid="stButton"] > button[kind="secondary"] {
                    min-width: 120px !important;
                    padding: 0.35rem 0.75rem !important;
                }
                </style>
                """,
                unsafe_allow_html=True
            )
            if c8.button("Get Classifiers", key=f"fetch_btn_{int(row_dict.get('id'))}"):
                run_env_actions(row_dict, do_sync=False, do_fetch=True)
            st.divider()
    else:
        st.info("No environments found yet. Add one above.")

    st.subheader("Current Environment Classifier Status")
    status_df = load_current_environment_classifier_status()
    if status_df.empty:
        st.caption("No environment classifier status available yet.")
    else:
        display_df = status_df.copy().rename(columns={
            "env_name": "Env",
            "base_url": "URL",
            "version": "Ver",
            "is_active": "Active",
            "classifier_count": "Cls Cnt",
            "classifier_list": "Classifiers"
        })

        rows_html = []
        for _, r in display_df.iterrows():
            cls_raw = str(r.get("Classifiers") or "").strip()
            if cls_raw:
                cls_html = "<br>".join(html.escape(x.strip()) for x in cls_raw.split(",") if x.strip())
            else:
                cls_html = ""
            cls_cnt_val = r.get("Cls Cnt")
            if pd.isna(cls_cnt_val):
                cls_cnt_text = ""
            else:
                cls_cnt_text = str(int(cls_cnt_val)) if str(cls_cnt_val).replace(".", "", 1).isdigit() else str(cls_cnt_val)
            rows_html.append(
                "<tr>"
                f"<td>{html.escape(str(r.get('Env') or ''))}</td>"
                f"<td>{html.escape(str(r.get('URL') or ''))}</td>"
                f"<td>{html.escape(str(r.get('Ver') or ''))}</td>"
                f"<td>{'Yes' if bool(r.get('Active')) else 'No'}</td>"
                f"<td>{html.escape(cls_cnt_text)}</td>"
                f"<td class='cls-wrap'>{cls_html}</td>"
                "</tr>"
            )

        history_html = dedent(f"""
        <style>
          .env-history-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
          }}
          .env-history-table th, .env-history-table td {{
            border: 1px solid #e5e7eb;
            padding: 6px 8px;
            text-align: left;
            vertical-align: top;
          }}
          .env-history-table th {{
            background: #f8fafc;
            font-weight: 600;
          }}
          .env-history-table .cls-wrap {{
            white-space: normal;
            word-break: break-word;
            line-height: 1.35;
            max-width: 620px;
          }}
        </style>
        <table class="env-history-table">
          <thead>
            <tr>
              <th>Env</th>
              <th>URL</th>
              <th>Ver</th>
              <th>Active</th>
              <th>Cls Cnt</th>
              <th>Classifiers</th>
            </tr>
          </thead>
          <tbody>
            {''.join(rows_html)}
          </tbody>
        </table>
        """)
        st.markdown(history_html, unsafe_allow_html=True)


def page_version_comparison():
    st.title("🧬 Version Comparison")
    st.markdown("Compare classifiers across different **build versions**.")

    stats = load_build_version_classifier_stats()
    if stats.empty:
        st.info("No build-wise classifier data found yet. Run Sync/Get Classifiers first.")
        return

    # Line chart: number of classifiers over time
    st.subheader("Classifier Count Trend")
    env_options = sorted(stats["env_name"].dropna().astype(str).unique().tolist())
    selected_envs = st.multiselect("Environment Filter", env_options, default=env_options[:1] if env_options else [])
    trend_df = stats.copy()
    if selected_envs:
        trend_df = trend_df[trend_df["env_name"].isin(selected_envs)]
    trend_df["build_version"] = trend_df["build_version"].astype(str).str.strip()
    trend_df = trend_df[(trend_df["build_version"] != "") & (trend_df["build_version"].str.lower() != "nan")]
    trend_df["last_synced_at"] = pd.to_datetime(trend_df["last_synced_at"], errors="coerce")
    trend_df = trend_df.dropna(subset=["last_synced_at"])

    def version_sort_key(v):
        s = str(v or "").strip()
        parts = s.split(".")
        nums = []
        for p in parts:
            p = p.strip()
            if p.isdigit():
                nums.append(int(p))
            else:
                # keep non-numeric versions stable but after numeric
                return (1, s.lower())
        return (0, tuple(nums), s)

    if not trend_df.empty:
        ordered_builds = sorted(trend_df["build_version"].unique().tolist(), key=version_sort_key)
        # Keep x-axis strictly categorical (string labels), never numeric.
        trend_df["build_version_label"] = trend_df["build_version"].astype(str)
        trend_df["build_version_label"] = pd.Categorical(
            trend_df["build_version_label"],
            categories=ordered_builds,
            ordered=True
        )
        fig = px.line(
            trend_df.sort_values(["build_version_label", "last_synced_at"]),
            x="build_version_label",
            y="classifier_count",
            color="env_name",
            markers=True,
            title="Number of Classifiers by Build Version"
        )
        fig.update_xaxes(type="category", categoryorder="array", categoryarray=ordered_builds, title_text="Build Version")
        fig.update_yaxes(title_text="Classifier Count")
        st.plotly_chart(fig, width="stretch")
    else:
        st.caption("No trend data available.")

    st.divider()
    st.subheader("Build-to-Build Comparison")
    cmp_df = stats.copy()
    if cmp_df.empty:
        st.info("Need at least two build versions for comparison.")
        return

    selected_env = st.selectbox("Environment", env_options, key="build_cmp_env")
    env_df = cmp_df[cmp_df["env_name"] == selected_env].copy()
    build_versions = env_df["build_version"].dropna().astype(str).unique().tolist()
    if len(build_versions) < 2:
        st.info("Need at least two build versions in selected environment.")
        return

    c1, c2 = st.columns(2)
    baseline_build = c1.selectbox("Baseline Build", build_versions, index=0, key="build_cmp_base")
    candidate_build = c2.selectbox("Candidate Build", build_versions, index=1 if len(build_versions) > 1 else 0, key="build_cmp_cand")

    env_id = int(env_df.iloc[0]["environment_id"])
    base_set = set(get_classifier_names_for_env_version(env_id, baseline_build))
    cand_set = set(get_classifier_names_for_env_version(env_id, candidate_build))

    added = sorted(cand_set - base_set)
    removed = sorted(base_set - cand_set)
    common = sorted(base_set & cand_set)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Baseline Count", len(base_set))
    m2.metric("Candidate Count", len(cand_set))
    m3.metric("Added", len(added))
    m4.metric("Removed", len(removed))

    st.caption(
        f"Comparing `{selected_env} {baseline_build}` vs `{selected_env} {candidate_build}`"
    )

    t1, t2, t3 = st.columns(3)
    with t1:
        st.markdown("**Added in Candidate**")
        st.dataframe(pd.DataFrame({"Classifier": added}) if added else pd.DataFrame({"Classifier": []}), width="stretch")
    with t2:
        st.markdown("**Removed from Baseline**")
        st.dataframe(pd.DataFrame({"Classifier": removed}) if removed else pd.DataFrame({"Classifier": []}), width="stretch")
    with t3:
        st.markdown("**Common**")
        st.dataframe(pd.DataFrame({"Classifier": common}) if common else pd.DataFrame({"Classifier": []}), width="stretch")


@st.fragment
def page_scan_status():
    st.title("🛰️ Scan Status")
    st.caption("Combines in-progress and completed parent scans, merged by `_id`.")

    ensure_scan_tracking_tables()
    ensure_auto_scan_benchmarks_table()
    ensure_scan_progress_table()

    env_df = load_test_environments()
    if env_df.empty:
        st.info("No test environments found. Add one in `Manage Test Environments` first.")
        return

    work_df = env_df.copy()
    work_df["is_active"] = work_df["is_active"].apply(lambda x: bool(x))
    work_df = work_df.sort_values(by=["is_active", "env_name"], ascending=[False, True]).reset_index(drop=True)

    labels = [
        f"{row.env_name} ({'active' if row.is_active else 'inactive'}) - {row.base_url or 'no-base-url'}"
        for row in work_df.itertuples()
    ]
    selected_label = st.selectbox("Environment", labels, index=0)
    selected_idx = labels.index(selected_label)
    selected_env = work_df.iloc[selected_idx].to_dict()

    c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.0, 1.0])
    status_view = c1.radio("Show", ["All", "In Progress", "Completed"], horizontal=True)
    limit = c2.number_input("Limit per API", min_value=10, max_value=1000, value=200, step=10)
    refresh = c3.button("Reload from DB", type="primary", width="stretch")
    manual_resync = c4.button("Manual Re-sync (API)", width="stretch")
    load_insights = True

    if refresh:
        try:
            st.cache_data.clear()
        except Exception:
            pass
        st.rerun()

    running_states = [
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
    completed_states = ["Failed", "CompletedWithFailures", "Completed", "Stopped", "Aborted", "Cancelled", "Canceled"]
    def _compact_state(v: str) -> str:
        return str(v or "").strip().lower().replace(" ", "")
    running_compact = {_compact_state(x) for x in running_states}
    completed_compact = {_compact_state(x) for x in completed_states}

    active_envs = [
        row._asdict()
        for row in work_df.itertuples()
        if bool(row.is_active)
    ]
    sync_batch_per_env = 20
    auto_batch_per_env = 8
    # Keep sync light; background tracking should be done by tracker.py (no browser required).
    auto_sync_interval_seconds = 600
    active_env_ids = [int(r.get("id")) for r in active_envs if r.get("id") is not None]
    preloaded_db_items = load_scan_parents_for_environments(
        active_env_ids,
        status_view="All",
        limit=max(int(limit) * max(1, len(active_env_ids)), 500),
    ) if active_env_ids else []
    now_epoch = datetime.now().timestamp()
    last_auto_sync_epoch = float(st.session_state.get("scan_status_last_auto_sync_epoch", 0.0) or 0.0)
    # DB-only view. Tracker is the only updater.
    should_auto_sync = False

    # Manual API re-sync (event-driven). This updates scan_parents in DB for selected environment.
    # We keep it light: just refresh scan list/status; deep insight snapshotting remains per-scan.
    if manual_resync:
        with st.spinner("Re-syncing scan list from API (writing to DB)..."):
            try:
                res = fetch_combined_parent_scans_for_env(selected_env, running_states, completed_states, limit=int(limit))
                api_items = res.get("items") if isinstance(res, dict) else []
                running_total_api = res.get("running_total") if isinstance(res, dict) else None
                completed_total_api = res.get("completed_total") if isinstance(res, dict) else None

                upserted = 0
                for scan in (api_items or []):
                    if not isinstance(scan, dict):
                        continue
                    sid = str(scan.get("_id") or scan.get("id") or "").strip()
                    if not sid:
                        continue
                    status = resolve_scan_status_value(scan)
                    upsert_scan_parent_record(selected_env.get("id"), scan, status)
                    upserted += 1

                # Update totals cache for caption.
                try:
                    per_env_totals_cache[str(int(selected_env.get("id")))] = {
                        "running_total": int(running_total_api) if running_total_api is not None else None,
                        "completed_total": int(completed_total_api) if completed_total_api is not None else None,
                    }
                except Exception:
                    pass
                st.session_state["scan_status_last_auto_sync_human"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

                st.success(f"Manual re-sync complete. Upserted {upserted} scans into DB.")
                try:
                    st.cache_data.clear()
                except Exception:
                    pass
                st.rerun()
            except Exception as ex:
                st.error(f"Manual re-sync failed: {ex}")

    # Note: we intentionally do NOT force auto-refresh here.
    # Continuous tracking without a browser is handled by `tracker.py`.

    per_env_scan_cache = st.session_state.setdefault("scan_status_per_env_items", {})
    per_env_totals_cache = st.session_state.setdefault("scan_status_per_env_totals", {})
    if not active_envs:
        st.warning("No active environments found. Activate at least one environment to auto-sync scans.")
    else:
        st.caption("DB-only view. Progress + insights are updated by `tracker.py`.")

    items = preloaded_db_items if preloaded_db_items else load_scan_parents_for_environments(
        active_env_ids,
        status_view="All",
        limit=max(int(limit) * max(1, len(active_env_ids)), 500),
    )
    # Ensure running scans are not missed: union in latest running scan_progress rows.
    try:
        running_states_local = {
            "started",
            "awaiting",
            "inprogress",
            "in progress",
            "running",
            "queued",
            "pending",
            "stoprequested",
            "stop requested",
            "stopping",
        }
        def _compact(s: str) -> str:
            return str(s or "").strip().lower().replace(" ", "")
        running_compact = {_compact(x) for x in running_states_local}

        # Only treat scan_progress as "running" if the latest snapshot is fresh.
        # Otherwise stale rows can look like extra running scans.
        try:
            stale_minutes = int(os.getenv("SCAN_STATUS_PROGRESS_STALE_MINUTES", "60") or "60")
        except Exception:
            stale_minutes = 60
        now_utc = datetime.utcnow()
        def _age_minutes(pr: dict) -> Optional[float]:
            try:
                ts = pr.get("captured_at_utc") or pr.get("timestamp")
                if ts is None:
                    return None
                # ts is usually a datetime from DB; handle strings too.
                if isinstance(ts, str):
                    dt = pd.to_datetime(ts, errors="coerce")
                    if pd.isna(dt):
                        return None
                    dt = dt.to_pydatetime()
                else:
                    dt = ts
                return (now_utc - dt).total_seconds() / 60.0
            except Exception:
                return None

        progress_latest = load_latest_scan_progress_rows_for_environments(active_env_ids)
        items_by_id = {}
        for it in (items or []):
            try:
                sid = str(it.get("_id") or it.get("id") or "").strip()
            except Exception:
                sid = ""
            if sid and sid not in items_by_id:
                items_by_id[sid] = it
        added_from_progress = 0
        skipped_stale_progress = 0
        for pr in (progress_latest or []):
            sid = str(pr.get("scan_id") or "").strip()
            if not sid:
                continue
            p_state = str(pr.get("scan_state") or "").strip()
            if _compact(p_state) not in running_compact:
                continue
            age_m = _age_minutes(pr)
            if age_m is not None and age_m > float(stale_minutes):
                skipped_stale_progress += 1
                continue
            if sid in items_by_id:
                # If scan_parents state is stale/non-running but progress shows running, trust progress.
                try:
                    cur = items_by_id[sid]
                    cur_state = resolve_scan_status_value(cur)
                    if _compact(cur_state) not in running_compact:
                        cur["state"] = p_state
                except Exception:
                    pass
                continue
            env_id = pr.get("environment_id")
            env_name = ""
            try:
                env_name = str(next((e.get("env_name") for e in active_envs if int(e.get("id")) == int(env_id)), "") or "")
            except Exception:
                env_name = ""
            items.append({
                "_id": sid,
                "name": pr.get("scan_name") or "",
                "scanName": pr.get("scan_name") or "",
                "environment_id": env_id,
                "environment_name": env_name,
                "state": p_state,
            })
            added_from_progress += 1
        if added_from_progress:
            st.caption(f"Added {added_from_progress} running scans from scan_progress (scan_parents was missing them).")
        if skipped_stale_progress and st.query_params.get("debug") == "true":
            st.caption(f"Skipped {skipped_stale_progress} stale running rows from scan_progress (>{stale_minutes} min old).")
    except Exception:
        pass
    running_total = None
    completed_total = None
    try:
        running_total = sum(
            int((per_env_totals_cache.get(str(eid), {}) or {}).get("running_total") or 0)
            for eid in active_env_ids
        )
        completed_total = sum(
            int((per_env_totals_cache.get(str(eid), {}) or {}).get("completed_total") or 0)
            for eid in active_env_ids
        )
    except Exception:
        running_total = None
        completed_total = None
    loaded_at = str(st.session_state.get("scan_status_last_auto_sync_human") or "").strip()

    st.caption(
        f"Loaded {len(items)} scans across active environments"
        + (f" | running totalCount={running_total}" if running_total is not None else "")
        + (f" | completed totalCount={completed_total}" if completed_total is not None else "")
        + (f" | last sync at {loaded_at}" if loaded_at else "")
    )

    # Row-level manual sync via query param from table link.
    try:
        sync_scan_id = str(st.query_params.get("sync_scan_id", "") or "").strip()
    except Exception:
        sync_scan_id = ""
    if sync_scan_id:
        sync_msg = st.info(f"Syncing scan insights for `{sync_scan_id}` ...")
        try:
            target_env_row = get_environment_row_for_scan_id(sync_scan_id) or selected_env
            insight_payload, _insight_url = fetch_parent_scan_insight_with_auth_retry(target_env_row, sync_scan_id)
            new_cnt, removed_cnt, attrs_payload, _attrs_url = fetch_parent_scan_attribute_counts_with_auth_retry(
                target_env_row, sync_scan_id, limit=200
            )
            insight_metrics = extract_scan_insight_metrics(insight_payload if isinstance(insight_payload, dict) else {})
            insight_metrics["new_attributes_count"] = new_cnt
            insight_metrics["removed_attributes_count"] = removed_cnt
            upsert_scan_insight_record(
                sync_scan_id,
                insight_metrics,
                insight_payload=insight_payload if isinstance(insight_payload, dict) else None,
                attributes_payload=attrs_payload if isinstance(attrs_payload, dict) else None,
            )
            sync_msg.success(f"Synced scan `{sync_scan_id}` successfully.")
        except Exception as ex:
            sync_msg.error(f"Failed to sync scan `{sync_scan_id}`: {ex}")
        # Remove one-shot param so it doesn't re-run on every rerender.
        try:
            del st.query_params["sync_scan_id"]
        except Exception:
            pass

    if not items:
        st.info("No scans found in DB for active environments. Please click 'Refresh Scans' to pull data from API.")
        return
    
    st.caption(f"Showing {len(items)} scans from DB for active environments.")

    env_by_id = {}
    for er in active_envs:
        try:
            env_by_id[int(er.get("id"))] = er
        except Exception:
            continue

    scan_ids_all = []
    for scan in items:
        if not isinstance(scan, dict):
            continue
        sid = str(scan.get("_id") or scan.get("id") or "").strip()
        if sid:
            scan_ids_all.append(sid)
    latest_progress_state_map = load_latest_scan_progress_state_map(scan_ids_all)
    db_insight_map = load_scan_insight_map_for_scan_ids(scan_ids_all) if load_insights else {}
    saved_ids = get_saved_benchmark_scan_ids()

    summary_rows = []
    for scan in items:
        if not isinstance(scan, dict):
            continue
        state = resolve_scan_status_value(scan)
        scan_id = str(scan.get("_id") or scan.get("id") or "").strip()
        scan_env_name = str(scan.get("environment_name") or "").strip()
        scan_env_id = scan.get("environment_id")
        try:
            scan_env_row = env_by_id.get(int(scan_env_id)) if scan_env_id is not None else None
        except Exception:
            scan_env_row = None

        # Merge scan_progress only when it shows *running* and the parent row lags (non-running).
        # Never promote to terminal from scan_progress alone: local snapshots can be wrong/stale and
        # previously overwrote scan_parents via upsert — false "Completed" while BigID was still In Progress.
        try:
            p_state = str((latest_progress_state_map or {}).get(scan_id) or "").strip()
            if p_state and (_compact_state(p_state) in running_compact) and (_compact_state(state) not in running_compact) and (status_view != "Completed"):
                state = p_state
        except Exception:
            pass

        if status_view == "In Progress" and _compact_state(state) not in running_compact:
            continue
        if status_view == "Completed" and _compact_state(state) not in completed_compact:
            continue

        # UI normalization:
        # - keep "Stop Requested"/"Stopping" explicit (important diagnostic)
        # - otherwise treat "running" variants as "In Progress"
        cs = _compact_state(state)
        if cs in {"stoprequested", "stopping"}:
            display_state = "Stop Requested" if cs == "stoprequested" else "Stopping"
        else:
            display_state = "In Progress" if cs in running_compact else (state or "Unknown")

        insight_metrics = {
            "distinct_classification_attributes": "",
            "classifier_attribute_names": "",
            "data_source_names": "",
            "objects_with_pi": "",
            "objects_scanned": "",
            "objects_enumerated": "",
            "total_classification": "",
            "new_attributes_count": "",
            "removed_attributes_count": "",
        }

        if load_insights and scan_id:
            db_metrics = db_insight_map.get(scan_id)
            if db_metrics:
                # dict.get(k, default) returns None when key exists with value None — normalize for display.
                def _im_val(d: dict, k: str, dflt: str = ""):
                    v = d.get(k)
                    return dflt if v is None else v

                insight_metrics.update({
                    "distinct_classification_attributes": _im_val(db_metrics, "distinct_classification_attributes", ""),
                    "classifier_attribute_names": _im_val(db_metrics, "classifier_attribute_names", ""),
                    "data_source_names": _im_val(db_metrics, "data_source_names", ""),
                    "objects_with_pi": _im_val(db_metrics, "objects_with_pi", ""),
                    "objects_scanned": _im_val(db_metrics, "objects_scanned", ""),
                    "objects_enumerated": _im_val(db_metrics, "objects_enumerated", ""),
                    "total_classification": _im_val(db_metrics, "total_classification", ""),
                    "new_attributes_count": _im_val(db_metrics, "new_attributes_count", ""),
                    "removed_attributes_count": _im_val(db_metrics, "removed_attributes_count", ""),
                })

        started_at = scan.get("startedAt") or scan.get("startTime") or scan.get("createdAt")
        completed_at = scan.get("completedAt") or scan.get("endTime") or scan.get("endedAt")

        raw_scan_type = scan.get("type") or ""
        tmpl_name = scan.get("scanTemplateName") or ""
        cs_type = str(raw_scan_type or "").strip().lower().replace(" ", "")
        cs_tmpl = str(tmpl_name or "").strip().lower()
        if cs_type in {"dsscan", "fullscan", "full_scan"} or ("data source scan" in cs_tmpl and cs_type in {"", "dsscan"}):
            display_scan_type = "Full Scan"
        elif cs_type:
            display_scan_type = str(raw_scan_type or "").strip()
        else:
            display_scan_type = ""

        summary_rows.append({
            "Environment": scan_env_name or str((scan_env_row or {}).get("env_name") or selected_env.get("env_name") or ""),
            "Scan Name": scan.get("name") or scan.get("scanName") or scan.get("displayName") or "",
            "Scan Id": scan_id,
            "Scan Type": display_scan_type,
            "Start Time": started_at or "",
            "End Time": completed_at or "",
            "Template": scan.get("scanTemplateName") or "",
            "Scan Status": display_state,
            "Data Sources": insight_metrics["data_source_names"],
            "Distinct Classification Attributes": insight_metrics["distinct_classification_attributes"],
            "Classifier Attribute Names": insight_metrics["classifier_attribute_names"],
            "Objects With Pi": insight_metrics["objects_with_pi"],
            "Objects Scanned": insight_metrics["objects_scanned"],
            "Objects Enumerated": insight_metrics["objects_enumerated"],
            "Total Classification": insight_metrics["total_classification"],
            "New Attributes": insight_metrics["new_attributes_count"],
            "Removed Attributes": insight_metrics["removed_attributes_count"],
            "Scan Time": format_scan_duration(
                started_at, completed_at, is_running=(cs in running_compact)
            ),
        })

    summary_df = pd.DataFrame(summary_rows)
    if summary_df.empty:
        st.info(f"No scans match `{status_view}` in combined results.")
        return

    # Arrow-safe numeric columns (avoid mixing '' strings with ints).
    for col in [
        "Distinct Classification Attributes",
        "Objects With Pi",
        "Objects Scanned",
        "Objects Enumerated",
        "Total Classification",
        "New Attributes",
        "Removed Attributes",
    ]:
        if col in summary_df.columns:
            summary_df[col] = pd.to_numeric(summary_df[col], errors="coerce").astype("Int64")

    scan_ids = [str(x).strip() for x in summary_df["Scan Id"].tolist() if str(x).strip()]
    active_map = get_scan_parent_active_map(scan_ids)
    summary_df.insert(0, "Active", summary_df["Scan Id"].map(lambda sid: active_map.get(str(sid), True)))
    
    # Filter out completed scans (no need for Done column since they're hidden)
    done_scan_ids = {str(sid).strip().lower() for sid in summary_df["Scan Id"] if str(sid).strip().lower() in saved_ids}
    summary_df = summary_df[~summary_df["Scan Id"].apply(lambda sid: str(sid).strip().lower() in saved_ids)]
    
    summary_df.insert(1, "Open", summary_df.apply(lambda r: f"?page=Add+New+Benchmark&scan_id={r['Scan Id']}" if str(r['Scan Id']).strip() else "", axis=1))
    summary_df.insert(2, "Sync", summary_df["Scan Id"].map(lambda sid: f"?page=Scan+Status&sync_scan_id={sid}" if str(sid).strip() else ""))
    progress_ids = get_scans_with_progress_ids()
    def _progress_link(row):
        sid = str(row.get("Scan Id") or "").strip()
        if not sid:
            return ""
        state = str(row.get("Scan Status") or "").strip()
        # Show link if we have history already OR the scan is currently running.
        if sid in progress_ids or _compact_state(state) in running_compact:
            return f"?page=Scan+Progress&scan_id={sid}"
        return ""
    summary_df.insert(3, "Progress", summary_df.apply(_progress_link, axis=1))

    # Debug Mode
    if st.query_params.get("debug") == "true":
        st.divider()
        st.subheader("🛠️ Debug Information")
        st.caption(f"Status View: `{status_view}` | Running States: `{running_states}`")
        if items:
            debug_df = pd.DataFrame([
                {
                    "ID": s.get("_id") or s.get("id"),
                    "Name": s.get("name") or s.get("scanName"),
                    "State": s.get("state"),
                    "Status": s.get("status"),
                    "ProgState": (s.get("progress") or {}).get("state") if isinstance(s.get("progress"), dict) else "N/A",
                    "EnvID": s.get("environment_id")
                }
                for s in items
            ])
            st.dataframe(debug_df, width="stretch")
        else:
            st.write("Items list is empty.")

    st.markdown("### Scan Summary")
    summary_column_order = [
        "Active",
        "Open",
        "Sync",
        "Progress",
        "Scan Status",
        "Environment",
        "Scan Name",
        "Scan Id",
        "Scan Type",
        "Start Time",
        "End Time",
        "Template",
        "Data Sources",
        "Distinct Classification Attributes",
        "Classifier Attribute Names",
        "Objects With Pi",
        "Objects Scanned",
        "Objects Enumerated",
        "Total Classification",
        "New Attributes",
        "Removed Attributes",
        "Scan Time",
    ]
    summary_column_order = [c for c in summary_column_order if c in summary_df.columns]
    
    # Apply row styling based on Scan Status
    def style_by_status(row):
        status = str(row.get("Scan Status", "")).lower()
        if "completedwithfailures" in status.replace(" ", ""):
            return ['background-color: #ffe4cc'] * len(row)  # Light orange/amber
        elif "completed" in status or "success" in status:
            return ['background-color: #d4edda'] * len(row)  # Light green
        elif "running" in status or "in progress" in status:
            return ['background-color: #fff3cd'] * len(row)  # Light yellow
        elif "failed" in status or "error" in status:
            return ['background-color: #f8d7da'] * len(row)  # Light red
        else:
            return [''] * len(row)
    
    styled_df = summary_df.style.apply(style_by_status, axis=1)
    
    edited_summary = st.data_editor(
        styled_df,
        width="stretch",
        hide_index=True,
        num_rows="fixed",
        column_order=summary_column_order,
        column_config={
            "Active": st.column_config.CheckboxColumn("Active"),
            "Open": st.column_config.LinkColumn("Open", display_text="Open"),
            "Sync": st.column_config.LinkColumn("Sync", display_text="Sync"),
            "Progress": st.column_config.LinkColumn("Progress", display_text="View"),
            "Environment": st.column_config.TextColumn("Environment"),
        },
        disabled=[c for c in summary_df.columns if c not in ["Active"]]
    )
    try:
        for _, r in edited_summary.iterrows():
            sid = str(r.get("Scan Id") or "").strip()
            if not sid:
                continue
            old_val = bool(active_map.get(sid, True))
            new_val = bool(r.get("Active"))
            if new_val != old_val:
                set_scan_parent_active(sid, new_val)
                active_map[sid] = new_val
    except Exception:
        pass
    with st.expander("Auto Trend Rows (from scans)", expanded=False):
        preview_df = load_auto_scan_benchmarks_preview(selected_env.get("id"), limit=300)
        if preview_df.empty:
            st.caption("No auto trend rows yet.")
        else:
            st.dataframe(preview_df, width="stretch", hide_index=True)

    st.markdown("### Scan Details")
    shown = 0
    for scan in items:
        if not isinstance(scan, dict):
            continue
        state = resolve_scan_status_value(scan)
        if status_view == "In Progress" and state not in running_states:
            continue
        if status_view == "Completed" and state not in completed_states:
            continue
        shown += 1
        scan_id = scan.get("_id") or scan.get("id") or "N/A"
        name = scan.get("name") or scan.get("scanName") or "Unnamed Scan"
        with st.expander(f"{shown}. {name} | {state or 'Unknown'} | {scan_id}", expanded=False):
            trend_row = load_auto_scan_benchmark_row(scan_id)
            if trend_row:
                st.caption("Auto trend details for this scan")
                st.dataframe(pd.DataFrame([trend_row]), width="stretch", hide_index=True)
            else:
                st.caption("Auto trend row not available yet for this scan.")
            attr_rows = get_scan_attribute_findings_prefill(scan_id)
            if attr_rows:
                st.caption("New/Removed classifier attributes for this scan")
                st.dataframe(
                    pd.DataFrame(attr_rows, columns=["Classifier Attribute", "Attribute Value", "Findings"]),
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.caption("No attribute-level findings available for this scan yet.")
            st.caption("Raw scan payload")
            st.json(scan)

# --- Build Analysis Page ---
@st.fragment
def page_build_analysis(df):
    st.markdown("## 🔍 Build Depth-Analysis")
    if df.empty:
        st.warning("No data found.")
        return

    # Select Build
    all_versions = sorted(df['Build_Version'].astype(str).unique(), reverse=True)
    selected_build = st.selectbox("Select Build to Analyze", all_versions)
    
    build_df = df[df['Build_Version'].astype(str) == selected_build].copy()
    
    # Pre-calculate previous build data for regressions
    idx = all_versions.index(selected_build)
    prev_build = all_versions[idx+1] if idx < len(all_versions) - 1 else None
    prev_df = df[df['Build_Version'].astype(str) == prev_build].copy() if prev_build else pd.DataFrame()

    # TABS
    tab_overview, tab_scenario, tab_regressions, tab_ai, tab_details = st.tabs([
        "📊 Overview", "🧩 Scenario Matrix", "⚠️ Regression Hunter", "🤖 AI Diagnostics", "📋 Full Details"
    ])

    with tab_overview:
        st.markdown(f"### Performance Summary: `{selected_build}`")
        # KPIs
        k1, k2, k3, k4 = st.columns(4)
        avg_tp = build_df['throughput_cluster_gb_hr'].mean()
        avg_file_tp = build_df['throughput_cluster_files_hr'].mean()
        total_scans = len(build_df)
        
        # Stability: Pass Rate
        fail_statuses = ['failed', 'error', 'aborted', 'stopped']
        total_runs = len(build_df)
        if total_runs > 0:
            failed_runs = build_df[build_df['Scan_Status'].astype(str).str.lower().isin(fail_statuses)]
            success_rate = ((total_runs - len(failed_runs)) / total_runs) * 100
        else:
            success_rate = 0

        k1.metric("Avg Throughput", f"{avg_tp:.2f} GB/hr")
        k2.metric("Avg Speed", f"{avg_file_tp:,.0f} files/hr")
        k3.metric("Scans Performed", total_scans)
        k4.metric("Stability (Pass %)", f"{success_rate:.1f}%")

        # Historical Trend Line (last 5 builds)
        st.subheader("Historical Benchmark Trend (Overall)")
        recent_versions = all_versions[idx:idx+5]
        trend_df = df[df['Build_Version'].astype(str).isin(recent_versions)].copy()
        v_sorted = sorted(recent_versions) 
        trend_df['Build_Version'] = pd.Categorical(trend_df['Build_Version'], categories=v_sorted, ordered=True)
        trend_summary = trend_df.groupby('Build_Version', observed=True)['throughput_cluster_gb_hr'].mean().reset_index()
        
        fig_trend = px.line(trend_summary, x='Build_Version', y='throughput_cluster_gb_hr', 
                            markers=True, title="Throughput Trend (Last 5 Builds)",
                            labels={'Build_Version': 'Build', 'throughput_cluster_gb_hr': 'GB/hr'})
        fig_trend.update_layout(height=400)
        st.plotly_chart(fig_trend, width='stretch')

        # Basic distribution charts
        c1, c2 = st.columns(2)
        with c1:
            fig1 = px.bar(build_df, x='Scan_Type', y='throughput_cluster_gb_hr', color='file_set_type',
                         barmode='group', title="Throughput (GB/hr) per Scan Category")
            st.plotly_chart(fig1, width='stretch')
        with c2:
            fig2 = px.scatter(build_df, x='scanner_count', y='throughput_cluster_gb_hr', 
                              size='Total_Size_GB', color='Scan_Type',
                              title="Scanner Scaling Efficiency")
            st.plotly_chart(fig2, width='stretch')

    with tab_scenario:
        st.subheader(f"🧩 Scenario Matrix — Build `{selected_build}`")
        st.caption("Side-by-side comparison of all scenario variations for each File Set Type. Compares Base (Full Scan, no flags), ACL On, OLS Scan, and Structured Discovery FF scans.")

        # --- classify each row into a named scenario ---
        def _classify_scenario(row):
            scan_type = str(row.get("Scan_Type") or "").strip().lower()
            is_acl = bool(row.get("is_acl"))
            is_ff  = bool(row.get("is_feature_flags"))
            if "ols" in scan_type:
                return "OLS Scan"
            if is_acl and is_ff:
                return "ACL + Structured FF"
            if is_acl:
                return "ACL On"
            if is_ff:
                return "Structured FF"
            return "Base (Full Scan)"

        sm_df = build_df.copy()
        sm_df["Scenario"] = sm_df.apply(_classify_scenario, axis=1)

        # Pick best row per (DS type, file set, scenario)
        metric_cols = ["throughput_cluster_gb_hr", "throughput_cluster_files_hr", "total_scan_hours", "objects_scanned"]
        mgrp_cols   = ["Data_Source_Type", "file_set_type", "Scenario"]
        sm_best = (
            sm_df.groupby(mgrp_cols, group_keys=False, sort=False)
            .apply(lambda g: g.loc[g["throughput_cluster_gb_hr"].idxmax()] if g["throughput_cluster_gb_hr"].notna().any() else g.iloc[0])
            .reset_index(drop=True)
        )

        SCENARIO_ORDER = ["Base (Full Scan)", "ACL On", "OLS Scan", "Structured FF", "ACL + Structured FF"]

        groups = sm_best.groupby(["Data_Source_Type", "file_set_type"])
        if groups.ngroups == 0:
            st.info("No benchmark records found for this build. Add scans via 'Add New Benchmark'.")
        else:
            for (ds_type, file_set), grp in groups:
                with st.expander(f"**{ds_type}** — {file_set}  ({len(grp)} scenario(s))", expanded=True):
                    # Build pivot table
                    rows_out = []
                    base_tp = None
                    for scen in SCENARIO_ORDER:
                        sr = grp[grp["Scenario"] == scen]
                        if sr.empty:
                            continue
                        r = sr.iloc[0]
                        tp   = r.get("throughput_cluster_gb_hr")
                        ftp  = r.get("throughput_cluster_files_hr")
                        hrs  = r.get("total_scan_hours")
                        objs = r.get("objects_scanned")

                        delta_str = ""
                        if base_tp is None and scen == "Base (Full Scan)":
                            base_tp = tp
                        elif base_tp and tp and base_tp > 0:
                            pct = (tp - base_tp) / base_tp * 100
                            arrow = "▲" if pct >= 0 else "▼"
                            delta_str = f"{arrow}{abs(pct):.1f}%"

                        rows_out.append({
                            "Scenario": scen,
                            "GB/hr": f"{tp:.2f}" if pd.notna(tp) else "—",
                            "Files/hr": f"{ftp:,.0f}" if pd.notna(ftp) else "—",
                            "Scan Hrs": f"{hrs:.2f}" if pd.notna(hrs) else "—",
                            "Objects": f"{int(objs):,}" if pd.notna(objs) and objs else "—",
                            "vs Base": delta_str,
                        })

                    if rows_out:
                        disp = pd.DataFrame(rows_out)

                        def _color_delta(val):
                            if not val or val == "":
                                return ""
                            if "▲" in str(val):
                                return "color: green; font-weight: bold;"
                            if "▼" in str(val):
                                return "color: red; font-weight: bold;"
                            return ""

                        st.dataframe(
                            disp.style.applymap(_color_delta, subset=["vs Base"]),
                            hide_index=True,
                            width='stretch',
                        )
                    else:
                        st.caption("No scenario data for this group.")

    with tab_regressions:

        st.subheader("⚠️ Regression Hunter")
        if prev_build:
            st.caption(f"Comparing current build `{selected_build}` vs previous `{prev_build}`")
            
            # Group by config to find regressions
            cur_grp = build_df.groupby(['Scan_Type', 'Data_Source_Type', 'file_set_type'])['throughput_cluster_gb_hr'].mean().reset_index()
            prv_grp = prev_df.groupby(['Scan_Type', 'Data_Source_Type', 'file_set_type'])['throughput_cluster_gb_hr'].mean().reset_index()
            
            merged = pd.merge(cur_grp, prv_grp, on=['Scan_Type', 'Data_Source_Type', 'file_set_type'], suffixes=('_cur', '_prv'))
            merged['Delta %'] = ((merged['throughput_cluster_gb_hr_cur'] - merged['throughput_cluster_gb_hr_prv']) / merged['throughput_cluster_gb_hr_prv'] * 100)
            
            regressions = merged[merged['Delta %'] < -5].sort_values(by='Delta %')
            improvements = merged[merged['Delta %'] > 5].sort_values(by='Delta %', ascending=False)
            
            if not regressions.empty:
                st.error(f"Found {len(regressions)} configurations with >5% performance drop!")
                disp_reg = regressions.copy()
                disp_reg.columns = ["Scan Type", "Source", "File Set", "Current GB/hr", "Prev GB/hr", "Delta %"]
                st.dataframe(disp_reg.style.format({'Delta %': '{:.1f}%', 'Current GB/hr': '{:.2f}', 'Prev GB/hr': '{:.2f}'}), width='stretch', hide_index=True)
            else:
                st.success("No significant regressions detected!")

            if not improvements.empty:
                with st.expander("Show Performance Improvements"):
                    disp_imp = improvements.copy()
                    disp_imp.columns = ["Scan Type", "Source", "File Set", "Current GB/hr", "Prev GB/hr", "Delta %"]
                    st.dataframe(disp_imp.style.format({'Delta %': '{:.1f}%', 'Current GB/hr': '{:.2f}', 'Prev GB/hr': '{:.2f}'}), width='stretch', hide_index=True)
        else:
            st.info("No previous build data available for regression analysis.")

    with tab_ai:
        st.subheader("🤖 AI Diagnostic Assessment")
        if _local_llm_is_configured():
            context_text = f"Build: {selected_build}\nAvg Throughput: {avg_tp:.2f} GB/hr\nStability: {success_rate:.1f}%\n"
            if prev_build:
                reg_list = regressions.to_dict('records')[:8] if 'regressions' in locals() else []
                context_text += f"Comparison with {prev_build}:\n"
                if reg_list:
                    for r in reg_list:
                        context_text += f"- {r['Data_Source_Type']} ({r['Scan_Type']}): {r['Delta %']:.1f}% drop ({r['throughput_cluster_gb_hr_cur']:.2f} vs {r['throughput_cluster_gb_hr_prv']:.2f} GB/hr)\n"
                else:
                    context_text += "- No significant regressions found.\n"
            
            if st.button("Generate Diagnostic Report", type="primary"):
                with st.spinner("Analyzing build diagnostics and performance anomalies..."):
                    sys_prompt = "You are a Senior Performance Architect. Analyze the benchmark deltas between builds and provide concise, high-impact diagnostic insights. Focus on potential root causes for regressions."
                    usr_prompt = f"Data Summary:\n{context_text}\n\nPlease provide: 1) Executive Assessment (1-2 sentences), 2) Top 3 Performance Anomalies/Risks, 3) Suggested architectural areas to investigate."
                    report = generate_local_llm_analysis(sys_prompt, usr_prompt)
                    st.info(report)
        else:
            st.warning("Local LLM not configured. Set LOCAL_LLM_PROVIDER to enable AI insights.")

    with tab_details:
        st.subheader("Raw Run Details")
        detail_cols = ['Scan_Type', 'Data_Source_Type', 'file_set_type', 'throughput_cluster_gb_hr', 'scanner_count', 'thread_count', 'is_acl', 'Scan_Status']
        detail_df = build_df[[c for c in detail_cols if c in build_df.columns]].copy()
        st.dataframe(detail_df, width='stretch', hide_index=True)

# --- Comparison Page (V4.1 Enhanced) ---
def page_comparison(df):
    st.markdown("## ⚖️ Head-to-Head Comparison")
    if df.empty:
        st.warning("No data available.")
        return

    st.markdown("### Select Comparison Criteria")
    st.caption("Compare two different configurations across multiple dimensions")
    
    def _uniq_sorted(vals):
        try:
            out = [str(x) for x in pd.Series(vals).dropna().unique().tolist()]
        except Exception:
            out = [str(x) for x in (vals or [])]
        out = [x.strip() for x in out if str(x).strip()]
        return sorted(set(out))

    # Two columns for A vs B selection
    c1, c2 = st.columns(2)
    
    with c1:
        st.markdown("#### 🅰️ Baseline Configuration")
        # keep explicit values visible (after selections)
        
        ver_a_options = _uniq_sorted(df.get("Build_Version"))
        ver_a = st.selectbox("Build Version", ver_a_options, key='v_a')
        
        # Get available options based on version A
        df_a_filtered = df[df['Build_Version'].astype(str) == str(ver_a)].copy()
        
        ds_a_options = _uniq_sorted(df_a_filtered.get("Data_Source_Type"))
        ds_a = st.selectbox("Data Source", ds_a_options, key='d_a')
        df_a_filtered = df_a_filtered[df_a_filtered['Data_Source_Type'].astype(str) == str(ds_a)]
        
        scan_a_options = _uniq_sorted(df_a_filtered.get("Scan_Type"))
        scan_a = st.selectbox("Scan Type", scan_a_options, key='s_a')
        df_a_filtered = df_a_filtered[df_a_filtered['Scan_Type'].astype(str) == str(scan_a)]

        fs_a_options = _uniq_sorted(df_a_filtered.get("file_set_type"))
        fs_a = st.selectbox("File Set Type", fs_a_options, key='fs_a')
        df_a_filtered = df_a_filtered[df_a_filtered['file_set_type'].astype(str) == str(fs_a)]
        
        # Configuration flags
        st.caption("Configuration Flags (Optional)")
        acl_a = st.selectbox("ACL Filter", ["All", "ACL On", "ACL Off"], key='acl_a')
        ocr_a = st.selectbox("OCR Filter", ["All", "OCR On", "OCR Off"], key='ocr_a')
        ff_a = st.selectbox("FF Filter", ["All", "FF On", "FF Off"], key='ff_a')
        # keep explicit values visible
        
    with c2:
        st.markdown("#### 🅱️ Candidate Configuration")
        # keep explicit values visible (after selections)
        
        ver_b_options = ver_a_options
        ver_b_default_idx = min(1, max(0, len(ver_b_options) - 1))
        ver_b = st.selectbox("Build Version", ver_b_options, key='v_b', index=ver_b_default_idx)
        
        df_b_filtered = df[df['Build_Version'].astype(str) == str(ver_b)].copy()
        
        ds_b_options = _uniq_sorted(df_b_filtered.get("Data_Source_Type"))
        ds_b = st.selectbox("Data Source", ds_b_options, key='d_b')
        df_b_filtered = df_b_filtered[df_b_filtered['Data_Source_Type'].astype(str) == str(ds_b)]
        
        scan_b_options = _uniq_sorted(df_b_filtered.get("Scan_Type"))
        scan_b = st.selectbox("Scan Type", scan_b_options, key='s_b')
        df_b_filtered = df_b_filtered[df_b_filtered['Scan_Type'].astype(str) == str(scan_b)]

        fs_b_options = _uniq_sorted(df_b_filtered.get("file_set_type"))
        fs_b = st.selectbox("File Set Type", fs_b_options, key='fs_b')
        df_b_filtered = df_b_filtered[df_b_filtered['file_set_type'].astype(str) == str(fs_b)]
        
        st.caption("Configuration Flags (Optional)")
        acl_b = st.selectbox("ACL Filter", ["All", "ACL On", "ACL Off"], key='acl_b')
        ocr_b = st.selectbox("OCR Filter", ["All", "OCR On", "OCR Off"], key='ocr_b')
        ff_b = st.selectbox("FF Filter", ["All", "FF On", "FF Off"], key='ff_b')
        # keep explicit values visible

    # Apply filters
    run_a = df_a_filtered.copy()
    run_b = df_b_filtered.copy()
    
    def apply_flag_filter(df_in, col, choice):
        if choice.endswith("On"):
            return df_in[df_in[col] == 1]
        if choice.endswith("Off"):
            return df_in[df_in[col] == 0]
        return df_in

    run_a = apply_flag_filter(run_a, 'is_acl', acl_a)
    run_a = apply_flag_filter(run_a, 'is_ocr', ocr_a)
    run_a = apply_flag_filter(run_a, 'is_feature_flags', ff_a)

    run_b = apply_flag_filter(run_b, 'is_acl', acl_b)
    run_b = apply_flag_filter(run_b, 'is_ocr', ocr_b)
    run_b = apply_flag_filter(run_b, 'is_feature_flags', ff_b)
    
    baseline_title = f"#### 🅰️ Baseline ({scan_a} {ds_a} {fs_a} | ACL {acl_a}) Configuration"
    candidate_title = f"#### 🅱️ Candidate ({scan_b} {ds_b} {fs_b} | ACL {acl_b}) Configuration"
    
    run_a = run_a.sort_values('Date', ascending=False)
    run_b = run_b.sort_values('Date', ascending=False)

    if run_a.empty or run_b.empty:
        st.error("❌ No data found for one or both selections. Please adjust your filters.")
        return

    # If multiple runs match, let user select
    if len(run_a) > 1:
        st.info(f"ℹ️ Found {len(run_a)} runs matching Baseline criteria. Using most recent.")
    if len(run_b) > 1:
        st.info(f"ℹ️ Found {len(run_b)} runs matching Candidate criteria. Using most recent.")
        
    ra = run_a.iloc[0]
    rb = run_b.iloc[0]

    st.divider()
    st.divider()
    
    # Summary Cards
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(baseline_title)
        st.markdown(f"""
        - Build: `{ver_a}`
        - Scanners: `{ra['scanner_count']}`
        - Threads: `{ra['thread_count']}`
        """)
    
    with col2:
        st.markdown(candidate_title)
        st.markdown(f"""
        - Build: `{ver_b}`
        - Scanners: `{rb['scanner_count']}`
        - Threads: `{rb['thread_count']}`
        """)
    
    st.divider()
    
    # 1. Configuration Diff Table
    st.markdown("### Detailed Comparison")
    
    # helper for n/a values
    fmt_h = lambda x: f"{x:.2f}h" if x and not pd.isna(x) else "N/A"
    fmt_f = lambda x: f"{x:,.0f}" if x and not pd.isna(x) else "N/A"
    fmt_g = lambda x: f"{x}GB" if x and not pd.isna(x) else "N/A"

    conf_df = pd.DataFrame({
        "Metric / Setting": [
            "Scan Duration", 
            "Total Volume", 
            "Total Files", 
            "Classifier Set",
            "Scanners", 
            "Threads / Scanner", 
            "Total Threads", 
            "ACL Enabled", 
            "OCR Enabled", 
            "Discover File FF"
        ],
        f"Baseline ({ver_a})": [
            fmt_h(ra.get('total_scan_hours')),
            fmt_g(ra.get('Total_Size_GB')),
            fmt_f(ra.get('Total_Files')),
            ra.get('Classifier_Name', 'Default'),
            ra['scanner_count'], 
            ra['thread_count'],
            ra['scanner_count'] * ra['thread_count'],
            "✅" if ra['is_acl'] else "❌",
            "✅" if ra['is_ocr'] else "❌",
            "✅" if ra['is_feature_flags'] else "❌"
        ],
        f"Candidate ({ver_b})": [
            fmt_h(rb.get('total_scan_hours')),
            fmt_g(rb.get('Total_Size_GB')),
            fmt_f(rb.get('Total_Files')),
            rb.get('Classifier_Name', 'Default'),
            rb['scanner_count'], 
            rb['thread_count'],
            rb['scanner_count'] * rb['thread_count'],
            "✅" if rb['is_acl'] else "❌",
            "✅" if rb['is_ocr'] else "❌",
            "✅" if rb['is_feature_flags'] else "❌"
        ]
    })
    render_centered_table(conf_df)

    # 2. Performance Metrics Comparison
    st.markdown("### Performance Metrics")
    
    k1, k2, k3, k4 = st.columns(4)
    
    # Data Throughput
    tp_a = ra['throughput_cluster_gb_hr'] or 0
    tp_b = rb['throughput_cluster_gb_hr'] or 0
    delta_tp = tp_b - tp_a
    pct_tp = (delta_tp / tp_a * 100) if tp_a else 0
    
    k1.metric("Data Throughput (GB/hr)", 
              f"{tp_b:.2f}", 
              f"{delta_tp:+.2f} ({pct_tp:+.1f}%)",
              delta_color="normal")
    
    # File Throughput
    files_a = ra['throughput_cluster_files_hr'] or 0
    files_b = rb['throughput_cluster_files_hr'] or 0
    delta_f = files_b - files_a
    pct_f = (delta_f / files_a * 100) if files_a else 0
    
    k2.metric("File Throughput (Files/hr)", 
              f"{files_b:,.0f}", 
              f"{delta_f:+,.0f} ({pct_f:+.1f}%)",
              delta_color="normal")
    
    # Total Files
    k3.metric("Total Files", 
              f"{rb['Total_Files']:,.0f}" if rb['Total_Files'] else "N/A",
              f"{(rb['Total_Files'] or 0) - (ra['Total_Files'] or 0):+,.0f}")
    
    # Total Size
    k4.metric("Total Size (GB)", 
              f"{rb['Total_Size_GB']}" if rb['Total_Size_GB'] else "N/A",
              f"{(rb['Total_Size_GB'] or 0) - (ra['Total_Size_GB'] or 0):+}")
    
    # Line charts for baseline vs candidate
    st.markdown("### Performance Trend (Baseline vs Candidate)")
    l1, l2 = st.columns(2)
    with l1:
        line_df = pd.DataFrame({
            "Configuration": ["Baseline", "Candidate"],
            "GB/hr": [tp_a, tp_b]
        })
        fig_line_gb = px.line(line_df, x="Configuration", y="GB/hr", markers=True, title="Data Throughput (GB/hr)")
        fig_line_gb.update_traces(line=dict(color="#8B5CF6", width=3))
        st.plotly_chart(fig_line_gb, width="stretch")
    with l2:
        line_df_f = pd.DataFrame({
            "Configuration": ["Baseline", "Candidate"],
            "Files/hr": [files_a, files_b]
        })
        fig_line_f = px.line(line_df_f, x="Configuration", y="Files/hr", markers=True, title="File Throughput (Files/hr)")
        fig_line_f.update_traces(line=dict(color="#10B981", width=3))
        st.plotly_chart(fig_line_f, width="stretch")

    # Summary table
    st.markdown("### Summary Table")
    summary_df = pd.DataFrame({
        "Metric": ["Data Throughput (GB/hr)", "File Throughput (Files/hr)", "Total Files", "Total Size (GB)", "Scan Hours"],
        "Baseline": [
            f"{tp_a:.2f}",
            f"{files_a:,.0f}",
            f"{ra['Total_Files']:,.0f}" if ra['Total_Files'] else "N/A",
            f"{ra['Total_Size_GB']}" if ra['Total_Size_GB'] else "N/A",
            f"{ra.get('total_scan_hours', 0):.2f}" if pd.notnull(ra.get('total_scan_hours', None)) else "N/A"
        ],
        "Candidate": [
            f"{tp_b:.2f}",
            f"{files_b:,.0f}",
            f"{rb['Total_Files']:,.0f}" if rb['Total_Files'] else "N/A",
            f"{rb['Total_Size_GB']}" if rb['Total_Size_GB'] else "N/A",
            f"{rb.get('total_scan_hours', 0):.2f}" if pd.notnull(rb.get('total_scan_hours', None)) else "N/A"
        ]
    })
    st.dataframe(summary_df, width="stretch", hide_index=True)
    
    # 3. Visual Comparison Charts
    st.markdown("### Visual Comparison")
    
    chart_col1, chart_col2 = st.columns(2)
    
    with chart_col1:
        # Throughput comparison
        chart_data = pd.DataFrame([
            {'Configuration': f"Baseline\n({ver_a})", 'GB/hr': tp_a, 'Files/hr': files_a},
            {'Configuration': f"Candidate\n({ver_b})", 'GB/hr': tp_b, 'Files/hr': files_b}
        ])
        
        fig1 = px.bar(chart_data, x='Configuration', y='GB/hr', 
                     title="Data Throughput Comparison",
                     color='Configuration',
                     color_discrete_sequence=['#8B5CF6', '#6366F1'])
        st.plotly_chart(fig1, width="stretch")
    
    with chart_col2:
        fig2 = px.bar(chart_data, x='Configuration', y='Files/hr', 
                     title="File Throughput Comparison",
                     color='Configuration',
                     color_discrete_sequence=['#8B5CF6', '#6366F1'])
        st.plotly_chart(fig2, width="stretch")
    
    # Winner determination
    st.divider()
    if pct_tp > 5:
        st.success(f"🏆 **Candidate is {pct_tp:.1f}% faster** in data throughput!")
    elif pct_tp < -5:
        st.warning(f"⚠️ **Baseline is {abs(pct_tp):.1f}% faster** in data throughput!")
    else:
        st.info("ℹ️ **Performance is comparable** (within 5% difference)")


# --- Build + Data Source Runs (same build) ---
def page_build_ds_runs():
    st.markdown("## 🧪 Build + Data Source Runs")
    st.caption("Compare **all runs** for a selected Build + DS (e.g. 248.1 + Small Files + S3), and optionally compare across two DS types.")

    src = st.radio(
        "Runs source",
        ["Scan Status DB (all runs)", "Auto Scans", "Manual Benchmarks", "Both"],
        horizontal=True,
        index=0,
        key="build_ds_runs_source",
    )
    include_inactive = st.checkbox("Include inactive runs (manual only)", value=False, key="build_ds_runs_inactive")

    frames = []
    if src in {"Scan Status DB (all runs)", "Both"}:
        sp = load_scan_parents_runs_as_manual_like_df(include_inactive_envs=include_inactive)
        if not sp.empty:
            sp = sp.copy()
            sp["_source"] = "scan_db"
            sp["_run_key"] = "sp:" + sp.get("scan_id").astype(str)
            frames.append(sp)

    if src in {"Manual Benchmarks", "Both"}:
        m = load_data(include_inactive=include_inactive)
        if not m.empty:
            m = m.copy()
            m["_source"] = "manual"
            m["_run_key"] = "m:" + m.get("id").astype(str)
            frames.append(m)
    if src in {"Auto Scans", "Both"}:
        a = load_auto_trend_as_manual_like_df()
        if not a.empty:
            a = a.copy()
            a["_source"] = "auto"
            a["_run_key"] = "a:" + a.get("scan_id", a.get("id")).astype(str)
            frames.append(a)

    df = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    if df.empty:
        st.warning("No runs found for the selected source(s) yet.")
        return

    # Standardize column for dashboard consumption
    for old_col, new_col in [("scan_status", "Scan_Status"), ("build_version", "Build_Version")]:
        if old_col in df.columns:
            if new_col not in df.columns:
                df[new_col] = df[old_col]
            else:
                df[new_col] = df[new_col].fillna(df[old_col])

    df = _ensure_throughput_columns(df)
    # Avoid Arrow type issues when mixing manual numeric ids + auto scan_id strings.
    try:
        if "id" in df.columns:
            df["id"] = df["id"].astype("string")
    except Exception:
        pass

    def _normalize_file_set_for_compare(v: str) -> str:
        s = str(v or "").strip()
        u = s.upper()
        if "SMALL FILE" in u:
            return "Small Files"
        if "LARGE FILE" in u:
            return "Large Files"
        if "MIXED" in u:
            return "Mixed Files"
        if "CSV" in u:
            return "CSV Files"
        return s

    for c in ["Build_Version", "Data_Source_Type", "Scan_Type", "file_set_type"]:
        if c in df.columns:
            df[c] = df[c].astype(str)
    if "file_set_type" in df.columns:
        df["file_set_type"] = df["file_set_type"].apply(_normalize_file_set_for_compare)

    def _uniq_sorted(series):
        try:
            vals = [str(x).strip() for x in series.dropna().unique().tolist()]
        except Exception:
            vals = [str(x).strip() for x in series.unique().tolist()]
        vals = [v for v in vals if v and v.lower() != "nan"]
        return sorted(set(vals))

    def _pick_default(options, contains_text):
        for o in options:
            if contains_text.lower() in o.lower():
                return o
        return options[0] if options else ""

    build_options = sorted(_uniq_sorted(df["Build_Version"]), reverse=True)
    selected_build = st.selectbox("Build Version", build_options, index=0 if build_options else None)
    build_df = df[df["Build_Version"].astype(str) == str(selected_build)]
    if build_df.empty:
        st.info("No runs for this build.")
        return

    # DS first (all DS types for this build), then file set types based on DS selection.
    # DS options: filter out obvious garbage (scan-name-like strings).
    raw_ds_options = _uniq_sorted(build_df["Data_Source_Type"])
    ds_options = []
    for v in raw_ds_options:
        s = str(v or "").strip()
        if not s or s.lower() == "nan":
            continue
        # Relaxed filter: allow spaces and slightly longer names
        if len(s) > 40 or "scanner" in s.lower() or "thread" in s.lower():
            continue
        ds_options.append(s)
    ds_options = sorted(set(ds_options)) if ds_options else raw_ds_options
    default_ds = _pick_default(ds_options, "S3")
    default_sel = [default_ds] if default_ds else []
    selected_ds_list = st.multiselect(
        "Data Source Types to compare (pick 1–2)",
        options=ds_options,
        default=default_sel,
    )
    # Keep it simple: clamp to 2 (user asked for two).
    if len(selected_ds_list) > 2:
        selected_ds_list = selected_ds_list[:2]
        st.caption("Showing first 2 selected Data Sources.")
    if not selected_ds_list:
        st.warning("Select at least one Data Source Type.")
        return

    ds_df = build_df[build_df["Data_Source_Type"].astype(str).isin([str(x) for x in selected_ds_list])]

    fs_options = _uniq_sorted(ds_df["file_set_type"]) if not ds_df.empty else _uniq_sorted(build_df["file_set_type"])
    default_fs = _pick_default(fs_options, "Small Files")
    selected_fs = st.selectbox("File Set Type", fs_options, index=fs_options.index(default_fs) if default_fs in fs_options else 0)
    fs_df = ds_df[ds_df["file_set_type"].astype(str).str.contains(str(selected_fs), case=False, na=False)]

    if fs_df.empty:
        fs_df = ds_df.copy() # fall back if the file set is invalid for the new selection

    scan_type_options = _uniq_sorted(fs_df["Scan_Type"]) if not fs_df.empty else _uniq_sorted(ds_df["Scan_Type"])
    selected_scan_type = st.selectbox("Scan Type", ["(All)"] + scan_type_options)

    filtered = fs_df.copy()
    if selected_scan_type != "(All)":
        filtered = filtered[filtered["Scan_Type"].astype(str) == str(selected_scan_type)]

    if filtered.empty:
        st.warning("No runs match these filters.")
        return
    filtered = _ensure_throughput_columns(filtered)

    # Summary per DS
    st.markdown("### Summary (per Data Source)")
    try:
        tmp = filtered.copy()
        tmp["GB_hr"] = pd.to_numeric(tmp.get("throughput_cluster_gb_hr"), errors="coerce")
        tmp["Files_hr"] = pd.to_numeric(tmp.get("throughput_cluster_files_hr"), errors="coerce")
        tmp["Hours"] = pd.to_numeric(tmp.get("total_scan_hours"), errors="coerce")
        summ = tmp.groupby("Data_Source_Type", dropna=False).agg(
            Runs=("Data_Source_Type", "size"),
            Avg_GB_hr=("GB_hr", "mean"),
            Best_GB_hr=("GB_hr", "max"),
            Avg_Files_hr=("Files_hr", "mean"),
            Fastest_Hours=("Hours", "min"),
        ).reset_index()
        st.dataframe(summ, width="stretch", hide_index=True)
    except Exception:
        pass

    st.divider()

    run_ids = filtered["_run_key"].dropna().astype(str).tolist() if "_run_key" in filtered.columns else []
    run_label_map = {}
    try:
        tmp = filtered.copy()
        tmp["Date"] = pd.to_datetime(tmp.get("Date"), errors="coerce")
        tmp = tmp.sort_values(by=["Data_Source_Type", "Date", "_run_key"], ascending=[True, False, False])
        for _, r in tmp.iterrows():
            k = str(r.get("_run_key") or "").strip()
            if not k:
                continue
            ds = str(r.get("Data_Source_Type") or "").strip()
            dt = r.get("Date")
            dt_txt = ""
            try:
                if pd.notna(dt):
                    dt_txt = pd.to_datetime(dt).strftime("%Y-%m-%d")
            except Exception:
                dt_txt = ""
            tail = k[-10:] if len(k) > 10 else k
            run_label_map[k] = f"{ds} | {dt_txt} | {tail}"
    except Exception:
        run_label_map = {}
    selected_ids = st.multiselect(
        "Select specific runs (optional)",
        options=run_ids,
        default=run_ids[:8] if len(run_ids) > 8 else run_ids,
        format_func=lambda k: run_label_map.get(k, str(k)),
    )
    show_df = filtered[filtered["_run_key"].isin(selected_ids)] if selected_ids and "_run_key" in filtered.columns else filtered


    # --- Run-by-run charts for a single DS (compare multiple runs within same build + DS) ---
    try:
        single_ds = (len(selected_ds_list) == 1)
    except Exception:
        single_ds = False

    if single_ds:
        st.markdown("### Run-by-run charts (same Build + DS)")
        try:
            cmp = show_df.copy()
            # Ensure we only compare scan-id-backed runs (auto/scan_db)
            if "scan_id" in cmp.columns:
                cmp = cmp[cmp["scan_id"].astype(str).str.strip().ne("")]
            cmp["Date"] = pd.to_datetime(cmp.get("Date"), errors="coerce")
            cmp = cmp.sort_values(["Date", "scan_id"], ascending=[True, True])

            def _run_label(row):
                sid = str(row.get("scan_id") or "").strip()
                dt = row.get("Date")
                dts = ""
                try:
                    if pd.notna(dt):
                        dts = pd.to_datetime(dt).strftime("%m-%d %H:%M")
                except Exception:
                    dts = ""
                tail = sid[-6:] if len(sid) >= 6 else sid
                return (f"{dts} | {tail}" if dts else tail) or sid

            cmp["Run"] = cmp.apply(_run_label, axis=1)

            # Pull numeric metrics
            num_cols = [
                "completed_parts","failed_parts","total_parts_scanned",
                "objects_scanned","objects_failed",
                "objects_with_pi","total_classifications","newly_added_classifications","false_removed_classifications",
                "distinct_classification_attributes","New_Attributes","Removed_Attributes","total_scan_hours",
            ]
            for c in num_cols:
                if c in cmp.columns:
                    cmp[c] = pd.to_numeric(cmp[c], errors="coerce")

            def _bar_text(vals):
                out = []
                for v in list(vals):
                    try:
                        if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
                            out.append("")
                            continue
                    except Exception:
                        if v is None:
                            out.append("")
                            continue
                    try:
                        out.append(f"{int(v):,}")
                    except Exception:
                        out.append(str(v))
                return out

            # 3 charts in one row (bar charts are better for comparing runs)
            c1, c2, c3 = st.columns(3)

            with c1:
                fig1 = go.Figure()
                if all(c in cmp.columns for c in ["completed_parts", "failed_parts", "total_parts_scanned"]):
                    fig1.add_trace(go.Bar(x=cmp["Run"], y=cmp["completed_parts"], name="completed_parts", marker_color="#10B981", text=_bar_text(cmp["completed_parts"]), textposition="outside", cliponaxis=False, textfont=dict(size=10)))
                    fig1.add_trace(go.Bar(x=cmp["Run"], y=cmp["failed_parts"], name="failed_parts", marker_color="#EF4444", text=_bar_text(cmp["failed_parts"]), textposition="outside", cliponaxis=False, textfont=dict(size=10)))
                    fig1.add_trace(go.Bar(x=cmp["Run"], y=cmp["total_parts_scanned"], name="total_parts", marker_color="#3B82F6", text=_bar_text(cmp["total_parts_scanned"]), textposition="outside", cliponaxis=False, textfont=dict(size=10)))
                    fig1.update_layout(
                        title="Parts (Totals)",
                        barmode="group",
                        height=320,
                        margin=dict(l=10, r=10, t=55, b=90),
                        legend=dict(orientation="h", yanchor="top", y=-0.35, xanchor="left", x=0),
                    )
                    fig1.update_xaxes(type="category", tickangle=-30)
                    fig1.update_yaxes(title_text="Count", rangemode="tozero")
                    st.plotly_chart(fig1, width="stretch")

            with c2:
                fig2 = go.Figure()
                if all(c in cmp.columns for c in ["objects_scanned", "objects_failed"]):
                    fig2.add_trace(go.Bar(x=cmp["Run"], y=cmp["objects_scanned"], name="objects_scanned", marker_color="#8B5CF6", text=_bar_text(cmp["objects_scanned"]), textposition="outside", cliponaxis=False, textfont=dict(size=10)))
                    fig2.add_trace(go.Bar(x=cmp["Run"], y=cmp["objects_failed"], name="objects_failed", marker_color="#EF4444", text=_bar_text(cmp["objects_failed"]), textposition="outside", cliponaxis=False, textfont=dict(size=10)))
                    fig2.update_layout(
                        title="Objects (Totals)",
                        barmode="group",
                        height=320,
                        margin=dict(l=10, r=10, t=55, b=90),
                        legend=dict(orientation="h", yanchor="top", y=-0.35, xanchor="left", x=0),
                    )
                    fig2.update_xaxes(type="category", tickangle=-30)
                    fig2.update_yaxes(title_text="Count", rangemode="tozero")
                    st.plotly_chart(fig2, width="stretch")

            with c3:
                fig3 = go.Figure()
                added_any = False
                if "objects_with_pi" in cmp.columns:
                    fig3.add_trace(go.Bar(x=cmp["Run"], y=cmp["objects_with_pi"], name="objectsWithPi", marker_color="#10B981", text=_bar_text(cmp["objects_with_pi"]), textposition="outside", cliponaxis=False, textfont=dict(size=10)))
                    added_any = True
                if "total_classifications" in cmp.columns:
                    fig3.add_trace(go.Bar(x=cmp["Run"], y=cmp["total_classifications"], name="classifications.total", marker_color="#3B82F6", text=_bar_text(cmp["total_classifications"]), textposition="outside", cliponaxis=False, textfont=dict(size=10)))
                    added_any = True
                if "New_Attributes" in cmp.columns:
                    fig3.add_trace(go.Bar(x=cmp["Run"], y=cmp["New_Attributes"], name="attrs_added(delta)", marker_color="#F59E0B", text=_bar_text(cmp["New_Attributes"]), textposition="outside", cliponaxis=False, textfont=dict(size=10)))
                    added_any = True
                if "Removed_Attributes" in cmp.columns:
                    fig3.add_trace(go.Bar(x=cmp["Run"], y=cmp["Removed_Attributes"], name="attrs_removed(delta)", marker_color="#EF4444", text=_bar_text(cmp["Removed_Attributes"]), textposition="outside", cliponaxis=False, textfont=dict(size=10)))
                    added_any = True
                if added_any:
                    fig3.update_layout(
                        title="ObjectsWithPi + Classifications",
                        barmode="group",
                        height=320,
                        margin=dict(l=10, r=10, t=55, b=90),
                        legend=dict(orientation="h", yanchor="top", y=-0.35, xanchor="left", x=0),
                    )
                    fig3.update_xaxes(type="category", tickangle=-30)
                    fig3.update_yaxes(title_text="Count", rangemode="tozero")
                    st.plotly_chart(fig3, width="stretch")

            # Observations table
            st.markdown("### Observations (per run)")

            def _compact_state(x: str) -> str:
                return str(x or '').strip().lower().replace(' ', '')

            obs_rows = []
            for _, r in cmp.iterrows():
                sid = str(r.get('scan_id') or '').strip()
                nm = str(r.get('scan_name') or '').strip()
                exp = resolve_expected_totals_for_scan(nm)
                exp_files = exp.get('expected_total_files')
                cur_obj = int(r.get('objects_scanned') or 0) if str(r.get('objects_scanned') or '').strip() != '' else 0
                short_by = (int(exp_files) - cur_obj) if exp_files and cur_obj < int(exp_files) else 0
                stv = str(r.get('scan_status') or r.get('scan_state_progress') or '').strip()
                failed_parts = int(r.get('failed_parts') or 0)
                obj_failed = int(r.get('objects_failed') or 0)
                term = _compact_state(stv) in {'completed','completedwithfailures','failed'}

                notes = []
                if exp_files:
                    notes.append(f"objects {cur_obj:,}/{int(exp_files):,}")
                if short_by > 0:
                    notes.append(f"short {short_by:,}")
                if term and short_by > 0 and failed_parts == 0 and obj_failed == 0:
                    notes.append("terminal but short with no failures")
                if _compact_state(stv) == 'completedwithfailures' and failed_parts == 0 and obj_failed == 0:
                    notes.append("CompletedWithFailures but failures=0 (likely rescanned)")
                obs_rows.append({
                    'Run': str(r.get('Run') or ''),
                    'scan_id': sid,
                    'status': stv,
                    'hours': r.get('total_scan_hours'),
                    'objects_scanned': cur_obj,
                    'expected_objects': exp_files,
                    'short_by': short_by,
                    'distinct_classifications': r.get('distinct_classification_attributes'),
                    'attrs_added': r.get('New_Attributes'),
                    'attrs_removed': r.get('Removed_Attributes'),
                    'notes': '; '.join([x for x in notes if x]),
                })

            st.dataframe(pd.DataFrame(obs_rows), width="stretch", hide_index=True)
        except Exception as ex:
            st.warning(f"Run-by-run charts unavailable: {ex}")

    cols = [
        "_source",
        "_run_key",
        "Date",
        "Scenario_Type",
        "Scan_Type",
        "Data_Source_Type",
        "file_set_type",
        "scanner_count",
        "thread_count",
        "Total_Size_GB",
        "Total_Files",
        "total_scan_hours",
        "throughput_cluster_gb_hr",
        "throughput_cluster_files_hr",
        "New_Attributes",
        "Total_Classification",
        "Removed_Attributes",
    ]
    cols = [c for c in cols if c in show_df.columns]
    try:
        show_sorted = show_df[cols].sort_values(by=["Date", "_run_key"], ascending=[False, False])
    except Exception:
        show_sorted = show_df[cols]
    st.dataframe(show_sorted, width="stretch", hide_index=True)

    chart_df = show_df.copy()
    # Make a readable label for the x-axis (run id can be long for scan_id).
    try:
        d = pd.to_datetime(chart_df.get("Date"), errors="coerce")
        d_txt = d.dt.strftime("%m-%d") if hasattr(d, "dt") else ""
    except Exception:
        d_txt = ""
    try:
        rk = chart_df.get("_run_key", chart_df.index.astype(str)).astype(str)
        rk_short = rk.apply(lambda s: (s[-10:] if len(s) > 14 else s))
        chart_df["Run_Label"] = chart_df.get("Data_Source_Type", "").astype(str) + " | " + rk_short + ((" | " + d_txt) if isinstance(d_txt, (pd.Series,)) else "")
    except Exception:
        chart_df["Run_Label"] = chart_df.get("_run_key", chart_df.index.astype(str)).astype(str)

    st.markdown("### Charts (trend-style)")
    chart_df["Date"] = pd.to_datetime(chart_df.get("Date"), errors="coerce")
    chart_df = chart_df.sort_values(by=["Data_Source_Type", "Date", "_run_key"], ascending=[True, True, True])
    # If Date is missing, fall back to a categorical x.
    x_col = "Date" if chart_df.get("Date").notna().any() else "Run_Label"

    def _line_fig(y_col: str, title: str, y_title: str):
        fig = go.Figure()
        for ds in sorted(chart_df["Data_Source_Type"].dropna().astype(str).unique().tolist()):
            sdf = chart_df[chart_df["Data_Source_Type"].astype(str) == str(ds)].copy()
            yv = pd.to_numeric(sdf.get(y_col), errors="coerce")
            fig.add_trace(go.Scatter(
                x=sdf[x_col],
                y=yv,
                mode="lines+markers",
                name=str(ds),
            ))
        fig.update_layout(
            title=title,
            xaxis_title="Run Date" if x_col == "Date" else "Run",
            yaxis_title=y_title,
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        if x_col == "Date":
            fig.update_xaxes(type="date")
        else:
            fig.update_xaxes(type="category")
        return fig

    c1, c2, c3 = st.columns(3)
    with c1:
        st.plotly_chart(_line_fig("throughput_cluster_gb_hr", "GB/hr", "GB/hr"), width="stretch")
    with c2:
        st.plotly_chart(_line_fig("throughput_cluster_files_hr", "Files/hr", "Files/hr"), width="stretch")
    with c3:
        st.plotly_chart(_line_fig("total_scan_hours", "Total scan hours", "Hours"), width="stretch")


# --- Cross Data Source Trend Comparison ---
def page_data_source_trend_comparison(df):
    st.markdown("## 📈 Data Source Trend Comparison")
    st.caption("Compare multiple Data Sources (one line per DS) for a selected File Set and Scan Type.")
    src = st.radio(
        "Runs source",
        ["Auto Scans (recommended)", "Manual Benchmarks", "Both"],
        horizontal=True,
        index=0,
        key="ds_trend_source",
    )
    include_inactive = st.checkbox("Include inactive runs (manual only)", value=False, key="ds_trend_inactive")
    frames = []
    if src in {"Manual Benchmarks", "Both"}:
        m = load_data(include_inactive=include_inactive)
        if not m.empty:
            frames.append(m)
    if src in {"Auto Scans (recommended)", "Both"}:
        a = load_auto_trend_as_manual_like_df()
        if not a.empty:
            frames.append(a)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        st.warning("No runs found for the selected source(s) yet.")
        return
    df = _ensure_throughput_columns(df)

    fs_options = sorted(set([str(x).strip() for x in df.get("file_set_type", pd.Series(dtype=str)).dropna().unique().tolist() if str(x).strip()]))
    if not fs_options:
        st.warning("No file set types found.")
        return
    default_fs = None
    for o in fs_options:
        if "large files" in o.lower():
            default_fs = o
            break
    if default_fs is None:
        default_fs = fs_options[0]
    selected_fs = st.selectbox("File Set Type", fs_options, index=fs_options.index(default_fs) if default_fs in fs_options else 0)

    scan_type_options = sorted(set([str(x).strip() for x in df.get("Scan_Type", pd.Series(dtype=str)).dropna().unique().tolist() if str(x).strip()]))
    selected_scan_type = st.selectbox("Scan Type", ["(All)"] + scan_type_options)

    scan_type_filter = None if selected_scan_type == "(All)" else selected_scan_type
    render_trend_analysis(
        df,
        title=f"Data Source Trend Comparison ({selected_fs})",
        file_set_filter=str(selected_fs),
        scan_type_filter=scan_type_filter,
        compare_by="Data_Source_Type",
        show_standard_filters=False,
    )

# --- Classification Trends (from scan_insights) ---
def load_classification_trend_from_scan_insights(file_set_filter: str, data_source_type: str, scan_type: str) -> pd.DataFrame:
    """
    Build-by-build classification metrics based on tracker-populated tables:
    - scan_parents (scan_name / updated_at)
    - scan_insights (total_classification, new/removed attrs, classifier count)

    We infer build + ds + scan_type + file_set_type from scan_name to match trend filters.
    """
    engine = get_engine()
    if engine is None:
        return pd.DataFrame()

    file_set_filter = str(file_set_filter or "").strip()
    data_source_type = str(data_source_type or "").strip()
    scan_type = str(scan_type or "").strip()

    def _norm_ds(v: str) -> str:
        return str(v or "").strip().upper()

    def _norm_scan_type(v: str) -> str:
        s = str(v or "").strip().lower()
        if "ols" in s:
            return "ols"
        if "full" in s:
            return "full"
        return s

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT
                        sp.scan_id,
                        sp.scan_name,
                        sp.updated_at,
                        si.total_classification,
                        si.new_attributes_count,
                        si.removed_attributes_count,
                        si.distinct_classification_attributes
                    FROM scan_parents sp
                    JOIN scan_insights si ON si.scan_id = sp.scan_id
                    WHERE
                        si.total_classification IS NOT NULL
                        OR si.new_attributes_count IS NOT NULL
                        OR si.removed_attributes_count IS NOT NULL
                        OR si.distinct_classification_attributes IS NOT NULL
                    ORDER BY sp.updated_at DESC
                    LIMIT 6000
                """)
            ).fetchall()
    except Exception:
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=[
        "scan_id",
        "scan_name",
        "updated_at",
        "total_classification",
        "new_attributes_count",
        "removed_attributes_count",
        "distinct_classification_attributes",
    ])

    # Infer filters from scan_name
    def _infer_row(scan_name: str) -> Dict[str, str]:
        d = infer_add_benchmark_defaults_from_scan_name(scan_name)
        return {
            "build_version": str(d.get("build_version") or parse_build_version_from_text(scan_name) or "").strip(),
            "data_source_type": str(d.get("data_source_type") or "").strip(),
            "scan_type": str(d.get("scan_type") or "").strip(),
            "file_set_type": str(d.get("file_set_type") or "").strip(),
        }

    inferred = df["scan_name"].astype(str).apply(_infer_row)
    df["Build_Version"] = inferred.apply(lambda x: x.get("build_version", ""))
    df["Data_Source_Type"] = inferred.apply(lambda x: x.get("data_source_type", ""))
    df["Scan_Type"] = inferred.apply(lambda x: x.get("scan_type", ""))
    df["file_set_type"] = inferred.apply(lambda x: x.get("file_set_type", ""))

    df = df[df["Build_Version"].astype(str).str.strip().ne("")]

    if file_set_filter:
        df = df[df["file_set_type"].astype(str).str.contains(file_set_filter, case=False, na=False)]
    if data_source_type:
        want_ds = _norm_ds(data_source_type)
        df = df[df["Data_Source_Type"].astype(str).map(_norm_ds) == want_ds]
    if scan_type:
        want_st = _norm_scan_type(scan_type)
        df = df[df["Scan_Type"].astype(str).map(_norm_scan_type) == want_st]

    if df.empty:
        return pd.DataFrame()

    # Numeric coercion (Arrow-safe and Plotly-safe)
    for c in ["total_classification", "new_attributes_count", "removed_attributes_count", "distinct_classification_attributes"]:
        try:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        except Exception:
            pass

    # Pick latest scan per build (based on scan_parents.updated_at)
    try:
        df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce")
    except Exception:
        pass
    df = df.sort_values(["Build_Version", "updated_at"], ascending=[True, False])
    df = df.drop_duplicates(subset=["Build_Version"], keep="first")
    df = df.sort_values("Build_Version")

    out = df[[
        "Build_Version",
        "total_classification",
        "new_attributes_count",
        "removed_attributes_count",
    ]].copy()
    out = out.rename(columns={
        "total_classification": "Total_Classifications",
        "new_attributes_count": "New_Attributes",
        "removed_attributes_count": "Removed_Attributes",
    })

    # Template classifier count: from build_classifier_mappings (what user means by "template has ~72 classifiers").
    try:
        build_versions = [str(x).strip() for x in out["Build_Version"].astype(str).tolist() if str(x).strip()]
        build_versions = list(dict.fromkeys(build_versions))
        if build_versions:
            engine2 = get_engine()
            if engine2 is not None:
                params = {}
                ph = []
                for i, bv in enumerate(build_versions[:200]):
                    k = f"bv{i}"
                    params[k] = bv
                    ph.append(f":{k}")
                if ph:
                    with engine2.connect() as conn:
                        rows2 = conn.execute(
                            text(f"""
                                SELECT b.version, COUNT(DISTINCT bcm.classifier_id) AS n
                                FROM builds b
                                JOIN build_classifier_mappings bcm ON bcm.build_id = b.id
                                WHERE b.version IN ({",".join(ph)})
                                GROUP BY b.version
                            """),
                            params,
                        ).fetchall()
                    m = {str(r[0]).strip(): (int(r[1]) if r[1] is not None else None) for r in (rows2 or [])}
                    out["Template_Classifiers"] = out["Build_Version"].astype(str).map(lambda v: m.get(str(v).strip()))
                else:
                    out["Template_Classifiers"] = pd.NA
            else:
                out["Template_Classifiers"] = pd.NA
        else:
            out["Template_Classifiers"] = pd.NA
    except Exception:
        out["Template_Classifiers"] = pd.NA
    return out

# --- Trend Analysis (Multi-Build Comparison) ---
def render_trend_analysis(df, title, file_set_filter=None, scan_type_filter=None, compare_by=None, acl_filter=None, ff_filter=None, show_standard_filters=True):
    st.markdown(f"## 📈 {title}")
    st.write("") # Added space before filters
    
    if df.empty:
        st.warning("No data available.")
        return

    # Ensure these are always defined (used by Classification Trends chart)
    selected_ds = ""
    selected_scan = ""
    
    # Base filtering
    df_filtered = df.copy()
    # Normalize key filter columns (prevents missing rows due to whitespace/casing inconsistencies)
    try:
        for col in ["Build_Version", "Data_Source_Type", "Scan_Type", "file_set_type"]:
            if col in df_filtered.columns:
                df_filtered[col] = df_filtered[col].astype(str).str.strip()
    except Exception:
        pass
    if file_set_filter:
        # Use partial match (case-insensitive) to capture "SMB Large Files", "S3 Large Files", etc.
        df_filtered = df_filtered[df_filtered['file_set_type'].astype(str).str.contains(file_set_filter, case=False, na=False)]
    if scan_type_filter:
        df_filtered = df_filtered[df_filtered['Scan_Type'] == scan_type_filter]
        # Fallback: some data uses Scenario_Type to indicate OLS
        if df_filtered.empty and 'Scenario_Type' in df.columns and 'OLS' in scan_type_filter:
            df_filtered = df[df['Scenario_Type'].astype(str).str.contains('OLS', case=False, na=False)]
    if acl_filter is not None:
        df_filtered = df_filtered[df_filtered['is_acl'] == acl_filter]
    if ff_filter is not None:
        df_filtered = df_filtered[df_filtered['is_feature_flags'] == ff_filter]
    
    if df_filtered.empty:
        st.warning(f"No data available for these conditions.")
        return

    # st.markdown(f"### Performance Trends: {title}") # Removed duplicate title
    
    # If we are NOT comparing, show the standard filters
    if show_standard_filters and not compare_by:
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            available_ds = sorted(df_filtered['Data_Source_Type'].unique())
            selected_ds = st.selectbox(f"Data Source", available_ds, key=f"ds_{title}")
            df_filtered = df_filtered[df_filtered['Data_Source_Type'] == selected_ds]
        with col2:
            available_scans = sorted(df_filtered['Scan_Type'].unique())
            selected_scan = st.selectbox(f"Scan Type", available_scans, key=f"st_{title}")
            df_filtered = df_filtered[df_filtered['Scan_Type'] == selected_scan]
        with col3:
            filter_acl = st.checkbox("ACL Only", key=f"acl_{title}")
        with col4:
            filter_ff = st.checkbox("FF Only (Structured Files Discovery)", key=f"ff_{title}")
        if filter_acl:
            df_filtered = df_filtered[df_filtered['is_acl'] == 1]
        if filter_ff:
            df_filtered = df_filtered[df_filtered['is_feature_flags'] == 1]
    
    # Handle duplicates/Best Run selection
    def select_best_run(group):
        # 1. Try to pick fastest run by completion time
        if 'total_scan_hours' in group.columns and group['total_scan_hours'].notna().any():
            valid_time = group[group['total_scan_hours'].notna()]
            return valid_time.loc[valid_time['total_scan_hours'].idxmin()]
        
        # 2. Fallback: Pick run with highest throughput
        if 'throughput_cluster_gb_hr' in group.columns and group['throughput_cluster_gb_hr'].notna().any():
            valid_tp = group[group['throughput_cluster_gb_hr'].notna()]
            return valid_tp.loc[valid_tp['throughput_cluster_gb_hr'].idxmax()]
            
        return group.iloc[0]

    # Group by Build and Compare_By if present
    group_cols = ['Build_Version']
    if compare_by: group_cols.append(compare_by)
    
    # Pick best run per (Build_Version, compare_by?) without groupby.apply().
    # This avoids pandas deprecation warnings AND keeps grouping columns (Build_Version),
    # which we need for sorting/plotting.
    picked_idxs = []
    try:
        gb = df_filtered.groupby(group_cols, sort=False, dropna=False)
    except TypeError:
        # Older pandas: no dropna kwarg
        gb = df_filtered.groupby(group_cols, sort=False)
    for _, group in gb:
        try:
            if 'total_scan_hours' in group.columns and group['total_scan_hours'].notna().any():
                valid_time = group[group['total_scan_hours'].notna()]
                if not valid_time.empty:
                    picked_idxs.append(valid_time['total_scan_hours'].idxmin())
                    continue
            if 'throughput_cluster_gb_hr' in group.columns and group['throughput_cluster_gb_hr'].notna().any():
                valid_tp = group[group['throughput_cluster_gb_hr'].notna()]
                if not valid_tp.empty:
                    picked_idxs.append(valid_tp['throughput_cluster_gb_hr'].idxmax())
                    continue
            picked_idxs.append(group.index[0])
        except Exception:
            try:
                picked_idxs.append(group.index[0])
            except Exception:
                pass
    plot_df = df_filtered.loc[picked_idxs].copy() if picked_idxs else df_filtered.head(0).copy()
    plot_df = plot_df.reset_index(drop=True)
    plot_df = plot_df.sort_values('Build_Version')
    # Drop rows with missing cluster throughput
    if 'throughput_cluster_gb_hr' in plot_df.columns:
        plot_df = plot_df[plot_df['throughput_cluster_gb_hr'].notna()]

    # Auto scans may not have per-scanner/per-thread columns. Keep rendering robust.
    for col in [
        "throughput_scanner_gb_hr",
        "throughput_thread_gb_hr",
        "throughput_scanner_files_hr",
        "throughput_thread_files_hr",
    ]:
        if col not in plot_df.columns:
            plot_df[col] = pd.NA

    # Quick sanity caption so it's obvious if latest build is included
    try:
        builds = plot_df["Build_Version"].astype(str).tolist()
        max_build = sorted({b.strip() for b in builds if b and b.strip()})[-1] if builds else ""
        st.caption(f"Builds in view: {plot_df['Build_Version'].nunique()} | Latest build in view: `{max_build}`")
    except Exception:
        pass

    # Charts
    t_col1, t_col2, t_col3 = st.columns(3)
    
    with t_col1:
        fig1 = go.Figure()
        if compare_by:
            categories = plot_df[compare_by].unique()
            colors = ['#8B5CF6', '#10B981', '#F59E0B', '#EF4444']
            for i, cat in enumerate(categories):
                cat_df = plot_df[plot_df[compare_by] == cat]
                name = f"{cat}"
                if compare_by == 'is_feature_flags': name = "FF ON" if cat else "FF OFF"
                if compare_by == 'is_acl': name = "ACL ON" if cat else "ACL OFF"
                fig1.add_trace(go.Scatter(x=cat_df['Build_Version'], y=cat_df['throughput_cluster_gb_hr'], mode='lines+markers', name=name, line=dict(color=colors[i % len(colors)], width=3)))
        else:
            fig1.add_trace(go.Scatter(x=plot_df['Build_Version'], y=plot_df['throughput_cluster_gb_hr'], mode='lines+markers', name='Cluster', line=dict(color='#8B5CF6', width=4), marker=dict(size=10)))
            try:
                if "throughput_scanner_gb_hr" in plot_df.columns and plot_df["throughput_scanner_gb_hr"].notna().any():
                    fig1.add_trace(go.Scatter(x=plot_df['Build_Version'], y=plot_df['throughput_scanner_gb_hr'], mode='lines+markers', name='Per Scanner', line=dict(color='#6366F1', width=2), marker=dict(size=8)))
                if "throughput_thread_gb_hr" in plot_df.columns and plot_df["throughput_thread_gb_hr"].notna().any():
                    fig1.add_trace(go.Scatter(x=plot_df['Build_Version'], y=plot_df['throughput_thread_gb_hr'], mode='lines+markers', name='Per Thread', line=dict(color='#A78BFA', width=2), marker=dict(size=8)))
            except Exception:
                pass
        
        fig1.update_layout(title="Data Throughput Trend (GB/hr)", xaxis_title="Build Version", yaxis_title="GB/hour", hovermode='x unified', legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        fig1.update_xaxes(type='category', categoryorder='category ascending')
        st.plotly_chart(fig1, width="stretch")

    with t_col2:
        fig2 = go.Figure()
        if compare_by:
            categories = plot_df[compare_by].unique()
            colors = ['#10B981', '#8B5CF6', '#F59E0B', '#EF4444']
            for i, cat in enumerate(categories):
                cat_df = plot_df[plot_df[compare_by] == cat]
                name = f"{cat}"
                if compare_by == 'is_feature_flags': name = "FF ON" if cat else "FF OFF"
                if compare_by == 'is_acl': name = "ACL ON" if cat else "ACL OFF"
                fig2.add_trace(go.Scatter(x=cat_df['Build_Version'], y=cat_df['throughput_cluster_files_hr'], mode='lines+markers', name=name, line=dict(color=colors[i % len(colors)], width=3)))
        else:
            fig2.add_trace(go.Scatter(x=plot_df['Build_Version'], y=plot_df['throughput_cluster_files_hr'], mode='lines+markers', name='Cluster', line=dict(color='#10B981', width=4), marker=dict(size=10)))
            try:
                if "throughput_scanner_files_hr" in plot_df.columns and plot_df["throughput_scanner_files_hr"].notna().any():
                    fig2.add_trace(go.Scatter(x=plot_df['Build_Version'], y=plot_df['throughput_scanner_files_hr'], mode='lines+markers', name='Per Scanner', line=dict(color='#34D399', width=2), marker=dict(size=8)))
                if "throughput_thread_files_hr" in plot_df.columns and plot_df["throughput_thread_files_hr"].notna().any():
                    fig2.add_trace(go.Scatter(x=plot_df['Build_Version'], y=plot_df['throughput_thread_files_hr'], mode='lines+markers', name='Per Thread', line=dict(color='#6EE7B7', width=2), marker=dict(size=8)))
            except Exception:
                pass
        
        fig2.update_layout(title="File Throughput Trend (files/hr)", xaxis_title="Build Version", yaxis_title="Files/hour", hovermode='x unified', legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        fig2.update_xaxes(type='category', categoryorder='category ascending')
        st.plotly_chart(fig2, width="stretch")

    with t_col3:
        if compare_by:
            st.caption("Classification Trends are shown only for the standard (non-compare) view.")
        else:
            # IMPORTANT:
            # Always start from the Trend dataset values (benchmarks) so every build in view
            # plots, then optionally overlay scan_insights to fill missing fields.
            cls_df = pd.DataFrame()

            # Always anchor the chart to the same build list as the throughput charts.
            # Otherwise it will look like we "only have one build" when classification data
            # exists for just a subset of builds.
            try:
                build_anchor = pd.DataFrame({"Build_Version": plot_df["Build_Version"].astype(str).tolist()})
                build_anchor = build_anchor.drop_duplicates(subset=["Build_Version"], keep="first")
                build_anchor = build_anchor.sort_values("Build_Version")
            except Exception:
                build_anchor = pd.DataFrame()

            # Base series from the trend dataset itself (benchmarks):
            # - benchmarks.total_catalog_items is treated as "New Attributes" per your meaning.
            try:
                fb = plot_df.copy()
                base = pd.DataFrame({"Build_Version": fb["Build_Version"].astype(str)})
                base["New_Attributes"] = pd.to_numeric(fb.get("New_Attributes"), errors="coerce") if "New_Attributes" in fb.columns else pd.NA
                base["Total_Classifications"] = pd.to_numeric(fb.get("Total_Classification"), errors="coerce") if "Total_Classification" in fb.columns else pd.NA
                base["Removed_Attributes"] = pd.to_numeric(fb.get("Removed_Attributes"), errors="coerce") if "Removed_Attributes" in fb.columns else pd.NA
                cls_df = base.drop_duplicates(subset=["Build_Version"], keep="first").sort_values("Build_Version")
            except Exception:
                cls_df = pd.DataFrame()

            # (Intentionally no scan_insights overlay here.)
            # scan_insights is already joined into the base dataset in `load_data()` using scan_id,
            # so the Trend chart stays simple and consistent.

            # Pad missing builds so x-axis always shows full build range.
            try:
                if not build_anchor.empty:
                    if cls_df is None or cls_df.empty:
                        cls_df = build_anchor.copy()
                        cls_df["Total_Classifications"] = pd.NA
                        cls_df["New_Attributes"] = pd.NA
                        cls_df["Removed_Attributes"] = pd.NA
                    else:
                        cls_df = build_anchor.merge(cls_df, on="Build_Version", how="left")
            except Exception:
                pass

            if cls_df is None or cls_df.empty or ("Build_Version" not in cls_df.columns):
                st.caption("Classification Trends: no data available yet for these filters. (Tracker will populate `scan_insights`; fallback data not present either.)")
            else:
                # Ensure template classifier counts are present when possible.
                if "Template_Classifiers" not in cls_df.columns or cls_df["Template_Classifiers"].isna().all():
                    try:
                        engine3 = get_engine()
                        if engine3 is not None and "Build_Version" in cls_df.columns:
                            bvs = [str(x).strip() for x in cls_df["Build_Version"].astype(str).tolist() if str(x).strip()]
                            bvs = list(dict.fromkeys(bvs))
                            params = {}
                            ph = []
                            for i, bv in enumerate(bvs[:200]):
                                k = f"bv{i}"
                                params[k] = bv
                                ph.append(f":{k}")
                            if ph:
                                with engine3.connect() as conn:
                                    rows3 = conn.execute(
                                        text(f"""
                                            SELECT b.version, COUNT(DISTINCT bcm.classifier_id) AS n
                                            FROM builds b
                                            JOIN build_classifier_mappings bcm ON bcm.build_id = b.id
                                            WHERE b.version IN ({",".join(ph)})
                                            GROUP BY b.version
                                        """),
                                        params,
                                    ).fetchall()
                                m3 = {str(r[0]).strip(): (int(r[1]) if r[1] is not None else None) for r in (rows3 or [])}
                                cls_df["Template_Classifiers"] = cls_df["Build_Version"].astype(str).map(lambda v: m3.get(str(v).strip()))
                    except Exception:
                        pass

                # Tiny debug caption: how many builds have any classification data.
                try:
                    present = 0
                    for c in ["Total_Classifications", "New_Attributes", "Removed_Attributes", "Template_Classifiers"]:
                        if c in cls_df.columns:
                            present = max(present, int(cls_df[c].notna().sum()))
                    st.caption(f"Classification data present for {present} / {cls_df['Build_Version'].nunique()} builds in view.")
                except Exception:
                    pass
                figc = go.Figure()
                figc.add_trace(go.Scatter(
                    x=cls_df["Build_Version"], y=cls_df["Total_Classifications"],
                    mode="lines+markers", name="Total Classifications",
                    line=dict(color="#3B82F6", width=3), marker=dict(size=8),
                ))
                figc.add_trace(go.Scatter(
                    x=cls_df["Build_Version"], y=cls_df["New_Attributes"],
                    mode="lines+markers", name="New Attributes",
                    line=dict(color="#10B981", width=3), marker=dict(size=8),
                ))
                figc.add_trace(go.Scatter(
                    x=cls_df["Build_Version"], y=cls_df["Removed_Attributes"],
                    mode="lines+markers", name="Removed Attributes",
                    line=dict(color="#EF4444", width=3), marker=dict(size=8),
                ))
                y_template = cls_df["Template_Classifiers"] if "Template_Classifiers" in cls_df.columns else None
                figc.add_trace(go.Scatter(
                    x=cls_df["Build_Version"], y=y_template,
                    mode="lines+markers", name="Template Classifiers",
                    line=dict(color="#8B5CF6", width=3), marker=dict(size=8),
                ))
                figc.update_layout(
                    title="Classification Trends",
                    xaxis_title="Build Version",
                    yaxis_title="Count",
                    hovermode="x unified",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                )
                figc.update_xaxes(type="category", categoryorder="category ascending")
                st.plotly_chart(figc, width="stretch")

    # Grouped Bar Charts - Restore these
    st.markdown("### Throughput Comparison (Grouped Bar Charts)")
    b_col1, b_col2 = st.columns(2)
    
    with b_col1:
        fig3 = go.Figure()
        if compare_by:
            # For comparisons, show cluster throughput side-by-side
            categories = plot_df[compare_by].unique()
            colors = ['#8B5CF6', '#10B981', '#F59E0B', '#EF4444']
            for i, cat in enumerate(categories):
                cat_df = plot_df[plot_df[compare_by] == cat]
                name = f"{cat}"
                if compare_by == 'is_feature_flags': name = "FF ON" if cat else "FF OFF"
                if compare_by == 'is_acl': name = "ACL ON" if cat else "ACL OFF"
                fig3.add_trace(go.Bar(x=cat_df['Build_Version'], y=cat_df['throughput_cluster_gb_hr'], name=name, marker_color=colors[i % len(colors)]))
        else:
            fig3.add_trace(go.Bar(x=plot_df['Build_Version'], y=plot_df['throughput_cluster_gb_hr'], name='Cluster', marker_color='#8B5CF6'))
            try:
                if "throughput_scanner_gb_hr" in plot_df.columns and plot_df["throughput_scanner_gb_hr"].notna().any():
                    fig3.add_trace(go.Bar(x=plot_df['Build_Version'], y=plot_df['throughput_scanner_gb_hr'], name='Per Scanner', marker_color='#6366F1'))
                if "throughput_thread_gb_hr" in plot_df.columns and plot_df["throughput_thread_gb_hr"].notna().any():
                    fig3.add_trace(go.Bar(x=plot_df['Build_Version'], y=plot_df['throughput_thread_gb_hr'], name='Per Thread', marker_color='#A78BFA'))
            except Exception:
                pass
        
        fig3.update_layout(title="Data Throughput Comparison", barmode='group', xaxis={'type': 'category', 'categoryorder': 'category ascending'}, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        st.plotly_chart(fig3, width="stretch")

    with b_col2:
        fig4 = go.Figure()
        if compare_by:
            categories = plot_df[compare_by].unique()
            colors = ['#10B981', '#8B5CF6', '#F59E0B', '#EF4444']
            for i, cat in enumerate(categories):
                cat_df = plot_df[plot_df[compare_by] == cat]
                name = f"{cat}"
                if compare_by == 'is_feature_flags': name = "FF ON" if cat else "FF OFF"
                if compare_by == 'is_acl': name = "ACL ON" if cat else "ACL OFF"
                fig4.add_trace(go.Bar(x=cat_df['Build_Version'], y=cat_df['throughput_cluster_files_hr'], name=name, marker_color=colors[i % len(colors)]))
        else:
            fig4.add_trace(go.Bar(x=plot_df['Build_Version'], y=plot_df['throughput_cluster_files_hr'], name='Cluster', marker_color='#10B981'))
            try:
                if "throughput_scanner_files_hr" in plot_df.columns and plot_df["throughput_scanner_files_hr"].notna().any():
                    fig4.add_trace(go.Bar(x=plot_df['Build_Version'], y=plot_df['throughput_scanner_files_hr'], name='Per Scanner', marker_color='#34D399'))
                if "throughput_thread_files_hr" in plot_df.columns and plot_df["throughput_thread_files_hr"].notna().any():
                    fig4.add_trace(go.Bar(x=plot_df['Build_Version'], y=plot_df['throughput_thread_files_hr'], name='Per Thread', marker_color='#6EE7B7'))
            except Exception:
                pass
        
        fig4.update_layout(title="File Throughput Comparison", barmode='group', xaxis={'type': 'category', 'categoryorder': 'category ascending'}, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        st.plotly_chart(fig4, width="stretch")

    # Detailed Table
    st.markdown("### Summary Data")
    # Filter for active rows (extra safety layer). Treat NULL/truthy as active.
    try:
        plot_df = plot_df[plot_df['is_active'].apply(lambda x: bool(int(x)) if str(x).strip().isdigit() else bool(x))]
    except Exception:
        try:
            plot_df = plot_df[plot_df['is_active'].apply(lambda x: bool(x))]
        except Exception:
            pass

    # Table order: newest build first (descending) while charts remain ascending.
    try:
        table_df = plot_df.copy()
        table_df["_build_num"] = pd.to_numeric(table_df["Build_Version"], errors="coerce")
        table_df = table_df.sort_values(by=["_build_num", "Build_Version"], ascending=[False, False]).drop(columns=["_build_num"])
    except Exception:
        table_df = plot_df.copy()
    
    # Select columns in desired order: Data Throughput first, then File Throughput
    if compare_by:
        # In compare views (e.g., DS vs DS), per-scanner/per-thread is often missing and not meaningful.
        cols_to_show = [
            'Build_Version', 'Scan_Type',
            'throughput_cluster_gb_hr',
            'throughput_cluster_files_hr',
            'scanner_count', 'thread_count', 'total_scan_hours',
        ]
    else:
        cols_to_show = [
            'Build_Version', 'Scan_Type',
            # Data Throughput columns first
            'throughput_cluster_gb_hr', 'throughput_scanner_gb_hr', 'throughput_thread_gb_hr',
            # File Throughput columns second
            'throughput_cluster_files_hr', 'throughput_scanner_files_hr', 'throughput_thread_files_hr',
            # Configuration columns
            'scanner_count', 'thread_count', 'total_scan_hours'
        ]
    
    # Add comparison column if present
    if compare_by and compare_by not in cols_to_show:
        cols_to_show.append(compare_by)

    # Avoid KeyError when some columns are absent (auto scans).
    cols_to_show = [c for c in cols_to_show if c in table_df.columns]
    disp_df = table_df[cols_to_show].copy() if cols_to_show else table_df.copy()
    
    # Define clearer display names for columns (with line breaks for compact display)
    display_names = {
        "Build_Version": "Build",
        "Scan_Type": "Type",
        # Data Throughput
        "throughput_cluster_gb_hr": "Data Cluster<br>(GB/h)",
        "throughput_scanner_gb_hr": "Data Scanner<br>(GB/h)",
        "throughput_thread_gb_hr": "Data Thread<br>(GB/h)",
        # File Throughput
        "throughput_cluster_files_hr": "Files Cluster<br>(f/h)",
        "throughput_scanner_files_hr": "Files Scanner<br>(f/h)",
        "throughput_thread_files_hr": "Files Thread<br>(f/h)",
        # Configuration
        "scanner_count": "Scanners",
        "thread_count": "Threads",
        "total_scan_hours": "Hours",
        "is_feature_flags": "FF",
        "is_acl": "ACL"
    }
    
    # Rename columns and format numeric values for HTML display
    disp_df = disp_df.rename(columns=display_names)
    
    # Apply numeric formatting to the DataFrame
    num_cols = ["Data Cluster<br>(GB/h)", "Data Scanner<br>(GB/h)", "Data Thread<br>(GB/h)", "Files Thread<br>(f/h)", "Hours"]
    def format_two_dec_round(val):
        if pd.isnull(val):
            return "N/A"
        try:
            rounded = round(float(val), 2)
            return f"{rounded:.2f}"
        except Exception:
            return "N/A"
    for col in num_cols:
        if col in disp_df.columns:
            disp_df[col] = disp_df[col].apply(format_two_dec_round)
            
    int_cols = ["Files Cluster<br>(f/h)", "Files Scanner<br>(f/h)", "Scanners", "Threads"]
    for col in int_cols:
        if col in disp_df.columns:
            disp_df[col] = disp_df[col].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "N/A")

    render_centered_table(disp_df)


def page_classifier_build_diffs():
    st.title("🧩 Classifiers: Build-by-Build Changes")
    st.caption("Shows newly added/removed enabled classifiers per build (from `build_classifier_mappings`).")

    if not engine:
        st.error("Database not connected.")
        return

    ensure_build_templates_table()
    ensure_test_environments_table(engine)
    try:
        with engine.connect() as conn:
            env_rows = conn.execute(text("SELECT id, env_name, is_active FROM test_environments ORDER BY is_active DESC, env_name")).fetchall()
    except Exception:
        env_rows = []

    env_options = [("All environments", None)]
    for r in env_rows or []:
        try:
            env_options.append((f"{str(r[1] or '').strip()} (id={int(r[0])})", int(r[0])))
        except Exception:
            pass

    sel_label = st.selectbox("Environment", [x[0] for x in env_options], index=0)
    sel_env_id = None
    for lbl, eid in env_options:
        if lbl == sel_label:
            sel_env_id = eid
            break

    # Load sets per build
    try:
        with engine.connect() as conn:
            if sel_env_id is None:
                rows = conn.execute(text("""
                    SELECT b.version, c.name
                    FROM build_classifier_mappings bcm
                    JOIN builds b ON b.id = bcm.build_id
                    JOIN classifiers c ON c.id = bcm.classifier_id
                    ORDER BY b.version, c.name
                """)).fetchall()
            else:
                rows = conn.execute(text("""
                    SELECT b.version, c.name
                    FROM build_classifier_mappings bcm
                    JOIN builds b ON b.id = bcm.build_id
                    JOIN classifiers c ON c.id = bcm.classifier_id
                    WHERE bcm.environment_id = :e
                    ORDER BY b.version, c.name
                """), {"e": int(sel_env_id)}).fetchall()
    except Exception as ex:
        st.error(f"Failed to load classifier mappings: {ex}")
        return

    build_map: Dict[str, set] = {}
    for ver, name in rows or []:
        v = str(ver or "").strip()
        n = str(name or "").strip()
        if not v or not n:
            continue
        build_map.setdefault(v, set()).add(n)

    if not build_map:
        st.warning("No build classifier mappings found yet. Let `tracker.py` run (it will sync global classifiers per build).")
        return

    # Sort builds numerically when possible
    def _build_key(v: str):
        try:
            return float(v)
        except Exception:
            return float("inf")

    builds = sorted(build_map.keys(), key=_build_key)
    rows_out = []
    prev_set: Optional[set] = None
    prev_build = ""
    for bver in builds:
        cur_set = build_map.get(bver) or set()
        added = sorted(cur_set - (prev_set or set()))
        removed = sorted((prev_set or set()) - cur_set) if prev_set is not None else []
        rows_out.append({
            "Build": bver,
            "Classifier Count": len(cur_set),
            "Added vs prev": len(added) if prev_set is not None else None,
            "Removed vs prev": len(removed) if prev_set is not None else None,
            "_added_list": added,
            "_removed_list": removed,
            "_prev": prev_build,
        })
        prev_set = cur_set
        prev_build = bver

    summary = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows_out])
    st.dataframe(summary, width="stretch", hide_index=True)

    st.divider()
    st.markdown("### Compare any two builds (classifier names)")
    st.caption(
        "Diffs the **enabled classifier name sets** from `build_classifier_mappings` for the environment you selected above. "
        "No extra API calls — use this to see what changed between e.g. 249.14 and 231.96."
    )
    if len(builds) < 2:
        st.info("Need at least two builds in the mapping table to compare side by side.")
    else:
        pc1, pc2 = st.columns(2)
        with pc1:
            pair_a = st.selectbox("Build A", builds, key="cl_pair_build_a")
        with pc2:
            cand_b = [x for x in builds if x != pair_a]
            pair_b = st.selectbox("Build B", cand_b, index=0, key="cl_pair_build_b") if cand_b else pair_a
        set_a = build_map.get(pair_a, set())
        set_b = build_map.get(pair_b, set())
        only_a = sorted(set_a - set_b)
        only_b = sorted(set_b - set_a)
        both_n = len(set_a & set_b)
        m1, m2, m3 = st.columns(3)
        m1.metric(f"Only in {pair_a}", len(only_a))
        m2.metric(f"Only in {pair_b}", len(only_b))
        m3.metric("In both builds", both_n)
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            st.markdown(f"**Only in {pair_a}** ({len(only_a)})")
            st.dataframe(
                pd.DataFrame({"Classifier": only_a}) if only_a else pd.DataFrame({"Classifier": []}),
                width="stretch",
                hide_index=True,
                height=min(400, 120 + 28 * max(1, len(only_a))),
            )
        with tc2:
            st.markdown(f"**Only in {pair_b}** ({len(only_b)})")
            st.dataframe(
                pd.DataFrame({"Classifier": only_b}) if only_b else pd.DataFrame({"Classifier": []}),
                width="stretch",
                hide_index=True,
                height=min(400, 120 + 28 * max(1, len(only_b))),
            )
        with tc3:
            st.markdown(f"**In both** ({both_n})")
            st.caption("Same name present in both builds (metadata may still differ — see Deep-Dive below).")
            st.dataframe(
                pd.DataFrame({"Classifier": sorted(set_a & set_b)}),
                width="stretch",
                hide_index=True,
                height=min(400, 120 + 28 * max(1, both_n)),
            )

    st.divider()
    st.markdown("### Added / Removed details")
    for r in reversed(rows_out):  # newest first
        bver = r["Build"]
        prev = r["_prev"]
        added = r["_added_list"]
        removed = r["_removed_list"]
        label = f"Build {bver}" + (f" (vs {prev})" if prev else "")
        with st.expander(label, expanded=False):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Added**")
                st.dataframe(pd.DataFrame({"Classifier": added}) if added else pd.DataFrame({"Classifier": []}), width="stretch", hide_index=True)
            with c2:
                st.markdown("**Removed**")
                st.dataframe(pd.DataFrame({"Classifier": removed}) if removed else pd.DataFrame({"Classifier": []}), width="stretch", hide_index=True)

    st.divider()
    st.markdown("### 🔍 Classifier Deep-Dive Comparison")
    st.caption(
        "Compares **stored classifier JSON** (`classifier_details_snapshots`: proximity, sensitivity, enabled, …). "
        "That is separate from the **name lists** in `build_classifier_mappings` used in the summary table above — "
        "use **Compare any two builds** for name-only diffs between any versions."
    )

    ensure_classifier_details_snapshots_table()
    snap_summary_df = load_classifier_detail_snapshot_builds_summary(sel_env_id)
    snapshot_build_versions: List[str] = []
    if not snap_summary_df.empty:
        snapshot_build_versions = (
            snap_summary_df["build_version"].astype(str).str.strip().drop_duplicates().tolist()
        )

    st.markdown("**Builds that have metadata snapshots** (Deep-Dive only lists these)")
    if snap_summary_df.empty:
        st.info(
            f"No rows in `classifier_details_snapshots` for **{sel_label}** yet. "
            "Use **Manual Refresh Snapshots** after choosing a concrete environment, or let **tracker** sync classifier details. "
            "Each capture is stored under that environment’s **live** `releaseVersion` from `/about`."
        )
    else:
        st.dataframe(
            snap_summary_df,
            width="stretch",
            hide_index=True,
            height=min(260, 80 + 28 * max(1, len(snap_summary_df))),
        )

    refresh_env_id = sel_env_id
    if sel_env_id is None:
        st.info("For **Manual Refresh**, choose which environment’s BigID API to read from.")
        local_env_options = [lbl for lbl, eid in env_options if eid is not None]
        if local_env_options:
            sel_local = st.selectbox("Refresh From Environment", local_env_options, key="cl_refresh_local_env")
            for lbl, eid in env_options:
                if lbl == sel_local:
                    refresh_env_id = eid
                    break
        else:
            st.warning("No environments configured for manual refresh.")

    _, btn_col = st.columns([3, 1])
    with btn_col:
        fetch_btn = st.button(
            "Manual Refresh Snapshots",
            width="stretch",
            type="secondary",
            help="Fetches classifier list + per-classifier details from the selected environment; stored under live /about version.",
        )

    if fetch_btn:
        if refresh_env_id is None:
            st.error("Please select a specific Environment to refresh snapshots from.")
        else:
            env_row = get_test_environment_by_id(refresh_env_id)
            if env_row:
                with st.spinner(f"Capturing classifier metadata from {env_row['env_name']}..."):
                    try:
                        token = fetch_auth_token_for_env(env_row)
                        base_url = env_row.get("base_url")
                        api_path = env_row.get("classifiers_api_path") or "/api/v1/classifications"

                        # Get the actual current version from /about to ensure we save to the right build_id
                        about = fetch_about(base_url, token)
                        if not about:
                            st.error(f"Failed to connect to {base_url}. Please check if the environment is up and reachable.")
                        else:
                            env_version = about.get("releaseVersion") or about.get("version") or env_row.get("version")

                            url = build_api_url(base_url, api_path)
                            headers_raw = {"authorization": token} if token else {}
                            headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}

                            resp = requests.get(url, headers=headers_raw, timeout=60)
                            if resp.status_code in (401, 403) and token:
                                resp = requests.get(url, headers=headers_bearer, timeout=60)

                            if resp.status_code == 200:
                                list_with_ids = extract_classifier_list_with_ids(resp.json())
                                if list_with_ids:
                                    with engine.connect() as conn:
                                        target_bid = get_or_create_id(conn, "builds", "version", str(env_version))

                                        success_count = 0
                                        for item in list_with_ids:
                                            detail = fetch_classifier_detail_from_api(base_url, item["id"], token)
                                            if detail:
                                                save_classifier_details_snapshot(
                                                    refresh_env_id, target_bid, item["id"], item["name"], detail
                                                )
                                                success_count += 1
                                    if success_count > 0:
                                        st.success(
                                            f"Successfully captured {success_count} classifier snapshots for version {env_version}"
                                        )
                                        st.rerun()
                                    else:
                                        st.error(
                                            "Fetched classifier list, but failed to retrieve details for any individual classifier. Check API permissions."
                                        )
                                else:
                                    st.warning("API returned success but found 0 classifiers in this environment.")
                            else:
                                st.error(f"API Error ({resp.status_code}): Could not fetch classifier list from {url}")
                    except Exception as e:
                        st.error(f"Manual sync failed: {e}")

    if len(snapshot_build_versions) < 2:
        st.warning(
            f"Deep-Dive needs **at least two** build versions with stored metadata for **{sel_label}**. "
            f"You currently have **{len(snapshot_build_versions)}**. "
            "The dropdowns used to show every build from **name mappings**, which made it look like you could compare metadata — "
            "but metadata only exists after refresh/tracker. "
            "Refresh once per version you care about (or use **All environments** if snapshots exist from any env)."
        )
    else:
        dc1, dc2 = st.columns(2)
        with dc1:
            build_a = st.selectbox(
                "Compare Build (Baseline)",
                snapshot_build_versions,
                index=max(0, len(snapshot_build_versions) - 2),
                key="cl_detail_build_a",
            )
        with dc2:
            cand_b = [v for v in snapshot_build_versions if v != build_a]
            build_b = st.selectbox(
                "To Build (Newer)",
                cand_b,
                index=max(0, len(cand_b) - 1) if cand_b else 0,
                key="cl_detail_build_b",
            )

        with engine.connect() as conn:
            bid_a = (
                conn.execute(text("SELECT id FROM builds WHERE version = :v LIMIT 1"), {"v": build_a}).scalar()
                if build_a
                else None
            )
            bid_b = (
                conn.execute(text("SELECT id FROM builds WHERE version = :v LIMIT 1"), {"v": build_b}).scalar()
                if build_b
                else None
            )

        snap_a = load_classifier_snapshots_for_build(sel_env_id, bid_a) if bid_a else []
        snap_b = load_classifier_snapshots_for_build(sel_env_id, bid_b) if bid_b else []

        if not snap_a or not snap_b:
            st.info(
                "Could not load snapshots for this pair with the current **Environment** filter. "
                f"Try **All environments**, or run **Manual Refresh** for **{sel_label}** for both versions."
            )
        else:
            map_a = {s["classifier_name"]: s["snapshot"] for s in snap_a}
            map_b = {s["classifier_name"]: s["snapshot"] for s in snap_b}
            all_names = sorted(set(map_a.keys()) | set(map_b.keys()))
            
            diff_rows = []
            for name in all_names:
                sa, sb = map_a.get(name, {}), map_b.get(name, {})
                changed_keys = []
                for key in CLASSIFIER_DETAIL_KEYS:
                    val_a = str(sa.get(key) if sa.get(key) is not None else "").strip()
                    val_b = str(sb.get(key) if sb.get(key) is not None else "").strip()
                    if val_a != val_b:
                        changed_keys.append(key)
                
                status = "Identical"
                if name not in map_a: status = "Added"
                elif name not in map_b: status = "Removed"
                elif changed_keys: status = "Changed"
                
                diff_rows.append({
                    "Classifier": name,
                    "Status": status,
                    "Changes": ", ".join(changed_keys) if changed_keys else "-",
                    "_map_a": sa,
                    "_map_b": sb,
                    "_changed_keys": changed_keys
                })

            # Filter for changes by default?
            show_all = st.checkbox("Show identical classifiers", value=False)
            if not show_all:
                display_rows = [r for r in diff_rows if r["Status"] != "Identical"]
            else:
                display_rows = diff_rows

            if not display_rows:
                st.success("No differences found between these two builds.")
            else:
                # Premium Table with expansion for details
                for r in display_rows:
                    status_color = "#10b981" if r["Status"] == "Added" else "#ef4444" if r["Status"] == "Removed" else "#f59e0b" if r["Status"] == "Changed" else "#6b7280"
                    
                    with st.expander(f"**{r['Classifier']}** — {r['Status']}", expanded=(r["Status"] != "Identical")):
                        if r["Status"] == "Added":
                            st.write("Newly added in this build version.")
                            st.json(r["_map_b"])
                        elif r["Status"] == "Removed":
                            st.write("Removed/Disabled in this build version.")
                            st.json(r["_map_a"])
                        else:
                            # Side-by-side diff
                            cols = st.columns(2)
                            with cols[0]:
                                st.markdown(f"**{build_a} (Baseline)**")
                            with cols[1]:
                                st.markdown(f"**{build_b} (Newer)**")
                            
                            for k in CLASSIFIER_DETAIL_KEYS:
                                va = str(r["_map_a"].get(k) if r["_map_a"].get(k) is not None else "").strip()
                                vb = str(r["_map_b"].get(k) if r["_map_b"].get(k) is not None else "").strip()
                                
                                if va != vb:
                                    c1, c2 = st.columns(2)
                                    with c1:
                                        st.error(f"**{k}**: {va}")
                                    with c2:
                                        st.success(f"**{k}**: {vb}")
                                else:
                                    if show_all:
                                        st.text(f"{k}: {va}")


@st.cache_data(ttl=600)
def load_auto_trend_dataset():
    if not engine:
        return pd.DataFrame()
    ensure_auto_scan_benchmarks_table()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    ab.scan_id,
                    COALESCE(te.env_name, '') AS environment_name,
                    COALESCE(ab.build_version, '') AS build_version,
                    ab.run_date,
                    COALESCE(ab.scenario_type, '') AS scenario_type,
                    COALESCE(ab.scan_type, '') AS scan_type,
                    COALESCE(ab.data_source_type, '') AS data_source_type,
                    COALESCE(ab.data_type, '') AS data_type,
                    COALESCE(ab.file_set_type, '') AS file_set_type,
                    COALESCE(NULLIF(ab.scan_name, ''), NULLIF(sp.scan_name, ''), '') AS scan_name,
                    ab.scan_start_time,
                    ab.scan_end_time,
                    ab.total_scan_hours,
                    ab.total_size_gb,
                    ab.total_files,
                    ab.objects_scanned,
                    ab.total_classification,
                    ab.new_attributes,
                    ab.removed_attributes,
                    ab.updated_at
                FROM auto_scan_benchmarks ab
                LEFT JOIN test_environments te ON te.id = ab.environment_id
                LEFT JOIN scan_parents sp ON sp.scan_id = ab.scan_id
                WHERE LOWER(COALESCE(sp.scan_status, '')) NOT IN ('stopped', 'aborted')
                  AND LOWER(COALESCE(sp.scan_name, '')) NOT LIKE 'instant scan data%'
                  AND LOWER(COALESCE(ab.scan_name, '')) NOT LIKE 'instant scan data%'
                ORDER BY ab.run_date DESC, ab.updated_at DESC
            """)).fetchall()
        cols = [
            "scan_id", "environment_name", "build_version", "run_date", "scenario_type",
            "scan_type", "data_source_type", "data_type", "file_set_type", "scan_name",
            "scan_start_time", "scan_end_time", "total_scan_hours",
            "total_size_gb_table", "total_files_table", "objects_scanned", "total_classification", 
            "new_attributes", "removed_attributes", "updated_at"
        ]
        df = pd.DataFrame(rows, columns=cols)
        if df.empty:
            return df
        df["run_date"] = pd.to_datetime(df["run_date"], errors="coerce")
        df["scan_start_time"] = pd.to_datetime(df["scan_start_time"], errors="coerce")
        df["scan_end_time"] = pd.to_datetime(df["scan_end_time"], errors="coerce")
        for col in ["total_scan_hours", "total_size_gb_table", "total_files_table", "objects_scanned", "total_classification", "new_attributes", "removed_attributes"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # If hours is missing/invalid/tiny, recompute from start/end.
        try:
            calc = (df["scan_end_time"] - df["scan_start_time"]).dt.total_seconds() / 3600.0
            mask = df["total_scan_hours"].isna() | (df["total_scan_hours"] <= 0) | (df["total_scan_hours"] < 0.01)
            df.loc[mask & calc.notna() & (calc > 0), "total_scan_hours"] = calc[mask & calc.notna() & (calc > 0)]
        except Exception:
            pass
        return df
    except Exception:
        return pd.DataFrame()


def load_auto_trend_as_manual_like_df():
    auto_df = load_auto_trend_dataset()
    if auto_df.empty:
        return pd.DataFrame()

    size_map = {}
    if engine:
        try:
            with engine.connect() as conn:
                rows = conn.execute(text("SELECT name, total_size_gb FROM file_set_types")).fetchall()
                for name, size in rows:
                    key = str(name or "").strip().lower()
                    if not key:
                        continue
                    try:
                        size_map[key] = float(size) if size is not None else None
                    except Exception:
                        size_map[key] = None
        except Exception:
            size_map = {}

    out = auto_df.copy()
    out["id"] = out["scan_id"]
    out["raw_file_set_type"] = out["file_set_type"].astype(str)
    out["Build_Version"] = out["build_version"].astype(str)
    out["Date"] = pd.to_datetime(out["run_date"], errors="coerce")
    out["Scan_Type"] = out["scan_type"].astype(str)
    out["Data_Source_Type"] = out["data_source_type"].astype(str)
    out["Data_Type"] = out["data_type"].astype(str)
    out["scan_name"] = out.get("scan_name", "").astype(str)

    # Re-infer core fields from scan_name when the auto table fields are missing/garbage.
    try:
        inferred = out["scan_name"].astype(str).apply(lambda s: infer_add_benchmark_defaults_from_scan_name(s) if str(s).strip() else {})
        inf_build = inferred.apply(lambda d: str((d or {}).get("build_version") or "").strip())
        inf_ds = inferred.apply(lambda d: str((d or {}).get("data_source_type") or "").strip())
        inf_scan_type = inferred.apply(lambda d: str((d or {}).get("scan_type") or "").strip())
        inf_fs = inferred.apply(lambda d: str((d or {}).get("file_set_type") or "").strip())

        def _looks_like_ds(v: str) -> bool:
            s = str(v or "").strip()
            if not s:
                return False
            if any(ch.isdigit() for ch in s) and len(s) > 6:
                return False
            if " " in s:
                return False
            if len(s) > 12:
                return False
            return True

        # Build version: if empty/unknown, use inferred.
        bad_build = out["Build_Version"].astype(str).str.strip().isin(["", "Unknown", "unknown", "nan", "None"])
        out.loc[bad_build & (inf_build != ""), "Build_Version"] = inf_build[bad_build & (inf_build != "")]

        # DS: if not a compact token, use inferred.
        bad_ds = ~out["Data_Source_Type"].astype(str).apply(_looks_like_ds)
        out.loc[bad_ds & (inf_ds != ""), "Data_Source_Type"] = inf_ds[bad_ds & (inf_ds != "")]

        # Scan type: if missing, use inferred.
        bad_st = out["Scan_Type"].astype(str).str.strip().isin(["", "nan", "None"])
        out.loc[bad_st & (inf_scan_type != ""), "Scan_Type"] = inf_scan_type[bad_st & (inf_scan_type != "")]

        # File set type: if missing, use inferred (will be normalized below).
        bad_fs = out["file_set_type"].astype(str).str.strip().isin(["", "nan", "None"])
        out.loc[bad_fs & (inf_fs != ""), "file_set_type"] = inf_fs[bad_fs & (inf_fs != "")]
    except Exception:
        pass

    def normalize_file_set(v):
        s = str(v or "")
        u = s.upper()
        if "SMALL FILES" in u:
            return "Small Files"
        if "LARGE FILES" in u:
            return "Large Files"
        if "MIXED" in u:
            return "Mixed Files"
        return s

    out["file_set_type"] = out["file_set_type"].apply(normalize_file_set)
    
    # Use table values if present, otherwise use mapping
    out["Total_Files"] = out["total_files_table"].fillna(pd.to_numeric(out["objects_scanned"], errors="coerce"))
    out["Total_Folders"] = pd.NA
    # benchmarks.total_catalog_items in the manual trend schema means "New Attributes"
    out["New_Attributes"] = pd.to_numeric(out.get("new_attributes"), errors="coerce")
    out["Total_Classification"] = pd.to_numeric(out.get("total_classification"), errors="coerce")
    out["Removed_Attributes"] = pd.to_numeric(out.get("removed_attributes"), errors="coerce")
    out["total_scan_hours"] = pd.to_numeric(out["total_scan_hours"], errors="coerce")
    out["is_active"] = 1
    out["is_acl"] = 0
    out["is_ocr"] = 0
    out["is_feature_flags"] = 0
    out["scanner_count"] = pd.NA
    out["thread_count"] = pd.NA

    # Size logic: table size first, then mapping fallback
    out["Total_Size_GB"] = out["total_size_gb_table"]
    missing_size = out["Total_Size_GB"].isna()
    if missing_size.any():
        out.loc[missing_size, "Total_Size_GB"] = out.loc[missing_size, "raw_file_set_type"].astype(str).str.strip().str.lower().map(size_map)
    
    missing_size = out["Total_Size_GB"].isna()
    if missing_size.any():
        out.loc[missing_size, "Total_Size_GB"] = out.loc[missing_size, "file_set_type"].astype(str).str.strip().str.lower().map(size_map)
        
    missing_size = out["Total_Size_GB"].isna()
    if missing_size.any():
        ds_key = out.loc[missing_size, "Data_Source_Type"].astype(str).str.strip().str.lower()
        fs_key = out.loc[missing_size, "file_set_type"].astype(str).str.strip().str.lower()
        combo = (ds_key + " " + fs_key).str.strip().str.lower()
        out.loc[missing_size, "Total_Size_GB"] = combo.map(size_map)

    out["avg_file_size"] = pd.NA
    out["Scenario_Type"] = out["scenario_type"].astype(str)

    hours = pd.to_numeric(out["total_scan_hours"], errors="coerce")
    files = pd.to_numeric(out["Total_Files"], errors="coerce")
    size_gb = pd.to_numeric(out["Total_Size_GB"], errors="coerce")
    out["throughput_cluster_files_hr"] = files / hours
    out["throughput_cluster_gb_hr"] = size_gb / hours

    # Relax filtering for build version but keep hours requirement
    out["Build_Version"] = out["Build_Version"].apply(lambda x: x if str(x).strip() else "Unknown")
    out["Scan_Status"] = "Completed"
    if "scan_id" not in out.columns and "id" in out.columns:
        out["scan_id"] = out["id"]
    return out

def load_latest_scan_progress_map_for_scan_ids(scan_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Batch-load latest scan_progress metrics for scan_ids."""
    if not engine:
        return {}
    cleaned: List[str] = []
    seen: set[str] = set()
    for sid in (scan_ids or []):
        x = str(sid or '').strip()
        if not x or x in seen:
            continue
        seen.add(x)
        cleaned.append(x)
    if not cleaned:
        return {}
    try:
        from sqlalchemy import bindparam
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT sp.scan_id,
                           sp.scan_state,
                           sp.completed_parts, sp.failed_parts, sp.total_parts_scanned,
                           sp.objects_scanned, sp.objects_failed, sp.objects_with_pi,
                           sp.total_classifications, sp.newly_added_classifications, sp.false_removed_classifications,
                           sp.timestamp, sp.captured_at_utc
                    FROM scan_progress sp
                    JOIN (
                        SELECT z.scan_id, MAX(z.id) AS max_id
                        FROM scan_progress z
                        JOIN (
                            SELECT scan_id, MAX(COALESCE(captured_at_utc, timestamp)) AS mx
                            FROM scan_progress
                            WHERE scan_id IN :ids
                            GROUP BY scan_id
                        ) t
                          ON t.scan_id = z.scan_id
                         AND COALESCE(z.captured_at_utc, z.timestamp) = t.mx
                        WHERE z.scan_id IN :ids
                        GROUP BY z.scan_id
                    ) u ON u.max_id = sp.id
                """).bindparams(bindparam('ids', expanding=True)),
                {'ids': cleaned},
            ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows or []:
            sid = str(r[0] or '').strip()
            if not sid:
                continue
            out[sid] = {
                'scan_state': str(r[1] or '').strip(),
                'completed_parts': r[2],
                'failed_parts': r[3],
                'total_parts_scanned': r[4],
                'objects_scanned': r[5],
                'objects_failed': r[6],
                'objects_with_pi': r[7],
                'total_classifications': r[8],
                'newly_added_classifications': r[9],
                'false_removed_classifications': r[10],
                'timestamp': r[11],
                'captured_at_utc': r[12],
            }
        return out
    except Exception:
        return {}


def load_scan_parents_runs_as_manual_like_df(*, include_inactive_envs: bool = False, limit: int = 2000) -> pd.DataFrame:
    """Use scan_parents + latest scan_progress + scan_insights to build a runs dataset."""
    if not engine:
        return pd.DataFrame()
    try:
        with engine.connect() as conn:
            where_env = '' if include_inactive_envs else 'AND COALESCE(te.is_active,1)=1'
            rows = conn.execute(text(f"""
                SELECT
                    sp.scan_id,
                    sp.scan_name,
                    sp.environment_id,
                    sp.scan_type,
                    sp.scan_status,
                    sp.started_at,
                    sp.completed_at,
                    sp.updated_at,
                    COALESCE(te.env_name,'') AS env_name
                FROM scan_parents sp
                LEFT JOIN test_environments te ON te.id = sp.environment_id
                WHERE LOWER(COALESCE(sp.scan_status,'')) NOT IN ('stopped','aborted')
                  AND LOWER(COALESCE(sp.scan_name,'')) NOT LIKE 'instant scan data%'
                  {where_env}
                ORDER BY sp.updated_at DESC
                LIMIT :lim
            """), {'lim': int(limit)}).fetchall()
        cols = ['scan_id','scan_name','environment_id','scan_type_raw','scan_status','started_at','completed_at','updated_at','env_name']
        df = pd.DataFrame(rows, columns=cols)
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return df

    # Parse basics from scan_name
    try:
        parsed = df['scan_name'].astype(str).apply(lambda x: infer_add_benchmark_defaults_from_scan_name(x) if str(x).strip() else {})
        df['Build_Version'] = parsed.apply(lambda d: str((d or {}).get('build_version') or '').strip())
        df['Data_Source_Type'] = parsed.apply(lambda d: str((d or {}).get('data_source_type') or '').strip())
        df['Scan_Type'] = parsed.apply(lambda d: str((d or {}).get('scan_type') or '').strip())
        df['file_set_type'] = parsed.apply(lambda d: str((d or {}).get('file_set_type') or '').strip())
        df['Scenario_Type'] = parsed.apply(lambda d: str((d or {}).get('scenario_type') or '').strip())
    except Exception:
        df['Build_Version'] = ''
        df['Data_Source_Type'] = ''
        df['Scan_Type'] = ''
        df['file_set_type'] = ''
        df['Scenario_Type'] = ''

    def _norm_fs(v: str) -> str:
        u = str(v or '').upper()
        if 'SMALL FILE' in u:
            return 'Small Files'
        if 'LARGE FILE' in u:
            return 'Large Files'
        if 'CSV' in u:
            return 'CSV Files'
        if 'MIXED' in u:
            return 'Mixed Files'
        return str(v or '').strip()
    df['file_set_type'] = df['file_set_type'].apply(_norm_fs)

    try:
        df['started_at_dt'] = pd.to_datetime(df['started_at'], errors='coerce', utc=True, format='mixed')
    except TypeError:
        df['started_at_dt'] = pd.to_datetime(df['started_at'], errors='coerce', utc=True)

    try:
        df['completed_at_dt'] = pd.to_datetime(df['completed_at'], errors='coerce', utc=True, format='mixed')
    except TypeError:
        df['completed_at_dt'] = pd.to_datetime(df['completed_at'], errors='coerce', utc=True)
    df['Date'] = df['completed_at_dt'].fillna(df['started_at_dt'])

    try:
        df['total_scan_hours'] = (df['completed_at_dt'] - df['started_at_dt']).dt.total_seconds() / 3600.0
    except Exception:
        df['total_scan_hours'] = pd.NA

    scan_ids = df['scan_id'].astype(str).tolist()
    pmap = load_latest_scan_progress_map_for_scan_ids(scan_ids)

    def _pget(sid: str, k: str):
        try:
            return (pmap.get(str(sid), {}) or {}).get(k)
        except Exception:
            return None

    for col, key in [
        ('scan_state_progress','scan_state'),
        ('completed_parts','completed_parts'),
        ('failed_parts','failed_parts'),
        ('total_parts_scanned','total_parts_scanned'),
        ('objects_scanned','objects_scanned'),
        ('objects_failed','objects_failed'),
        ('objects_with_pi','objects_with_pi'),
        ('total_classifications','total_classifications'),
        ('newly_added_classifications','newly_added_classifications'),
        ('false_removed_classifications','false_removed_classifications'),
    ]:
        df[col] = df['scan_id'].apply(lambda sid: _pget(sid, key))

    try:
        imap = load_scan_insight_map_for_scan_ids(scan_ids)
    except Exception:
        imap = {}

    def _iget(sid: str, k: str):
        try:
            return (imap.get(str(sid), {}) or {}).get(k)
        except Exception:
            return None

    df['distinct_classification_attributes'] = df['scan_id'].apply(lambda sid: _iget(sid, 'distinct_classification_attributes'))
    df['New_Attributes'] = df['scan_id'].apply(lambda sid: _iget(sid, 'new_attributes_count'))
    df['Removed_Attributes'] = df['scan_id'].apply(lambda sid: _iget(sid, 'removed_attributes_count'))
    df['Total_Classification'] = df['scan_id'].apply(lambda sid: _iget(sid, 'total_classification'))

    mask_tc = df['Total_Classification'].isna() | (df['Total_Classification'].astype(str).str.strip() == '')
    df.loc[mask_tc, 'Total_Classification'] = df.loc[mask_tc, 'total_classifications']

    df['Total_Files'] = df['objects_scanned']

    # For throughput, prefer actual scan_insights.total_size_gb; fall back to expected size from file_set_types.
    df['Total_Size_GB'] = df['scan_id'].apply(lambda sid: _iget(sid, 'total_size_gb'))
    try:
        missing = df['Total_Size_GB'].isna()
        if missing.any():
            df.loc[missing, 'Total_Size_GB'] = df.loc[missing, 'scan_name'].astype(str).apply(
                lambda nm: (resolve_expected_totals_for_scan(nm) or {}).get('expected_total_size_gb')
            )
    except Exception:
        pass

    df['Total_Folders'] = pd.NA
    df['Data_Type'] = ''
    df['is_active'] = 1
    df['id'] = df['scan_id'].astype(str)
    
    # Standardize column for dashboard consumption
    if 'scan_status' in df.columns:
        df['Scan_Status'] = df['scan_status']

    return df



def render_auto_trend_analysis(title, file_set_keyword=None, scan_type_filter=None):
    auto_like = load_auto_trend_as_manual_like_df()
    if auto_like.empty:
        st.markdown(f"## 📈 {title}")
        st.warning("No usable auto-scan rows for trend analysis yet.")
        return

    target_file_set = None
    kw = str(file_set_keyword or "").strip().lower()
    if kw.startswith("small"):
        target_file_set = "Small Files"
    elif kw.startswith("large"):
        target_file_set = "Large Files"

    render_trend_analysis(
        auto_like,
        title,
        file_set_filter=target_file_set,
        scan_type_filter=scan_type_filter,
        show_standard_filters=False,
    )


def page_edit_benchmark(df):
    st.markdown("## 🛠️ Record Management Console")
    if df.empty:
        st.warning("No data available to edit.")
        return

    st.markdown("Edit records directly in the table below. Use search to narrow the list.")
    search_term = st.text_input("Search (Build, Scan Type, Data Source, ID)")
    edit_df = df.copy()
    if search_term:
        term = search_term.strip().lower()
        edit_df = edit_df[
            edit_df['Build_Version'].astype(str).str.lower().str.contains(term) |
            edit_df['Scan_Type'].astype(str).str.lower().str.contains(term) |
            edit_df['Data_Source_Type'].astype(str).str.lower().str.contains(term) |
            edit_df['id'].astype(str).str.lower().str.contains(term)
        ]
    if 'throughput_cluster_gb_hr' in edit_df.columns:
        edit_df = edit_df[edit_df['throughput_cluster_gb_hr'].notna()]
    
    # Always show all active records in the edit table

    editable_cols = [
        'is_active',
        'id',
        'Build_Version',
        'Date',
        'Scenario_Type',
        'Scan_Type',
        'Data_Source_Type',
        'Data_Type',
        'file_set_type',
        'Total_Files',
        'Total_Folders',
        'Total_Size_GB',
        'New_Attributes',
        'avg_file_size',
        'total_scan_hours',
        'throughput_cluster_gb_hr',
        'throughput_scanner_gb_hr',
        'throughput_thread_gb_hr',
        'throughput_cluster_files_hr',
        'throughput_scanner_files_hr',
        'throughput_thread_files_hr',
        'scanner_count',
        'thread_count',
        'is_acl',
        'is_ocr',
        'is_feature_flags',
        'feature_flags',
        'comment',
        'config_id'
    ]
    cols_to_show = [c for c in editable_cols if c in edit_df.columns]
    table_df = edit_df[cols_to_show].copy()
    if 'Date' in table_df.columns:
        try:
            table_df['Date'] = pd.to_datetime(table_df['Date'], errors='coerce', format='mixed')
        except TypeError:
            table_df['Date'] = pd.to_datetime(table_df['Date'], errors='coerce')
        table_df['Date'] = table_df['Date'].dt.date
    if 'total_scan_hours' in table_df.columns:
        table_df['total_scan_hours'] = table_df['total_scan_hours'].apply(
            lambda x: round(float(x), 2) if pd.notnull(x) else x
        )
    table_df.insert(0, "Delete", False)
    # Force JSON-serializable types for data editor
    table_df = pd.DataFrame(table_df.to_dict(orient="records"))
    table_df = table_df.reset_index(drop=True)

    disabled_cols = {
        'id', 'Build_Version', 'config_id', 'scanner_count', 'thread_count',
        'is_acl', 'is_ocr', 'is_feature_flags', 'feature_flags', 'comment',
        'total_scan_hours'
    }

    edited_df = st.data_editor(
        table_df,
        width="stretch",
        hide_index=True,
        num_rows="fixed",
        disabled=[c for c in disabled_cols if c in table_df.columns],
        column_config={
            "Delete": st.column_config.CheckboxColumn("Delete"),
            "Date": st.column_config.DateColumn("Run Date"),
            "Total_Files": st.column_config.NumberColumn("Total Files", min_value=0, step=1000),
            "Total_Folders": st.column_config.NumberColumn("Total Folders", min_value=0, step=100),
            "Total_Size_GB": st.column_config.NumberColumn("Total Size (GB)", min_value=0.0, step=0.1),
            "New_Attributes": st.column_config.NumberColumn("New Attributes", min_value=0, step=1000),
            "total_scan_hours": st.column_config.NumberColumn("Scan Hours", min_value=0.0, step=0.1, format="%.2f"),
            "throughput_cluster_gb_hr": st.column_config.NumberColumn("Cluster GB/hr", min_value=0.0, step=0.1, format="%.2f"),
            "throughput_scanner_gb_hr": st.column_config.NumberColumn("Scanner GB/hr", min_value=0.0, step=0.1, format="%.2f"),
            "throughput_thread_gb_hr": st.column_config.NumberColumn("Thread GB/hr", min_value=0.0, step=0.0001, format="%.4f"),
            "throughput_cluster_files_hr": st.column_config.NumberColumn("Cluster Files/hr", min_value=0.0, step=100.0),
            "throughput_scanner_files_hr": st.column_config.NumberColumn("Scanner Files/hr", min_value=0.0, step=100.0),
            "throughput_thread_files_hr": st.column_config.NumberColumn("Thread Files/hr", min_value=0.0, step=10.0),
            "is_active": st.column_config.CheckboxColumn("Active"),
        }
    )

    submitted = st.button("Save Table Changes", type="primary", width='stretch')
    if submitted:
        try:
            from sqlalchemy import text, bindparam
            original = table_df.set_index('id')
            updated = edited_df.set_index('id')

            def values_equal(a, b):
                if pd.isna(a) and pd.isna(b):
                    return True
                return a == b

            update_cols = [
                'Date', 'Scenario_Type', 'Scan_Type', 'Data_Source_Type', 'Data_Type', 'file_set_type',
                'Total_Files', 'Total_Folders', 'Total_Size_GB', 'New_Attributes', 'avg_file_size',
                'throughput_cluster_gb_hr', 'throughput_scanner_gb_hr', 'throughput_thread_gb_hr',
                'throughput_cluster_files_hr', 'throughput_scanner_files_hr', 'throughput_thread_files_hr',
                'is_active'
            ]
            update_cols = [c for c in update_cols if c in updated.columns]

            def clean_value(val):
                if pd.isna(val):
                    return None
                return val

            with engine.connect() as conn:
                # Delete marked rows
                if "Delete" in updated.columns:
                    to_delete = updated[updated["Delete"] == True].index.tolist()
                    if to_delete:
                        ids_param = bindparam("ids", expanding=True)
                        conn.execute(text("DELETE FROM benchmark_findings WHERE benchmark_id IN :ids").bindparams(ids_param), {"ids": to_delete})
                        conn.execute(text("DELETE FROM benchmarks WHERE id IN :ids").bindparams(ids_param), {"ids": to_delete})
                for row_id, row in updated.iterrows():
                    if row.get("Delete") is True:
                        continue
                    if row_id not in original.index:
                        continue
                    changed = False
                    for col in update_cols:
                        if not values_equal(original.at[row_id, col], row[col]):
                            changed = True
                            break
                    if not changed:
                        continue

                    try:
                        run_date_val = pd.to_datetime(row.get('Date'), errors='coerce', format='mixed')
                    except TypeError:
                        run_date_val = pd.to_datetime(row.get('Date'), errors='coerce')
                    run_date_str = str(run_date_val.date()) if pd.notnull(run_date_val) else None

                    is_auto = "scan_id" in updated.columns or (isinstance(row_id, str) and "-" in str(row_id))
                    
                    if is_auto:
                        upd_stmt = text("""
                            UPDATE auto_scan_benchmarks 
                            SET scenario_type = :scn, scan_type = :st, data_source_type = :dst, data_type = :dtype,
                                file_set_type = :fst, total_files = :tf, total_folders = :tfo, total_size_gb = :tgb,
                                total_classification = :tcl
                            WHERE scan_id = :sid
                        """)
                        conn.execute(upd_stmt, {
                            'scn': clean_value(row.get('Scenario_Type')),
                            'st': clean_value(row.get('Scan_Type')),
                            'dst': clean_value(row.get('Data_Source_Type')),
                            'dtype': clean_value(row.get('Data_Type')),
                            'fst': clean_value(row.get('file_set_type')),
                            'tf': clean_value(row.get('Total_Files')),
                            'tfo': clean_value(row.get('Total_Folders')),
                            'tgb': clean_value(row.get('Total_Size_GB')),
                            'tcl': clean_value(row.get('New_Attributes')),
                            'sid': row_id
                        })
                    else:
                        upd_bench = text("""
                            UPDATE benchmarks 
                            SET run_date = :rd, scenario_type = :scn, scan_type = :st, data_source_type = :dst, data_type = :dtype,
                                file_set_type = :fst, total_files = :tf, total_folders = :tfo, total_size_gb = :tgb,
                                total_catalog_items = :tci, avg_file_size = :avg,
                                throughput_cluster_gb_hr = :tpcg, throughput_scanner_gb_hr = :tpsg, throughput_thread_gb_hr = :tptg,
                                throughput_cluster_files_hr = :tpcf, throughput_scanner_files_hr = :tpsf, throughput_thread_files_hr = :tptf,
                                is_active = :act
                            WHERE id = :bid
                        """)
                        conn.execute(upd_bench, {
                            'rd': run_date_str,
                            'scn': clean_value(row.get('Scenario_Type')),
                            'st': clean_value(row.get('Scan_Type')),
                            'dst': clean_value(row.get('Data_Source_Type')),
                            'dtype': clean_value(row.get('Data_Type')),
                            'fst': clean_value(row.get('file_set_type')),
                            'tf': clean_value(row.get('Total_Files')),
                            'tfo': clean_value(row.get('Total_Folders')),
                            'tgb': clean_value(row.get('Total_Size_GB')),
                            'tci': clean_value(row.get('New_Attributes')),
                            'avg': clean_value(row.get('avg_file_size')),
                            'tpcg': clean_value(row.get('throughput_cluster_gb_hr')),
                            'tpsg': clean_value(row.get('throughput_scanner_gb_hr')),
                            'tptg': clean_value(row.get('throughput_thread_gb_hr')),
                            'tpcf': clean_value(row.get('throughput_cluster_files_hr')),
                            'tpsf': clean_value(row.get('throughput_scanner_files_hr')),
                            'tptf': clean_value(row.get('throughput_thread_files_hr')),
                            'act': 1 if bool(row.get('is_active')) else 0,
                            'bid': row_id
                        })
                conn.commit()

            st.success("Table changes saved.")
            st.cache_data.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Error saving changes: {e}")



@st.cache_data(ttl=300)
def load_scan_progress_history(scan_id):
    if not engine or not scan_id:
        return pd.DataFrame()
    query = text("""
        SELECT
               timestamp AS captured_at_actual,
               captured_at_utc AS captured_at_slot_utc,
               scan_state,
               environment_id,
               completed_parts, failed_parts, total_parts_scanned,
               objects_scanned, objects_failed,
               objects_with_pi,
               total_classifications, newly_added_classifications, false_removed_classifications
        FROM scan_progress
        WHERE scan_id = :sid
        ORDER BY timestamp ASC
    """)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(query, conn, params={"sid": scan_id})
            return df
    except Exception:
        return pd.DataFrame()


def _progress_column_header_two_lines(col_name: str) -> str:
    """Underscores → spaces; split long names across two lines to save horizontal space."""
    raw = str(col_name).replace("_", " ").strip()
    parts = raw.split()
    if len(parts) <= 1:
        return raw
    mid = (len(parts) + 1) // 2
    return " ".join(parts[:mid]) + "\n" + " ".join(parts[mid:])


_RAW_PROGRESS_HEADER_OVERRIDES = {
    "captured_at_utc": "captured at\nutc",
    "scan_state": "scan\nstate",
    "completed_parts": "completed\nparts",
    "total_parts_scanned": "total parts\nscanned",
    "objects_scanned": "objects\nscanned",
    "total_classifications": "total\nclassifications",
    "newly_added_classifications": "newly added\nclassifications",
    "false_removed_classifications": "false removed\nclassifications",
    "avg_scanner_cpu_pct": "avg scanner\nCPU %",
    "avg_scanner_mem_pct": "avg scanner\nmemory %",
    "scanner_pods_n": "scanner\npods (n)",
}


def merge_scanner_averages_into_progress_df(
    df_prog: pd.DataFrame,
    df_scanner_i: pd.DataFrame,
    *,
    tolerance_minutes: int = 20,
) -> pd.DataFrame:
    """
    Add mean CPU % and memory % across scanner pods per snapshot, matched by nearest
    scanner_individual_performance.captured_at_utc to scan_progress.captured_at_slot_utc.
    """
    if df_prog is None or df_prog.empty or df_scanner_i is None or df_scanner_i.empty:
        return df_prog
    if "captured_at_slot_utc" not in df_prog.columns:
        return df_prog
    out = df_prog.copy()
    dfi = df_scanner_i.copy()
    dfi["captured_at_utc"] = pd.to_datetime(dfi["captured_at_utc"], errors="coerce", utc=True)
    dfi = dfi.dropna(subset=["captured_at_utc"])
    if dfi.empty:
        return out
    if "memory_usage_pct" not in dfi.columns:
        pm = pd.to_numeric(dfi.get("physical_memory_mb"), errors="coerce")
        fm = pd.to_numeric(dfi.get("free_memory_mb"), errors="coerce")
        dfi["memory_usage_pct"] = np.where(pm > 0, (pm - fm) / pm * 100.0, np.nan)
    dfi["_cpu"] = pd.to_numeric(dfi.get("cpu_pct"), errors="coerce")
    agg = dfi.groupby("captured_at_utc", as_index=False).agg(
        avg_scanner_cpu_pct=("_cpu", "mean"),
        avg_scanner_mem_pct=("memory_usage_pct", "mean"),
        scanner_pods_n=("scanner_id", "count"),
    )
    if agg.empty:
        return out
    agg = agg.sort_values("captured_at_utc")
    left = out.copy()
    left["_slot"] = pd.to_datetime(left["captured_at_slot_utc"], errors="coerce", utc=True)
    left = left.sort_values("_slot")
    tol = pd.Timedelta(minutes=int(tolerance_minutes))
    try:
        merged = pd.merge_asof(
            left,
            agg,
            left_on="_slot",
            right_on="captured_at_utc",
            direction="nearest",
            tolerance=tol,
        )
    except Exception:
        return out
    drop_cols = [c for c in ("_slot", "captured_at_utc") if c in merged.columns]
    merged = merged.drop(columns=drop_cols, errors="ignore")
    for col in ("avg_scanner_cpu_pct", "avg_scanner_mem_pct"):
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce").round(2)
    if "scanner_pods_n" in merged.columns:
        merged["scanner_pods_n"] = (
            pd.to_numeric(merged["scanner_pods_n"], errors="coerce").fillna(0).astype("Int64")
        )
    return merged


@st.cache_data(ttl=300)
def load_scanner_group_performance(env_id, start_dt, end_dt):
    if not engine or env_id is None:
        return pd.DataFrame()
    query = text("""
        SELECT captured_at_utc, group_name, running_scanners, running_threads,
               max_threads, scanner_threads, avg_scan_speed,
               physical_memory_mb, free_memory_mb, group_scanner_ids_json
        FROM scanner_group_performance
        WHERE environment_id = :eid
          AND captured_at_utc BETWEEN :start AND :end
        ORDER BY captured_at_utc ASC
    """)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(query, conn, params={"eid": env_id, "start": start_dt, "end": end_dt})
            if not df.empty:
                def clean_num(v):
                    if isinstance(v, str):
                        return v.upper().replace("MB", "").replace(" ", "").strip()
                    return v

                df["physical_memory_mb"] = pd.to_numeric(df["physical_memory_mb"].apply(clean_num), errors="coerce").fillna(0)
                df["free_memory_mb"] = pd.to_numeric(df["free_memory_mb"].apply(clean_num), errors="coerce").fillna(0)
                df["used_memory_mb"] = df["physical_memory_mb"] - df["free_memory_mb"]
                for _col in ("max_threads", "scanner_threads"):
                    if _col in df.columns:
                        df[_col] = pd.to_numeric(df[_col], errors="coerce").fillna(0).astype(int)
                if "avg_scan_speed" in df.columns:
                    df["avg_scan_speed"] = pd.to_numeric(df["avg_scan_speed"], errors="coerce").fillna(0.0)
                    # BigID scanner-groups returns bytes/sec (e.g. 24006 ≈ 23.4 KiB/s in product UI)
                    df["avg_scan_speed_kib_s"] = df["avg_scan_speed"] / 1024.0
            return df
    except Exception:
        return pd.DataFrame()


def spm_infer_running_scanner_ids_from_individual(df_i: pd.DataFrame) -> set:
    """
    Fallback when `group_scanner_ids_json` was not stored yet: pods whose **latest** row is
    not failed/inactive and has **running_threads > 0** (typical BigID "busy" running set).
    """
    if df_i is None or df_i.empty or "scanner_id" not in df_i.columns:
        return set()
    try:
        d = df_i.sort_values("captured_at_utc", na_position="last")
        last = d.groupby("scanner_id", as_index=False).last()
        out: set = set()
        for _, r in last.iterrows():
            sid = str(r.get("scanner_id") or "").strip()
            if not sid:
                continue
            if _scanner_status_inactive(r.get("status")):
                continue
            rt = int(pd.to_numeric(r.get("running_threads"), errors="coerce") or 0)
            if rt > 0:
                out.add(sid)
        return out
    except Exception:
        return set()


def spm_effective_running_scanner_id_set(df_g: pd.DataFrame, df_i: pd.DataFrame) -> Tuple[set, str]:
    """
    Prefer scanner-groups `scannersIDs` (matches BigID runningScanners count).
    Else infer from services-status latest rows (threads > 0, non-failed).
    """
    ids = spm_latest_group_scanner_id_set(df_g)
    if ids:
        return ids, "scanner-groups `scannersIDs`"
    ids2 = spm_infer_running_scanner_ids_from_individual(df_i)
    if ids2:
        return ids2, "inferred (latest non-failed + running_threads > 0)"
    return set(), ""


def spm_latest_group_scanner_id_set(df_g: pd.DataFrame) -> set:
    """
    Union of BigID `scannersIDs` from the latest `captured_at_utc` in group rows
    (same list that backs `runningScanners` in the UI).
    """
    if df_g is None or df_g.empty or "group_scanner_ids_json" not in df_g.columns:
        return set()
    try:
        tmax = df_g["captured_at_utc"].max()
        if pd.isna(tmax):
            return set()
        sub = df_g[df_g["captured_at_utc"] == tmax]
        out = set()
        for _, r in sub.iterrows():
            raw = r.get("group_scanner_ids_json")
            if raw is None or (isinstance(raw, float) and pd.isna(raw)) or str(raw).strip() == "":
                continue
            arr = json.loads(str(raw))
            if isinstance(arr, list):
                out.update(str(x).strip() for x in arr if x is not None and str(x).strip())
        return out
    except Exception:
        return set()


def spm_latest_per_scanner_df(df: pd.DataFrame) -> pd.DataFrame:
    """Latest row per scanner_id (for bar charts / at-a-glance metrics)."""
    if df is None or df.empty or "scanner_id" not in df.columns:
        return pd.DataFrame()
    d = df.sort_values("captured_at_utc", na_position="last").copy()
    d = d[d["scanner_id"].astype(str).str.strip().ne("")]
    if d.empty:
        return pd.DataFrame()
    return d.groupby("scanner_id", as_index=False).last()


def spm_dedupe_scanner_group_rows(df_g: pd.DataFrame) -> pd.DataFrame:
    """
    One row per (captured_at_utc, group_name) before aggregating by time only.
    Stops **running_scanners** / **running_threads** from being summed across duplicate
    snapshots (same group + same slot stored more than once → e.g. 5+5+5 = 15).
    """
    if df_g is None or df_g.empty:
        return df_g
    d = df_g.sort_values("captured_at_utc", na_position="last").copy()
    key_cols = ["captured_at_utc"]
    if "group_name" in d.columns:
        key_cols.append("group_name")
    return d.drop_duplicates(subset=key_cols, keep="last")


def spm_apply_plotly_ticks(fig_obj, x_vals):
    """
    Avoid dense dtick labels across multi-hour/day gaps (Scan Progress + Scanner infrastructure).
    """
    try:
        xs = [x for x in list(x_vals) if x is not None and str(x) != "NaT"]
    except Exception:
        xs = []
    if not xs:
        return
    try:
        xs = [pd.to_datetime(x) for x in xs]
    except Exception:
        pass
    try:
        xs = sorted(xs)
    except Exception:
        pass
    max_ticks = 6
    if len(xs) > max_ticks:
        try:
            idx = np.linspace(0, len(xs) - 1, num=max_ticks)
            idx = sorted({int(round(i)) for i in idx})
            xs = [xs[i] for i in idx]
        except Exception:
            xs = [xs[0], xs[-1]]
    try:
        x_min = min(xs)
        x_max = max(xs)
        show_date = getattr(x_min, "date", None) and getattr(x_max, "date", None) and (x_min.date() != x_max.date())
    except Exception:
        show_date = False
    try:
        if show_date:
            tick_text = [pd.to_datetime(x).strftime("%m-%d<br>%H:%M") for x in xs]
        else:
            tick_text = [pd.to_datetime(x).strftime("%H:%M") for x in xs]
        tick_vals = [pd.to_datetime(x).strftime("%Y-%m-%d %H:%M:%S") for x in xs]
    except Exception:
        tick_text = [str(x) for x in xs]
        tick_vals = [str(x) for x in xs]
    fig_obj.update_xaxes(
        title_text="UTC time",
        tickmode="array",
        tickvals=tick_vals,
        ticktext=tick_text,
        tickangle=0,
        tickfont=dict(size=10),
        automargin=True,
        ticklabeloverflow="allow",
    )


def _spm_layout_merge(base: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    """Merge Plotly layout dict with overrides (avoids duplicate keyword errors from **base + height=...)."""
    out = dict(base or {})
    out.update({k: v for k, v in overrides.items() if v is not None})
    return out


def _spm_scanner_two_line_label(name: Any) -> str:
    """Underscores → spaces; split into two lines for compact x-axis labels (Plotly accepts <br>)."""
    s = str(name or "").replace("_", " ").strip()
    if not s:
        return "—"
    parts = s.split()
    if len(parts) <= 1:
        return s
    mid = (len(parts) + 1) // 2
    return "<br>".join([" ".join(parts[:mid]), " ".join(parts[mid:])])


@st.cache_data(ttl=300)
def load_scanner_individual_performance(env_id, start_dt, end_dt):
    if not engine or env_id is None:
        return pd.DataFrame()
    query = text("""
        SELECT captured_at_utc, scanner_id, name, status, group_name, running_threads, total_num_of_threads,
               system_load_average, available_processors, cpu_pct, physical_memory_mb, free_memory_mb
        FROM scanner_individual_performance
        WHERE environment_id = :eid
          AND captured_at_utc BETWEEN :start AND :end
        ORDER BY captured_at_utc ASC
    """)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(query, conn, params={"eid": env_id, "start": start_dt, "end": end_dt})
            if not df.empty:
                def get_disp(r):
                    base = str(r["name"])
                    sid = str(r["scanner_id"]) if r["scanner_id"] else ""
                    if sid and f"[{sid[:4]}]" not in base:
                        return f"{base} [{sid[:4]}]"
                    return base

                df["display_name"] = df.apply(get_disp, axis=1)

                def clean_num(v):
                    if isinstance(v, str):
                        return v.upper().replace("MB", "").replace(" ", "").strip()
                    return v
                df["physical_memory_mb"] = pd.to_numeric(df["physical_memory_mb"].apply(clean_num), errors="coerce").fillna(0)
                df["free_memory_mb"] = pd.to_numeric(df["free_memory_mb"].apply(clean_num), errors="coerce").fillna(0)
                df["used_memory_mb"] = df["physical_memory_mb"] - df["free_memory_mb"]
                df["memory_usage_pct"] = df.apply(
                    lambda r: (r["used_memory_mb"] / r["physical_memory_mb"] * 100) if r["physical_memory_mb"] > 0 else 0,
                    axis=1,
                )
                if "cpu_pct" in df.columns:
                    df["cpu_pct"] = pd.to_numeric(df["cpu_pct"], errors="coerce")
                else:
                    df["cpu_pct"] = np.nan
                if "available_processors" in df.columns:
                    df["available_processors"] = pd.to_numeric(df["available_processors"], errors="coerce")
                else:
                    df["available_processors"] = np.nan
                # Older rows: derive CPU % for charts if load + processors exist (same as BigID UI)
                try:
                    _m = (
                        df["cpu_pct"].isna()
                        & df["system_load_average"].notna()
                        & (pd.to_numeric(df["available_processors"], errors="coerce") > 0)
                    )
                    if _m.any():
                        _ap = pd.to_numeric(df.loc[_m, "available_processors"], errors="coerce").clip(lower=1.0)
                        df.loc[_m, "cpu_pct"] = (
                            pd.to_numeric(df.loc[_m, "system_load_average"], errors="coerce") * 100.0 / _ap
                        )
                    # Rows captured before `available_processors` existed: use default logical CPU count (BigID often 7)
                    try:
                        _def_p = float(str(os.getenv("BIGID_SCANNER_DEFAULT_PROCESSORS", "7") or "7").strip())
                    except Exception:
                        _def_p = 7.0
                    if _def_p <= 0:
                        _def_p = 7.0
                    _m2 = df["cpu_pct"].isna() & pd.to_numeric(df["system_load_average"], errors="coerce").notna()
                    if _m2.any():
                        df.loc[_m2, "cpu_pct"] = (
                            pd.to_numeric(df.loc[_m2, "system_load_average"], errors="coerce") * 100.0 / _def_p
                        )
                except Exception:
                    pass
            return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_latest_scan_progress_state_map(scan_ids):
    """
    Returns {scan_id: latest scan_state from scan_progress}.
    This helps keep Scan Status correct when scan_parents lags.
    """
    if not engine:
        return {}
    cleaned = []
    seen = set()
    for sid in (scan_ids or []):
        s = str(sid or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
    if not cleaned:
        return {}
    try:
        from sqlalchemy import bindparam
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT scan_id, COALESCE(captured_at_utc, timestamp) AS captured_at, scan_state
                    FROM scan_progress
                    WHERE scan_id IN :ids
                    ORDER BY COALESCE(captured_at_utc, timestamp) DESC, id DESC
                """).bindparams(bindparam("ids", expanding=True)),
                {"ids": cleaned},
            ).fetchall()
        out = {}
        for r in rows:
            sid = str(r[0] or "").strip()
            if not sid or sid in out:
                continue
            out[sid] = str(r[2] or "").strip()
        return out
    except Exception:
        return {}


@st.cache_data(ttl=60)
def load_latest_scan_progress_rows_for_environments(environment_ids):
    """
    Returns latest scan_progress rows per scan_id for given environment_ids.
    Used to ensure Scan Status cannot miss running scans even if scan_parents lags.
    """
    if not engine:
        return []
    env_ids = []
    seen = set()
    for eid in (environment_ids or []):
        try:
            v = int(eid)
        except Exception:
            continue
        if v in seen:
            continue
        seen.add(v)
        env_ids.append(v)
    if not env_ids:
        return []
    try:
        from sqlalchemy import bindparam
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT sp.scan_id, sp.scan_name, sp.environment_id, sp.scan_state, sp.captured_at_utc, sp.timestamp, sp.id
                    FROM scan_progress sp
                    JOIN (
                        SELECT z.scan_id, MAX(z.id) AS max_id
                        FROM scan_progress z
                        JOIN (
                            SELECT scan_id, MAX(COALESCE(captured_at_utc, timestamp)) AS mx
                            FROM scan_progress
                            WHERE environment_id IN :eids
                              AND scan_id IS NOT NULL AND scan_id <> ''
                            GROUP BY scan_id
                        ) t
                          ON t.scan_id = z.scan_id
                         AND COALESCE(z.captured_at_utc, z.timestamp) = t.mx
                        WHERE z.environment_id IN :eids
                        GROUP BY z.scan_id
                    ) u ON u.max_id = sp.id
                    ORDER BY COALESCE(sp.captured_at_utc, sp.timestamp) DESC, sp.id DESC
                """).bindparams(bindparam("eids", expanding=True)),
                {"eids": env_ids},
            ).fetchall()
        out = []
        for r in rows or []:
            out.append({
                "scan_id": str(r[0] or "").strip(),
                "scan_name": str(r[1] or "").strip(),
                "environment_id": r[2],
                "scan_state": str(r[3] or "").strip(),
                "captured_at_utc": r[4],
                "timestamp": r[5],
                "id": r[6],
            })
        return [x for x in out if x.get("scan_id")]
    except Exception:
        return []

@st.fragment
def page_scan_progress():
    st.title("📈 Scan Progress Details")
    query_params = st.query_params
    scan_id = query_params.get("scan_id")
    if isinstance(scan_id, list): scan_id = scan_id[0]
    
    if not scan_id:
        st.warning("No scan_id provided in URL.")
        if st.button("Go to Scan Status"):
            st.query_params["page"] = "Scan Status"
            st.rerun()
        return

    # DB-only page: show scan id/name once (no duplicates).

    # Ensure table exists even when user navigates here directly.
    ensure_scan_progress_table()

    # Live tracking (no user settings required)
    running_states_local = {"started", "awaiting", "inprogress", "in progress", "running", "queued", "pending"}
    terminal_states_local = {
        "completed", "completedwithfailures", "completed with failures",
        "failed", "stopped", "stop", "aborted", "cancelled", "canceled",
    }

    def _norm_state(s):
        return str(s or "").strip()

    def _compact(s):
        return _norm_state(s).lower().replace(" ", "")

    def _is_terminal_state(s):
        return _compact(s) in {_compact(x) for x in terminal_states_local}

    def _is_running_state(s):
        return _compact(s) in {_compact(x) for x in running_states_local}

    # IMPORTANT: Do NOT write snapshots from this page.
    # Background tracking should be done by `tracker.py` (no browser required).
    auto_track = False
    refresh_seconds = 0
    save_seconds = 0
    
    # IMPORTANT: Do not call BigID APIs from this page. Tracker is the only updater.
    live_payload = None
    live_state = None
    live_scan_name = ""
    live_metrics = None

    # Show scan name beside scan id (DB name as fallback)
    try:
        parent = get_scan_parent_details(str(scan_id)) or {}
        db_name = str(parent.get("name") or "").strip()
        db_started_at = str(parent.get("started_at") or "").strip()
        db_completed_at = str(parent.get("completed_at") or "").strip()
    except Exception:
        db_name = ""
        db_started_at = ""
        db_completed_at = ""
    display_name = db_name
    if display_name:
        st.caption(f"Scan: **{display_name}** | Scan ID: `{scan_id}`")
    else:
        st.caption(f"Scan ID: `{scan_id}`")

    # Manual sync on-demand (event-driven).
    # Uses the same DB schema/slotting logic as the tracker (10-minute captured_at_utc + dedupe).
    is_terminal_now = _is_terminal_state(parent.get("scan_status"))
    manual_sync = st.button("Sync now (manual)", type="primary", disabled=is_terminal_now)
    if is_terminal_now:
        st.caption("Manual sync is disabled because the scan has finished.")
    if manual_sync:
        ensure_scan_tracking_tables()
        sync_env_row = get_environment_row_for_scan_id(scan_id)
        if not sync_env_row:
            # As a fallback, brute-force active envs by trying the insights endpoint.
            try:
                env_df = load_test_environments()
                if not env_df.empty and "is_active" in env_df.columns:
                    env_df = env_df[env_df["is_active"].apply(lambda x: bool(x))]
                env_rows = env_df.to_dict(orient="records") if not env_df.empty else []
            except Exception:
                env_rows = []
            found = None
            for er in env_rows:
                try:
                    fetch_parent_scan_insight_with_auth_retry(er, scan_id)
                    found = er
                    break
                except Exception:
                    continue
            sync_env_row = found

        if not sync_env_row:
            st.error("Cannot resolve environment for this scan_id. Add it to Scan Status (or ensure env is active) and try again.")
        else:
            with st.spinner("Syncing scan snapshot + insights..."):
                try:
                    insight_payload, _ = fetch_parent_scan_insight_with_auth_retry(sync_env_row, scan_id)
                    # Best-effort: update scan_parents row too
                    try:
                        si = insight_payload.get("data", {}).get("scanInsights", {}) if isinstance(insight_payload, dict) else {}
                        if not si:
                            si = insight_payload if isinstance(insight_payload, dict) else {}
                        sp = si.get("scanProfile") if isinstance(si.get("scanProfile"), dict) else {}
                        scan_name = str(sp.get("name") or db_name or "").strip()
                        scan_type = str(sp.get("type") or "").strip()
                        started_at = sp.get("startedAt")
                        updated_at = sp.get("updatedAt")
                        completed_at = sp.get("completedAt")
                        template_name = str(sp.get("scanTemplateName") or "").strip()
                        state = str(si.get("state") or "").strip()
                        scan_row = {
                            "_id": str(scan_id),
                            "name": scan_name,
                            "type": scan_type,
                            "startedAt": started_at or updated_at,
                            "completedAt": completed_at,
                            "scanTemplateName": template_name,
                            "state": state,
                        }
                        upsert_scan_parent_record(sync_env_row.get("id"), scan_row, state)
                        try:
                            st.caption(
                                f"Manual sync used env: `{str(sync_env_row.get('env_name') or sync_env_row.get('id') or '')}` | "
                                f"API state: `{state or 'unknown'}` | updatedAt: `{str(updated_at or '')}` | completedAt: `{str(completed_at or '')}`"
                            )
                        except Exception:
                            pass
                    except Exception:
                        scan_name = db_name

                    # Snapshot row (scan_progress)
                    write_res = upsert_scan_progress_record(
                        sync_env_row.get("id"),
                        str(scan_id),
                        scan_name or db_name or "",
                        insight_payload,
                        align_10min=False,  # manual sync should record current time (not 10-min slot)
                    )
                    if not (isinstance(write_res, dict) and write_res.get("ok")):
                        st.error(f"Snapshot write failed: {str(write_res.get('error') if isinstance(write_res, dict) else write_res)}")
                    else:
                        cau = write_res.get("captured_at_utc")
                        st.caption(f"Wrote snapshot slot: `{str(cau)}`")
                        try:
                            st.caption(f"Wrote scan_state: `{str(write_res.get('scan_state') or '')}`")
                        except Exception:
                            pass

                    # Full normalized scan_insights + attributes delta map (for Add New Benchmark prefill)
                    sync_single_scan_insight(str(scan_id), sync_env_row)

                    try:
                        _cau_scan = write_res.get("captured_at_utc") if isinstance(write_res, dict) else None
                        if _cau_scan is not None and hasattr(_cau_scan, "replace"):
                            _cap_at = _cau_scan.replace(microsecond=0)
                        else:
                            _cap_at = datetime.utcnow().replace(microsecond=0)
                        p_res = capture_scanner_performance_for_env(
                            engine,
                            sync_env_row,
                            captured_at=_cap_at,
                        )
                        if p_res.get("ok"):
                            st.caption(
                                f"Captured scanner performance: `{p_res.get('groups')} groups, {p_res.get('individuals')} scanners`"
                            )
                    except Exception:
                        pass

                    try:
                        # Avoid showing stale cached empty-history after a successful manual write.
                        load_scan_progress_history.clear()
                    except Exception:
                        pass
                    st.success("Synced. Reloading…")
                    st.rerun()
                except Exception as ex:
                    st.error(f"Manual sync failed: {ex}")

    # Live metrics are intentionally not shown here (DB-only).

    # No auto-save here.

    # Load (possibly updated) history after any auto-save.
    df = load_scan_progress_history(scan_id)
    _scanner_i_for_progress_merge = pd.DataFrame()

    def _tracker_slot_minutes() -> int:
        # Prefer process env, fallback to .env in repo root.
        try:
            v = str(os.getenv("TRACKER_SLOT_MINUTES", "") or "").strip()
            if v:
                m = int(v)
                return m if 1 <= m <= 60 else 10
        except Exception:
            pass
        try:
            env_path = Path(__file__).parent / ".env"
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    ss = line.strip()
                    if not ss or ss.startswith('#') or '=' not in ss:
                        continue
                    k, v = ss.split('=', 1)
                    if k.strip() == 'TRACKER_SLOT_MINUTES':
                        mv = int(str(v).strip().strip('"').strip("'"))
                        return mv if 1 <= mv <= 60 else 10
        except Exception:
            pass
        return 10

    slot_m = _tracker_slot_minutes()
    stale_threshold_min = float((slot_m * 2) + 5)

    # No forced auto-refresh here.

    if df.empty:
        st.warning("No saved snapshots yet for this scan.")
        st.caption(f"Waiting for `tracker.py` to write snapshots (every {slot_m} minutes).")

        if st.button("Back to Scan Status"):
            st.query_params["page"] = "Scan Status"
            st.rerun()
        return

    # Tracker health (DB-only): show last snapshot time and staleness.
    try:
        # Use slot time for "are we getting 10-min snapshots?"
        last_ts = pd.to_datetime(df["captured_at_slot_utc"], errors="coerce", utc=True).max()
    except Exception:
        last_ts = None
    try:
        now_utc = pd.Timestamp.utcnow()
    except Exception:
        now_utc = None
    age_min = None
    last_line = ""
    if last_ts is not None and pd.notnull(last_ts) and now_utc is not None:
        age_min = (now_utc - last_ts).total_seconds() / 60.0
        last_line = f"this scan: `{last_ts.strftime('%Y-%m-%d %H:%M UTC')}` (age: {age_min:.1f} min)"

    # Also show latest snapshot across ALL scans (helps confirm if tracker is alive at all).
    try:
        if engine:
            with engine.connect() as conn:
                row = conn.execute(text("""
                    SELECT MAX(COALESCE(captured_at_utc, timestamp))
                    FROM scan_progress
                """)).fetchone()
            global_last = row[0] if row else None
        else:
            global_last = None
    except Exception:
        global_last = None
    g_age = None
    global_line = ""
    if global_last:
        try:
            gl = pd.to_datetime(global_last, errors="coerce", utc=True)
            if pd.notnull(gl) and now_utc is not None:
                g_age = (now_utc - gl).total_seconds() / 60.0
                global_line = f"DB (any scan): `{gl.strftime('%Y-%m-%d %H:%M UTC')}` (age: {g_age:.1f} min)"
        except Exception:
            pass

    if last_line or global_line:
        joined = " | ".join([x for x in [last_line, global_line] if x])
        st.caption(f"Latest snapshots — {joined}")

    # Single consolidated warning (avoid duplicate warnings)
    # Only warn for running scans. For terminal scans, no new snapshots are expected.
    try:
        latest_state_for_scan = ""
        try:
            latest_state_for_scan = str(
                df.sort_values("captured_at_actual", ascending=True).iloc[-1].get("scan_state") if not df.empty else ""
            ).strip()
        except Exception:
            latest_state_for_scan = ""
        if isinstance(age_min, (int, float)) and age_min >= stale_threshold_min and (not _is_terminal_state(latest_state_for_scan)):
            extra = ""
            if isinstance(g_age, (int, float)):
                extra = f" (DB-wide latest age: {g_age:.1f} min)"
            st.warning(
                f"Tracker looks stale for this scan (no new snapshots in {stale_threshold_min:.0f}+ minutes). "
                f"If you expect updates every {slot_m} minutes while the scan is running, the tracker is likely not running or is failing."
                + extra
            )
    except Exception:
        pass

    # Tracker heartbeat (DB-only) - shows whether tracker is running at all (even if no scans were tracked).
    try:
        hb = None
        if engine:
            with engine.connect() as conn:
                row = conn.execute(text("""
                    SELECT last_started_at_utc, last_finished_at_utc, last_status, last_message
                    FROM tracker_heartbeat
                    WHERE name = 'default'
                    LIMIT 1
                """)).fetchone()
            hb = row
        if hb:
            started_at, finished_at, stt, msg = hb
            s = pd.to_datetime(started_at, errors="coerce", utc=True) if started_at else None
            f = pd.to_datetime(finished_at, errors="coerce", utc=True) if finished_at else None
            try:
                if now_utc is not None and f is not None and pd.notnull(f):
                    hb_age = (now_utc - f).total_seconds() / 60.0
                    if (not _is_terminal_state(latest_state_for_scan)) and hb_age >= stale_threshold_min:
                        st.error(
                            f"Tracker appears stuck: last finished cycle was {hb_age:.0f} minutes ago. "
                            f"Expected cadence is ~{slot_m} minutes."
                        )
            except Exception:
                pass
            st.caption(
                "Tracker heartbeat — "
                + (f"status={stt} " if stt else "")
                + (f"started={s.strftime('%Y-%m-%d %H:%M UTC')} " if s is not None and pd.notnull(s) else "")
                + (f"finished={f.strftime('%Y-%m-%d %H:%M UTC')}" if f is not None and pd.notnull(f) else "")
            )
    except Exception:
        pass

    # Progress charts (DB-only)
    try:
        df["captured_at_slot_utc"] = pd.to_datetime(df["captured_at_slot_utc"], errors="coerce", utc=True)
        df["captured_at_actual"] = pd.to_datetime(df["captured_at_actual"], errors="coerce", utc=True)
    except Exception:
        pass

    # ---------- Analysis (stats + narrative) ----------
    try:
        # Normalize numeric columns
        for col in [
            "completed_parts",
            "failed_parts",
            "total_parts_scanned",
            "objects_scanned",
            "objects_failed",
            "objects_with_pi",
            "total_classifications",
            "newly_added_classifications",
            "false_removed_classifications",
        ]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
    except Exception:
        pass

    try:
        scan_name_for_ai = display_name or db_name or ""
        exp = resolve_expected_totals_for_scan(scan_name_for_ai)
        expected_total_files = exp.get("expected_total_files")
        lookup_key = exp.get("lookup_key") or ""
    except Exception:
        expected_total_files = None
        lookup_key = ""
        exp = {}

    try:
        df_ai = df.dropna(subset=["captured_at_actual"]).copy()
        df_ai = df_ai.sort_values("captured_at_actual")
        last = df_ai.iloc[-1] if not df_ai.empty else None
    except Exception:
        df_ai = df.copy()
        last = df_ai.iloc[-1] if not df_ai.empty else None

    def _safe_num(v, default=None):
        try:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return default
            return float(v)
        except Exception:
            return default

    # Core current values
    cur_objects = int(_safe_num(last.get("objects_scanned"), 0)) if last is not None else 0
    cur_parts_done = int(_safe_num(last.get("completed_parts"), 0)) if last is not None else 0
    cur_parts_total = int(_safe_num(last.get("total_parts_scanned"), 0)) if last is not None else 0
    cur_failed_parts = int(_safe_num(last.get("failed_parts"), 0)) if last is not None else 0
    cur_state = str(last.get("scan_state") or "").strip() if last is not None else ""
    cur_objects_failed = int(_safe_num(last.get("objects_failed"), 0)) if last is not None else 0

    pct_parts = None
    if cur_parts_total and cur_parts_total > 0:
        pct_parts = min(100.0, max(0.0, (cur_parts_done / cur_parts_total) * 100.0))

    # Rates (use last 3 deltas when possible)
    files_per_min = None
    parts_per_min = None
    eta_minutes = None
    try:
        if len(df_ai) >= 2:
            tail = df_ai.tail(3)
            t0 = pd.to_datetime(tail.iloc[0]["captured_at_actual"], utc=True)
            t1 = pd.to_datetime(tail.iloc[-1]["captured_at_actual"], utc=True)
            dt_min = max(1e-6, (t1 - t0).total_seconds() / 60.0)
            d_files = _safe_num(tail.iloc[-1].get("objects_scanned"), 0) - _safe_num(tail.iloc[0].get("objects_scanned"), 0)
            d_parts = _safe_num(tail.iloc[-1].get("completed_parts"), 0) - _safe_num(tail.iloc[0].get("completed_parts"), 0)
            files_per_min = d_files / dt_min if d_files is not None else None
            parts_per_min = d_parts / dt_min if d_parts is not None else None
    except Exception:
        pass

    # Health flags
    flags = []
    regression_warnings = []
    stall_minutes = None
    last_change_at = None
    completion_lag_minutes = None
    completion_lag_summary = ""
    false_removed_delta = None

    # Regression check: if ObjectsWithPi / Classifications stay 0, compare vs last previous-build run (DB-only)
    try:
        cur_max_pi = 0
        cur_max_cls = 0
        if isinstance(df_ai, pd.DataFrame) and not df_ai.empty:
            if "objects_with_pi" in df_ai.columns:
                cur_max_pi = int(pd.to_numeric(df_ai["objects_with_pi"], errors="coerce").fillna(0).max() or 0)
            if "total_classifications" in df_ai.columns:
                cur_max_cls = int(pd.to_numeric(df_ai["total_classifications"], errors="coerce").fillna(0).max() or 0)

        def _wf_ctx_for_scan(_sid: str) -> dict:
            if not engine:
                return {}
            try:
                with engine.connect() as conn:
                    row = conn.execute(text('SELECT ds_kind, file_set_type, is_acl, is_ols, is_structured_ff, build_version, environment_id\nFROM workflow_steps\nWHERE scan_id = :sid\nORDER BY updated_at DESC, id DESC\nLIMIT 1'), {"sid": str(_sid or "").strip()}).fetchone()
                if not row:
                    return {}
                return {
                    "ds_kind": str(row[0] or "").strip(),
                    "file_set_type": (str(row[1]).strip() if row[1] is not None else None),
                    "is_acl": int(row[2] or 0),
                    "is_ols": int(row[3] or 0),
                    "is_structured_ff": int(row[4] or 0),
                    "build_version": str(row[5] or "").strip(),
                    "environment_id": (int(row[6]) if row[6] is not None else None),
                }
            except Exception:
                return {}

        def _find_prev_scan(ctx: dict, cur_scan_id: str) -> tuple:
            if not engine:
                return ("", "")
            dk = str(ctx.get("ds_kind") or "").strip()
            if not dk:
                return ("", "")
            fst = ctx.get("file_set_type")
            eid = ctx.get("environment_id")
            params = {
                "cur": str(cur_scan_id or "").strip(),
                "dk": dk,
                "fst": fst,
                "acl": int(ctx.get("is_acl") or 0),
                "ols": int(ctx.get("is_ols") or 0),
                "sff": int(ctx.get("is_structured_ff") or 0),
                "eid": int(eid) if eid is not None else -1,
            }
            try:
                with engine.connect() as conn:
                    row = conn.execute(text("SELECT ws.scan_id, ws.build_version\nFROM workflow_steps ws\nWHERE ws.scan_id IS NOT NULL AND TRIM(ws.scan_id) <> ''\n  AND ws.scan_id <> :cur\n  AND ws.ds_kind = :dk\n  AND ((:fst IS NULL AND ws.file_set_type IS NULL) OR ws.file_set_type = :fst)\n  AND COALESCE(ws.is_acl, 0) = :acl\n  AND COALESCE(ws.is_ols, 0) = :ols\n  AND COALESCE(ws.is_structured_ff, 0) = :sff\nORDER BY (ws.environment_id = :eid) DESC, ws.updated_at DESC, ws.id DESC\nLIMIT 1"), params).fetchone()
                if not row:
                    return ("", "")
                return (str(row[0] or "").strip(), str(row[1] or "").strip())
            except Exception:
                return ("", "")

        # Only warn if scanning has started (objects > 0) and classification metrics remain 0
        if cur_objects > 0 and cur_max_pi == 0 and cur_max_cls == 0:
            ctx = _wf_ctx_for_scan(str(scan_id))
            prev_sid, prev_build = _find_prev_scan(ctx or {}, str(scan_id))
            if prev_sid:
                prev_df = load_scan_progress_history(prev_sid)
                if isinstance(prev_df, pd.DataFrame) and not prev_df.empty:
                    prev_max_pi = int(pd.to_numeric(prev_df.get("objects_with_pi", 0), errors="coerce").fillna(0).max() or 0)
                    prev_max_cls = int(pd.to_numeric(prev_df.get("total_classifications", 0), errors="coerce").fillna(0).max() or 0)
                else:
                    prev_max_pi = 0
                    prev_max_cls = 0
                if prev_max_pi > 0 or prev_max_cls > 0:
                    regression_warnings.append(
                        f"ObjectsWithPi/Classifications are 0 so far, but previous run (build `{prev_build or 'unknown'}`) had objects_with_pi={prev_max_pi:,}, total_classifications={prev_max_cls:,}."
                    )
                    flags.append("Classification metrics missing vs previous build (possible regression / classification disabled / insights gap).")
            else:
                regression_warnings.append(
                    "ObjectsWithPi/Classifications are 0 so far. No previous workflow run found to compare; this can indicate classification disabled or insights payload missing."
                )
                flags.append("Classification metrics are 0 so far (check classification enablement + insights).")
    except Exception:
        pass

    # total_classifications discrepancy vs previous scan for same scenario
    cls_vs_prev: dict = {}  # keys: cur, prev, delta, pct, prev_build, prev_sid
    cls_new_removed_vs_prev: dict = {}  # keys: cur_new, prev_new, d_new, cur_removed, prev_removed, d_removed
    try:
        if engine and cur_max_cls > 0:
            ctx = _wf_ctx_for_scan(str(scan_id))
            prev_sid_cls, prev_build_cls = _find_prev_scan(ctx or {}, str(scan_id))
            if prev_sid_cls:
                try:
                    with engine.connect() as _conn_nr:
                        cur_nr_row = _conn_nr.execute(
                            text(
                                "SELECT "
                                "MAX(CAST(COALESCE(newly_added_classifications, 0) AS SIGNED)) AS cur_new, "
                                "MAX(CAST(COALESCE(false_removed_classifications, 0) AS SIGNED)) AS cur_removed "
                                "FROM scan_progress WHERE scan_id = :sid"
                            ),
                            {"sid": str(scan_id)},
                        ).fetchone()
                        prev_nr_row = _conn_nr.execute(
                            text(
                                "SELECT "
                                "MAX(CAST(COALESCE(newly_added_classifications, 0) AS SIGNED)) AS prev_new, "
                                "MAX(CAST(COALESCE(false_removed_classifications, 0) AS SIGNED)) AS prev_removed "
                                "FROM scan_progress WHERE scan_id = :sid"
                            ),
                            {"sid": str(prev_sid_cls)},
                        ).fetchone()
                    cur_new = int(cur_nr_row[0] or 0) if cur_nr_row else 0
                    cur_removed = int(cur_nr_row[1] or 0) if cur_nr_row else 0
                    prev_new = int(prev_nr_row[0] or 0) if prev_nr_row else 0
                    prev_removed = int(prev_nr_row[1] or 0) if prev_nr_row else 0
                    cls_new_removed_vs_prev = {
                        "cur_new": cur_new,
                        "prev_new": prev_new,
                        "d_new": cur_new - prev_new,
                        "cur_removed": cur_removed,
                        "prev_removed": prev_removed,
                        "d_removed": cur_removed - prev_removed,
                        "prev_build": prev_build_cls or "unknown",
                        "prev_sid": prev_sid_cls,
                    }
                except Exception:
                    cls_new_removed_vs_prev = {}

                try:
                    with engine.connect() as _conn:
                        prev_cls_row = _conn.execute(
                            text(
                                "SELECT MAX(CAST(COALESCE(total_classifications, 0) AS SIGNED)) "
                                "FROM scan_progress WHERE scan_id = :sid"
                            ),
                            {"sid": prev_sid_cls},
                        ).fetchone()
                    prev_cls_val = int(prev_cls_row[0] or 0) if prev_cls_row and prev_cls_row[0] is not None else 0
                except Exception:
                    prev_cls_val = 0
                if prev_cls_val > 0:
                    delta_cls = cur_max_cls - prev_cls_val
                    pct_cls = (abs(delta_cls) / prev_cls_val * 100.0) if prev_cls_val else 0.0
                    # Flag when difference > 5% OR > 1000 absolute
                    if pct_cls > 5.0 or abs(delta_cls) > 1000:
                        cls_vs_prev = {
                            "cur": cur_max_cls,
                            "prev": prev_cls_val,
                            "delta": delta_cls,
                            "pct": pct_cls,
                            "prev_build": prev_build_cls or "unknown",
                            "prev_sid": prev_sid_cls,
                            "missing": [],
                            "new": [],
                            "changed": [],
                            "top_delta_rows": []
                        }
                        
                        # Fetch and compare individual classifiers if available
                        try:
                            import json
                            def _get_cls_dict(sid: str) -> dict:
                                with engine.connect() as _c:
                                    r = _c.execute(
                                        text("SELECT attributes_payload FROM scan_insights WHERE scan_id = :sid LIMIT 1"),
                                        {"sid": sid}
                                    ).fetchone()
                                if not r or not r[0]:
                                    return {}
                                raw = r[0]
                                if isinstance(raw, dict):
                                    data = raw
                                else:
                                    data = json.loads(str(raw))
                                # Parse all known payload shapes (attributes, dataSourcesInsights, etc.)
                                attrs, _total_hint = extract_scan_attributes_payload(data if isinstance(data, dict) else {})
                                res = {}
                                for a in attrs:
                                    if not isinstance(a, dict):
                                        continue
                                    raw_name = a.get("_id") or a.get("id") or a.get("name")
                                    if raw_name is None:
                                        continue
                                    c_name = str(raw_name).replace("classifier.", "").strip()
                                    if not c_name:
                                        continue
                                    try:
                                        c_cnt = int(a.get("new", 0) or 0)
                                    except Exception:
                                        c_cnt = 0
                                    if c_cnt > 0:
                                        res[c_name] = c_cnt
                                return res

                            cur_dict = _get_cls_dict(str(scan_id))
                            prev_dict = _get_cls_dict(prev_sid_cls)

                            if cur_dict or prev_dict:
                                missing = []
                                new_cls = []
                                changed = []
                                delta_rows = []
                                all_keys = set(cur_dict.keys()).union(set(prev_dict.keys()))
                                for k in sorted(all_keys):
                                    c_val = int(cur_dict.get(k, 0) or 0)
                                    p_val = int(prev_dict.get(k, 0) or 0)
                                    if p_val > 0 and c_val == 0:
                                        missing.append(k)
                                    elif c_val > 0 and p_val == 0:
                                        new_cls.append(k)
                                    if p_val > 0 or c_val > 0:
                                        diff_signed = c_val - p_val
                                        diff_abs = abs(diff_signed)
                                        if diff_abs > 0:
                                            delta_rows.append((k, p_val, c_val, diff_signed, diff_abs))
                                    if p_val > 0 and c_val > 0:
                                        diff = abs(c_val - p_val)
                                        pct = (diff / p_val) * 100.0
                                        if pct > 10.0 or diff > 100:
                                            changed.append(f"{k} ({p_val:,} -> {c_val:,})")

                                delta_rows = sorted(delta_rows, key=lambda x: x[4], reverse=True)
                                top_delta_rows = [
                                    f"{nm} ({pv:,} -> {cv:,}, {sgn}{dv:,})"
                                    for (nm, pv, cv, dv, _abs) in delta_rows[:5]
                                    for sgn in (["+"] if dv > 0 else [""])
                                ]

                                cls_vs_prev["missing"] = missing
                                cls_vs_prev["new"] = new_cls
                                cls_vs_prev["changed"] = changed
                                cls_vs_prev["top_delta_rows"] = top_delta_rows
                        except Exception:
                            pass

    except Exception:
        cls_vs_prev = {}

    # Classifier / attribute rows from scan_insights (one load for Observations + Analysis expander)
    classifier_hits_df = pd.DataFrame()
    classifier_hits_note = ""
    try:
        classifier_hits_df, classifier_hits_note = load_scan_insight_classifier_hits_df(str(scan_id))
    except Exception:
        pass

    # Classifier names from stored parent-scan insight JSON (available without attributes API sync)
    insight_payload_classifier_names: List[str] = []
    try:
        if engine:
            with engine.connect() as conn:
                r_ip = conn.execute(
                    text("SELECT insight_payload FROM scan_insights WHERE scan_id = :sid LIMIT 1"),
                    {"sid": str(scan_id).strip()},
                ).fetchone()
            if r_ip and r_ip[0]:
                raw_ip = r_ip[0]
                if isinstance(raw_ip, str):
                    ip_obj = json.loads(raw_ip)
                elif isinstance(raw_ip, dict):
                    ip_obj = raw_ip
                else:
                    ip_obj = None
                if isinstance(ip_obj, dict):
                    mx = extract_scan_insight_metrics(ip_obj)
                    tnames = str(mx.get("classifier_attribute_names") or "").strip()
                    if tnames:
                        insight_payload_classifier_names = sorted(
                            {x.strip() for x in tnames.split(",") if x.strip()}
                        )
    except Exception:
        pass

    # Observations (rule-based): highlight inconsistencies like "Completed but objects < expected".
    observations = []
    try:
        def _cstate(x: str) -> str:
            return str(x or '').strip().lower().replace(' ', '')

        exp_files = int(expected_total_files) if expected_total_files is not None and str(expected_total_files).strip() != '' else None
        short_by = (int(exp_files) - int(cur_objects)) if (exp_files is not None and cur_objects is not None and int(cur_objects) < int(exp_files)) else 0

        # Duration / timestamps from scan_parents when available
        try:
            is_run = cur_state and _is_running_state(cur_state)
            # If in-progress, ignore the database completed_at which might be premature
            eff_completed_at = None if is_run else db_completed_at
            dur = format_scan_duration(db_started_at, eff_completed_at, is_running=bool(is_run))
            if db_started_at:
                observations.append(f"Start: `{str(db_started_at)}`")
            if eff_completed_at:
                observations.append(f"End: `{str(eff_completed_at)}`")
            if dur:
                obs_label = "Elapsed (so far)" if is_run else "Duration"
                observations.append(f"{obs_label}: **{dur}** (Scan time {'so far' if is_run else 'total'})")
        except Exception:
            pass

        # Expected totals compare
        if exp_files is not None and exp_files > 0:
            lk = str(lookup_key or '').strip()
            observations.append(
                f"Objects scanned: **{int(cur_objects):,} / {int(exp_files):,}**"
                + (f" (benchmark expected from `{lk}` — **not** scan status)" if lk else " (benchmark expected — **not** scan status)")
            )
            if short_by > 0:
                observations.append(f"Object shortfall: **{short_by:,}**")

        # Pull scan_insights summary when available
        try:
            ins = load_scan_insight_from_db(str(scan_id))
        except Exception:
            ins = None
        if isinstance(ins, dict) and ins:
            try:
                dca = ins.get('distinct_classification_attributes')
                if dca not in (None, '', '0'):
                    observations.append(
                        f"Distinct classification **attributes** (summary count): **{int(dca):,}** "
                        "(BigID insight counter — **named list** is in this column when available, "
                        "or in **Classifier / attribute hits** just **below**.)"
                    )
            except Exception:
                pass
            try:
                can = str(ins.get("classifier_attribute_names") or "").strip()
                if can:
                    parts = sorted({x.strip() for x in can.split(",") if x.strip()})
                    if parts:
                        observations.append(
                            "**Names from classifier-attribute delta map** (DB): "
                            + ", ".join(parts[:40])
                            + (f" … (+{len(parts) - 40} more)" if len(parts) > 40 else "")
                        )
                elif insight_payload_classifier_names:
                    observations.append(
                        "**Classifier / attribute names** (from stored `scan_insights.insight_payload`): "
                        + ", ".join(insight_payload_classifier_names[:60])
                        + (
                            f" … (+{len(insight_payload_classifier_names) - 60} more)"
                            if len(insight_payload_classifier_names) > 60
                            else ""
                        )
                    )
            except Exception:
                pass
            try:
                tcl = ins.get("total_classification")
                if tcl not in (None, "", "0"):
                    observations.append(
                        f"**Total classifications** (insight aggregate counter): **{int(tcl):,}** "
                        "(BigID sum-style metric — per-row **New findings** counts are in **Classifier / attribute hits** just below; run **Sync now** if that table is empty.)"
                    )
            except Exception:
                pass
            try:
                nac = ins.get('new_attributes_count')
                rac = ins.get('removed_attributes_count')
                if nac not in (None, ''):
                    observations.append(f"Attributes added (delta sum): **{int(nac):,}**")
                if rac not in (None, ''):
                    observations.append(f"Attributes removed (delta sum): **{int(rac):,}**")
            except Exception:
                pass

        # Classification counters vs throughput (same scan timeline)
        try:
            if len(df_ai) >= 2 and "total_classifications" in df_ai.columns:
                tc0 = _safe_num(df_ai.iloc[0].get("total_classifications"), None)
                tc1 = _safe_num(df_ai.iloc[-1].get("total_classifications"), None)
                if tc0 is not None and tc1 is not None:
                    d_tc = int(tc1 - tc0)
                    if d_tc != 0:
                        fpm_note = ""
                        if files_per_min is not None and files_per_min > 0:
                            fpm_note = (
                                f" Recent object throughput ≈ **{files_per_min:.1f}**/min (last snapshots). "
                                "If classifications jump while throughput drops (or the reverse), check **Classifier Changes** "
                                "for this build and **Classifier / attribute hits** below."
                            )
                        observations.append(
                            f"**Total classifications** changed by **{d_tc:+,}** from first to latest snapshot.{fpm_note}"
                        )
            if len(df_ai) >= 2 and "newly_added_classifications" in df_ai.columns:
                na0 = _safe_num(df_ai.iloc[0].get("newly_added_classifications"), None)
                na1 = _safe_num(df_ai.iloc[-1].get("newly_added_classifications"), None)
                if na0 is not None and na1 is not None:
                    d_na = int(na1 - na0)
                    if abs(d_na) >= 50:
                        observations.append(
                            f"**Newly added classifications** moved by **{d_na:+,}** across snapshots — "
                            "often tracks new findings rate; compare with env classifier list/metadata if the scan profile changed."
                        )
        except Exception:
            pass

        # Inconsistency rules
        stc = _cstate(cur_state)
        terminal = stc in {'completed','completedwithfailures','failed','stopped','aborted','cancelled','canceled'}
        comp = stc in {'completed'}
        comp_fail = stc in {'completedwithfailures'}

        if terminal and short_by > 0 and int(cur_failed_parts) == 0 and int(cur_objects_failed) == 0:
            observations.append(
                "Inconsistency: scan is terminal but objects are below expected and no failed parts/objects are reported."
            )

        if comp_fail and int(cur_failed_parts) == 0 and int(cur_objects_failed) == 0:
            observations.append(
                "Note: `CompletedWithFailures` but failed parts/objects are 0. BigID often shows this when failures were rescanned/cleared."
            )

        if comp and short_by > 0:
            observations.append(
                "Note: `Completed` despite object shortfall. This can be caused by filtering/sampling/enumeration changes, or missing insight signals."
            )

        # Parts at 100% but objects short
        try:
            if cur_parts_total and cur_parts_done >= cur_parts_total and short_by > 0:
                observations.append(
                    "Parts show 100% complete but object count is short vs expected. Treat completion based on API state, not percent." 
                )
        except Exception:
            pass

    except Exception:
        observations = observations or []

    # Which classifiers/attributes actually have new findings (from attributes API snapshot)
    try:
        if classifier_hits_df is not None and not classifier_hits_df.empty:
            col_new = "New findings"
            col_lbl = "Classifier / attribute"
            if col_new in classifier_hits_df.columns and col_lbl in classifier_hits_df.columns:
                matched = (
                    classifier_hits_df[pd.to_numeric(classifier_hits_df[col_new], errors="coerce").fillna(0) > 0][
                        col_lbl
                    ]
                    .astype(str)
                    .str.strip()
                )
                matched = sorted({x for x in matched if x})
                if matched:
                    observations.append(
                        f"**Classifiers / attributes with new findings** ({len(matched)}): "
                        + ", ".join(matched[:50])
                        + (f" … (+{len(matched) - 50} more)" if len(matched) > 50 else "")
                    )
                elif insight_payload_classifier_names and not any(
                    "insight_payload" in str(x) or "delta map" in str(x) for x in observations
                ):
                    observations.append(
                        "**Classifier / attribute names** (insight JSON; **New findings** per row not loaded — click **Sync now** for the attributes table): "
                        + ", ".join(insight_payload_classifier_names[:50])
                        + (
                            f" … (+{len(insight_payload_classifier_names) - 50} more)"
                            if len(insight_payload_classifier_names) > 50
                            else ""
                        )
                    )
    except Exception:
        pass

    try:
        if len(df_ai) >= 3:
            a = df_ai.iloc[-3]
            b = df_ai.iloc[-1]
            if int(_safe_num(a.get("objects_scanned"), 0)) == int(_safe_num(b.get("objects_scanned"), 0)) and int(_safe_num(a.get("completed_parts"), 0)) == int(_safe_num(b.get("completed_parts"), 0)):
                flags.append("No progress change across last snapshots (possible stall).")
            # falseRemoved delta over last window
            try:
                fr0 = _safe_num(a.get("false_removed_classifications"), 0) or 0
                fr1 = _safe_num(b.get("false_removed_classifications"), 0) or 0
                false_removed_delta = int(fr1 - fr0)
                if false_removed_delta > 0:
                    flags.append(f"falseRemoved classifications increased by {false_removed_delta}.")
            except Exception:
                pass
        # Find last time objects_scanned changed (for stall age)
        if len(df_ai) >= 2 and "objects_scanned" in df_ai.columns:
            prev = None
            for i in range(len(df_ai) - 1, -1, -1):
                v = int(_safe_num(df_ai.iloc[i].get("objects_scanned"), 0) or 0)
                if prev is None:
                    prev = v
                    continue
                if v != prev:
                    last_change_at = df_ai.iloc[i + 1].get("captured_at_actual")
                    break
            if last_change_at is None:
                last_change_at = df_ai.iloc[0].get("captured_at_actual")
            try:
                now_utc = pd.Timestamp.utcnow()
                lc = pd.to_datetime(last_change_at, utc=True)
                stall_minutes = max(0.0, (now_utc - lc).total_seconds() / 60.0) if pd.notnull(lc) else None
            except Exception:
                stall_minutes = None
        if cur_failed_parts > 0:
            flags.append(f"Failed parts present: {cur_failed_parts}.")
    except Exception:
        pass

    # Completion lag analysis:
    # If we reached 100% scanned objects / parts but the scan stayed InProgress for a long time,
    # highlight the lag between "work done" and "state flipped".
    try:
        def _compact_state_local(s):
            return str(s or "").strip().lower().replace(" ", "")

        terminal_states_local = {
            "completed", "completedwithfailures", "failed", "stopped", "aborted", "cancelled", "canceled"
        }

        if not df_ai.empty:
            df_lag = df_ai.copy()
            df_lag["t"] = pd.to_datetime(df_lag["captured_at_actual"], errors="coerce", utc=True)
            df_lag["state_c"] = df_lag["scan_state"].apply(_compact_state_local) if "scan_state" in df_lag.columns else ""

            # Pre-compute numeric columns used across multiple checks
            if "objects_scanned" in df_lag.columns:
                df_lag["objects_scanned_num"] = pd.to_numeric(df_lag["objects_scanned"], errors="coerce")
            if "completed_parts" in df_lag.columns:
                df_lag["completed_parts_num"] = pd.to_numeric(df_lag["completed_parts"], errors="coerce")
            if "total_parts_scanned" in df_lag.columns:
                df_lag["total_parts_num"] = pd.to_numeric(df_lag["total_parts_scanned"], errors="coerce")

            # First time we reached expected totals (objects)
            t_reach_files = None
            if expected_total_files and expected_total_files > 0 and "objects_scanned_num" in df_lag.columns:
                reach_rows = df_lag[(df_lag["objects_scanned_num"] >= float(expected_total_files)) & df_lag["t"].notna()]
                if not reach_rows.empty:
                    t_reach_files = reach_rows.iloc[0]["t"]

            # First time parts appear complete (completed_parts >= total_parts)
            t_reach_parts = None
            if "completed_parts_num" in df_lag.columns and "total_parts_num" in df_lag.columns:
                reach_p = df_lag[(df_lag["total_parts_num"] > 0) & (df_lag["completed_parts_num"] >= df_lag["total_parts_num"]) & df_lag["t"].notna()]
                if not reach_p.empty:
                    t_reach_parts = reach_p.iloc[0]["t"]

            # Plateau fallback: for scans that end with failures (CompletedWithFailures),
            # objects/parts may never reach 100%.  Detect the FIRST snapshot where both
            # objects_scanned AND completed_parts stop changing vs the previous snapshot.
            # That sustained plateau is the point at which all scanning work was effectively done.
            t_reach_plateau = None
            try:
                if len(df_lag) >= 2:
                    prev_obj = None
                    prev_parts = None
                    for i, row in df_lag.iterrows():
                        obj_v = row.get("objects_scanned_num") if "objects_scanned_num" in df_lag.columns else None
                        parts_v = row.get("completed_parts_num") if "completed_parts_num" in df_lag.columns else None
                        t_v = row.get("t")
                        if prev_obj is not None and pd.notnull(t_v):
                            obj_same = (obj_v == prev_obj) if (obj_v is not None and pd.notnull(obj_v)) else True
                            parts_same = (parts_v == prev_parts) if (parts_v is not None and pd.notnull(parts_v)) else True
                            if obj_same and parts_same and prev_obj > 0:
                                # Use the time of the PREVIOUS snapshot (first time it flatlined)
                                t_reach_plateau = df_lag.loc[df_lag["t"] < t_v, "t"].max() if (df_lag["t"] < t_v).any() else t_v
                                break
                        prev_obj = obj_v if (obj_v is not None and pd.notnull(obj_v)) else prev_obj
                        prev_parts = parts_v if (parts_v is not None and pd.notnull(parts_v)) else prev_parts
            except Exception:
                t_reach_plateau = None

            # First terminal state time
            t_terminal = None
            term_rows = df_lag[(df_lag["state_c"].isin(terminal_states_local)) & df_lag["t"].notna()]
            if not term_rows.empty:
                t_terminal = term_rows.iloc[0]["t"]

            # Combine reach candidates: prefer exact 100% over plateau
            # Use plateau only when neither 100%-objects nor 100%-parts were achieved.
            reach_candidates = [x for x in [t_reach_files, t_reach_parts] if x is not None and pd.notnull(x)]
            if not reach_candidates and t_reach_plateau is not None and pd.notnull(t_reach_plateau):
                reach_candidates = [t_reach_plateau]
            t_reach = max(reach_candidates) if reach_candidates else None

            if t_reach is not None and t_terminal is not None and pd.notnull(t_terminal) and t_terminal > t_reach:
                completion_lag_minutes = (t_terminal - t_reach).total_seconds() / 60.0

                # What changed during lag?
                lag_slice = df_lag[(df_lag["t"] >= t_reach) & (df_lag["t"] <= t_terminal)].copy()
                obs = []
                try:
                    if "objects_scanned_num" in lag_slice.columns and lag_slice["objects_scanned_num"].notna().any():
                        d = float(lag_slice["objects_scanned_num"].max() - lag_slice["objects_scanned_num"].min())
                        if d == 0:
                            obs.append("objects scanned stayed flat")
                        else:
                            obs.append(f"objects scanned still changed by {int(d):,}")
                except Exception:
                    pass
                try:
                    if "state_c" in lag_slice.columns and lag_slice["state_c"].nunique(dropna=True) <= 1:
                        obs.append("state stayed InProgress")
                except Exception:
                    pass

                # Render summary (UTC)
                try:
                    reach_s = pd.to_datetime(t_reach).strftime("%Y-%m-%d %H:%M UTC")
                    term_s = pd.to_datetime(t_terminal).strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    reach_s = str(t_reach)
                    term_s = str(t_terminal)

                if completion_lag_minutes >= 60:
                    lag_s = f"{completion_lag_minutes/60.0:.1f} hours"
                else:
                    lag_s = f"{completion_lag_minutes:.0f} minutes"

                completion_lag_summary = (
                    f"Reached 100% scanning at `{reach_s}` but did not become terminal until `{term_s}` "
                    f"(lag: **{lag_s}**)"
                    + (f"; during lag: {', '.join(obs)}." if obs else ".")
                )
    except Exception:
        completion_lag_minutes = None
        completion_lag_summary = ""

    # Narrative (LLM-style, but local/deterministic)
    try:
        bits = []
        bits.append(f"Current objects scanned: **{cur_objects:,}**.")
        if pct_parts is not None:
            bits.append(f"Parts completed: **{cur_parts_done:,} / {cur_parts_total:,}** (**{pct_parts:.1f}%**).")
        if files_per_min is not None and files_per_min >= 0:
            bits.append(f"Recent throughput: **{files_per_min:,.0f} objects/min**.")
        if cur_state:
            bits.append(f"Latest state: `{cur_state}`.")
        if completion_lag_summary:
            bits.append(f"Completion lag: {completion_lag_summary}.")
        if flags:
            bits.append("Flags: " + "; ".join([f"`{x}`" for x in flags]))
        narrative = " ".join(bits)
    except Exception:
        narrative = ""

    # Markdown report payload for download inside Analysis expander (no separate section / no CSV).
    _md_rep: Optional[str] = None
    _fn_base = ""
    _fn_safe_key = re.sub(r"[^0-9a-zA-Z_-]+", "_", str(scan_id).strip())[:48]
    try:
        wf_ctx_report = load_workflow_context_for_scan_id(str(scan_id))
        _rep_obs: List[str] = []
        for _o in observations or []:
            _s = str(_o).strip()
            if not _s:
                continue
            if _s.startswith("Objects scanned:") or _s.startswith("Object shortfall:"):
                continue
            _rep_obs.append(_s)
        _rep_obs = _rep_obs[:45]
        _md_rep, _ = build_scan_progress_manager_reports(
            scan_id=str(scan_id),
            display_name=str(display_name or db_name or ""),
            db_started_at=str(db_started_at or ""),
            db_completed_at=str(db_completed_at or ""),
            cur_state=str(cur_state or ""),
            cur_objects=int(cur_objects or 0),
            cur_parts_done=int(cur_parts_done or 0),
            cur_parts_total=int(cur_parts_total or 0),
            cur_failed_parts=int(cur_failed_parts or 0),
            cur_objects_failed=int(cur_objects_failed or 0),
            pct_parts=pct_parts,
            files_per_min=files_per_min,
            expected_total_files=expected_total_files,
            lookup_key=str(lookup_key or ""),
            wf_ctx=wf_ctx_report,
            cls_vs_prev=cls_vs_prev or {},
            cls_new_removed_vs_prev=cls_new_removed_vs_prev or {},
            classifier_hits_df=classifier_hits_df,
            classifier_hits_note=str(classifier_hits_note or ""),
            narrative=str(narrative or ""),
            flags=list(flags or []),
            completion_lag_summary=str(completion_lag_summary or ""),
            regression_warnings=list(regression_warnings or []),
            observations=_rep_obs,
        )
        _fn_ts = datetime.utcnow().strftime("%Y%m%d_%H%MUTC")
        _fn_base = f"scan_manager_report_{_fn_safe_key}_{_fn_ts}"
    except Exception:
        _md_rep = None
        _fn_base = ""

    # Always-visible object progress summary (outside expander).
    try:
        if expected_total_files is not None and str(expected_total_files).strip() != "":
            exp_files_now = int(expected_total_files)
            short_now = max(0, int(exp_files_now) - int(cur_objects or 0))
            lk_now = str(lookup_key or "").strip()
            st.markdown("### Objects")
            st.caption("Compared to **benchmark expected** counts only — does **not** determine In Progress vs Completed (use **Latest state** / API).")
            st.markdown(
                f"- Objects scanned: **{int(cur_objects):,} / {int(exp_files_now):,}**"
                + (f" (benchmark `{lk_now}`)" if lk_now else "")
            )
            if short_now > 0:
                st.markdown(f"- Object shortfall: **{int(short_now):,}**")
    except Exception:
        pass

    with st.expander("🧠 Analysis (stats + narrative)", expanded=True):
        m1, m2, m3, m4, m5, m_dl = st.columns([1, 1, 1, 1, 1, 1.05])
        m1.metric("Objects scanned", f"{cur_objects:,}")
        m2.metric("Parts progress", f"{(pct_parts or 0):.1f}%" if pct_parts is not None else "N/A")
        m3.metric("Failed parts", f"{cur_failed_parts:,}")
        if false_removed_delta is not None:
            m4.metric("falseRemoved (Δ)", f"{false_removed_delta:+,}")
        else:
            m4.metric("falseRemoved (Δ)", "N/A")
        # Total scan time (so far)
        try:
            is_run = cur_state and _is_running_state(cur_state)
            eff_completed_at = None if is_run else db_completed_at
            runtime_str = "N/A"
            
            s_dt = pd.to_datetime(db_started_at, errors='coerce', utc=True) if db_started_at else None
            # Fallback for s_dt
            if s_dt is None or pd.isna(s_dt):
                if isinstance(df_ai, pd.DataFrame) and 'captured_at_actual' in df_ai.columns:
                    s_dt = pd.to_datetime(df_ai['captured_at_actual'], errors='coerce', utc=True).min()

            e_dt = pd.to_datetime(eff_completed_at, errors='coerce', utc=True) if eff_completed_at else None
            if e_dt is None or pd.isna(e_dt):
                e_dt = pd.Timestamp.utcnow()

            if s_dt is not None and pd.notnull(s_dt) and e_dt is not None and pd.notnull(e_dt):
                diff_sec = (e_dt - s_dt).total_seconds()
                mins_tot = max(0.0, diff_sec / 60.0)
                h = int(mins_tot // 60)
                m = int(mins_tot % 60)
                runtime_str = (f"{h}h {m}m" if h > 0 else f"{m}m")
        except Exception:
            runtime_str = 'N/A'
        m5.metric("Scan time (so far)", runtime_str)
        with m_dl:
            st.markdown('<div style="height:1.6rem"></div>', unsafe_allow_html=True)
            if _md_rep is not None and _fn_base:
                st.download_button(
                    "Download Report (md)",
                    data=_md_rep.encode("utf-8"),
                    file_name=_fn_base + ".md",
                    mime="text/markdown",
                    key=f"dl_md_{_fn_safe_key}",
                    use_container_width=True,
                )

        st.divider()
        # Observations first so the three-column summary sits directly above Classifier / attribute hits (table + names).
        try:
            if observations:
                st.markdown("**Observations**")
                time_lines: List[str] = []
                obj_lines: List[str] = []
                cls_lines: List[str] = []
                other_lines: List[str] = []
                for o in observations:
                    s = str(o).strip()
                    if not s:
                        continue
                    sl = s.lower()
                    # Object summary is shown once in the dedicated "Objects" section above.
                    if s.startswith("Objects scanned:") or s.startswith("Object shortfall:"):
                        continue
                    if s.startswith("Start:") or s.startswith("End:") or "elapsed" in sl or "duration" in sl:
                        time_lines.append(s)
                    elif "object" in sl or "shortfall" in sl:
                        obj_lines.append(s)
                    elif (
                        "classification" in sl
                        or "classifier" in sl
                        or "attribute" in sl
                        or "names from classifier" in sl
                    ):
                        cls_lines.append(s)
                    else:
                        other_lines.append(s)

                oc1, oc2, oc3 = st.columns(3)
                with oc1:
                    st.markdown("**Timing**")
                    if time_lines:
                        st.markdown("\n\n".join(f"- {line}" for line in time_lines))
                    else:
                        st.caption("—")
                with oc2:
                    st.markdown("**Objects**")
                    if obj_lines:
                        st.markdown("\n\n".join(f"- {line}" for line in obj_lines))
                    else:
                        st.caption("—")
                with oc3:
                    st.markdown("**Classifications (summary)**")
                    cls_compact = [
                        x
                        for x in cls_lines
                        if "names from classifier" not in str(x).lower()
                        and "parent-scan insight" not in str(x).lower()
                    ]
                    if cls_compact:
                        st.markdown("\n\n".join(f"- {line}" for line in cls_compact))
                    else:
                        st.caption("See detailed classifier hits table below.")
                if other_lines:
                    st.markdown("**Other**")
                    st.markdown("\n\n".join(f"- {line}" for line in other_lines))
        except Exception:
            pass

        st.divider()
        # Per-classifier / attribute hits (names + table) directly under Observations
        try:
            hits_df, hits_note = classifier_hits_df, classifier_hits_note
            hits_total_hint = load_scan_insight_classifier_total_count_hint(str(scan_id))
            if (hits_df is None or hits_df.empty):
                # Fallback: call live attributes API once so users see real classifier counts even before tracker sync lands.
                live_env = get_environment_row_for_scan_id(str(scan_id))
                if live_env:
                    _live_df, _live_note, _live_total = fetch_live_scan_classifier_hits_df(str(scan_id), live_env)
                    if _live_df is not None and not _live_df.empty:
                        hits_df = _live_df
                        hits_note = _live_note
                        hits_total_hint = _live_total
            st.markdown("### Classifier / attribute hits")
            st.caption(
                "Rows come from BigID attributes response (`type=classifier`). **New findings** is the classifier hit count in this scan."
            )
            if hits_df is not None and not hits_df.empty:
                try:
                    if hits_total_hint is not None:
                        st.markdown(f"**Classifier totalCount:** **{int(hits_total_hint):,}**")
                    else:
                        st.markdown(f"**Classifiers returned:** **{int(len(hits_df)):,}**")
                except Exception:
                    pass
                if hits_note:
                    st.caption(hits_note)
                show_hits = hits_df
                if st.checkbox("Only show rows with New findings > 0 or Removed > 0", value=True, key="scan_prog_hits_filter"):
                    show_hits = hits_df[(hits_df["New findings"] > 0) | (hits_df["Removed"] > 0)]
                    if show_hits.empty:
                        show_hits = hits_df
                        st.caption("No rows with non-zero deltas; showing full table.")
                st.dataframe(show_hits, width="stretch", hide_index=True, height=min(420, 120 + 28 * min(25, len(show_hits))))
            else:
                if insight_payload_classifier_names:
                    st.markdown("**Names from parent-scan insight** (stored JSON — not the attributes breakdown table)")
                    ic1, ic2, ic3 = st.columns(3)
                    _n = len(insight_payload_classifier_names)
                    for ci, col in enumerate((ic1, ic2, ic3)):
                        lo = (_n * ci) // 3
                        hi = (_n * (ci + 1)) // 3
                        chunk = insight_payload_classifier_names[lo:hi]
                        col.markdown("\n".join(f"- `{x}`" for x in chunk) if chunk else "—")
                else:
                    st.caption("No classifier rows available yet. Use **Sync now (manual)** or wait for tracker.")
        except Exception:
            pass

        # Optional Local LLM narrative (Windows-ready later)
        llm_enabled = st.toggle("Generate LLM narrative (local)", value=False, help="Uses LOCAL_LLM_* env vars when configured.")
        if llm_enabled:
            if not _local_llm_is_configured():
                st.warning("Local LLM not configured. Set LOCAL_LLM_PROVIDER, LOCAL_LLM_BASE_URL, and LOCAL_LLM_MODEL.")
            else:
                # Build a compact, factual prompt from computed metrics (no secrets).
                try:
                    sys_p = (
                        "You are an analyst for BigID scan progress. "
                        "Explain what is happening in the scan based on the metrics. "
                        "Be concise, factual, and actionable. Do not invent numbers."
                    )
                    u_p = textwrap.dedent(f"""
                    Scan name: {scan_name_for_ai}
                    Scan id: {scan_id}
                    Latest state: {cur_state}

                    Latest snapshot:
                    - objects_scanned: {cur_objects}
                    - parts: {cur_parts_done}/{cur_parts_total}
                    - failed_parts: {cur_failed_parts}
                    - pct_parts: {pct_parts if pct_parts is not None else 'N/A'}
                    - files_per_min: {files_per_min if files_per_min is not None else 'N/A'}
                    - eta_minutes: {eta_minutes if eta_minutes is not None else 'N/A'}
                    - stall_minutes: {stall_minutes if stall_minutes is not None else 'N/A'}
                    - false_removed_delta: {false_removed_delta if false_removed_delta is not None else 'N/A'}

                    Completion lag (if detected): {completion_lag_summary or 'N/A'}
                    Flags: {', '.join(flags) if flags else 'None'}

                    Write:
                    - 3 bullet executive summary
                    - Then a short paragraph explaining likely phase (scanning vs classification/finalization)
                    - Then 3 next checks/actions
                    """).strip()
                    llm_text = generate_local_llm_analysis(sys_p, u_p)
                    if llm_text:
                        st.markdown(llm_text)
                    else:
                        st.warning("Local LLM call returned empty output (or failed).")
                except Exception as ex:
                    st.warning(f"LLM narrative failed: {ex}")

        # Regression warnings (DB-only compare vs previous build run)
        try:
            if regression_warnings:
                st.error("Possible regression detected in classification metrics:")
                for w in regression_warnings:
                    st.caption(f"- {w}")
        except Exception:
            pass

        # Total classifications discrepancy vs previous same-scenario scan
        try:
            if cls_vs_prev:
                cur_v = cls_vs_prev["cur"]
                prev_v = cls_vs_prev["prev"]
                delta_v = cls_vs_prev["delta"]
                pct_v = cls_vs_prev["pct"]
                prev_bld = cls_vs_prev["prev_build"]
                prev_s = cls_vs_prev["prev_sid"]
                direction = "higher" if delta_v > 0 else "lower"
                sign = "+" if delta_v > 0 else ""
                thr_note = ""
                if files_per_min is not None and files_per_min > 0:
                    thr_note = (
                        f" Current run throughput ≈ **{files_per_min:.1f}** objects/min — "
                        "pair this delta with **Classifier Changes** (metadata) and per-classifier hits below."
                    )
                st.error(
                    f"⚠️ **Total classifications discrepancy vs previous scan** "
                    f"(same scenario type, build `{prev_bld}`, scan `{prev_s}`): "
                    f"current = **{cur_v:,}** vs previous = **{prev_v:,}** "
                    f"— {direction} by **{sign}{delta_v:,}** ({sign}{pct_v:.1f}%). "
                    f"Possible causes: changed classifier set, data drift, or classification regression.{thr_note}"
                )
                # Always print exact missing/new names directly in this warning section.
                _missing_now = list(cls_vs_prev.get("missing") or [])
                _new_now = list(cls_vs_prev.get("new") or [])
                if _missing_now or _new_now:
                    if _new_now:
                        st.caption(
                            "Only in current: " + ", ".join(_new_now[:20]) +
                            (f" … (+{len(_new_now)-20} more)" if len(_new_now) > 20 else "")
                        )
                    if _missing_now:
                        _missing_text = (
                            "Missing in current (present in previous): " + ", ".join(_missing_now[:20]) +
                            (f" … (+{len(_missing_now)-20} more)" if len(_missing_now) > 20 else "")
                        )
                        st.markdown(
                            f"<span style='color:#d32f2f'>{_missing_text}</span>",
                            unsafe_allow_html=True,
                        )
                top_delta_rows = cls_vs_prev.get("top_delta_rows", [])
                if top_delta_rows:
                    st.markdown("**Top classifier deltas (current vs previous):**")
                    for _td in top_delta_rows:
                        st.caption(f"- {_td}")
                try:
                    cur_tc_hint = load_scan_insight_classifier_total_count_hint(str(scan_id))
                    prev_tc_hint = load_scan_insight_classifier_total_count_hint(str(prev_s))
                    if cur_tc_hint is None:
                        cur_tc_hint = len(classifier_hits_df) if classifier_hits_df is not None else None
                    if prev_tc_hint is not None and cur_tc_hint is not None and int(prev_tc_hint) != int(cur_tc_hint):
                        d_tc = int(cur_tc_hint) - int(prev_tc_hint)
                        sign_tc = "+" if d_tc > 0 else ""
                        st.warning(
                            f"Classifier totalCount mismatch vs previous scan: "
                            f"current **{int(cur_tc_hint):,}** vs previous **{int(prev_tc_hint):,}** "
                            f"({sign_tc}{d_tc:,})."
                        )

                    # Always show exact name diff when we are comparing current vs previous scan.
                    cur_map = load_scan_classifier_hits_map(str(scan_id))
                    prev_map = load_scan_classifier_hits_map(str(prev_s))
                    if cur_map or prev_map:
                        cur_set = {k for k, v in cur_map.items() if int(v or 0) > 0}
                        prev_set = {k for k, v in prev_map.items() if int(v or 0) > 0}
                        only_cur = sorted(cur_set - prev_set)
                        only_prev = sorted(prev_set - cur_set)
                        st.markdown("**Exact classifier-name mismatch (current vs previous):**")
                        if only_cur:
                            st.caption(
                                "Only in current: " + ", ".join(only_cur[:20]) +
                                (f" … (+{len(only_cur)-20} more)" if len(only_cur) > 20 else "")
                            )
                        if only_prev:
                            _missing_text = (
                                "Missing in current (present in previous): " + ", ".join(only_prev[:20]) +
                                (f" … (+{len(only_prev)-20} more)" if len(only_prev) > 20 else "")
                            )
                            st.markdown(
                                f"<span style='color:#d32f2f'>{_missing_text}</span>",
                                unsafe_allow_html=True,
                            )
                        if not only_cur and not only_prev:
                            st.caption("No missing classifier names between current and previous scan.")
                except Exception:
                    pass

                # Show classifier difference breakdown if available
                missing = cls_vs_prev.get("missing", [])
                new_cls = cls_vs_prev.get("new", [])
                changed = cls_vs_prev.get("changed", [])
                
                if missing or new_cls or changed:
                    with st.expander("🔍 View Classifier Differences", expanded=True):
                        if missing:
                            st.markdown("**Missing Classifiers (in previous, but 0 in current):**")
                            for m in missing:
                                st.caption(f"- 🔴 {m}")
                        if new_cls:
                            st.markdown("**New Classifiers (in current, but 0 in previous):**")
                            for n in new_cls:
                                st.caption(f"- 🟢 {n}")
                        if changed:
                            st.markdown("**Significantly Changed ( >10% or >100 diff ):**")
                            for c in changed:
                                st.caption(f"- 🟡 {c}")
        except Exception:
            pass

        # Compare newly-added/removed classifications vs previous scan for same scenario.
        try:
            if cls_new_removed_vs_prev:
                _nr_prev_bld = cls_new_removed_vs_prev.get("prev_build", "unknown")
                _nr_prev_sid = cls_new_removed_vs_prev.get("prev_sid", "")
                _cur_new = int(cls_new_removed_vs_prev.get("cur_new", 0) or 0)
                _prev_new = int(cls_new_removed_vs_prev.get("prev_new", 0) or 0)
                _d_new = int(cls_new_removed_vs_prev.get("d_new", 0) or 0)
                _cur_rem = int(cls_new_removed_vs_prev.get("cur_removed", 0) or 0)
                _prev_rem = int(cls_new_removed_vs_prev.get("prev_removed", 0) or 0)
                _d_rem = int(cls_new_removed_vs_prev.get("d_removed", 0) or 0)
                _s_new = "+" if _d_new > 0 else ""
                _s_rem = "+" if _d_rem > 0 else ""
                def _delta_color(v: int) -> str:
                    if v > 0:
                        return "#1b5e20"  # green
                    if v < 0:
                        return "#c62828"  # red
                    return "#616161"      # neutral gray

                _new_color = _delta_color(_d_new)
                _rem_color = _delta_color(_d_rem)
                st.markdown(
                    (
                        f"**Newly added / removed classifications vs previous scan** "
                        f"(same scenario type, build `{_nr_prev_bld}`, scan `{_nr_prev_sid}`): "
                        f"newly added **{_cur_new:,}** vs **{_prev_new:,}** "
                        f"(<span style='color:{_new_color};font-weight:700'>{_s_new}{_d_new:,}</span>), "
                        f"removed **{_cur_rem:,}** vs **{_prev_rem:,}** "
                        f"(<span style='color:{_rem_color};font-weight:700'>{_s_rem}{_d_rem:,}</span>)."
                    ),
                    unsafe_allow_html=True,
                )
        except Exception:
            pass

        # Highlight completion lag (the issue you mentioned: scan hit 100% but status took hours to flip)
        try:
            if completion_lag_summary and completion_lag_minutes is not None and completion_lag_minutes >= 30:
                st.error(f"Status update lag detected: {completion_lag_summary}.")
            elif completion_lag_summary:
                st.warning(f"Status update lag: {completion_lag_summary}.")
        except Exception:
            pass

        # Red warning when scan appears done but still running
        try:
            running_states_local = {"started", "awaiting", "inprogress", "in progress", "running", "queued", "pending"}
            is_running = str(cur_state or "").strip().lower().replace(" ", "") in {s.replace(" ", "") for s in running_states_local}
            # Do not infer "scanning work done" from expected object totals (1,013,583/1,013,583).
            # That ratio is a benchmark hint only — lifecycle status must follow API scan_state / parent scan.
            done_parts = bool(cur_parts_total and cur_parts_total > 0 and cur_parts_done >= cur_parts_total)

            # "No scanning progress" should consider BOTH parts + objects.
            no_progress_last3 = False
            try:
                if len(df_ai) >= 3:
                    a = df_ai.iloc[-3]
                    b = df_ai.iloc[-1]
                    no_progress_last3 = (
                        int(_safe_num(a.get("objects_scanned"), 0) or 0) == int(_safe_num(b.get("objects_scanned"), 0) or 0)
                        and int(_safe_num(a.get("completed_parts"), 0) or 0) == int(_safe_num(b.get("completed_parts"), 0) or 0)
                    )
            except Exception:
                no_progress_last3 = False

            stalled = bool(no_progress_last3) or (
                stall_minutes is not None and stall_minutes >= 30 and files_per_min is not None and abs(files_per_min) < 1e-9
            )
            scanning_done = bool(done_parts)

            if is_running and stalled:
                extra = []
                extra.append(f"parts: {cur_parts_done:,} / {cur_parts_total:,}" if cur_parts_total else f"parts: {cur_parts_done:,}")
                extra.append(f"objects scanned: {cur_objects:,}")
                if expected_total_files and expected_total_files > 0:
                    extra.append(f"expected objects: {int(expected_total_files):,}")
                if no_progress_last3:
                    if stall_minutes is not None and stall_minutes >= 0:
                        extra.append(f"no change in parts/objects for the last **{stall_minutes:.0f} minutes** (since last snapshot with progress)")
                    else:
                        extra.append("no change in parts/objects across last snapshots (last snapshot time not available)")
                elif stall_minutes is not None and stall_minutes >= 30:
                    extra.append(f"no object-count increase for ~{stall_minutes:.0f} minutes")

                # Add classification/finalization diagnosis (no speculation about expected totals being wrong).
                try:
                    if scanning_done and len(df_ai) >= 3:
                        t = df_ai.tail(3)
                        d_pi = _safe_num(t.iloc[-1].get("objects_with_pi"), 0) - _safe_num(t.iloc[0].get("objects_with_pi"), 0)
                        d_new = _safe_num(t.iloc[-1].get("newly_added_classifications"), 0) - _safe_num(t.iloc[0].get("newly_added_classifications"), 0)
                        d_totc = _safe_num(t.iloc[-1].get("total_classifications"), 0) - _safe_num(t.iloc[0].get("total_classifications"), 0)
                        if (d_pi and d_pi != 0) or (d_new and d_new != 0) or (d_totc and d_totc != 0):
                            extra.append("classification/findings counters are still changing (post-processing still running)")
                        else:
                            extra.append("classification/findings counters are flat (likely finalization/state update lag)")
                except Exception:
                    pass

                if scanning_done:
                    msg = "Scan appears complete (scanning work looks done), but state is still InProgress."
                    st.error(msg + (" Observations: " + "; ".join(extra) + "." if extra else ""))
                else:
                    msg = "Scan appears stalled (no recent progress), but scanning does not look complete yet."
                    st.warning(msg + (" Observations: " + "; ".join(extra) + "." if extra else ""))
        except Exception:
            pass

        if narrative:
            st.markdown(narrative)

    c1, c2, c3 = st.columns(3)
    # Use actual capture time for x-axis and sort so graph order matches real sequence (first snapshot = first point).
    def _plot_time_sorted(df):
        out = df.copy()
        # Prefer actual timestamp (timestamp col) for plotting to avoid "V-spikes" caused by slot-rounding.
        # Fallback to slot time only if actual is missing.
        if "captured_at_actual" in out.columns:
            out["captured_at_utc"] = pd.to_datetime(out["captured_at_actual"], errors="coerce", utc=True)
            # If some actual times are missing, fill them with slot times
            if out["captured_at_utc"].isna().any():
                slot_times = pd.to_datetime(out["captured_at_slot_utc"], errors="coerce", utc=True)
                out["captured_at_utc"] = out["captured_at_utc"].fillna(slot_times)
        else:
            out["captured_at_utc"] = pd.to_datetime(out["captured_at_slot_utc"], errors="coerce", utc=True)

        if out["captured_at_utc"].dt.tz is not None:
            out["captured_at_utc"] = out["captured_at_utc"].dt.tz_localize(None)
        
        return out.sort_values("captured_at_utc")

    with c1:
        st.subheader("Parts (Totals)")
        plot_df = _plot_time_sorted(df)

        # Plotly: parts only
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=plot_df["captured_at_utc"],
                y=plot_df.get("completed_parts", 0),
                mode="lines+markers",
                name="Parts Completed (count)",
                line=dict(color="#10B981", width=3),  # green
                marker=dict(color="#10B981", size=7),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=plot_df["captured_at_utc"],
                y=plot_df.get("total_parts_scanned", 0),
                mode="lines+markers",
                name="Parts Total (count)",
                line=dict(color="#3B82F6", width=2),  # blue
                marker=dict(color="#3B82F6", size=7),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=plot_df["captured_at_utc"],
                y=plot_df.get("failed_parts", 0),
                mode="lines+markers",
                name="Parts Failed (count)",
                line=dict(color="#EF4444", width=2),  # red
                marker=dict(color="#EF4444", size=7),
            )
        )
        spm_apply_plotly_ticks(fig, plot_df["captured_at_utc"])
        fig.update_yaxes(title_text="Count", rangemode="tozero")
        fig.update_layout(
            margin=dict(l=10, r=10, t=30, b=70),
            legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="left", x=0),
            hovermode="x unified",
        )
        st.plotly_chart(fig, width="stretch")

    with c2:
        st.subheader("Objects (Totals)")
        plot_df = _plot_time_sorted(df)
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(
            x=plot_df["captured_at_utc"], y=plot_df.get("objects_scanned", 0),
            mode="lines+markers", name="Objects Scanned (count)",
            line=dict(color="#8B5CF6", width=3), marker=dict(color="#8B5CF6", size=7),
        ))
        fig2.add_trace(go.Scatter(
            x=plot_df["captured_at_utc"], y=plot_df.get("objects_failed", 0),
            mode="lines+markers", name="Objects Failed (count)",
            line=dict(color="#F97316", width=2), marker=dict(color="#F97316", size=7),
        ))
        spm_apply_plotly_ticks(fig2, plot_df["captured_at_utc"])
        fig2.update_yaxes(title_text="Count", rangemode="tozero")
        fig2.update_layout(
            margin=dict(l=10, r=10, t=30, b=70),
            legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="left", x=0),
            hovermode="x unified",
        )
        st.plotly_chart(fig2, width="stretch")

    with c3:
        st.subheader("ObjectsWithPi + Classifications")
        plot_df = _plot_time_sorted(df)
        fig3 = go.Figure()
        fig3.add_trace(go.Scatter(
            x=plot_df["captured_at_utc"], y=plot_df.get("objects_with_pi", 0),
            mode="lines+markers", name="objectsWithPi",
            line=dict(color="#10B981", width=3), marker=dict(color="#10B981", size=7),
        ))
        fig3.add_trace(go.Scatter(
            x=plot_df["captured_at_utc"], y=plot_df.get("total_classifications", 0),
            mode="lines+markers", name="classifications.total",
            line=dict(color="#3B82F6", width=3), marker=dict(color="#3B82F6", size=7),
        ))
        fig3.add_trace(go.Scatter(
            x=plot_df["captured_at_utc"], y=plot_df.get("newly_added_classifications", 0),
            mode="lines+markers", name="classifications.newlyAdded",
            line=dict(color="#6366F1", width=2), marker=dict(color="#6366F1", size=7),
        ))
        fig3.add_trace(go.Scatter(
            x=plot_df["captured_at_utc"], y=plot_df.get("false_removed_classifications", 0),
            mode="lines+markers", name="classifications.falseRemoved",
            line=dict(color="#EF4444", width=2), marker=dict(color="#EF4444", size=7),
        ))
        spm_apply_plotly_ticks(fig3, plot_df["captured_at_utc"])
        fig3.update_yaxes(title_text="Count", rangemode="tozero")
        fig3.update_layout(
            margin=dict(l=10, r=10, t=30, b=70),
            legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="left", x=0),
            hovermode="x unified",
        )
        st.plotly_chart(fig3, width="stretch")

    # Scanner performance (DB): group + per-scanner metrics captured by tracker or "Sync now"
    try:
        p_env_id = None
        if "environment_id" in df.columns:
            valid_ids = df["environment_id"].dropna().unique()
            if len(valid_ids) > 0:
                p_env_id = valid_ids[0]
        if p_env_id is None and scan_id and engine:
            try:
                with engine.connect() as conn:
                    p_env_id = conn.execute(
                        text(
                            "SELECT environment_id FROM scan_parents WHERE scan_id = :sid ORDER BY last_seen_at DESC, id DESC LIMIT 1"
                        ),
                        {"sid": str(scan_id)},
                    ).scalar()
            except Exception:
                pass

        _spm_expand = False
        try:
            _last_st = str(
                df.sort_values("captured_at_actual", ascending=True).iloc[-1].get("scan_state") if not df.empty else ""
            ).strip()
            _spm_expand = bool(_is_running_state(_last_st))
        except Exception:
            _spm_expand = False

        if p_env_id is not None:
            ts_series = pd.to_datetime(df["captured_at_actual"], errors="coerce")
            p_start = ts_series.min()
            p_end = ts_series.max()
            if pd.notna(p_start) and pd.notna(p_end):
                p_start_padded = p_start.to_pydatetime() - timedelta(minutes=30)
                p_end_padded = p_end.to_pydatetime() + timedelta(minutes=60)
                df_perf_g = load_scanner_group_performance(p_env_id, p_start_padded, p_end_padded)
                df_perf_i = load_scanner_individual_performance(p_env_id, p_start_padded, p_end_padded)

                if not df_perf_g.empty:
                    df_perf_g = df_perf_g.copy()
                    df_perf_g["captured_at_utc"] = pd.to_datetime(df_perf_g["captured_at_utc"], errors="coerce")
                if not df_perf_i.empty:
                    df_perf_i = df_perf_i.copy()
                    df_perf_i["captured_at_utc"] = pd.to_datetime(df_perf_i["captured_at_utc"], errors="coerce")

                _scanner_i_for_progress_merge = df_perf_i.copy() if not df_perf_i.empty else pd.DataFrame()

                _spm_has = not df_perf_g.empty or not df_perf_i.empty
                # Keep this collapsed by default; users can expand when needed.
                with st.expander("📊 Scanner Performance Metrics", expanded=False):
                    if not _spm_has:
                        st.caption(
                            "No **scanner_group_performance** / **scanner_individual_performance** in this window — "
                            "**Sync now** or run **tracker.py** with scan snapshots."
                        )
                        st.info(
                            "No rows in `scanner_group_performance` / `scanner_individual_performance` for this window. "
                            "Click **Sync now (manual)** above, or wait for **tracker.py** (runs with scan snapshots)."
                        )
                    else:
                        st.markdown(
                            """
<style>
.spm-hero {
  background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 40%, #e2e8f0 100%);
  color: #0f172a;
  border-radius: 14px;
  padding: 1rem 1.2rem 1.15rem;
  margin: 0 0 0.85rem 0;
  border: 1px solid #cbd5e1;
  box-shadow: 0 4px 18px rgba(15, 23, 42, 0.06);
  max-width: min(28rem, 100%);
  width: fit-content;
}
.spm-hero h3 { margin: 0 0 0.4rem 0; font-size: 1.12rem; font-weight: 700; letter-spacing: -0.02em; color: #0f172a; }
.spm-hero .spm-sub { margin: 0; font-size: 0.8rem; line-height: 1.5; color: #475569; }
.spm-note-ok {
  background: linear-gradient(90deg, rgba(59, 130, 246, 0.08), rgba(99, 102, 241, 0.05));
  border-left: 4px solid #6366f1;
  padding: 0.65rem 0.9rem;
  border-radius: 0 12px 12px 0;
  margin: 0.45rem 0 0.75rem 0;
  font-size: 0.88rem;
  color: #334155;
}
.spm-note-warn {
  background: rgba(254, 252, 232, 0.85);
  border-left: 4px solid #ca8a04;
  padding: 0.65rem 0.9rem;
  border-radius: 0 12px 12px 0;
  margin: 0.45rem 0 0.75rem 0;
  font-size: 0.88rem;
  color: #713f12;
}
</style>
""",
                            unsafe_allow_html=True,
                        )
                        _spm_env_name = ""
                        try:
                            _spm_er = get_test_environment_by_id(int(p_env_id))
                            if _spm_er:
                                _spm_env_name = str(_spm_er.get("env_name") or "").strip()
                        except Exception:
                            pass
                        if _spm_env_name:
                            _spm_hero_title = (
                                f"{html.escape(_spm_env_name)} "
                                f'<span style="font-weight:600;color:#64748b;font-size:0.92em">· Environment #{int(p_env_id)}</span>'
                            )
                        else:
                            _spm_hero_title = f"Environment #{int(p_env_id)}"
                        st.markdown(
                            f"""<div class="spm-hero">
<h3>{_spm_hero_title}</h3>
<p class="spm-sub">
  Window <b>{p_start_padded:%Y-%m-%d %H:%M}</b> → <b>{p_end_padded:%Y-%m-%d %H:%M}</b> UTC<br/>
  <span style="opacity:0.92">Sources: <code style="background:#e2e8f0;color:#0f172a;padding:0.12em 0.4em;border-radius:4px;font-size:0.9em">/scanner-groups</code>
  + <code style="background:#e2e8f0;color:#0f172a;padding:0.12em 0.4em;border-radius:4px;font-size:0.9em">/health-checks/services-status</code>
  · Speed stored as B/s in DB, charts use KiB/s</span>
</p></div>""",
                            unsafe_allow_html=True,
                        )
                        _spm_m = dict(
                            template="plotly_white",
                            paper_bgcolor="#f8fafc",
                            plot_bgcolor="#ffffff",
                            font=dict(color="#334155", family="ui-sans-serif, system-ui, sans-serif", size=12),
                            hovermode="x unified",
                            height=400,
                            margin=dict(l=52, r=48, t=42, b=96),
                            legend=dict(
                                orientation="h",
                                yanchor="top",
                                y=-0.30,
                                xanchor="left",
                                x=0,
                                bgcolor="rgba(248,250,252,0.92)",
                                bordercolor="#e2e8f0",
                                borderwidth=1,
                            ),
                        )
                        n_g = len(df_perf_g)
                        n_i = len(df_perf_i)
                        try:
                            _mins = []
                            _maxs = []
                            if n_g:
                                _mins.append(df_perf_g["captured_at_utc"].min())
                                _maxs.append(df_perf_g["captured_at_utc"].max())
                            if n_i:
                                _mins.append(df_perf_i["captured_at_utc"].min())
                                _maxs.append(df_perf_i["captured_at_utc"].max())
                            t_min = min(_mins) if _mins else None
                            t_max = max(_maxs) if _maxs else None
                        except Exception:
                            t_min, t_max = None, None
                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("Group samples", f"{n_g:,}")
                        m2.metric("Pod samples", f"{n_i:,}")
                        try:
                            _span_long = (
                                (t_max - t_min).total_seconds() > 86400
                                if t_min is not None
                                and t_max is not None
                                and pd.notna(t_min)
                                and pd.notna(t_max)
                                else False
                            )
                            _tfmt = "%m-%d %H:%M" if _span_long else "%H:%M"
                        except Exception:
                            _tfmt = "%H:%M"
                        m3.metric(
                            "First capture",
                            pd.Timestamp(t_min).strftime(_tfmt) if t_min is not None and pd.notna(t_min) else "—",
                        )
                        m4.metric(
                            "Last capture",
                            pd.Timestamp(t_max).strftime(_tfmt) if t_max is not None and pd.notna(t_max) else "—",
                        )

                        # Default charts: BigID **status** (Active vs Failed/inactive), not load/thread heuristics.
                        _spm_active_names = set()
                        _spm_show_inactive_status = False
                        _spm_show_all_pods = False
                        df_i_chart = df_perf_i
                        if not df_perf_i.empty:
                            try:
                                _ord = df_perf_i.sort_values("captured_at_utc")
                                _last_st = _ord.groupby("display_name", dropna=False)["status"].last()
                                _spm_active_names = {
                                    str(nm)
                                    for nm, st in _last_st.items()
                                    if not _scanner_status_inactive(st)
                                }
                                if not _spm_active_names:
                                    _spm_active_names = set(df_perf_i["display_name"].astype(str).unique())
                            except Exception:
                                _spm_active_names = set(df_perf_i["display_name"].astype(str).unique())
                            _n_scan_tot = int(df_perf_i["display_name"].nunique())
                            _n_scan_active = len(_spm_active_names)
                            _n_failed_hidden = max(0, _n_scan_tot - _n_scan_active)
                            st.markdown(
                                f"**Pods (latest BigID status):** {_n_scan_active} active · {_n_scan_tot} distinct in window"
                                + (
                                    f" · {_n_failed_hidden} failed/inactive hidden"
                                    if _n_failed_hidden
                                    else ""
                                )
                            )
                            _cb_a, _cb_b = st.columns(2)
                            with _cb_a:
                                _spm_show_inactive_status = st.checkbox(
                                    "Include failed / inactive pods",
                                    value=False,
                                    key=f"spm_show_inactive_{str(scan_id)}",
                                    help="Uses BigID **status** on the latest row per scanner (e.g. Active vs Failed).",
                                )
                            with _cb_b:
                                _spm_show_all_pods = st.checkbox(
                                    "All UUIDs in window (noisy legends)",
                                    value=False,
                                    key=f"spm_all_pods_{str(scan_id)}",
                                    help="Unchecked = BigID **running** pod set only. Check to debug every services-status UUID.",
                                )
                            if not _spm_show_inactive_status:
                                df_i_chart = df_perf_i[
                                    df_perf_i["display_name"].astype(str).isin(_spm_active_names)
                                ].copy()
                                if df_i_chart.empty:
                                    df_i_chart = df_perf_i.copy()
                                    st.warning("Active-status filter matched no rows; showing all scanners.")
                            else:
                                df_i_chart = df_perf_i.copy()

                        # Default: BigID **running** pod set, not every historical UUID from services-status.
                        _spm_eff_ids, _spm_eff_src = spm_effective_running_scanner_id_set(df_perf_g, df_perf_i)
                        if _spm_eff_ids:
                            st.markdown(
                                f'<div class="spm-note-ok"><strong>Default chart set:</strong> {len(_spm_eff_ids)} pod(s) · '
                                f'<em>{html.escape(str(_spm_eff_src))}</em> — same idea as BigID <strong>runningScanners</strong>.</div>',
                                unsafe_allow_html=True,
                            )
                        elif not df_perf_i.empty:
                            st.markdown(
                                '<div class="spm-note-warn"><strong>No running-set IDs</strong> (missing <code>scannersIDs</code> '
                                "or no <code>running_threads &gt; 0</code> on latest row). Showing <strong>all non-failed</strong> pods — "
                                "<strong>Sync now</strong> after scanner-groups capture.</div>",
                                unsafe_allow_html=True,
                            )

                        if (not df_perf_i.empty) and (not _spm_show_all_pods) and _spm_eff_ids:
                            _d_prev = df_i_chart.copy()
                            _tgid = df_i_chart[df_i_chart["scanner_id"].astype(str).isin(_spm_eff_ids)].copy()
                            if not _tgid.empty:
                                df_i_chart = _tgid
                            else:
                                st.warning(
                                    "Running-set filter matched no rows (stale IDs vs current services-status). "
                                    "Showing scanners after status filter only."
                                )
                                df_i_chart = _d_prev

                        st.caption("Tabs: **Overview** · **Groups** · **Pods** · **Raw DB**")

                        tab_sum, tab_grp, tab_scan, tab_raw = st.tabs(
                            ["Overview", "Groups", "Pods", "Raw DB"]
                        )

                        with tab_sum:
                            if not df_perf_i.empty:
                                try:
                                    snap = spm_latest_per_scanner_df(df_i_chart)
                                    if snap.empty:
                                        st.caption("No per-scanner rows after filters (try **Show all pods** or include failed).")
                                    else:
                                        snap_threads = snap.copy()
                                        snap_threads["running_threads"] = pd.to_numeric(
                                            snap_threads["running_threads"], errors="coerce"
                                        ).fillna(0)
                                        snap_threads = snap_threads.sort_values(
                                            "running_threads", ascending=True
                                        )
                                        _order = snap_threads["display_name"].astype(str).tolist()
                                        _x_lbl = [_spm_scanner_two_line_label(n) for n in _order]
                                        _y_rt = snap_threads["running_threads"].tolist()
                                        _idx_snap = snap.set_index("display_name", drop=False)
                                        _cpu_aligned = pd.to_numeric(
                                            _idx_snap.reindex(_order)["cpu_pct"], errors="coerce"
                                        )
                                        _mem_raw = _idx_snap.reindex(_order)
                                        if "memory_usage_pct" in _mem_raw.columns:
                                            _mem_aligned = pd.to_numeric(
                                                _mem_raw["memory_usage_pct"], errors="coerce"
                                            )
                                        else:
                                            _mem_aligned = pd.Series(np.nan, index=_order)
                                        if bool(_mem_aligned.isna().all()) and "physical_memory_mb" in _mem_raw.columns:
                                            _pm = pd.to_numeric(_mem_raw["physical_memory_mb"], errors="coerce")
                                            _fm = pd.to_numeric(
                                                _mem_raw["free_memory_mb"]
                                                if "free_memory_mb" in _mem_raw.columns
                                                else pd.Series(np.nan, index=_mem_raw.index),
                                                errors="coerce",
                                            )
                                            _mem_aligned = pd.Series(
                                                np.where(
                                                    _pm > 0, (_pm - _fm) / _pm * 100.0, np.nan
                                                ),
                                                index=_order,
                                            )
                                        _sum_h = max(340, min(560, 120 + len(_order) * 52))
                                        _sum_marg = dict(l=8, r=8, t=44, b=96)
                                        st.caption("Latest snapshot — **vertical bars** · `services-status`")
                                        _col_rt, _col_cpu, _col_mem = st.columns(3)
                                        with _col_rt:
                                            fig_rt = go.Figure(
                                                go.Bar(
                                                    x=_x_lbl,
                                                    y=_y_rt,
                                                    width=0.42,
                                                    marker_color="#6366F1",
                                                    hovertemplate="%{x}<br><b>Threads</b> %{y}<extra></extra>",
                                                )
                                            )
                                            fig_rt.update_layout(
                                                **_spm_layout_merge(
                                                    _spm_m,
                                                    title=dict(text="Running threads", font=dict(size=13)),
                                                    yaxis=dict(title="Threads", rangemode="tozero"),
                                                    xaxis=dict(tickfont=dict(size=10)),
                                                    showlegend=False,
                                                    bargap=0.48,
                                                    height=_sum_h,
                                                    margin=_sum_marg,
                                                ),
                                            )
                                            st.plotly_chart(fig_rt, width="stretch")
                                        with _col_cpu:
                                            if _cpu_aligned.notna().any():
                                                _mx_c = float(_cpu_aligned.max())
                                                _rng = max(110.0, _mx_c * 1.08 + 20.0) if _mx_c > 0 else 100.0
                                                fig_sb_c = go.Figure(
                                                    go.Bar(
                                                        x=_x_lbl,
                                                        y=_cpu_aligned.fillna(0.0),
                                                        width=0.42,
                                                        marker_color="#e11d48",
                                                        hovertemplate="%{x}<br><b>CPU</b> %{y:.2f}%<extra></extra>",
                                                    )
                                                )
                                                fig_sb_c.update_layout(
                                                    **_spm_layout_merge(
                                                        _spm_m,
                                                        title=dict(text="CPU %", font=dict(size=13)),
                                                        yaxis=dict(title="CPU %", rangemode="tozero", range=[0, _rng]),
                                                        xaxis=dict(tickfont=dict(size=10)),
                                                        showlegend=False,
                                                        bargap=0.48,
                                                        height=_sum_h,
                                                        margin=_sum_marg,
                                                    ),
                                                )
                                                st.plotly_chart(fig_sb_c, width="stretch")
                                            else:
                                                st.caption("No CPU % in latest rows.")
                                        with _col_mem:
                                            _mem_s = pd.to_numeric(_mem_aligned, errors="coerce")
                                            if _mem_s.notna().any():
                                                fig_sb_m = go.Figure(
                                                    go.Bar(
                                                        x=_x_lbl,
                                                        y=_mem_s.fillna(0.0),
                                                        width=0.42,
                                                        marker_color="#0891b2",
                                                        hovertemplate="%{x}<br><b>Memory</b> %{y:.2f}%<extra></extra>",
                                                    )
                                                )
                                                fig_sb_m.update_layout(
                                                    **_spm_layout_merge(
                                                        _spm_m,
                                                        title=dict(text="Memory %", font=dict(size=13)),
                                                        yaxis=dict(title="Memory %", rangemode="tozero", range=[0, 100]),
                                                        xaxis=dict(tickfont=dict(size=10)),
                                                        showlegend=False,
                                                        bargap=0.48,
                                                        height=_sum_h,
                                                        margin=_sum_marg,
                                                    ),
                                                )
                                                st.plotly_chart(fig_sb_m, width="stretch")
                                            else:
                                                st.caption("No memory % in latest rows.")
                                        c_a, c_b, c_c, c_d = st.columns(4)
                                        _mcpu = pd.to_numeric(snap.get("cpu_pct"), errors="coerce")
                                        _mload = pd.to_numeric(snap.get("system_load_average"), errors="coerce")
                                        _mmem = pd.to_numeric(snap.get("memory_usage_pct"), errors="coerce")
                                        _rt_sum = pd.to_numeric(snap["running_threads"], errors="coerce").fillna(0)
                                        with c_a:
                                            st.metric("Scanners (latest)", len(snap))
                                        with c_b:
                                            st.metric("Σ running threads", int(_rt_sum.sum()))
                                        with c_c:
                                            if _mcpu.notna().any():
                                                st.metric("Mean / peak CPU %", f"{float(_mcpu.mean()):.1f} / {float(_mcpu.max()):.1f}")
                                            elif _mload.notna().any():
                                                st.metric("Mean load avg", f"{float(_mload.mean()):.2f}")
                                            else:
                                                st.metric("Mean load avg", "—")
                                        with c_d:
                                            if _mmem.notna().any():
                                                st.metric("Mean / peak mem %", f"{float(_mmem.mean()):.1f} / {float(_mmem.max()):.1f}")
                                            elif _mcpu.notna().any():
                                                st.metric("Peak CPU %", f"{float(_mcpu.max()):.1f}")
                                            elif _mload.notna().any():
                                                st.metric("Peak load avg", f"{float(_mload.max()):.2f}")
                                            else:
                                                st.metric("Peak load avg", "—")
                                except Exception as _sum_err:
                                    st.warning(f"Could not build summary chart: `{_sum_err!s}`")
                            elif not df_perf_g.empty:
                                st.info("No per-scanner rows in window; open **Groups** tab.")
                            else:
                                st.caption("No data.")

                        with tab_grp:
                            try:
                                if df_perf_g.empty:
                                    st.caption("No group-level rows.")
                                else:
                                    _dg_tab = spm_dedupe_scanner_group_rows(df_perf_g)
                                    _agg_kw = dict(
                                        running_scanners=("running_scanners", "sum"),
                                        running_threads=("running_threads", "sum"),
                                        avg_scan_speed=("avg_scan_speed", "mean"),
                                        used_memory_mb=("used_memory_mb", "sum"),
                                        physical_memory_mb=("physical_memory_mb", "max"),
                                    )
                                    g_sum = _dg_tab.groupby("captured_at_utc", as_index=False).agg(**_agg_kw).sort_values(
                                        "captured_at_utc"
                                    )
                                    g_sum["avg_scan_speed_kib_s"] = pd.to_numeric(
                                        g_sum["avg_scan_speed"], errors="coerce"
                                    ).fillna(0.0) / 1024.0
                                    # Pod CPU → group-level trend: mean CPU % per scanner group per slot, then mean across groups (matches BigID group × pods).
                                    _cpu_t = pd.DataFrame()
                                    try:
                                        if df_perf_i is not None and not df_perf_i.empty and "cpu_pct" in df_perf_i.columns:
                                            _di = df_perf_i.copy()
                                            _di["captured_at_utc"] = pd.to_datetime(
                                                _di["captured_at_utc"], errors="coerce"
                                            )
                                            _di["cpu_pct"] = pd.to_numeric(_di["cpu_pct"], errors="coerce")
                                            _di = _di.dropna(subset=["captured_at_utc", "cpu_pct"])
                                            if not _di.empty:
                                                _has_gn = (
                                                    "group_name" in _di.columns
                                                    and _di["group_name"]
                                                    .astype(str)
                                                    .str.strip()
                                                    .replace({"nan": "", "None": ""})
                                                    .ne("")
                                                    .any()
                                                )
                                                if _has_gn:
                                                    _di["_g"] = (
                                                        _di["group_name"].astype(str).str.strip().replace(
                                                            {"nan": "", "None": "", "NaN": ""}
                                                        )
                                                    )
                                                    _sub = _di[_di["_g"].astype(str).str.len() > 0]
                                                    if not _sub.empty:
                                                        _per_g = _sub.groupby(
                                                            ["captured_at_utc", "_g"], as_index=False
                                                        )["cpu_pct"].mean()
                                                        _cpu_t = (
                                                            _per_g.groupby("captured_at_utc", as_index=False)[
                                                                "cpu_pct"
                                                            ]
                                                            .mean()
                                                            .sort_values("captured_at_utc")
                                                        )
                                                if _cpu_t.empty:
                                                    _cpu_t = (
                                                        _di.groupby("captured_at_utc", as_index=False)["cpu_pct"]
                                                        .mean()
                                                        .sort_values("captured_at_utc")
                                                    )
                                                _cpu_t = _cpu_t.rename(columns={"cpu_pct": "group_cpu_pct"})
                                    except Exception:
                                        _cpu_t = pd.DataFrame()

                                    cga, cgb, cgc = st.columns(3)
                                    with cga:
                                        st.markdown("**Capacity vs throughput**")
                                        fig_a = go.Figure()
                                        fig_a.add_trace(
                                            go.Scatter(
                                                x=g_sum["captured_at_utc"],
                                                y=g_sum["running_scanners"],
                                                mode="lines+markers",
                                                name="Running scanners (Σ groups, deduped)",
                                                line=dict(color="#2563EB", width=2),
                                            )
                                        )
                                        fig_a.add_trace(
                                            go.Scatter(
                                                x=g_sum["captured_at_utc"],
                                                y=g_sum["avg_scan_speed_kib_s"],
                                                mode="lines+markers",
                                                name="Avg scan speed (mean, KiB/s)",
                                                line=dict(color="#059669", width=2),
                                                yaxis="y2",
                                            )
                                        )
                                        fig_a.update_layout(
                                            **_spm_layout_merge(
                                                _spm_m,
                                                yaxis=dict(title="Scanners", rangemode="tozero"),
                                                yaxis2=dict(
                                                    title="Scan speed (KiB/s)",
                                                    overlaying="y",
                                                    side="right",
                                                    rangemode="tozero",
                                                ),
                                            ),
                                        )
                                        spm_apply_plotly_ticks(fig_a, g_sum["captured_at_utc"])
                                        st.plotly_chart(fig_a, width="stretch")
                                        st.caption(
                                            "**Running scanners:** summed per timestamp after **dedupe (time × group_name)** so duplicate rows don’t inflate. "
                                            "Right axis: `avgScanSpeed` as **KiB/s** (÷1024); raw **B/s** in **Raw tables**."
                                        )
                                    with cgb:
                                        st.markdown("**Group memory (aggregated)**")
                                        fig_b = go.Figure()
                                        fig_b.add_trace(
                                            go.Scatter(
                                                x=g_sum["captured_at_utc"],
                                                y=g_sum["used_memory_mb"],
                                                mode="lines+markers",
                                                name="Σ used (MB)",
                                                fill="tozeroy",
                                                line=dict(color="#DC2626", width=2),
                                            )
                                        )
                                        fig_b.add_trace(
                                            go.Scatter(
                                                x=g_sum["captured_at_utc"],
                                                y=g_sum["physical_memory_mb"],
                                                mode="lines+markers",
                                                name="Max physical (MB)",
                                                line=dict(color="#7C3AED", width=1, dash="dot"),
                                            )
                                        )
                                        fig_b.update_layout(
                                            **_spm_layout_merge(
                                                _spm_m,
                                                yaxis=dict(title="MB", rangemode="tozero"),
                                            ),
                                        )
                                        spm_apply_plotly_ticks(fig_b, g_sum["captured_at_utc"])
                                        st.plotly_chart(fig_b, width="stretch")
                                    with cgc:
                                        st.markdown("**Group CPU (aggregated)**")
                                        if not _cpu_t.empty:
                                            fig_gcpu = go.Figure()
                                            fig_gcpu.add_trace(
                                                go.Scatter(
                                                    x=_cpu_t["captured_at_utc"],
                                                    y=_cpu_t["group_cpu_pct"],
                                                    mode="lines+markers",
                                                    name="Mean CPU % (pods)",
                                                    line=dict(color="#e11d48", width=2),
                                                )
                                            )
                                            try:
                                                _ymx = float(_cpu_t["group_cpu_pct"].max())
                                            except Exception:
                                                _ymx = 0.0
                                            _yr = max(110.0, _ymx * 1.08 + 10.0) if _ymx > 0 else 100.0
                                            fig_gcpu.update_layout(
                                                **_spm_layout_merge(
                                                    _spm_m,
                                                    yaxis=dict(title="CPU %", rangemode="tozero", range=[0, _yr]),
                                                ),
                                            )
                                            spm_apply_plotly_ticks(fig_gcpu, _cpu_t["captured_at_utc"])
                                            st.plotly_chart(fig_gcpu, width="stretch")
                                            st.caption(
                                                "Mean **pod CPU %** per snapshot: averaged within each **group_name**, "
                                                "then averaged across groups (same slots as scanner-groups). "
                                                "Falls back to all pods if `group_name` is empty."
                                            )
                                        else:
                                            st.caption(
                                                "No pod **cpu_pct** in this window — **Sync now** or wait for **tracker** "
                                                "(`services-status`)."
                                            )

                            except Exception as _grp_err:
                                st.error(f"Scanner groups tab: `{_grp_err!s}`")
                        with tab_scan:
                            try:
                                if df_perf_i.empty:
                                    st.caption("No per-scanner rows.")
                                elif df_i_chart.empty:
                                    st.caption("No rows after filters.")
                                else:
                                    st.markdown("### Pods · `services-status`")
                                    st.caption(
                                        "**CPU %** = BigID table formula when no native field: load × 100 ÷ processors. "
                                        "**Memory %** = (physical − free) / physical. "
                                        "Group-level charts → **Groups** tab."
                                    )
                                    _snap_ps = spm_latest_per_scanner_df(df_i_chart)
                                    st.markdown("##### Latest snapshot")
                                    _bcpu, _bmem = st.columns(2)
                                    with _bcpu:
                                        _row_cpu = _snap_ps.dropna(subset=["cpu_pct"]).copy()
                                        if _row_cpu.empty:
                                            st.info(
                                                "No **cpu_pct** (and nothing to derive). **Sync now** or wait for **tracker**; "
                                                "set **`BIGID_SCANNER_CPU_DERIVE_FROM_LOAD=1`** (default)."
                                            )
                                        else:
                                            _row_cpu = _row_cpu.sort_values("cpu_pct", ascending=True)
                                            _la = pd.to_numeric(_row_cpu["system_load_average"], errors="coerce")
                                            _ap = pd.to_numeric(_row_cpu.get("available_processors"), errors="coerce")
                                            _la_h = _la.map(lambda x: f"{float(x):.4f}" if pd.notna(x) else "—")
                                            _ap_h = _ap.map(lambda x: str(int(x)) if pd.notna(x) and float(x) > 0 else "—")
                                            fig_bar_cpu = go.Figure(
                                                go.Bar(
                                                    x=_row_cpu["cpu_pct"],
                                                    y=_row_cpu["display_name"],
                                                    orientation="h",
                                                    marker_color="#e11d48",
                                                    hovertemplate=(
                                                        "%{y}<br><b>CPU</b> %{x:.2f}%<br>status: %{customdata[0]}<br>"
                                                        "load: %{customdata[1]} · processors: %{customdata[2]}<extra></extra>"
                                                    ),
                                                    customdata=np.column_stack(
                                                        [_row_cpu["status"].astype(str).values, _la_h.values, _ap_h.values]
                                                    ),
                                                )
                                            )
                                            _cmx = float(_row_cpu["cpu_pct"].max())
                                            _ctop = max(110.0, _cmx * 1.08 + 25.0) if _cmx > 0 else 100.0
                                            fig_bar_cpu.update_layout(
                                                **_spm_layout_merge(
                                                    _spm_m,
                                                    title="CPU % (BigID Scanners table)",
                                                    xaxis=dict(title="CPU %", range=[0, _ctop]),
                                                    yaxis=dict(autorange="reversed"),
                                                    showlegend=False,
                                                    height=max(340, 32 * len(_row_cpu) + 100),
                                                ),
                                            )
                                            st.plotly_chart(fig_bar_cpu, width="stretch")
                                    with _bmem:
                                        _row_mem = _snap_ps.sort_values("memory_usage_pct", ascending=True).copy()
                                        _um = pd.to_numeric(_row_mem["used_memory_mb"], errors="coerce")
                                        _pm = pd.to_numeric(_row_mem["physical_memory_mb"], errors="coerce")
                                        fig_bar_mem = go.Figure(
                                            go.Bar(
                                                x=_row_mem["memory_usage_pct"],
                                                y=_row_mem["display_name"],
                                                orientation="h",
                                                marker_color="#0891b2",
                                                hovertemplate=(
                                                    "%{y}<br><b>Memory</b> %{x:.2f}%<br>used %{customdata[0]:.0f} MB / "
                                                    "phys %{customdata[1]:.0f} MB<br>status: %{customdata[2]}<extra></extra>"
                                                ),
                                                customdata=np.column_stack(
                                                    [
                                                        _um.fillna(0),
                                                        _pm.fillna(0),
                                                        _row_mem["status"].astype(str),
                                                    ]
                                                ),
                                            )
                                        )
                                        fig_bar_mem.update_layout(
                                            **_spm_layout_merge(
                                                _spm_m,
                                                title="Memory usage % (host reported by scanner)",
                                                xaxis=dict(title="Memory %", range=[0, 100]),
                                                yaxis=dict(autorange="reversed"),
                                                showlegend=False,
                                                height=max(340, 32 * len(_row_mem) + 100),
                                            ),
                                        )
                                        st.plotly_chart(fig_bar_mem, width="stretch")

                                    st.markdown("#### Over time — **CPU %** *(per scanner)*")
                                    _cpu_series = pd.to_numeric(df_i_chart.get("cpu_pct"), errors="coerce")
                                    _has_cpu = bool(_cpu_series.notna().any())
                                    if _has_cpu:
                                        fig_cpu = px.line(
                                            df_i_chart.dropna(subset=["cpu_pct"]),
                                            x="captured_at_utc",
                                            y="cpu_pct",
                                            color="display_name",
                                            markers=True,
                                        )
                                        fig_cpu.update_traces(mode="lines+markers")
                                        _cpu_mx = float(_cpu_series.max() or 0)
                                        if _cpu_mx <= 0:
                                            _cpu_top = 100.0
                                        elif _cpu_mx < 100:
                                            _cpu_top = max(100.0, _cpu_mx * 1.25 + 10.0)
                                        else:
                                            _cpu_top = max(700.0, _cpu_mx * 1.15 + 50.0)
                                        fig_cpu.update_layout(
                                            **_spm_layout_merge(
                                                _spm_m,
                                                yaxis=dict(title="CPU %", rangemode="tozero", range=[0, _cpu_top]),
                                            ),
                                        )
                                        spm_apply_plotly_ticks(fig_cpu, df_i_chart["captured_at_utc"])
                                        st.caption(f"Y-axis capped ~**{_cpu_top:.0f}%** (series max **{_cpu_mx:.1f}%**).")
                                        st.plotly_chart(fig_cpu, width="stretch")
                                    else:
                                        st.warning(
                                            "No **cpu_pct** in this window. **Sync now** / **tracker** after upgrading capture; "
                                            "**`BIGID_SCANNER_CPU_DERIVE_FROM_LOAD`** (default on) or store **`available_processors`** for display backfill."
                                        )

                                    st.markdown("#### Over time — **memory usage %** *(per scanner)*")
                                    fig_mem_pct = px.line(
                                        df_i_chart,
                                        x="captured_at_utc",
                                        y="memory_usage_pct",
                                        color="display_name",
                                        markers=True,
                                    )
                                    fig_mem_pct.update_traces(mode="lines+markers")
                                    fig_mem_pct.update_layout(
                                        **_spm_layout_merge(
                                            _spm_m,
                                            yaxis=dict(title="Memory %", rangemode="tozero", range=[0, 100]),
                                        ),
                                    )
                                    spm_apply_plotly_ticks(fig_mem_pct, df_i_chart["captured_at_utc"])
                                    st.plotly_chart(fig_mem_pct, width="stretch")

                                    st.markdown("#### Over time — **used memory (MB)** *(absolute)*")
                                    fig_mem_mb = px.line(
                                        df_i_chart,
                                        x="captured_at_utc",
                                        y="used_memory_mb",
                                        color="display_name",
                                        markers=True,
                                    )
                                    fig_mem_mb.update_traces(mode="lines+markers")
                                    fig_mem_mb.update_layout(
                                        **_spm_layout_merge(
                                            _spm_m,
                                            yaxis=dict(title="Used MB", rangemode="tozero"),
                                        ),
                                    )
                                    spm_apply_plotly_ticks(fig_mem_mb, df_i_chart["captured_at_utc"])
                                    st.plotly_chart(fig_mem_mb, width="stretch")

                                    st.markdown("#### Over time — **running threads**")
                                    fig_t = px.line(
                                        df_i_chart,
                                        x="captured_at_utc",
                                        y="running_threads",
                                        color="display_name",
                                        markers=True,
                                    )
                                    fig_t.update_traces(mode="lines+markers")
                                    fig_t.update_layout(**_spm_m)
                                    spm_apply_plotly_ticks(fig_t, df_i_chart["captured_at_utc"])
                                    st.plotly_chart(fig_t, width="stretch")

                                    with st.expander("Raw **system load average** from API *(not the same as CPU %)*"):
                                        fig_l = px.line(
                                            df_i_chart,
                                            x="captured_at_utc",
                                            y="system_load_average",
                                            color="display_name",
                                            markers=True,
                                        )
                                        fig_l.update_traces(mode="lines+markers")
                                        fig_l.update_layout(**_spm_m)
                                        spm_apply_plotly_ticks(fig_l, df_i_chart["captured_at_utc"])
                                        st.plotly_chart(fig_l, width="stretch")

                            except Exception as _scan_err:
                                st.error(f"Per-scanner tab: `{_scan_err!s}`")
                        with tab_raw:
                            try:
                                st.caption("Tables below are **unfiltered** (all scanners in the time window).")
                                if not df_perf_g.empty:
                                    st.markdown("**scanner_group_performance**")
                                    _raw_g = df_perf_g.sort_values("captured_at_utc", ascending=False).head(500).copy()
                                    if "avg_scan_speed" in _raw_g.columns:
                                        _raw_g["avg_scan_speed_kib_s"] = (
                                            pd.to_numeric(_raw_g["avg_scan_speed"], errors="coerce").fillna(0.0) / 1024.0
                                        )
                                    st.dataframe(_raw_g, width="stretch", height=280)
                                if not df_perf_i.empty:
                                    st.markdown("**scanner_individual_performance**")
                                    _raw_i = df_perf_i.sort_values("captured_at_utc", ascending=False).head(500).copy()
                                    if "memory_usage_pct" not in _raw_i.columns and "physical_memory_mb" in _raw_i.columns:
                                        _pm = pd.to_numeric(_raw_i["physical_memory_mb"], errors="coerce")
                                        _fm = pd.to_numeric(_raw_i["free_memory_mb"], errors="coerce")
                                        _raw_i["memory_usage_pct"] = np.where(
                                            _pm > 0, (_pm - _fm) / _pm * 100.0, np.nan
                                        )
                                    st.dataframe(_raw_i, width="stretch", height=320)
                            except Exception as _raw_err:
                                st.error(f"Raw tables tab: `{_raw_err!s}`")
    except Exception:
        pass

    st.subheader("Raw Progress Data")
    # Show oldest first so the first row shows scan start time.
    show_df = df.copy()
    try:
        show_df = merge_scanner_averages_into_progress_df(show_df, _scanner_i_for_progress_merge)
    except Exception:
        pass
    try:
        show_df = show_df.rename(columns={
            "captured_at_actual": "captured_at_utc",
        })
        sort_col0 = "captured_at_utc" if "captured_at_utc" in show_df.columns else "captured_at_actual"
        if sort_col0 in show_df.columns:
            show_df = show_df.sort_values(sort_col0, ascending=True)
        for c in ["captured_at_utc"]:
            if c in show_df.columns:
                show_df[c] = pd.to_datetime(show_df[c], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
        cols = [
            "captured_at_utc",
            "scan_state",
            "completed_parts",
            "total_parts_scanned",
            "objects_scanned",
            "total_classifications",
        ]
        existing_cols = [c for c in cols if c in show_df.columns]
        other_cols = [c for c in show_df.columns if c not in existing_cols]
        if existing_cols:
            show_df = show_df[existing_cols + other_cols]
    except Exception:
        pass
    try:
        sort_col = "captured_at_utc" if "captured_at_utc" in show_df.columns else "captured_at_actual"
        show_sorted = show_df.sort_values(sort_col, ascending=True)
    except Exception:
        show_sorted = show_df
    try:
        _hdr_map = {
            c: _RAW_PROGRESS_HEADER_OVERRIDES.get(c, _progress_column_header_two_lines(c))
            for c in show_sorted.columns
        }
        show_disp = show_sorted.rename(columns=_hdr_map)
    except Exception:
        show_disp = show_sorted

    st.dataframe(show_disp, width="stretch")
    
    if st.button("Back to Scan Status"):
        st.query_params["page"] = "Scan Status"
        st.rerun()


@st.cache_data(ttl=600)
def _build_default_workflow_steps() -> List[Dict[str, Any]]:
    """
    UI plan (no side-effects). Backend runner will execute these sequentially.
    Environment IDs are selected at execution time, not creation time.
    """
    base: List[Dict[str, Any]] = [
        {"ds_kind": "postgres", "scan_kind": "full_scan", "file_set_type": None, "is_acl": False, "is_ols": False, "is_structured_ff": False, "feature_flags": None, "notes": "Postgres Full Scan"},
        {"ds_kind": "mssql", "scan_kind": "full_scan", "file_set_type": None, "is_acl": False, "is_ols": False, "is_structured_ff": False, "feature_flags": None, "notes": "MSSQL Full Scan"},

        {"ds_kind": "s3",  "scan_kind": "full_scan", "file_set_type": "Small Files", "is_acl": False, "is_ols": False, "is_structured_ff": False, "feature_flags": None, "notes": "S3 Full Scan (Small Files)"},
        {"ds_kind": "smb", "scan_kind": "full_scan", "file_set_type": "Small Files", "is_acl": False, "is_ols": False, "is_structured_ff": False, "feature_flags": None, "notes": "SMB Full Scan (Small Files)"},

        {"ds_kind": "s3",  "scan_kind": "full_scan", "file_set_type": "Large Files", "is_acl": False, "is_ols": False, "is_structured_ff": False, "feature_flags": None, "notes": "S3 Full Scan (Large Files)"},
        {"ds_kind": "smb", "scan_kind": "full_scan", "file_set_type": "Large Files", "is_acl": False, "is_ols": False, "is_structured_ff": False, "feature_flags": None, "notes": "SMB Full Scan (Large Files)"},

        {"ds_kind": "s3",  "scan_kind": "full_scan", "file_set_type": "Small Files", "is_acl": True, "is_ols": False, "is_structured_ff": False, "feature_flags": "ACL", "notes": "S3 Full Scan (Small Files) ACL ON"},
        {"ds_kind": "smb", "scan_kind": "full_scan", "file_set_type": "Small Files", "is_acl": True, "is_ols": False, "is_structured_ff": False, "feature_flags": "ACL", "notes": "SMB Full Scan (Small Files) ACL ON"},

        {"ds_kind": "s3",  "scan_kind": "full_scan", "file_set_type": "Large Files", "is_acl": True, "is_ols": False, "is_structured_ff": False, "feature_flags": "ACL", "notes": "S3 Full Scan (Large Files) ACL ON"},
        {"ds_kind": "smb", "scan_kind": "full_scan", "file_set_type": "Large Files", "is_acl": True, "is_ols": False, "is_structured_ff": False, "feature_flags": "ACL", "notes": "SMB Full Scan (Large Files) ACL ON"},

        {"ds_kind": "s3",  "scan_kind": "full_scan", "file_set_type": "CSV Files", "is_acl": False, "is_ols": False, "is_structured_ff": False, "feature_flags": None, "notes": "S3 Full Scan (CSV Files)"},
        {"ds_kind": "s3",  "scan_kind": "full_scan", "file_set_type": "CSV Files", "is_acl": False, "is_ols": False, "is_structured_ff": True,  "feature_flags": "Structured Files Discovery", "notes": "S3 Full Scan (CSV Files) Structured Discovery FF ON"},

        {"ds_kind": "s3",  "scan_kind": "full_scan", "file_set_type": "Small Files", "is_acl": False, "is_ols": True, "is_structured_ff": False, "feature_flags": "OLS", "notes": "S3 Full Scan (Small Files) NO ACL + OLS (enable FF before OLS)"},
        {"ds_kind": "smb", "scan_kind": "full_scan", "file_set_type": "Small Files", "is_acl": False, "is_ols": True, "is_structured_ff": False, "feature_flags": "OLS", "notes": "SMB Full Scan (Small Files) NO ACL + OLS (enable FF before OLS)"},

        {"ds_kind": "s3",  "scan_kind": "full_scan", "file_set_type": "Small Files", "is_acl": True, "is_ols": True, "is_structured_ff": False, "feature_flags": "ACL, OLS", "notes": "S3 Full Scan (Small Files) ACL ON + OLS (enable FF before OLS)"},
        {"ds_kind": "smb", "scan_kind": "full_scan", "file_set_type": "Small Files", "is_acl": True, "is_ols": True, "is_structured_ff": False, "feature_flags": "ACL, OLS", "notes": "SMB Full Scan (Small Files) ACL ON + OLS (enable FF before OLS)"},

        {"ds_kind": "s3",  "scan_kind": "full_scan", "file_set_type": "Large Files", "is_acl": False, "is_ols": True, "is_structured_ff": False, "feature_flags": "OLS", "notes": "S3 Full Scan (Large Files) NO ACL + OLS (enable FF before OLS)"},
        {"ds_kind": "smb", "scan_kind": "full_scan", "file_set_type": "Large Files", "is_acl": False, "is_ols": True, "is_structured_ff": False, "feature_flags": "OLS", "notes": "SMB Full Scan (Large Files) NO ACL + OLS (enable FF before OLS)"},

        {"ds_kind": "s3",  "scan_kind": "full_scan", "file_set_type": "Large Files", "is_acl": True, "is_ols": True, "is_structured_ff": False, "feature_flags": "ACL, OLS", "notes": "S3 Full Scan (Large Files) ACL ON + OLS (enable FF before OLS)"},
        {"ds_kind": "smb", "scan_kind": "full_scan", "file_set_type": "Large Files", "is_acl": True, "is_ols": True, "is_structured_ff": False, "feature_flags": "ACL, OLS", "notes": "SMB Full Scan (Large Files) ACL ON + OLS (enable FF before OLS)"},
    ]

    def _standard_ds_name(ds_kind: str, file_set_type: Optional[str], is_acl: bool, is_ols: bool, is_structured_ff: bool) -> str:
        k = str(ds_kind or "").strip().lower()
        fs = str(file_set_type or "").strip()
        if k in {"s3", "smb"}:
            prefix = "S3" if k == "s3" else "SMB"
            if fs:
                nm = f"{prefix} {fs}"
            else:
                nm = prefix

            # Only tag when ACL is enabled; default case stays clean (no "No ACL")
            if fs in {"Small Files", "Large Files"} and bool(is_acl):
                nm += " With ACL"

            # Structured discovery naming (esp for CSV)
            if bool(is_structured_ff):
                nm += " Structured Files Discovery"

            if bool(is_ols):
                nm += " With OLS"

            return nm.strip()

        if k == "postgres":
            return "Postgres"
        if k == "mssql":
            return "MSSQL"
        if k == "mysql":
            return "MySQL"
        return str(ds_kind or "").strip().upper() or "DS"

    steps: List[Dict[str, Any]] = []
    for idx, stp in enumerate(base):
        stp2 = dict(stp)
        stp2["environment_id"] = None
        stp2["ds_name"] = _standard_ds_name(
            str(stp2.get("ds_kind") or ""),
            stp2.get("file_set_type"),
            bool(stp2.get("is_acl")),
            bool(stp2.get("is_ols")),
            bool(stp2.get("is_structured_ff")),
        )
        stp2["status"] = "pending"
        steps.append(stp2)
    return steps


@st.fragment
def render_workflow_creation_section(wf_pool_df, in_use_env_ids):
    def _env_label(r):
        lbl = f"{r.env_name} (id={int(r.id)})"
        if int(r.id) in in_use_env_ids:
            lbl = f"⚠️ {lbl} [IN USE]"
        return lbl

    with st.expander("➕ Create New Workflow Run", expanded=False):
        if wf_pool_df.empty:
            st.warning(
                "No environments are in the **workflow pool**. Enable **Use for workflow runs** "
                "for at least one active environment in **Manage Test Environments**."
            )
        else:
            if in_use_env_ids:
                st.warning(
                    "**Environments marked ⚠️ [IN USE]** are currently running another workflow. "
                    "You can still create a plan; steps queue until capacity is free."
                )

            st.info(
                "💡 **Tip:** Test Environments are mapped when you click **start** (▶️) on a run below, "
                "so you can reuse workflow plans across different active environments."
            )

            c1, c2 = st.columns([2.0, 1.0])
            wf_name = c1.text_input(
                "Workflow name",
                value=f"DS+Scan workflow {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
                key="wf_name_new_expander",
            )
            cooldown = int(
                c2.number_input(
                    "Cooldown (minutes)",
                    min_value=1,
                    max_value=180,
                    value=15,
                    step=1,
                    key="wf_cd_new_expander",
                )
            )

            create_disabled = False

            preview_steps = _build_default_workflow_steps()
            for stp in preview_steps or []:
                if isinstance(stp, dict) and "is_enabled" not in stp:
                    stp["is_enabled"] = True
            with st.expander(f"Preview steps ({len(preview_steps or [])})", expanded=False):
                prev_df = pd.DataFrame(preview_steps)
                show_cols = [
                    "is_enabled",
                    "environment_id",
                    "ds_kind",
                    "ds_name",
                    "scan_kind",
                    "file_set_type",
                    "is_acl",
                    "is_structured_ff",
                    "is_ols",
                    "feature_flags",
                    "notes",
                    "status",
                ]
                show_cols = [c for c in show_cols if c in prev_df.columns]
                editable_df = st.data_editor(
                    prev_df[show_cols],
                    width="stretch",
                    hide_index=True,
                    key="wf_preview_steps_editor_new_expander",
                    column_config={
                        "is_enabled": st.column_config.CheckboxColumn("Execute", default=True),
                    },
                    disabled=[c for c in show_cols if c != "is_enabled"],
                )
                try:
                    preview_steps = editable_df.to_dict("records")
                except Exception:
                    pass

            if st.button("Create workflow plan", type="primary", disabled=create_disabled, key="wf_create_btn_expander"):
                rid = create_workflow_run(
                    wf_name,
                    env1_id=None,
                    env2_id=None,
                    steps=preview_steps,
                    cooldown_minutes=int(cooldown),
                )
                if rid:
                    st.success(f"Created workflow run id={rid}. Reloading…")
                    st.query_params["page"] = "Workflow Automation"
                    st.query_params["run_id"] = str(int(rid))
                    try:
                        st.session_state["wf_run_sel_id"] = int(rid)
                    except Exception:
                        pass
                    try:
                        # Since we're in a fragment, we want to clear the whole app cache for new data
                        st.cache_data.clear()
                    except Exception:
                        pass
                    st.rerun()
                else:
                    st.error("Failed to create workflow plan.")

@st.fragment
def render_workflow_step_editor(run_id, selected_status):
    steps_df = load_workflow_steps(int(run_id))
    with st.expander("Edit steps (before run)", expanded=False):
        st.caption("Toggle which steps should execute. Pause/Stop the run first.")
        if selected_status == "running":
            st.info("Pause/Stop the workflow run to edit steps.")
        elif steps_df.empty:
            st.info("No steps found for this run.")
        else:
            edit_df = steps_df.copy()
            if "is_enabled" not in edit_df.columns:
                edit_df["is_enabled"] = edit_df["status"].fillna("").astype(str).str.strip().str.lower().ne("disabled").astype(int)
            try: edit_df["is_enabled"] = edit_df["is_enabled"].apply(lambda x: bool(int(x)) if str(x).strip() else bool(x))
            except Exception: edit_df["is_enabled"] = edit_df["is_enabled"].apply(lambda x: bool(x))
            show_cols = ["id", "seq", "environment", "ds_kind", "ds_name", "file_set_type", "is_acl", "is_structured_ff", "is_ols", "status", "is_enabled"]
            show_cols = [c for c in show_cols if c in edit_df.columns]
            try:
                env_df = load_test_environments()
                if not env_df.empty and "is_active" in env_df.columns:
                    active_envs = env_df[env_df["is_active"] == 1]["env_name"].tolist()
                    env_map = {row["env_name"]: row["id"] for _, row in env_df.iterrows()}
                else:
                    active_envs, env_map = [], {}
            except Exception:
                active_envs, env_map = [], {}

            view = edit_df[show_cols].rename(columns={"seq": "Step", "environment": "Env", "ds_kind": "DS Type", "ds_name": "DS Name", "file_set_type": "File Set", "status": "Step Status"})
            editor = st.data_editor(view, width="stretch", hide_index=True, key=f"wf_edit_steps_{run_id}",
                column_config={
                    "is_enabled": st.column_config.CheckboxColumn("Execute", default=True),
                    "Env": st.column_config.SelectboxColumn("Env", options=active_envs, required=False),
                },
                disabled=[c for c in view.columns if c not in ("is_enabled", "Env")])
            
            if st.button("Save step modifications", type="primary", key=f"wf_save_steps_{run_id}"):
                try:
                    # 1. Handle Enable/Disable
                    desired = editor[["id", "is_enabled"]].copy(); desired["id"] = desired["id"].astype(int)
                    enable_ids = [int(x) for x in desired[desired["is_enabled"] == True]["id"].tolist()]
                    disable_ids = [int(x) for x in desired[desired["is_enabled"] == False]["id"].tolist()]
                    
                    # 2. Handle Env Changes
                    desired_env = editor[["id", "Env"]].copy(); desired_env["id"] = desired_env["id"].astype(int)
                    env_updates = []
                    for _, r in desired_env.iterrows():
                        if pd.notna(r["Env"]) and str(r["Env"]).strip() and str(r["Env"]).strip() in env_map:
                            env_updates.append({"id": int(r["id"]), "env_id": int(env_map[str(r["Env"]).strip()])})

                    from sqlalchemy import bindparam
                    with engine.connect() as conn:
                        if disable_ids:
                            conn.execute(text("UPDATE workflow_steps SET is_enabled=0, status='disabled', updated_at=CURRENT_TIMESTAMP WHERE id IN :ids AND COALESCE(status,'')<>'completed'").bindparams(bindparam("ids", expanding=True)), {"ids": disable_ids})
                        if enable_ids:
                            conn.execute(text("UPDATE workflow_steps SET is_enabled=1, status=CASE WHEN COALESCE(status,'')='disabled' THEN 'pending' ELSE status END, last_error=CASE WHEN COALESCE(status,'')='disabled' THEN NULL ELSE last_error END, updated_at=CURRENT_TIMESTAMP WHERE id IN :ids AND COALESCE(status,'')<>'completed'").bindparams(bindparam("ids", expanding=True)), {"ids": enable_ids})
                        
                        # Process environment updates
                        for u in env_updates:
                            conn.execute(text("UPDATE workflow_steps SET environment_id=:env_id, updated_at=CURRENT_TIMESTAMP WHERE id=:id AND COALESCE(status,'') NOT IN ('completed', 'scan_running')"), {"env_id": u["env_id"], "id": u["id"]})

                        conn.commit()
                    try: st.cache_data.clear()
                    except Exception: pass
                    st.success("Saved."); st.rerun()
                except Exception as ex:
                    st.error(f"Save failed: {ex}")



@st.fragment
def page_workflow_automation():
    global engine
    st.title("🧪 Workflow Automation")
    st.caption(
        "UI-first: plan + pause/resume. The runner uses every **workflow-pool** env (Manage Test Environments → **Use for workflow runs**) "
        "and runs **up to one active step per env in parallel** (odd steps → Env 1, even → Env 2). "
        "Each step: DS → connection test → scan profile → start scan → wait for terminal state → cooldown. "
        "Concurrency cap: `WORKFLOW_MAX_CONCURRENT_SCANS` (default = pool size)."
    )

    ensure_test_environments_table(engine)
    ensure_scan_tracking_tables()
    ensure_scan_progress_table()
    ensure_workflow_tables()

    def _wf_env_int(val: Any, fallback: int) -> int:
        """workflow_runs env ids from DataFrames may be float NaN; never pass those to int()."""
        if val is None:
            return fallback
        try:
            if pd.isna(val):
                return fallback
        except Exception:
            pass
        try:
            return int(val)
        except (TypeError, ValueError):
            return fallback

    _wf_flash = st.session_state.pop("wf_delete_flash", None)
    if _wf_flash:
        st.success(str(_wf_flash))
    _wf_reconcile_flash = st.session_state.pop("wf_reconcile_flash", None)
    if _wf_reconcile_flash:
        st.success(str(_wf_reconcile_flash))

    # Query-param actions (runs before env empty-check so delete/start still work if env list is empty).
    try:
        _wf_action = str(st.query_params.get("wf_action", "") or "").strip().lower()
        _wf_act_rid_str = str(st.query_params.get("run_id", "") or "").strip()
    except Exception:
        _wf_action = ""
        _wf_act_rid_str = ""
    if _wf_action and _wf_act_rid_str.isdigit() and engine:
        _wf_act_rid = int(_wf_act_rid_str)
        try:
            if _wf_action == "start":
                set_workflow_run_status(_wf_act_rid, "running")
            elif _wf_action == "pause":
                set_workflow_run_status(_wf_act_rid, "paused")
                release_workflow_run_envs(engine, _wf_act_rid)
            elif _wf_action == "resume":
                set_workflow_run_status(_wf_act_rid, "running")
            elif _wf_action == "stop":
                set_workflow_run_status(_wf_act_rid, "stopped")
                release_workflow_run_envs(engine, _wf_act_rid)
                with engine.connect() as conn:
                    conn.execute(text("UPDATE workflow_runs SET finished_at=COALESCE(finished_at,CURRENT_TIMESTAMP) WHERE id=:rid"), {"rid": _wf_act_rid})
                    conn.commit()
            elif _wf_action == "reconcile":
                res = reconcile_workflow_run(_wf_act_rid)
                st.session_state["wf_reconcile_flash"] = (
                    f"Run {_wf_act_rid} reconciled: updated_steps={res.get('updated_steps')} "
                    f"completed_run={res.get('completed_run')}"
                )
            elif _wf_action == "delete":
                _purged = delete_workflow_run_and_purge_scans(engine, _wf_act_rid)
                st.session_state["wf_delete_flash"] = (
                    f"Removed workflow run {_wf_act_rid} and purged {len(_purged)} distinct scan id(s) "
                    "from local DB (scan_insights, scan_progress, scan_parents, auto_scan_benchmarks, manual benchmarks when present)."
                )
        except Exception as _e:
            st.error(f"Action '{_wf_action}' failed for run {_wf_act_rid}: {_e}")
        finally:
            try:
                del st.query_params["wf_action"]
                if _wf_action == "delete":
                    del st.query_params["run_id"]
            except Exception:
                pass
            try:
                st.cache_data.clear()
            except Exception:
                pass
            st.rerun()

    env_df = load_test_environments()
    if env_df.empty:
        st.info("No test environments found. Add them in `Manage Test Environments` first.")
        return

    work_df = env_df.copy()
    work_df["is_active"] = work_df["is_active"].apply(lambda x: bool(x))
    if "include_in_workflow_runs" not in work_df.columns:
        work_df["include_in_workflow_runs"] = True
    else:
        work_df["include_in_workflow_runs"] = (
            pd.to_numeric(work_df["include_in_workflow_runs"], errors="coerce").fillna(1).astype(int).ne(0)
        )
    active_df = work_df[work_df["is_active"] == True].copy()
    active_df = active_df.sort_values(by=["env_name"], ascending=[True]).reset_index(drop=True)
    if active_df.empty:
        st.warning("No active environments. Add or activate one under **Manage Test Environments**.")
        return
    wf_pool_df = active_df[active_df["include_in_workflow_runs"] == True].copy().reset_index(drop=True)

    try:
        with engine.connect() as conn:
            used_rows = conn.execute(
                text("""
                    SELECT DISTINCT ws.environment_id
                    FROM workflow_steps ws
                    JOIN workflow_runs wr ON wr.id = ws.run_id
                    WHERE LOWER(COALESCE(wr.status,'')) = 'running'
                      AND ws.environment_id IS NOT NULL
                    UNION
                    SELECT id FROM test_environments WHERE is_busy = 1
                """)
            ).fetchall()
        in_use_env_ids = {int(r[0]) for r in used_rows if r[0] is not None}
    except Exception:
        in_use_env_ids = set()

    st.divider()
    render_workflow_creation_section(wf_pool_df, in_use_env_ids)

    # --- Load runs ---
    st.markdown("### Workflow Runs")
    st.caption(
        "Click a run id to expand details. Row **🔁** = quick reconcile from DB. **Reset** options live under "
        "**Controls — Run …** below the table (scroll down). **Delete** (🗑️) purges local DB rows for that run’s "
        "`scan_id`s — not BigID."
    )
    runs_df = load_workflow_runs(limit=50)
    if runs_df.empty:
        st.info("No workflow runs yet. Use the **➕ New Run** tab below to create one.")

    if not runs_df.empty:
        try:
            run_id_qp = str(st.query_params.get("run_id", "") or "").strip()
        except Exception:
            run_id_qp = ""
        want = None
        try:
            if str(st.session_state.get("wf_run_sel_id", "")).strip():
                want = int(st.session_state.get("wf_run_sel_id"))
        except Exception:
            pass
        if want is None and run_id_qp.isdigit():
            want = int(run_id_qp)
        # Keep session in sync when opening via URL so expansion and selectbox persist
        if want is not None:
            try:
                st.session_state["wf_run_sel_id"] = int(want)
            except Exception:
                pass

        # --- Progress flow helper ---
        def _pf(row_dict):
            s = _workflow_norm_step_status(row_dict.get("status"))
            if s == "disabled": return "🚫 Disabled"
            ds_id = str(row_dict.get("ds_id") or "").strip()
            spid = str(row_dict.get("scan_profile_id") or "").strip()
            sid = str(row_dict.get("scan_id") or "").strip()
            
            # Terminal states from workflow_runner.TERMINAL_STATES
            terminals = {"failed", "completedwithfailures", "completed", "stopped", "aborted", "cancelled", "canceled"}
            
            # has_scan is true if we have a scan_id OR the workflow step itself is marked completed/running.
            has_scan = bool(sid) or (s in {"scan_running", "completed"})
            has_profile = bool(spid) or has_scan or (s in {"scan_created", "scan_running", "completed"})
            has_test = s in {"ds_test_ok", "scan_created", "scan_running", "completed"}
            has_ds = bool(ds_id) or (s in {"ds_created", "ds_test_ok", "scan_created", "scan_running", "completed"}) or has_profile or has_scan
            
            if s in {"pending", ""}: return "➖ DS"
            if s in {"running", "pre_action_done"}: return "⏳ DS"
            
            if s == "error":
                # Detailed error flow for better visibility
                if sid: return f"✅DS ➔ ✅Test ➔ ✅Profile ➔ ⏳Scan ⚠️"
                err_text = str(row_dict.get("last_error") or "").lower()
                
                # Check based on last_error message content combined with known IDs
                if "scan start failed" in err_text or has_profile:
                    return "✅DS ➔ ✅Test ➔ ✅Profile ❌Scan"
                if "scan profile" in err_text or "template" in err_text:
                    return "✅DS ➔ ✅Test ❌Profile"
                if "connection test failed" in err_text:
                    return "✅DS ❌Test"
                if "ds create failed" in err_text or not has_ds:
                    return "❌ DS"
                
                # Fallback matching original logic but more cautious
                if has_ds:
                    # If we have a DS but errored, and didn't hit any subsequent phase strings
                    return "✅DS ❌Test"
                return "❌ Error"
                
            parts = []
            if has_ds: parts.append("✅DS")
            if has_test: parts.append("✅Test")
            if has_profile: parts.append("✅Profile")
            if has_scan:
                # Be VERY explicit: Only ✅ if status is 'completed'. Everything else is ⏳.
                if s == "completed":
                    parts.append("✅Scan")
                else:
                    parts.append("⏳Scan")
            
            return " ➔ ".join(parts) if parts else "➖ DS"

        # --- Pre-load step data first (derive env busy from steps + te.is_busy; API sync lags te.is_busy alone) ---
        all_run_steps = {}
        run_ids_list = runs_df["id"].tolist()
        batch_steps_df = load_workflow_steps_batch(run_ids_list)
        derived_busy_env_ids: set[int] = set()
        if not batch_steps_df.empty:
            for _, _sr in batch_steps_df.iterrows():
                _rd = _sr.to_dict()
                if not _workflow_step_implies_env_busy(_rd):
                    continue
                _eid = _rd.get("environment_id")
                if _eid is None:
                    continue
                try:
                    if isinstance(_eid, float) and pd.isna(_eid):
                        continue
                except Exception:
                    pass
                try:
                    derived_busy_env_ids.add(int(_eid))
                except Exception:
                    pass

        env_status_parts = []
        try:
            with engine.connect() as conn:
                env_busy_rows = conn.execute(text(
                    "SELECT id, env_name, is_busy FROM test_environments WHERE is_active = 1 ORDER BY env_name"
                )).fetchall()
            for er in env_busy_rows or []:
                name = str(er[1] or "").strip() or f"id={er[0]}"
                db_flag = bool(er[2]) if er[2] is not None else False
                try:
                    eid_int = int(er[0])
                except Exception:
                    eid_int = None
                busy = db_flag or (eid_int is not None and eid_int in derived_busy_env_ids)
                env_status_parts.append(f"{'🔴' if busy else '🟢'} {html.escape(name)} ({'busy' if busy else 'free'})")
        except Exception:
            pass
        env_status_line = " &nbsp; ".join(env_status_parts) if env_status_parts else ""

        for rid in run_ids_list:
            try:
                sdf = batch_steps_df[batch_steps_df["run_id"] == rid]
                steps_to_show = []
                # Initialize counts for the summary bar
                completed = 0
                errors = 0
                disabled = 0
                in_prog = 0
                total_steps = len(sdf)

                for _, sr in sdf.iterrows():
                    rd = sr.to_dict()
                    c_status = _workflow_norm_step_status(rd.get("status"))
                    last_err = str(rd.get("last_error") or "").strip()
                    is_en = bool(rd.get("is_enabled", True)) if rd.get("is_enabled") is not None else True
                    try:
                        if rd.get("is_enabled") is not None and not bool(int(rd.get("is_enabled"))):
                            is_en = False
                    except Exception:
                        pass

                    # Counts from DB status (emoji progress strings miss phases like ds_created → false "Pending")
                    if not is_en or c_status == "disabled":
                        disabled += 1
                    elif c_status == "error" or (
                        last_err
                        and c_status not in ("completed", "disabled")
                        and not _workflow_is_transient_step_message(last_err)
                    ):
                        errors += 1
                    elif c_status == "completed":
                        completed += 1
                    elif c_status in {"", "pending"}:
                        pass  # included in metrics.pending below (residual)
                    else:
                        in_prog += 1

                    prog = _pf(rd)

                    # Visibility filter: "no need to show disable steps"
                    if c_status == "disabled": continue
                    if not bool(rd.get("is_enabled", True)): continue

                    flags = ", ".join([x for x in [
                        "ACL" if bool(rd.get("is_acl")) else "",
                        "STRUCT" if bool(rd.get("is_structured_ff")) else "",
                        "OLS" if bool(rd.get("is_ols")) else "",
                    ] if x])
                    scan_link = ""
                    sid_val = str(rd.get("scan_id") or "").strip()
                    if sid_val:
                        scan_link = f'<a href="?page=Scan+Progress&scan_id={html.escape(sid_val)}" target="_blank" rel="noopener noreferrer" style="color:{PRIMARY_COLOR};text-decoration:none;font-size:0.8rem;">View Progress</a>'
                    # Env with busy/free indicator: 🟢 free, 🔴 busy (derive from step + te.is_busy; API/te.is_busy can lag)
                    env_name = str(rd.get("environment") or "").strip()
                    row_env_busy = _workflow_step_implies_env_busy(rd)
                    if env_name:
                        env_display = ("🔴 " if row_env_busy else "🟢 ") + env_name
                    else:
                        env_display = "—"
                    display_err = str(rd.get("last_error") or "").strip()
                    if c_status in {"scan_waiting", "scan_running", "scan_created", "ds_created", "ds_test_ok"} and _workflow_is_transient_step_message(display_err):
                        display_err = ""
                    steps_to_show.append({
                        "seq": rd.get("seq", ""),
                        "env": env_display,
                        "ds_type": str(rd.get("ds_kind") or "").strip(),
                        "ds_name": str(rd.get("ds_name") or "").strip(),
                        "scan_type": str(rd.get("scan_kind") or "").strip(),
                        "file_set": str(rd.get("file_set_type") or "").strip(),
                        "flags": flags,
                        "progress": prog,
                        "scan_id": sid_val,
                        "scan_link": scan_link,
                        "notes": str(rd.get("notes") or "").strip(),
                        "last_error": display_err,
                    })
                all_run_steps[int(rid)] = {
                    "rows": steps_to_show,
                    "metrics": {
                        "completed": completed,
                        "errors": errors,
                        "disabled": disabled,
                        "in_prog": in_prog,
                        "pending": total_steps - completed - errors - disabled - in_prog
                    }
                }
            except Exception:
                all_run_steps[int(rid)] = {"rows": [], "metrics": {"completed":0,"errors":0,"disabled":0,"in_prog":0,"pending":0}}

        # --- Status badge ---
        def _badge(status_val):
            s = str(status_val or "").strip().lower()
            colors = {
                "running": ("#dcfce7", "#16a34a"), "completed": ("#dbeafe", "#2563eb"),
                "created": ("#f3f4f6", "#6b7280"), "paused": ("#fef9c3", "#ca8a04"),
                "stopped": ("#fee2e2", "#dc2626"), "failed": ("#fee2e2", "#dc2626"),
            }
            bg, fg = colors.get(s, ("#f3f4f6", "#6b7280"))
            return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:10px;font-size:0.78rem;font-weight:600;">{html.escape(str(status_val or ""))}</span>'

        # --- Build HTML ---
        default_expanded = want  # None = no row expanded (compact view); otherwise expand that run
        rows_html = ""
        for _, rr in runs_df.iterrows():
            rid = int(rr["id"])
            is_exp = (default_expanded is not None and rid == default_expanded)
            arrow = "▾" if is_exp else "▸"
            d_disp = "table-row" if is_exp else "none"
            bg = "background:#f0ecff;" if is_exp else ""
            rstatus = str(rr.get("status") or "").strip().lower()

            # ---- Action icon buttons (context-sensitive) ----
            btn_style = "background:none;border:none;cursor:pointer;font-size:1.1rem;padding:2px 4px;border-radius:4px;line-height:1;text-decoration:none;display:inline-block;"
            disabled_style = "background:none;border:none;font-size:1.1rem;padding:2px 4px;opacity:0.3;cursor:default;display:inline-block;"
            page_base = "?page=Workflow+Automation"

            def _btn(emoji, action, tooltip, enabled):
                tt = html.escape(str(tooltip or ""), quote=True)
                if enabled:
                    # st.html runs in an iframe: navigate the top frame or links appear dead / greyed-out.
                    return (
                        f'<a href="{page_base}&run_id={rid}&wf_action={action}" title="{tt}" style="{btn_style}" '
                        f'onclick="event.stopPropagation();try{{window.top.location.assign(this.href);}}catch(e){{window.location.href=this.href;}}return false;">{emoji}</a>'
                    )
                return f'<span title="{tt}" style="{disabled_style}">{emoji}</span>'

            can_start = rstatus in {"created", "stopped", "paused", "failed"}
            can_pause  = rstatus == "running"
            can_resume = rstatus == "paused"
            can_stop   = rstatus in {"running", "paused"}
            can_delete = rstatus not in {"running"}

            actions_html = (
                _btn("▶️", "start",  "Start",          can_start)  +
                _btn("⏸", "pause",  "Pause",          can_pause)  +
                _btn("↩️", "resume", "Resume",         can_resume) +
                _btn("⏹", "stop",   "Stop",           can_stop)   +
                _btn("🔁", "reconcile", "Reconcile from DB (scan_parents / scan_progress → step status)", True) +
                _btn("🗑️", "delete", "Delete workflow + purge local scan DB rows for its scan ids", can_delete)
            )

            # Format timestamps to avoid "NaT"
            def _fmt_ts(val):
                if pd.isna(val) or val is None or str(val).strip().lower() == "nan":
                    return "—"
                return html.escape(str(val))

            open_run_href = f"{page_base}&run_id={rid}"
            arrow = "▾" if is_exp else "▸"
            rows_html += f"""<tr class="main-row" data-rid="{rid}" style="{bg}">
  <td style="padding:8px 12px;font-weight:700;white-space:nowrap;">
    <a href="{html.escape(open_run_href)}" style="color:{PRIMARY_COLOR};text-decoration:none;cursor:pointer;display:block;" onclick="window.top.location.href=this.getAttribute('href');return false;"><span style="margin-right:5px;">{arrow}</span> Run {rid}</a>
  </td>
  <td style="padding:8px 12px;">{html.escape(str(rr["name"] or ""))}</td>
  <td style="padding:8px 12px;">{_badge(rr["status"])}</td>
  <td style="padding:8px 12px;font-size:0.82rem;">{_fmt_ts(rr.get("started_at"))}</td>
  <td style="padding:8px 12px;font-size:0.82rem;">{_fmt_ts(rr.get("finished_at"))}</td>
  <td style="padding:8px 12px;font-size:0.82rem;color:#dc2626;max-width:280px;word-break:break-word;" title="{html.escape(str(rr.get('last_error') or ''))}">{html.escape(str(rr.get("last_error") or "")[:200])}</td>
  <td style="padding:8px 12px;white-space:nowrap;">{actions_html}</td>
</tr>\n"""

            # Sub-table (detail row)
            steps_obj = all_run_steps.get(rid, {})
            if isinstance(steps_obj, dict) and steps_obj.get("rows"):
                s_rows = ""
                for i, sr in enumerate(steps_obj["rows"]):
                    row_bg = "#f9f8ff" if i % 2 == 0 else "#ffffff"
                    # Hide transient "no free env this tick" message (cleared by runner when env is assigned)
                    err_val = (sr["last_error"] or "").strip()
                    if err_val and "No free env this tick" in err_val:
                        err_val = ""
                    _err_full = str(sr.get("last_error") or "")
                    err_cell = (
                        f'<span style="color:#dc2626;font-size:0.78rem;" title="{html.escape(_err_full)}">{html.escape(err_val[:40])}</span>'
                        if err_val
                        else ""
                    )
                    s_rows += f"""<tr style="background:{row_bg};">
<td style="padding:4px 8px;text-align:center;font-weight:600;">{sr["seq"]}</td>
<td style="padding:4px 8px;">{html.escape(sr["env"])}</td>
<td style="padding:4px 8px;">{html.escape(sr["ds_type"])}</td>
<td style="padding:4px 8px;">{html.escape(sr["ds_name"])}</td>
<td style="padding:4px 8px;">{html.escape(sr["scan_type"])}</td>
<td style="padding:4px 8px;">{html.escape(sr["file_set"])}</td>
<td style="padding:4px 8px;font-size:0.82rem;">{html.escape(sr["flags"])}</td>
<td style="padding:4px 8px;">{sr["progress"]}</td>
<td style="padding:4px 8px;font-size:0.82rem;">{html.escape(sr["scan_id"])}</td>
<td style="padding:4px 8px;">{sr["scan_link"]}</td>
<td style="padding:4px 8px;font-size:0.82rem;">{html.escape(sr["notes"])}</td>
<td style="padding:4px 8px;">{err_cell}</td>
</tr>\n"""

                # Summary counts from pre-calculated metrics
                metrics = steps_obj.get("metrics", {})
                pending = metrics.get("pending", 0)
                in_prog = metrics.get("in_prog", 0)
                completed = metrics.get("completed", 0)
                disabled = metrics.get("disabled", 0)
                errors = metrics.get("errors", 0)

                metrics_html = f'''<div style="display:flex;gap:24px;margin-bottom:10px;flex-wrap:wrap;">
      <div><span style="font-size:0.78rem;color:#6b7280;">Pending</span><br><strong style="font-size:1.1rem;">{pending}</strong></div>
      <div><span style="font-size:0.78rem;color:#6b7280;">In Progress</span><br><strong style="font-size:1.1rem;color:#ca8a04;">{in_prog}</strong></div>
      <div><span style="font-size:0.78rem;color:#6b7280;">Completed</span><br><strong style="font-size:1.1rem;color:#16a34a;">{completed}</strong></div>
      <div><span style="font-size:0.78rem;color:#6b7280;">Disabled</span><br><strong style="font-size:1.1rem;color:#6b7280;">{disabled}</strong></div>
      <div><span style="font-size:0.78rem;color:#6b7280;">Errors</span><br><strong style="font-size:1.1rem;color:#dc2626;">{errors}</strong></div>
    </div>'''

                detail_inner = f"""{metrics_html}
<table style="width:100%;border-collapse:collapse;font-size:0.82rem;">
  <thead><tr style="background:#e8e3f5;">
    <th style="padding:5px 8px;text-align:center;">Step</th>
    <th style="padding:5px 8px;">Env</th>
    <th style="padding:5px 8px;">DS Type</th>
    <th style="padding:5px 8px;">DS Name</th>
    <th style="padding:5px 8px;">Scan</th>
    <th style="padding:5px 8px;">File Set</th>
    <th style="padding:5px 8px;">Flags</th>
    <th style="padding:5px 8px;">Progress</th>
    <th style="padding:5px 8px;">Scan Id</th>
    <th style="padding:5px 8px;">Link</th>
    <th style="padding:5px 8px;">Notes</th>
    <th style="padding:5px 8px;">Error</th>
  </tr></thead>
  <tbody>{s_rows}</tbody>
</table>"""
            else:
                detail_inner = '<span style="color:#6b7280;font-style:italic;">No steps found for this run.</span>'

            detail_display = "table-row" if is_exp else "none"
            rows_html += f"""<tr class="detail-row" style="display:{detail_display};">
  <td colspan="7" style="padding:0;border:none;">
    <div style="border-left:4px solid {PRIMARY_COLOR};margin:0 0 0 16px;padding:12px 16px;background:#faf8ff;">
      <div style="font-weight:700;font-size:0.95rem;color:{PRIMARY_COLOR};margin-bottom:8px;">Step Progress — Run {rid}</div>
      {detail_inner}
    </div>
  </td>
</tr>\n"""

        env_status_block = f'<div style="font-size:0.85rem;color:#6b7280;margin-bottom:10px;">Env status: {env_status_line}</div>' if env_status_line else ""
        full_html = f"""
    <style>
      .wf-tbl {{ width:100%; border-collapse:collapse; font-family:'Inter','Segoe UI',sans-serif; margin-bottom:20px; }}
      .wf-tbl thead th {{ background:{PRIMARY_COLOR}; color:#fff; padding:10px 12px; font-size:0.82rem; font-weight:600; text-align:left; }}
      .main-row:hover {{ background:#f0ecff !important; }}
      .main-row td {{ border-bottom:1px solid #e2e8f0; }}
    </style>
    <div class="wf-container">
      {env_status_block}
      <table class="wf-tbl">
        <thead><tr>
          <th style="min-width:90px;">Run</th><th>Name</th><th>Status</th><th>Started</th><th>Finished</th><th>Last Error</th><th>Actions</th>
        </tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    """
        try:
            st.html(full_html)
        except Exception:
            st.markdown(full_html, unsafe_allow_html=True)

        # --- Controls section (for the selected/expanded run) ---
        # Build label list for selectbox (newest first = same order as table)
        run_options = []
        for _, rr in runs_df.iterrows():
            rid_i = int(rr["id"])
            rstatus = str(rr.get("status") or "").strip()
            rname   = str(rr.get("name") or "").strip()[:40]
            run_options.append(f"Run {rid_i} — {rname} [{rstatus}]")

        # Determine default index from URL / session state
        preferred_run_id = default_expanded if default_expanded is not None else None
        sel_default_idx = 0
        if preferred_run_id is not None:
            try:
                all_ids = [int(runs_df.iloc[i]["id"]) for i in range(len(runs_df))]
                if preferred_run_id in all_ids:
                    sel_default_idx = all_ids.index(preferred_run_id)
            except Exception:
                pass

        sel_label = st.selectbox(
            "🎛️ Select run to control",
            run_options,
            index=sel_default_idx,
            key="wf_ctrl_run_sel",
        )
        run_id = int(runs_df.iloc[run_options.index(sel_label)]["id"])
        st.session_state["wf_run_sel_id"] = run_id

        try:
            rrow = runs_df[runs_df["id"] == run_id].iloc[0].to_dict()
        except Exception:
            rrow = {}
        selected_status = str(rrow.get("status") or "").strip().lower()
        selected_is_running = selected_status == "running"

        # Check for other running workflows
        try:
            tmp = runs_df.copy()
            tmp["status_norm"] = tmp["status"].astype(str).str.strip().str.lower()
            running_rows = tmp[tmp["status_norm"] == "running"].copy()
            any_running = not running_rows.empty
            running_ids = sorted({int(x) for x in running_rows["id"].tolist() if str(x).strip().isdigit()})
        except Exception:
            any_running = False
            running_ids = []
        other_running_ids = [x for x in running_ids if int(x) != int(run_id)]

        st.markdown(f"#### Controls — Run {run_id}")
        st.caption(
            f"status=**{str(rrow.get('status') or '').strip()}**"
            + (f" | next_eligible_at={rrow.get('next_eligible_at')}" if rrow.get("next_eligible_at") else "")
            + (f" | cooldown={rrow.get('cooldown_minutes')}m" if rrow.get("cooldown_minutes") is not None else "")
        )

        if other_running_ids:
            st.warning(f"Note: Workflow(s) {', '.join([str(x) for x in other_running_ids])} are also running. The runner will process all active runs across available environments.")
        elif selected_is_running:
            st.info("This workflow run is currently running.")

        last_err = str(rrow.get("last_error") or "").strip()
        if last_err:
            if selected_status in {"paused", "stopped", "failed"}:
                st.error(last_err)
            else:
                st.caption(f"Last failure (history): {last_err}")

        b1, b2, b3, b4, b5, b6 = st.columns([1.0, 1.0, 1.0, 1.0, 1.3, 1.0])
        # Allow starting if current run is not already running.
        # The runner will handle global concurrency.
        start_disabled = selected_is_running
        pause_disabled = (selected_status != "running")
        resume_disabled = (selected_status != "paused")
        stop_disabled = (selected_status not in {"running", "paused"})

        chosen_e1_id = None
        chosen_e2_id = None
        if selected_status in {"created", "stopped", "paused", "failed"}:
            st.markdown("##### 🚀 Start / Resume Environment Mapping")
            st.caption(
                "Select environments in the **workflow pool** (Use for workflow runs). "
                "Steps alternate between Env 1 and Env 2."
            )
            c_env1, c_env2 = st.columns(2)

            env_labels_ctrl: List[str] = []
            env_ids_ctrl: List[int] = []
            if wf_pool_df.empty:
                st.warning(
                    "No environments in the workflow pool. Enable **Use for workflow runs** under "
                    "**Manage Test Environments**, then map envs here before starting."
                )
            else:
                env_labels_ctrl = [_env_label(r) for r in wf_pool_df.itertuples()]
                env_ids_ctrl = [int(r.id) for r in wf_pool_df.itertuples()]

            if env_ids_ctrl:
                prev_e1 = _wf_env_int(rrow.get("env1_id"), env_ids_ctrl[0])
                prev_e2 = _wf_env_int(
                    rrow.get("env2_id"),
                    env_ids_ctrl[1] if len(env_ids_ctrl) > 1 else prev_e1,
                )

                idx1 = env_ids_ctrl.index(prev_e1) if prev_e1 in env_ids_ctrl else 0
                idx2 = env_ids_ctrl.index(prev_e2) if prev_e2 in env_ids_ctrl else (1 if len(env_ids_ctrl) > 1 else 0)

                chosen_env1_lbl = c_env1.selectbox(
                    "Env 1 (Primary)", env_labels_ctrl, index=idx1, key=f"start_e1_{run_id}"
                )
                chosen_env2_lbl = c_env2.selectbox(
                    "Env 2 (Secondary)", env_labels_ctrl, index=idx2, key=f"start_e2_{run_id}"
                )
                chosen_e1_id = env_ids_ctrl[env_labels_ctrl.index(chosen_env1_lbl)]
                chosen_e2_id = env_ids_ctrl[env_labels_ctrl.index(chosen_env2_lbl)]
            st.markdown("<br>", unsafe_allow_html=True)

        b1, b2, b3, b4, b5, b6 = st.columns([1.0, 1.0, 1.0, 1.0, 1.3, 1.0])

        def _apply_run_envs(r_id, e1_id, e2_id):
            if e1_id is None or e2_id is None: return
            with engine.connect() as conn:
                conn.execute(text("UPDATE workflow_runs SET env1_id=:e1, env2_id=:e2 WHERE id=:rid"), {"e1": e1_id, "e2": e2_id, "rid": r_id})
                # Lock environments only for steps that are not already running/completed etc
                conn.execute(text("""
                    UPDATE workflow_steps 
                    SET environment_id = CASE WHEN (seq % 2) = 1 THEN :e1 ELSE :e2 END
                    WHERE run_id=:rid AND COALESCE(status,'') NOT IN ('scan_running', 'completed', 'disabled')
                """), {"e1": e1_id, "e2": e2_id, "rid": r_id})
                conn.commit()

        if b1.button("Start Workflow", type="primary", disabled=start_disabled):
            try:
                _apply_run_envs(int(run_id), chosen_e1_id, chosen_e2_id)
                set_workflow_run_status(int(run_id), "running")
                try:
                    if callable(ensure_workflow_runner_running): ensure_workflow_runner_running(engine)
                except Exception: pass
                st.rerun()
            except Exception as ex:
                st.error(f"Start failed: {ex}")
        if b2.button("Pause", disabled=pause_disabled):
            set_workflow_run_status(int(run_id), "paused")
            release_workflow_run_envs(engine, int(run_id))
            st.rerun()
        if b3.button("Resume Workflow", disabled=resume_disabled):
            try:
                _apply_run_envs(int(run_id), chosen_e1_id, chosen_e2_id)
                set_workflow_run_status(int(run_id), "running")
                try:
                    if callable(ensure_workflow_runner_running): ensure_workflow_runner_running(engine)
                except Exception: pass
                st.rerun()
            except Exception as ex:
                st.error(f"Resume failed: {ex}")
        if b6.button("Stop Workflow", disabled=stop_disabled):
            set_workflow_run_status(int(run_id), "stopped")
            release_workflow_run_envs(engine, int(run_id))
            try:
                with engine.connect() as conn:
                    conn.execute(text("UPDATE workflow_runs SET finished_at=COALESCE(finished_at,CURRENT_TIMESTAMP) WHERE id=:rid"), {"rid": int(run_id)})
                    conn.commit()
            except Exception: pass
            st.rerun()
        if b4.button("Refresh"):
            try: st.cache_data.clear()
            except Exception: pass
            st.rerun()
        if b5.button("Reconcile from DB"):
            res = reconcile_workflow_run(int(run_id))
            st.caption(f"Reconciled: updated_steps={res.get('updated_steps')} completed_run={res.get('completed_run')}")
            try: st.cache_data.clear()
            except Exception: pass
            st.rerun()

        # --- Delete Workflow ---
        st.divider()
        delete_disabled = selected_status == "running"
        with st.expander("🗑️ Delete Workflow Run", expanded=False):
            if delete_disabled:
                st.warning("Stop the workflow before deleting it.")
            else:
                st.error(
                    f"**Danger!** Deletes Run **{run_id}** and all its steps, and **purges local DB data** for every "
                    "`scan_id` recorded on those steps: `scan_insights`, `scan_progress`, `scan_parents`, "
                    "`auto_scan_benchmarks`, attribute/delta maps, and **manual** `benchmarks` rows linked by `scan_id`. "
                    "If the same scan id also appears on another workflow run, those rows are removed once — the other run "
                    "would still exist but lose matching local history. Data sources and scans in **BigID are NOT deleted**."
                )
                if st.button("⚠️ Confirm Delete Run", key=f"wf_delete_confirm_{run_id}", type="primary"):
                    try:
                        purged = delete_workflow_run_and_purge_scans(engine, int(run_id))
                        try:
                            st.session_state.pop("wf_run_sel_id", None)
                        except Exception:
                            pass
                        try:
                            st.cache_data.clear()
                        except Exception:
                            pass
                        st.success(
                            f"Run {run_id} deleted; purged {len(purged)} distinct scan id(s) from local tables."
                        )
                        st.rerun()
                    except Exception as ex:
                        st.error(f"Delete failed: {ex}")

        # Reset
        st.markdown("**Reset** — Clears step progress in DB only. Does NOT delete DS or Scans in BigID.")
        br1, br2 = st.columns(2)
        with br1:
            if st.button("Reset unfinished steps", key="wf_reset_soft"):
                st.session_state["wf_confirm_reset"] = "soft"
                st.rerun()
        with br2:
            if st.button("Full reset (re-test DS connection)", key="wf_reset_full"):
                st.session_state["wf_confirm_reset"] = "full"
                st.rerun()

        if st.session_state.get("wf_confirm_reset") in ("soft", "full"):
            kind = st.session_state["wf_confirm_reset"]
            running_scan_steps = []
            try:
                with engine.connect() as conn:
                    rows = conn.execute(text("SELECT id, seq, status, scan_id, ds_name FROM workflow_steps WHERE run_id=:rid AND COALESCE(status,'') IN ('scan_running','scan_waiting') AND TRIM(COALESCE(scan_id,''))<>''"), {"rid": int(run_id)}).fetchall()
                    running_scan_steps = [{"seq": r[1], "scan_id": r[3], "ds_name": r[4]} for r in (rows or [])]
            except Exception: pass
            if running_scan_steps:
                step_str = ", ".join(f"Step {s['seq']} ({s['ds_name'] or s['scan_id']})" for s in running_scan_steps)
                st.error(f"**Cannot reset:** steps with running scan: {step_str}")
                if st.button("Cancel", key="wf_reset_cancel"):
                    del st.session_state["wf_confirm_reset"]; st.rerun()
            else:
                st.warning("Confirm: clears step progress in dashboard DB only. DS/Scans in BigID NOT deleted.")
                rc1, rc2 = st.columns(2)
                with rc1:
                    if st.button("Confirm reset", key="wf_reset_confirm"):
                        try:
                            with engine.connect() as conn:
                                if kind == "full":
                                    conn.execute(text("UPDATE workflow_steps SET status='pending', last_error=NULL, started_at=NULL, finished_at=NULL, scan_id=NULL, ds_id=NULL, scan_profile_id=NULL, scan_profile_name=NULL, updated_at=CURRENT_TIMESTAMP WHERE run_id=:rid AND COALESCE(status,'') NOT IN ('completed','disabled')"), {"rid": int(run_id)})
                                else:
                                    conn.execute(text("UPDATE workflow_steps SET status='pending', last_error=NULL, started_at=NULL, finished_at=NULL, scan_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE run_id=:rid AND COALESCE(status,'') NOT IN ('completed','disabled')"), {"rid": int(run_id)})
                                conn.execute(text("UPDATE workflow_runs SET last_error=NULL, next_eligible_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=:rid"), {"rid": int(run_id)})
                                conn.commit()
                            del st.session_state["wf_confirm_reset"]
                            try: st.cache_data.clear()
                            except Exception: pass
                            st.success("Reset done."); st.rerun()
                        except Exception as ex:
                            st.error(str(ex))
                with rc2:
                    if st.button("Cancel", key="wf_reset_cancel2"):
                        del st.session_state["wf_confirm_reset"]; st.rerun()

        # Cooldown / runner status
        try:
            ne = rrow.get("next_eligible_at"); now_utc = datetime.utcnow(); ne_dt = None
            if isinstance(ne, datetime): ne_dt = ne
            elif isinstance(ne, str) and ne.strip(): ne_dt = _parse_utc_dt(ne.strip())
            if selected_is_running and ne_dt and ne_dt > now_utc:
                remaining_m = int(max(0, (ne_dt - now_utc).total_seconds())) // 60
                st.info(f"Cooldown active. Next step eligible in ~{remaining_m} min (at {ne_dt} UTC).")
        except Exception: pass

        if selected_is_running:
            try:
                if callable(ensure_workflow_runner_running):
                    ok, msg = ensure_workflow_runner_running(engine)
                    st.caption(f"Runner: {msg}")
            except Exception: pass
            try:
                with engine.connect() as conn:
                    row = conn.execute(text("SELECT last_started_at_utc, last_finished_at_utc, last_status, last_message FROM tracker_heartbeat WHERE name='workflow_runner' LIMIT 1")).fetchone()
                if row: st.caption(f"Runner heartbeat: status={row[2]} started={row[0]} finished={row[1]} msg={row[3]}")
            except Exception: pass

        # Edit steps
        render_workflow_step_editor(run_id, selected_status)

        # Debug templates
        if st.query_params.get("debug") == "true" and not steps_df.empty:
            st.divider()
            st.markdown("### Templates (Debug)")
            try: step_nums = sorted({int(x) for x in steps_df["seq"].tolist() if str(x).strip()})
            except Exception: step_nums = list(range(1, 17))
            pick_step = st.selectbox("Select step to edit templates", step_nums, index=0, key="wf_tpl_step")
            try: step_row = steps_df[steps_df["seq"] == int(pick_step)].iloc[0].to_dict()
            except Exception: step_row = {}
            ds_key = _workflow_ds_template_key(step_row or {})
            scan_key = _workflow_scan_template_key(step_row or {})
            t1, t2, t3 = st.columns(3)
            with t1:
                st.markdown("**DS template**"); st.code(ds_key)
                ds_existing = get_workflow_template_payload(ds_key)
                ds_text = st.text_area("DS JSON", value=ds_existing or "{}", height=220, key="wf_ds_json")
                if st.button("Save DS template", key="wf_save_ds"):
                    upsert_workflow_template(ds_key, "ds", ds_text); st.success("Saved.")
            with t2:
                st.markdown("**Scan template**"); st.code(scan_key)
                scan_existing = get_workflow_template_payload(scan_key)
                scan_text = st.text_area("Scan JSON", value=scan_existing or "{}", height=220, key="wf_scan_json")
                if st.button("Save Scan template", key="wf_save_scan"):
                    upsert_workflow_template(scan_key, "scan", scan_text); st.success("Saved.")
            with t3:
                st.markdown("**Pre-OLS action (FF change)**")
                action_key_global = "action::pre_ols"
                action_key_env = f"action::pre_ols::env={int(step_row.get('environment_id'))}" if step_row.get("environment_id") is not None else ""
                existing = get_workflow_template_payload(action_key_env) if action_key_env else ""
                if not existing: existing = get_workflow_template_payload(action_key_global)
                action_text = st.text_area("Action JSON", value=existing or "{}", height=220, key="wf_action_json")
                if st.button("Save Pre-OLS action", key="wf_save_action"):
                    upsert_workflow_template(action_key_env or action_key_global, "action", action_text); st.success("Saved.")


def page_create_data_source():
    """
    Wizard-like tool:
    1) Get scanner groups
    2) Create/update DS connection
    3) Test connection
    """
    st.title("🧩 Create Data Source + Test Connection")
    st.caption("Creates/updates a BigID DS connection and runs the Test Connection API.")

    env_df = load_test_environments()
    if env_df.empty:
        st.info("No test environments found. Add one in `Manage Test Environments` first.")
        return

    work_df = env_df.copy()
    work_df["is_active"] = work_df["is_active"].apply(lambda x: bool(x))
    work_df = work_df.sort_values(by=["is_active", "env_name"], ascending=[False, True]).reset_index(drop=True)
    labels = [
        f"{row.env_name} ({'active' if row.is_active else 'inactive'}) - {row.base_url or 'no-base-url'}"
        for row in work_df.itertuples()
    ]
    selected_label = st.selectbox("Environment", labels, index=0)
    selected_env = work_df.iloc[labels.index(selected_label)].to_dict()

    ensure_workflow_tables()
    st.markdown("### Environment Info")
    base_url = str(selected_env.get("base_url") or "").strip()
    env_id_val = selected_env.get("id")
    env_name_val = str(selected_env.get("env_name") or "").strip()
    current_version = str(selected_env.get("version") or "").strip()

    def _build_prefix_from_version(v: str) -> str:
        s = str(v or "").strip()
        if not s:
            return ""
        # 248.1 -> 248-1
        if "." in s:
            parts = s.split(".")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                return f"{parts[0]}-{parts[1]}"
        if s.lower().startswith("release-"):
            s = s[len("release-"):].strip()
        return s.replace(".", "-")

    b1, b2, b3 = st.columns([1.0, 1.0, 1.4])
    if b1.button("Fetch build (/about)", type="primary"):
        try:
            tok = fetch_auth_token_for_env(selected_env)
        except Exception:
            tok = ""
        try:
            clean_version, raw_version, about_url = fetch_core_version_from_api(base_url, tok, INTERNAL_ABOUT_API_PATH)
            if clean_version:
                try:
                    record_environment_version(env_id_val, env_name_val, clean_version)
                except Exception:
                    pass
                selected_env["version"] = clean_version
                st.session_state["create_ds_env_version"] = clean_version
                st.success(f"Build version: `{clean_version}`")
                st.caption(f"API: `{about_url}`")
            else:
                st.warning("Could not extract releaseVersion from /about response.")
        except Exception as ex:
            st.error(f"Failed to fetch /about: {ex}")

    effective_version = str(st.session_state.get("create_ds_env_version") or current_version or "").strip()
    b2.metric("Build version", effective_version or "—")
    b3.metric("Build prefix", _build_prefix_from_version(effective_version) or "—")

    st.markdown("### Automation Defaults")
    st.caption("Saved to DB (used by workflow runner).")
    key_env = f"config::scan_template_id::env={int(env_id_val)}" if env_id_val is not None else ""
    key_global = "config::scan_template_id"
    # Automatic: try to populate env scanTemplateId from API (Data source scan).
    try:
        auto_id = ensure_env_default_scan_template_id(selected_env)
    except Exception:
        auto_id = ""
    existing_id = (get_workflow_template_payload(key_env) if key_env else "") or get_workflow_template_payload(key_global) or ""
    if (not existing_id) and auto_id:
        existing_id = auto_id
    c1, c2, c3 = st.columns([1.6, 1.0, 1.0])
    scan_template_id = c1.text_input("Default Scan Template ID", value=str(existing_id or "").strip(), key="create_ds_scan_template_id")
    if c2.button("Save (this env)"):
        if not key_env:
            st.error("Missing environment id.")
        elif not str(scan_template_id or "").strip():
            st.error("Scan Template ID is required.")
        else:
            upsert_workflow_template(key_env, "config", str(scan_template_id).strip())
            st.success("Saved env scanTemplateId.")
    if c3.button("Save (global)"):
        if not str(scan_template_id or "").strip():
            st.error("Scan Template ID is required.")
        else:
            upsert_workflow_template(key_global, "config", str(scan_template_id).strip())
            st.success("Saved global scanTemplateId.")

    # Default scanner group for automation
    st.markdown("**Default scanner group (automation)**")
    sg_key_env = f"config::scanner_group::env={int(env_id_val)}" if env_id_val is not None else ""
    sg_key_global = "config::scanner_group"
    sg_existing = (get_workflow_template_payload(sg_key_env) if sg_key_env else "") or get_workflow_template_payload(sg_key_global) or ""
    try:
        groups_cached = st.session_state.get("scanner_groups_last") or []
        group_names_cached = []
        for g in groups_cached:
            if isinstance(g, dict):
                nm = str(g.get("name") or g.get("scanner_group") or g.get("id") or g.get("_id") or "").strip()
                if nm:
                    group_names_cached.append(nm)
        group_names_cached = sorted(set(group_names_cached))
    except Exception:
        group_names_cached = []
    sg_options = group_names_cached or ["default"]
    default_idx = 0
    try:
        if str(sg_existing).strip() and str(sg_existing).strip() in sg_options:
            default_idx = sg_options.index(str(sg_existing).strip())
    except Exception:
        default_idx = 0
    sg_val = st.selectbox("Scanner group (default for runner)", sg_options, index=default_idx, key="create_ds_default_scanner_group")
    s1, s2 = st.columns([1.0, 1.0])
    if s1.button("Save scanner group (this env)"):
        if not sg_key_env:
            st.error("Missing environment id.")
        else:
            upsert_workflow_template(sg_key_env, "config", str(sg_val).strip())
            st.success("Saved env scanner group.")
    if s2.button("Save scanner group (global)"):
        upsert_workflow_template(sg_key_global, "config", str(sg_val).strip())
        st.success("Saved global scanner group.")

    st.markdown("**Scan template id (automatic)**")
    st.caption("Auto-detected from Scan Templates API by picking template name `Data source scan`.")
    if st.query_params.get("debug") == "true":
        if st.button("Debug: show templates list"):
            with st.spinner("Fetching scan templates..."):
                try:
                    items, _total, url = fetch_scan_templates_with_auth_retry(selected_env, limit=200, skip=0)
                    rows = []
                    for it in items or []:
                        if isinstance(it, dict):
                            rows.append({
                                "name": str(it.get("name") or "").strip(),
                                "_id": str(it.get("_id") or it.get("id") or "").strip(),
                                "updated_at": it.get("updated_at") or it.get("updatedAt") or it.get("updated") or "",
                            })
                    st.caption(f"API: `{url}`")
                    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
                except Exception as ex:
                    st.error(f"Failed: {ex}")

    st.divider()
    st.markdown("**Step 1: Fetch scanner groups**")
    if st.button("Get scanner groups", type="primary"):
        with st.spinner("Fetching scanner groups..."):
            try:
                items, _total, url = fetch_scanner_groups_with_auth_retry(selected_env)
                st.session_state["scanner_groups_last"] = items or []
                st.session_state["scanner_groups_last_url"] = url
                st.success(f"Loaded {len(items or [])} scanner groups.")
            except Exception as ex:
                st.error(f"Failed to fetch scanner groups: {ex}")

    groups = st.session_state.get("scanner_groups_last") or []
    group_names = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        nm = str(g.get("name") or g.get("scanner_group") or g.get("id") or g.get("_id") or "").strip()
        if nm:
            group_names.append(nm)
    group_names = sorted(set(group_names)) or ["default"]
    scanner_group = st.selectbox("Scanner group", group_names, index=0)

    st.divider()
    st.markdown("**Step 2: Create/Update DS connection**")
    with st.form("create_ds_form"):
        ds_name = st.text_input("Data source name", value="")
        ds_type = st.selectbox("DS type", ["smb_v2", "smb", "s3", "nfs", "sharepoint"], index=0)
        resource_address = st.text_input("Resource address / host", value="")
        auth_username = st.text_input("Username", value="")
        auth_password = st.text_input("Password (plain)", value="", type="password")
        auth_password_enc = st.text_input("Password Enc (optional)", value="", type="password")

        st.caption("SMB filters (used when DS type is smb_v2)")
        container_name = st.text_input("Container / share name (e.g. millions_files)", value="")
        sub_path = st.text_input("Sub-path (e.g. benchmark_test_data/...)", value="")

        parsing_threads = st.number_input("Number of parsing threads", min_value=1, max_value=200, value=10, step=1)
        enable_lite_mode = st.checkbox("Enable lite mode", value=True)
        submit = st.form_submit_button("Create/Update DS")

    if submit:
        if not ds_name.strip():
            st.error("Data source name is required.")
        elif not resource_address.strip():
            st.error("Resource address is required.")
        else:
            # Build ds_connection payload (minimal, aligned to the cURL shape you shared).
            auth_props = {
                "@authenticationType": "basicAuthentication",
                "username": str(auth_username or "").strip(),
                "password_enc": (str(auth_password_enc or "").strip() or None),
            }
            if str(auth_password or "").strip():
                # Some environments accept plain password; keep as best-effort (never displayed).
                auth_props["password"] = str(auth_password)

            ds_conn = {
                "name": str(ds_name).strip(),
                "enabled": "yes",
                "tags": [],
                "type": str(ds_type).strip(),
                "resourceProperties": {"resourceAddress": str(resource_address).strip()},
                "authStrategy": "basicAuthentication",
                "authenticationProperties": auth_props,
                "stopOnEnumerationFailure": False,
                "scanner_group": str(scanner_group).strip() or "default",
                "numberOfParsingThreads": str(int(parsing_threads)),
                "classification_is_enabled": True,
                "enableLiteMode": bool(enable_lite_mode),
            }

            # SMB-specific knobs (safe no-ops for other DS types)
            if str(ds_type).strip() == "smb_v2":
                ds_conn.update({
                    "typeRequestProperties": {
                        "@requestType": "unstructureRequestProperties",
                        "enableStructuredFilesDiscovery": False,
                        "maxArchiveSizeInMbToExtract": 100,
                        "extractArchives": False,
                    },
                    "containerizedFilterType": "containerizedSpecificNameFilter",
                    "containerizedFilter": {
                        "@containerizedFilterType": "containerizedSpecificNameFilter",
                        "specificNameFilters": {
                            "containerUseCase": {"useCase": "INCLUDE", "name": str(container_name or "").strip()},
                            "subContainerUseCase": {"name": str(sub_path or "").strip()},
                        },
                    },
                })

            payload = {"ds_connection": ds_conn, "isSavedAsDraft": False}

            with st.spinner("Calling DS create/update API..."):
                try:
                    out, url = put_ds_connection_with_auth_retry(selected_env, ds_name, payload)
                    st.session_state["last_ds_payload"] = payload
                    st.session_state["last_ds_name"] = ds_name
                    st.session_state["last_ds_env"] = selected_env
                    # Try to capture password_enc from response for test connection.
                    try:
                        resp_conn = (out or {}).get("data", {}).get("ds_connection") or (out or {}).get("ds_connection") or {}
                        resp_auth = resp_conn.get("authenticationProperties") if isinstance(resp_conn.get("authenticationProperties"), dict) else {}
                        resp_enc = resp_auth.get("password_enc")
                        if resp_enc and not auth_password_enc:
                            st.session_state["last_ds_password_enc"] = str(resp_enc)
                    except Exception:
                        pass
                    st.success("DS connection saved.")
                    st.caption(f"API: `{url}`")
                    st.json(_redact_secrets(out))
                except Exception as ex:
                    st.error(f"DS create/update failed: {ex}")

    st.divider()
    st.markdown("**Step 3: Test connection**")
    last_ds_name = str(st.session_state.get("last_ds_name") or "").strip()
    if not last_ds_name:
        st.caption("Create/update a DS first to enable Test Connection.")
        return

    from uuid import uuid4
    default_broadcast = f"{last_ds_name}-test-connection-complete-{uuid4()}"
    broadcast_event = st.text_input("broadcastEvent", value=default_broadcast)

    if st.button("Run Test Connection"):
        env_row = st.session_state.get("last_ds_env") or selected_env
        payload = st.session_state.get("last_ds_payload") or {}

        # If we learned password_enc from create response, prefer it for test payload.
        try:
            enc = str(st.session_state.get("last_ds_password_enc") or "").strip()
            if enc:
                payload = dict(payload)
                ds_conn = dict((payload.get("ds_connection") or {}))
                auth = dict((ds_conn.get("authenticationProperties") or {}))
                auth["password_enc"] = enc
                # Do not send plain password on test call
                auth.pop("password", None)
                ds_conn["authenticationProperties"] = auth
                payload["ds_connection"] = ds_conn
        except Exception:
            pass

        # BigID often expects top-level broadcastEvent too.
        try:
            payload = dict(payload)
            payload["broadcastEvent"] = str(broadcast_event).strip()
            payload.setdefault("isNewSessionToken", False)
            payload.setdefault("isFirstAttempt", False)
        except Exception:
            pass

        with st.spinner("Calling test connection API..."):
            try:
                out, url = post_ds_connection_test_with_auth_retry(env_row, broadcast_event, payload)
                st.success("Test connection request submitted.")
                st.caption(f"API: `{url}`")
                st.json(_redact_secrets(out))
            except Exception as ex:
                st.error(f"Test connection failed: {ex}")


def render_centered_table(df):
    """Renders a scrollable HTML table. Styling is handled via global CSS."""
    html = df.to_html(index=False, classes='centered-table', escape=False)
    st.markdown(f'<div class="scroll-container">{html}</div>', unsafe_allow_html=True)


def main():
    if not engine:
        st.error("Checking DB Connection...")
        return
    
    # --- Global Styling (Stable & Styled) ---
    st.markdown("""
        <style>
            [data-testid="stHeader"] { display: none; }
            .main, .main .block-container, [data-testid="stAppViewContainer"] .main .block-container {
                padding-top: 0 !important;
                margin-top: 0 !important;
                max-width: 1400px !important;
            }
            html, body, .stApp, [data-testid="stAppViewContainer"],
            [data-testid="stAppViewContainer"] > .main,
            [data-testid="stAppViewContainer"] > .main .block-container {
                margin: 0 !important;
                padding: 0 !important;
            }
            [data-testid="stToolbar"], [data-testid="stDecoration"] {
                display: none !important;
                height: 0 !important;
            }
            /* Table Styling & Zebra Striping */
            .scroll-container {
                overflow-x: auto !important;
                width: 100%;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
                margin-top: 1.5rem;
                margin-bottom: 2.5rem;
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
            }
            .centered-table {
                width: 100% !important;
                border-collapse: collapse !important;
                font-family: inherit;
                font-size: 0.95rem !important;
            }
            .centered-table th {
                background-color: #f8fafc !important;
                color: #475569 !important;
                font-weight: 600 !important;
                padding: 8px 10px !important;
                border-bottom: 2px solid #e2e8f0 !important;
                text-align: center !important;
                white-space: nowrap !important;
            }
            .centered-table td {
                padding: 6px 10px !important;
                border-bottom: 1px solid #f1f5f9 !important;
                text-align: center !important;
                color: #334155 !important;
            }
            .centered-table tr:nth-child(even) { background-color: #f8fafc !important; }
            .centered-table tr:hover { background-color: #f1f5f9 !important; }
        </style>
    """, unsafe_allow_html=True)

    # --- Logout Action ---
    query_params = st.query_params
    if query_params.get("action") == "logout":
        from dashboard_auth import logout_user
        logout_user()
        return

    # --- Session Persistence Check ---
    if not st.session_state.get("authenticated"):
        from dashboard_auth import get_persisted_session, get_user_permissions
        persisted = get_persisted_session()
        if persisted:
            st.session_state.authenticated = True
            st.session_state.username = persisted["username"]
            st.session_state.user_role = persisted["role"]
            st.session_state.user_role_id = persisted["role_id"]
            st.session_state.allowed_pages = get_user_permissions(engine, persisted["role_id"])

    if not st.session_state.get("authenticated"):
        render_login_page()
        return

    from urllib.parse import unquote_plus
    query_params = st.query_params
    page_map = {
        "Overview": "Overview",
        "Build Analysis": "Build Analysis",
        "Comparison": "Comparison",
        "Build DS Runs": "Build DS Runs",
        "Data Source Trend Comparison": "Data Source Trend Comparison",
        "Add New Benchmark": "Add New Benchmark",
        "Small Files Trend Analysis": "Small Files Trend Analysis",
        "Large Files Trend Analysis": "Large Files Trend Analysis",
        "Small Files OLS Trend": "Small Files OLS Trend",
        "Large Files OLS Trend": "Large Files OLS Trend",
        "Small Files OLS Trend Analysis": "Small Files OLS Trend",
        "Large Files OLS Trend Analysis": "Large Files OLS Trend",
        "OLS vs Full Scan (Small)": "OLS vs Full Scan (Small)",
        "OLS vs Full Scan (Large)": "OLS vs Full Scan (Large)",
        "Discover File FF On vs Off": "Discover File FF On vs Off",
        "ACL ON vs OLS (ACL ON)": "ACL ON vs OLS (ACL ON)",
        "ACL OFF vs OLS (ACL OFF)": "ACL OFF vs OLS (ACL OFF)",
        "Run Comparison Tool": "Comparison",
        "Edit Records": "Edit Records",
        "Manage Classifiers": "Manage Classifiers",
        "Classifier Changes": "Classifier Changes",
        "Manage Test Environments": "Manage Test Environments",
        "Version Comparison": "Version Comparison",
        "Workflow Automation": "Workflow Automation",
        "Create Data Source": "Create Data Source",
        "Scan Status": "Scan Status",
        "Scan Progress": "Scan Progress",
        "Scanner Infrastructure": "Scanner Infrastructure",
        "Manage RBAC": "Manage RBAC"
    }

    if "page" in query_params:
        page_val = query_params["page"]
        if isinstance(page_val, list):
            page_val = page_val[0] if page_val else ""
        if isinstance(page_val, str) and page_val:
            normalized = unquote_plus(page_val)
            if normalized in page_map:
                st.session_state.current_page = page_map[normalized]

    if 'current_page' not in st.session_state:
        st.session_state.current_page = "Small Files Trend Analysis"

    # Hard override: remove any remaining top gap
    st.markdown("""
        <style>
        [data-testid="stAppViewContainer"] > .main,
        .main .block-container,
        section.main,
        .block-container {
            padding-top: 0 !important;
            margin-top: -12px !important;
        }
        [data-testid="stSidebar"] > div:first-child,
        [data-testid="stSidebar"] .block-container {
            padding-top: 0 !important;
            margin-top: 0 !important;
        }
        .sidebar-header {
            margin-top: -12px !important;
        }
        /* Fix for potential "page inside page" / iframe nesting issues */
        iframe[title="streamlit.components.v1.html"] {
            border: none !important;
        }
        </style>
    """, unsafe_allow_html=True)

    # Custom Flyout/Hover Menu (restored)
    logo_b64 = get_logo_base64()
    logo_html = f'<img src="data:image/png;base64,{logo_b64}" style="width: 70%; border-radius: 4px; margin-bottom: 6px;" alt="BigID logo" />' if logo_b64 else '<div style="color:#ffffff;font-weight:700;">BigID</div>'
    sidebar_html = f"""
        <style>
        /* Sidebar Font & Logo */
        [data-testid="stSidebar"] * {{
            font-family: 'Inter', 'Roboto', 'Segoe UI', sans-serif !important;
        }}
        .material-icons, .material-symbols-outlined {{
            font-family: 'Material Icons', 'Material Symbols Outlined', sans-serif !important;
            font-weight: normal;
            font-style: normal;
            font-size: 18px;
            line-height: 1;
            letter-spacing: normal;
            text-transform: none;
            display: inline-block;
            white-space: nowrap;
            word-wrap: normal;
            direction: ltr;
            -webkit-font-feature-settings: 'liga';
            -webkit-font-smoothing: antialiased;
        }}
        .sidebar-header {{
            text-align: left;
            padding-left: 10px;
            padding-bottom: 6px;
        }}
        .sidebar-header h1 {{
            color: #8B5CF6;
            margin-bottom: 0px;
            font-size: 1.2rem;
            font-weight: 700;
        }}
        .sidebar-header p {{
            font-size: 0.75rem !important;
        }}
        
        [data-testid="stSidebar"] > div:first-child {{
            overflow: visible !important;
        }}
        [data-testid="stSidebarNav"] {{
            display: none;
        }}

        .nav-category {{
            position: relative;
            width: 100%;
            padding: 8px 10px;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            gap: 8px;
            color: #1e293b !important;
            margin-bottom: 2px;
            background: transparent;
            border: 1px solid transparent;
            z-index: 99;
        }}
        .nav-category:hover {{
            background-color: #f5f3ff;
            border-color: #8B5CF6;
            color: #8B5CF6 !important;
            z-index: 1000;
        }}
        .nav-category b {{ 
            font-size: 0.9rem; 
            color: inherit;
        }}
        
        .flyout-menu {{
            display: none;
            position: absolute;
            left: 100%; 
            top: -5px;
            background-color: white !important;
            min-width: 210px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            z-index: 10001;
            padding: 6px 10px 10px 10px;
            border-radius: 12px;
            border: 1px solid #e2e8f0;
            margin-left: 6px;
        }}
        .flyout-menu::before {{
            content: "";
            position: absolute;
            left: -8px;
            top: 18px;
            width: 12px;
            height: 12px;
            background: white;
            transform: rotate(45deg);
            border-left: 1px solid #e2e8f0;
            border-bottom: 1px solid #e2e8f0;
        }}

        .nav-category:hover .flyout-menu {{
            display: block;
            animation: fadeIn 0.15s ease-out;
        }}
        .flyout-menu:hover {{
            display: block;
        }}
        .nav-category::after {{
            content: "";
            position: absolute;
            right: -28px;
            top: 0;
            width: 28px;
            height: 100%;
        }}
        .flyout-menu {{
            pointer-events: auto;
        }}
        
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateX(-5px); }}
            to {{ opacity: 1; transform: translateX(0); }}
        }}

        .flyout-header {{
            font-size: 0.6rem;
            font-weight: 800;
            color: #94a3b8 !important;
            text-transform: uppercase;
            margin-top: 4px;
            margin-bottom: 4px;
            letter-spacing: 1px;
            border-bottom: 1px solid #f1f5f9;
            padding-bottom: 2px;
        }}
        
        .flyout-item {{
            display: flex !important;
            align-items: center;
            gap: 6px;
            padding: 6px 8px;
            color: #000000 !important;
            text-decoration: none !important;
            font-size: 0.8rem !important;
            border-radius: 6px;
            transition: all 0.2s;
            margin-bottom: 1px;
            font-weight: 600 !important;
            background: #ffffff !important;
            border: 1px solid transparent;
        }}
        .flyout-item:hover {{
            background-color: #f1f5f9 !important;
            color: #8B5CF6 !important;
            border-color: #e2e8f0 !important;
        }}
        .flyout-item span {{
            color: #000000 !important;
        }}
        .flyout-item:hover span {{
            color: #8B5CF6 !important;
        }}
        
        #MainMenu {{visibility: hidden;}}
        header {{visibility: hidden;}}
        </style>
        
        <div class="sidebar-header" style="text-align: left; padding: 6px 10px; background: rgba(255,255,255,0.05); border-radius: 10px; margin: 2px 8px 6px;">
            {logo_html}
            <p style="color: #e2e8f0; font-size: 0.8rem; margin: 0; font-weight: 600; padding-left: 2px;">Benchmark Explorer</p>
        </div>
        
        <div style="padding: 0 16px 10px 16px; border-bottom: 1px solid rgba(255,255,255,0.1); margin-bottom: 10px;">
            <p style="color: #a78bfa; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px;">Logged in as</p>
            <p style="color: white; font-weight: 600; font-size: 0.9rem; margin: 0;">{st.session_state.username} ({st.session_state.user_role})</p>
        </div>

        <div class="nav-category">
            <span style="font-size: 1rem;">📊</span> <b style="color: #ffffff !important; font-size: 0.9rem;">Standard Trends</b>
            <div class="flyout-menu">
                <div class="flyout-header">Trend Analysis</div>
                {f'<a href="?page=Small+Files+Trend+Analysis" target="_self" class="flyout-item"><span>📄</span> <span>Small Files Trend</span></a>' if check_access("Small Files Trend Analysis") else ""}
                {f'<a href="?page=Large+Files+Trend+Analysis" target="_self" class="flyout-item"><span>📦</span> <span>Large Files Trend</span></a>' if check_access("Large Files Trend Analysis") else ""}
                {f'<a href="?page=Small+Files+OLS+Trend+Analysis" target="_self" class="flyout-item"><span>⚡</span> <span>Small Files OLS</span></a>' if check_access("Small Files OLS Trend") else ""}
                {f'<a href="?page=Large+Files+OLS+Trend+Analysis" target="_self" class="flyout-item"><span>⚡</span> <span>Large Files OLS</span></a>' if check_access("Large Files OLS Trend") else ""}
            </div>
        </div>
        <div class="nav-category">
            <span style="font-size: 1rem;">⚖️</span> <b style="color: #ffffff !important; font-size: 0.9rem;">Expert Comparisons</b>
            <div class="flyout-menu">
                <div class="flyout-header">Head-to-Head</div>
                {f'<a href="?page=Build+Analysis" target="_self" class="flyout-item"><span>📉</span> <span>Build Analysis</span></a>' if check_access("Build Analysis") else ""}
                {f'<a href="?page=Build+DS+Runs" target="_self" class="flyout-item"><span>🧪</span> <span>Build + DS Runs</span></a>' if check_access("Build DS Runs") else ""}
                {f'<a href="?page=Data+Source+Trend+Comparison" target="_self" class="flyout-item"><span>📈</span> <span>DS Trend Comparison</span></a>' if check_access("Data Source Trend Comparison") else ""}
                {f'<a href="?page=OLS+vs+Full+Scan+(Small)" target="_self" class="flyout-item"><span>⚖️</span> <span>OLS vs Full (Small)</span></a>' if check_access("OLS vs Full Scan (Small)") else ""}
                {f'<a href="?page=OLS+vs+Full+Scan+(Large)" target="_self" class="flyout-item"><span>⚖️</span> <span>OLS vs Full (Large)</span></a>' if check_access("OLS vs Full Scan (Large)") else ""}
                {f'<a href="?page=Discover+File+FF+On+vs+Off" target="_self" class="flyout-item"><span>⚙️</span> <span>Discover FF: On/Off</span></a>' if check_access("Discover File FF On vs Off") else ""}
                {f'<a href="?page=ACL+ON+vs+OLS+(ACL+ON)" target="_self" class="flyout-item"><span>🔒</span> <span>ACL ON Comparison</span></a>' if check_access("ACL ON vs OLS (ACL ON)") else ""}
                {f'<a href="?page=ACL+OFF+vs+OLS+(ACL+OFF)" target="_self" class="flyout-item"><span>🔓</span> <span>ACL OFF Comparison</span></a>' if check_access("ACL OFF vs OLS (ACL OFF)") else ""}
            </div>
        </div>

        <div class="nav-category">
            <span style="font-size: 1rem;">🛠️</span> <b style="color: #ffffff !important; font-size: 0.9rem;">System Tools</b>
            <div class="flyout-menu">
                <div class="flyout-header">Administration</div>
                {f'<a href="?page=Run+Comparison+Tool" target="_self" class="flyout-item"><span>🔄</span> <span>Run Comparison Tool</span></a>' if check_access("Comparison") else ""}
                {f'<a href="?page=Classifier+Changes" target="_self" class="flyout-item"><span>🧬</span> <span>Classifier Comparison</span></a>' if check_access("Classifier Changes") else ""}
                {f'<a href="?page=Add+New+Benchmark" target="_self" class="flyout-item"><span>➕</span> <span>Add New Data Record</span></a>' if check_access("Add New Benchmark") else ""}
                {f'<a href="?page=Edit+Records" target="_self" class="flyout-item"><span>🛠️</span> <span>Edit Existing Records</span></a>' if check_access("Edit Records") else ""}
                {f'<a href="?page=Manage+Test+Environments" target="_self" class="flyout-item"><span>🌐</span> <span>Manage Test Environments</span></a>' if check_access("Manage Test Environments") else ""}
                {f'<a href="?page=Version+Comparison" target="_self" class="flyout-item"><span>🧬</span> <span>Version Comparison</span></a>' if check_access("Version Comparison") else ""}
                {f'<a href="?page=Workflow+Automation" target="_self" class="flyout-item"><span>🧪</span> <span>Workflow Automation</span></a>' if check_access("Workflow Automation") else ""}
                {f'<a href="?page=Create+Data+Source" target="_self" class="flyout-item"><span>🧩</span> <span>Create Data Source</span></a>' if check_access("Create Data Source") else ""}
                {f'<a href="?page=Scan+Status" target="_self" class="flyout-item"><span>🛰️</span> <span>Scan Status</span></a>' if check_access("Scan Status") else ""}
                {f'<a href="?page=Scan+Progress" target="_self" class="flyout-item"><span>📈</span> <span>Scan Progress</span></a>' if check_access("Scan Progress") else ""}
                {f'<a href="?page=Scanner+Infrastructure" target="_self" class="flyout-item"><span>🖥️</span> <span>Scanner Infrastructure</span></a>' if check_access("Scan Status") else ""}
                {f'<a href="?page=Manage+RBAC" target="_self" class="flyout-item"><span>🛡️</span> <span>Manage RBAC</span></a>' if check_access("Manage RBAC") else ""}
            </div>
        </div>

        <div style="margin-top: auto; padding: 10px 0;">
            <a href="?action=logout" target="_self" style="text-decoration: none;">
                <div class="nav-category" style="background: rgba(239, 68, 68, 0.1); border-color: rgba(239, 68, 68, 0.2);">
                    <span style="font-size: 1rem;">🚪</span> <b style="color: #f87171 !important; font-size: 0.9rem;">Logout</b>
                </div>
            </a>
        </div>
    """
    st.sidebar.markdown(sidebar_html, unsafe_allow_html=True)
    
    st.sidebar.markdown("<br>", unsafe_allow_html=True)
    if st.sidebar.button("Refresh", width='stretch'): 
        st.cache_data.clear()
        st.rerun()

    page = st.session_state.current_page
    
    data = load_data()
    
    # Standard Pages
    if page == "Overview": 
        page_overview(data)
    elif page == "Build Analysis":
        page_build_analysis(data)
    elif page == "Comparison":
        page_comparison(data)
    elif page == "Build DS Runs":
        page_build_ds_runs()
    elif page == "Data Source Trend Comparison":
        page_data_source_trend_comparison(data)
    elif page == "Small Files Trend Analysis":
        render_trend_analysis(data, "Small Files Trend Analysis", file_set_filter="Small Files")
    elif page == "Large Files Trend Analysis":
        render_trend_analysis(data, "Large Files Trend Analysis", file_set_filter="Large Files")
    elif page == "Small Files OLS Trend":
        render_trend_analysis(data, "Small Files OLS Trend Analysis", file_set_filter="Small Files", scan_type_filter="OLS Scan")
    elif page == "Large Files OLS Trend":
        render_trend_analysis(data, "Large Files OLS Trend Analysis", file_set_filter="Large Files", scan_type_filter="OLS Scan")
    elif page == "Auto Small Files Trend Analysis":
        render_auto_trend_analysis("Auto Small Files Trend Analysis", file_set_keyword="Small Files")
    elif page == "Auto Large Files Trend Analysis":
        render_auto_trend_analysis("Auto Large Files Trend Analysis", file_set_keyword="Large Files")
    elif page == "Auto Small Files OLS Trend":
        render_auto_trend_analysis("Auto Small Files OLS Trend", file_set_keyword="Small Files", scan_type_filter="OLS Scan")
    elif page == "Auto Large Files OLS Trend":
        render_auto_trend_analysis("Auto Large Files OLS Trend", file_set_keyword="Large Files", scan_type_filter="OLS Scan")
    
    # Specialized Comparisons
    elif page == "OLS vs Full Scan (Small)":
        render_trend_analysis(data, "OLS vs Full Scan (Small Files)", file_set_filter="Small Files", compare_by="Scan_Type")
    elif page == "OLS vs Full Scan (Large)":
        render_trend_analysis(data, "OLS vs Full Scan (Large Files)", file_set_filter="Large Files", compare_by="Scan_Type")
    elif page == "Discover File FF On vs Off":
        render_trend_analysis(data, "Discover File FF Comparison (Full Scan)", scan_type_filter="Full Scan", compare_by="is_feature_flags")
    elif page == "ACL ON vs OLS (ACL ON)":
        # Compare Full Scan (ACL ON) vs OLS (ACL ON)
        # We filter for ACL=1 and compare by Scan Type
        render_trend_analysis(data, "Full Scan vs OLS (ACL ON)", acl_filter=1, compare_by="Scan_Type")
    elif page == "ACL OFF vs OLS (ACL OFF)":
        render_trend_analysis(data, "Full Scan vs OLS (ACL OFF)", acl_filter=0, compare_by="Scan_Type")
    
    # Tools
    elif page == "Add New Benchmark": 
        page_add_benchmark()
    elif page == "Edit Records":
        mode = st.radio("Record Type", ["Manual Benchmarks", "Auto Scans"], horizontal=True)
        if mode == "Manual Benchmarks":
            full_data = load_data(include_inactive=True)
            page_edit_benchmark(full_data)
        else:
            full_data = load_auto_trend_as_manual_like_df()
            page_edit_benchmark(full_data)
    elif page == "Manage Classifiers":
        page_manage_classifiers()
    elif page == "Classifier Changes":
        page_classifier_build_diffs()
    elif page == "Manage Test Environments":
        page_manage_test_environments()
    elif page == "Version Comparison":
        page_version_comparison()
    elif page == "Workflow Automation":
        page_workflow_automation()
    elif page == "Create Data Source":
        page_create_data_source()
    elif page == "Scan Status":
        page_scan_status()
    elif page == "Scan Progress":
        page_scan_progress()
    elif page == "Scanner Infrastructure":
        if check_access("Scan Status"):
            page_scanner_infrastructure()
        else:
            st.error("Access denied.")
    elif page == "Manage RBAC":
        if check_access("Manage RBAC"):
            page_manage_rbac()
        else:
            st.error("Access Denied. Admin role required.")
    
    # Pre-check access for standard pages
    if page in page_map.values() and page != "Manage RBAC":
        if not check_access(page):
            st.error(f"Access Denied. You do not have permission to view '{page}'.")
            return


def render_login_page():
    """Renders a centered, premium login form."""
    c1, c2, c3 = st.columns([1, 1.2, 1])
    with c2:
        st.markdown("<br><br><br>", unsafe_allow_html=True)
        st.markdown(f"""
            <div style="background: white; padding: 2.1rem; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); border: 1px solid #e2e8f0;">
                <h1 style="text-align: center; color: {PRIMARY_COLOR}; margin-bottom: 0.5rem; font-size: 1.8rem;">Welcome Back</h1>
                <p style="text-align: center; color: #64748b; margin-bottom: 0.5rem; font-size: 0.9rem;">Sign in to access Benchmark Dashboard</p>
            </div>
            <div style="height: 20px;"></div>
        """, unsafe_allow_html=True)
        
        # No st.form so we can have reactive password visibility
        email = st.text_input("Email Address", placeholder="name@bigid.com", key="login_email_input").strip().lower()
        
        # Admin is bpatel@bigid.com
        is_admin_field = (email == "bpatel@bigid.com")
        
        password = ""
        if is_admin_field:
            password = st.text_input("Password", type="password", key="login_password_input")

        st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)
        if st.button("Sign In", width='stretch', type="primary"):
            if not email:
                st.error("Please enter your email address.")
            else:
                ok, err = login_user(engine, email)
                if ok:
                    st.success("Login successful!")
                    st.rerun()
                else:
                    st.error(err or "Login failed")

        st.markdown("<p style='text-align: center; color: #94a3b8; font-size: 0.75rem; margin-top: 2rem;'>© 2026 BigID Benchmark Tools</p>", unsafe_allow_html=True)


def page_manage_rbac():
    """Admin-only UI for managing users, roles, and permissions."""
    if not st.session_state.get("user_role") == "Admin":
        st.error("Admin access required.")
        return

    st.title("🛡️ RBAC & User Management")
    
    tab1, tab2, tab3 = st.tabs(["👤 Users", "👥 Roles", "🔐 Permissions"])
    
    with tab1:
        st.subheader("Manage Users")
        # List users
        with engine.connect() as conn:
            users_df = pd.read_sql(text("""
                SELECT u.id, u.username, r.role_name, u.created_at 
                FROM app_users u 
                JOIN app_roles r ON u.role_id = r.id
            """), conn)
            roles_df = pd.read_sql(text("SELECT id, role_name FROM app_roles"), conn)
        
        st.dataframe(users_df, width='stretch', hide_index=True)
        
        with st.expander("👤 Edit User Role / Add User"):
            # List users for editing
            user_to_edit = st.selectbox("Select User to Edit Role", ["-- Create New --"] + users_df['username'].tolist())
            
            roles_df = pd.read_sql(text("SELECT id, role_name FROM app_roles"), engine)
            role_map = {r['role_name']: r['id'] for _, r in roles_df.iterrows()}
            
            if user_to_edit == "-- Create New --":
                new_user = st.text_input("New Email Address")
                new_role_name = st.selectbox("Assign Role", list(role_map.keys()), key="new_role_sel")
                
                if st.button("Create User"):
                    if new_user and "@bigid.com" in new_user:
                        try:
                            with engine.connect() as conn:
                                conn.execute(text("INSERT INTO app_users (username, password_hash, role_id) VALUES (:u, 'PASSWORDLESS', :r)"),
                                             {"u": new_user.strip().lower(), "r": role_map[new_role_name]})
                                conn.commit()
                            st.success(f"User {new_user} created.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error: {e}")
                    else:
                        st.warning("Please provide a valid @bigid.com email.")
            else:
                # Edit Existing
                current_user_row = users_df[users_df['username'] == user_to_edit].iloc[0]
                current_role = current_user_row['role_name']
                new_role_name = st.selectbox("Update Role", list(role_map.keys()), index=list(role_map.keys()).index(current_role))
                
                if st.button("Update User Role"):
                    from dashboard_auth import update_user_role
                    update_user_role(engine, current_user_row['id'], role_map[new_role_name])
                    st.success(f"Updated {user_to_edit} to {new_role_name}")
                    st.rerun()

    with tab2:
        st.subheader("Manage Roles")
        st.dataframe(roles_df, width='stretch', hide_index=True)
        
        with st.expander("➕ Add New Role"):
            new_role = st.text_input("Role Name")
            if st.button("Create Role"):
                if new_role:
                    try:
                        with engine.connect() as conn:
                            conn.execute(text("INSERT INTO app_roles (role_name) VALUES (:r)"), {"r": new_role})
                            conn.commit()
                        st.success(f"Role {new_role} created.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

    with tab3:
        st.subheader("Role Permissions")
        role_to_config = st.selectbox("Select Role to Configure", roles_df['role_name'].tolist())
        target_role_id = roles_df[roles_df['role_name'] == role_to_config]['id'].iloc[0]
        
        # All available pages
        all_pages = [
            "Overview", "Build Analysis", "Comparison", "Build DS Runs", "Data Source Trend Comparison",
            "Small Files Trend Analysis", "Large Files Trend Analysis", "Small Files OLS Trend", "Large Files OLS Trend",
            "OLS vs Full Scan (Small)", "OLS vs Full Scan (Large)", "Discover File FF On vs Off",
            "ACL ON vs OLS (ACL ON)", "ACL OFF vs OLS (ACL OFF)", "Edit Records", "Manage Classifiers",
            "Classifier Changes", "Manage Test Environments", "Version Comparison", "Workflow Automation",
            "Create Data Source", "Scan Status", "Scan Progress"
        ]
        
        current_perms = get_user_permissions(engine, target_role_id)
        
        selected_pages = st.multiselect("Allowed Pages", all_pages, default=current_perms)
        
        if st.button("Update Permissions"):
            try:
                with engine.connect() as conn:
                    conn.execute(text("DELETE FROM app_role_pages WHERE role_id = :rid"), {"rid": int(target_role_id)})
                    for p in selected_pages:
                        conn.execute(text("INSERT INTO app_role_pages (role_id, page_name) VALUES (:rid, :p)"),
                                     {"rid": int(target_role_id), "p": p})
                    conn.commit()
                st.success("Permissions updated.")
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")

if __name__ == "__main__":
    main()
