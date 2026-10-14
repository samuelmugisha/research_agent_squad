import os
import sys
import tempfile
from pathlib import Path

# Import the app modules from the project folder, and keep test data
# (SQLite history, knowledge base) out of the real data directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="research-squad-tests-")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
# Tests must never send traces to LangSmith
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
# Tests sign in many times from one client; test_rbac checks the limit itself
os.environ["LOGIN_ATTEMPTS_PER_15_MIN"] = "0"


def signed_in_client(role: str = "principal_investigator"):
    """A TestClient signed in as a fresh user with the given role."""
    import secrets as _secrets

    from fastapi.testclient import TestClient

    import auth
    import research_ui_server

    username = f"{role}-{_secrets.token_hex(4)}"
    password = "Test-Password-123"
    user = auth.create_user(username, f"Test {role}", role, password, must_change=False)
    client = TestClient(research_ui_server.app)
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.user = user
    return client
