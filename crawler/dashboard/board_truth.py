"""Display-only truth readers: cell ledger, supply gaps, deploy sha.

Nothing here writes, trades, or touches gates. Missing inputs come back as
an explicit "missing" reason, never as a zero or a hardcoded fallback.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard.settings import db_root

log = logging.getLogger(__name__)

HKT = timezone(timedelta(hours=8))
LEDGER_MISSING = "格数：总账缺失"
GAPS_MISSING = "断供记录缺失"
TENTATIVE_REJECT = "暂定拒绝"
# jev 接线 bug 期的格子：暂定拒绝，待重测；出口 37 格 + 随机基线保持原判。
TENTATIVE_REJECT_LINE = (
    f"{TENTATIVE_REJECT}：jev 期约 129 格（接线 bug 期，stance 读反/jev 过期，待重测）"
    " · 出口 37 格 + 随机基线维持原判"
)


def _root() -> Path:
    try:
        return db_root()
    except Exception:
        return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- cell ledger


@dataclass(frozen=True)
class LedgerCount:
    max_cell_id: int | None
    tentative_reject: int | None
    reason: str | None
    skipped: int = 0
    reserved: tuple[int, ...] = ()


def _cell_upper(raw: str) -> int | None:
    """'271' -> 271; range '1-267' / '1–267' / '1—267' -> 267; else None."""
    text = str(raw or "").strip()
    for dash in ("–", "—", "-"):
        if dash in text:
            lo, _, hi = text.partition(dash)
            lo, hi = lo.strip(), hi.strip()
            if lo.isdigit() and hi.isdigit() and int(lo) <= int(hi):
                return int(hi)
            return None
    return int(text) if text.isdigit() else None


def cell_ledger_path() -> Path:
    raw = os.environ.get("CELL_LEDGER_CSV", "").strip()
    if raw:
        return Path(raw).expanduser()
    return _root() / "docs" / "CELL_LEDGER.csv"


def load_cell_ledger(path: Path | None = None) -> LedgerCount:
    target = path if path is not None else cell_ledger_path()
    if not target.is_file():
        return LedgerCount(None, None, "总账缺失")
    try:
        with target.open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
        log.warning("cell ledger unreadable")
        return LedgerCount(None, None, "总账不可读")
    ids: list[int] = []
    tentative = 0
    skipped = 0
    reserved: list[int] = []
    for row in rows:
        upper = _cell_upper(str(row.get("cell_id") or ""))
        end_raw = str(row.get("cell_id_end") or "").strip()
        if end_raw:
            end = _cell_upper(end_raw)
            if end is None or upper is None:
                skipped += 1
                continue
            upper = max(upper, end)
        if upper is None:
            skipped += 1
            continue
        if str(row.get("status") or "").strip().lower() == "reserved":
            reserved.append(upper)
            continue
        ids.append(upper)
        if TENTATIVE_REJECT in str(row.get("status") or ""):
            tentative += 1
    if not ids:
        return LedgerCount(None, None, "总账无 cell_id", skipped, tuple(reserved))
    return LedgerCount(max(ids), tentative, None, skipped, tuple(reserved))


def grid_count_label(ledger: LedgerCount | None = None) -> str:
    led = ledger if ledger is not None else load_cell_ledger()
    if led.max_cell_id is None:
        return LEDGER_MISSING if led.reason == "总账缺失" else f"格数：{led.reason}"
    n = led.max_cell_id
    label = f"累计共测 {n} 格 · Bonferroni 按 {n}（CELL_LEDGER 最大 cell_id）"
    if led.reserved:
        ids = "、".join(str(i) for i in led.reserved)
        label += f" · 另有 {len(led.reserved)} 行 reserved（{ids}）"
    if led.skipped:
        label += f" · {led.skipped} 行无法解析已跳过"
    return label


def tentative_reject_line(ledger: LedgerCount | None = None) -> str:
    led = ledger if ledger is not None else load_cell_ledger()
    if led.tentative_reject:
        return f"{TENTATIVE_REJECT_LINE} · 总账标记 {led.tentative_reject} 格"
    return TENTATIVE_REJECT_LINE


# ---------------------------------------------------------------- supply gaps


@dataclass(frozen=True)
class SupplyGap:
    component: str
    start: datetime
    end: datetime | None  # None = ongoing
    reason: str


def supply_gaps_path() -> Path:
    raw = os.environ.get("SUPPLY_GAPS_CSV", "").strip()
    if raw:
        return Path(raw).expanduser()
    return _root() / "exports" / "reports" / "supply_gaps.csv"


def _utc(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_supply_gaps(path: Path | None = None) -> tuple[SupplyGap, ...] | None:
    """None = file missing/unreadable (show 断供记录缺失)."""
    target = path if path is not None else supply_gaps_path()
    if not target.is_file():
        return None
    try:
        with target.open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
        log.warning("supply gaps unreadable")
        return None
    out: list[SupplyGap] = []
    for row in rows:
        start = _utc(row.get("start_at") or "")
        if start is None:
            continue
        out.append(
            SupplyGap(
                component=str(row.get("component") or "").strip() or "?",
                start=start,
                end=_utc(row.get("end_at") or ""),
                reason=str(row.get("reason") or "").strip(),
            )
        )
    return tuple(out)


def day_coverage(
    day: str, gaps: tuple[SupplyGap, ...] | None, *, now: datetime | None = None
) -> str:
    """'' when no overlap; else per-component 部分/缺失 for one HKT day."""
    if gaps is None:
        return GAPS_MISSING
    try:
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=HKT)
    except ValueError:
        return ""
    d1 = d0 + timedelta(days=1)
    clock = now or datetime.now(timezone.utc)
    parts: list[str] = []
    for gap in gaps:
        end = gap.end or clock
        lo, hi = max(gap.start, d0), min(end, d1)
        if hi <= lo:
            continue
        full = lo <= d0 and hi >= min(d1, clock)
        tag = "缺失" if full else "部分"
        tail = "进行中" if gap.end is None and hi >= min(d1, clock) else f"{hi.astimezone(HKT):%H:%M}"
        span = f"{lo.astimezone(HKT):%H:%M}–{tail}"
        parts.append(f"{gap.component} {tag} {span}")
    return "；".join(parts)


# ---------------------------------------------------------------- deploy sha


@dataclass(frozen=True)
class DeployRow:
    component: str
    declared: str | None
    tree_sha256: str | None
    status: str  # ok / red / yellow / unreported / unknown
    detail: str


# component dir -> process cmdline fragment (for restart check)
DEPLOY_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("decision_engine", "decision_engine"),
    ("execution", "execution.run"),
    ("sources", "sources.jev"),
    ("review", "review"),
    ("dashboard", "dashboard.app"),
    ("indicators", "indicators.run"),
)


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _git(repo: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _tree(repo: Path, rev: str, sub: str) -> dict[str, str] | None:
    out = _git(repo, "ls-tree", "-r", rev, "--", sub)
    if out is None:
        return None
    tree: dict[str, str] = {}
    for line in out.splitlines():
        meta, _, name = line.partition("\t")
        bits = meta.split()
        if len(bits) == 3 and bits[1] == "blob":
            tree[name[len(sub) + 1:]] = bits[2]
    return tree


def _read_declared(folder: Path) -> tuple[str | None, str | None]:
    path = folder / "DEPLOY_SHA"
    if not path.is_file():
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, None
    sha = tree = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        key, sep, val = line.partition("=")
        if sep and key.strip() == "tree_sha256":
            tree = val.strip()
        elif sep and key.strip() in {"sha", "commit"}:
            sha = val.strip()
        elif not sep and sha is None:
            sha = line.split()[0]
    return sha, tree


def _proc_start(fragment: str) -> float | None:
    """Earliest start (epoch) of a process whose cmdline contains fragment."""
    best: float | None = None
    try:
        boot = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                boot = float(line.split()[1])
        tick = os.sysconf("SC_CLK_TCK")
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
                if fragment not in cmd or "python" not in cmd:
                    continue
                stat = Path(f"/proc/{pid}/stat").read_text()
                start = boot + int(stat.rsplit(")", 1)[1].split()[19]) / tick if boot else None
            except (OSError, ValueError, IndexError):
                continue
            if start is not None and (best is None or start < best):
                best = start
    except (OSError, ValueError):
        return None
    return best


def _disk_files(folder: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for path in folder.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".db"}:
            continue
        if path.name in {"DEPLOY_SHA", ".env"} or path.name.startswith(".env"):
            continue
        out[str(path.relative_to(folder))] = path
    return out


def deploy_repo_path() -> Path | None:
    raw = os.environ.get("DEPLOY_COMPARE_REPO", "").strip()
    if raw:
        return Path(raw).expanduser()
    for cand in (_root(), _root().parent / "trade-main-sync"):
        if (cand / ".git").exists():
            return cand
    return None


# live dir -> repo path on origin/main when they differ; default = same name.
DEPLOY_REPO_PATHS: dict[str, str] = {"indicators": "crawler/indicators"}


def compare_component(root: Path, repo: Path | None, name: str, proc: str) -> DeployRow:
    folder = root / name
    if not folder.is_dir():
        return DeployRow(name, None, None, "unknown", "目录不存在")
    declared, tree_sha = _read_declared(folder)
    repo_sub = DEPLOY_REPO_PATHS.get(name, name)
    disk = _disk_files(folder)
    notes: list[str] = []
    status = "ok"
    if declared is None:
        status = "unreported"
        notes.append("未报")
    if repo is None:
        notes.append("无 git 仓库可对 origin/main")
        if status == "ok":
            status = "unknown"
    else:
        main = _tree(repo, "origin/main", repo_sub)
        if main is None:
            notes.append("origin/main 不可读")
        elif not main:
            notes.append("无可比文件")
            if status == "ok":
                status = "unknown"
        else:
            tracked = main
            diff = sum(
                1 for rel, blob in tracked.items()
                if rel in disk and _blob_sha(disk[rel].read_bytes()) != blob
            )
            gone = sum(1 for rel in tracked if rel not in disk)
            notes.append(f"对 main：{diff} 文件不同 · {gone} 缺")
        if declared is not None:
            want = _tree(repo, declared, repo_sub)
            if want is None:
                status = "red"
                notes.append(f"声明 sha {declared[:8]} 仓库里找不到")
            elif not want:
                notes.append(f"声明 sha {declared[:8]} 下无可比文件")
            else:
                bad = sum(
                    1 for rel, blob in want.items()
                    if rel in disk and _blob_sha(disk[rel].read_bytes()) != blob
                ) + sum(1 for rel in want if rel not in disk)
                if bad:
                    status = "red"
                    notes.append(f"声明 sha {declared[:8]} 与文件不符 {bad} 个")
                else:
                    notes.append(f"声明 sha {declared[:8]} 与文件一致")
    if tree_sha:
        h = hashlib.sha256()
        for rel in sorted(disk):
            h.update(rel.encode() + b"\0" + hashlib.sha256(disk[rel].read_bytes()).digest())
        if h.hexdigest() != tree_sha:
            notes.append("tree_sha256 不符（算法可能不同，仅参考）")
        else:
            notes.append("tree_sha256 一致")
    started = _proc_start(proc)
    newest = max((p.stat().st_mtime for p in disk.values()), default=None)
    if started is None:
        notes.append("进程未找到")
    elif newest is not None and newest > started:
        if status not in {"red"}:
            status = "yellow"
        notes.append("磁盘已换、进程未重启")
    return DeployRow(name, declared, tree_sha, status, " · ".join(notes))


_CACHE: tuple[float, tuple[DeployRow, ...]] | None = None


def deploy_rows(*, ttl: float = 60.0) -> tuple[DeployRow, ...]:
    global _CACHE
    now = time.monotonic()
    if _CACHE is not None and now - _CACHE[0] < ttl:
        return _CACHE[1]
    root, repo = _root(), deploy_repo_path()
    rows = tuple(compare_component(root, repo, n, p) for n, p in DEPLOY_COMPONENTS)
    _CACHE = (now, rows)
    return rows


# ---------------------------------------------------------------- watchdog heartbeat

# Collection watchdog (#133) touches this file under the live root.
# Override with WATCHDOG_HEARTBEAT_FILE.
WATCHDOG_HEARTBEAT_REL = Path("logs") / "heartbeat_gap_watch.alive"
WATCHDOG_STALE_SECONDS = 300
WATCHDOG_MISSING = "看门未上线/缺失"


def watchdog_status(path: Path | None = None, *, now: float | None = None) -> tuple[str, str]:
    """(tone, text): tone ok / warn / mute. Display only."""
    raw = os.environ.get("WATCHDOG_HEARTBEAT_FILE", "").strip()
    target = path or (Path(raw).expanduser() if raw else _root() / WATCHDOG_HEARTBEAT_REL)
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return "mute", WATCHDOG_MISSING
    age = (now if now is not None else time.time()) - mtime
    stamp = datetime.fromtimestamp(mtime, HKT).strftime("%m-%d %H:%M HKT")
    if age > WATCHDOG_STALE_SECONDS:
        return "warn", f"心跳过期 {int(age // 60)} 分钟（{stamp}）"
    return "ok", f"心跳正常（{stamp}）"
