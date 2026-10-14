"""
Web UI for the Multi-Agent Research System.

Streams each agent's progress to the browser with Server-Sent Events so the
UI can show the 5 agents working together in real time.

Run:
    uv run uvicorn research_ui_server:app --port 8000
Then open http://localhost:8000
"""

import json
import logging
import os
import queue
import secrets
import threading
import time
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Literal

# Restore saved data before any module opens the database: Hugging Face
# Spaces wipe the disk on restart (see persistence.py)
import persistence

persistence.restore()

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import auth
import evaluation
import history
import knowledge_base
import report_chat
import report_export
import research_memory
import security
import tracing
import usage
from multi_agent_research_system import CHAT_MODEL, create_research_system
from web_search import search_provider

UI_FILE = Path(__file__).parent / "research_ui" / "index.html"
REPORT_PAGE = Path(__file__).parent / "research_ui" / "report.html"
LOGIN_PAGE = Path(__file__).parent / "research_ui" / "login.html"

log = logging.getLogger("uvicorn.error")


def rebuild_indexes() -> None:
    """Vector indexes are derived data, so rebuild what's missing: the knowledge
    base after a restore from backup, and long-term memory from saved reports."""
    try:
        if persistence.status()["restored"]:
            indexed = {d["source"] for d in knowledge_base.list_documents()}
            for name, info in persistence.restored_manifest().items():
                path = knowledge_base.file_path(name)
                if name in indexed or not path.is_file():
                    continue
                try:
                    knowledge_base.add_file(name, path.read_bytes(), info.get("kind", "upload"), info.get("added_at"))
                except ValueError as exc:
                    log.warning("Could not re-index %s: %s", name, exc)
        if research_memory.count() == 0:
            reports = []
            for run in history.list_runs(limit=1000):
                state = history.load_state(run["id"]) if run["status"] == "done" else None
                if state and state.get("report"):
                    reports.append((run, state))
            if reports:
                log.info("Indexed %d past reports into long-term memory", research_memory.rebuild(reports))
    except Exception as exc:
        log.error("Rebuilding indexes failed: %s", exc)


def remember(run_id: str, topic: str, state: dict) -> None:
    """Add a finished report to long-term memory (runs in the background)."""
    try:
        run = history.get_run(run_id)
        research_memory.add_report(run_id, topic, run["created_at"] if run else "", state)
    except Exception as exc:
        log.error("Could not add report %s to memory: %s", run_id, exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if admin := auth.bootstrap_admin():
        log.info("Created the first Principal Investigator account: %s", admin["username"])
    elif auth.user_count() == 0:
        log.warning("No user accounts yet: set ADMIN_USERNAME and ADMIN_PASSWORD to create the first one.")
    threading.Thread(target=rebuild_indexes, daemon=True, name="reindex").start()
    persistence.start()
    yield
    persistence.stop()


app = FastAPI(title="Research Agent Squad", lifespan=lifespan)
research_system = create_research_system(checkpointer=history.checkpointer)

# Abuse limits (per client IP, per hour); 0 turns a limit off
research_limiter = security.RateLimiter(int(os.getenv("RESEARCH_RUNS_PER_HOUR", "10")))
chat_limiter = security.RateLimiter(int(os.getenv("CHAT_MESSAGES_PER_HOUR", "60")))
upload_limiter = security.RateLimiter(int(os.getenv("UPLOADS_PER_HOUR", "30")))
eval_limiter = security.RateLimiter(int(os.getenv("EVAL_RUNS_PER_HOUR", "6")))
login_limiter = security.RateLimiter(int(os.getenv("LOGIN_ATTEMPTS_PER_15_MIN", "20")), window=900)
run_slots = security.RunSlots(int(os.getenv("MAX_CONCURRENT_RUNS", "3")))


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Share links carry their token in the URL; don't leak it to other sites
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# ============================================================
# Authentication and access control (see auth.py)
# ============================================================

# Reachable without signing in: the login page, and public share links
PUBLIC_PATHS = {"/login", "/api/auth/login", "/favicon.ico"}
PUBLIC_PREFIXES = ("/share/", "/api/share/")
# Allowed while a temporary password still has to be changed
PASSWORD_CHANGE_PATHS = {"/", "/api/auth/me", "/api/auth/password", "/api/auth/logout"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Deny by default: every page and API needs a signed-in user, except the
    login page and public share links."""
    path = request.url.path
    request.state.user = auth.session_user(request.cookies.get(auth.SESSION_COOKIE))
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    user = request.state.user
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Please sign in."}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=303)
    if user["must_change_password"] and path not in PASSWORD_CHANGE_PATHS:
        return JSONResponse({"detail": "Please change your temporary password first."}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """CSRF protection: state-changing requests must come from this site.
    (The session cookie is SameSite=None on HTTPS so the app works inside
    Hugging Face's iframe, so SameSite alone isn't enough.)"""
    origin = request.headers.get("origin")
    if request.method in UNSAFE_METHODS and origin:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        if origin.split("://", 1)[-1] != host:
            return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
    return await call_next(request)


# Registered last, so it is the outermost middleware and also covers the
# 401/403/redirect responses produced above
app.middleware("http")(security_headers)


def current_user(request: Request) -> dict:
    return request.state.user


def require(request: Request, permission: str) -> dict:
    """The signed-in user, or 403 if their role lacks the permission."""
    user = current_user(request)
    if not auth.can(user, permission):
        role = user["role_label"] if user else "Your role"
        raise HTTPException(status_code=403,
                            detail=f"{role}s can't {auth.PERMISSIONS[permission][0].lower()}{auth.PERMISSIONS[permission][1:]}.")
    return user


def can_manage_run(user: dict, run: dict) -> bool:
    """Share or delete a report: anyone's with runs.manage_any, else only your own."""
    return auth.can(user, "runs.manage_any") or (
        auth.can(user, "runs.manage_own") and run.get("user_id") == user["id"])


def require_run_manager(request: Request, run: dict) -> None:
    if not can_manage_run(current_user(request), run):
        raise HTTPException(status_code=403, detail="You can only share or delete reports you started.")


def client_id(request: Request) -> str:
    """Rate-limit key: the signed-in user, else the client IP (behind a proxy,
    uvicorn --proxy-headers sets it from X-Forwarded-For)."""
    user = getattr(request.state, "user", None)
    if user:
        return f"user:{user['id']}"
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(limiter: "security.RateLimiter", request: Request, what: str) -> None:
    retry_after = limiter.check(client_id(request))
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"Too many {what}. Please try again in {security.wait_text(retry_after)}.",
            headers={"Retry-After": str(retry_after)},
        )


def enforce_guard(text: str, kind: str, tracker: "usage.UsageTracker | None" = None) -> None:
    try:
        verdict = security.check_request(text, kind, callbacks=[tracker] if tracker else None)
    except security.GuardUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not verdict.allowed:
        raise HTTPException(status_code=400, detail=f"Request blocked by the safety check: {verdict.reason}")


class ResearchRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=500)
    sources: list[Literal["web", "academic", "kb", "memory"]] = Field(
        default=["web", "academic", "kb", "memory"], min_length=1
    )


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)
    remember: bool = False


class PasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


class NewUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    role: str


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    role: str | None = None
    active: bool | None = None


class EvalRequest(BaseModel):
    ids: list[str] | None = None   # None runs every evaluation
    include_llm: bool = True       # False runs only the free deterministic checks


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class NoteRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=200_000)


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def task_started(name: str, state: dict) -> dict:
    """Describe what an agent is about to do, from its input state."""
    if name == "search_agent":
        return {"query": state.get("search_query", "")}
    if name == "report_writer":
        return {"revision": state.get("iteration", 0)}
    return {}


def public_finding(f: dict) -> dict:
    return {
        "title": str(f.get("title", "")),
        "detail": str(f.get("detail", "")),
        "query": f.get("source_query", ""),
        "sources": f.get("sources", []),
    }


def task_finished(name: str, result: dict) -> dict:
    """Pick the parts of an agent's output worth showing in the UI."""
    if name == "supervisor":
        return {"queries": result.get("search_queries", [])}
    if name == "search_agent":
        findings = result.get("findings", [])
        return {
            "query": findings[0].get("source_query", "") if findings else "",
            "findings": [public_finding(f) for f in findings if isinstance(f, dict)],
            "tool_calls": result.get("tool_calls", []),
        }
    if name == "analyst":
        return {"analysis": result.get("analysis", ""), "sources": result.get("sources", [])}
    if name == "report_writer":
        return {"chars": len(result.get("report", ""))}
    if name == "quality_checker":
        score = result.get("quality_score", 0.0)
        # Mirrors quality_gate(): approved when score is high or after 2 rounds
        approved = score >= 0.7 or result.get("iteration", 0) >= 2
        return {
            "score": score,
            "feedback": result.get("quality_feedback", ""),
            "approved": approved,
        }
    return {}


def result_payload(run: dict, state: dict, user: dict) -> dict:
    """Everything the UI needs to render a finished report, filtered by role."""
    show_costs = auth.can(user, "costs.view")
    score = state.get("quality_score", 0.0)
    return {
        "report_id": run["id"],
        "share_token": run.get("share_token"),
        "topic": run["topic"],
        "created_at": run["created_at"],
        "seconds": run["seconds"],
        "queries": state.get("search_queries", []),
        "findings": [public_finding(f) for f in state.get("findings", [])],
        "sources": state.get("sources", []),
        "analysis": state.get("analysis", ""),
        "report": state.get("report", ""),
        "quality": {
            "score": score,
            "feedback": state.get("quality_feedback", ""),
            # Mirrors quality_gate()
            "approved": score >= 0.7 or state.get("iteration", 0) >= 2,
        },
        "iterations": state.get("iteration", 0),
        "usage": run.get("usage") if show_costs else None,
        "trace": tracing.enabled() and show_costs,
        "owner": owner_name(run),
        "can_manage": can_manage_run(user, run),
    }


def owner_name(run: dict, names: dict | None = None) -> str | None:
    if run.get("user_id") is None:
        return None
    names = names if names is not None else auth.user_names()
    return names.get(run["user_id"], "Former member")


def execute_run(run_id: str, topic: str, sources: list[str], tracker: "usage.UsageTracker",
                user: dict, emit) -> None:
    """Run the research graph, passing progress events to emit().
    Releases its run slot when done."""
    initial_state = {
        "messages": [],
        "topic": topic,
        "search_sources": sources,
        "search_queries": [],
        "findings": [],
        "sources": [],
        "analysis": "",
        "report": "",
        "quality_score": 0.0,
        "quality_feedback": "",
        "iteration": 0,
    }
    final_state = initial_state
    started_at = time.time()
    show_costs = auth.can(user, "costs.view")

    emit({"type": "run_started", "run_id": run_id, "topic": topic, "model": CHAT_MODEL,
          "search": search_provider(), "sources": sources, "tracing": tracing.enabled()})
    # One LangSmith trace per run (when tracing is on); the tracker records cost
    config = {**tracing.run_config(run_id, topic, sources, [tracker]),
              **history.thread_config(run_id)}
    try:
        for mode, task in research_system.stream(
            initial_state, config, stream_mode=["tasks", "values"]
        ):
            if mode == "values":
                final_state = task
                continue

            name, task_id = task["name"], task["id"]
            if "input" in task:
                emit({"type": "agent_started", "agent": name, "task_id": task_id,
                      **task_started(name, task["input"])})
                continue

            if task.get("error"):
                raise RuntimeError(f"{name} failed: {task['error']}")

            result = task.get("result") or {}
            emit({"type": "agent_finished", "agent": name, "task_id": task_id,
                  "usage": tracker.summary() if show_costs else None, **task_finished(name, result)})

        history.finish_run(run_id, final_state, round(time.time() - started_at, 1), tracker.summary())
        threading.Thread(target=remember, args=(run_id, topic, final_state), daemon=True).start()
        report_chat.save_report(run_id, topic, final_state)
        emit({"type": "run_finished", **result_payload(history.get_run(run_id), final_state, user)})
    except Exception as exc:
        history.fail_run(run_id, str(exc), tracker.summary())
        emit({"type": "error", "message": str(exc)})
    finally:
        run_slots.release()
        emit(None)


def start_research(topic: str, sources: list[str], tracker: "usage.UsageTracker", user: dict) -> queue.Queue:
    """
    Start a run in a background thread and return its event queue. Closing
    the page doesn't stop the run: it still finishes and is saved to history.
    The caller must already hold a run slot.
    """
    run_id = uuid.uuid4().hex
    history.start_run(run_id, topic, sources, user["id"])
    events: queue.Queue = queue.Queue()
    threading.Thread(
        target=execute_run, args=(run_id, topic, sources, tracker, user, events.put), daemon=True
    ).start()
    return events


def stream_events(events: queue.Queue):
    while (event := events.get()) is not None:
        yield sse(event)


@app.get("/")
def index():
    return FileResponse(UI_FILE)


@app.get("/login")
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(LOGIN_PAGE)


# ============================================================
# Accounts: sign in/out, own password, team management
# ============================================================


def set_session_cookie(request: Request, response: Response, token: str, max_age: int | None) -> None:
    """Write the session cookie (an empty token with max_age=0 deletes it).

    Inside Hugging Face's iframe the app is a third-party site, which needs
    SameSite=None + Secure, plus Partitioned for browsers that block
    third-party cookies. Locally over http, SameSite=Lax is used. The header
    is built by hand because Starlette only supports Partitioned on Python 3.14+."""
    parts = [f"{auth.SESSION_COOKIE}={token}", "Path=/", "HttpOnly"]
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    if request.url.scheme == "https":
        parts += ["Secure", "SameSite=None", "Partitioned"]
    else:
        parts.append("SameSite=Lax")
    response.headers.append("set-cookie", "; ".join(parts))


@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request, response: Response):
    enforce_rate_limit(login_limiter, request, "sign-in attempts")
    try:
        user = auth.authenticate(body.username, body.password)
    except auth.LoginError as exc:
        log.warning("Failed sign-in for %r from %s", body.username[:80], client_id(request))
        raise HTTPException(status_code=401, detail=str(exc))
    token, lifetime = auth.create_session(user["id"], body.remember)
    set_session_cookie(request, response, token, lifetime if body.remember else None)
    return {"user": user}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    set_session_cookie(request, response, "", max_age=0)  # same attributes, so it really is removed
    return {"signed_out": True}


@app.get("/api/auth/me")
def me(request: Request):
    return {"user": current_user(request),
            "roles": {k: {"label": v["label"], "description": v["description"],
                          "permissions": sorted(v["permissions"])} for k, v in auth.ROLES.items()},
            "permissions": auth.PERMISSIONS}


@app.post("/api/auth/password")
def change_password(body: PasswordRequest, request: Request):
    user = current_user(request)
    if not auth.check_password(user["id"], body.current_password):
        raise HTTPException(status_code=400, detail="Your current password is incorrect.")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="Choose a different password from the current one.")
    if problem := auth.password_problem(body.new_password):
        raise HTTPException(status_code=400, detail=problem)
    auth.set_password(user["id"], body.new_password, must_change=False)
    auth.end_sessions(user["id"], keep_token=request.cookies.get(auth.SESSION_COOKIE))
    return {"user": auth.get_user(user["id"])}


@app.get("/api/users")
def users_list(request: Request):
    require(request, "users.manage")
    return {"users": auth.list_users()}


@app.post("/api/users")
def users_create(body: NewUserRequest, request: Request):
    require(request, "users.manage")
    password = auth.temporary_password()
    try:
        user = auth.create_user(body.username, body.name, body.role, password, must_change=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log.info("%s created account %s (%s)", current_user(request)["username"], user["username"], user["role"])
    return {"user": user, "temporary_password": password}


@app.patch("/api/users/{user_id}")
def users_update(user_id: int, body: UpdateUserRequest, request: Request):
    me_ = require(request, "users.manage")
    if user_id == me_["id"] and (body.active is False):
        raise HTTPException(status_code=400, detail="You can't deactivate your own account.")
    try:
        user = auth.update_user(user_id, name=body.name, role=body.role, active=body.active)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"user": user}


@app.post("/api/users/{user_id}/reset-password")
def users_reset_password(user_id: int, request: Request):
    require(request, "users.manage")
    if auth.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found.")
    password = auth.temporary_password()
    auth.set_password(user_id, password, must_change=True)
    auth.end_sessions(user_id)
    return {"temporary_password": password}


@app.get("/api/health")
def health():
    return {"status": "ok", "model": CHAT_MODEL, "search": search_provider(),
            "kb_documents": len(knowledge_base.list_documents()),
            "memory_reports": research_memory.count(),
            "backup": persistence.status(),
            "active_runs": run_slots.active,
            "tracing": tracing.enabled(), "langsmith_project": tracing.project() if tracing.enabled() else None}


@app.post("/api/research")
def research(body: ResearchRequest, request: Request):
    user = require(request, "research.run")
    topic = body.topic.strip()
    # Cheapest checks first: rate limit, then the safety guard, then a run slot
    enforce_rate_limit(research_limiter, request, "research runs")
    tracker = usage.UsageTracker()  # counts the safety check too
    enforce_guard(topic, "topic", tracker)
    if not run_slots.acquire():
        raise HTTPException(
            status_code=503,
            detail="The squad is busy with other research right now. Please try again in a minute.",
            headers={"Retry-After": "60"},
        )
    try:
        events = start_research(topic, list(dict.fromkeys(body.sources)), tracker, user)
    except Exception:
        run_slots.release()
        raise
    return StreamingResponse(
        stream_events(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# Knowledge base
# ============================================================


@app.get("/api/kb")
def kb_list():
    documents = knowledge_base.list_documents()
    return {"documents": documents, "chunks": sum(d["chunks"] for d in documents)}


@app.post("/api/kb/upload")
def kb_upload(request: Request, files: list[UploadFile] = File(...)):
    require(request, "kb.upload")
    enforce_rate_limit(upload_limiter, request, "uploads")
    results = []
    for upload in files:
        try:
            added = knowledge_base.add_file(upload.filename or "document", upload.file.read())
            results.append({"ok": True, **added})
        except ValueError as exc:
            results.append({"ok": False, "source": upload.filename, "error": str(exc)})
    return {"results": results}


@app.post("/api/kb/note")
def kb_note(note: NoteRequest, request: Request):
    require(request, "kb.upload")
    enforce_rate_limit(upload_limiter, request, "uploads")
    try:
        return knowledge_base.add_note(note.title, note.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/kb/documents/{source}")
def kb_delete(source: str, request: Request):
    require(request, "kb.delete")
    if not knowledge_base.delete_document(source):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"deleted": source}


@app.get("/api/kb/search")
def kb_search(q: str, k: int = 5):
    return {"results": knowledge_base.search(q, k=min(k, 10))}


@app.get("/api/kb/files/{source}")
def kb_file(source: str):
    path = knowledge_base.file_path(source)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    media_type = "application/pdf" if path.suffix == ".pdf" else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media_type, content_disposition_type="inline")


# ============================================================
# Follow-up chat on a report
# ============================================================


def stream_chat(session: "report_chat.ReportSession", question: str, tracker: "usage.UsageTracker",
                show_costs: bool):
    try:
        for text in session.stream_answer(question, callbacks=[tracker]):
            yield sse({"type": "token", "text": text})
        answer_usage = tracker.summary()
        run_usage = history.add_usage(session.run_id, answer_usage)
        yield sse({"type": "done", "usage": answer_usage if show_costs else None,
                   "run_usage": run_usage if show_costs else None})
    except Exception as exc:
        yield sse({"type": "error", "message": str(exc)})


@app.post("/api/reports/{report_id}/chat")
def report_followup(report_id: str, body: ChatRequest, request: Request):
    user = require(request, "research.run")
    session = report_chat.get_session(report_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="This report was not found. It may have been deleted from history.",
        )
    question = body.question.strip()
    enforce_rate_limit(chat_limiter, request, "follow-up questions")
    tracker = usage.UsageTracker()
    enforce_guard(question, "followup", tracker)
    return StreamingResponse(
        stream_chat(session, question, tracker, auth.can(user, "costs.view")),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# Evaluation
# ============================================================


@app.get("/api/evals")
def evals_status(request: Request):
    require(request, "eval.run")
    return evaluation.status()


@app.post("/api/evals/run", status_code=202)
def evals_run(body: EvalRequest, request: Request):
    require(request, "eval.run")
    unknown = [i for i in body.ids or [] if i not in evaluation.BY_ID]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown evaluations: {', '.join(unknown)}")
    enforce_rate_limit(eval_limiter, request, "evaluation runs")
    if not evaluation.start(body.ids, body.include_llm):
        raise HTTPException(status_code=409, detail="An evaluation is already running.")
    return evaluation.status()


# ============================================================
# Saved history
# ============================================================


@app.get("/api/runs")
def runs_list(request: Request, q: str = "", limit: int = 100):
    user = current_user(request)
    show_costs = auth.can(user, "costs.view")
    names = auth.user_names()
    runs = history.list_runs(q, limit=min(limit, 200))
    for run in runs:
        run["owner"] = owner_name(run, names)
        run["can_manage"] = can_manage_run(user, run)
        if not show_costs:
            run["usage"] = run["cost"] = None
        if run["snippet"]:
            run["snippet"] = run["snippet"].replace(history.MARK_START, "[[").replace(history.MARK_END, "]]")
    return {"runs": runs}


@app.get("/api/runs/{run_id}")
def runs_get(run_id: str, request: Request):
    user = current_user(request)
    run = history.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    state = history.load_state(run_id) if run["status"] == "done" else None
    run["owner"] = owner_name(run)
    run["can_manage"] = can_manage_run(user, run)
    if not auth.can(user, "costs.view"):
        run["usage"] = run["cost"] = None
    return {
        "run": run,
        "result": result_payload(run, state, user) if state else None,
        "chat": history.get_chat(run_id),
    }


@app.get("/api/runs/{run_id}/trace")
def runs_trace(run_id: str, request: Request):
    """Open this run's trace in LangSmith."""
    require(request, "costs.view")
    url = tracing.trace_url(run_id)
    if url is None:
        raise HTTPException(
            status_code=404,
            detail="No LangSmith trace for this run (tracing was off, or it is still uploading).",
        )
    return RedirectResponse(url)


@app.delete("/api/runs/{run_id}")
def runs_delete(run_id: str, request: Request):
    run = history.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    require_run_manager(request, run)
    if not history.delete_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    report_chat.forget(run_id)
    research_memory.delete(run_id)
    return {"deleted": run_id}


# ============================================================
# Export: Markdown, print/PDF page, share links
# ============================================================


def existing_run(run_id: str) -> dict:
    run = history.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return run


def finished_run(run_id: str) -> tuple[dict, dict]:
    run = history.get_run(run_id)
    state = history.load_state(run_id) if run and run["status"] == "done" else None
    if state is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return run, state


def shared_run(token: str) -> tuple[dict, dict]:
    run = history.get_run_by_share_token(token)
    state = history.load_state(run["id"]) if run else None
    if state is None:
        raise HTTPException(status_code=404, detail="This share link is invalid or was turned off.")
    return run, state


def markdown_response(run: dict, state: dict, download: bool) -> Response:
    markdown = report_export.to_markdown(report_export.document(run, state))
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{report_export.slugify(run["topic"])}.md"'
    return Response(markdown, media_type="text/markdown; charset=utf-8", headers=headers)


@app.get("/api/runs/{run_id}/document")
def run_document(run_id: str):
    return report_export.document(*finished_run(run_id))


@app.get("/api/runs/{run_id}/export.md")
def run_markdown(run_id: str, download: bool = False):
    return markdown_response(*finished_run(run_id), download)


@app.post("/api/runs/{run_id}/share")
def share_create(run_id: str, request: Request):
    require_run_manager(request, existing_run(run_id))
    run, _ = finished_run(run_id)
    token = run["share_token"] or secrets.token_urlsafe(16)
    history.set_share_token(run_id, token)
    return {"token": token, "path": f"/share/{token}"}


@app.delete("/api/runs/{run_id}/share")
def share_revoke(run_id: str, request: Request):
    require_run_manager(request, existing_run(run_id))
    finished_run(run_id)
    history.set_share_token(run_id, None)
    return {"shared": False}


@app.get("/api/share/{token}")
def share_document(token: str):
    return report_export.document(*shared_run(token))


@app.get("/api/share/{token}/export.md")
def share_markdown(token: str, download: bool = False):
    return markdown_response(*shared_run(token), download)


@app.get("/share/{token}")
def share_page(token: str):
    return FileResponse(REPORT_PAGE)


@app.get("/runs/{run_id}/print")
def print_page(run_id: str):
    return FileResponse(REPORT_PAGE)
