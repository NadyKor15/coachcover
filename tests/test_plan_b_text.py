"""Plan B in plain words: names, money ($8.50), minutes and days for people, never an id, a code or decimal hours.
The text model sees only that view; its text is shown only if it passes the check, else the template.

The live model is a fake transport here: nothing leaves the machine, no model is called.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from psycopg.types.json import Jsonb

from coachcover import messages as M
from coachcover import seed
from coachcover.models import TEXT_MODEL, DbLedger, MemoryLedger, ModelClient
from coachcover.pipeline import Pipeline

NY = ZoneInfo("America/New_York")
SHIFT = {"id": "L-1109-G002", "label": "Mon 09:30-10:15 · Instructor · Riverside", "venue": "Riverside", "zone_id": "V1-TEACH",
         "start": "2026-11-09T14:30:00+00:00", "end": "2026-11-09T15:15:00+00:00", "role": "instructor"}
EVERY_KIND = [  # the options as the core builds them (planb.py), ids included: the view must drop them
    {"kind": "merge", "mode": "propose", "into_shift_id": "L-1109-G121", "staff_id": "S04", "staff_name": "Daniel Shaw",
     "groups": ["G121", "G002"], "participants": 8, "capacity": 8},
    {"kind": "lead", "mode": "propose", "staff_id": "S06", "staff_name": "Liam Foster"},
    {"kind": "overtime", "mode": "propose", "staff_id": "S11", "staff_name": "Camila Herrera", "hours_over": 0.25, "cost_usd": 8.5},
    {"kind": "reschedule", "mode": "propose", "groups": ["G002"], "families": 8, "remedy": "makeup", "valid_until": "2026-12-09"},
    {"kind": "close", "mode": "propose", "zone_id": "V1-TEACH", "groups": ["G002"], "families": 8, "remedy": "credit",
     "valid_until": None},
    {"kind": "split", "mode": "propose", "into_shift_id": "L-1109-G121", "staff_id": "S04", "staff_name": "Daniel Shaw",
     "children": 2, "group_id": "G003", "participants_after": 8, "capacity": 8, "families": {"es": 2}},
    {"kind": "move", "mode": "propose", "to_shift_id": "L-1110-G015", "to_group_id": "G015", "staff_id": "S15", "children": 2,
     "group_id": "G003", "starts_at": "2026-11-10T11:00:00+00:00", "venue_id": "V1", "families": {"es": 2}},
    {"kind": "cancel_lesson", "mode": "propose", "shift_id": "L-1109-G003", "groups": ["G003"], "families": 6, "remedy": "credit"},
    {"kind": "reschedule_own", "mode": "propose", "staff_id": "S05", "staff_name": "Marina Petrova", "when": "Wed 17:00",
     "starts_at": "2026-11-11T22:00:00+00:00", "ends_at": "2026-11-11T22:30:00+00:00", "groups": ["P001"], "families": 1},
    {"kind": "credit", "mode": "propose", "groups": ["P001"], "families": 1, "remedy": "credit", "amount_usd": 50.0},
]
MACHINE = {"headline": "Plan B for L-1109-G002", "lines": [
    "Propose overtime with Camila Herrera S11 for 0.25 hours at $8.5 cost", "Propose reschedule group G002",
    "Propose close zone V1-TEACH"]}  # what the live model wrote on the stand, word for word


def facts(options):
    return {"shift": SHIFT, "options": options}


def test_every_kind_of_option_reads_as_plain_words():
    text = M.plan_b_fallback(facts(EVERY_KIND))
    assert text["lines"] == [
        "Merge with Daniel Shaw's group next door: 8 of 8 places, within the norm.",
        "Liam Foster (reserve) steps in.",
        "Camila Herrera works 15 minutes of overtime ($8.50).",
        "Move the lesson: 8 families book a makeup by Dec 9.",
        "Cancel the lesson: 8 families get a credit.",
        "Daniel Shaw takes the 2 newest children as a group of their own level, at the same time (8 of 8 places).",
        "Move the 2 newest children to the nearest lesson of their level with room.",
        "Cancel the whole lesson: 6 families get a credit.",
        "Move the lesson to Marina, Wed 17:00: the family keeps their coach.",
        "Cancel: the family gets a credit for one lesson ($50).",
    ]
    assert M.plan_text_problems(M.plan_b_view(facts(EVERY_KIND)), text) == []  # the template passes its own check
    view = json.dumps(M.plan_b_view(facts(EVERY_KIND)), ensure_ascii=False)
    assert not M.PLAN_IDS.search(view) and not M.ISO_DATE.search(view)  # the model never sees an id or a date
    assert [M.duration_words(h) for h in (0.25, 1, 1.5, 2)] == ["15 minutes", "1 h", "1 h 30 min", "2 h"]
    assert [M.money_words(v) for v in (8.5, 51, 1200, 0.3)] == ["$8.50", "$51", "$1,200", "$0.30"]


def test_the_machine_text_from_the_stand_is_refused():
    view = M.plan_b_view(facts([o for o in EVERY_KIND if o["kind"] in ("overtime", "reschedule", "close")]))
    problems = M.plan_text_problems(view, MACHINE)
    for expected in ("headline: an id (L-1109-G002)", "line 1: an id (S11)", "line 1: a code (Propose)",
                     "line 1: decimal hours (0.25 hours)", "line 1: cents with one digit ($8.5)", "line 2: an id (G002)",
                     "line 3: an id (V1-TEACH)", "line 1: no $8.50"):
        assert expected in problems, (expected, problems)
    assert M.plan_text_problems(view, {"headline": "x", "lines": ["one line"]}) == ["1 lines for 3 options"]


def test_every_prepared_answer_passes_the_check():
    for path in sorted((Path(__file__).parent / "fixtures" / "models").glob("synthetic-plan-b-*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        text = json.loads(rec["response"]["choices"][0]["message"]["content"])
        assert M.plan_text_problems(rec["input"], text) == [], path.name


def live_pipeline(answer: dict) -> tuple[Pipeline, MemoryLedger, list]:
    sent = []

    def transport(path, request):
        sent.append(request["json"])
        return {"model": TEXT_MODEL, "usage": {"prompt_tokens": 300, "completion_tokens": 60},
                "choices": [{"message": {"content": json.dumps(answer)}}]}, 800

    ledger = MemoryLedger()
    return Pipeline(None, ModelClient(ledger, mode="live", api_key="test-only", transport=transport), "x"), ledger, sent


def test_a_machine_live_text_is_not_shown_the_template_is():
    options = [o for o in EVERY_KIND if o["kind"] in ("overtime", "reschedule", "close")]
    p, ledger, sent = live_pipeline(MACHINE)
    text, source = p._plan_text(facts(options), datetime(2026, 11, 9, 8, 45, tzinfo=NY), 1)
    assert source == "template" and text["lines"][0] == "Camila Herrera works 15 minutes of overtime ($8.50)."
    assert "line 1: an id (S11)" in text["model_text_rejected"]
    seen = sent[0]["messages"][1]["content"]
    assert "S11" not in seen and "V1-TEACH" not in seen and "2026-12-09" not in seen  # the model saw names and words
    assert ledger.calls[0].ok  # the call worked; its text did not pass


def test_a_clear_live_text_is_shown_as_written():
    options = [o for o in EVERY_KIND if o["kind"] in ("overtime", "close")]
    answer = {"headline": "Nobody has taken the 09:30 lesson yet",
              "lines": ["Camila Herrera can cover it with 15 minutes of overtime ($8.50).", "Cancel it: 8 families get a credit."]}
    p, _, _ = live_pipeline(answer)
    text, source = p._plan_text(facts(options), datetime(2026, 11, 9, 8, 45, tzinfo=NY), 1)
    assert source == "model" and text == answer


@pytest.mark.db
def test_the_childrens_norm_plan_reads_as_plain_words(new_database):
    """G003 grows past its norm and nobody is asked: plan B for the extra seat speaks in names and numbers."""
    conn = new_database()
    seed.load_school(conn)
    conn.execute("UPDATE policy SET autonomy = autonomy || %s WHERE id = %s", (Jsonb({"assign_cover": "off"}), seed.POLICY_ID))
    p = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), seed.POLICY_ID)
    alerts = p.enroll("G003", 2, seed.NOW + timedelta(minutes=1), language="es")["alerts"]
    case = conn.execute("SELECT id FROM cover_case WHERE code = %s", (alerts[0]["cases"][0]["case_id"],)).fetchone()[0]
    plan = p.propose_plan_b(case, seed.NOW + timedelta(minutes=2))
    snapshot = plan["snapshot"]
    assert [o["kind"] for o in snapshot["options"]]  # split / move / a reserve person / cancel: whatever the norm allows
    lines = snapshot["text"]["lines"]
    assert lines and all(not M.PLAN_IDS.search(line) and not M.PLAN_CODES.search(line) for line in lines), lines
    assert M.plan_text_problems(M.plan_b_view(snapshot), snapshot["text"]) == []


def test_the_morning_strip_says_the_plan_in_a_few_words():
    lines = [M.plan_b_short_line(o) for o in M.plan_b_view(facts(EVERY_KIND))["options"]]
    assert lines[1:5] == ["Liam Foster (reserve) steps in", "Camila Herrera: 15 min overtime ($8.50)",
                          "Move it: makeup for 8 families by Dec 9", "Cancel: credit for 8 families"]
    assert not any(M.PLAN_IDS.search(x) or M.PLAN_CODES.search(x) or M.DECIMAL_HOURS.search(x) for x in lines)


def test_a_line_without_a_verb_is_not_shown():
    """Live, 2026-11-07: "Camila Herrera 15 minutes of overtime ($8.50)" named the person and the money but said
    nothing happens; the template goes out instead."""
    options = [o for o in EVERY_KIND if o["kind"] in ("overtime", "close")]
    view = M.plan_b_view(facts(options))
    bare = {"headline": "Nobody has taken the 09:30 lesson yet",
            "lines": ["Camila Herrera 15 minutes of overtime ($8.50)", "Cancel it: 8 families get a credit."]}
    assert M.plan_text_problems(view, bare) == ["line 1: does not say what happens (overtime)"]
    for ok in ("Camila Herrera works 15 minutes of overtime ($8.50).", "Camila Herrera: 15 min overtime ($8.50)",
               "Camila Herrera covers it with 15 minutes of overtime ($8.50)."):
        assert M.plan_text_problems(view, {**bare, "lines": [ok, bare["lines"][1]]}) == [], ok
    p, _, _ = live_pipeline(bare)
    text, source = p._plan_text(facts(options), datetime(2026, 11, 9, 8, 47, tzinfo=NY), 1)
    assert source == "template" and text["lines"][0] == "Camila Herrera works 15 minutes of overtime ($8.50)."
    assert text["model_text_rejected"] == ["line 1: does not say what happens (overtime)"]
