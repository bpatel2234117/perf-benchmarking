import os
import sys

def check():
    print("=== Dashboard Auth Diagnostic ===")
    try:
        import dashboard_auth
        print(f"Loaded module from: {dashboard_auth.__file__}")
        
        expected = ['ensure_rbac_tables', 'login_user', 'logout_user', 'check_access', 'get_user_permissions', 'hash_password', 'verify_password']
        
        missing = []
        for name in expected:
            if hasattr(dashboard_auth, name):
                print(f"[OK] {name} is present.")
            else:
                print(f"[MISSING] {name} is NOT found!")
                missing.append(name)
        
        if not missing:
            print("\nSUCCESS: All expected functions are present in dashboard_auth.")
        else:
            print(f"\nFAILURE: {len(missing)} functions are missing. Check if you have the latest file.")
            
    except ImportError as e:
        print(f"CRITICAL: Could not import dashboard_auth: {e}")
    except Exception as e:
        print(f"ERROR: Unexpected error: {e}")

if __name__ == "__main__":
    check()
