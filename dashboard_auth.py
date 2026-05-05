import streamlit as st
from sqlalchemy import text
import hashlib
import os
import json
import time
from pathlib import Path

SESSION_DIR = Path.home() / ".streamlit_sessions"
SESSION_DIR.mkdir(exist_ok=True)

__all__ = [
    'ensure_rbac_tables', 
    'login_user', 
    'logout_user', 
    'check_access', 
    'get_user_permissions', 
    'hash_password', 
    'verify_password',
    'update_user_role'
]

# Diagnostic for ImportError debugging
import logging
logging.debug("Dashboard Auth module initialized. Exports: " + ", ".join(__all__))

def get_client_id():
    """Identify the client based on headers."""
    headers = st.context.headers
    # Use IP or User-Agent to distinguish clients on same IP if possible
    # For localhost, this might just be 127.0.0.1
    ip = headers.get("X-Forwarded-For", "unknown")
    ua = headers.get("User-Agent", "unknown")
    return hashlib.sha256(f"{ip}_{ua}".encode()).hexdigest()

def save_session(username, role, role_id):
    """Save session to a local file."""
    client_id = get_client_id()
    session_data = {
        "username": username,
        "role": role,
        "role_id": role_id,
        "timestamp": time.time()
    }
    with open(SESSION_DIR / f"{client_id}.json", "w") as f:
        json.dump(session_data, f)

def get_persisted_session():
    """Load session from local file if valid (thin for 4 hours)."""
    client_id = get_client_id()
    session_path = SESSION_DIR / f"{client_id}.json"
    if session_path.exists():
        try:
            with open(session_path, "r") as f:
                data = json.load(f)
            # 4 hour expiry
            if time.time() - data["timestamp"] < 14400:
                return data
        except:
            pass
    return None

def clear_session():
    """Delete session file."""
    client_id = get_client_id()
    session_path = SESSION_DIR / f"{client_id}.json"
    if session_path.exists():
        session_path.unlink()

def hash_password(password: str) -> str:
    """Return the SHA256 hash of a password."""
    if not password:
        return ""
    return hashlib.sha256(password.strip().encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash."""
    if not password or not hashed:
        return False
    return hash_password(password) == hashed

def ensure_rbac_tables(engine):
    """Ensure tables exist and seed default admin if empty."""
    try:
        from benchmark_db import is_postgresql_engine

        use_pg = is_postgresql_engine(engine)
    except Exception:
        use_pg = False

    try:
        with engine.begin() as conn:
            if use_pg:
                # DDL comes from `ensure_test_environments_table` using `v2_api.db_config_pg` when Postgres is the dashboard DB.
                pass
            else:
                from db_config import DDL_STATEMENTS

                tables_to_ensure = ["app_roles", "app_users", "app_role_pages"]
                for tbl in tables_to_ensure:
                    if tbl in DDL_STATEMENTS:
                        conn.execute(text(DDL_STATEMENTS[tbl]))

            # Seed Admin Role
            res = conn.execute(text("SELECT id FROM app_roles WHERE role_name = 'Admin'")).fetchone()
            if not res:
                conn.execute(text("INSERT INTO app_roles (role_name) VALUES ('Admin')"))
                admin_role_id = conn.execute(text("SELECT id FROM app_roles WHERE role_name = 'Admin'")).fetchone()[0]
            else:
                admin_role_id = res[0]

            # Seed Viewer Role
            res_v = conn.execute(text("SELECT id FROM app_roles WHERE role_name = 'Viewer'")).fetchone()
            if not res_v:
                conn.execute(text("INSERT INTO app_roles (role_name) VALUES ('Viewer')"))
                viewer_role_id = conn.execute(text("SELECT id FROM app_roles WHERE role_name = 'Viewer'")).fetchone()[0]
            else:
                viewer_role_id = res_v[0]

            # Seed Default Admin User (bpatel@bigid.com)
            res_u = conn.execute(text("SELECT id FROM app_users WHERE username = 'bpatel@bigid.com'")).fetchone()
            if not res_u:
                # Seed with password 'bigid123'
                admin_pwd_hash = hash_password('bigid123')
                conn.execute(text("INSERT INTO app_users (username, password_hash, role_id) VALUES (:u, :p, :r)"),
                             {"u": "bpatel@bigid.com", "p": admin_pwd_hash, "r": admin_role_id})
    except Exception as e:
        # Graceful error handling: log the error instead of breaking the landing page UI.
        # This prevents transient connection issues from preventing the user from reaching the login page.
        import logging
        logging.error(f"Error ensuring RBAC tables: {e}")

ADMIN_ONLY_PAGES = [
    "Add New Benchmark",
    "Edit Records",
    "Manage Classifiers",
    "Manage Test Environments",
    "Create Data Source",
    "Manage RBAC"
]

def check_access(page_name: str) -> bool:
    """
    Check if the current session user has access to page_name.
    - Admins have access to everything.
    - ADMIN_ONLY_PAGES are restricted to Admin role only.
    - Other pages are accessible to any authenticated user.
    """
    if not st.session_state.get("authenticated"):
        return False
    
    user_role = st.session_state.get("user_role")
    if user_role == "Admin":
        return True
    
    # Block non-admins from sensitive modification pages
    if page_name in ADMIN_ONLY_PAGES:
        return False
    
    # Allow authenticated users access to everything else (Workflows, Progress, Trends, etc.)
    return True

def get_user_permissions(engine, role_id):
    """Fetch list of permitted pages for a role."""
    with engine.connect() as conn:
        res = conn.execute(text("SELECT page_name FROM app_role_pages WHERE role_id = :rid"), {"rid": role_id}).fetchall()
        return [r[0] for r in res]

def login_user(engine, email):
    """Verify email domain and set session state. Auto-create user if bigid.com."""
    email = str(email).strip().lower()
    if not email.endswith("@bigid.com"):
        return False, "Only @bigid.com emails are allowed."

    with engine.connect() as conn:
        res = conn.execute(text("""
            SELECT u.id, r.role_name, u.role_id, u.password_hash 
            FROM app_users u
            JOIN app_roles r ON u.role_id = r.id
            WHERE u.username = :u
        """), {"u": email}).fetchone()
        
        if res:
            user_id, role_name, role_id, pwd_hash = res
            
            # If user is Admin, require password
            if role_name == "Admin":
                from streamlit import session_state as st_ss
                entered_pwd = st_ss.get("login_password_input", "")
                if not entered_pwd:
                    return False, "Password required for Admin."
                
                # Check hash
                if not verify_password(entered_pwd, pwd_hash):
                    return False, "Invalid admin password."
        else:
            # Auto-create as Viewer
            viewer_role = conn.execute(text("SELECT id FROM app_roles WHERE role_name = 'Viewer'")).fetchone()
            v_role_id = viewer_role[0] if viewer_role else 1
            conn.execute(text("INSERT INTO app_users (username, password_hash, role_id) VALUES (:u, 'PASSWORDLESS', :r)"),
                         {"u": email, "r": v_role_id})
            conn.commit()
            
            res_new = conn.execute(text("""
                SELECT u.id, r.role_name, u.role_id, u.password_hash 
                FROM app_users u
                JOIN app_roles r ON u.role_id = r.id
                WHERE u.username = :u
            """), {"u": email}).fetchone()
            user_id, role_name, role_id, pwd_hash = res_new

        st.session_state.authenticated = True
        st.session_state.username = email
        st.session_state.user_role = role_name
        st.session_state.user_role_id = role_id
        st.session_state.allowed_pages = get_user_permissions(engine, role_id)
        
        # Persist session
        save_session(email, role_name, role_id)
        
        return True, ""

def logout_user():
    st.session_state.authenticated = False
    st.session_state.username = None
    st.session_state.user_role = None
    st.session_state.allowed_pages = []
    clear_session()
    st.rerun()

def update_user_role(engine, user_id, role_id):
    """Update a user's role."""
    with engine.connect() as conn:
        conn.execute(text("UPDATE app_users SET role_id = :r WHERE id = :id"), {"r": role_id, "id": user_id})
        conn.commit()
    return True
