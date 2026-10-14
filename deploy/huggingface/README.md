---
title: Research Agent Squad
emoji: 🔎
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: Five AI agents research the web, papers and your docs
---

# Research Agent Squad

A multi-agent research assistant built with LangGraph. Ask a question and watch
five agents work together:

| Agent | Role |
| --- | --- |
| **Atlas** | Research lead: plans focused search queries |
| **Scout** | Searches the web, recent academic papers (OpenAlex, arXiv) and your knowledge base, in parallel |
| **Prism** | Analyst: synthesizes findings and numbers the sources |
| **Quill** | Writer: produces a cited report |
| **Sentinel** | Reviewer: scores the report and sends it back for revision if needed |

Also included: a knowledge base for your own PDFs and notes, follow-up chat on
each report, searchable history, and export to Markdown, PDF or a share link.

## Configuration

Set in the Space's **Settings → Variables and secrets**:

| Name | Type | Example |
| --- | --- | --- |
| `OPENAI_API_KEY` | secret | OpenRouter or OpenAI key |
| `OPENAI_BASE_URL` | variable | `https://openrouter.ai/api/v1` |
| `CHAT_MODEL` | variable | `openai/gpt-4o-mini` |
| `EMBEDDING_MODEL` | variable | `openai/text-embedding-3-small` |
| `TAVILY_API_KEY` | secret, optional | use Tavily for web search instead of OpenRouter |
| `DATA_DIR` | variable, optional | `/data` when persistent storage is enabled |

Without persistent storage, history and uploaded documents are cleared
whenever the Space restarts.
