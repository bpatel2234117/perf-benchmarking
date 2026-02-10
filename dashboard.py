import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import hashlib
import base64
import numpy as np
from pathlib import Path
from db_config import DB_CONFIG, DB_CONNECTION_STRING

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

def get_file_set_types(data_type=None):
    if not engine:
        return []
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS file_set_types (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) UNIQUE,
                    data_type VARCHAR(50)
                )
            """))
            try:
                conn.execute(text("ALTER TABLE file_set_types ADD COLUMN data_type VARCHAR(50)"))
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
                    text("SELECT name FROM file_set_types WHERE data_type = :dt ORDER BY name"),
                    {"dt": data_type}
                ).fetchall()
            else:
                rows = conn.execute(text("SELECT name FROM file_set_types ORDER BY name")).fetchall()
            names = [r[0] for r in rows]
            if data_type == "Unstructured":
                names = [n for n in names if "table" not in n.lower()]
            if data_type == "Structured":
                names = [n for n in names if "file" not in n.lower() and "csv" not in n.lower()]
            return names
    except Exception:
        return []

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
    classifier_input = st.text_input("Classifiers (comma-separated)", help="Example: PII, PCI, GDPR")
    
    # 3. Configuration & Flags
    st.subheader("3. Configuration & Flags")
    c3, c4, c5 = st.columns(3)
    scan_count = c3.number_input("Scanner Count", min_value=1, value=5)
    thread_count = c4.number_input("Threads per Scanner", min_value=1, value=50)
    
    st.caption("Feature Flags")
    chk1, chk2, chk3 = st.columns(3)
    is_acl = chk1.checkbox("ACL Enabled")
    is_ocr = chk2.checkbox("OCR Enabled")
    is_ff = chk3.checkbox("Structured Discovery FF")
    
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
    c_ds, c_dt, c_fs = st.columns(3)
    with c_ds:
        ds_type_choice = st.selectbox("Data Source Type", ds_options + ["Other (custom)"])
    with c_dt:
        data_type = st.selectbox("Data Type", ["Structured", "Unstructured", "Mixed"], key="data_type")
    with c_fs:
        if data_type == "Structured":
            fs_defaults = ["Small Tables", "Large Tables", "Mixed Tables"]
            file_set_choice = st.selectbox("File Set Type", fs_defaults + ["Other (custom)"], key="file_set_structured")
        else:
            fs_defaults = ["Small Files", "Large Files", "Mixed Files", "CSV Files"]
            file_set_choice = st.selectbox("File Set Type", fs_defaults + ["Other (custom)"], key="file_set_unstructured")
    custom_ds_type = None
    if ds_type_choice == "Other (custom)":
        custom_ds_type = st.text_input("Custom Data Source Type")
    ds_type = custom_ds_type.strip() if custom_ds_type else ds_type_choice
    custom_fs = st.text_input("Custom File Set Type", key="custom_file_set_type") if file_set_choice == "Other (custom)" else ""
    file_set_type = custom_fs.strip() if custom_fs.strip() else file_set_choice

    r2_c1, r2_c2, r2_c3 = st.columns(3)
    if data_type == "Structured":
        total_files = r2_c1.number_input("Total Rows", min_value=0, step=1000, key="total_rows")
        total_folders = r2_c2.number_input("Total Columns", min_value=0, step=10, key="total_columns")
    else:
        total_files = r2_c1.number_input("Total Files", min_value=0, step=1000, key="total_files")
        total_folders = r2_c2.number_input("Total Folders", min_value=0, step=100, key="total_folders")
    total_gb = r2_c3.number_input("Total Size (GB)", min_value=0.0, format="%.2f")

    r3_c1, r3_c2 = st.columns(2)
    total_catalog_items = r3_c1.number_input("Total Catalog Items", min_value=0, step=1000)
    avg_size = r3_c2.text_input("Avg File Size (Text, e.g. '50kb')")

    with st.form("add_benchmark_form"):
        st.markdown("#### Scan Timing")
        st.caption("Enter start and end times - throughput will be calculated automatically")
        
        t1_c1, t1_c2, t1_c3, t1_c4 = st.columns(4)
        scan_start_date = t1_c1.date_input("Scan Start Date", key="scan_start_date")
        scan_start_time_str = t1_c1.text_input("Scan Start Time (HH:MM[:SS])", key="scan_start_time_str", placeholder="2:13:17")
        scan_end_date = t1_c2.date_input("Scan End Date", key="scan_end_date")
        scan_end_time_str = t1_c2.text_input("Scan End Time (HH:MM[:SS])", key="scan_end_time_str", placeholder="2:29:34")
        enum_start_date = t1_c3.date_input("Enum Start Date", key="enum_start_date")
        enum_start_time_str = t1_c3.text_input("Enum Start Time (HH:MM[:SS])", key="enum_start_time_str", placeholder="2:13:00")
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
        enum_start_time = parse_time_value(enum_start_time_str)
        enum_end_time = parse_time_value(enum_end_time_str)

        scan_start = datetime.combine(scan_start_date, scan_start_time) if scan_start_date and scan_start_time else None
        scan_end = datetime.combine(scan_end_date, scan_end_time) if scan_end_date and scan_end_time else None
        enum_start = datetime.combine(enum_start_date, enum_start_time) if enum_start_date and enum_start_time else None
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
        st.caption("Add multiple rows for classifier findings")
        findings_df = st.data_editor(
            pd.DataFrame([{"Classifier": "", "Findings": 0}]),
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "Classifier": st.column_config.TextColumn("Classifier"),
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
                "Edit Records": "Edit Records"
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

if __name__ == "__main__":
    main()
