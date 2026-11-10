#!/usr/bin/env python3
"""Run the retained PR208 tests. Does not skip or delete assertions.

The working tree must be a git checkout that contains bc392fea and
refs/remotes/origin/main. A box without GitHub uses the bundle in the
acceptance archive; it does not clone the project from the network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

GROUPS = (
    ("published", 3, "decision_engine.tests.test_published_signal_contract"),
    (
        "prior40",
        40,
        " ".join(
            (
                "decision_engine.tests.test_public_feed_collector",
                "decision_engine.tests.test_production_universe_top2",
                "decision_engine.tests.test_volume_z_1h",
                "decision_engine.tests.test_regime_strategy_router",
                "decision_engine.tests.test_state3_arm_ticks",
            )
        ),
    ),
    ("cityu", 6, "decision_engine.tests.test_cityu_institutional_alpha"),
    ("news", 8, "decision_engine.tests.test_news_isolation"),
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _require_git() -> None:
    if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        raise SystemExit("this directory is not a git checkout; use the local bundle in HOWTO.txt")
    if _git("cat-file", "-e", "bc392fea^{commit}").returncode != 0:
        raise SystemExit("git object bc392fea is missing; fetch it from the local bundle, not from GitHub")
    if _git("rev-parse", "--verify", "refs/remotes/origin/main").returncode != 0:
        raise SystemExit("refs/remotes/origin/main is missing; point it at ab2d2eb from the local bundle")
    for rel in (
        "scripts/import_lseg_bars_incremental.py",
        "scripts/build_news_isolation_engine.py",
        "deploy/news_isolation/preflight.py",
        "deploy/news_isolation/engine.py",
    ):
        if not (ROOT / rel).is_file():
            raise SystemExit(f"missing {rel}")


def _run(modules: str) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}{os.pathsep}{ROOT / 'crawler'}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", *modules.split(), "-q"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode, proc.stderr + proc.stdout


def _obi() -> tuple[int, str]:
    stage = ROOT / "packaging" / "decision_release" / "deploy" / "decision_engine"
    if stage.exists():
        shutil.rmtree(stage.parent)
    shutil.copytree(
        ROOT / "decision_engine",
        stage,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "packaging/decision_release/validate/test_obi_private_fixtures.py"), "-q"],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(stage.parent, ignore_errors=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode, proc.stderr + proc.stdout


def _count(text: str) -> int | None:
    for line in text.splitlines():
        if line.startswith("Ran ") and " test" in line:
            return int(line.split()[1])
    return None


def main() -> int:
    _require_git()
    failed = 0
    totals = {}
    for name, expected, modules in GROUPS:
        code, text = _run(modules)
        got = _count(text)
        totals[name] = got
        if code != 0 or got != expected:
            print(f"FAIL {name}: exit={code} ran={got} expected={expected}", file=sys.stderr)
            failed += 1
        else:
            print(f"PASS {name}={got}")
    code, text = _obi()
    got = _count(text)
    totals["obi"] = got
    if code != 0 or got != 3:
        print(f"FAIL obi: exit={code} ran={got} expected=3", file=sys.stderr)
        failed += 1
    else:
        print("PASS obi=3")
    cityu = totals.get("cityu") or 0
    prior = totals.get("prior40") or 0
    news = totals.get("news") or 0
    published = totals.get("published") or 0
    obi = totals.get("obi") or 0
    print(
        f"counts prior40+cityu={prior}+{cityu}={prior + cityu} "
        f"news={news} obi={obi} published={published} "
        f"sum={prior + cityu + news + obi + published}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
