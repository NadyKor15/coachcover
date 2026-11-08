"""The old tagline is retired: the founder said it makes the product sound as if it were only about calling people with
certificates. No tracked text may carry it again, in English or Russian. The footer says how the models answer:
"CoachCover · Running on Vultr." when the server runs them live, "CoachCover · Replay mode." in replay. The screen
title "Who is allowed on deck at 09:30" is a different sentence and stays.

The patterns are built from parts, so this file holds no literal match."""

import re
from pathlib import Path

from tests.test_public_repo_guard import _files

ROOT = Path(__file__).resolve().parents[1]
_APOS = "['’]"
RETIRED = re.compile(
    "|".join([
        r"not\s+who(?:" + _APOS + r"s|\s+is)\s+free",  # "Not who's free", "not who is free"
        r"who" + _APOS + r"s\s+allowed",  # "who's allowed" (the screen title says "Who is allowed", which stays)
        "не\\s+кто\\s+свобод" + "ен",  # the Russian version, both halves
        "а\\s+ком" + "у\\s+можно",
    ]),
    re.IGNORECASE,
)
FOOTER = "CoachCover · Running on Vultr."
REPLAY_FOOTER = "CoachCover · Replay mode."


def find(text: str) -> list[str]:
    return [m.group(0) for m in RETIRED.finditer(text)]


def test_the_guard_sees_every_form_of_the_old_tagline():
    who, s = "wh" + "o", "'" + "s"
    samples = [f"Not {who}{s} free — {who}{s} allowed. Running on Vultr.", f"not {who} is free", f"{who}’s allowed",
               "Н" + "е кто свободен, а кому можно", f"NOT {who.upper()}{s.upper()} FREE"]
    assert all(find(x) for x in samples), samples
    for fine in ("Who is allowed on deck at 09:30", "Who is allowed", "40 staff → 5 allowed", FOOTER,
                 "free and willing · BLOCKED: WSI certificate expired Nov 5"):
        assert find(fine) == [], fine


def test_no_tracked_text_carries_the_old_tagline():
    files = list(_files())
    assert len(files) > 100
    hits = [f"{p.relative_to(ROOT)}:{n}" for p in files if p != Path(__file__).resolve()
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1) if find(line)]
    assert hits == []


def test_every_screen_footer_is_filled_by_the_models_mode():
    """One place holds the footer's two texts (web/assets/api.js); every screen has the slot and fills it from there,
    so a local copy in replay never says "Running on Vultr"."""
    api = (ROOT / "web/assets/api.js").read_text(encoding="utf-8")
    assert f"live: '{FOOTER}'" in api and f"replay: '{REPLAY_FOOTER}'" in api
    for page in ("web/index.html", "web/case.html", "web/week.html", "web/network.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert 'data-slot="footer-line"' in html and FOOTER not in html and REPLAY_FOOTER not in html, page
    for script in ("today.js", "case.js", "week.js", "network.js"):
        assert "CC.fillFooter(" in (ROOT / "web/assets" / script).read_text(encoding="utf-8"), script
