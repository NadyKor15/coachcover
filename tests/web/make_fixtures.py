"""Writes web/fixtures (training school, fictional names of the API contract). Usage: python3 tests/web/make_fixtures.py web/fixtures"""
import copy, hashlib, json, sys
from pathlib import Path

OUT = Path(sys.argv[1])
PLAN_TEXT = "merge Level 3 with Level 2 (ratio 1:8 ok) · senior guard on deck · message 8 families"
H1 = hashlib.sha256(("P-1|" + PLAN_TEXT).encode()).hexdigest()
H2 = hashlib.sha256(("P-2|" + PLAN_TEXT).encode()).hexdigest()
H3 = hashlib.sha256(("P-2|changed|" + PLAN_TEXT).encode()).hexdigest()

REASONS = [  # code, label, tone, staff ids
    ("CERT_EXPIRED", "Certificate expired Nov 5", "red", ["S09", "S18", "S33"]),
    ("HOURS_LIMIT_EXCEEDED", "Over 40 h this week", "plain", ["S05", "S26"]),
    ("CERT_MISSING", "No WSI", "plain", ["S01", "S02", "S04", "S06", "S08", "S10", "S11", "S13", "S14", "S16", "S17", "S19"]),
    ("LEVEL_MISSING", "Not cleared for LTS-3", "plain", ["S20", "S23", "S24", "S25"]),
    ("OVERLAPS_COMMITMENT", "Teaching at 6:00", "plain", ["S27", "S28", "S29", "S31", "S32", "S34", "S35", "S36", "S37"]),
    ("CERT_NEEDS_REVIEW", "Readers disagree — review", "amber", ["S38"]),
    ("DAY_OFF", "Day off", "plain", ["S39", "S40", "S21"]),
]
ELIGIBLE = [("S03", "Sofia Reyes"), ("S07", "Leo Martins"), ("S15", ""), ("S22", ""), ("S12", "Maria Lopez"), ("S30", "")]
NAMES = {"S09": "Anna Kim"}

staff = [{"id": i, "name": n, "eligible": True, "highlight": n == "Maria Lopez", "reason_label": None} for i, n in ELIGIBLE]
blocked = [(sid, label) for _, label, _, ids in REASONS for sid in ids]
blocked.sort(key=lambda x: (x[0] != "S09", int(x[0][1:])))  # Anna Kim first among the dimmed
staff += [{"id": sid, "name": NAMES.get(sid, ""), "eligible": False, "highlight": False, "reason_label": label} for sid, label in blocked]
ids = [s["id"] for s in staff]
assert len(staff) == 40 and len(set(ids)) == 40, (len(staff), len(set(ids)))
assert sum(len(r[3]) for r in REASONS) == 34

today = {
    "title": "Who is allowed on deck at 06:00",
    "venue": {"id": "V1", "name": "Riverside Aquatic Center", "term": "pool"},
    "now_label": "Mon, Nov 9 · 05:19", "user_label": "Dana Ruiz · Manager", "lang": "en",
    "callout": {"case_id": "C-1109-01", "time_label": "05:12", "staff": "Marina Petrova · Lifeguard + WSI",
                "audio_url": "/media/callouts/C-1109-01.ogg", "duration_label": "0:09", "language": "Russian",
                "transcript_original": "Привет, это Марина. У меня температура 38, я не выйду в 6. У меня в 6 дорожка и в 7 урок у малышей. Прости.",
                "transcript_en": "Hi, it's Marina. I have a 38 fever, I can't come at 6. I have the 6:00 lane and the 7:00 little ones lesson. Sorry.",
                "asr_label": "whisper on Vultr · 1.0 s"},
    "shift": {"id": "L-1109-G002", "time_label": "06:00–08:00",
              "lanes_label": "Lanes 3–4 lap swim + Learn-to-Swim Level 3 (8 kids)",
              "needs_label": "Lifeguard + WSI", "program_label": "Program LTS-3"},
    "eligibility": {"total": 40, "eligible": 6,
                    "reasons": [{"code": c, "label": l, "count": len(i), "tone": t} for c, l, t, i in REASONS],
                    "staff": staff,
                    "first_refusal": {"staff": "Anna Kim",
                                      "label": "free and willing · BLOCKED: Lifeguard certificate expired Nov 5 · 8 kids · ratio 1:4",
                                      "contrast_quote": "“An expired certificate does not block rostering.” — RosterElf help center"}},
    "timeline": [{"time_label": "05:12", "kind": "callout", "label": "Call-out"},
                 {"time_label": "05:13", "kind": "declined", "label": "Sofia Reyes declined"},
                 {"time_label": "05:15", "kind": "no_reply", "label": "Leo Martins no reply in 5 min"},
                 {"time_label": "05:19", "kind": "accepted", "label": "Maria Lopez accepted"},
                 {"time_label": "06:00", "kind": "shift_start"}],
    "offers": [{"id": "O-2", "time_label": "05:13", "staff": "Sofia Reyes", "result": "declined", "why_first": None},
               {"id": "O-3", "time_label": "05:15", "staff": "Leo Martins", "result": "no_reply", "why_first": None},
               {"id": "O-1", "time_label": "05:19", "staff": "Maria Lopez", "result": "accepted",
                "why_first": "Maria first: 12 h this week, Level 3, same pool"}],
    "outcome": {"covered_by": "Maria Lopez", "covered_time": "06:00", "lessons_cancelled": 0},
    "plan_b": {"id": "P-1", "state": "standby", "text": PLAN_TEXT, "hash": H1, "autonomy": "propose"},
    "summary_cards": [{"title": "Tomorrow", "value": "14 of 14 shifts covered"},
                      {"title": "Expiring in 30 days", "value": "2 certificates"},
                      {"title": "Closures today", "value": "none"}],
    "money": {"uncovered_morning_usd": None, "uncovered_morning_label": None, "case_cost_cents": 0.3, "manager_hour_usd": None},
    "model_log": {"label": "Models: whisper · muse-glimmer-30b · nemotron-omni — 4 calls · $0.003 · running on Vultr",
                  "calls": 4, "usd": 0.003},
}

nobody = copy.deepcopy(today)
nobody["now_label"] = "Mon, Nov 9 · 05:20"
nobody["callout"]["case_id"] = "C-1109-02"
nobody["eligibility"]["staff"] = [dict(s, highlight=False) for s in staff]
nobody["timeline"] = [{"time_label": "05:12", "kind": "callout", "label": "Call-out"},
                      {"time_label": "05:13", "kind": "declined", "label": "Sofia Reyes declined"},
                      {"time_label": "05:15", "kind": "no_reply", "label": "Leo Martins no reply in 5 min"},
                      {"time_label": "05:19", "kind": "declined", "label": "Maria Lopez declined"},
                      {"time_label": "05:20", "kind": "plan_b", "label": "Plan B proposed"},
                      {"time_label": "06:00", "kind": "shift_start"}]
nobody["offers"] = [{"id": "O-2", "time_label": "05:13", "staff": "Sofia Reyes", "result": "declined", "why_first": None},
                    {"id": "O-3", "time_label": "05:15", "staff": "Leo Martins", "result": "no_reply", "why_first": None},
                    {"id": "O-1", "time_label": "05:19", "staff": "Maria Lopez", "result": "declined", "why_first": None}]
nobody["outcome"] = {"covered_by": None, "covered_time": "06:00", "lessons_cancelled": 0}
nobody["plan_b"] = {"id": "P-2", "state": "proposed", "text": PLAN_TEXT, "hash": H2, "autonomy": "propose"}

offer = {"offer_id": "O-1", "shift_day": "today", "shift_start": "06:00", "shift_end": "08:00",
         "venue": "Riverside Aquatic Center", "shift_detail": "Lanes 3–4 + Learn-to-Swim Level 3 (8 kids)",
         "why": ["Lifeguard — valid to Mar 2027", "WSI — valid", "LTS-3 program — current", "12 h this week"],
         "expires_in_s": 252, "window_s": 300, "manager": {"name": "Dana Ruiz", "role": "Manager"}}

mocks = {
    "_note": "Mock answers for ?fixture=1. Add &mock=plan_changed or &mock=not_eligible to get the 409 answers.",
    "judge_run": {"dangerous_morning": {"case_id": "C-1109-01"}, "nobody_accepts": {"case_id": "C-1109-02"},
                  "nightly_check": {"case_id": "C-1109-N1"}},
    "today_by_scenario": {"nobody_accepts": "today.nobody_accepts.json"},
    "offers": {"O-1": {"accept": {"state": "accepted"}, "decline": {"state": "declined"},
                       "accept_conflict": {"reason": "already booked 06:00–08:00"}}},
    "plans": {"P-2": {"current_hash": H2, "approve": {"state": "approved"},
                      "conflict": {"reason": "plan changed", "current_hash": H3}}},
}

def dump(path, obj):
    p = OUT / path; p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(p)

dump("today.json", today)
dump("today.nobody_accepts.json", nobody)
dump("offers/O-1.json", offer)
dump("mocks.json", mocks)

# ---- GET /api/cases/C-1109-01: the case page of the covered morning, same training data as today.json ----
def utc(hms):
    return "2026-11-09T" + hms + "Z", hms + " UTC"

def step(kind, hms, title, **kw):
    at, label = utc(hms)
    return {"kind": kind, "at": at, "time_label": label, "title": title, **kw}

c = today["callout"]
case = {
    "case_id": "C-1109-01",
    "status": "covered",
    "status_label": "Covered",
    "covered_time_label": "05:19",
    "shift_label": "06:00 Lanes 3–4 + Learn-to-Swim Level 3",
    "venue": {"id": "V1", "name": "Riverside Aquatic Center"},
    "date_label": "Mon, Nov 9",
    "user_label": "Dana Ruiz · Manager",
    "opened_at": "2026-11-09T10:12:04Z",
    "closed_at": "2026-11-09T10:19:31Z",
    "steps": [
        step("callout", "10:12:04", "Voice call-out", staff=c["staff"], language=c["language"], audio_url=c["audio_url"],
             transcript_original=c["transcript_original"], transcript_en=c["transcript_en"],
             model_tag="whisper-large-v3-turbo · 1.0 s"),
        step("shift", "10:12:06", "Shift identified", shift_id=today["shift"]["id"],
             summary="06:00–08:00 · Needs: Lifeguard + WSI · Program LTS-3", model_tag="muse-glimmer-30b"),
        step("allowed", "10:12:07", "Who is allowed", total=40, allowed=6,
             blocked={"staff": "Anna Kim", "label": today["eligibility"]["first_refusal"]["label"]},
             reasons=[{"code": r["code"], "label": r["label"], "count": r["count"]} for r in today["eligibility"]["reasons"]],
             staff=[{"id": s["id"], "name": s["name"], "allowed": s["eligible"], "reason_label": s["reason_label"]} for s in staff]),
        step("offers", "10:12:08", "Offers in order",
             offers=[{"id": o["id"], "time_label": o["time_label"], "staff": o["staff"], "result": o["result"],
                      "result_label": {"declined": "declined", "no_reply": "no reply in 5 min", "accepted": "accepted"}[o["result"]],
                      "why_first": o["why_first"]} for o in today["offers"]]),
        step("covered", "10:19:31", "Schedule covered", summary="06:00 covered by Maria Lopez · 0 lessons cancelled"),
    ],
    "proof": {"plan_id": today["plan_b"]["id"], "plan_hash": today["plan_b"]["hash"], "policy_label": "cover = auto · plan B = propose"},
    "model_calls": [
        {"at": "2026-11-09T10:12:04Z", "purpose": "transcribe", "model": "whisper-large-v3-turbo", "model_label": "whisper-large-v3-turbo",
         "ms": 1050, "input_tokens": None, "output_tokens": None, "cost_usd": None, "source": "recorded answer tests/fixtures/models/transcribe-ru.json"},
        {"at": "2026-11-09T10:12:06Z", "purpose": "extract_callout", "model": "muse-glimmer-30b", "model_label": "muse-glimmer-30b",
         "ms": 1400, "input_tokens": 445, "output_tokens": 70, "cost_usd": None, "source": "recorded answer tests/fixtures/models/extract-ru.json (synthetic)"},
        {"at": "2026-11-09T10:12:08Z", "purpose": "plan_b_text", "model": "muse-glimmer-30b", "model_label": "muse-glimmer-30b",
         "ms": 1600, "input_tokens": 520, "output_tokens": 110, "cost_usd": None, "source": "recorded answer tests/fixtures/models/synthetic-plan-b-l-1109-g001.json (synthetic)"},
        {"at": None, "purpose": None, "model": "nemotron-3-nano-omni-30b-a3b-reasoning", "model_label": "nemotron-omni",
         "ms": None, "input_tokens": None, "output_tokens": None, "cost_usd": None, "source": "count from the contract example; the run fills it"},
    ],
    "model_cost_usd": today["model_log"]["usd"],
    "risk": {"kids": 8, "refunds_usd": None, "overtime_usd": None, "total_usd": None},
}
dump("cases/C-1109-01.json", case)
