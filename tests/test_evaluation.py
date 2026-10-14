"""Evaluation page API and the free deterministic checks (no LLM calls)."""

from unittest.mock import patch

from conftest import signed_in_client

import evaluation
import research_ui_server as server

client = signed_in_client("principal_investigator")


def test_lists_every_evaluation_as_not_run():
    data = client.get("/api/evals").json()
    assert [e["id"] for e in data["evaluations"]] == [e.id for e in evaluation.EVALUATIONS]
    assert {e["type"] for e in data["evaluations"]} == {"Deterministic", "LLM", "LLM Judge"}


def test_unknown_evaluation_is_rejected():
    assert client.post("/api/evals/run", json={"ids": ["nope"]}).status_code == 400


def test_second_run_while_running_is_rejected():
    with patch.object(evaluation, "start", return_value=False):
        assert client.post("/api/evals/run", json={}).status_code == 409


def test_injection_pattern_eval_passes():
    result = evaluation.eval_injection_patterns()
    assert result["status"] == "pass" and len(result["details"]) == 20


def test_history_checks_skip_without_reports():
    with patch.object(evaluation, "saved_reports", return_value=[]):
        assert evaluation.eval_citation_integrity()["status"] == "skipped"
        assert evaluation.eval_quality_scores()["status"] == "skipped"


def test_citation_integrity_catches_invalid_citation():
    run = {"topic": "t"}
    state = {"report": "- A long factual bullet point about something important [1][7]",
             "sources": [{"id": 1}]}
    with patch.object(evaluation, "saved_reports", return_value=[(run, state)]):
        result = evaluation.eval_citation_integrity()
    assert result["status"] == "fail" and "[7]" in result["details"][0]["got"]
