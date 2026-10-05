"""HTTP-level integration tests for requirement 8."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from abuse.api import create_app
from abuse.config import Config


@pytest.fixture
def client(tmp_path):
    config = Config.default()
    app = create_app(str(tmp_path / "api.db"), config=config, start_sweeper=False)
    with TestClient(app) as c:
        yield c


def _headers(actor_id: str, role: str = "user"):
    return {"X-Actor-Id": actor_id, "X-Actor-Role": role}


def _report(client, reporter, target="t1", event="e1", role="user", source="web"):
    return client.post(
        "/reports",
        json={"source": source, "target_account_id": target, "event_key": event, "evidence": "ev"},
        headers=_headers(reporter, role),
    )


def test_auth_required(client):
    r = client.post(
        "/reports",
        json={"source": "web", "target_account_id": "t", "event_key": "e", "evidence": ""},
    )
    assert r.status_code == 401


def test_duplicate_report_http_is_idempotent(client):
    r1 = _report(client, "u1")
    assert r1.status_code == 200 and r1.json()["is_new"] is True
    r2 = _report(client, "u1")
    assert r2.status_code == 200
    body = r2.json()
    assert body["is_new"] is False
    assert body["duplicate_count"] == 2
    assert body["report"]["id"] == r1.json()["report"]["id"]
    assert body["case"]["evidence_count"] == 1


def test_review_and_action_flow_http(client):
    case_id = _report(client, "u1").json()["case"]["id"]

    # user cannot review
    assert client.post(f"/cases/{case_id}/review", headers=_headers("u1")).status_code == 403

    r = client.post(f"/cases/{case_id}/review", headers=_headers("rev1", "reviewer"))
    assert r.status_code == 200
    assert r.json()["status"] == "in_review"

    r = client.post(
        f"/cases/{case_id}/actions",
        json={"action_type": "ban_timed", "reason": "spam", "basis": "3 reports", "duration_seconds": 60},
        headers=_headers("rev1", "reviewer"),
    )
    assert r.status_code == 200, r.text
    action = r.json()
    assert action["status"] == "active"
    assert action["expires_at"] is not None

    # enforcement explanation is available
    r = client.get("/accounts/t1/enforcement", headers=_headers("rev1", "reviewer"))
    assert r.json()["active"]["type"] == "ban_timed"
    assert "automatically released" in r.json()["active"]["explanation"]


def test_appeal_flow_http(client):
    case_id = _report(client, "u1").json()["case"]["id"]
    client.post(
        f"/cases/{case_id}/actions",
        json={"action_type": "mute", "reason": "r", "basis": "b", "duration_seconds": 60},
        headers=_headers("rev1", "reviewer"),
    )
    # non-owner blocked
    assert client.post(f"/cases/{case_id}/appeals", json={"reason": "x"},
                       headers=_headers("other")).status_code == 403
    r = client.post(f"/cases/{case_id}/appeals", json={"reason": "not me"},
                    headers=_headers("t1"))
    assert r.status_code == 200
    appeal_id = r.json()["id"]

    r = client.post(f"/appeals/{appeal_id}/decision", json={"grant": True, "reason": "exonerated"},
                    headers=_headers("boss", "admin"))
    assert r.status_code == 200 and r.json()["status"] == "granted"
    r = client.get("/accounts/t1/enforcement", headers=_headers("t1"))
    assert r.json()["active"] is None


def test_rate_limit_http(client):
    for i in range(5):
        assert _report(client, "flooder", target="v", event=f"e{i}").status_code == 200
    r = _report(client, "flooder", target="v", event="e5")
    assert r.status_code == 429


def test_audit_records_decisions_and_denials(client):
    case_id = _report(client, "u1").json()["case"]["id"]
    client.post(f"/cases/{case_id}/review", headers=_headers("mallory"))  # denied 403
    r = client.get("/audit", headers=_headers("rev1", "reviewer"))
    assert r.status_code == 200
    actions = {e["action"] for e in r.json()["entries"]}
    assert "report.submit" in actions
    denied = [e for e in r.json()["entries"] if not e["allowed"]]
    assert denied and denied[0]["actor_id"] == "mallory"


def test_illegal_transition_http_conflict(client):
    case_id = _report(client, "u1").json()["case"]["id"]
    r = client.post(
        f"/cases/{case_id}/transition",
        json={"status": "appealed"},
        headers=_headers("rev1", "reviewer"),
    )
    assert r.status_code == 409
