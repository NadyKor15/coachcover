"""Writes web/fixtures from the running API, so the fixtures are the answers of the real code.

Usage (stack from compose.yaml, replay mode, judge limit high enough for 4 runs):
    python3 tests/web/make_fixtures.py http://127.0.0.1:18088 web/fixtures
The loan states other than the judge's network scene need Python inside the stack (--part all or ext):
    python3 tests/web/make_fixtures.py http://127.0.0.1:18088 web/fixtures \\
        --setup-cmd "docker compose -p ccweb-fx exec -T api python -"
(--part core | ext | all, default all; ext runs first, core last, so the stack ends on the main story.)

Order of calls (each judge run resets the training school):
  1. judge dangerous_morning → today.json, every offer of the case, the case page;
     enrol 2 children into G003 (Mon 09:00) → today.ratio_open.json and the ratio case; the first offer of that case is the pending
     phone screen; decline the second and accept the first (real 200 answers) → today.ratio_covered.json;
     accept Leo's stood-down offer → the real 409 answer.
  2. judge nobody_accepts → today.nobody_accepts.json; approve the plan with a wrong hash (real 409), then with its own
     hash (real 200).
  3. judge nightly_check → today.nightly_check.json.
  4. judge dangerous_morning again, so the stack is left on the main story.
Standard library only.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def call(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def ok(base: str, method: str, path: str, body: dict | None = None, expect: int = 200) -> dict:
    status, answer = call(base, method, path, body)
    if status != expect:
        raise SystemExit(f"{method} {path}: expected {expect}, got {status}: {answer}")
    return answer


# ---- the week and the network loans ----
# Setups run as Python inside the stack (same steps as the lane's own tests): a fresh school in the deck story (the
# network loan's morning: Marina's 06:00 deck shift), her call-out and every cleared person at Riverside saying no, so
# the hero shift is open with nobody left to ask.
EXHAUST = """
import os
from datetime import timedelta
import psycopg
from coachcover import a2a
from coachcover.demo import reset_school, set_clock, VOICES
from coachcover.models import DbLedger, ModelClient
from coachcover.pipeline import Pipeline
from coachcover.seed import DECK_NOW as NOW, POLICY_ID
conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
reset_school(conn, "deck")
VOICE = VOICES["deck"]
shift_id = conn.execute("SELECT id FROM shift WHERE staff_id = 'S05' AND venue_id = 'V1' ORDER BY starts_at, id LIMIT 1").fetchone()[0]
p = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), POLICY_ID)
p.callout("S05", now=NOW, audio=VOICE.read_bytes(), filename=VOICE.name)
case_id = conn.execute("SELECT id FROM cover_case WHERE shift_id = %s AND status = 'open'", (shift_id,)).fetchone()[0]
t = NOW
while True:
    row = conn.execute("SELECT id FROM offer WHERE case_id = %s AND status = 'pending'", (case_id,)).fetchone()
    if row is None:
        break
    t += timedelta(seconds=30)
    p.respond(row[0], False, t)
set_clock(conn, t + timedelta(minutes=1))
a2a.ensure_network(conn)
"""
# Asking the network right after the call-out, before Riverside's own cleared people were asked: the real 409.
ASK_EARLY = EXHAUST.split("t = NOW\n")[0] + "set_clock(conn, NOW + timedelta(minutes=1))\na2a.ensure_network(conn)\n"
# Northside would keep only 3 guards on site or free, and its minimum is 4: it declines ("would leave itself short").
SITE_SHORT = EXHAUST + "conn.execute(\"UPDATE ext_network_member SET min_guards = 4 WHERE venue_id = 'N1'\")\n"
# Kai Morgan gets 32 h more this week: Northside (44 h) still offers him, Riverside (40 h) rejects him.
# Riverside's week is Mon 9 - Sun 15, Northside's Sun 8 - Sat 14: the Sunday block counts on Riverside's side only.
OVER_HOURS = EXHAUST + """
role = conn.execute("SELECT role_id FROM shift WHERE id = 'N-1110-1000'").fetchone()[0]
for i, (day, begin, finish) in enumerate((("2026-11-15", "06:00", "14:00"), ("2026-11-11", "13:00", "21:00"),
                                       ("2026-11-13", "06:00", "14:00"), ("2026-11-14", "06:00", "14:00"))):
    conn.execute("INSERT INTO shift (id, venue_id, zone_id, role_id, starts_at, ends_at, staff_id)"
                 " VALUES (%s, 'N1', 'N1-MAIN', %s, %s::timestamp AT TIME ZONE 'America/New_York',"
                 " %s::timestamp AT TIME ZONE 'America/New_York', 'N01')",
                 (f"N-EXTRA-{i}", role, f"{day} {begin}", f"{day} {finish}"))
"""


def run_setup(setup_cmd: list[str], code: str) -> None:
    done = subprocess.run(setup_cmd, input=code, text=True, capture_output=True, timeout=180)
    if done.returncode != 0:
        raise SystemExit(f"setup failed: {done.stderr[-800:]}")


def ext_fixtures(base: str, setup_cmd: list[str], save, out: Path) -> None:
    hero = "L-1109-G002"
    mocks: dict = {"_note": "Answers of the network routes, captured by tests/web/make_fixtures.py --part ext. In ?fixture=1 "
                            "&mock=<state> opens a loan in that state: candidate, one_approval, one_approval_lender, "
                            "assigned, declined_site_short, rejected_rules; &mock=approve_conflict gives the 409.",
                   "loan_files": {}, "approve": {}, "ask": {}}

    def loan_state(name: str, loan: dict) -> None:
        save(f"loans/{name}.json", loan)
        mocks["loan_files"][name] = f"loans/{name}.json"

    # the week of the main story (the API's own week: Monday to Sunday), then standby proposals on it
    ok(base, "POST", "/api/judge/run", {"scenario": "dangerous_morning"})
    week = ok(base, "GET", "/api/week?venue=V1")
    save("week.json", week)
    pick = week["fixes"]["standby"][0]
    cell = next(c for d in week["days"] for c in d["cells"] if c["shift_id"] == pick["shift_id"])
    today = ok(base, "GET", "/api/today")
    lifeguard_only = next(s["id"] for s in today["eligibility"]["staff"] if s["reason_label"] == "Role: not an instructor")
    standby = {}
    standby["created"] = ok(base, "POST", "/api/standby", {"shift_id": pick["shift_id"], "staff_id": pick["staff_id"]}, expect=201)
    standby["repeat"] = ok(base, "POST", "/api/standby", {"shift_id": pick["shift_id"], "staff_id": pick["staff_id"]}, expect=409)
    standby["holder"] = ok(base, "POST", "/api/standby", {"shift_id": pick["shift_id"], "staff_id": cell["holder"]["staff_id"]}, expect=409)
    standby["not_cleared"] = ok(base, "POST", "/api/standby", {"shift_id": pick["shift_id"], "staff_id": lifeguard_only}, expect=409)
    standby["unknown"] = ok(base, "POST", "/api/standby", {"shift_id": "L-0000-NONE", "staff_id": pick["staff_id"]}, expect=404)
    save("week_mocks.json", {"_note": "Answers of POST /api/standby on the week above, captured by make_fixtures.py. In ?fixture=1 the "
                                      "first proposal answers created (201), the same one again repeat (409); &mock=standby_conflict "
                                      "answers not_cleared (409).", "standby": standby})

    # the judge's network scene: 5 of 5 say no at Riverside, Northside lends Kai Morgan, both managers approve
    run = ok(base, "POST", "/api/judge/run", {"scenario": "network_loan"})
    if run.get("loan_status") != "assigned":
        raise SystemExit(f"network_loan: expected an assigned loan, got {run}")
    mocks["judge_run"] = run
    loan_state("assigned", ok(base, "GET", f"/api/network/loans/{run['loan']}"))
    save("today.network_assigned.json", ok(base, "GET", "/api/today"))
    save(f"cases/{run['case_id']}.network_loan.json", ok(base, "GET", f"/api/cases/{run['case_id']}"))

    # asking while own people are left: 409
    run_setup(setup_cmd, ASK_EARLY)
    mocks["ask"]["conflict"] = ok(base, "POST", "/api/network/loans", {"shift_id": hero}, expect=409)

    # the loan, approved Riverside first
    run_setup(setup_cmd, EXHAUST)
    save("network.json", ok(base, "GET", "/api/network"))
    save("today.network_open.json", ok(base, "GET", "/api/today"))
    created = ok(base, "POST", "/api/network/loans", {"shift_id": hero}, expect=201)
    mocks["ask"]["created"] = created
    loan_state("candidate", created)
    ref = created["code"]
    borrower, lender = created["borrower"], created["lender"]
    approve = lambda who, expect=200: ok(base, "POST", f"/api/network/loans/{ref}/approve",  # noqa: E731
                                         {"manager": who["manager"], "venue": who["venue_id"]}, expect=expect)
    mocks["approve"]["forbidden"] = ok(base, "POST", f"/api/network/loans/{ref}/approve",
                                       {"manager": lender["manager"], "venue": borrower["venue_id"]}, expect=403)
    mocks["approve"]["first"] = approve(borrower)
    loan_state("one_approval", ok(base, "GET", f"/api/network/loans/{ref}"))
    mocks["approve"]["repeat"] = approve(borrower, 409)
    mocks["approve"]["second"] = approve(lender)
    mocks["approve"]["closed"] = approve(lender, 409)

    # the same loan, approved Northside first
    run_setup(setup_cmd, EXHAUST)
    created = ok(base, "POST", "/api/network/loans", {"shift_id": hero}, expect=201)
    ok(base, "POST", f"/api/network/loans/{created['code']}/approve",
       {"manager": created["lender"]["manager"], "venue": created["lender"]["venue_id"]})
    loan_state("one_approval_lender", ok(base, "GET", f"/api/network/loans/{created['code']}"))

    # Northside declines: it would leave its own site short
    run_setup(setup_cmd, SITE_SHORT)
    loan = ok(base, "POST", "/api/network/loans", {"shift_id": hero}, expect=201)
    if loan["status"] != "declined":
        raise SystemExit(f"expected a declined loan, got {loan['status']}")
    loan_state("declined_site_short", loan)
    mocks["approve"]["declined"] = ok(base, "POST", f"/api/network/loans/{loan['code']}/approve",
                                      {"manager": loan["borrower"]["manager"], "venue": loan["borrower"]["venue_id"]}, expect=409)

    # Riverside rejects the offered person by its own rules
    run_setup(setup_cmd, OVER_HOURS)
    loan = ok(base, "POST", "/api/network/loans", {"shift_id": hero}, expect=201)
    if loan["status"] != "rejected":
        raise SystemExit(f"expected a rejected loan, got {loan['status']}")
    loan_state("rejected_rules", loan)

    ok(base, "POST", "/api/judge/run", {"scenario": "dangerous_morning"})
    mocks["_captured_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    save("network_mocks.json", mocks)


def main() -> None:
    args = sys.argv[1:]
    part = "all"
    setup_cmd: list[str] | None = None
    if "--part" in args:
        i = args.index("--part"); part = args[i + 1]; del args[i:i + 2]
    if "--setup-cmd" in args:
        i = args.index("--setup-cmd"); setup_cmd = shlex.split(args[i + 1]); del args[i:i + 2]
    if len(args) != 2 or part not in ("all", "core", "ext"):
        raise SystemExit(__doc__)
    base, out = args[0].rstrip("/"), Path(args[1])
    written: list[str] = []

    def save(name: str, obj: dict) -> None:
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(name)

    if ok(base, "GET", "/health").get("model_mode") != "replay":
        raise SystemExit("the stack must run in replay mode: fixtures are recorded answers, never live calls")
    if part in ("all", "ext"):
        if setup_cmd is None:
            raise SystemExit("--part ext needs --setup-cmd: the loan states are set up by Python inside the stack")
        ext_fixtures(base, setup_cmd, save, out)
    if part == "ext":
        print("\n".join(written))
        return
    mocks: dict = {
        "_note": "Answers captured from the running API by tests/web/make_fixtures.py. In ?fixture=1 add "
                 "&mock=plan_changed or &mock=not_eligible for the 409 answers, &mock=ratio_open or &mock=ratio_covered "
                 "for the ratio banner.",
        "judge_run": {}, "today_by_scenario": {}, "today_by_mock": {}, "offers": {}, "plans": {},
    }

    # 1. the main story
    mocks["judge_run"]["dangerous_morning"] = ok(base, "POST", "/api/judge/run", {"scenario": "dangerous_morning"})
    today = ok(base, "GET", "/api/today")
    save("today.json", today)
    for o in today["offers"]:
        save(f"offers/{o['id']}.json", ok(base, "GET", f"/api/offers/{o['id']}"))
    case_id = today["callout"]["case_id"]
    save(f"cases/{case_id}.json", ok(base, "GET", f"/api/cases/{case_id}"))

    ok(base, "POST", "/api/groups/G003/enroll", {"add": 2, "by": "front desk"})
    ratio_open = ok(base, "GET", "/api/today")
    if not ratio_open.get("ratio_alert"):
        raise SystemExit("enrolment did not open a ratio case")
    save("today.ratio_open.json", ratio_open)
    mocks["today_by_mock"]["ratio_open"] = "today.ratio_open.json"
    ratio_case = ratio_open["ratio_alert"]["case_id"]
    page = ok(base, "GET", f"/api/cases/{ratio_case}")
    save(f"cases/{ratio_case}.json", page)
    offers = next(s for s in page["steps"] if s["kind"] == "offers")["offers"]
    pending = [o["id"] for o in offers if o["result"] == "pending"]
    if len(pending) < 2:
        raise SystemExit(f"ratio case {ratio_case}: expected at least 2 pending offers, got {offers}")
    phone = ok(base, "GET", f"/api/offers/{pending[0]}")
    save(f"offers/{pending[0]}.json", phone)
    declined = ok(base, "POST", f"/api/offers/{pending[1]}/decline")
    accepted = ok(base, "POST", f"/api/offers/{pending[0]}/accept")
    stood_down = next(o["id"] for o in today["offers"] if o["result"] == "no_reply")
    conflict = ok(base, "POST", f"/api/offers/{stood_down}/accept", expect=409)
    for o in [*today["offers"], {"id": pending[0]}]:
        mocks["offers"][o["id"]] = {"accept": accepted, "decline": declined, "accept_conflict": conflict}
    deadline = time.monotonic() + 150
    while True:  # the yes is held for the collect window, then the worker fills the seat
        covered = ok(base, "GET", "/api/today")
        if (covered.get("ratio_alert") or {}).get("tone") == "green":
            break
        if time.monotonic() > deadline:
            raise SystemExit(f"ratio case not covered in time: {covered.get('ratio_alert')}")
        time.sleep(3)
    save("today.ratio_covered.json", covered)
    mocks["today_by_mock"]["ratio_covered"] = "today.ratio_covered.json"

    # 2. nobody accepts: plan B by hash
    mocks["judge_run"]["nobody_accepts"] = ok(base, "POST", "/api/judge/run", {"scenario": "nobody_accepts"})
    nobody = ok(base, "GET", "/api/today")
    save("today.nobody_accepts.json", nobody)
    mocks["today_by_scenario"]["nobody_accepts"] = "today.nobody_accepts.json"
    plan = nobody["plan_b"]
    if not plan or plan["state"] != "proposed":
        raise SystemExit(f"nobody_accepts: expected a proposed plan B, got {plan}")
    refused = ok(base, "POST", f"/api/plans/{plan['id']}/approve", {"hash": "0" * 64}, expect=409)
    approved = ok(base, "POST", f"/api/plans/{plan['id']}/approve", {"hash": plan["hash"]})
    mocks["plans"][plan["id"]] = {"current_hash": plan["hash"], "approve": approved, "conflict": refused}

    # 3. night check
    mocks["judge_run"]["nightly_check"] = ok(base, "POST", "/api/judge/run", {"scenario": "nightly_check"})
    save("today.nightly_check.json", ok(base, "GET", "/api/today"))
    mocks["today_by_scenario"]["nightly_check"] = "today.nightly_check.json"

    # 4. leave the stack on the main story
    ok(base, "POST", "/api/judge/run", {"scenario": "dangerous_morning"})
    mocks["_captured_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    save("mocks.json", mocks)
    print("\n".join(written))


if __name__ == "__main__":
    main()
