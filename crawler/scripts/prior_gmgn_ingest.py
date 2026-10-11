#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prior_gmgn_ingest.py — GMGN Solana Token Charts 清洗入库（Step2）

用法: python3 scripts/prior_gmgn_ingest.py
输入: data/prior/raw/gmgn/unpacked/** (csv 或 json)
输出: prior.db 的 token_ohlcv 表 + provenance 更新
原则: 先打印结构, 再映射; 不匹配就退出。
标注: Kaggle v3 为完整数据集 n=956（非 SAMPLE / 非更大 pack 缺失）。
"""
import csv, glob, json, os, sqlite3, sys
from datetime import datetime, timezone

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "prior")
DB = os.path.join(BASE, "db", "prior.db")
RAW = os.path.join(BASE, "raw", "gmgn", "unpacked")


def to_epoch(v):
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip()
    try:
        x = int(float(s))
        if x > 10**12:  # ms → sec
            x //= 1000
        return x
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(s[:26].replace("Z", ""), fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


_PARSE_LOGGED = set()


def parse_ohlcv(obj):
    """obj 可能是 list[list] / list[dict] / API wrapper / dict{k:list}; → [(ts_sec, price)]"""
    # GMGN API wrapper {"code":0,"data":{"list":[{"time":ms,...}]}}
    if isinstance(obj, dict) and "data" in obj and not any(
        k for k in obj if "time" in k.lower() or k in ("ts", "t")
    ):
        data = obj["data"]
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            obj = data["list"]
        elif isinstance(data, list):
            obj = data
    if isinstance(obj, list) and obj:
        first = obj[0]
        if isinstance(first, dict):
            keys = list(first.keys())
            sig = tuple(keys)
            if sig not in _PARSE_LOGGED:
                print("[ohlcv] dict 条目标签:", keys)
                _PARSE_LOGGED.add(sig)
            # {'t','c'} short / {'time','close'} / {'timestamp','price'} / {'0'..'4'}
            t = next((k for k in keys if k in ("t", "time", "timestamp", "ts", "0")
                      or "time" in k.lower() or k.lower() == "ts"), None)
            c = next((k for k in keys if k in ("c", "close", "price", "1", "4")
                      or "close" in k.lower() or "price" in k.lower()), None)
            if not t or not c:
                print("[FAIL] 无法定位 OHLCV 的 时间/价格 列; 样例第一条:", json.dumps(first)[:500])
                sys.exit(1)
            return [(to_epoch(r[t]) or 0, float(r[c])) for r in obj if r.get(c) is not None]
        # list[list]: [ [ts, open, high, low, close, vol], ... ]
        return [(to_epoch(r[0]) or 0, float(r[4])) for r in obj if len(r) >= 5 and r[4] is not None]
    if isinstance(obj, dict):
        # 可能是 {"ts":[...], "price":[...]} 或 {"times":..., "closes":...}
        tkey = next((k for k in obj if "time" in k.lower() or "ts" in k.lower() or k == "t"), None)
        ckey = next((k for k in obj if "close" in k.lower() or "price" in k.lower() or k == "c"
                     or "open" in k.lower()), None)
        if tkey and ckey and isinstance(obj[tkey], list) and isinstance(obj[ckey], list):
            return [(to_epoch(t) or 0, float(p)) for t, p in zip(obj[tkey], obj[ckey]) if p is not None]
        print("[FAIL] dict 结构未识别; keys:", list(obj.keys())[:20])
        sys.exit(1)
    print("[FAIL] OHLCV 类型未识别:", type(obj))
    sys.exit(1)


def main():
    if not os.path.isdir(RAW):
        sys.exit("[FAIL] 先完成 §2 获取 GMGN 数据")
    files = []
    for ext in ("*.csv", "*.json", "*.jsonl"):
        files += glob.glob(os.path.join(RAW, "**", ext), recursive=True)
    if not files:
        sys.exit("[FAIL] unpacked/ 下无 csv/json; 检查下载与解压")
    print("[gmgn] 文件:", [os.path.relpath(f, RAW) for f in files])

    conn = sqlite3.connect(DB)
    conn.executescript(open(os.path.join(BASE, "..", "..", "models", "prior_tables.sql")).read())
    # re-ingest: replace prior gmgn OHLCV rows only
    conn.execute("DELETE FROM token_ohlcv WHERE source = 'gmgn' OR source IS NULL OR source = ''")
    conn.commit()

    n = 0
    for fp in files:
        print(f"\n--- 处理 {os.path.relpath(fp, RAW)} ---")
        if fp.endswith(".csv"):
            with open(fp, newline="", encoding="utf-8", errors="replace") as f:
                rd = csv.DictReader(f)
                cols = rd.fieldnames
                print("[csv] 列名:", cols)
                def find(keys):
                    for k in cols:
                        kl = k.strip().lower()
                        if any(x in kl for x in keys):
                            return k
                    return None
                c_addr = find(["address", "mint", "token"])
                c_tick = find(["ticker", "symbol"])
                c_disc = find(["discovery", "discovered", "first_seen", "created", "launch"])
                c_death = find(["death", "reason", "status", "label"])
                c_ohlcv = find(["ohlcv", "chart", "json", "history", "data"])
                if not c_addr or not c_ohlcv:
                    print(f"[FAIL] 缺列 address/ohlcv; 真实列: {cols}")
                    sys.exit(1)
                print("[map] address=%s ticker=%s discovery=%s death=%s ohlcv=%s" % (
                    c_addr, c_tick, c_disc, c_death, c_ohlcv))
                for row in rd:
                    try:
                        bars = parse_ohlcv(json.loads(row[c_ohlcv] or "[]"))
                    except Exception as e:
                        print(f"[FAIL] OHLCV 解析异常: {e}; 原值前200字符: {str(row[c_ohlcv])[:200]}")
                        sys.exit(1)
                    if not bars:
                        continue
                    bars_sorted = sorted(bars, key=lambda x: x[0])
                    first_p = bars_sorted[0][1]
                    peak = max(bars_sorted, key=lambda x: x[1])
                    last_p = bars_sorted[-1][1]
                    conn.execute(
                        """INSERT INTO token_ohlcv
                           (token_address, ticker, discovery_ts, death_reason, ohlcv_json,
                            peak_ts, peak_price, peak_return, first_price, last_price, n_bars, ingested_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (row[c_addr].strip(), (row.get(c_tick) or "").strip() if c_tick else "",
                         to_epoch(row.get(c_disc)) if c_disc else None,
                         (row.get(c_death) or "").strip() if c_death else None,
                         json.dumps(bars, ensure_ascii=False),
                         peak[0], peak[1], (peak[1] / first_p - 1) if first_p else None,
                         first_p, last_p, len(bars), datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    )
                    n += 1
        elif fp.endswith((".json", ".jsonl")):
            with open(fp, encoding="utf-8", errors="replace") as f:
                data = json.load(f) if fp.endswith(".json") else [json.loads(l) for l in f if l.strip()]
            if isinstance(data, dict):
                data = [data]
            for row in data:
                keys = list(row.keys())
                if n == 0:
                    print("[json] 记录 key:", keys)
                addr = row.get("address") or row.get("mint") or row.get("token_address") or row.get("token")
                ohlcv = row.get("ohlcv") or row.get("chart") or row.get("history") or row.get("data")
                if not addr or not ohlcv:
                    print("[FAIL] json 记录缺 address/ohlcv; key:", keys)
                    sys.exit(1)
                bars = parse_ohlcv(ohlcv)
                if not bars:
                    continue
                bars_sorted = sorted(bars, key=lambda x: x[0])
                first_p = bars_sorted[0][1]
                peak = max(bars_sorted, key=lambda x: x[1])
                conn.execute(
                    """INSERT INTO token_ohlcv
                       (token_address, ticker, discovery_ts, death_reason, ohlcv_json,
                        peak_ts, peak_price, peak_return, first_price, last_price, n_bars, ingested_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(addr).strip(), str(row.get("ticker") or row.get("symbol") or "").strip(),
                     to_epoch(row.get("discovery_ts") or row.get("discovery") or row.get("first_seen")),
                     str(row.get("death_reason") or "").strip() or None,
                     json.dumps(bars, ensure_ascii=False),
                     peak[0], peak[1], (peak[1] / first_p - 1) if first_p else None,
                     first_p, bars_sorted[-1][1], len(bars),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                n += 1
    conn.commit()
    conn.execute("INSERT OR REPLACE INTO provenance VALUES (?,?,?,?,?,?)",
                 ("gmgn", datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "https://www.kaggle.com/datasets/moussaouallaf/gmgn-solana-token-charts",
                  "Kaggle 条款", "gmgn-solana-token-charts.zip", "OHLCV+death_reason; death_reason=监控终止原因≠归零; 完整数据集 n=956 (Kaggle moussaouallaf/gmgn-solana-token-charts v3; Rows=956; one file combined_tokens.csv; ~120928 candles; ~126 bars/token; ~11.8MB; no larger pack; acceptance unique tokens =956)"))
    conn.commit()
    row = conn.execute("SELECT count(*), count(distinct token_address) FROM token_ohlcv").fetchone()
    print(f"\n[汇总] token_ohlcv: 行={row[0]} 唯一token={row[1]}")
    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
