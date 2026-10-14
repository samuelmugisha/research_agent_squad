"""
Follow-up chat on a finished research report.

Each report is stored with its sources, findings and analysis, plus its own
conversation history (InMemoryChatMessageHistory, as in research_assistant.py),
so follow-ups like "expand recommendation 2" or "why?" keep their context.

Sessions are cached in memory; after a restart they are rebuilt from the
saved run (history.py), including the earlier conversation.
"""

import threading
from collections import OrderedDict

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import history
from security import UNTRUSTED_RULE, untrusted
from multi_agent_research_system import creative_llm, format_findings, format_sources

MAX_REPORTS = 50
MAX_HISTORY_MESSAGES = 10

SYSTEM_PROMPT = """You are Atlas, lead of a research agent squad. The user is asking \
follow-up questions about a research report your team produced.

Answer using the report, analysis, findings and sources below. Rules:
- Cite sources inline with bracketed numbers like [1] or [2][3], using ONLY numbers \
from the Sources list. Cite every factual claim that comes from a source.
- When asked to expand a section or item (e.g. "recommendation 2"), find it in the \
report and go deeper: concrete steps, trade-offs, and supporting evidence.
- If the material does not cover the question, say so plainly and suggest a new \
research question the user could run. Do not invent facts or sources.
- Use markdown. Be concise unless asked for detail. Do not add a references section.
- {untrusted_rule}

# Topic
{topic}

# Report
{report}

# Analysis
{analysis}

# Findings
{findings}

# Sources
{sources}"""


class ReportSession:
    def __init__(self, run_id: str, topic: str, state: dict):
        self.run_id = run_id
        self.topic = topic
        self.system = SystemMessage(
            content=SYSTEM_PROMPT.format(
                topic=topic,
                report=state["report"],
                analysis=state["analysis"],
                findings=untrusted(format_findings(state["findings"], state["sources"])),
                sources=untrusted(format_sources(state["sources"]) or "(none)"),
                untrusted_rule=UNTRUSTED_RULE,
            )
        )
        self.chat = InMemoryChatMessageHistory()
        for message in history.get_chat(run_id):
            cls = HumanMessage if message["role"] == "user" else AIMessage
            self.chat.add_message(cls(content=message["content"]))
        self.lock = threading.Lock()  # one answer at a time per report

    def stream_answer(self, question: str, callbacks: list | None = None):
        """Yield the answer in chunks, then save the exchange to history."""
        with self.lock:
            messages = [
                self.system,
                *self.chat.messages[-MAX_HISTORY_MESSAGES:],
                HumanMessage(content=question),
            ]
            answer = ""
            config = {"callbacks": callbacks or [], "run_name": "follow_up_chat",
                      "metadata": {"usage_label": "follow_up_chat", "app_run_id": self.run_id}}
            for chunk in creative_llm.stream(messages, config=config):
                if chunk.content:
                    answer += chunk.content
                    yield chunk.content
            self.chat.add_message(HumanMessage(content=question))
            self.chat.add_message(AIMessage(content=answer))
            history.add_chat_message(self.run_id, "user", question)
            history.add_chat_message(self.run_id, "assistant", answer)


_sessions: "OrderedDict[str, ReportSession]" = OrderedDict()
_sessions_lock = threading.Lock()


def _cache(session: ReportSession) -> ReportSession:
    with _sessions_lock:
        _sessions[session.run_id] = session
        _sessions.move_to_end(session.run_id)
        while len(_sessions) > MAX_REPORTS:
            _sessions.popitem(last=False)
    return session


def save_report(run_id: str, topic: str, state: dict) -> None:
    _cache(ReportSession(run_id, topic, state))


def get_session(run_id: str) -> ReportSession | None:
    """Return the chat session, rebuilding it from saved history if needed."""
    with _sessions_lock:
        if run_id in _sessions:
            return _sessions[run_id]
    run = history.get_run(run_id)
    state = history.load_state(run_id) if run and run["status"] == "done" else None
    return _cache(ReportSession(run_id, run["topic"], state)) if state else None


def forget(run_id: str) -> None:
    with _sessions_lock:
        _sessions.pop(run_id, None)
