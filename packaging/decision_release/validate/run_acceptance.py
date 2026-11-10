#!/usr/bin/env python3
"""Rerun the pinned decision tests from this release, without .env or orders.

Deploy files stay in deploy/. This script copies them into a temporary tree
with the validation-only logging, fetch, gate, protect, and websocket modules.
It does not read a .env file, open a production database, or start a process.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

CLAIMED = (
    "decision_engine.tests.test_public_feed_collector",
    "decision_engine.tests.test_production_universe_top2",
    "decision_engine.tests.test_volume_z_1h",
    "decision_engine.tests.test_regime_strategy_router",
    "decision_engine.tests.test_state3_arm_ticks",
)
ISOLATION_FILES = {
    "logging_setup.py": "logging_setup.py",
    "config.py": "config.py",
    "scripts/fetch_public_derivatives_and_counterparty.py": "scripts/fetch_public_derivatives_and_counterparty.py",
    "execution/__init__.py": "execution/__init__.py",
    "execution/verified_fet.py": "execution/verified_fet.py",
    "execution/binance_ws_stream.py": "execution/binance_ws_stream.py",
    "execution/executors/__init__.py": "execution/executors/__init__.py",
    "execution/executors/binance_spot.py": "execution/executors/binance_spot.py",
    "execution/executors/binance_spot_gate.py": "execution/executors/binance_spot_gate.py",
    "execution/executors/binance_spot_protect.py": "execution/executors/binance_spot_protect.py",
}
SITECUSTOMIZE = '''
import socket

def _blocked(*_args, **_kwargs):
    raise RuntimeError("validation network blocked")

socket.create_connection = _blocked
socket.getaddrinfo = _blocked

class _Guard(socket.socket):
    def connect(self, address):
        raise RuntimeError(f"validation network blocked: {address}")

    def connect_ex(self, address):
        raise RuntimeError(f"validation network blocked: {address}")

socket.socket = _Guard
'''


def release_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _reject_bytecode(root: Path) -> None:
    bad = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            bad.append(path)
        if path.name == "test_obi_top5_reader.py":
            bad.append(path)
    if bad:
        raise SystemExit("release contains bytecode or the removed reader test: " + ", ".join(str(p) for p in bad))


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def main() -> int:
    root = release_root()
    _reject_bytecode(root)
    deploy = root / "deploy" / "decision_engine"
    isolation = root / "validate" / "isolation"
    tests = root / "validate" / "tests"
    if not deploy.is_dir() or not isolation.is_dir() or not tests.is_dir():
        print("release layout is incomplete", file=sys.stderr)
        return 2
    fixture = subprocess.run(
        [sys.executable, str(root / "validate" / "test_obi_private_fixtures.py"), "-q"],
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C"},
    )
    if fixture.returncode != 0:
        print("obi private fixtures failed", file=sys.stderr)
        return fixture.returncode

    with tempfile.TemporaryDirectory(prefix="pr197-accept-") as tmp_name:
        stage = Path(tmp_name)
        _copy_tree(deploy, stage / "decision_engine")
        test_dst = stage / "decision_engine" / "tests"
        test_dst.mkdir()
        shutil.copy2(tests / "__init__.py", test_dst / "__init__.py")
        for module_name in CLAIMED:
            filename = module_name.rsplit(".", 1)[-1] + ".py"
            shutil.copy2(tests / filename, test_dst / filename)
        for src_rel, dst_rel in ISOLATION_FILES.items():
            target = stage / dst_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(isolation / src_rel, target)
        (stage / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(stage),
            "TMPDIR": str(stage),
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(stage),
            "LOG_HANDLER": "stdout",
        }
        print("claimed_modules=" + ",".join(CLAIMED))
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", *CLAIMED, "-q"],
            cwd=stage,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        if proc.returncode != 0 or "Ran 40 tests" not in proc.stderr:
            print("claimed 40 tests did not pass", file=sys.stderr)
            return proc.returncode or 1
    print("acceptance_ok claimed=40 obi_private_fixtures=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
