"""Focused contract tests for the CoWork hackathon bug fixes."""
import csv
import io
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app, raise_server_exceptions=False)


def _org(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _future(hours: int) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    )


def _register(org: str, username: str = "admin") -> dict:
    response = client.post(
        "/auth/register",
        json={"org_name": org, "username": username, "password": "pw12345"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(org: str, username: str = "admin") -> tuple[dict, dict]:
    response = client.post(
        "/auth/login",
        json={"org_name": org, "username": username, "password": "pw12345"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body


def _create_room(headers: dict, name: str, rate: int = 1000) -> int:
    response = client.post(
        "/rooms",
        json={"name": name, "capacity": 4, "hourly_rate_cents": rate},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_booking(headers: dict, room_id: int, start: datetime, end: datetime):
    return client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        },
        headers=headers,
    )


def test_rooms_contract_shape_and_duplicate_names_allowed():
    org = _org("rooms")
    _register(org)
    headers, _ = _login(org)

    _create_room(headers, "Focus")
    _create_room(headers, "Focus")

    response = client.get("/rooms", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert [room["name"] for room in body] == ["Focus", "Focus"]


def test_room_creation_invalidates_usage_report_cache():
    org = _org("report-cache")
    _register(org)
    headers, _ = _login(org)
    date = _future(72).date().isoformat()

    _create_room(headers, "First")
    before = client.get(f"/admin/usage-report?from={date}&to={date}", headers=headers)
    assert before.status_code == 200
    assert len(before.json()["rooms"]) == 1

    _create_room(headers, "Second")
    after = client.get(f"/admin/usage-report?from={date}&to={date}", headers=headers)
    assert after.status_code == 200
    assert [room["room_name"] for room in after.json()["rooms"]] == ["First", "Second"]


def test_availability_and_usage_report_filter_by_start_time():
    org = _org("start-time")
    _register(org)
    headers, _ = _login(org)
    room_id = _create_room(headers, "Late Room", rate=1500)

    start = _future(96).replace(hour=23)
    end = start + timedelta(hours=2)
    created = _create_booking(headers, room_id, start, end)
    assert created.status_code == 201, created.text

    next_date = (start + timedelta(days=1)).date().isoformat()
    availability = client.get(f"/rooms/{room_id}/availability?date={next_date}", headers=headers)
    assert availability.status_code == 200
    assert availability.json()["busy"] == []

    report = client.get(f"/admin/usage-report?from={next_date}&to={next_date}", headers=headers)
    assert report.status_code == 200
    row = report.json()["rooms"][0]
    assert row["confirmed_bookings"] == 0
    assert row["revenue_cents"] == 0


def test_usage_report_accepts_iso_datetime_bounds():
    org = _org("report-iso")
    _register(org)
    headers, _ = _login(org)
    room_id = _create_room(headers, "Iso Room", rate=2000)

    start = _future(120)
    created = _create_booking(headers, room_id, start, start + timedelta(hours=1))
    assert created.status_code == 201, created.text

    frm = (start - timedelta(minutes=5)).isoformat()
    to = (start + timedelta(minutes=5)).isoformat()
    report = client.get("/admin/usage-report", params={"from": frm, "to": to}, headers=headers)
    assert report.status_code == 200
    assert report.json()["rooms"][0]["confirmed_bookings"] == 1


def test_refresh_token_is_single_use_under_concurrency():
    org = _org("refresh")
    _register(org)
    _, tokens = _login(org)
    refresh_token = tokens["refresh_token"]
    barrier = threading.Barrier(12)

    def attempt() -> int:
        barrier.wait()
        return client.post("/auth/refresh", json={"refresh_token": refresh_token}).status_code

    with ThreadPoolExecutor(max_workers=12) as executor:
        statuses = [future.result() for future in as_completed([executor.submit(attempt) for _ in range(12)])]

    assert statuses.count(200) == 1
    assert statuses.count(401) == 11


def test_admin_export_is_org_scoped_and_rejects_cross_org_room_id():
    org = _org("export")
    other_org = _org("export-other")
    _register(org)
    _register(org, "member")
    _register(other_org)
    admin_headers, _ = _login(org)
    member_headers, _ = _login(org, "member")
    other_headers, _ = _login(other_org)

    room_id = _create_room(admin_headers, "Org Room")
    other_room_id = _create_room(other_headers, "Other Room")
    start = _future(144)
    booking = _create_booking(member_headers, room_id, start, start + timedelta(hours=1))
    assert booking.status_code == 201, booking.text

    export = client.get("/admin/export?include_all=false", headers=admin_headers)
    assert export.status_code == 200
    rows = list(csv.reader(io.StringIO(export.text)))
    assert rows[0] == [
        "id",
        "reference_code",
        "room_id",
        "user_id",
        "start_time",
        "end_time",
        "status",
        "price_cents",
    ]
    assert len(rows) == 2
    assert int(rows[1][2]) == room_id

    cross_org = client.get(
        f"/admin/export?include_all=true&room_id={other_room_id}",
        headers=admin_headers,
    )
    assert cross_org.status_code == 404
    assert cross_org.json()["code"] == "ROOM_NOT_FOUND"


def test_invalid_booking_datetime_returns_contract_error():
    org = _org("bad-date")
    _register(org)
    headers, _ = _login(org)
    room_id = _create_room(headers, "Date Room")

    response = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": "not-a-date", "end_time": "also-bad"},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_BOOKING_WINDOW"


def test_concurrent_double_booking_reference_codes_and_cancel_idempotency():
    org = _org("concurrency")
    _register(org)
    headers, _ = _login(org)
    room_id = _create_room(headers, "Conflict Room")
    start = _future(168)
    end = start + timedelta(hours=1)
    barrier = threading.Barrier(10)

    def attempt_booking() -> tuple[int, str | None]:
        barrier.wait()
        response = _create_booking(headers, room_id, start, end)
        body = response.json()
        return response.status_code, body.get("reference_code")

    with ThreadPoolExecutor(max_workers=10) as executor:
        booking_results = [
            future.result() for future in as_completed([executor.submit(attempt_booking) for _ in range(10)])
        ]

    assert [status for status, _ in booking_results].count(201) == 1
    assert [status for status, _ in booking_results].count(409) == 9
    reference_codes = [code for status, code in booking_results if status == 201]
    assert len(reference_codes) == len(set(reference_codes))

    booking_id = client.get("/bookings", headers=headers).json()["items"][0]["id"]
    cancel_barrier = threading.Barrier(8)

    def attempt_cancel() -> int:
        cancel_barrier.wait()
        return client.post(f"/bookings/{booking_id}/cancel", headers=headers).status_code

    with ThreadPoolExecutor(max_workers=8) as executor:
        cancel_statuses = [
            future.result() for future in as_completed([executor.submit(attempt_cancel) for _ in range(8)])
        ]

    assert cancel_statuses.count(200) == 1
    assert cancel_statuses.count(409) == 7
    detail = client.get(f"/bookings/{booking_id}", headers=headers)
    assert len(detail.json()["refunds"]) == 1


def test_concurrent_successful_bookings_have_unique_references_and_rate_limit_counts_all_requests():
    org = _org("references-rate")
    _register(org)
    headers, _ = _login(org)
    room_ids = [_create_room(headers, f"Room {i}") for i in range(8)]
    barrier = threading.Barrier(8)

    def attempt_success(index: int) -> tuple[int, str | None]:
        start = _future(220 + index * 2)
        barrier.wait()
        response = _create_booking(headers, room_ids[index], start, start + timedelta(hours=1))
        body = response.json()
        return response.status_code, body.get("reference_code")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = [
            future.result()
            for future in as_completed([executor.submit(attempt_success, index) for index in range(8)])
        ]

    assert all(status == 201 for status, _ in results)
    reference_codes = [code for _, code in results]
    assert len(reference_codes) == len(set(reference_codes))

    limited_org = _org("rate-limit")
    _register(limited_org)
    limited_headers, _ = _login(limited_org)
    statuses = []
    for index in range(21):
        start = _future(260 + index)
        response = _create_booking(limited_headers, 999999, start, start + timedelta(hours=1))
        statuses.append(response.status_code)

    assert statuses[:20] == [404] * 20
    assert statuses[20] == 429
