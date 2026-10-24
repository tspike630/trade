"""Display-only truth readers: cell ledger, supply gaps, deploy sha.

Nothing here writes, trades, or touches gates. Missing inputs come back as
an explicit "missing" reason, never as a zero or a hardcoded fallback.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, replace
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
    return _parse_ledger_rows(rows)


def _parse_ledger_rows(rows: list[dict[str, str]]) -> LedgerCount:
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


@dataclass(frozen=True)
class LedgerSource:
    kind: str  # "override" / "git" / "missing"
    detail: str  # path for override; "origin/main@<sha>" for git
    sha: str | None = None
    fetched_at: datetime | None = None
    stale: bool = False
    reason: str | None = None


LEDGER_GIT_PATH = "docs/CELL_LEDGER.csv"
FETCH_STALE_SECONDS = 3600
FETCH_INTERVAL_SECONDS = 900
_FETCH_STATE: dict[str, object] = {"last_error": None}
_LEDGER_CACHE: tuple[float, LedgerCount, LedgerSource] | None = None


def _fetch_head_mtime(repo: Path) -> float | None:
    best: float | None = None
    for args in (("rev-parse", "--git-path", "FETCH_HEAD"), ("rev-parse", "--git-common-dir")):
        out = _git(repo, *args)
        if not out:
            continue
        path = Path(out.strip())
        if args[-1] == "--git-common-dir":
            path = path / "FETCH_HEAD"
        if not path.is_absolute():
            path = repo / path
        try:
            m = path.stat().st_mtime
        except OSError:
            continue
        best = m if best is None else max(best, m)
    return best


def load_ledger_with_source(*, now: float | None = None) -> tuple[LedgerCount, LedgerSource]:
    """CELL_LEDGER_CSV override first (本地覆盖), else git show origin/main."""
    raw = os.environ.get("CELL_LEDGER_CSV", "").strip()
    if raw:
        path = Path(raw).expanduser()
        return load_cell_ledger(path), LedgerSource("override", str(path))
    repo = deploy_repo_path()
    if repo is None:
        return LedgerCount(None, None, "总账缺失（无比对仓库）"), LedgerSource("missing", "", reason="无比对仓库")
    sha_out = _git(repo, "rev-parse", "origin/main")
    if sha_out is None:
        return LedgerCount(None, None, "总账缺失（git 不可用或无 origin/main）"), LedgerSource(
            "missing", "", reason="git 不可用或无 origin/main"
        )
    sha = sha_out.strip()
    mtime = _fetch_head_mtime(repo)
    clock = now if now is not None else time.time()
    fetched = datetime.fromtimestamp(mtime, HKT) if mtime is not None else None
    stale = mtime is None or clock - mtime > FETCH_STALE_SECONDS or bool(_FETCH_STATE.get("last_error"))
    src = LedgerSource("git", f"origin/main@{sha[:8]}", sha, fetched, stale)
    text = _git(repo, "show", f"{sha}:{LEDGER_GIT_PATH}")
    if text is None:
        return LedgerCount(None, None, f"总账缺失（origin/main 无 {LEDGER_GIT_PATH}）"), replace(
            src, kind="missing", reason=f"origin/main 无 {LEDGER_GIT_PATH}"
        )
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except csv.Error:
        return LedgerCount(None, None, "总账缺失（git 内容不可解析）"), replace(src, kind="missing")
    return _parse_ledger_rows(rows), src


def cached_ledger(*, ttl: float = 60.0) -> tuple[LedgerCount, LedgerSource]:
    global _LEDGER_CACHE
    if os.environ.get("CELL_LEDGER_CSV", "").strip():
        return load_ledger_with_source()
    mono = time.monotonic()
    if _LEDGER_CACHE is not None and mono - _LEDGER_CACHE[0] < ttl:
        return _LEDGER_CACHE[1], _LEDGER_CACHE[2]
    led, src = load_ledger_with_source()
    _LEDGER_CACHE = (mono, led, src)
    return led, src


def ledger_source_line(src: LedgerSource) -> tuple[str, bool]:
    """(text, warn). warn = yellow."""
    if src.kind == "override":
        return f"来源：本地覆盖 {src.detail}（临时副本）", True
    if src.kind == "missing":
        return f"来源：缺失（{src.reason or '未知'}）", True
    fetched = src.fetched_at.strftime("%m-%d %H:%M HKT") if src.fetched_at else "从未 fetch"
    err = _FETCH_STATE.get("last_error")
    tail = f" · 最近 fetch 失败：{err}" if err else ""
    stale = " · 超过 1 小时未 fetch" if src.stale and not err else ""
    return f"来源：git origin/main {src.sha[:8] if src.sha else '?'} · 本地副本 fetch 于 {fetched}{stale}{tail}", src.stale


def fetch_compare_repo_once() -> bool:
    """Read-only `git fetch origin` (never touches the work tree)."""
    repo = deploy_repo_path()
    if repo is None:
        _FETCH_STATE["last_error"] = "无比对仓库"
        log.warning("ledger fetch skipped: no compare repo")
        return False
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "fetch", "-q", "origin"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _FETCH_STATE["last_error"] = type(exc).__name__
        log.warning("compare repo fetch failed: %s", type(exc).__name__)
        return False
    if done.returncode != 0:
        _FETCH_STATE["last_error"] = f"exit {done.returncode}"
        log.warning("compare repo fetch failed: exit %s", done.returncode)
        return False
    _FETCH_STATE["last_error"] = None
    global _LEDGER_CACHE, _CACHE
    _LEDGER_CACHE = None
    _CACHE = None
    return True


def start_background_fetch(interval: float = FETCH_INTERVAL_SECONDS) -> threading.Thread:
    def loop() -> None:
        while True:
            try:
                fetch_compare_repo_once()
            except Exception:
                log.exception("compare repo fetch loop error")
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="compare-repo-fetch", daemon=True)
    thread.start()
    return thread


def grid_count_label(ledger: LedgerCount | None = None) -> str:
    led = ledger if ledger is not None else cached_ledger()[0]
    if led.max_cell_id is None:
        if led.reason == "总账缺失":
            return LEDGER_MISSING
        if (led.reason or "").startswith("总账缺失"):
            return f"格数：{led.reason}"
        return f"格数：{led.reason}"
    n = led.max_cell_id
    label = f"累计共测 {n} 格 · Bonferroni 按 {n}（CELL_LEDGER 最大 cell_id）"
    if led.reserved:
        ids = "、".join(str(i) for i in led.reserved)
        label += f" · 另有 {len(led.reserved)} 行 reserved（{ids}）"
    if led.skipped:
        label += f" · {led.skipped} 行无法解析已跳过"
    return label


def tentative_reject_line(ledger: LedgerCount | None = None) -> str:
    led = ledger if ledger is not None else cached_ledger()[0]
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


def _git_rc(repo: Path, *args: str) -> int | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode


def declared_lags_tip(repo: Path, declared: str) -> bool | None:
    """True when ``declared`` is a strict ancestor of ``origin/main``.

    None means git could not answer. A lag is a stamp note, not a dead process.
    """
    tip = _git(repo, "rev-parse", "--verify", "origin/main")
    if tip is None:
        return None
    tip_sha = tip.strip()
    declared_sha = declared.strip()
    if not tip_sha or not declared_sha:
        return None
    if declared_sha == tip_sha:
        return False
    code = _git_rc(repo, "merge-base", "--is-ancestor", declared_sha, "origin/main")
    if code == 0:
        return True
    if code == 1:
        return False
    return None


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


_MAIN_DIFF_RE = re.compile(r"(?:^| · )对 main：(\d+) 文件不同 · (\d+) 缺(?:$| · )")


def main_diff_nonzero(detail: str) -> bool:
    """True when detail reports a non-zero diff or missing count against main.

    Matches the exact phrase produced by compare_component (not a substring of
    "10 文件不同"). Numbers are parsed; a zero/zero report is not a diff.
    """
    matched = _MAIN_DIFF_RE.search(f" · {detail} · ")
    if matched is None:
        return False
    return int(matched.group(1)) != 0 or int(matched.group(2)) != 0


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
    blobs_match_tip = False
    declared_mismatch: str | None = None
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
            blobs_match_tip = diff == 0 and gone == 0
            if (diff or gone) and status == "ok":
                status = "yellow"
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
                    declared_mismatch = f"声明 sha {declared[:8]} 与文件不符 {bad} 个"
                    notes.append(declared_mismatch)
                else:
                    notes.append(f"声明 sha {declared[:8]} 与文件一致")
    if (
        status == "red"
        and blobs_match_tip
        and declared_mismatch is not None
        and declared is not None
        and repo is not None
        and declared_lags_tip(repo, declared) is True
    ):
        status = "yellow"
        lag = f"声明 sha {declared[:8]} 落后 tip，文件与 tip 一致"
        notes = [lag if note == declared_mismatch else note for note in notes]
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


# ---------------------------------------------------------------- supply / wal lock

# Crawler heartbeat (crawler/config.py HEARTBEAT_STALE_SEC). Display only.
SUPPLY_HEARTBEAT_REL = Path("logs") / "heartbeat.json"
SUPPLY_HEARTBEAT_STALE_SEC = 120
SUPPLY_WAL_NAME = "dexscreener.db-wal"
_STATUS_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


@dataclass(frozen=True)
class SupplyWalLock:
    """File evidence beside the db root. None numbers were not observed."""

    tone: str  # ok / warn / mute
    age_seconds: float | None
    mtime_hkt: str | None
    status: str | None
    wal_bytes: int | None
    where: str
    note: str


def supply_heartbeat_candidates(root: Path | None = None) -> tuple[tuple[str, Path], ...]:
    base = root if root is not None else _root()
    return (
        ("DASHBOARD_DB_ROOT/logs/heartbeat.json", base / SUPPLY_HEARTBEAT_REL),
        ("上级/logs/heartbeat.json", base.parent / SUPPLY_HEARTBEAT_REL),
    )


def _supply_tone(age: float | None, status: str | None, json_ok: bool) -> str:
    if age is None:
        return "mute"
    bad = status in {"error", "failed", "degraded"}
    if age < 0:
        return "warn" if bad or not json_ok else "mute"
    stale = age > SUPPLY_HEARTBEAT_STALE_SEC
    if stale or bad or not json_ok:
        return "warn"
    if status == "ok":
        return "ok"
    return "mute"


def supply_wal_lock(root: Path | None = None, *, now: float | None = None) -> SupplyWalLock:
    """Heartbeat mtime plus optional WAL size. Missing files stay blank."""
    base = root if root is not None else _root()
    clock = time.time() if now is None else now
    where = ""
    target: Path | None = None
    for label, path in supply_heartbeat_candidates(base):
        try:
            exists = path.is_file()
        except OSError:
            continue
        if exists:
            where = label
            target = path
            break
    age: float | None = None
    mtime_hkt: str | None = None
    status: str | None = None
    notes: list[str] = []
    tone = "mute"
    if target is None:
        notes.append("心跳文件缺失")
    else:
        try:
            mtime = target.stat().st_mtime
        except OSError:
            notes.append("心跳文件不可读")
        else:
            age = clock - mtime
            mtime_hkt = datetime.fromtimestamp(mtime, HKT).strftime("%m-%d %H:%M HKT")
            json_ok = False
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                notes.append("心跳 JSON 不可读")
            else:
                if not isinstance(payload, dict):
                    notes.append("心跳 JSON 不可读")
                else:
                    json_ok = True
                    raw_status = payload.get("status")
                    if isinstance(raw_status, str) and _STATUS_RE.fullmatch(raw_status.strip()):
                        status = raw_status.strip()
                    elif raw_status is None or (isinstance(raw_status, str) and not raw_status.strip()):
                        notes.append("无 status 字段")
                    else:
                        notes.append("status 不可读")
            if age < 0:
                notes.append("mtime 在未来")
            tone = _supply_tone(age, status, json_ok)
    wal_bytes: int | None = None
    wal_path = base / SUPPLY_WAL_NAME
    try:
        if wal_path.is_file():
            wal_bytes = wal_path.stat().st_size
        else:
            notes.append("WAL 无")
    except OSError:
        notes.append("WAL 不可读")
    return SupplyWalLock(
        tone=tone,
        age_seconds=age,
        mtime_hkt=mtime_hkt,
        status=status,
        wal_bytes=wal_bytes,
        where=where,
        note=" · ".join(notes),
    )
