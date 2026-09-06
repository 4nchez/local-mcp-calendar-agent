"""API Server 테스트.

AI 계층 없이 도메인 서버 단독으로 검증한다.
(이 서버가 LLM/MCP를 몰라야 한다는 설계 원칙이 지켜지는지에 대한 확인이기도 하다)
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app, store

KST = timezone(timedelta(hours=9))


@pytest.fixture(autouse=True)
def clean_store():
    store.clear()
    yield
    store.clear()


@pytest.fixture
def client():
    return TestClient(app)


def at(hour: int, minute: int = 0, day: int = 6) -> str:
    return datetime(2026, 3, day, hour, minute, tzinfo=KST).isoformat()


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_create_and_read(client):
    res = client.post("/schedules", json={"title": "면접", "start_at": at(14)})
    assert res.status_code == 201
    body = res.json()
    assert body["title"] == "면접"
    # end_at을 안 주면 1시간짜리로 채워진다
    assert body["end_at"].startswith("2026-03-06T15:00")

    assert client.get(f"/schedules/{body['id']}").status_code == 200
    assert len(client.get("/schedules").json()) == 1


def test_conflict_is_rejected(client):
    client.post("/schedules", json={"title": "면접", "start_at": at(14)})
    res = client.post("/schedules", json={"title": "회의", "start_at": at(14, 30)})
    assert res.status_code == 409
    assert res.json()["detail"]["conflicts"][0]["title"] == "면접"


def test_force_overrides_conflict(client):
    client.post("/schedules", json={"title": "면접", "start_at": at(14)})
    res = client.post("/schedules?force=true", json={"title": "회의", "start_at": at(14, 30)})
    assert res.status_code == 201
    assert len(client.get("/schedules").json()) == 2


def test_touching_edges_is_not_conflict(client):
    """14:00~15:00과 15:00~16:00은 겹치지 않는다."""
    client.post("/schedules", json={"title": "면접", "start_at": at(14), "end_at": at(15)})
    res = client.post("/schedules", json={"title": "회의", "start_at": at(15), "end_at": at(16)})
    assert res.status_code == 201


def test_update_keeps_duration(client):
    created = client.post(
        "/schedules", json={"title": "면접", "start_at": at(14), "end_at": at(16)}
    ).json()
    res = client.patch(f"/schedules/{created['id']}", json={"start_at": at(18)})
    assert res.status_code == 200
    # 시작만 옮기면 2시간이라는 소요 시간은 유지된다
    assert res.json()["end_at"].startswith("2026-03-06T20:00")


def test_update_ignores_self_conflict(client):
    created = client.post("/schedules", json={"title": "면접", "start_at": at(14)}).json()
    assert client.patch(f"/schedules/{created['id']}", json={"title": "최종 면접"}).status_code == 200


def test_delete(client):
    created = client.post("/schedules", json={"title": "면접", "start_at": at(14)}).json()
    assert client.delete(f"/schedules/{created['id']}").status_code == 204
    assert client.delete(f"/schedules/{created['id']}").status_code == 404


def test_filters(client):
    client.post("/schedules", json={"title": "면접", "start_at": at(14, day=6)})
    client.post("/schedules", json={"title": "팀 회의", "start_at": at(14, day=10)})

    assert len(client.get("/schedules", params={"keyword": "회의"}).json()) == 1
    ranged = client.get(
        "/schedules", params={"start_from": at(0, day=6), "start_to": at(0, day=7)}
    ).json()
    assert len(ranged) == 1 and ranged[0]["title"] == "면접"


def test_invalid_range_is_rejected(client):
    res = client.post("/schedules", json={"title": "면접", "start_at": at(14), "end_at": at(13)})
    assert res.status_code == 422
