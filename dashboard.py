import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import altair as alt
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
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
from typing import Any, Dict, List, Optional
from db_config import DB_CONFIG, DB_CONNECTION_STRING
from bigid_auth import Get_auth_token, get_saved_environment_credentials, save_environment_credentials
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
        engine = create_engine(DB_CONNECTION_STRING)
        with engine.connect() as conn: pass
        return engine
    except Exception as e:
        return None

engine = get_engine()

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

    # Pull totals from scan_insights when available, without requiring benchmarks schema changes.
    # If benchmarks has its own columns, prefer them but fall back to scan_insights.
    total_class_expr = (
        "COALESCE(b.total_classification, si.total_classification) as Total_Classification"
        if has_total_classification
        else "si.total_classification as Total_Classification"
    )
    removed_attr_expr = (
        "COALESCE(b.removed_attributes, si.removed_attributes_count) as Removed_Attributes"
        if has_removed_attributes
        else "si.removed_attributes_count as Removed_Attributes"
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
            b.total_catalog_items as New_Attributes,
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
            b.is_active
        FROM benchmarks b
        JOIN builds bu ON b.build_id = bu.id
        JOIN configurations conf ON b.config_id = conf.id
        LEFT JOIN classifiers c ON b.classifier_id = c.id
        LEFT JOIN scan_insights si ON si.scan_id = b.scan_id
        {where_clause}
    """
    try:
        df = pd.read_sql(query, engine)
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


def ensure_test_environments_table():
    if not engine:
        return
    try:
        with engine.connect() as conn:
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


def load_test_environments():
    if not engine:
        return pd.DataFrame()
    ensure_test_environments_table()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, env_name, base_url, version, auth_module_path, auth_function_name, classifiers_api_path, is_active
                FROM test_environments
                ORDER BY env_name
            """)).fetchall()
            cols = ["id", "env_name", "base_url", "version", "auth_module_path", "auth_function_name", "classifiers_api_path", "is_active"]
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
    progress = scan.get("progress")
    if isinstance(progress, dict):
        state = str(progress.get("state") or "").strip()
        if state:
            return state
    return str(scan.get("state") or scan.get("status") or "").strip()


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


def format_scan_duration(started_at, completed_at):
    start_dt = parse_iso_datetime(started_at)
    end_dt = parse_iso_datetime(completed_at)
    if not start_dt or not end_dt:
        return ""
    seconds = int((end_dt - start_dt).total_seconds())
    if seconds < 0:
        return ""
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
            # Non-breaking schema upgrades for existing DBs
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
                "eid": stp.get("environment_id"),
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


def load_workflow_runs(limit: int = 30) -> pd.DataFrame:
    ensure_workflow_tables()
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
                        wr.last_error
                    FROM workflow_runs wr
                    ORDER BY wr.id DESC
                    LIMIT :lim
                """),
                {"lim": int(limit)},
            ).fetchall()
        cols = [
            "id","name","status","env1_id","env2_id","cooldown_minutes",
            "next_eligible_at","started_at","finished_at","updated_at","created_at","last_error",
        ]
        return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    except Exception:
        return pd.DataFrame()


def load_workflow_steps(run_id: int) -> pd.DataFrame:
    ensure_workflow_tables()
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
        pending = steps_df[steps_df["status"].astype(str).isin(["pending", "running", "created", "scan_running", "ds_created", "scan_created"])]
    except Exception:
        pending = steps_df
    scan_ids = [str(x).strip() for x in pending.get("scan_id", pd.Series(dtype=str)).tolist() if str(x).strip()]
    if not scan_ids:
        return out

    # Resolve latest statuses for scan_ids
    status_map: Dict[str, str] = {}
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

    try:
        with engine.connect() as conn:
            for row in pending.itertuples():
                sid = str(getattr(row, "scan_id", "") or "").strip()
                if not sid:
                    continue
                stv = status_map.get(sid, "")
                if not _is_terminal_state_anywhere(stv):
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
        if not steps_df2.empty and (steps_df2["status"].astype(str) == "completed").all():
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
    scan_state = str(si.get("state") or progress.get("state") or "").strip() or None
    
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
                    objects_with_pi = VALUES(objects_with_pi)
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
        return {"ok": True, "captured_at_utc": captured_at_utc, "scan_state": scan_state}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


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
                SELECT scan_id, scan_name, environment_id, scan_type, scan_status, is_active
                FROM scan_parents WHERE scan_id = :sid
            """), {"sid": scan_id}).fetchone()
            if row:
                return {
                    "scan_id": row[0],
                    "name": row[1],
                    "environment_id": row[2],
                    "type": row[3],
                    "scan_status": row[4],
                    "is_active": row[5]
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
                "distinct_classification_attributes": row[0] if row[0] is not None else "",
                "classifier_attribute_names": classifier_attribute_names,
                "data_source_names": data_source_names,
                "objects_with_pi": row[1] if row[1] is not None else "",
                "objects_scanned": row[2] if row[2] is not None else "",
                "objects_enumerated": row[3] if row[3] is not None else "",
                "total_classification": row[4] if row[4] is not None else "",
                "new_attributes_count": map_new_count if map_new_count else (row[5] if row[5] is not None else ""),
                "removed_attributes_count": map_removed_count if map_removed_count else (row[6] if row[6] is not None else ""),
            }
    except Exception:
        return None


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
                    removed_attributes_count
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
                # If everything is zero but si has explicit 0, keep 0; else empty string.
                if si_val is not None:
                    return int(si_val)
                return ""

            result[sid] = {
                "distinct_classification_attributes": r[1] if r[1] is not None else "",
                "classifier_attribute_names": str(r[2] or "").strip(),
                "data_source_names": str(r[3] or "").strip(),
                "objects_with_pi": r[4] if r[4] is not None else "",
                "objects_scanned": r[5] if r[5] is not None else "",
                "objects_enumerated": r[6] if r[6] is not None else "",
                "total_classification": r[7] if r[7] is not None else "",
                "new_attributes_count": _pick_count(d_new, si_new_i, p_new),
                "removed_attributes_count": _pick_count(d_removed, si_removed_i, p_removed),
            }
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

    ensure_test_environments_table()
    env_df = load_test_environments()

    # Bulk: sync current versions for all active envs + global enabled classifiers (no manual follow-up steps).
    st.subheader("0. Bulk Actions")
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

    c1, c2 = st.columns(2)
    env_name = c1.text_input("Environment Name", key="env_name_input")
    base_url = c2.text_input("Base URL", key="env_base_url_input", placeholder="https://test-env.example.com")

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

    c3, c4 = st.columns(2)
    version = c3.text_input("Version", key="env_version_input")
    auth_username = c4.text_input("Username (optional)", key="env_auth_username_input", help="If blank, defaults to bigid")
    c5, _ = st.columns(2)
    auth_password = c5.text_input("Password (optional)", key="env_auth_password_input", type="password", help="If blank, defaults to bigid111")
    is_active = st.checkbox("Active", value=bool(selected_row.get("is_active", True)))

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
                        INSERT INTO test_environments (env_name, base_url, version, auth_module_path, auth_function_name, classifiers_api_path, is_active)
                        VALUES (:n, :u, :v, :ap, :af, :cp, :ia)
                        ON DUPLICATE KEY UPDATE
                            base_url = VALUES(base_url),
                            version = VALUES(version),
                            auth_module_path = VALUES(auth_module_path),
                            auth_function_name = VALUES(auth_function_name),
                            classifiers_api_path = VALUES(classifiers_api_path),
                            is_active = VALUES(is_active)
                    """), {
                        "n": env_name.strip(),
                        "u": normalize_base_url(base_url),
                        "v": version.strip() if version else None,
                        "ap": resolved_auth_module_path or None,
                        "af": resolved_auth_function or "Get_auth_token",
                        "cp": resolved_api_path or DEFAULT_CLASSIFIERS_API_PATH,
                        "ia": 1 if is_active else 0
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
                        else:
                            st.warning("Environment saved, but classifier API returned no names.")
                    except Exception as ex:
                        st.warning(f"Environment saved, but classifier sync failed: {ex}")

                st.cache_data.clear()
            except Exception as e:
                st.error(f"Error saving environment: {e}")

    if not env_df.empty:
        show_df = env_df.copy()
        show_df["is_active"] = show_df["is_active"].apply(lambda x: bool(x))
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
                                st.cache_data.clear()
                                return
                            st.session_state["env_action_level"] = "info"
                            st.session_state["env_action_msg"] = f"[{env_name_val}] No version change ({clean_version}). Classifier sync skipped."
                            st.info(st.session_state["env_action_msg"])
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
        h1, h2, h3, h4, h5, h6, h7 = st.columns([1.2, 3.2, 0.8, 0.7, 2.2, 1.1, 1.2])
        h1.markdown("**env_name**")
        h2.markdown("**base_url**")
        h3.markdown("**version**")
        h4.markdown("**is_active**")
        h5.markdown("**Build Change Path**")
        h6.markdown("**Sync Core Ver**")
        h7.markdown("**Get Classifiers**")
        st.divider()

        for _, r in show_df.iterrows():
            row_dict = r.to_dict()
            row_dict["classifiers_api_path"] = str(row_dict.get("classifiers_api_path") or INTERNAL_CLASSIFIERS_API_PATH).strip()
            c1, c2, c3, c4, c5, c6, c7 = st.columns([1.2, 3.2, 0.8, 0.7, 2.2, 1.1, 1.2])
            c1.write(str(row_dict.get("env_name") or ""))
            c2.write(str(row_dict.get("base_url") or ""))
            c3.write(str(row_dict.get("version") or ""))
            c4.write("Yes" if bool(row_dict.get("is_active")) else "No")
            env_id = row_dict.get("id")
            path_text = build_paths.get(int(env_id), f"None -> {str(row_dict.get('version') or '').strip()}") if env_id is not None else f"None -> {str(row_dict.get('version') or '').strip()}"
            c5.write(path_text)
            c6.markdown(
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
            if c6.button("Sync Core Ver", key=f"sync_btn_{int(row_dict.get('id'))}"):
                run_env_actions(row_dict, do_sync=True, do_fetch=False)
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
            if c7.button("Get Classifiers", key=f"fetch_btn_{int(row_dict.get('id'))}"):
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

        # Keep Scan Status accurate when scan_parents lags (case/space-insensitive):
        # - if scan_parents says running but scan_progress says terminal -> trust scan_progress
        # - if scan_parents says terminal/unknown but scan_progress says running -> trust scan_progress
        try:
            p_state = str((latest_progress_state_map or {}).get(scan_id) or "").strip()
            if p_state and (_compact_state(p_state) in completed_compact) and (_compact_state(state) in running_compact):
                state = p_state
                # Backfill scan_parents for consistency (best-effort).
                try:
                    upsert_scan_parent_record(scan_env_id or selected_env.get("id"), scan, state)
                except Exception:
                    pass
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
                insight_metrics.update({
                    "distinct_classification_attributes": db_metrics.get("distinct_classification_attributes", ""),
                    "classifier_attribute_names": db_metrics.get("classifier_attribute_names", ""),
                    "data_source_names": db_metrics.get("data_source_names", ""),
                    "objects_with_pi": db_metrics.get("objects_with_pi", ""),
                    "objects_scanned": db_metrics.get("objects_scanned", ""),
                    "objects_enumerated": db_metrics.get("objects_enumerated", ""),
                    "total_classification": db_metrics.get("total_classification", ""),
                    "new_attributes_count": db_metrics.get("new_attributes_count", ""),
                    "removed_attributes_count": db_metrics.get("removed_attributes_count", ""),
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
            "Scan Time": format_scan_duration(started_at, completed_at),
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
def page_build_analysis(df):
    st.markdown("## 🔍 Build Depth-Analysis")
    if df.empty:
        st.warning("No data found.")
        return

    # Select Build
    all_versions = sorted(df['Build_Version'].astype(str).unique(), reverse=True)
    selected_build = st.selectbox("Select Build to Analyze", all_versions)
    
    build_df = df[df['Build_Version'].astype(str) == selected_build]
    
    st.markdown(f"### Build Performance Deep-Dive: `{selected_build}`")
    
    # KPIs for this specific build
    k1, k2, k3, k4 = st.columns(4)
    avg_tp = build_df['throughput_cluster_gb_hr'].mean()
    avg_file_tp = build_df['throughput_cluster_files_hr'].mean()
    total_scans = len(build_df)
    primary_ds = build_df['Data_Source_Type'].mode()[0] if not build_df.empty else "N/A"
    
    k1.metric("Average GB/hr", f"{avg_tp:.2f}")
    k2.metric("Average Files/hr", f"{avg_file_tp:,.0f}")
    k3.metric("Scans Performed", total_scans)
    k4.metric("Primary Data Source", primary_ds)
    
    st.divider()
    
    # Comparison with prior build if available
    idx = all_versions.index(selected_build)
    if idx < len(all_versions) - 1:
        prev_build = all_versions[idx+1]
        prev_df = df[df['Build_Version'].astype(str) == prev_build]
        prev_avg = prev_df['throughput_cluster_gb_hr'].mean()
        
        diff = avg_tp - prev_avg if pd.notnull(avg_tp) and pd.notnull(prev_avg) else 0
        pct = (diff / prev_avg * 100) if prev_avg else 0
        
        if diff != 0:
            st.info(f"✨ Compared to previous build `{prev_build}`, this build is **{abs(pct):.1f}% {'faster' if diff > 0 else 'slower'}** on average.")
    
    # Charts for this build
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Performance by Scan Type")
        fig = px.bar(build_df, x='Scan_Type', y='throughput_cluster_gb_hr', color='File_Set_Type',
                     barmode='group', title="Throughput (GB/hr) per Scan Category")
        st.plotly_chart(fig, width="stretch")
        
    with c2:
        st.subheader("Scanner Scaling Efficiency")
        # Throughput vs Scanner Count
        fig2 = px.scatter(build_df, x='scanner_count', y='throughput_cluster_gb_hr', 
                          size='Total_Size_GB', color='Scan_Type',
                          title="Throughput vs Scanners (Size = Volume)")
        st.plotly_chart(fig2, width="stretch")
        
    st.subheader("Individual Run Details")
    detail_df = build_df[['Scan_Type', 'Data_Source_Type', 'File_Set_Type', 'throughput_cluster_gb_hr', 'scanner_count', 'thread_count', 'is_acl']].copy()
    
    # Clean column names for display
    detail_df.columns = ["Scan Type", "Source", "File Set", "GB/hr", "Scanners", "Threads", "ACL"]
    render_centered_table(detail_df)

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
    st.caption("Compare **all runs** for a selected Build + File Set across **two Data Sources** (e.g. 247.7 + Small Files + S3 vs SMB).")

    src = st.radio(
        "Runs source",
        ["Auto Scans (recommended)", "Manual Benchmarks", "Both"],
        horizontal=True,
        index=0,
        key="build_ds_runs_source",
    )
    include_inactive = st.checkbox("Include inactive runs (manual only)", value=False, key="build_ds_runs_inactive")

    frames = []
    if src in {"Manual Benchmarks", "Both"}:
        m = load_data(include_inactive=include_inactive)
        if not m.empty:
            m = m.copy()
            m["_source"] = "manual"
            m["_run_key"] = "m:" + m.get("id").astype(str)
            frames.append(m)
    if src in {"Auto Scans (recommended)", "Both"}:
        a = load_auto_trend_as_manual_like_df()
        if not a.empty:
            a = a.copy()
            a["_source"] = "auto"
            a["_run_key"] = "a:" + a.get("scan_id", a.get("id")).astype(str)
            frames.append(a)

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        st.warning("No runs found for the selected source(s) yet.")
        return
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
        if len(s) > 20 or "scanner" in s.lower() or "thread" in s.lower() or " " in s:
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

    scan_type_options = _uniq_sorted(fs_df["Scan_Type"]) if not fs_df.empty else _uniq_sorted(ds_df["Scan_Type"])
    selected_scan_type = st.selectbox("Scan Type", ["(All)"] + scan_type_options)

    filtered = fs_df.copy() if not fs_df.empty else ds_df.copy()
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
    ensure_test_environments_table()
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
    out = out[(hours.notna()) & (hours > 0)]
    out["Build_Version"] = out["Build_Version"].apply(lambda x: x if str(x).strip() else "Unknown")
    return out


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

    submitted = st.button("Save Table Changes", type="primary", use_container_width=True)
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



def load_scan_progress_history(scan_id):
    if not engine or not scan_id:
        return pd.DataFrame()
    query = text("""
        SELECT
               timestamp AS captured_at_actual,
               captured_at_utc AS captured_at_slot_utc,
               scan_state,
               completed_parts, failed_parts, total_parts_scanned,
               objects_scanned, objects_failed,
               objects_with_pi,
               total_classifications, newly_added_classifications, false_removed_classifications
        FROM scan_progress
        WHERE scan_id = :sid
        ORDER BY COALESCE(captured_at_utc, timestamp) ASC
    """)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(query, conn, params={"sid": scan_id})
            return df
    except Exception:
        return pd.DataFrame()


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
    except Exception:
        db_name = ""
    display_name = db_name
    if display_name:
        st.caption(f"Scan: **{display_name}** | Scan ID: `{scan_id}`")
    else:
        st.caption(f"Scan ID: `{scan_id}`")

    # Manual sync on-demand (event-driven).
    # Uses the same DB schema/slotting logic as the tracker (10-minute captured_at_utc + dedupe).
    manual_sync = st.button("Sync now (manual)", type="primary")
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
                        state = str(si.get("state") or (sp.get("progress") or {}).get("state") or "").strip()
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

                    # Full normalized scan_insights + attributes delta map (for Add New Benchmark prefill)
                    sync_single_scan_insight(str(scan_id), sync_env_row)

                    st.success("Synced. Reloading…")
                    st.rerun()
                except Exception as ex:
                    st.error(f"Manual sync failed: {ex}")

    # Live metrics are intentionally not shown here (DB-only).

    # No auto-save here.

    # Load (possibly updated) history after any auto-save.
    df = load_scan_progress_history(scan_id)

    # No forced auto-refresh here.

    if df.empty:
        st.warning("No saved snapshots yet for this scan.")
        st.caption("Waiting for `tracker.py` to write snapshots (every 10 minutes).")

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
        st.caption(f"Latest snapshots: {joined}")

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
        if isinstance(age_min, (int, float)) and age_min >= 25 and (not _is_terminal_state(latest_state_for_scan)):
            extra = ""
            if isinstance(g_age, (int, float)):
                extra = f" (DB-wide latest age: {g_age:.1f} min)"
            st.warning(
                "Tracker looks stale for this scan (no new snapshots in 25+ minutes). "
                "If you expect updates every 10 minutes while the scan is running, the tracker is likely not running or is failing."
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
            st.caption(
                "Tracker heartbeat: "
                + (f"status=`{stt}` " if stt else "")
                + (f"started=`{s.strftime('%Y-%m-%d %H:%M UTC')}` " if s is not None and pd.notnull(s) else "")
                + (f"finished=`{f.strftime('%Y-%m-%d %H:%M UTC')}` " if f is not None and pd.notnull(f) else "")
            )
            if msg:
                st.caption(f"Tracker message: `{str(msg)[:200]}`")
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
    stall_minutes = None
    last_change_at = None
    completion_lag_minutes = None
    completion_lag_summary = ""
    false_removed_delta = None
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

            # First time we reached expected totals (objects)
            t_reach_files = None
            if expected_total_files and expected_total_files > 0 and "objects_scanned" in df_lag.columns:
                df_lag["objects_scanned_num"] = pd.to_numeric(df_lag["objects_scanned"], errors="coerce")
                reach_rows = df_lag[(df_lag["objects_scanned_num"] >= float(expected_total_files)) & df_lag["t"].notna()]
                if not reach_rows.empty:
                    t_reach_files = reach_rows.iloc[0]["t"]

            # First time parts appear complete (parts)
            t_reach_parts = None
            if "completed_parts" in df_lag.columns and "total_parts_scanned" in df_lag.columns:
                df_lag["completed_parts_num"] = pd.to_numeric(df_lag["completed_parts"], errors="coerce")
                df_lag["total_parts_num"] = pd.to_numeric(df_lag["total_parts_scanned"], errors="coerce")
                reach_p = df_lag[(df_lag["total_parts_num"] > 0) & (df_lag["completed_parts_num"] >= df_lag["total_parts_num"]) & df_lag["t"].notna()]
                if not reach_p.empty:
                    t_reach_parts = reach_p.iloc[0]["t"]

            # First terminal state time
            t_terminal = None
            term_rows = df_lag[(df_lag["state_c"].isin(terminal_states_local)) & df_lag["t"].notna()]
            if not term_rows.empty:
                t_terminal = term_rows.iloc[0]["t"]

            # Define "reached 100%" time as max of the available reach times (when both are satisfied).
            reach_candidates = [x for x in [t_reach_files, t_reach_parts] if x is not None and pd.notnull(x)]
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
        ds = str(exp.get("data_source_type") or "").strip()
        fs = str(exp.get("file_set_type") or "").strip()
        if ds or fs:
            bits.append(f"Scan looks like `{ds} {fs}` based on scan name.")
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

    with st.expander("🧠 Analysis (stats + narrative)", expanded=True):
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Objects scanned", f"{cur_objects:,}")
        m2.metric("Parts progress", f"{(pct_parts or 0):.1f}%" if pct_parts is not None else "N/A")
        m3.metric("Failed parts", f"{cur_failed_parts:,}")
        if false_removed_delta is not None:
            m4.metric("falseRemoved (Δ)", f"{false_removed_delta:+,}")
        else:
            m4.metric("falseRemoved (Δ)", "N/A")

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
            # IMPORTANT:
            # - Do not claim "finished" based on percent rounding (99.9%) or object-only stalls.
            # - Treat "done" only when parts are actually complete OR expected object total is reached.
            done_parts = bool(cur_parts_total and cur_parts_total > 0 and cur_parts_done >= cur_parts_total)
            done_objects = bool(expected_total_files and expected_total_files > 0 and cur_objects >= int(expected_total_files))

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
            scanning_done = bool(done_parts or done_objects)

            if is_running and stalled:
                extra = []
                extra.append(f"parts: {cur_parts_done:,} / {cur_parts_total:,}" if cur_parts_total else f"parts: {cur_parts_done:,}")
                extra.append(f"objects scanned: {cur_objects:,}")
                if expected_total_files and expected_total_files > 0:
                    extra.append(f"expected objects: {int(expected_total_files):,}")
                if no_progress_last3:
                    extra.append("no change in parts/objects across last snapshots")
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
    def _apply_snapshot_ticks(fig_obj, x_vals):
        """
        Avoid dense dtick labels across multi-hour/day gaps.
        Show a small set of snapshot times (evenly spaced).
        """
        try:
            xs = [x for x in list(x_vals) if x is not None and str(x) != "NaT"]
        except Exception:
            xs = []
        if not xs:
            return

        # Normalize to pandas timestamps for stable formatting/ordering
        try:
            xs = [pd.to_datetime(x) for x in xs]
        except Exception:
            pass
        try:
            xs = sorted(xs)
        except Exception:
            pass

        # Pick up to N evenly spaced tick labels (always include first+last).
        max_ticks = 6
        if len(xs) > max_ticks:
            try:
                import numpy as _np
                idx = _np.linspace(0, len(xs) - 1, num=max_ticks)
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

        # Use string tickvals so Plotly doesn't drop labels in tight layouts
        # (datetime tickvals can still be auto-thinned in small subplots).
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

    with c1:
        st.subheader("Parts (Totals)")
        plot_df = df.copy()
        plot_df["captured_at_utc"] = plot_df["captured_at_slot_utc"].dt.tz_convert("UTC").dt.tz_localize(None)
        plot_df = plot_df.sort_values("captured_at_utc")

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
        _apply_snapshot_ticks(fig, plot_df["captured_at_utc"])
        fig.update_yaxes(title_text="Count", rangemode="tozero")
        fig.update_layout(
            margin=dict(l=10, r=10, t=30, b=70),
            legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="left", x=0),
            hovermode="x unified",
        )
        st.plotly_chart(fig, width="stretch")

    with c2:
        st.subheader("Objects (Totals)")
        plot_df = df.copy()
        plot_df["captured_at_utc"] = plot_df["captured_at_slot_utc"].dt.tz_convert("UTC").dt.tz_localize(None)
        plot_df = plot_df.sort_values("captured_at_utc")
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
        _apply_snapshot_ticks(fig2, plot_df["captured_at_utc"])
        fig2.update_yaxes(title_text="Count", rangemode="tozero")
        fig2.update_layout(
            margin=dict(l=10, r=10, t=30, b=70),
            legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="left", x=0),
            hovermode="x unified",
        )
        st.plotly_chart(fig2, width="stretch")

    with c3:
        st.subheader("ObjectsWithPi + Classifications")
        plot_df = df.copy()
        plot_df["captured_at_utc"] = plot_df["captured_at_slot_utc"].dt.tz_convert("UTC").dt.tz_localize(None)
        plot_df = plot_df.sort_values("captured_at_utc")
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
        _apply_snapshot_ticks(fig3, plot_df["captured_at_utc"])
        fig3.update_yaxes(title_text="Count", rangemode="tozero")
        fig3.update_layout(
            margin=dict(l=10, r=10, t=30, b=70),
            legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="left", x=0),
            hovermode="x unified",
        )
        st.plotly_chart(fig3, width="stretch")

    st.subheader("Raw Progress Data")
    # Show newest first (includes scan_state, so terminal updates show here)
    show_df = df.copy()
    try:
        show_df = show_df.rename(columns={
            "captured_at_actual": "captured_at_actual_utc",
            "captured_at_slot_utc": "captured_at_slot_utc",
        })
    except Exception:
        pass
    st.dataframe(show_df.sort_values("captured_at_actual_utc", ascending=False), width="stretch")
    
    if st.button("Back to Scan Status"):
        st.query_params["page"] = "Scan Status"
        st.rerun()


def _build_default_workflow_steps(env1_id: Optional[int], env2_id: Optional[int]) -> List[Dict[str, Any]]:
    """
    UI plan (no side-effects). Backend runner will execute these sequentially.
    Alternates env1/env2 per step.
    """
    envs = [env1_id, env2_id]
    def _env_for_idx(i: int) -> Optional[int]:
        if not envs or (envs[0] is None and envs[1] is None):
            return None
        return envs[i % 2] if envs[i % 2] is not None else envs[(i + 1) % 2]

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
        stp2["environment_id"] = _env_for_idx(idx)
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


def page_workflow_automation():
    st.title("🧪 Workflow Automation")
    st.caption("UI-first: plan + pause/resume. A background runner will execute DS+scan creation sequentially.")

    ensure_test_environments_table()
    ensure_scan_tracking_tables()
    ensure_scan_progress_table()
    ensure_workflow_tables()

    env_df = load_test_environments()
    if env_df.empty:
        st.info("No test environments found. Add them in `Manage Test Environments` first.")
        return

    work_df = env_df.copy()
    work_df["is_active"] = work_df["is_active"].apply(lambda x: bool(x))
    active_df = work_df[work_df["is_active"] == True].copy()
    active_df = active_df.sort_values(by=["env_name"], ascending=[True]).reset_index(drop=True)
    if active_df.empty:
        st.warning("No active environments. Mark at least two environments active to run this workflow.")
        return

    # --- Create new workflow plan ---
    st.markdown("### Create Workflow Plan")
    c1, c2, c3 = st.columns([1.4, 1.4, 1.0])
    env_labels = [f"{r.env_name} (id={int(r.id)})" for r in active_df.itertuples()]
    env_ids = [int(r.id) for r in active_df.itertuples()]
    env1_label = c1.selectbox("Env 1 (first step)", env_labels, index=0, key="wf_env1")
    env2_label = c2.selectbox("Env 2 (second step)", env_labels, index=(1 if len(env_labels) > 1 else 0), key="wf_env2")
    cooldown = int(c3.number_input("Cooldown (minutes)", min_value=1, max_value=180, value=15, step=1, key="wf_cd"))
    env1_id = env_ids[env_labels.index(env1_label)]
    env2_id = env_ids[env_labels.index(env2_label)]
    wf_name = st.text_input("Workflow name", value=f"DS+Scan workflow {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}", key="wf_name")

    preview_steps = _build_default_workflow_steps(env1_id, env2_id)
    for stp in preview_steps or []:
        if isinstance(stp, dict) and "is_enabled" not in stp:
            stp["is_enabled"] = True
    with st.expander(f"Preview steps ({len(preview_steps or [])})", expanded=False):
        prev_df = pd.DataFrame(preview_steps)
        show_cols = ["is_enabled", "environment_id", "ds_kind", "ds_name", "scan_kind", "file_set_type", "is_acl", "is_structured_ff", "is_ols", "feature_flags", "notes", "status"]
        show_cols = [c for c in show_cols if c in prev_df.columns]
        editable_df = st.data_editor(
            prev_df[show_cols],
            width="stretch",
            hide_index=True,
            key="wf_preview_steps_editor",
            column_config={
                "is_enabled": st.column_config.CheckboxColumn("Execute", default=True),
            },
            disabled=[c for c in show_cols if c != "is_enabled"],
        )
        try:
            preview_steps = editable_df.to_dict("records")
        except Exception:
            pass

    if st.button("Create workflow plan", type="primary"):
        rid = create_workflow_run(
            wf_name,
            env1_id=int(env1_id),
            env2_id=int(env2_id),
            steps=preview_steps,
            cooldown_minutes=int(cooldown),
        )
        if rid:
            st.success(f"Created workflow run id={rid}.")
            st.query_params["page"] = "Workflow Automation"
            st.query_params["run_id"] = str(int(rid))
            try:
                st.cache_data.clear()
            except Exception:
                pass
            st.rerun()
        else:
            st.error("Failed to create workflow plan.")

    st.divider()

    # --- Existing runs ---
    st.markdown("### Workflow Runs")
    runs_df = load_workflow_runs(limit=50)
    if runs_df.empty:
        st.info("No workflow runs yet. Create one above.")
        return

    try:
        run_id_qp = str(st.query_params.get("run_id", "") or "").strip()
    except Exception:
        run_id_qp = ""
    run_options = []
    run_map = {}
    for r in runs_df.itertuples():
        rid = int(r.id)
        label = f"{rid} | {str(r.status or '').strip() or 'unknown'} | {str(r.name or '').strip()}"
        run_options.append(label)
        run_map[label] = rid
    default_idx = 0
    if run_id_qp.isdigit():
        want = int(run_id_qp)
        for i, lab in enumerate(run_options):
            if run_map.get(lab) == want:
                default_idx = i
                break
    selected_run_label = st.selectbox("Select run", run_options, index=default_idx, key="wf_run_sel")
    run_id = int(run_map[selected_run_label])

    # Controls
    b1, b2, b3, b4, b5 = st.columns([1.0, 1.0, 1.0, 1.2, 1.0])
    if b1.button("Start Workflow", type="primary"):
        try:
            with engine.connect() as conn:
                conn.execute(text("""
                    UPDATE workflow_runs
                    SET status = 'running',
                        started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                        next_eligible_at = COALESCE(next_eligible_at, CURRENT_TIMESTAMP)
                    WHERE id = :rid
                """), {"rid": int(run_id)})
                conn.commit()
            # Ensure background runner is started (so workflow continues unattended).
            try:
                if callable(ensure_workflow_runner_running):
                    ensure_workflow_runner_running(engine)
            except Exception:
                pass
            st.rerun()
        except Exception as ex:
            st.error(f"Start failed: {ex}")
    if b2.button("Pause"):
        set_workflow_run_status(int(run_id), "paused")
        st.rerun()
    if b3.button("Resume Workflow"):
        set_workflow_run_status(int(run_id), "running")
        try:
            if callable(ensure_workflow_runner_running):
                ensure_workflow_runner_running(engine)
        except Exception:
            pass
        st.rerun()
    if b5.button("Stop Workflow"):
        set_workflow_run_status(int(run_id), "stopped")
        try:
            with engine.connect() as conn:
                conn.execute(text("UPDATE workflow_runs SET finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP) WHERE id=:rid"), {"rid": int(run_id)})
                conn.commit()
        except Exception:
            pass
        st.rerun()
    if b4.button("Reconcile from DB"):
        res = reconcile_workflow_run(int(run_id))
        st.caption(f"Reconciled: updated_steps={res.get('updated_steps')} completed_run={res.get('completed_run')}")
        try:
            st.cache_data.clear()
        except Exception:
            pass
        st.rerun()

    # Run meta
    try:
        rrow = runs_df[runs_df["id"] == run_id].iloc[0].to_dict()
    except Exception:
        rrow = {}
    st.caption(
        f"Run id={run_id} | status={str(rrow.get('status') or '').strip() or 'unknown'}"
        + (f" | next_eligible_at={rrow.get('next_eligible_at')}" if rrow.get("next_eligible_at") else "")
        + (f" | cooldown={rrow.get('cooldown_minutes')}m" if rrow.get("cooldown_minutes") is not None else "")
    )
    if str(rrow.get("last_error") or "").strip():
        st.error(str(rrow.get("last_error") or "").strip())

    steps_df = load_workflow_steps(int(run_id))
    if steps_df.empty:
        st.info("No steps found for this run.")
        return

    # Convenience display columns
    disp = steps_df.copy()
    disp["Flags"] = disp.apply(
        lambda r: ", ".join([x for x in [
            "ACL" if bool(r.get("is_acl")) else "",
            "STRUCT" if bool(r.get("is_structured_ff")) else "",
            "OLS" if bool(r.get("is_ols")) else "",
        ] if x]),
        axis=1,
    )
    disp["Progress"] = disp["scan_id"].map(lambda sid: f"?page=Scan+Progress&scan_id={sid}" if str(sid or "").strip() else "")
    disp = disp.rename(columns={
        "seq": "Step",
        "environment": "Environment",
        "ds_kind": "DS Type",
        "ds_name": "DS Name",
        "scan_kind": "Scan Type",
        "file_set_type": "File Set",
        "status": "Step Status",
        "scan_id": "Scan Id",
        "ds_id": "DS Id",
        "notes": "Notes",
    })
    st.markdown("### Steps")
    cols = ["Step", "Environment", "DS Type", "DS Name", "Scan Type", "File Set", "Flags", "Step Status", "build_version", "build_prefix", "DS Id", "Scan Id", "Progress", "Notes", "last_error", "updated_at"]
    cols = [c for c in cols if c in disp.columns]
    st.dataframe(
        disp[cols],
        width="stretch",
        hide_index=True,
        column_config={
            "Progress": st.column_config.LinkColumn("Progress", display_text="View"),
        },
    )

    # Hide manual template editing in normal mode (workflow should be automatic).
    # If needed for troubleshooting, enable debug view via ?debug=true
    if st.query_params.get("debug") == "true":
        st.divider()
        st.markdown("### Templates (Debug)")
        st.caption("Debug-only: manual editing of workflow templates stored in DB.")

        # Pick a step to derive keys
        try:
            step_nums = [int(x) for x in steps_df["seq"].tolist() if str(x).strip()]
            step_nums = sorted(set(step_nums))
        except Exception:
            step_nums = list(range(1, 17))
        pick_step = st.selectbox("Select step to edit templates", step_nums, index=0, key="wf_tpl_step")
        try:
            step_row = steps_df[steps_df["seq"] == int(pick_step)].iloc[0].to_dict()
        except Exception:
            step_row = {}

        ds_key = _workflow_ds_template_key(step_row or {})
        scan_key = _workflow_scan_template_key(step_row or {})

        t1, t2, t3 = st.columns(3)
        with t1:
            st.markdown("**DS template**")
            st.code(ds_key)
            ds_existing = get_workflow_template_payload(ds_key)
            ds_text = st.text_area(
                "DS JSON (PUT /ds_connections/<name>)",
                value=ds_existing or "{\n  \"ds_connection\": {\n    \"name\": \"{{DS_NAME}}\",\n    \"type\": \"{{DS_TYPE}}\",\n    \"enabled\": true\n  }\n}\n",
                height=220,
                key="wf_ds_json",
            )
            if st.button("Save DS template", key="wf_save_ds"):
                upsert_workflow_template(ds_key, "ds", ds_text)
                st.success("Saved DS template.")
        with t2:
            st.markdown("**Scan template**")
            st.code(scan_key)
            scan_existing = get_workflow_template_payload(scan_key)
            scan_text = st.text_area(
                "Scan JSON (POST /scanProfiles)",
                value=scan_existing or "{\n  \"name\": \"{{SCAN_PROFILE_NAME}}\",\n  \"description\": \"{{NOTES}}\",\n  \"dataSourceList\": [\"{{DS_ID}}\"],\n  \"scanTemplateId\": \"{{SCAN_TEMPLATE_ID}}\",\n  \"isSingleRunScan\": true\n}\n",
                height=220,
                key="wf_scan_json",
            )
            if st.button("Save Scan template", key="wf_save_scan"):
                upsert_workflow_template(scan_key, "scan", scan_text)
                st.success("Saved Scan template.")
        with t3:
            st.markdown("**Pre-OLS action (FF change)**")
            st.caption("Executed automatically before OLS steps. Use env-specific key if needed.")
            action_key_global = "action::pre_ols"
            action_key_env = f"action::pre_ols::env={int(step_row.get('environment_id'))}" if step_row.get("environment_id") is not None else ""
            st.code(action_key_env or action_key_global)
            existing = get_workflow_template_payload(action_key_env) if action_key_env else ""
            if not existing:
                existing = get_workflow_template_payload(action_key_global)
            action_text = st.text_area(
                "Action JSON (method/path/body)",
                value=existing or "{\n  \"method\": \"POST\",\n  \"path\": \"/api/v1/REPLACE_ME\",\n  \"params\": {},\n  \"body\": {}\n}\n",
                height=220,
                key="wf_action_json",
            )
            if st.button("Save Pre-OLS action", key="wf_save_action"):
                upsert_workflow_template(action_key_env or action_key_global, "action", action_text)
                st.success("Saved Pre-OLS action template.")


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

    from urllib.parse import unquote_plus
    query_params = st.query_params
    if "page" in query_params:
        page_val = query_params["page"]
        if isinstance(page_val, list):
            page_val = page_val[0] if page_val else ""
        if isinstance(page_val, str) and page_val:
            normalized = unquote_plus(page_val)
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
                "Scan Progress": "Scan Progress"
            }
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
        </style>
    """, unsafe_allow_html=True)

    # Custom Flyout/Hover Menu (restored)
    logo_b64 = get_logo_base64()
    logo_html = f'<img src="data:image/png;base64,{logo_b64}" style="width: 70%; border-radius: 4px; margin-bottom: 6px;" alt="BigID logo" />' if logo_b64 else '<div style="color:#ffffff;font-weight:700;">BigID</div>'
    sidebar_html = """
        <style>
        /* Sidebar Font & Logo */
        [data-testid="stSidebar"] * {
            font-family: 'Inter', 'Roboto', 'Segoe UI', sans-serif !important;
        }
        .material-icons, .material-symbols-outlined {
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
        }
        .sidebar-header {
            text-align: left;
            padding-left: 10px;
            padding-bottom: 6px;
        }
        .sidebar-header h1 {
            color: #8B5CF6;
            margin-bottom: 0px;
            font-size: 1.2rem;
            font-weight: 700;
        }
        .sidebar-header p {
            font-size: 0.75rem !important;
        }
        
        [data-testid="stSidebar"] > div:first-child {
            overflow: visible !important;
        }
        [data-testid="stSidebarNav"] {
            display: none;
        }

        .nav-category {
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
        }
        .nav-category:hover {
            background-color: #f5f3ff;
            border-color: #8B5CF6;
            color: #8B5CF6 !important;
            z-index: 1000;
        }
        .nav-category b { 
            font-size: 0.9rem; 
            color: inherit;
        }
        
        .flyout-menu {
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
        }
        .flyout-menu::before {
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
        }

        .nav-category:hover .flyout-menu {
            display: block;
            animation: fadeIn 0.15s ease-out;
        }
        .flyout-menu:hover {
            display: block;
        }
        .nav-category::after {
            content: "";
            position: absolute;
            right: -28px;
            top: 0;
            width: 28px;
            height: 100%;
        }
        .flyout-menu {
            pointer-events: auto;
        }
        
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateX(-5px); }}
            to {{ opacity: 1; transform: translateX(0); }}
        }}

        .flyout-header {
            font-size: 0.6rem;
            font-weight: 800;
            color: #94a3b8 !important;
            text-transform: uppercase;
            margin-top: 4px;
            margin-bottom: 4px;
            letter-spacing: 1px;
            border-bottom: 1px solid #f1f5f9;
            padding-bottom: 2px;
        }
        
        .flyout-item {
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
        }
        .flyout-item:hover {
            background-color: #f1f5f9 !important;
            color: #8B5CF6 !important;
            border-color: #e2e8f0 !important;
        }
        .flyout-item span {
            color: #000000 !important;
        }
        .flyout-item:hover span {
            color: #8B5CF6 !important;
        }
        
        #MainMenu {visibility: hidden;}
        header {visibility: hidden;}
        </style>
        
        <div class="sidebar-header" style="text-align: left; padding: 6px 10px; background: rgba(255,255,255,0.05); border-radius: 10px; margin: 2px 8px 6px;">
            __LOGO__
            <p style="color: #e2e8f0; font-size: 0.8rem; margin: 0; font-weight: 600; padding-left: 2px;">Benchmark Explorer</p>
        </div>

        <div class="nav-category">
            <span style="font-size: 1rem;">📊</span> <b style="color: #ffffff !important; font-size: 0.9rem;">Standard Trends</b>
            <div class="flyout-menu">
                <div class="flyout-header">Trend Analysis</div>
                <a href="?page=Small+Files+Trend+Analysis" target="_self" class="flyout-item"><span>📄</span> <span>Small Files Trend</span></a>
                <a href="?page=Large+Files+Trend+Analysis" target="_self" class="flyout-item"><span>📦</span> <span>Large Files Trend</span></a>
                <a href="?page=Small+Files+OLS+Trend+Analysis" target="_self" class="flyout-item"><span>⚡</span> <span>Small Files OLS</span></a>
                <a href="?page=Large+Files+OLS+Trend+Analysis" target="_self" class="flyout-item"><span>⚡</span> <span>Large Files OLS</span></a>
            </div>
        </div>

        <div class="nav-category">
            <span style="font-size: 1rem;">⚖️</span> <b style="color: #ffffff !important; font-size: 0.9rem;">Expert Comparisons</b>
            <div class="flyout-menu">
                <div class="flyout-header">Head-to-Head</div>
                <a href="?page=Build+DS+Runs" target="_self" class="flyout-item"><span>🧪</span> <span>Build + DS Runs</span></a>
                <a href="?page=Data+Source+Trend+Comparison" target="_self" class="flyout-item"><span>📈</span> <span>DS Trend Comparison</span></a>
                <a href="?page=OLS+vs+Full+Scan+(Small)" target="_self" class="flyout-item"><span>⚖️</span> <span>OLS vs Full (Small)</span></a>
                <a href="?page=OLS+vs+Full+Scan+(Large)" target="_self" class="flyout-item"><span>⚖️</span> <span>OLS vs Full (Large)</span></a>
                <a href="?page=Discover+File+FF+On+vs+Off" target="_self" class="flyout-item"><span>⚙️</span> <span>Discover FF: On/Off</span></a>
                <a href="?page=ACL+ON+vs+OLS+(ACL+ON)" target="_self" class="flyout-item"><span>🔒</span> <span>ACL ON Comparison</span></a>
                <a href="?page=ACL+OFF+vs+OLS+(ACL+OFF)" target="_self" class="flyout-item"><span>🔓</span> <span>ACL OFF Comparison</span></a>
            </div>
        </div>

        <div class="nav-category">
            <span style="font-size: 1rem;">🛠️</span> <b style="color: #ffffff !important; font-size: 0.9rem;">System Tools</b>
            <div class="flyout-menu">
                <div class="flyout-header">Administration</div>
                <a href="?page=Run+Comparison+Tool" target="_self" class="flyout-item"><span>🔄</span> <span>Run Comparison Tool</span></a>
                <a href="?page=Classifier+Changes" target="_self" class="flyout-item"><span>🧬</span> <span>Classifier Comparison</span></a>
                <a href="?page=Add+New+Benchmark" target="_self" class="flyout-item"><span>➕</span> <span>Add New Data Record</span></a>
                <a href="?page=Edit+Records" target="_self" class="flyout-item"><span>🛠️</span> <span>Edit Existing Records</span></a>
                <a href="?page=Manage+Test+Environments" target="_self" class="flyout-item"><span>🌐</span> <span>Manage Test Environments</span></a>
                <a href="?page=Version+Comparison" target="_self" class="flyout-item"><span>🧬</span> <span>Version Comparison</span></a>
                <a href="?page=Workflow+Automation" target="_self" class="flyout-item"><span>🧪</span> <span>Workflow Automation</span></a>
                <a href="?page=Create+Data+Source" target="_self" class="flyout-item"><span>🧩</span> <span>Create Data Source</span></a>
                <a href="?page=Scan+Status" target="_self" class="flyout-item"><span>🛰️</span> <span>Scan Status</span></a>
                <a href="?page=Scan+Progress" target="_self" class="flyout-item"><span>📈</span> <span>Scan Progress</span></a>
                <div class="flyout-header" style="margin-top: 10px;">Archived / Disabled</div>
                <a href="#" class="flyout-item" style="opacity: 0.5; pointer-events: none; color: #94a3b8 !important;"><span>📁</span> <span>Overview Dashboard (Off)</span></a>
                <a href="#" class="flyout-item" style="opacity: 0.5; pointer-events: none; color: #94a3b8 !important;"><span>📉</span> <span>Build Analysis (Off)</span></a>
            </div>
        </div>
    """
    st.sidebar.markdown(sidebar_html.replace("__LOGO__", logo_html), unsafe_allow_html=True)
    
    st.sidebar.markdown("<br><br>", unsafe_allow_html=True)
    if st.sidebar.button("Clear Cache & Refresh Data", width="stretch"): 
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

if __name__ == "__main__":
    main()
