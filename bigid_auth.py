import os
import re
from pathlib import Path
import requests

ENV_FILE_PATH = Path(__file__).parent / ".env"

def _extract_token(payload):
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""

    for key in ["access_token", "token", "auth_token", "jwt", "id_token", "authorization"]:
        val = payload.get(key)
        if val:
            return str(val).strip()

    for outer in ["data", "result", "response"]:
        nested = payload.get(outer)
        if isinstance(nested, dict):
            for key in ["access_token", "token", "auth_token", "jwt", "id_token", "authorization"]:
                val = nested.get(key)
                if val:
                    return str(val).strip()
        elif isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _build_url(base_url, path):
    base = str(base_url or "").strip().rstrip("/")
    p = str(path or "").strip()
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if not p.startswith("/"):
        p = "/" + p
    return f"{base}{p}"


def _sanitize_env_name(env_name):
    raw = str(env_name or "").strip().upper()
    cleaned = re.sub(r"[^A-Z0-9]+", "_", raw).strip("_")
    return cleaned


def _read_dotenv_map():
    values = {}
    if not ENV_FILE_PATH.exists():
        return values
    try:
        text = ENV_FILE_PATH.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return values
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


def _upsert_dotenv_values(updates):
    ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = []
    if ENV_FILE_PATH.exists():
        try:
            existing_lines = ENV_FILE_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            existing_lines = []

    pending = dict(updates)
    out = []
    key_regex = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for line in existing_lines:
        m = key_regex.match(line)
        if not m:
            out.append(line)
            continue
        key = m.group(1)
        if key in pending:
            out.append(f"{key}={pending[key]}")
            pending.pop(key, None)
        else:
            out.append(line)

    if pending:
        if out and out[-1].strip():
            out.append("")
        out.append("# Auto-managed environment credentials")
        for key in sorted(pending.keys()):
            out.append(f"{key}={pending[key]}")

    ENV_FILE_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def _env_keys_for_environment(env_name):
    slug = _sanitize_env_name(env_name)
    if not slug:
        return None, None
    return f"BIGID_{slug}_USERNAME", f"BIGID_{slug}_PASSWORD"


def get_saved_environment_credentials(env_name):
    """
    Returns credentials configured specifically for this env name.
    If not present, returns default credentials.
    """
    user_key, pass_key = _env_keys_for_environment(env_name)
    dotenv_map = _read_dotenv_map()
    username = ""
    password = ""
    if user_key:
        username = os.getenv(user_key, "") or dotenv_map.get(user_key, "")
    if pass_key:
        password = os.getenv(pass_key, "") or dotenv_map.get(pass_key, "")
    if not username:
        username = "bigid"
    if not password:
        password = "bigid111"
    return username, password


def save_environment_credentials(env_name, username, password):
    user_key, pass_key = _env_keys_for_environment(env_name)
    if not user_key or not pass_key:
        return
    u = str(username or "").strip() or "bigid"
    p = str(password or "").strip() or "bigid111"
    _upsert_dotenv_values({user_key: u, pass_key: p})
    # Make them available immediately for current process
    os.environ[user_key] = u
    os.environ[pass_key] = p


def _resolve_credentials(env_name):
    dotenv_map = _read_dotenv_map()
    user_key, pass_key = _env_keys_for_environment(env_name)

    # 1) Env-specific creds
    if user_key and pass_key:
        u = os.getenv(user_key, "") or dotenv_map.get(user_key, "")
        p = os.getenv(pass_key, "") or dotenv_map.get(pass_key, "")
        if u and p:
            return u, p, f"env_specific:{_sanitize_env_name(env_name)}"

    # 2) Global auth env vars
    u = os.getenv("BIGID_AUTH_USERNAME", os.getenv("BIGID_USERNAME", "")).strip()
    p = os.getenv("BIGID_AUTH_PASSWORD", os.getenv("BIGID_PASSWORD", "")).strip()
    if u and p:
        return u, p, "global_env"

    # 3) Default credentials requested by user
    return "bigid", "bigid111", "default"


def Get_auth_token(base_url, _env_row=None):
    """
    Project-local auth function used by dashboard.
    Priority:
    1) BIGID_AUTH_TOKEN
    2) Login API with BIGID_AUTH_USERNAME/BIGID_AUTH_PASSWORD
    """
    static_token = os.getenv("BIGID_AUTH_TOKEN", "").strip()
    if static_token:
        return static_token

    env_name = ""
    if isinstance(_env_row, dict):
        env_name = str(_env_row.get("env_name") or "").strip()
    username, password, source = _resolve_credentials(env_name)
    credential_candidates = [(username, password, source)]

    login_path = os.getenv("BIGID_AUTH_LOGIN_PATH", "/api/v1/sessions").strip() or "/api/v1/sessions"
    login_paths = [
        login_path,
        "/api/v1/sessions",
        "/api/v1/login",
        "/api/v1/auth/login",
        "/api/v1/authenticate",
        "/api/login",
    ]

    errors = []
    for path in login_paths:
        url = _build_url(base_url, path)
        for username, password, source in credential_candidates:
            payloads = [
                ("json", {"username": username, "password": password}),
                ("json", {"userName": username, "password": password}),
                ("json", {"email": username, "password": password}),
                ("form", {"username": username, "password": password, "grant_type": "password"}),
            ]
            for mode, body in payloads:
                try:
                    if mode == "json":
                        resp = requests.post(url, json=body, timeout=30)
                    else:
                        resp = requests.post(url, data=body, timeout=30)

                    if resp.status_code >= 400:
                        errors.append(f"{url} [{resp.status_code}] ({source})")
                        continue

                    token = ""
                    try:
                        token = _extract_token(resp.json())
                    except Exception:
                        token = _extract_token(resp.text)
                    if token:
                        return token
                except Exception as ex:
                    errors.append(f"{url} [{ex}] ({source})")
                    continue

    sample = "; ".join(errors[:3]) if errors else "No response from auth endpoints"
    raise RuntimeError(f"Auth token fetch failed. {sample}")
