"""Writes web/fixtures from the running API, so the fixtures are the answers of the real code.

Usage (stack from compose.yaml, replay mode, judge limit high enough for 4 runs):
    python3 tests/web/make_fixtures.py http://127.0.0.1:18088 web/fixtures

Order of calls (each judge run resets the training school):
  1. judge dangerous_morning → today.json, every offer of the case, the case page;
     enrol 2 children into G003 → today.ratio_open.json and the ratio case; the first offer of that case is the pending
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


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    base, out = sys.argv[1].rstrip("/"), Path(sys.argv[2])
    written: list[str] = []

    def save(name: str, obj: dict) -> None:
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(name)

    if ok(base, "GET", "/health").get("model_mode") != "replay":
        raise SystemExit("the stack must run in replay mode: fixtures are recorded answers, never live calls")
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
