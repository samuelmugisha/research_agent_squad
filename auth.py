"""
Authentication and role-based access control (RBAC).

Roles follow a research lab's hierarchy. Every lab member can read all
reports; roles decide who can run research, change the knowledge base, see
costs, run evaluations and manage the team.

- Passwords are hashed with scrypt (memory-hard, from the standard library).
- Sessions are random tokens in an HttpOnly cookie; only their SHA-256 hash is
  stored, so a leaked database can't be used to hijack sessions.
- New accounts and password resets get a temporary password that must be
  changed at the next sign-in.
- After MAX_FAILURES wrong passwords an account is locked for LOCKOUT_MINUTES.
- Users and sessions live in the history database, so the Hugging Face backup
  (persistence.py) keeps them too.

The first Principal Investigator is created from ADMIN_USERNAME / ADMIN_PASSWORD
(and optional ADMIN_NAME) when the database has no users yet.
"""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta

import history

# ============================================================
# Roles and permissions
# ============================================================

PERMISSIONS = {
    "reports.view": "Read reports, history and the knowledge base",
    "research.run": "Run research and ask follow-up questions",
    "kb.upload": "Upload documents and write notes",
    "kb.delete": "Delete knowledge-base documents",
    "runs.manage_own": "Share and delete their own reports",
    "runs.manage_any": "Delete or share anyone's reports",
    "costs.view": "See costs and LangSmith traces",
    "eval.run": "View and run evaluations",
    "users.manage": "Manage team accounts and roles",
}

ROLES = {
    "principal_investigator": {
        "label": "Principal Investigator",
        "description": "Leads the lab: full access, including team accounts and roles.",
        "permissions": set(PERMISSIONS),
    },
    "research_officer": {
        "label": "Research Officer",
        "description": "Runs the research operation: research, knowledge base, costs and evaluations.",
        "permissions": {"reports.view", "research.run", "kb.upload", "kb.delete", "runs.manage_own",
                        "runs.manage_any", "costs.view", "eval.run"},
    },
    "research_associate": {
        "label": "Research Associate",
        "description": "Does the research: runs studies, adds documents, manages their own reports.",
        "permissions": {"reports.view", "research.run", "kb.upload", "runs.manage_own"},
    },
    "guest_reviewer": {
        "label": "Guest Reviewer",
        "description": "Read-only access to reports and documents, e.g. for collaborators or examiners.",
        "permissions": {"reports.view"},
    },
}
ROLE_ORDER = list(ROLES)

SESSION_COOKIE = "squad_session"
SESSION_HOURS = 12
REMEMBER_DAYS = 30
MAX_FAILURES = 5
LOCKOUT_MINUTES = 15
MIN_PASSWORD_LENGTH = 10

# ============================================================
# Storage
# ============================================================

_conn = sqlite3.connect(history.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_lock = threading.Lock()
with _lock, _conn:
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            failed_logins INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT,
            created_at TEXT NOT NULL,
            last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions(user_id);
        """
    )


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


# ============================================================
# Passwords
# ============================================================

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                                   n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(candidate.hex(), digest)
    except (ValueError, TypeError):
        return False


# Used when the username doesn't exist, so both cases take the same time
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def password_problem(password: str) -> str | None:
    """Why a new password isn't acceptable, or None if it is."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() == password or password.upper() == password or not re.search(r"\d", password):
        return "Mix upper- and lower-case letters and at least one number."
    return None


def temporary_password() -> str:
    """Readable one-time password, e.g. 'Kite-Pebble-4821'."""
    words = ["Atlas", "Scout", "Prism", "Quill", "Kite", "Pebble", "Harbor", "Cedar", "Comet",
             "Delta", "Ember", "Fjord", "Galaxy", "Maple", "Orbit", "Quartz", "Summit", "Willow"]
    return f"{secrets.choice(words)}-{secrets.choice(words)}-{secrets.randbelow(9000) + 1000}"


# ============================================================
# Users
# ============================================================

def public_user(row) -> dict | None:
    if row is None:
        return None
    user = dict(row)
    role = ROLES.get(user["role"], ROLES["guest_reviewer"])
    return {
        "id": user["id"], "username": user["username"], "name": user["name"], "role": user["role"],
        "role_label": role["label"], "permissions": sorted(role["permissions"]),
        "active": bool(user["active"]), "must_change_password": bool(user["must_change_password"]),
        "created_at": user["created_at"], "last_login": user["last_login"],
    }


def can(user: dict | None, permission: str) -> bool:
    return bool(user) and permission in ROLES.get(user["role"], {}).get("permissions", set())


def user_count() -> int:
    with _lock:
        return _conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_users() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM users ORDER BY active DESC, name COLLATE NOCASE").fetchall()
    order = {r: i for i, r in enumerate(ROLE_ORDER)}
    return sorted((public_user(r) for r in rows), key=lambda u: (not u["active"], order.get(u["role"], 9)))


def get_user(user_id: int) -> dict | None:
    with _lock:
        return public_user(_conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def user_names() -> dict[int, str]:
    with _lock:
        return {r["id"]: r["name"] for r in _conn.execute("SELECT id, name FROM users")}


def create_user(username: str, name: str, role: str, password: str, must_change: bool = True) -> dict:
    username, name = username.strip(), name.strip()
    if role not in ROLES:
        raise ValueError("Unknown role.")
    if not re.fullmatch(r"[A-Za-z0-9._@+-]{3,80}", username):
        raise ValueError("Username must be 3–80 characters: letters, numbers and . _ @ + -")
    if not name:
        raise ValueError("Name is required.")
    try:
        with _lock, _conn:
            cursor = _conn.execute(
                "INSERT INTO users (username, name, role, password_hash, must_change_password, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (username, name[:80], role, hash_password(password), int(must_change), _now().isoformat()),
            )
    except sqlite3.IntegrityError:
        raise ValueError("That username is already taken.")
    return get_user(cursor.lastrowid)


def _active_pis(excluding: int | None = None) -> int:
    with _lock:
        return _conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'principal_investigator' AND active = 1 AND id != ?",
            (excluding or -1,),
        ).fetchone()[0]


def update_user(user_id: int, *, name: str | None = None, role: str | None = None,
                active: bool | None = None) -> dict:
    user = get_user(user_id)
    if user is None:
        raise LookupError("User not found.")
    if role is not None and role not in ROLES:
        raise ValueError("Unknown role.")
    losing_pi = user["role"] == "principal_investigator" and user["active"] and (
        (role is not None and role != "principal_investigator") or active is False)
    if losing_pi and _active_pis(excluding=user_id) == 0:
        raise ValueError("The lab needs at least one active Principal Investigator.")
    fields = {"name": name.strip()[:80] if name else None, "role": role,
              "active": None if active is None else int(active)}
    updates = {k: v for k, v in fields.items() if v is not None}
    if updates:
        with _lock, _conn:
            _conn.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                          (*updates.values(), user_id))
    if active is False:
        end_sessions(user_id)
    return get_user(user_id)


def set_password(user_id: int, password: str, must_change: bool) -> None:
    with _lock, _conn:
        _conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = ?, failed_logins = 0, "
            "locked_until = NULL WHERE id = ?",
            (hash_password(password), int(must_change), user_id),
        )


def check_password(user_id: int, password: str) -> bool:
    with _lock:
        row = _conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row) and verify_password(password, row["password_hash"])


class LoginError(Exception):
    pass


def authenticate(username: str, password: str) -> dict:
    """Return the user for valid credentials, or raise LoginError with a
    message that doesn't reveal whether the username exists."""
    with _lock:
        row = _conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if row is None:
        verify_password(password, _DUMMY_HASH)  # same timing as a real check
        raise LoginError("Incorrect username or password.")
    now = _now()
    if row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > now:
        raise LoginError(f"Too many failed attempts. Try again in {LOCKOUT_MINUTES} minutes.")
    if not verify_password(password, row["password_hash"]):
        failures = row["failed_logins"] + 1
        locked = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat() if failures >= MAX_FAILURES else None
        with _lock, _conn:
            _conn.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                          (0 if locked else failures, locked, row["id"]))
        raise LoginError("Incorrect username or password.")
    if not row["active"]:
        raise LoginError("This account has been deactivated. Contact your Principal Investigator.")
    with _lock, _conn:
        _conn.execute("UPDATE users SET failed_logins = 0, locked_until = NULL, last_login = ? WHERE id = ?",
                      (now.isoformat(), row["id"]))
    return get_user(row["id"])


# ============================================================
# Sessions
# ============================================================

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int, remember: bool = False) -> tuple[str, int]:
    """Returns (token for the cookie, lifetime in seconds)."""
    token = secrets.token_urlsafe(32)
    lifetime = timedelta(days=REMEMBER_DAYS) if remember else timedelta(hours=SESSION_HOURS)
    now = _now()
    with _lock, _conn:
        _conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
        _conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                      (_hash_token(token), user_id, now.isoformat(), (now + lifetime).isoformat()))
    return token, int(lifetime.total_seconds())


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
            "WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.active = 1",
            (_hash_token(token), _now().isoformat()),
        ).fetchone()
    return public_user(row)


def end_session(token: str | None) -> None:
    if token:
        with _lock, _conn:
            _conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def end_sessions(user_id: int, keep_token: str | None = None) -> None:
    """Sign a user out everywhere (optionally except the current session)."""
    with _lock, _conn:
        _conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                      (user_id, _hash_token(keep_token) if keep_token else ""))


def bootstrap_admin() -> dict | None:
    """Create the first Principal Investigator from ADMIN_* settings."""
    username, password = os.getenv("ADMIN_USERNAME"), os.getenv("ADMIN_PASSWORD")
    if user_count() or not (username and password):
        return None
    user = create_user(username, os.getenv("ADMIN_NAME") or "Principal Investigator",
                       "principal_investigator", password, must_change=False)
    history.assign_unowned_runs(user["id"])  # earlier research belongs to the lab lead
    return user
