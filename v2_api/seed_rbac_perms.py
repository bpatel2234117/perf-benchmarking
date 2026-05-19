from sqlalchemy import create_engine, text
import os

DB_URL = os.getenv('TRACKER_DATABASE_URL', 'postgresql+psycopg2://postgres:bigid111!@10.2.20.89:5432/benchmark_db')
engine = create_engine(DB_URL)

ALL_PAGES = [
    "Overview", "Build Analysis", "Comparison", "Build DS Runs", "Data Source Trend Comparison",
    "Small Files Trend Analysis", "Large Files Trend Analysis", "Small Files OLS Trend", "Large Files OLS Trend",
    "OLS vs Full Scan (Small)", "OLS vs Full Scan (Large)", "Discover File FF On vs Off",
    "ACL ON vs OLS (ACL ON)", "ACL OFF vs OLS (ACL OFF)", "Edit Records", "Manage Classifiers",
    "Classifier Changes", "Manage Test Environments", "Version Comparison", "Workflow Automation",
    "Create Data Source", "Scan Status", "Scan Progress"
]

ROLES = {
    1: "Admin",
    2: "Viewer",
    3: "Executor ", # Note the trailing space in ID 3
    5: "Developer",
    6: "Auditor",
    7: "Workflow Runner"
}

def seed():
    with engine.begin() as conn:
        # Clear existing perms first to avoid duplicates
        conn.execute(text("DELETE FROM app_role_pages"))
        
        # 1. Admin: Everything
        print("Seeding Admin permissions...")
        for p in ALL_PAGES:
            conn.execute(text("INSERT INTO app_role_pages (role_id, page_name) VALUES (1, :p)"), {"p": p})
            
        # 2. Viewer: Read-only analytics
        print("Seeding Viewer permissions...")
        viewer_pages = [
            "Overview", "Build Analysis", "Comparison", "Build DS Runs", "Data Source Trend Comparison",
            "Small Files Trend Analysis", "Large Files Trend Analysis", "Small Files OLS Trend", "Large Files OLS Trend",
            "OLS vs Full Scan (Small)", "OLS vs Full Scan (Large)", "Discover File FF On vs Off",
            "ACL ON vs OLS (ACL ON)", "ACL OFF vs OLS (ACL OFF)", "Classifier Changes", "Version Comparison"
        ]
        for p in viewer_pages:
            conn.execute(text("INSERT INTO app_role_pages (role_id, page_name) VALUES (2, :p)"), {"p": p})
            
        # 3. Executor / Workflow Runner: Operational pages
        print("Seeding Executor & Workflow Runner permissions...")
        op_pages = ["Workflow Automation", "Create Data Source", "Scan Status", "Scan Progress", "Overview"]
        for rid in [3, 7]:
            for p in op_pages:
                conn.execute(text("INSERT INTO app_role_pages (role_id, page_name) VALUES (:rid, :p)"), {"rid": rid, "p": p})
        
        # 4. Developer: Everything except sensitive setup (like Manage Test Environments?)
        print("Seeding Developer permissions...")
        dev_pages = [p for p in ALL_PAGES if p != "Manage Test Environments"]
        for p in dev_pages:
            conn.execute(text("INSERT INTO app_role_pages (role_id, page_name) VALUES (5, :p)"), {"p": p})
            
        # 5. Auditor: Analytical comparison pages
        print("Seeding Auditor permissions...")
        audit_pages = ["Overview", "Comparison", "Build Analysis", "Version Comparison", "Classifier Changes"]
        for p in audit_pages:
            conn.execute(text("INSERT INTO app_role_pages (role_id, page_name) VALUES (6, :p)"), {"p": p})

    print("RBAC Seeding completed successfully.")

if __name__ == "__main__":
    seed()
