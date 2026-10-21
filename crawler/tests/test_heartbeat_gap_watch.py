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
