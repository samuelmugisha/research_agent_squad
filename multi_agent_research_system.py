"""
Section 4 Project: Multi-Agent Research System
Combines all patterns from Section 4 into a working research pipeline.

Patterns used:
- Supervisor architecture (4.3)
- Parallel execution (4.5) via Send API
- Shared state / blackboard (4.6)
- Iterative refinement loop
"""

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import Send
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, BaseMessage, ToolMessage
from langchain_core.runnables.config import ContextThreadPoolExecutor
from langchain_core.tools import tool
from typing_extensions import TypedDict, Annotated
from typing import Literal
from pydantic import BaseModel, Field
import operator
import json
import os
from dotenv import load_dotenv

load_dotenv()

from datetime import date

import knowledge_base
import research_memory
from security import UNTRUSTED_RULE, untrusted
from scholarly_search import scholarly_search
from web_search import search_provider, web_search

DEFAULT_SEARCH_SOURCES = ["web", "academic", "kb", "memory"]

CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")
llm = ChatOpenAI(model=CHAT_MODEL, temperature=0)
creative_llm = ChatOpenAI(model=CHAT_MODEL, temperature=0.7, stream_usage=True)


def parse_json(text: str):
    """Parse JSON from an LLM reply, tolerating ```json fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(text)


def number_sources(findings: list[dict]) -> list[dict]:
    """Give every unique source URL across all findings a citation number."""
    sources, seen = [], set()
    for finding in findings:
        for source in finding.get("sources", []):
            url = source.get("url")
            if url and url not in seen:
                seen.add(url)
                sources.append({
                    "id": len(sources) + 1,
                    "title": source.get("title") or url,
                    "url": url,
                    "kind": source.get("kind", "web"),
                    "meta": source.get("meta", ""),
                })
    return sources


def format_findings(findings: list[dict], sources: list[dict]) -> str:
    """Render findings as bullet points tagged with their [n] citations."""
    ids = {s["url"]: s["id"] for s in sources}
    lines = []
    for f in findings:
        cites = "".join(f"[{ids[s['url']]}]" for s in f.get("sources", []) if s.get("url") in ids)
        lines.append(f"- {f.get('title', '')}: {f.get('detail', '')} {cites}".rstrip())
    return "\n".join(lines)


def format_sources(sources: list[dict]) -> str:
    def label(s):
        if s.get("kind") == "kb":
            return "user's knowledge base"
        if s.get("kind") == "academic":
            return f"academic paper: {s.get('meta', '')}"
        if s.get("kind") == "memory":
            return "earlier report by this research squad"
        return s["url"]

    return "\n".join(f"[{s['id']}] {s['title']} ({label(s)})" for s in sources)


# ============================================================
# State Schema
# ============================================================


class ResearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    topic: str
    search_sources: list[str]  # any of "web", "academic", "kb"
    search_queries: list[str]
    findings: Annotated[list[dict], operator.add]
    tool_calls: Annotated[list[dict], operator.add]  # every search tool Scout called
    sources: list[dict]
    analysis: str
    report: str
    quality_score: float
    quality_feedback: str
    iteration: int


# State for individual search tasks (used with Send API)
class SearchTaskState(TypedDict):
    search_query: str
    search_sources: list[str]
    findings: Annotated[list[dict], operator.add]
    tool_calls: Annotated[list[dict], operator.add]


# ============================================================
# Tools: the searches Scout can call (function calling)
# The docstrings are what the LLM reads to decide which tool fits.
# ============================================================


@tool
def search_web(query: str) -> list[dict]:
    """Search the live web. Best for current facts, news, statistics, prices,
    products, companies, markets, regulations and practical how-to guidance."""
    return [{**r, "kind": "web"} for r in web_search(query)]


@tool
def search_papers(query: str) -> list[dict]:
    """Search recent peer-reviewed journal articles (OpenAlex) and arXiv
    preprints. Best for scientific, medical, technical or academic evidence.
    Use a short keyword query, e.g. 'solid-state battery electrolyte'."""
    return scholarly_search(query)


@tool
def search_knowledge_base(query: str) -> list[dict]:
    """Search the user's own uploaded documents and notes. Use it whenever the
    query could relate to the user's projects, organisation, suppliers or files."""
    return knowledge_base.search(query)


@tool
def search_past_research(query: str) -> list[dict]:
    """Search reports this research squad wrote earlier (long-term memory).
    Use it to build on previous research on the same or related topics, or
    when the query refers to earlier work ("last time", "previously")."""
    return research_memory.search(query)


SEARCH_TOOLS = {"web": search_web, "academic": search_papers, "kb": search_knowledge_base,
                "memory": search_past_research}
MAX_TOOL_ROUNDS = 2  # first searches + one optional follow-up search


def available_tools(use: list[str]) -> list:
    """Tools for the sources the user selected, minus any that can't work."""
    tools = []
    for source in use:
        if source == "web" and search_provider() == "none":
            continue
        if source == "kb" and knowledge_base.chunk_count() == 0:
            continue
        if source == "memory" and research_memory.count() == 0:
            continue
        if source in SEARCH_TOOLS:
            tools.append(SEARCH_TOOLS[source])
    return tools


def run_tool(tools: dict, call: dict) -> tuple[list[dict], str | None]:
    """Execute one tool call. Errors are returned, not raised, so one failing
    search doesn't stop the run and the LLM can see what went wrong."""
    selected = tools.get(call["name"])
    if selected is None:
        return [], f"Unknown tool '{call['name']}'"
    try:
        return selected.invoke(call["args"]), None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def describe_results(results: list[dict], error: str | None) -> str:
    """Short summary of a tool's results, sent back to the LLM as a ToolMessage."""
    if error:
        return f"Search failed: {error}"
    if not results:
        return "No results."
    return f"{len(results)} results:\n" + "\n".join(
        untrusted(f"{r['title']}: {r['content'][:200]}") for r in results
    )


# ============================================================
# Node: Supervisor — Plans the research
# ============================================================


def supervisor(state: ResearchState) -> dict:
    """Plans research by generating targeted search queries."""

    response = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are a research supervisor. Given a topic, generate exactly 3 "
                    "specific search queries that will cover different angles of the topic. "
                    "Return ONLY a JSON array of strings. No markdown formatting."
                )
            ),
            HumanMessage(content=f"Research topic: {state['topic']}"),
        ]
    )

    try:
        queries = parse_json(response.content)
    except json.JSONDecodeError:
        # Fallback: split by newlines
        queries = [
            f"{state['topic']} overview",
            f"{state['topic']} latest developments",
            f"{state['topic']} practical applications",
        ]

    return {
        "search_queries": queries[:3],
        "messages": [
            AIMessage(
                content=f"[SUPERVISOR]: Planned {len(queries)} research queries: {queries}",
                name="supervisor",
            )
        ],
    }


# ============================================================
# Node: Search Agent — Executes a single search query
# (Launched in parallel via Send API)
# ============================================================


SCOUT_TOOL_PROMPT = (
    "You are Scout, a research agent with search tools. For the research query, "
    "decide which tools are likely to hold relevant evidence and call them. "
    "You may call several tools at once, or one tool several times with different "
    "phrasings. Write each tool query as a concise search phrase, not a sentence. "
    "Skip tools that clearly don't fit the query. After seeing the results you may "
    "search once more if results are missing, failed or off-topic; otherwise reply "
    "DONE without calling tools.\n\n" + UNTRUSTED_RULE
)


def gather_results(query: str, use: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Let the LLM choose which search tools to call (function calling), run the
    calls in parallel, and return (all unique results, a log of the calls).
    """
    tools = available_tools(use)
    if not tools:
        return [], []
    by_name = {t.name: t for t in tools}

    results, log, seen = [], [], set()

    def execute(calls: list[dict]) -> list[str]:
        # Tool calls are slow network searches, so run them together. The
        # context-copying pool keeps them inside this run's trace and usage tracking.
        with ContextThreadPoolExecutor(max_workers=len(calls)) as pool:
            outputs = list(pool.map(lambda c: run_tool(by_name, c), calls))
        summaries = []
        for call, (found, error) in zip(calls, outputs):
            new = []
            for r in found:
                key = (r.get("url"), r.get("content", "")[:200])
                if key not in seen:
                    seen.add(key)
                    new.append(r)
            results.extend(new)
            log.append({"tool": call["name"], "query": call["args"].get("query", ""),
                        "results": len(new), "error": error})
            summaries.append(describe_results(new, error))
        return summaries

    messages = [SystemMessage(content=SCOUT_TOOL_PROMPT), HumanMessage(content=f"Today's date: {date.today()}\nResearch query: {query}")]
    try:
        scout = llm.bind_tools(tools)
        for _ in range(MAX_TOOL_ROUNDS):
            response = scout.invoke(messages)
            if not response.tool_calls:
                break
            messages.append(response)
            for call, summary in zip(response.tool_calls, execute(response.tool_calls)):
                messages.append(ToolMessage(content=summary, tool_call_id=call["id"]))
    except Exception as exc:
        # e.g. the model doesn't support tool calling; keep what we have
        log.append({"tool": "scout", "query": query, "results": 0, "error": str(exc)})

    if not results and not any(entry["error"] is None for entry in log):
        # The model searched nothing usable: fall back to every available tool
        execute([{"name": t.name, "args": {"query": query}, "id": t.name} for t in tools])

    return results, log


def search_agent(state: SearchTaskState) -> dict:
    """
    One Scout per query, run in parallel via the Send API. The LLM picks which
    search tools to call (web, academic papers, the user's knowledge base),
    then extracts findings with sources from the results.
    """
    query = state["search_query"]
    use = state.get("search_sources") or DEFAULT_SEARCH_SOURCES

    search_results, tool_calls = gather_results(query, use)

    if not search_results:
        errors = [f"{c['tool']}: {c['error']}" for c in tool_calls if c["error"]]
        detail = "No relevant results found." + (f" Search errors: {'; '.join(errors)}" if errors else "")
        return {
            "findings": [{"title": query, "detail": detail, "sources": [], "source_query": query}],
            "tool_calls": tool_calls,
        }

    return {"findings": extract_findings(query, search_results), "tool_calls": tool_calls}


def extract_findings(query: str, search_results: list[dict]) -> list[dict]:
    """Turn numbered search results into findings that cite real sources."""

    def origin(r):
        if r["kind"] == "kb":
            return "knowledge base"
        if r["kind"] == "academic":
            return "academic paper"
        if r["kind"] == "memory":
            return "past report"
        return r["url"]

    results_text = "\n\n".join(
        f"[{i}] ({origin(r)})\n" + untrusted(f"{r['title']}\n{r['content']}")
        for i, r in enumerate(search_results, 1)
    )
    response = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are a research agent. Using ONLY the numbered search results, "
                    "extract 3-4 key findings for the query. Each finding needs a 'title', "
                    "a 'detail' with specific facts (figures, dates, names), and 'sources': "
                    "a list of the result numbers it came from.\n"
                    "- 'knowledge base' results are the user's own documents: use them "
                    "whenever they are relevant.\n"
                    "- 'past report' results are earlier reports by this squad. Use them "
                    "for continuity (e.g. what was found before, what changed), but prefer "
                    "primary web or academic sources for facts when both are available.\n"
                    "- 'academic paper' results are recent journal articles or arXiv "
                    "preprints: include at least one finding from them when any are "
                    "relevant, mention the venue and year, and say 'preprint' for arXiv. "
                    "Ignore papers that are off-topic.\n"
                    "Return a JSON array. No markdown.\n\n" + UNTRUSTED_RULE
                )
            ),
            HumanMessage(content=f"Search query: {query}\n\nSearch results:\n{results_text}"),
        ]
    )

    try:
        findings = parse_json(response.content)
    except json.JSONDecodeError:
        all_ids = list(range(1, len(search_results) + 1))
        findings = [{"title": query, "detail": response.content, "sources": all_ids}]
    if isinstance(findings, dict):
        findings = [findings]

    # Swap result numbers for real {title, url} sources and tag the query
    for f in findings:
        ids = f.get("sources") or []
        f["sources"] = [
            {key: search_results[i - 1].get(key, "") for key in ("title", "url", "kind", "meta")}
            for i in ids
            if isinstance(i, int) and 1 <= i <= len(search_results)
        ]
        f["source_query"] = query

    return findings


# ============================================================
# Edge: Fan-out searches using Send API
# ============================================================


def dispatch_searches(state: ResearchState) -> list[Send]:
    """Dynamically create parallel search tasks using Send API."""
    return [
        Send(
            "search_agent",
            {
                "search_query": query,
                "search_sources": state.get("search_sources") or DEFAULT_SEARCH_SOURCES,
                "findings": [],
            },
        )
        for query in state["search_queries"]
    ]


# ============================================================
# Node: Analyst — Synthesizes all findings
# ============================================================


def analyst(state: ResearchState) -> dict:
    """Reads all findings from the blackboard and synthesizes."""
    sources = number_sources(state["findings"])
    findings_text = format_findings(state["findings"], sources)

    response = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are a research analyst. Synthesize the collected findings into "
                    "a clear analysis. Identify:\n"
                    "1. Key themes across all findings\n"
                    "2. Any contradictions or gaps\n"
                    "3. The most important insights\n\n"
                    "Write 2-3 paragraphs. Keep the [n] citation markers next to "
                    "the claims they support. The findings were extracted from "
                    "retrieved content.\n\n" + UNTRUSTED_RULE
                )
            ),
            HumanMessage(
                content=(
                    f"Research topic: {state['topic']}\n\n"
                    f"Collected findings:\n{untrusted(findings_text)}"
                )
            ),
        ]
    )

    return {
        "sources": sources,
        "analysis": response.content,
        "messages": [
            AIMessage(content=f"[ANALYST]: {response.content}", name="analyst")
        ],
    }


# ============================================================
# Node: Report Writer — Produces the final report
# ============================================================


def report_writer(state: ResearchState) -> dict:
    """Writes a structured research report from the analysis."""

    # Include quality feedback if this is a revision
    revision_note = ""
    if state["iteration"] > 0 and state.get("quality_feedback"):
        revision_note = (
            f"\n\nIMPORTANT — This is revision #{state['iteration']}. "
            f"Address this feedback: {state['quality_feedback']}"
        )

    # Add a literature section when Scouts found academic papers
    research_section = ""
    if any(s.get("kind") == "academic" for s in state.get("sources", [])):
        research_section = (
            "5. Recent Research (bullet points: each key paper's finding, with its "
            "journal or 'arXiv preprint' and year, and a citation)\n"
        )

    response = creative_llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are a report writer. Produce a well-structured research report "
                    "with these sections:\n"
                    "1. Executive Summary (2-3 sentences)\n"
                    "2. Key Findings (bullet points)\n"
                    "3. Analysis (1-2 paragraphs)\n"
                    "4. Recommendations (3 actionable items)\n"
                    f"{research_section}\n"
                    "Use markdown formatting. Be specific and actionable.\n\n"
                    "Cite sources inline with bracketed numbers like [1] or [2][3], "
                    "using ONLY numbers from the Sources list. Cite every factual claim. "
                    "Do not add a references section — it is added automatically.\n\n"
                    + UNTRUSTED_RULE
                    + revision_note
                )
            ),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n\n"
                    f"Analysis:\n{state['analysis']}\n\n"
                    f"Findings:\n{untrusted(format_findings(state['findings'], state.get('sources', [])))}\n\n"
                    f"Sources:\n{untrusted(format_sources(state.get('sources', [])))}"
                )
            ),
        ]
    )

    return {
        "report": response.content,
        "messages": [
            AIMessage(
                content=f"[REPORT WRITER]: Report {'revised' if state['iteration'] > 0 else 'drafted'}.",
                name="report_writer",
            )
        ],
    }


# ============================================================
# Node: Quality Checker — Reviews and scores the report
# ============================================================


class QualityReview(BaseModel):
    score: float = Field(description="Quality score from 0.0 to 1.0")
    feedback: str = Field(description="Specific feedback for improvement")
    approved: bool = Field(description="Whether the report meets quality standards")


def quality_checker(state: ResearchState) -> dict:
    """Reviews the report and either approves or sends back for revision."""

    review_llm = llm.with_structured_output(QualityReview)

    review = review_llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are a quality reviewer. Score the report on:\n"
                    "- Completeness: Does it cover the topic well?\n"
                    "- Clarity: Is it well-written and easy to understand?\n"
                    "- Actionability: Are recommendations specific?\n"
                    "- Citations: Are factual claims backed by [n] source citations?\n\n"
                    "Score from 0.0 to 1.0. Approve if score >= 0.7.\n"
                    "If this is iteration 2 or higher, be more lenient."
                )
            ),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"Iteration: {state['iteration']}\n\n"
                    f"Report:\n{state['report']}"
                )
            ),
        ]
    )

    # Force approve after 2 iterations to prevent infinite loops
    approved = review.approved or state["iteration"] >= 2

    return {
        "quality_score": review.score,
        "quality_feedback": review.feedback,
        "iteration": state["iteration"] + 1,
        "messages": [
            AIMessage(
                content=(
                    f"[QUALITY CHECK]: Score {review.score:.1f} — "
                    f"{'APPROVED' if approved else 'REVISION NEEDED'}: {review.feedback}"
                ),
                name="quality_checker",
            )
        ],
    }


# ============================================================
# Build the full research system
# ============================================================


def create_research_system(checkpointer=None):
    """
    Builds the multi-agent research system graph.

    Flow:
    1. Supervisor plans search queries
    2. Search agents run in parallel (Send API)
    3. Analyst synthesizes findings
    4. Report writer produces report
    5. Quality checker reviews — loops back if needed
    """

    graph = StateGraph(ResearchState)

    # Add all nodes
    graph.add_node("supervisor", supervisor)
    graph.add_node("search_agent", search_agent)
    graph.add_node("analyst", analyst)
    graph.add_node("report_writer", report_writer)
    graph.add_node("quality_checker", quality_checker)

    # Edges
    graph.add_edge(START, "supervisor")

    # Supervisor → parallel search agents (dynamic fan-out)
    graph.add_conditional_edges("supervisor", dispatch_searches, ["search_agent"])

    # All search agents → analyst (fan-in)
    graph.add_edge("search_agent", "analyst")

    # Analyst → report writer
    graph.add_edge("analyst", "report_writer")

    # Report writer → quality checker
    graph.add_edge("report_writer", "quality_checker")

    # Quality gate: approve or revise
    graph.add_conditional_edges(
        "quality_checker", quality_gate, {"report_writer": "report_writer", "end": END}
    )

    return graph.compile(checkpointer=checkpointer)


# Demos
def demo_research_with_streaming():
    """Run the research system with step-by-step streaming output."""

    system = create_research_system()
    graph = system.get_graph()
    png_data = graph.draw_mermaid_png()

    with open("research_graph.png", "wb") as f:
        f.write(png_data)

    topic = "Best practices for building multi-agent AI systems"
    print(f"Streaming Research: {topic}\n")

    initial_state = {
        "messages": [],
        "topic": topic,
        "search_queries": [],
        "findings": [],
        "sources": [],
        "analysis": "",
        "report": "",
        "quality_score": 0.0,
        "quality_feedback": "",
        "iteration": 0,
    }

    # Stream updates to see each step as it happens
    for step in system.stream(initial_state, stream_mode="updates"):
        for node_name, update in step.items():
            print(f"[{node_name}] completed")

            # Show interesting state changes
            if "search_queries" in update and update["search_queries"]:
                print(f"  Planned queries: {update['search_queries']}")
            if "findings" in update and update["findings"]:
                print(f"  Found {len(update['findings'])} results")
            if "quality_score" in update:
                print(f"  Quality score: {update['quality_score']:.1f}")
            if "report" in update and update["report"]:
                print(f"  Report length: {len(update['report'])} chars")

        print()


# ============================================================
# Routing: Quality gate
# ============================================================


def quality_gate(state: ResearchState) -> Literal["report_writer", "end"]:
    """Route back to writer if quality is insufficient."""
    if state["quality_score"] >= 0.7 or state["iteration"] >= 2:
        return "end"
    return "report_writer"


def demo_individual_search():
    """Demo just the search agent for testing."""

    print("Individual Search Agent Test:\n")

    # Test the search agent directly
    result = search_agent(
        {"search_query": "LangGraph multi-agent patterns", "findings": []}
    )

    print(f"Findings from search:")
    for f in result["findings"]:
        print(f"  - {f.get('title', 'N/A')}: {f.get('detail', 'N/A')[:80]}...")


def demo_full_research():
    """Run the complete research pipeline."""

    system = create_research_system()

    print("Multi-Agent Research System Demo")
    print("=" * 60)

    topic = "The impact of AI agents on software development in 2026"
    print(f"Topic: {topic}\n")

    result = system.invoke(
        {
            "messages": [],
            "topic": topic,
            "search_queries": [],
            "findings": [],
            "sources": [],
            "analysis": "",
            "report": "",
            "quality_score": 0.0,
            "quality_feedback": "",
            "iteration": 0,
        }
    )

    # Print the conversation trace
    print("Agent Activity Log:")
    print("-" * 40)
    for msg in result["messages"]:
        if isinstance(msg, AIMessage):
            print(f"  {msg.content[:120]}...")
    print()

    # Print final stats
    print(f"Total findings collected: {len(result['findings'])}")
    print(f"Quality score: {result['quality_score']:.1f}")
    print(f"Iterations: {result['iteration']}")
    print()

    # Print the final report
    print("=" * 60)
    print("FINAL RESEARCH REPORT")
    print("=" * 60)
    print(result["report"])


if __name__ == "__main__":
    # Run the individual search agent demo
    # demo_individual_search()
    # print("\n" + "=" * 50 + "\n")
    # Run the full research system demo with streaming output
    # demo_research_with_streaming()
    # print("\n" + "=" * 50 + "\n")
    # Run the full research system demo without streaming output
    demo_full_research()
