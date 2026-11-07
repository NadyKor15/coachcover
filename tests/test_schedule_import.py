"""POST /api/import: the school's schedule file -> importers -> shifts in the database -> the rules over it."""

from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover.api import create_app
from coachcover.checks import night_report
from coachcover.demo import clock_now, reset_school
from coachcover.seed import POLICY_ID
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db

FIX = Path(__file__).parent / "fixtures" / "import"

# Anna Kim twice at the same hour in one file: the second row cannot be saved, the check names the overlap.
DOUBLE_BOOKED_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CoachCover tests//double booking//EN
BEGIN:VEVENT
UID:a@example.com
SUMMARY:Lifeguard
CATEGORIES:Lifeguard
LOCATION:Maplewood Swim Center
DTSTART;TZID=America/New_York:20261110T140000
DTEND;TZID=America/New_York:20261110T160000
ATTENDEE;CN=Anna Kim:mailto:anna.kim@example.com
END:VEVENT
BEGIN:VEVENT
UID:b@example.com
SUMMARY:Lifeguard
CATEGORIES:Lifeguard
LOCATION:Lakeview Natatorium
DTSTART;TZID=America/New_York:20261110T150000
DTEND;TZID=America/New_York:20261110T170000
ATTENDEE;CN=Anna Kim:mailto:anna.kim@example.com
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def api(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dbname = conn.execute("SELECT current_database()").fetchone()[0]
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    with TestClient(create_app(make_conninfo(TEST_DSN, dbname=dbname))) as client:
        yield client, conn


def _upload(client, name: str, data: bytes, **form):
    return client.post("/api/import", files={"file": (name, data, "text/csv")}, data=form)


def test_school_csv_is_saved_and_checked_bad_rows_named(api):
    client, conn = api
    before = conn.execute("SELECT count(*) FROM shift").fetchone()[0]
    res = _upload(client, "school-monday.csv", (FIX / "school-monday.csv").read_bytes())
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["format"] == "generic CSV" and body["rows"] == 6
    assert body["errors"] == ["line 7, position: 'Barista' is not a role of this business"
                              " ['Head coach', 'Lifeguard', 'Swim instructor']"]
    assert any("Pat Doe" in w for w in body["warnings"])
    assert (body["saved"], body["already_there"], body["open"], body["not_saved"]) == (5, 0, 1, [])
    assert conn.execute("SELECT count(*) FROM shift").fetchone()[0] == before + 5
    assert conn.execute("SELECT count(*) FROM shift WHERE id LIKE 'IMP-%%' AND status = 'open' AND staff_id IS NULL"
                        ).fetchone()[0] == 1  # Pat Doe is not on the staff list: the gap stays visible
    rows = {row["name"]: row for row in body["checks"]}
    assert body["checked"] == 4 and body["blocked"] == 3
    assert (rows["Anna Kim"]["ok"], rows["Anna Kim"]["blocking"], rows["Anna Kim"]["line"]) == (False, ["CERT_EXPIRED"], 2)
    assert rows["Javier Morales"]["blocking"] == ["CERT_EXPIRED"]  # CPR ended Nov 8
    assert rows["Camila Herrera"]["blocking"] == ["HOURS_LIMIT_EXCEEDED"]  # 39.5 h already in the stored week + 4 h
    assert (rows["Grace Sullivan"]["ok"], rows["Grace Sullivan"]["warnings"]) == (True, ["CERT_EXPIRES_ON_SHIFT_DAY"])
    assert all(row["saved"] for row in body["checks"])
    assert abs((datetime.fromisoformat(body["checked_at"]) - clock_now(conn)).total_seconds()) < 60  # the demo clock


def test_reimport_saves_nothing_new_and_the_night_check_sees_the_imported_shifts(api):
    client, conn = api
    data = (FIX / "school-monday.csv").read_bytes()
    first = _upload(client, "school-monday.csv", data).json()
    again = _upload(client, "school-monday.csv", data).json()
    assert (again["saved"], again["already_there"]) == (0, first["saved"])
    assert [r["blocking"] for r in again["checks"]] == [r["blocking"] for r in first["checks"]]  # no self-overlap
    anna = next(r["shift_id"] for r in first["checks"] if r["name"] == "Anna Kim")
    report = night_report(conn, POLICY_ID, date(2026, 11, 9), clock_now(conn))
    problem = next(p for p in report["problems"] if p["shift_id"] == anna)
    assert [r["code"] for r in problem["reasons"]] == ["CERT_EXPIRED"]


# Gabriela Vargas has 18 h in the stored week; two 10 h rows make 38 h of the 40 h limit. A re-import must not
# count the stored copy of her other imported row a second time (48 h would block both rows).
TWO_LONG_DAYS = (b"Date,Start,End,Location,Position,Employee\n"
                 b"11/09/2026,8:00 AM,6:00 PM,Lakeview Natatorium,Lifeguard,Gabriela Vargas\n"
                 b"11/11/2026,8:00 AM,6:00 PM,Lakeview Natatorium,Lifeguard,Gabriela Vargas\n")


def test_reimport_does_not_count_a_stored_imported_row_twice(api):
    client, _ = api
    first = _upload(client, "two-days.csv", TWO_LONG_DAYS).json()
    again = _upload(client, "two-days.csv", TWO_LONG_DAYS).json()
    assert [r["ok"] for r in first["checks"]] == [True, True], first
    assert (again["saved"], again["already_there"]) == (0, 2)
    assert [r["ok"] for r in again["checks"]] == [True, True], again


def test_ical_double_booking_is_not_saved_and_is_named(api):
    client, conn = api
    res = _upload(client, "week.ics", DOUBLE_BOOKED_ICS)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["format"] == "generic iCal" and body["saved"] == 1
    assert [(n["line"], n["staff_id"]) for n in body["not_saved"]] == [(13, "S27")]
    assert all("OVERLAPS_COMMITMENT" in r["blocking"] for r in body["checks"])
    assert [r["saved"] for r in body["checks"]] == [True, False]


def test_bad_uploads_are_refused(api):
    client, _ = api
    assert _upload(client, "x.csv", b"").status_code == 422
    assert _upload(client, "x.csv", (FIX / "school-monday.csv").read_bytes(), tz="Mars/Base").status_code == 422
    assert _upload(client, "x.csv", (FIX / "school-monday.csv").read_bytes(), venue_id="V9").status_code == 422
    assert _upload(client, "x.ics", DOUBLE_BOOKED_ICS, date_from="2026-11-01").status_code == 422
    assert _upload(client, "x.csv", b"a" * (2 * 1024 * 1024 + 1)).status_code == 413
