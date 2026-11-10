#!/usr/bin/env python3
"""Build a source-only decision release from a clean git HEAD.

Deploy files are decision_engine source without tests or bytecode. The
validation kit is a separate tree: the real logging, public-feed, gate,
protect, and websocket modules pinned to this HEAD, plus authored files that
keep the acceptance run off .env, the paper ledger, and the network.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CLAIMED_TESTS = (
    "test_public_feed_collector.py",
    "test_production_universe_top2.py",
    "test_volume_z_1h.py",
    "test_regime_strategy_router.py",
    "test_state3_arm_ticks.py",
)
PINNED_BLOBS = {
    "validate/isolation/logging_setup.py": "crawler/logging_setup.py",
    "validate/isolation/scripts/fetch_public_derivatives_and_counterparty.py": "scripts/fetch_public_derivatives_and_counterparty.py",
    "validate/isolation/execution/__init__.py": "execution/__init__.py",
    "validate/isolation/execution/verified_fet.py": "execution/verified_fet.py",
    "validate/isolation/execution/binance_ws_stream.py": "execution/binance_ws_stream.py",
    "validate/isolation/execution/executors/binance_spot.py": "execution/executors/binance_spot.py",
    "validate/isolation/execution/executors/binance_spot_gate.py": "execution/executors/binance_spot_gate.py",
    "validate/isolation/execution/executors/binance_spot_protect.py": "execution/executors/binance_spot_protect.py",
}
AUTHORED = {
    "validate/isolation/config.py": HERE / "isolation" / "config.py",
    "validate/isolation/execution/executors/__init__.py": HERE / "isolation" / "executors_init.py",
    "validate/run_acceptance.py": HERE / "validate" / "run_acceptance.py",
    "validate/prove_obi_consumer.py": HERE / "validate" / "prove_obi_consumer.py",
    "validate/test_obi_private_fixtures.py": HERE / "validate" / "test_obi_private_fixtures.py",
    "validate/check_reader_preserved.py": HERE / "validate" / "check_reader_preserved.py",
    "HOWTO_DEPLOY.txt": HERE / "HOWTO_DEPLOY.txt",
    "HOWTO_VALIDATE.txt": HERE / "HOWTO_VALIDATE.txt",
}
READER_REL = "decision_engine/core/perp_microstructure_and_beta_hedge.py"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_bytes(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)


def deploy_sources() -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    root = REPO / "decision_engine"
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith("tests/") or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.name.startswith(".env"):
            continue
        out.append((f"deploy/decision_engine/{rel}", path.read_bytes()))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    status = git("status", "--porcelain")
    if status and not args.allow_dirty:
        print(status, file=sys.stderr)
        print("working tree is dirty; commit before building the pinned release", file=sys.stderr)
        return 2
    head = git("rev-parse", "HEAD")
    main_sha = git("rev-parse", "origin/main")
    parents = git("rev-parse", "HEAD^@").split()
    subject = git("log", "-1", "--format=%s")
    for required in (
        "0b63ebf49d028f4c0de1ba0167e06cd21c5158b4",
        "4f680142d6116abcc0c52c33ba3021c078a1cb53",
        "78168a8f2b961174834e8797927c8d010a553978",
        "511cdb51093bcd5975d368b2689a5b2b02d1b071",
    ):
        subprocess.check_call(["git", "merge-base", "--is-ancestor", required, "HEAD"], cwd=REPO)

    short = head[:12]
    dest = args.out / f"pr197-decision-release-{short}"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    files: list[tuple[str, bytes]] = []
    files.extend(deploy_sources())
    provenance_lines = [
        "source_pin",
        f"HEAD {head}",
        f"subject {subject}",
        f"parents {' '.join(parents)}",
        f"origin/main {main_sha}",
        "ancestor 0b63ebf49d028f4c0de1ba0167e06cd21c5158b4 main news isolation",
        "ancestor 4f680142d6116abcc0c52c33ba3021c078a1cb53 previous main snapshot",
        "ancestor 78168a8f2b961174834e8797927c8d010a553978 PR200 reader",
        "ancestor 511cdb51093bcd5975d368b2689a5b2b02d1b071 PR200 merge",
        "not_deployed repo deploy/news_isolation/engine.py is the bc392fea patch and is not this payload",
        "",
        "git commands",
    ]
    for command, output in (
        ("git rev-parse HEAD", head),
        ("git rev-parse HEAD^@", " ".join(parents)),
        ("git rev-parse origin/main", main_sha),
        ("git log -1 --format=%H%n%P%n%s", git("log", "-1", "--format=%H%n%P%n%s")),
        ("git diff --stat origin/main...HEAD", git("diff", "--stat", "origin/main...HEAD")),
        (
            "git rev-parse HEAD:decision_engine/core/perp_microstructure_and_beta_hedge.py",
            git("rev-parse", "HEAD:decision_engine/core/perp_microstructure_and_beta_hedge.py"),
        ),
        (
            "git rev-parse HEAD:execution/executors/__init__.py",
            git("rev-parse", "HEAD:execution/executors/__init__.py"),
        ),
    ):
        provenance_lines.append(f"$ {command}")
        provenance_lines.append(output)
        provenance_lines.append("")
    if status:
        provenance_lines.append("working tree was dirty; this archive is not a clean pin")
        provenance_lines.append(status)
    blob_lines = ["pinned blobs (validation copies, not deploy replacements)"]
    for rel, git_path in PINNED_BLOBS.items():
        data = subprocess.check_output(["git", "show", f"HEAD:{git_path}"], cwd=REPO)
        blob = git("rev-parse", f"HEAD:{git_path}")
        if sha256_bytes(data) != sha256_bytes(subprocess.check_output(["git", "cat-file", "blob", blob], cwd=REPO)):
            raise SystemExit(f"blob mismatch for {git_path}")
        files.append((rel, data))
        blob_lines.append(f"{rel} {git_path} {blob} {sha256_bytes(data)}")
    provenance_lines.extend(blob_lines)
    for rel, src in AUTHORED.items():
        data = src.read_bytes()
        files.append((rel, data))
        provenance_lines.append(f"authored {rel} not a production replacement")
    init_test = (REPO / "decision_engine" / "tests" / "__init__.py").read_bytes()
    files.append(("validate/tests/__init__.py", init_test))
    for name in CLAIMED_TESTS:
        files.append((f"validate/tests/{name}", (REPO / "decision_engine" / "tests" / name).read_bytes()))

    reader = next(data for rel, data in files if rel == f"deploy/{READER_REL}")
    reader_hash = sha256_bytes(reader)
    text = reader.decode("utf-8")
    for marker in ("MAX(obi_top5_source_ts_ms)", '_ts_is_fresh(r["obi_top5_source_ts_ms"]'):
        if marker not in text:
            raise SystemExit(f"reader missing {marker}")
    if "attach_spot_obi_top5" in text or "orderbook_imbalance_20 AS obi_top5" in text:
        raise SystemExit("reader contains a forbidden alias")
    files.append(("deploy/READER_SHA256", (reader_hash + "\n").encode()))
    files.append(("DEPLOY_SHA", (head + "\n").encode()))
    files.append(("PROVENANCE.txt", ("\n".join(provenance_lines).rstrip() + "\n").encode()))

    for rel, data in files:
        if rel.endswith(".pyc") or "__pycache__" in rel.split("/") or "test_obi_top5_reader" in rel:
            raise SystemExit(f"refusing {rel}")
        copy_bytes(dest / rel, data)

    listed = []
    for path in sorted(dest.rglob("*")):
        if path.is_file():
            rel = path.relative_to(dest).as_posix()
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                raise SystemExit(f"bytecode in release: {rel}")
            listed.append((rel, sha256_file(path), path.stat().st_size))
    manifest = ["role\tsha256\tbytes\tpath"]
    sums = []
    for rel, digest, size in listed:
        if rel.startswith("deploy/"):
            role = "deploy"
        elif rel.startswith("validate/"):
            role = "validate"
        else:
            role = "index"
        manifest.append(f"{role}\t{digest}\t{size}\t{rel}")
        sums.append(f"{digest}  {rel}")
    manifest_path = dest / "MANIFEST"
    sums_path = dest / "SHA256SUMS"
    manifest_path.write_text("\n".join(manifest) + "\n", encoding="utf-8")
    sums_path.write_text("\n".join(sums) + "\n", encoding="utf-8")

    tar_path = args.out / f"pr197-decision-release-{short}.tar.gz"
    if tar_path.exists():
        tar_path.unlink()
    with tarfile.open(tar_path, "w:gz") as archive:
        archive.add(dest, arcname=dest.name)
    tar_hash = sha256_file(tar_path)
    (args.out / f"pr197-decision-release-{short}.tar.gz.sha256").write_text(
        f"{tar_hash}  {tar_path.name}\n", encoding="utf-8"
    )
    print(f"release={dest}")
    print(f"tar={tar_path}")
    print(f"tar_sha256={tar_hash}")
    print(f"head={head}")
    print(f"reader_sha256={reader_hash}")
    print(f"files={len(listed) + 2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
