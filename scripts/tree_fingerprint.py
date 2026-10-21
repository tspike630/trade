#!/usr/bin/env python3
"""Content fingerprint of a deployed package tree (tree_sha256).

Algorithm (must stay identical to decision_engine/run.py::tree_sha256):
sha256 over every ``*.py`` under DIR, excluding any ``__pycache__`` or
``tests`` directory, sorted by relative POSIX path; per file the hash is fed
``rel_path + b"\\0" + content + b"\\0"``.

``--top-level-only`` restricts to ``DIR/*.py`` (used for the crawler
component, whose deploy dir is the deploy root).

Usage: python3 scripts/tree_fingerprint.py DIR [--top-level-only]
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

EXCLUDED_DIRS = ("__pycache__", "tests")


def tree_sha256(pkg_dir: str | Path, top_level_only: bool = False) -> str:
    root = Path(pkg_dir)
    it = root.glob("*.py") if top_level_only else root.rglob("*.py")
    files = []
    for f in it:
        if not f.is_file():
            continue
        rel = f.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]):
            continue
        files.append((rel.as_posix(), f))
    h = hashlib.sha256()
    for rel, f in sorted(files):
        h.update(rel.encode("utf-8") + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    print(tree_sha256(args[0], top_level_only="--top-level-only" in argv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
