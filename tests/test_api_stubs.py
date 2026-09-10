"""Step-0 contract tests.

These lock the API's response *shapes*. They must keep passing unchanged after
M7 replaces the stub bodies with real orchestrator calls - if one of these
breaks during integration, the contract moved and both branches need to agree.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.contracts import AgentResult, IngestResult, NetworkStatus, TaskStatus, TaskType

client = TestClient(app)


def test_health() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_public_config_exposes_no_filesystem_paths() -> None:
    body = client.get("/api/config").json()
    assert set(body["models"]) >= {"document", "code", "general"}
    assert "downloads_dir" not in body


def test_tools_are_json_schema_shaped() -> None:
    tools = client.get("/api/tools").json()
    assert {tool["name"] for tool in tools} >= {
        "read_file",
        "write_file",
        "list_files",
        "search_documents",
        "run_python",
    }
    for tool in tools:
        assert tool["input_schema"]["type"] == "object"
        assert "handler" not in tool  # never serialise the callable


def test_ingest_persists_upload_and_returns_documents() -> None:
    response = client.post(
        "/api/ingest",
        files={"file": ("scan.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
    )
    assert response.status_code == 200
    result = IngestResult.model_validate(response.json())
    assert result.filename == "scan.pdf"
    assert result.page_count == len(result.documents) > 0
    assert all(document.page >= 1 for document in result.documents)


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("Draft an approval note from the attached inspection report", TaskType.DOCUMENT),
        ("Write a python script to compute wall loss", TaskType.CODE),
        ("Who signed off on the last turnaround?", TaskType.GENERAL),
    ],
)
def test_router_is_visible_in_the_result(task: str, expected: TaskType) -> None:
    result = AgentResult.model_validate(client.post("/api/tasks", json={"task": task}).json())
    assert result.task_type is expected
    assert result.model_used, "the routing badge needs a model name"
    assert result.routing_reason, "the routing badge needs a reason (M2)"


def test_task_result_is_well_formed_and_retrievable() -> None:
    """Shape invariants that hold with or without a model server running.

    Updated at M7: the stub version also asserted that ``sources`` and
    ``deliverables`` were non-empty, but those were properties of the canned
    data. Sources now depend on the corpus actually being indexed, and
    deliverables on H2, so asserting them here tested the fake rather than the
    contract. The per-field invariants below are the durable part; content
    assertions live in the tests that control their own fixtures.
    """
    result = AgentResult.model_validate(
        client.post("/api/tasks", json={"task": "Review inspection report E-4102"}).json()
    )

    assert [step.step_number for step in result.steps] == list(range(1, len(result.steps) + 1))
    assert all(source.page >= 1 for source in result.sources)
    assert all(deliverable.filename for deliverable in result.deliverables)
    assert result.status in set(TaskStatus)
    assert result.final_text.strip(), "every result must say something the UI can render"
    assert client.get(f"/api/tasks/{result.task_id}").status_code == 200


def test_a_dead_model_server_is_reported_as_a_failed_result_not_a_500() -> None:
    """The demo must never show a stack trace.

    With no local model running, a task comes back as a FAILED AgentResult
    carrying an explanation - a shape the UI already knows how to render.
    """
    from src.core.orchestrator import get_orchestrator

    if get_orchestrator().health().get("inference_available"):
        pytest.skip("local inference server is up; this covers the unreachable case")

    response = client.post("/api/tasks", json={"task": "Review inspection report E-4102"})
    assert response.status_code == 200, "an unreachable model is a result, not a server error"

    result = AgentResult.model_validate(response.json())
    assert result.status is TaskStatus.FAILED
    assert result.error
    assert "ollama" in result.final_text.lower(), "tell the operator how to fix it"


def test_stream_emits_routing_then_steps_then_result() -> None:
    result = client.post("/api/tasks", json={"task": "Review inspection report"}).json()
    with client.stream("GET", f"/api/tasks/{result['task_id']}/stream") as response:
        assert response.status_code == 200
        events = [
            line.removeprefix("event: ")
            for line in response.iter_lines()
            if line.startswith("event: ")
        ]
    assert events[0] == "routing"
    assert events[-1] == "done"
    assert events.count("step") == len(result["steps"])


def test_unknown_task_is_404() -> None:
    assert client.get("/api/tasks/task_missing").status_code == 404
    assert client.get("/api/tasks/task_missing/stream").status_code == 404


def test_deliverable_download_round_trips() -> None:
    deliverables = client.get("/api/deliverables").json()
    assert deliverables
    response = client.get(deliverables[0]["download_url"])
    assert response.status_code == 200
    assert response.content


def test_deliverable_path_traversal_is_refused() -> None:
    assert client.get("/api/deliverables/..%2F..%2Fconfig.yaml").status_code == 404
    assert client.get("/api/deliverables/nope.docx").status_code == 404


def test_network_monitor_reports_zero_external_calls() -> None:
    status = NetworkStatus.model_validate(client.get("/api/network").json())
    assert status.external_calls == 0
    assert status.violations == []
    assert status.allowed_hosts, "the monitor must say which hosts it tolerates"


def test_no_provider_sdk_is_importable_from_the_app() -> None:
    """PROPOSAL.md ss2.2 - an `anthropic`/`openai` import in the repo is a defect."""
    import sys

    banned = {"anthropic", "openai", "google.generativeai", "cohere", "mistralai"}
    assert banned.isdisjoint(sys.modules)
