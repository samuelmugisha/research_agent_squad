"""
Deploy the research UI to a Hugging Face Docker Space.

Uploads only the files the app needs (never .env, history or uploads),
then sets the API key as a Space secret and the other settings as variables.

    uv run python deploy_space.py
"""

import shutil
import tempfile
from pathlib import Path

from dotenv import dotenv_values
from huggingface_hub import HfApi, get_token

REPO_ID = "dcsamuel/research-agent-squad"
# Private dataset the Space backs up its history and documents to (see persistence.py)
DATA_REPO_ID = "dcsamuel/research-agent-squad-data"
PRIVATE = True

ROOT = Path(__file__).parent
APP_FILES = [
    "research_ui_server.py",
    "multi_agent_research_system.py",
    "research_assistant.py",
    "knowledge_base.py",
    "web_search.py",
    "scholarly_search.py",
    "report_chat.py",
    "report_export.py",
    "security.py",
    "usage.py",
    "tracing.py",
    "evaluation.py",
    "auth.py",
    "persistence.py",
    "research_memory.py",
    "tests/conftest.py",
    "tests/test_security.py",
    "tests/test_usage.py",
    "tests/test_evaluation.py",
    "tests/test_rbac.py",
    "tests/test_persistence.py",
    "history.py",
    "research_ui/index.html",
    "research_ui/report.html",
    "research_ui/login.html",
    "research_ui/course.html",
    "pyproject.toml",
    "uv.lock",
]
SPACE_FILES = {  # Space-specific files placed at the repo root
    "deploy/huggingface/Dockerfile": "Dockerfile",
    "deploy/huggingface/README.md": "README.md",
}
SECRETS = ["OPENAI_API_KEY", "TAVILY_API_KEY", "LANGSMITH_API_KEY", "ADMIN_PASSWORD"]
VARIABLES = ["OPENAI_BASE_URL", "CHAT_MODEL", "EMBEDDING_MODEL", "SEARCH_MAX_RESULTS",
             "LANGSMITH_TRACING", "LANGSMITH_PROJECT", "ADMIN_USERNAME", "ADMIN_NAME"]


def stage(folder: Path) -> None:
    for name in APP_FILES:
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, folder / name)
    for source, target in SPACE_FILES.items():
        shutil.copy2(ROOT / source, folder / target)


def main() -> None:
    api = HfApi()
    env = dotenv_values(ROOT / ".env")

    url = api.create_repo(REPO_ID, repo_type="space", space_sdk="docker", private=PRIVATE, exist_ok=True)
    print(f"Space: {url}")

    # Long-term storage: the free Space disk is wiped on restart, so the app
    # backs up to a private dataset. It needs a token with write access to it.
    api.create_repo(DATA_REPO_ID, repo_type="dataset", private=True, exist_ok=True)
    api.add_space_variable(REPO_ID, "HF_DATASET_REPO", DATA_REPO_ID)
    token = env.get("HF_BACKUP_TOKEN") or get_token()
    if token:
        api.add_space_secret(REPO_ID, "HF_TOKEN", token)
        print(f"  backup   {DATA_REPO_ID} (secret HF_TOKEN)")

    # Secrets first, so the first build starts with a working configuration
    for name in SECRETS:
        if env.get(name):
            api.add_space_secret(REPO_ID, name, env[name])
            print(f"  secret   {name}")
    for name in VARIABLES:
        if env.get(name):
            api.add_space_variable(REPO_ID, name, env[name])
            print(f"  variable {name} = {env[name]}")

    with tempfile.TemporaryDirectory() as tmp:
        stage(Path(tmp))
        api.upload_folder(
            folder_path=tmp,
            repo_id=REPO_ID,
            repo_type="space",
            commit_message="Deploy Research Agent Squad",
        )
    print(f"Uploaded {len(APP_FILES) + len(SPACE_FILES)} files. Build logs: {url}?logs=build")


if __name__ == "__main__":
    main()
