"""The judge's sample certificate check: both readers on a bundled synthetic card, on recorded answers."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover.api import create_app
from coachcover.demo import reset_school
from coachcover.models import SECOND_READER_MODEL, TEXT_MODEL
from coachcover.sample_cert import CARDS_DIR, SAMPLES
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parent / "fixtures"
URL = "/api/judge/sample-certificate"


@pytest.fixture
def api(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dbname = conn.execute("SELECT current_database()").fetchone()[0]
    monkeypatch.setenv("COACHCOVER_JUDGE_RUNS_PER_HOUR", "6")
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    monkeypatch.delenv("COACHCOVER_RECORDINGS_DIR", raising=False)
    with TestClient(create_app(make_conninfo(TEST_DSN, dbname=dbname))) as client:
        yield client, conn


def _written(conn) -> tuple[int, int, int]:
    """(applicants, their certificate rows, ledger rows of sample checks)."""
    return (
        conn.execute("SELECT count(*) FROM staff WHERE id LIKE 'SAMPLE-%'").fetchone()[0],
        conn.execute("SELECT count(*) FROM staff_cert WHERE staff_id LIKE 'SAMPLE-%'").fetchone()[0],
        conn.execute("SELECT count(*) FROM model_call WHERE ref ? 'sample_check'").fetchone()[0],
    )


def test_the_bundled_cards_are_the_recorded_ones():
    for sample in SAMPLES:
        source = FIXTURES / "cards" / (f"hard/{sample.id.split('-')[0]}.jpg" if sample.id.startswith("h") else sample.file)
        assert (CARDS_DIR / sample.file).read_bytes() == source.read_bytes(), sample.id


def test_readers_agree_accepted_with_both_ledger_rows_priced(api):
    client, conn = api
    res = client.post(URL, json={"sample": "c3-photo"})
    assert res.status_code == 200
    body = res.json()
    assert body["sample"]["id"] == "c3-photo" and body["sample"]["image_url"] == "/media/sample-cards/c3-photo.jpg"
    assert (body["outcome"], body["dates_agree"], body["verdict"]) == ("accepted", True, "Dates agree: accepted")
    assert body["flags"] == [] and body["mode"] == "replay"
    muse, omni = body["readings"]
    assert (muse["model"], omni["model"]) == (TEXT_MODEL, SECOND_READER_MODEL)
    assert (muse["reader"], omni["reader"]) == ("Muse Glimmer", "Nemotron Omni")
    for r in (muse, omni):
        assert (r["ok"], r["issued"], r["expires"], r["card_number"]) == (True, "2025-12-01", "2027-12-01", "CFAI-PR-90412")
        assert r["source"] == "recorded" and r["cost_usd"] > 0 and r["ms"] > 0
    assert (muse["ms"], omni["ms"]) == (1480, 8780)  # as recorded on 2026-11-06
    ledger = body["ledger"]
    assert [row["model"] for row in ledger] == [TEXT_MODEL, SECOND_READER_MODEL]
    assert all(row["ok"] and row["cost_usd"] > 0 and row["cost_label"].endswith("¢") and row["time_label"] for row in ledger)
    assert body["total_cost_usd"] == pytest.approx(sum(row["cost_usd"] for row in ledger)) and body["unpriced_calls"] == 0
    assert body["card"]["review"] is False and body["card"]["expired"] is False
    assert body["runs_left_this_hour"] == 5 and body["runs_per_hour"] == 6
    # the row is the applicant's, who is not on the roster
    assert conn.execute("SELECT full_name, active FROM staff WHERE id = 'SAMPLE-C3-PHOTO'").fetchone() == ("Anna Petrova", False)
    assert conn.execute("SELECT needs_review FROM staff_cert WHERE staff_id = 'SAMPLE-C3-PHOTO'").fetchone() == (False,)
    # the global ledger shows the same two calls
    calls = client.get("/api/models/ledger").json()["calls"]
    assert sum(c["purpose"] == "read_card" for c in calls) == 2


def test_readers_disagree_on_dates_sent_to_a_person(api):
    client, conn = api
    body = client.post(URL, json={"sample": "h22-photo"}).json()
    assert (body["outcome"], body["dates_agree"], body["verdict"]) == ("sent_to_person", False, "Dates disagree: sent to a person")
    assert "dates_disagree" in body["flags"]
    muse, omni = body["readings"]
    assert (muse["expires"], omni["expires"]) == ("2027-08-19", "2027-06-19")  # the recorded readings of the check
    # the check no. 2 kept the fields only: no tokens, so no price is shown for these two calls
    assert body["unpriced_calls"] == 2 and body["total_cost_label"] is None and muse["cost_label"] is None
    assert conn.execute("SELECT needs_review FROM staff_cert WHERE staff_id = 'SAMPLE-H22-PHOTO'").fetchone() == (True,)


def test_one_reader_failed_is_said_honestly_and_sent_to_a_person(api, tmp_path, monkeypatch):
    client, conn = api
    recordings = tmp_path / "models"
    recordings.mkdir()
    for path in (FIXTURES / "models").glob("*.json"):
        if path.name != "card-c3-photo-omni.json":
            shutil.copy(path, recordings / path.name)
    monkeypatch.setenv("COACHCOVER_RECORDINGS_DIR", str(recordings))
    res = client.post(URL, json={"sample": "c3-photo"})
    assert res.status_code == 200
    body = res.json()
    assert body["flags"] == [f"reader_failed:{SECOND_READER_MODEL}"]
    assert (body["outcome"], body["dates_agree"], body["verdict"]) == ("sent_to_person", None, "Reader failed: sent to a person")
    muse, omni = body["readings"]
    assert muse["ok"] is True and omni["ok"] is False and omni["issued"] is None
    assert "NoRecording" in omni["error"]
    assert [row["ok"] for row in body["ledger"]] == [True, False]
    assert body["card"]["review"] is True


def test_both_readers_failed_no_card_row_sent_to_a_person(api, tmp_path, monkeypatch):
    client, conn = api
    recordings = tmp_path / "models"
    recordings.mkdir()
    for path in (FIXTURES / "models").glob("*.json"):
        if not path.name.startswith("card-c3-photo-"):
            shutil.copy(path, recordings / path.name)
    monkeypatch.setenv("COACHCOVER_RECORDINGS_DIR", str(recordings))
    body = client.post(URL, json={"sample": "c3-photo"}).json()
    assert body["outcome"] == "sent_to_person" and body["verdict"] == "Reader failed: sent to a person"
    assert body["card"] is None and len(body["ledger"]) == 2


def test_samples_go_in_turn_and_unknown_sample_is_refused_uncounted(api):
    client, _ = api
    assert client.post(URL, json={"sample": "nope"}).status_code == 422
    first = client.post(URL).json()
    second = client.post(URL, json={}).json()
    assert (first["sample"]["id"], second["sample"]["id"]) == (SAMPLES[0].id, SAMPLES[1].id)
    assert (first["outcome"], second["outcome"]) == ("accepted", "sent_to_person")
    assert second["runs_left_this_hour"] == 4  # the refused request did not count


def test_the_check_counts_under_the_judge_limit_with_scenes(api):
    client, _ = api
    assert client.post("/api/judge/run", json={"scenario": "dangerous_morning"}).status_code == 200  # 1
    for i in range(4):  # 2..5
        assert client.post(URL).status_code == 200
    sixth = client.post(URL)
    assert sixth.status_code == 200 and sixth.json()["runs_left_this_hour"] == 0
    seventh = client.post(URL)
    assert seventh.status_code == 429 and int(seventh.headers["Retry-After"]) > 0
    assert seventh.json()["error"] == "limit of 6 judge runs per hour reached"
    assert client.post("/api/judge/run", json={"scenario": "nightly_check"}).status_code == 429  # scenes share it


def test_a_scene_reset_removes_everything_the_check_wrote(api):
    client, conn = api
    client.post(URL, json={"sample": "c3-photo"})
    client.post(URL, json={"sample": "h22-photo"})
    assert _written(conn) == (2, 2, 4)
    assert client.post("/api/judge/run", json={"scenario": "dangerous_morning"}).status_code == 200
    assert _written(conn) == (0, 0, 0)
    assert conn.execute("SELECT count(*) FROM ops.judge_run").fetchone()[0] == 3  # the limit survives the reset


def test_the_check_does_not_change_the_scene_on_screen(api):
    client, _ = api
    client.post("/api/judge/run", json={"scenario": "dangerous_morning"})
    before = client.get("/api/today").json()
    for sample in SAMPLES:
        assert client.post(URL, json={"sample": sample.id}).status_code == 200
    after = client.get("/api/today").json()
    assert after["eligibility"] == before["eligibility"] and after["outcome"] == before["outcome"]


def test_the_staff_page_does_not_list_the_applicants(api):
    client, _ = api
    before = client.get("/api/staff").json()
    for sample in SAMPLES:
        assert client.post(URL, json={"sample": sample.id}).status_code == 200
    after = client.get("/api/staff").json()
    assert after == before and not any(p["id"].startswith("SAMPLE-") for p in after["staff"])


def test_info_counts_nothing_and_names_the_limit_and_mode(api):
    client, _ = api
    info = client.get(URL).json()
    assert (info["mode"], info["runs_per_hour"], info["runs_left_this_hour"]) == ("replay", 6, 6)
    assert info["next_sample"] == SAMPLES[0].id and [s["id"] for s in info["samples"]] == [s.id for s in SAMPLES]
    client.post(URL)
    info = client.get(URL).json()
    assert (info["runs_left_this_hour"], info["next_sample"]) == (5, SAMPLES[1].id)
    assert client.get(URL).json()["runs_left_this_hour"] == 5


def test_sample_card_images_are_served(api):
    client, _ = api
    img = client.get("/media/sample-cards/c3-photo.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert img.content == (CARDS_DIR / "c3-photo.jpg").read_bytes()
    assert client.get("/media/sample-cards/../api.py").status_code == 404
    assert client.get("/media/sample-cards/secret.jpg").status_code == 404
