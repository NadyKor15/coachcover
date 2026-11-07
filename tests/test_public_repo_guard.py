"""The repository is meant to be public: no chat message references or local home paths in any tracked text.

Generic patterns only, so this file names nothing it is guarding against."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = re.compile(r"\bTG ?\d{4}\b|/(?:Users|home)/[A-Za-z]")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
SKIP_FILES = {ROOT / "uv.lock", Path(__file__).resolve()}
BINARY = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".ogg", ".mp3", ".wav", ".pdf", ".zip", ".woff", ".woff2"}


def _files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() in BINARY or path in SKIP_FILES:
            continue
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        yield path


def find(text: str) -> list[str]:
    return [m.group(0) for m in PATTERNS.finditer(text)]


def test_the_guard_sees_message_numbers_and_home_paths():
    tg, home = "T" + "G", "/Us" + "ers"  # built from parts so this file holds no literal match
    assert len(find(f"decided in {tg} 1234, see {tg}1235; file at {home}/someone/x and /home/dev/y")) == 4
    assert find("We don't replace your schedule: 06:00 shift, 1:4 ratio, $30 per child, case C-1109-01.") == []


def test_no_message_reference_or_home_path_anywhere_in_the_repository():
    files = list(_files())
    assert len(files) > 100  # the scan really reads the tree
    hits = [f"{p.relative_to(ROOT)}:{n}: {', '.join(find(line))}" for p in files
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1) if find(line)]
    assert hits == []
