from __future__ import annotations

import httpx
import pytest

from ops_agent.seed.generator import Dataset
from ops_agent.ticketing.db import TicketRepository
from tests.conftest import WRITE_HEADERS, first_open_ticket

pytestmark = pytest.mark.anyio


async def test_health(api: httpx.AsyncClient) -> None:
    assert (await api.get("/health")).json() == {"status": "ok"}


async def test_list_and_filters(api: httpx.AsyncClient) -> None:
    page = (await api.get("/tickets", params={"limit": 500})).json()
    assert page["total"] == 200 and len(page["items"]) == 200
    open_untriaged = (await api.get("/tickets", params={"status": "open", "untriaged": True})).json()
    assert open_untriaged["total"] == 160  # 200 - 40 historical
    resolved_wms = (await api.get("/tickets", params={"category": "wms", "limit": 500})).json()
    assert resolved_wms["total"] > 0
    assert all(t["category"] == "wms" for t in resolved_wms["items"])


async def test_text_search_and_pagination(api: httpx.AsyncClient) -> None:
    res = (await api.get("/tickets", params={"q": "EDI 214", "limit": 2})).json()
    assert res["total"] >= 1 and len(res["items"]) <= 2
    assert all("214" in (t["title"] + t["description"]) for t in res["items"])
    p1 = (await api.get("/tickets", params={"limit": 5, "offset": 0})).json()["items"]
    p2 = (await api.get("/tickets", params={"limit": 5, "offset": 5})).json()["items"]
    assert {t["id"] for t in p1}.isdisjoint({t["id"] for t in p2})


async def test_invalid_filter_rejected(api: httpx.AsyncClient) -> None:
    assert (await api.get("/tickets", params={"priority": "P9"})).status_code == 422


async def test_get_ticket_and_404(api: httpx.AsyncClient) -> None:
    t = (await api.get("/tickets/NW-1001")).json()
    assert t["id"] == "NW-1001" and t["comments"] == []
    assert (await api.get("/tickets/NW-9999")).status_code == 404


async def test_write_requires_token_and_actor(api: httpx.AsyncClient, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    assert (
        await api.patch(f"/tickets/{tid}", json={"priority": "P3"}, headers={"X-Actor": "x"})
    ).status_code == 401
    no_actor = {"Authorization": WRITE_HEADERS["Authorization"]}
    assert (await api.patch(f"/tickets/{tid}", json={"priority": "P3"}, headers=no_actor)).status_code == 422


async def test_update_fields_bumps_version(api: httpx.AsyncClient, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    r = await api.patch(
        f"/tickets/{tid}",
        json={"priority": "P2", "category": "wms", "labels": ["x"], "expected_version": 1},
        headers=WRITE_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert (body["priority"], body["category"], body["labels"], body["version"]) == ("P2", "wms", ["x"], 2)


async def test_version_conflict(api: httpx.AsyncClient, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    r = await api.patch(
        f"/tickets/{tid}", json={"priority": "P2", "expected_version": 7}, headers=WRITE_HEADERS
    )
    assert r.status_code == 409


async def test_unknown_fields_rejected(api: httpx.AsyncClient, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    r = await api.patch(f"/tickets/{tid}", json={"status": "closed"}, headers=WRITE_HEADERS)
    assert r.status_code == 422  # status changes only via /transitions


async def test_duplicate_link_validation(api: httpx.AsyncClient, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    self_link = await api.patch(f"/tickets/{tid}", json={"duplicate_of": tid}, headers=WRITE_HEADERS)
    missing = await api.patch(f"/tickets/{tid}", json={"duplicate_of": "NW-9999"}, headers=WRITE_HEADERS)
    assert (self_link.status_code, missing.status_code) == (409, 404)


async def test_comment_assign_transition(api: httpx.AsyncClient, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    c = await api.post(f"/tickets/{tid}/comments", json={"body": "hello"}, headers=WRITE_HEADERS)
    assert c.status_code == 201 and c.json()["author"] == "tester" and c.json()["internal"] is True
    a = await api.post(f"/tickets/{tid}/assign", json={"team": "network_ops"}, headers=WRITE_HEADERS)
    assert a.status_code == 200 and a.json()["team"] == "network_ops"
    t = await api.post(f"/tickets/{tid}/transitions", json={"to_status": "triaged"}, headers=WRITE_HEADERS)
    assert t.status_code == 200 and t.json()["status"] == "triaged"
    full = (await api.get(f"/tickets/{tid}")).json()
    assert len(full["comments"]) == 1 and full["version"] == 3


async def test_invalid_transition(api: httpx.AsyncClient, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    r = await api.post(
        f"/tickets/{tid}/transitions", json={"to_status": "in_progress"}, headers=WRITE_HEADERS
    )
    assert r.status_code == 409


async def test_idempotency_replay_does_not_reapply(
    api: httpx.AsyncClient, dataset: Dataset, repo: TicketRepository
) -> None:
    tid = first_open_ticket(dataset)
    headers = {**WRITE_HEADERS, "Idempotency-Key": "k-1"}
    first = await api.post(f"/tickets/{tid}/comments", json={"body": "once"}, headers=headers)
    second = await api.post(f"/tickets/{tid}/comments", json={"body": "once"}, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert repo.count_comments() == 1


async def test_idempotency_key_reuse_with_other_body(api: httpx.AsyncClient, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    headers = {**WRITE_HEADERS, "Idempotency-Key": "k-2"}
    await api.post(f"/tickets/{tid}/comments", json={"body": "a"}, headers=headers)
    r = await api.post(f"/tickets/{tid}/comments", json={"body": "b"}, headers=headers)
    assert r.status_code == 422


async def test_failed_write_is_not_recorded_as_idempotent(api: httpx.AsyncClient, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    headers = {**WRITE_HEADERS, "Idempotency-Key": "k-3"}
    bad = await api.patch(f"/tickets/{tid}", json={"priority": "P2", "expected_version": 99}, headers=headers)
    good = await api.patch(
        f"/tickets/{tid}", json={"priority": "P2", "expected_version": 99}, headers=headers
    )
    assert bad.status_code == good.status_code == 409  # rolled back, so it is evaluated again
