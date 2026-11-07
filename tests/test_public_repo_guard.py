"""The repository goes public: no internal chat references, team or channel names, or local paths anywhere in it
(except uv.lock and web/fixtures, which the screens lane re-shoots). This file is the one place that names them."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = re.compile(
    r"TG ?\d{4}"
    r"|internal|ORCH-|internal|internal|internal|worktree-|lane/(core|web|ext|sim)|the founder|the founder",
    re.IGNORECASE,
)
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
SKIP_FILES = {ROOT / "uv.lock", Path(__file__).resolve()}
SKIP_TREES = (ROOT / "web" / "fixtures",)
BINARY = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".ogg", ".mp3", ".wav", ".pdf", ".zip", ".woff", ".woff2"}


def _files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() in BINARY or path in SKIP_FILES:
            continue
        rel = path.relative_to(ROOT)
        if SKIP_DIRS & set(rel.parts) or any(path.is_relative_to(tree) for tree in SKIP_TREES):
            continue
        yield path


def find(text: str) -> list[str]:
    return [m.group(0) for m in PATTERNS.finditer(text)]


def test_the_guard_sees_every_kind_of_internal_reference():
    sample = ("decided in a product decision by the founder; internal and internal over internal; worktree worktree-core on core-branch; "
              "internal at /home/someone; vault your-project; the founder")
    assert len(find(sample)) == 12
    assert find("We don't replace your schedule — we add the certification layer on top of it.") == []


def test_no_internal_reference_anywhere_in_the_repository():
    files = list(_files())
    assert len(files) > 100  # the scan really reads the tree
    hits = [f"{p.relative_to(ROOT)}:{n}: {', '.join(find(line))}" for p in files
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1) if find(line)]
    assert hits == []
