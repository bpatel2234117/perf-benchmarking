import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import hashlib
import base64
import numpy as np
import requests
import os
from textwrap import dedent
import html
import re
from pathlib import Path
from db_config import DB_CONFIG, DB_CONNECTION_STRING
from bigid_auth import Get_auth_token, get_saved_environment_credentials, save_environment_credentials

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

def load_data(include_inactive=False):
    if not engine: return pd.DataFrame()
    where_clause = "" if include_inactive else "WHERE b.is_active = 1"
    # Ensure optional columns exist (non-breaking for existing DBs)
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE benchmarks ADD COLUMN scenario_type VARCHAR(100)"))
            conn.commit()
    except Exception:
        pass
    # Join Tables for Flat View
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
            b.total_catalog_items as Total_Catalog_Items,
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
            for idx, row in df.iterrows():
                sc = row.get('scanner_count')
                tc = row.get('thread_count')
                # GB/hr
                if pd.notnull(row.get('throughput_cluster_gb_hr')) and pd.notnull(sc) and sc > 0:
                    df.at[idx, 'throughput_scanner_gb_hr'] = row['throughput_cluster_gb_hr'] / sc
                if pd.notnull(row.get('throughput_cluster_gb_hr')) and pd.notnull(tc) and tc > 0:
                    df.at[idx, 'throughput_thread_gb_hr'] = row['throughput_cluster_gb_hr'] / tc
                # Files/hr
                if pd.notnull(row.get('throughput_cluster_files_hr')) and pd.notnull(sc) and sc > 0:
                    df.at[idx, 'throughput_scanner_files_hr'] = row['throughput_cluster_files_hr'] / sc
                if pd.notnull(row.get('throughput_cluster_files_hr')) and pd.notnull(tc) and tc > 0:
                    df.at[idx, 'throughput_thread_files_hr'] = row['throughput_cluster_files_hr'] / tc
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
        filtered_df = filtered_df.groupby(['Build_Version', 'Data_Source_Type'], as_index=False).apply(select_best_run).reset_index(drop=True)

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
            st.plotly_chart(fig, use_container_width=True)

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
            st.plotly_chart(fig2, use_container_width=True)

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
    inserted = 0
    with engine.connect() as conn:
        for name in classifier_names:
            clean = str(name).strip()
            if not clean or re.match(r"^\d+\s+Classifiers\s+from\s+Global\s+list$", clean, flags=re.IGNORECASE):
                continue
            exists = conn.execute(text("SELECT id FROM classifiers WHERE name = :n"), {"n": clean}).fetchone()
            if not exists:
                conn.execute(text("INSERT INTO classifiers (name) VALUES (:n)"), {"n": clean})
                inserted += 1
        conn.commit()
    return inserted


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

    new_classifiers = 0
    with engine.connect() as conn:
        classifier_ids = []
        for name in sorted(cleaned, key=lambda x: x.lower()):
            row = conn.execute(text("SELECT id FROM classifiers WHERE name = :n"), {"n": name}).fetchone()
            if row:
                classifier_ids.append(int(row[0]))
            else:
                conn.execute(text("INSERT INTO classifiers (name) VALUES (:n)"), {"n": name})
                new_classifiers += 1
                row2 = conn.execute(text("SELECT id FROM classifiers WHERE name = :n"), {"n": name}).fetchone()
                classifier_ids.append(int(row2[0]))

        classifier_ids = sorted(set(classifier_ids))
        set_hash = hashlib.md5(",".join(str(i) for i in classifier_ids).encode()).hexdigest()

        set_row = conn.execute(text("SELECT id FROM classifier_sets WHERE set_hash = :h"), {"h": set_hash}).fetchone()
        if set_row:
            set_id = int(set_row[0])
        else:
            conn.execute(
                text("INSERT INTO classifier_sets (set_hash, classifier_count) VALUES (:h, :c)"),
                {"h": set_hash, "c": len(classifier_ids)}
            )
            set_id = int(conn.execute(text("SELECT id FROM classifier_sets WHERE set_hash = :h"), {"h": set_hash}).fetchone()[0])

        for cid in classifier_ids:
            conn.execute(
                text("""
                    INSERT IGNORE INTO classifier_set_members (set_id, classifier_id)
                    VALUES (:s, :c)
                """),
                {"s": set_id, "c": cid}
            )
        conn.execute(
            text("UPDATE classifier_sets SET classifier_count = :c WHERE id = :s"),
            {"c": len(classifier_ids), "s": set_id}
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

    new_classifiers = 0
    new_links = 0
    with engine.connect() as conn:
        build_id = get_or_create_id(conn, "builds", "version", build_version)
        env_id = int(environment_id) if environment_id is not None else None
        for name in cleaned:
            row = conn.execute(text("SELECT id FROM classifiers WHERE name = :n"), {"n": name}).fetchone()
            if row:
                classifier_id = int(row[0])
            else:
                conn.execute(text("INSERT INTO classifiers (name) VALUES (:n)"), {"n": name})
                new_classifiers += 1
                classifier_id = int(conn.execute(text("SELECT id FROM classifiers WHERE name = :n"), {"n": name}).fetchone()[0])

            if env_id is None:
                link = conn.execute(
                    text("""
                        SELECT id
                        FROM build_classifier_mappings
                        WHERE build_id = :b AND classifier_id = :c AND environment_id IS NULL
                    """),
                    {"b": int(build_id), "c": int(classifier_id)}
                ).fetchone()
            else:
                link = conn.execute(
                    text("""
                        SELECT id
                        FROM build_classifier_mappings
                        WHERE build_id = :b AND classifier_id = :c AND environment_id = :e
                    """),
                    {"b": int(build_id), "c": int(classifier_id), "e": env_id}
                ).fetchone()

            if link:
                conn.execute(
                    text("""
                        UPDATE build_classifier_mappings
                        SET last_synced_at = CURRENT_TIMESTAMP
                        WHERE id = :id
                    """),
                    {"id": int(link[0])}
                )
            else:
                conn.execute(
                    text("""
                        INSERT INTO build_classifier_mappings (build_id, classifier_id, environment_id)
                        VALUES (:b, :c, :e)
                    """),
                    {"b": int(build_id), "c": int(classifier_id), "e": env_id}
                )
                new_links += 1

        if env_id is None:
            total_links = int(conn.execute(
                text("SELECT COUNT(*) FROM build_classifier_mappings WHERE build_id = :b"),
                {"b": int(build_id)}
            ).fetchone()[0])
        else:
            total_links = int(conn.execute(
                text("SELECT COUNT(*) FROM build_classifier_mappings WHERE build_id = :b AND environment_id = :e"),
                {"b": int(build_id), "e": env_id}
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
    inserted = 0
    with engine.connect() as conn:
        b_row = conn.execute(text("SELECT id FROM builds WHERE version = :v"), {"v": str(build_version)}).fetchone()
        if not b_row:
            return 0
        build_id = int(b_row[0])

        src_rows = conn.execute(text("""
            SELECT DISTINCT classifier_id
            FROM build_classifier_mappings
            WHERE build_id = :b
              AND classifier_id IS NOT NULL
        """), {"b": build_id}).fetchall()

        for (classifier_id,) in src_rows:
            exists = conn.execute(text("""
                SELECT id
                FROM build_classifier_mappings
                WHERE build_id = :b AND classifier_id = :c AND environment_id = :e
                LIMIT 1
            """), {"b": build_id, "c": int(classifier_id), "e": int(environment_id)}).fetchone()
            if exists:
                continue
            conn.execute(text("""
                INSERT INTO build_classifier_mappings (build_id, classifier_id, environment_id)
                VALUES (:b, :c, :e)
            """), {"b": build_id, "c": int(classifier_id), "e": int(environment_id)})
            inserted += 1
        conn.commit()
    return inserted

def page_add_benchmark():
    st.title("➕ Add New Benchmark Run")
    st.markdown("Enter details below. This will populate the **normalized** tables (`builds`, `configs`, `classifiers`, `benchmarks`).")
    if st.session_state.get("benchmark_saved"):
        st.success("✅ Benchmark saved successfully.")
        st.session_state["benchmark_saved"] = False

    # 1. Build Info
    st.subheader("1. Build Information")
    c1, c2 = st.columns(2)
    build_ver = c1.text_input("Build Version (e.g. 218.117)", help="Required")
    
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
    
    is_acl = c5.checkbox("ACL Enabled")
    is_ff = c6.checkbox("Structured Discovery FF")
    
    feature_flags = st.text_input("Additional Feature Flags (comma-separated)")
    comment = st.text_area("Configuration Comment (Optional)")

    # 4. Run Metrics (dynamic)
    st.subheader("4. Run Metrics")
    r1_c1, r1_c2, r1_c3 = st.columns(3)
    run_date = r1_c1.date_input("Run Date")
    scenario_type = r1_c2.text_input("Scenario Type", help="Describe the scenario being tested")
    scan_type = r1_c3.selectbox("Scan Type", ["Full Scan", "Differential Scan", "OLS Scan", "Incremental Scan", "Other"])
    ds_options = get_data_source_types()
    if not ds_options:
        ds_options = ["S3", "SMB", "MSSQL", "Oracle", "NFS", "Unknown"]
    def reset_file_fields():
        for k in ["file_set_structured", "file_set_unstructured", "total_rows", "total_columns", "total_files", "total_folders"]:
            if k in st.session_state:
                del st.session_state[k]

    c_ds, c_dt, c_fs = st.columns(3)
    with c_ds:
        ds_type_choice = st.selectbox("Data Source Type", ds_options + ["Other (custom)"])
    with c_dt:
        data_type = st.selectbox(
            "Data Type",
            ["Structured", "Unstructured", "Mixed"],
            key="data_type",
            on_change=reset_file_fields
        )
    fs_names, fs_details = get_file_set_types(data_type)
    with c_fs:
        if data_type == "Structured":
            fs_defaults = ["Small Tables", "Large Tables", "Mixed Tables"]
            fs_options = sorted(set(fs_names + fs_defaults))
            file_set_choice = st.selectbox("File Set Type", fs_options + ["Other (custom)"], key="file_set_structured")
        else:
            fs_defaults = ["Small Files", "Large Files", "Mixed Files", "CSV Files"]
            fs_options = sorted(set(fs_names + fs_defaults))
            file_set_choice = st.selectbox("File Set Type", fs_options + ["Other (custom)"], key="file_set_unstructured")
    custom_ds_type = None
    if ds_type_choice == "Other (custom)":
        custom_ds_type = st.text_input("Custom Data Source Type")
    ds_type = custom_ds_type.strip() if custom_ds_type else ds_type_choice
    custom_fs = st.text_input("Custom File Set Type", key="custom_file_set_type") if file_set_choice == "Other (custom)" else ""
    file_set_type = custom_fs.strip() if custom_fs.strip() else file_set_choice

    preset = fs_details.get(file_set_type, {})
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
    total_catalog_items = 0

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
        st.caption("Select a classifier and enter findings count. Add rows as needed.")
        classifier_options = get_classifier_names_for_build(build_ver)
        if build_ver and not classifier_options:
            st.info(f"No classifiers found for build `{build_ver.strip()}` yet. Sync/fetch classifiers for this build first.")
        elif not build_ver.strip():
            st.info("Enter Build Version to load build-specific classifiers.")
        findings_df = st.data_editor(
            pd.DataFrame([{"Classifier": classifier_options[0] if classifier_options else "", "Findings": 0}]),
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "Classifier": st.column_config.SelectboxColumn("Classifier", options=classifier_options),
                "Findings": st.column_config.NumberColumn("Findings", min_value=0, step=1)
            }
        )
        findings = []
        for _, row in findings_df.iterrows():
            name = str(row.get("Classifier", "")).strip()
            count = row.get("Findings", None)
            if name:
                try:
                    count_val = int(count) if count is not None else 0
                except Exception:
                    count_val = 0
                findings.append((name, count_val))

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

                    ins_bench = text("""
                        INSERT INTO benchmarks 
                        (build_id, config_id, classifier_id, classifier_list, run_date, scenario_type, scan_type, data_source_type, data_type, file_set_type,
                         total_files, total_folders, total_size_gb, total_catalog_items, avg_file_size, 
                         scan_start_time, scan_end_time, enum_start_time, enum_end_time,
                         total_scan_hours, enumeration_hours,
                         throughput_cluster_gb_hr, throughput_scanner_gb_hr, throughput_thread_gb_hr,
                         throughput_cluster_files_hr, throughput_scanner_files_hr, throughput_thread_files_hr)
                        VALUES (:bid, :cid, :clid, :clist, :dt, :scn, :st, :dst, :dtype, :fstype,
                                :tf, :tfo, :tgb, :tci, :asz,
                                :ss, :se, :es, :ee, :tsh, :eh,
                                :tpcgb, :tpsgb, :tptgb, :tpcf, :tpsf, :tptf)
                    """)
                    
                    result = conn.execute(ins_bench, {
                        'bid': b_id, 'cid': conf_id, 'clid': c_id,
                        'clist': classifier_list_str,
                        'dt': str(run_date), 'scn': scenario_type, 'st': scan_type, 'dst': ds_type, 'dtype': data_type, 'fstype': file_set_type,
                        'tf': total_files, 'tfo': total_folders, 'tgb': total_gb, 'tci': total_catalog_items, 'asz': avg_size,
                        'ss': scan_start, 'se': scan_end, 
                        'es': enum_start, 'ee': enum_end,
                        'tsh': scan_duration, 'eh': enum_duration,
                        'tpcgb': tp_cluster_gb, 'tpsgb': tp_scanner_gb, 'tptgb': tp_thread_gb,
                        'tpcf': tp_cluster_files, 'tpsf': tp_scanner_files, 'tptf': tp_thread_files
                    })
                    bench_id = result.lastrowid
                    
                    if findings and bench_id:
                        ins_findings = text("""
                            INSERT INTO benchmark_findings (benchmark_id, classifier_name, finding_count)
                            VALUES (:bid, :cname, :cnt)
                        """)
                        for cname, cnt in findings:
                            conn.execute(ins_findings, {'bid': bench_id, 'cname': cname, 'cnt': cnt})
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
        st.plotly_chart(fig, use_container_width=True)
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
        st.dataframe(pd.DataFrame({"Classifier": added}) if added else pd.DataFrame({"Classifier": []}), use_container_width=True)
    with t2:
        st.markdown("**Removed from Baseline**")
        st.dataframe(pd.DataFrame({"Classifier": removed}) if removed else pd.DataFrame({"Classifier": []}), use_container_width=True)
    with t3:
        st.markdown("**Common**")
        st.dataframe(pd.DataFrame({"Classifier": common}) if common else pd.DataFrame({"Classifier": []}), use_container_width=True)

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
        st.plotly_chart(fig, use_container_width=True)
        
    with c2:
        st.subheader("Scanner Scaling Efficiency")
        # Throughput vs Scanner Count
        fig2 = px.scatter(build_df, x='scanner_count', y='throughput_cluster_gb_hr', 
                          size='Total_Size_GB', color='Scan_Type',
                          title="Throughput vs Scanners (Size = Volume)")
        st.plotly_chart(fig2, use_container_width=True)
        
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
    
    # Two columns for A vs B selection
    c1, c2 = st.columns(2)
    
    with c1:
        st.markdown("#### 🅰️ Baseline Configuration")
        # keep explicit values visible (after selections)
        
        ver_a = st.selectbox("Build Version", df['Build_Version'].unique(), key='v_a')
        
        # Get available options based on version A
        df_a_filtered = df[df['Build_Version'] == ver_a]
        
        ds_a = st.selectbox("Data Source", sorted(df['Data_Source_Type'].unique()), key='d_a')
        df_a_filtered = df_a_filtered[df_a_filtered['Data_Source_Type'] == ds_a]
        
        scan_a = st.selectbox("Scan Type", df_a_filtered['Scan_Type'].unique(), key='s_a')
        df_a_filtered = df_a_filtered[df_a_filtered['Scan_Type'] == scan_a]

        fs_a = st.selectbox("File Set Type", df['file_set_type'].unique(), key='fs_a')
        df_a_filtered = df_a_filtered[df_a_filtered['file_set_type'] == fs_a]
        
        # Configuration flags
        st.caption("Configuration Flags (Optional)")
        acl_a = st.selectbox("ACL Filter", ["All", "ACL On", "ACL Off"], key='acl_a')
        ocr_a = st.selectbox("OCR Filter", ["All", "OCR On", "OCR Off"], key='ocr_a')
        ff_a = st.selectbox("FF Filter", ["All", "FF On", "FF Off"], key='ff_a')
        # keep explicit values visible
        
    with c2:
        st.markdown("#### 🅱️ Candidate Configuration")
        # keep explicit values visible (after selections)
        
        ver_b = st.selectbox("Build Version", df['Build_Version'].unique(), key='v_b', 
                            index=min(1, len(df['Build_Version'].unique())-1))
        
        df_b_filtered = df[df['Build_Version'] == ver_b]
        
        ds_b = st.selectbox("Data Source", sorted(df['Data_Source_Type'].unique()), key='d_b')
        df_b_filtered = df_b_filtered[df_b_filtered['Data_Source_Type'] == ds_b]
        
        scan_b = st.selectbox("Scan Type", df_b_filtered['Scan_Type'].unique(), key='s_b')
        df_b_filtered = df_b_filtered[df_b_filtered['Scan_Type'] == scan_b]

        fs_b = st.selectbox("File Set Type", df['file_set_type'].unique(), key='fs_b')
        df_b_filtered = df_b_filtered[df_b_filtered['file_set_type'] == fs_b]
        
        st.caption("Configuration Flags (Optional)")
        acl_b = st.selectbox("ACL Filter", ["All", "ACL On", "ACL Off"], key='acl_b')
        ocr_b = st.selectbox("OCR Filter", ["All", "OCR On", "OCR Off"], key='ocr_b')
        ff_b = st.selectbox("FF Filter", ["All", "FF On", "FF Off"], key='ff_b')
        # keep explicit values visible

    # Apply filters
    run_a = df[(df['Build_Version'] == ver_a) & (df['Data_Source_Type'] == ds_a) & (df['Scan_Type'] == scan_a) & (df['file_set_type'] == fs_a)]
    run_b = df[(df['Build_Version'] == ver_b) & (df['Data_Source_Type'] == ds_b) & (df['Scan_Type'] == scan_b) & (df['file_set_type'] == fs_b)]
    
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
    fmt_g = lambda x: f"{x:.2f}GB" if x and not pd.isna(x) else "N/A"

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
              f"{rb['Total_Size_GB']:.2f}" if rb['Total_Size_GB'] else "N/A",
              f"{(rb['Total_Size_GB'] or 0) - (ra['Total_Size_GB'] or 0):+.2f}")
    
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
        st.plotly_chart(fig_line_gb, use_container_width=True)
    with l2:
        line_df_f = pd.DataFrame({
            "Configuration": ["Baseline", "Candidate"],
            "Files/hr": [files_a, files_b]
        })
        fig_line_f = px.line(line_df_f, x="Configuration", y="Files/hr", markers=True, title="File Throughput (Files/hr)")
        fig_line_f.update_traces(line=dict(color="#10B981", width=3))
        st.plotly_chart(fig_line_f, use_container_width=True)

    # Summary table
    st.markdown("### Summary Table")
    summary_df = pd.DataFrame({
        "Metric": ["Data Throughput (GB/hr)", "File Throughput (Files/hr)", "Total Files", "Total Size (GB)", "Scan Hours"],
        "Baseline": [
            f"{tp_a:.2f}",
            f"{files_a:,.0f}",
            f"{ra['Total_Files']:,.0f}" if ra['Total_Files'] else "N/A",
            f"{ra['Total_Size_GB']:.2f}" if ra['Total_Size_GB'] else "N/A",
            f"{ra.get('total_scan_hours', 0):.2f}" if pd.notnull(ra.get('total_scan_hours', None)) else "N/A"
        ],
        "Candidate": [
            f"{tp_b:.2f}",
            f"{files_b:,.0f}",
            f"{rb['Total_Files']:,.0f}" if rb['Total_Files'] else "N/A",
            f"{rb['Total_Size_GB']:.2f}" if rb['Total_Size_GB'] else "N/A",
            f"{rb.get('total_scan_hours', 0):.2f}" if pd.notnull(rb.get('total_scan_hours', None)) else "N/A"
        ]
    })
    st.dataframe(summary_df, use_container_width=True, hide_index=True)
    
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
        st.plotly_chart(fig1, use_container_width=True)
    
    with chart_col2:
        fig2 = px.bar(chart_data, x='Configuration', y='Files/hr', 
                     title="File Throughput Comparison",
                     color='Configuration',
                     color_discrete_sequence=['#8B5CF6', '#6366F1'])
        st.plotly_chart(fig2, use_container_width=True)
    
    # Winner determination
    st.divider()
    if pct_tp > 5:
        st.success(f"🏆 **Candidate is {pct_tp:.1f}% faster** in data throughput!")
    elif pct_tp < -5:
        st.warning(f"⚠️ **Baseline is {abs(pct_tp):.1f}% faster** in data throughput!")
    else:
        st.info("ℹ️ **Performance is comparable** (within 5% difference)")

# --- Trend Analysis (Multi-Build Comparison) ---
def render_trend_analysis(df, title, file_set_filter=None, scan_type_filter=None, compare_by=None, acl_filter=None, ff_filter=None):
    st.markdown(f"## 📈 {title}")
    st.write("") # Added space before filters
    
    if df.empty:
        st.warning("No data available.")
        return
    
    # Base filtering
    df_filtered = df.copy()
    if file_set_filter:
        df_filtered = df_filtered[df_filtered['file_set_type'] == file_set_filter]
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
    if not compare_by:
        col1, col2, col3, col4, col5 = st.columns(5)
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
            filter_ocr = st.checkbox("OCR Only", key=f"ocr_{title}")
        with col5:
            filter_ff = st.checkbox("FF Only", key=f"ff_{title}")
        if filter_acl:
            df_filtered = df_filtered[df_filtered['is_acl'] == 1]
        if filter_ocr:
            df_filtered = df_filtered[df_filtered['is_ocr'] == 1]
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
    
    plot_df = df_filtered.groupby(group_cols, as_index=False).apply(select_best_run).reset_index(drop=True)
    plot_df = plot_df.sort_values('Build_Version')
    # Drop rows with missing cluster throughput
    if 'throughput_cluster_gb_hr' in plot_df.columns:
        plot_df = plot_df[plot_df['throughput_cluster_gb_hr'].notna()]

    # Charts
    t_col1, t_col2 = st.columns(2)
    
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
            fig1.add_trace(go.Scatter(x=plot_df['Build_Version'], y=plot_df['throughput_scanner_gb_hr'], mode='lines+markers', name='Per Scanner', line=dict(color='#6366F1', width=2), marker=dict(size=8)))
            fig1.add_trace(go.Scatter(x=plot_df['Build_Version'], y=plot_df['throughput_thread_gb_hr'], mode='lines+markers', name='Per Thread', line=dict(color='#A78BFA', width=2), marker=dict(size=8)))
        
        fig1.update_layout(title="Data Throughput Trend (GB/hr)", xaxis_title="Build Version", yaxis_title="GB/hour", hovermode='x unified', legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        fig1.update_xaxes(type='category', categoryorder='category ascending')
        st.plotly_chart(fig1, use_container_width=True)

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
            fig2.add_trace(go.Scatter(x=plot_df['Build_Version'], y=plot_df['throughput_scanner_files_hr'], mode='lines+markers', name='Per Scanner', line=dict(color='#34D399', width=2), marker=dict(size=8)))
            fig2.add_trace(go.Scatter(x=plot_df['Build_Version'], y=plot_df['throughput_thread_files_hr'], mode='lines+markers', name='Per Thread', line=dict(color='#6EE7B7', width=2), marker=dict(size=8)))
        
        fig2.update_layout(title="File Throughput Trend (files/hr)", xaxis_title="Build Version", yaxis_title="Files/hour", hovermode='x unified', legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        fig2.update_xaxes(type='category', categoryorder='category ascending')
        st.plotly_chart(fig2, use_container_width=True)

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
            fig3.add_trace(go.Bar(x=plot_df['Build_Version'], y=plot_df['throughput_scanner_gb_hr'], name='Per Scanner', marker_color='#6366F1'))
            fig3.add_trace(go.Bar(x=plot_df['Build_Version'], y=plot_df['throughput_thread_gb_hr'], name='Per Thread', marker_color='#A78BFA'))
        
        fig3.update_layout(title="Data Throughput Comparison", barmode='group', xaxis={'type': 'category', 'categoryorder': 'category ascending'}, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        st.plotly_chart(fig3, use_container_width=True)

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
            fig4.add_trace(go.Bar(x=plot_df['Build_Version'], y=plot_df['throughput_scanner_files_hr'], name='Per Scanner', marker_color='#34D399'))
            fig4.add_trace(go.Bar(x=plot_df['Build_Version'], y=plot_df['throughput_thread_files_hr'], name='Per Thread', marker_color='#6EE7B7'))
        
        fig4.update_layout(title="File Throughput Comparison", barmode='group', xaxis={'type': 'category', 'categoryorder': 'category ascending'}, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        st.plotly_chart(fig4, use_container_width=True)

    # Detailed Table
    st.markdown("### Summary Data")
    # Filter for active rows (extra safety layer)
    plot_df = plot_df[plot_df['is_active'] == 1]
    
    # Select all throughput columns + identifier columns
    cols_to_show = [
        'Build_Version', 'Scan_Type', 
        'throughput_cluster_gb_hr', 'throughput_cluster_files_hr',
        'throughput_scanner_gb_hr', 'throughput_scanner_files_hr',
        'throughput_thread_gb_hr', 'throughput_thread_files_hr',
        'scanner_count', 'thread_count', 'total_scan_hours'
    ]
    
    # Add comparison column if present
    if compare_by and compare_by not in cols_to_show:
        cols_to_show.append(compare_by)
        
    disp_df = plot_df[cols_to_show].copy()
    
    # Define display names for columns
    display_names = {
        "Build_Version": "Build",
        "Scan_Type": "Scan Type",
        "throughput_cluster_gb_hr": "Cluster GB/hr",
        "throughput_cluster_files_hr": "Cluster Files/hr",
        "throughput_scanner_gb_hr": "Scanner GB/hr",
        "throughput_scanner_files_hr": "Scanner Files/hr",
        "throughput_thread_gb_hr": "Thread GB/hr",
        "throughput_thread_files_hr": "Thread Files/hr",
        "scanner_count": "Scanners",
        "thread_count": "Threads",
        "total_scan_hours": "Scan Hours",
        "is_feature_flags": "FF Enabled",
        "is_acl": "ACL Enabled"
    }
    
    # Rename columns and format numeric values for HTML display
    disp_df = disp_df.rename(columns=display_names)
    
    # Apply numeric formatting to the DataFrame
    num_cols = ["Cluster GB/hr", "Scanner GB/hr", "Thread GB/hr", "Thread Files/hr", "Scan Hours"]
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
            
    int_cols = ["Cluster Files/hr", "Scanner Files/hr", "Scanners", "Threads"]
    for col in int_cols:
        if col in disp_df.columns:
            disp_df[col] = disp_df[col].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "N/A")

    render_centered_table(disp_df)


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
        'Total_Catalog_Items',
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
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        disabled=[c for c in disabled_cols if c in table_df.columns],
        column_config={
            "Delete": st.column_config.CheckboxColumn("Delete"),
            "Date": st.column_config.DateColumn("Run Date"),
            "Total_Files": st.column_config.NumberColumn("Total Files", min_value=0, step=1000),
            "Total_Folders": st.column_config.NumberColumn("Total Folders", min_value=0, step=100),
            "Total_Size_GB": st.column_config.NumberColumn("Total Size (GB)", min_value=0.0, step=0.1, format="%.2f"),
            "Total_Catalog_Items": st.column_config.NumberColumn("Total Catalog Items", min_value=0, step=1000),
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
                'Total_Files', 'Total_Folders', 'Total_Size_GB', 'Total_Catalog_Items', 'avg_file_size',
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
                        'tci': clean_value(row.get('Total_Catalog_Items')),
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


def render_centered_table(df):
    """Renders a scrollable HTML table. Styling is handled via global CSS."""
    html = df.to_html(index=False, classes='centered-table')
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
                "Manage Test Environments": "Manage Test Environments",
                "Version Comparison": "Version Comparison"
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
                <a href="?page=Run+Comparison+Tool" target="_self" class="flyout-item"><span>🔄</span> <span>Run Head-to-Head</span></a>
                <a href="?page=Add+New+Benchmark" target="_self" class="flyout-item"><span>➕</span> <span>Add New Data Record</span></a>
                <a href="?page=Edit+Records" target="_self" class="flyout-item"><span>🛠️</span> <span>Edit Existing Records</span></a>
                <a href="?page=Manage+Classifiers" target="_self" class="flyout-item"><span>🧩</span> <span>Manage Classifiers</span></a>
                <a href="?page=Manage+Test+Environments" target="_self" class="flyout-item"><span>🌐</span> <span>Manage Test Environments</span></a>
                <a href="?page=Version+Comparison" target="_self" class="flyout-item"><span>🧬</span> <span>Version Comparison</span></a>
                <div class="flyout-header" style="margin-top: 10px;">Archived / Disabled</div>
                <a href="#" class="flyout-item" style="opacity: 0.5; pointer-events: none; color: #94a3b8 !important;"><span>📁</span> <span>Overview Dashboard (Off)</span></a>
                <a href="#" class="flyout-item" style="opacity: 0.5; pointer-events: none; color: #94a3b8 !important;"><span>📉</span> <span>Build Analysis (Off)</span></a>
            </div>
        </div>
    """
    st.sidebar.markdown(sidebar_html.replace("__LOGO__", logo_html), unsafe_allow_html=True)
    
    st.sidebar.markdown("<br><br>", unsafe_allow_html=True)
    if st.sidebar.button("Clear Cache & Refresh Data", use_container_width=True): 
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
    elif page == "Small Files Trend Analysis":
        render_trend_analysis(data, "Small Files Trend Analysis", file_set_filter="Small Files")
    elif page == "Large Files Trend Analysis":
        render_trend_analysis(data, "Large Files Trend Analysis", file_set_filter="Large Files")
    elif page == "Small Files OLS Trend":
        render_trend_analysis(data, "Small Files OLS Trend Analysis", file_set_filter="Small Files", scan_type_filter="OLS Scan")
    elif page == "Large Files OLS Trend":
        render_trend_analysis(data, "Large Files OLS Trend Analysis", file_set_filter="Large Files", scan_type_filter="OLS Scan")
    
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
        full_data = load_data(include_inactive=True)
        page_edit_benchmark(full_data)
    elif page == "Manage Classifiers":
        page_manage_classifiers()
    elif page == "Manage Test Environments":
        page_manage_test_environments()
    elif page == "Version Comparison":
        page_version_comparison()

if __name__ == "__main__":
    main()
