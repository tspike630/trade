import importlib.util, json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path

_p = Path(__file__).resolve().parent.parent / "scripts" / "heartbeat_gap_watch.py"
_s = importlib.util.spec_from_file_location("hgw", _p); hgw = importlib.util.module_from_spec(_s); _s.loader.exec_module(hgw)

T0 = datetime(2026, 10, 20, 17, 43, tzinfo=timezone.utc)
EXIST = (b"component,start_at,end_at,start_hkt,end_hkt,reason,source\n"
         b"execution_main_loop,2026-10-20T18:03:17Z,,2026-10-21T02:03:17+08:00,,db repair,execution\n"
         b"x,a,b,c,d,e,f\n")


def _hb(p, ts):
    p.write_text(json.dumps({"timestamp": hgw.fmt_utc(ts)}))


def test_open_idempotent_then_close(tmp_path):
    hb, csv = tmp_path / "hb.json", tmp_path / "gaps.csv"
    csv.write_bytes(EXIST)
    _hb(hb, T0)
    now = T0 + timedelta(seconds=700)
    assert hgw.check_once(hb, csv, 600, now) == "opened"
    assert hgw.check_once(hb, csv, 600, now + timedelta(minutes=5)) == "still_open"
    lines = csv.read_bytes().splitlines(keepends=True)
    assert len(lines) == 4 and csv.read_bytes().startswith(EXIST)
    assert lines[3] == (b"crawler(dex/helius/x),2026-10-20T17:43:00Z,,2026-10-21T01:43:00+08:00,,"
                        b"heartbeat_gap,collection_watchdog\n")
    t1 = T0 + timedelta(hours=6)
    _hb(hb, t1)
    assert hgw.check_once(hb, csv, 600, t1 + timedelta(seconds=5)) == "closed"
    new = csv.read_bytes().splitlines(keepends=True)
    assert new[:3] == lines[:3] and len(new) == 4
    assert new[3].decode().split(",")[2:5] == ["2026-10-20T23:43:00Z", "2026-10-21T01:43:00+08:00", "2026-10-21T07:43:00+08:00"]
    assert (tmp_path / "gaps.csv.bak").read_bytes() == b"".join(lines)
    assert hgw.check_once(hb, csv, 600, t1 + timedelta(seconds=65)) == "ok"


def test_other_open_rows_untouched_and_header_created(tmp_path):
    hb, csv = tmp_path / "hb.json", tmp_path / "r" / "gaps.csv"
    _hb(hb, T0)
    assert hgw.check_once(hb, csv, 600, T0 + timedelta(hours=1)) == "opened"
    assert csv.read_text().splitlines()[0] == hgw.HEADER


def test_restart_reuses_open_row(tmp_path):
    hb, csv = tmp_path / "hb.json", tmp_path / "gaps.csv"
    csv.write_bytes(EXIST + b"crawler(dex/helius/x),2026-10-20T17:43:00Z,,2026-10-21T01:43:00+08:00,,heartbeat_gap,collection_watchdog")
    _hb(hb, T0)
    assert hgw.check_once(hb, csv, 600, T0 + timedelta(hours=1)) == "still_open"


def test_mtime_fallback(tmp_path):
    hb = tmp_path / "hb.json"; hb.write_text("{bad")
    os.utime(hb, (T0.timestamp(), T0.timestamp()))
    assert hgw.last_heartbeat(hb) == T0


import logging, subprocess, sys, threading


def test_missing_uses_last_seen(tmp_path, caplog):
    hb, csv, st, al = tmp_path / "hb.json", tmp_path / "g.csv", tmp_path / "s.json", tmp_path / "a.alive"
    _hb(hb, T0)
    assert hgw.check_once(hb, csv, 600, T0 + timedelta(seconds=30), st, al) == "ok"
    hb.unlink()
    with caplog.at_level(logging.WARNING):
        assert hgw.check_once(hb, csv, 600, T0 + timedelta(minutes=2), st, al) == "opened"
    assert "missing" in caplog.text and al.exists()
    row = csv.read_text().splitlines()[1].split(",")
    assert row[1] == "2026-10-20T17:43:00Z" and row[5] == "heartbeat_gap"
    assert hgw.check_once(hb, csv, 600, T0 + timedelta(minutes=3), st, al) == "still_open"
    _hb(hb, T0 + timedelta(minutes=4))
    assert hgw.check_once(hb, csv, 600, T0 + timedelta(minutes=4), st, al) == "closed"


def test_missing_never_seen(tmp_path):
    hb, csv = tmp_path / "hb.json", tmp_path / "g.csv"
    assert hgw.check_once(hb, csv, 600, T0, tmp_path / "s.json", started_at=T0) == "opened"
    row = csv.read_text().splitlines()[1].split(",")
    assert row[1] == "2026-10-20T17:43:00Z" and row[5] == "heartbeat_missing"


def test_parse_fallback_warns(tmp_path, caplog):
    hb = tmp_path / "hb.json"; hb.write_text("{bad")
    with caplog.at_level(logging.WARNING):
        hgw.last_heartbeat(hb)
    assert "falling back to mtime" in caplog.text


def test_lock_blocks_writer(tmp_path):
    csv = tmp_path / "g.csv"; csv.write_bytes(EXIST)
    done = threading.Event()
    with hgw.csv_lock(csv):
        t = threading.Thread(target=lambda: (hgw.open_gap(csv, T0), done.set()))
        t.start(); t.join(0.3)
        assert not done.is_set()  # waits for the lock
        assert hgw.lock_path(csv).exists()
    t.join(5); assert done.is_set()


def test_close_retries_on_concurrent_change(tmp_path, monkeypatch):
    csv = tmp_path / "g.csv"; csv.write_bytes(EXIST)
    hgw.open_gap(csv, T0)
    real, calls = hgw._sig, {"n": 0}
    def fake(p):
        calls["n"] += 1
        s = real(p)
        return (s[0] + 1, s[1]) if calls["n"] == 2 else s  # 1st pre-replace check "changed"
    monkeypatch.setattr(hgw, "_sig", fake)
    assert hgw.close_gap(csv, T0 + timedelta(hours=1))
    assert calls["n"] >= 4 and csv.read_bytes().startswith(EXIST)
    assert not list(tmp_path.glob(".supply_gaps.*.tmp"))


def test_append_cli(tmp_path):
    csv = tmp_path / "g.csv"; csv.write_bytes(EXIST)
    cli = Path(__file__).resolve().parent.parent / "scripts" / "supply_gaps_append.py"
    r = subprocess.run([sys.executable, str(cli), "--csv", str(csv), "--component", "execution_main_loop",
                        "--start-at", "2026-10-20T18:03:17Z", "--reason", "r", "--source", "execution"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert csv.read_bytes().startswith(EXIST)
    assert csv.read_text().splitlines()[-1] == "execution_main_loop,2026-10-20T18:03:17Z,,2026-10-21T02:03:17+08:00,,r,execution"
