"""HTTP 接口端到端测试。"""
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ABUSE_DB_PATH", str(tmp_path / "api.db"))
    import importlib
    import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


REVIEWER = {"X-Actor": "rev1", "X-Role": "reviewer"}
ADMIN = {"X-Actor": "admin1", "X-Role": "admin"}


def _make_case(client, target="troll"):
    resp = client.post("/reports", json={
        "reporter_id": "u1", "target_account": target,
        "event_id": "evt-1", "reason": "辱骂", "evidence": ["shot-1"],
    })
    assert resp.status_code == 201
    return resp.json()["case_id"]


def test_full_workflow_over_http(client):
    case_id = _make_case(client)

    # 重复提交归并
    again = client.post("/reports", json={
        "reporter_id": "u1", "target_account": "troll",
        "event_id": "evt-1", "reason": "辱骂",
    })
    assert again.status_code == 201
    case = client.get(f"/cases/{case_id}").json()
    assert case["report_count"] == 1

    # 越权复核被拒
    resp = client.post(f"/cases/{case_id}/triage",
                       json={"reason": "x"}, headers={"X-Actor": "u9", "X-Role": "reporter"})
    assert resp.status_code == 403

    assert client.post(f"/cases/{case_id}/triage",
                       json={"reason": "受理"}, headers=REVIEWER).status_code == 200
    resp = client.post(f"/cases/{case_id}/decide", json={
        "action": "temp_ban", "reason": "屡犯", "duration": 3600,
        "evidence_refs": ["shot-1"],
    }, headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_at"] == pytest.approx(body["effective_at"] + 3600)

    # 非本人申诉被拒
    resp = client.post(f"/cases/{case_id}/appeals",
                       json={"reason": "冤枉"}, headers={"X-Actor": "u9"})
    assert resp.status_code == 403

    # 本人申诉 -> 复核撤销
    resp = client.post(f"/cases/{case_id}/appeals",
                       json={"reason": "我是本人，证据有误"}, headers={"X-Actor": "troll"})
    assert resp.status_code == 201
    appeal_id = resp.json()["appeal_id"]
    resp = client.post(f"/appeals/{appeal_id}/decide",
                       json={"uphold": False, "reason": "证据不足"}, headers=ADMIN)
    assert resp.status_code == 200
    assert client.get("/accounts/troll/sanction").json()["active"] is False

    # 审计链完整可还原
    trail = client.get("/audit", params={"target": case_id}).json()
    actions = [e["action"] for e in trail]
    for expected in ("submit_report", "triage", "decide",
                     "appeal_denied", "file_appeal", "appeal_overturned"):
        assert expected in actions
    decide_ev = next(e for e in trail if e["action"] == "decide")
    assert decide_ev["actor"] == "admin1" and decide_ev["reason"] == "屡犯"


def test_perm_ban_requires_admin_over_http(client):
    case_id = _make_case(client)
    client.post(f"/cases/{case_id}/triage", json={}, headers=REVIEWER)
    resp = client.post(f"/cases/{case_id}/decide",
                       json={"action": "perm_ban", "reason": "x"}, headers=REVIEWER)
    assert resp.status_code == 403
