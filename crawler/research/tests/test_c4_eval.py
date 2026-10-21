"""Unit tests for the locked C4 eval script — SYNTHETIC fixture only (no real DB)."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

import numpy as np

from research import c4_eval as C

T0 = C._ts(C.WINDOW_START)
STEP = 300  # 5 min


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA = """CREATE TABLE pair_history (id INTEGER PRIMARY KEY, chain_id TEXT, pair_address TEXT, token_address TEXT,
 recorded_at TEXT, price_usd REAL, volume_h24 REAL, volume_h1 REAL, liquidity_usd REAL, txns_h24 INTEGER, tier TEXT,
 market_cap REAL, fdv REAL, txns_m5_buys INTEGER, txns_m5_sells INTEGER, txns_h1_buys INTEGER, txns_h1_sells INTEGER,
 volume_m5 REAL)"""


def series(n=100, px=1.0, liq=200_000.0, buys=10, sells=10, vm5=100.0, tier="hot"):
    return [dict(px=px, liq=liq, buys=buys, sells=sells, vm5=vm5, tier=tier) for _ in range(n)]


def trigger_at(s, i, buys=20, sells=5, vm5=1000.0):
    s[i].update(buys=buys, sells=sells, vm5=vm5)


def make_db(path, pairs, t0=T0):
    con = sqlite3.connect(path); con.execute(SCHEMA)
    for name, tok, rows in pairs:
        for k, r in enumerate(rows):
            ts = t0 + k * STEP
            con.execute("INSERT INTO pair_history(chain_id,pair_address,token_address,recorded_at,price_usd,liquidity_usd,tier,fdv,txns_m5_buys,txns_m5_sells,volume_m5) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        ("solana", name, tok, iso(ts), r["px"], r["liq"], r["tier"],
                         None if r["px"] is None else r["px"] * 1e9, r["buys"], r["sells"], r["vm5"]))
    con.commit(); con.close()


def load(rows, name="p1", tok="t1"):
    d = tempfile.mkdtemp(); path = os.path.join(d, "f.db")
    make_db(path, [(name, tok, rows)])
    return C.load_pairs(C.open_ro(path))[0], path


class TestTrigger(unittest.TestCase):
    def test_basic_trigger(self):
        s = series(); trigger_at(s, 80)
        p, _ = load(s)
        self.assertEqual(list(np.where(C.c4_trigger(p))[0]), [80])

    def test_buy_share_and_txns_thresholds(self):
        s = series(); trigger_at(s, 80, buys=12, sells=8)   # 0.60 < 0.65
        trigger_at(s, 85, buys=13, sells=6)                 # 19 txns < 20
        trigger_at(s, 90, buys=13, sells=7)                 # 0.65, 20 -> trigger
        p, _ = load(s)
        self.assertEqual(list(np.where(C.c4_trigger(p))[0]), [90])

    def test_volume_multiple(self):
        s = series(); trigger_at(s, 80, vm5=299.0); trigger_at(s, 90, vm5=300.0)
        p, _ = load(s)
        self.assertEqual(list(np.where(C.c4_trigger(p))[0]), [90])

    def test_null_and_zero_no_trigger(self):
        s = series(); trigger_at(s, 80); s[80]["buys"] = None
        trigger_at(s, 85); s[85]["vm5"] = None
        s[90].update(buys=0, sells=0, vm5=1000.0)
        p, _ = load(s)
        self.assertFalse(C.c4_trigger(p).any())

    def test_zero_median_no_trigger(self):
        s = series(vm5=0.0); trigger_at(s, 80)
        p, _ = load(s)
        self.assertFalse(C.c4_trigger(p).any())

    def test_min_nonnull_history(self):
        s = series(); trigger_at(s, 29)   # only 29 prior snapshots
        trigger_at(s, 30)
        p, _ = load(s)
        self.assertEqual(list(np.where(C.c4_trigger(p))[0]), [30])

    def test_lookback_history_used_but_no_events_before_0708(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "f.db")
        # 01:08 HKT lookback start .. 07:08 HKT = 72 snapshots of history
        s = series(120); trigger_at(s, 50); trigger_at(s, 85)   # 50 = pre-07:08, 85 = after
        make_db(path, [("p1", "t1", s)], t0=C._ts(C.LOOKBACK_START))
        p = C.load_pairs(C.open_ro(path))[0]
        self.assertEqual(len(p.t), 120)
        self.assertEqual(list(np.where(C.c4_trigger(p))[0]), [50, 85])   # history-based trigger computable
        end = C._ts(C.WINDOW_END)
        for kind in ("c4", "random"):
            ev, _ = C.build_events([p], kind, end)
            self.assertTrue(all(e["trigger_ts"] >= T0 for e in ev), kind)
        ev, _ = C.build_events([p], "c4", end)
        self.assertEqual([e["trigger_ts"] for e in ev], [C._ts(C.LOOKBACK_START) + 85 * STEP])
        # first post-07:08 snapshot already has >=30 prior snapshots thanks to lookback
        self.assertTrue(C.pool_mask(p)[72])

    def test_data_before_lookback_start_ignored(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "f.db")
        make_db(path, [("p1", "t1", series(60))], t0=C._ts(C.LOOKBACK_START) - 40 * STEP)
        self.assertEqual(len(C.load_pairs(C.open_ro(path))[0].t), 20)

    def test_6h_boundary(self):
        # t-6h included, t excluded: snapshot at exactly t-6h counts toward the 30
        d = tempfile.mkdtemp(); path = os.path.join(d, "f.db")
        con = sqlite3.connect(path); con.execute(SCHEMA)
        t = T0 + 7 * 3600
        times = [t - 6 * 3600] + [t - 6 * 3600 + 600 * (k + 1) for k in range(29)] + [t]
        for ts in times:
            con.execute("INSERT INTO pair_history(chain_id,pair_address,token_address,recorded_at,price_usd,liquidity_usd,tier,fdv,txns_m5_buys,txns_m5_sells,volume_m5) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        ("solana", "p1", "t1", iso(ts), 1.0, 2e5, "hot", 1e9, 20, 5, 100.0 if ts < t else 1000.0))
        con.commit(); con.close()
        p = C.load_pairs(C.open_ro(path))[0]
        self.assertTrue(C.c4_trigger(p)[30]); self.assertTrue(C.pool_mask(p)[30])


class TestPool(unittest.TestCase):
    def test_hot_only(self):
        s = series(); s[80]["tier"] = "warm"
        p, _ = load(s)
        self.assertFalse(C.pool_mask(p)[80]); self.assertTrue(C.pool_mask(p)[81])

    def test_prior_snapshots(self):
        p, _ = load(series())
        m = C.pool_mask(p)
        self.assertFalse(m[29]); self.assertTrue(m[30])

    def test_liq_floor(self):
        s = series(); s[80]["liq"] = 49_999.0; s[81]["liq"] = None
        p, _ = load(s)
        m = C.pool_mask(p)
        self.assertFalse(m[80]); self.assertFalse(m[81]); self.assertTrue(m[82])


class TestULJ(unittest.TestCase):
    def test_flags(self):
        s = series()
        s[50]["liq"] = 500.0            # L abs
        s[60]["px"] = 10.0; s[60]["liq"] = 200_000.0   # J: x10 price, flat liq
        s[70]["liq"] = 10_000.0         # L rel (<0.1*median 200k)
        p, _ = load(s)
        U, L, Lrel, J = C.ulj_flags(p)
        self.assertTrue(L[50]); self.assertTrue(J[60]); self.assertTrue(Lrel[70]); self.assertFalse((U | L | J)[40])

    def test_unit_flag(self):
        s = series(); s[50]["px"] = 1e-20
        p, _ = load(s)
        self.assertTrue(C.ulj_flags(p)[0][50])

    def test_entry_skip(self):
        s = series(); trigger_at(s, 80); s[81]["px"] = 10.0  # entry snapshot J-bad
        d = tempfile.mkdtemp(); path = os.path.join(d, "f.db"); make_db(path, [("p1", "t1", s)])
        ev, st = C.build_events(C.load_pairs(C.open_ro(path)), "c4", C._ts(C.WINDOW_END))
        self.assertEqual(ev, []); self.assertEqual(st["ulj_skip"], 1)


class TestTrades(unittest.TestCase):
    def ev(self, s):
        d = tempfile.mkdtemp(); path = os.path.join(d, "f.db"); make_db(path, [("p1", "t1", s)])
        return C.build_events(C.load_pairs(C.open_ro(path)), "c4", C._ts(C.WINDOW_END))[0]

    def test_next_snapshot_entry_and_tp(self):
        s = series(); trigger_at(s, 80); s[80]["px"] = 0.5   # trigger price must NOT be used
        s[81]["px"] = 1.0; s[83]["px"] = 1.2
        e = self.ev(s)[0]
        self.assertEqual(e["entry_price"], 1.0); self.assertEqual(e["entry_ts"], T0 + 81 * STEP)
        self.assertAlmostEqual(e["ret_tp10_sl5_h60"], 0.10); self.assertEqual(e["why_tp10_sl5_h60"], "tp")

    def test_sl_and_time60(self):
        s = series(); trigger_at(s, 80); s[83]["px"] = 0.9
        for k in range(84, 100): s[k]["px"] = 1.02
        e = self.ev(s)[0]
        self.assertAlmostEqual(e["ret_tp10_sl5_h60"], -0.10); self.assertEqual(e["why_tp10_sl5_h60"], "sl")
        self.assertAlmostEqual(e["ret_time60"], 0.02)   # last snapshot within 60 min (k=93)

    def test_sl_gap_recorded_report_only(self):
        s = series(); trigger_at(s, 80); s[83]["px"] = 0.9; s[83]["liq"] = 123_456.0
        e = self.ev(s)[0]
        self.assertEqual(e["sl_gap_sec"], 300); self.assertEqual(e["sl_liq"], 123_456.0)
        s2 = series(); trigger_at(s2, 80)
        self.assertIsNone(self.ev(s2)[0]["sl_gap_sec"])   # no SL -> not recorded

    def test_sl_gap_report_thresholds(self):
        ev = [{"token": f"t{i}", "pair": f"p{i}", "trigger_ts": i, "sl_gap_sec": g, "sl_liq": 1e5}
              for i, g in enumerate([300, 600, 601, 900, 901, 1200])] + [{"token": "x", "sl_gap_sec": None}]
        r = C.sl_gap_report(ev)
        self.assertEqual(r["n_sl"], 6); self.assertEqual(r["gap_sec_dist"]["max"], 1200.0)
        self.assertEqual(r["n_gt_600s"], 4); self.assertEqual(r["n_gt_900s"], 2)
        self.assertEqual([x["gap_sec"] for x in r["gt_900s"]], [901, 1200])
        self.assertEqual(C.sl_gap_report([])["n_sl"], 0)

    def test_collapse(self):
        s = series(); trigger_at(s, 80); s[85]["liq"] = 5_000.0
        e = self.ev(s)[0]
        self.assertEqual(e["ret_tp10_sl5_h60"], -1.0); self.assertEqual(e["ret_time60"], -1.0)
        self.assertEqual(e["why_time60"], "collapse")

    def test_in_hold_bad_tick_ignored(self):
        s = series(); trigger_at(s, 80); s[84]["px"] = 10.0  # J spike, ignored
        e = self.ev(s)[0]
        self.assertEqual(e["why_tp10_sl5_h60"], "hold"); self.assertAlmostEqual(e["ret_tp10_sl5_h60"], 0.0)

    def test_dedup_60min(self):
        s = series(140); trigger_at(s, 80); trigger_at(s, 90); trigger_at(s, 92)
        self.assertEqual([e["trigger_ts"] for e in self.ev(s)], [T0 + 80 * STEP, T0 + 92 * STEP])

    def test_no_valid_post_price_settles_at_last_valid_and_counted(self):
        s = series(); trigger_at(s, 80)
        for k in range(82, 100): s[k]["px"] = None   # every post-entry tick invalid (U)
        d = tempfile.mkdtemp(); path = os.path.join(d, "f.db"); make_db(path, [("p1", "t1", s)])
        ev, st = C.build_events(C.load_pairs(C.open_ro(path)), "c4", C._ts(C.WINDOW_END))
        self.assertEqual(len(ev), 1); self.assertTrue(ev[0]["no_valid_post"])
        for ex in C.EXITS:
            self.assertAlmostEqual(ev[0][f"ret_{ex}_a"], 0.0)    # variant (a): last valid price = entry
            self.assertEqual(ev[0][f"ret_{ex}"], -1.0)           # variant (b): -100%, used for verdict
        self.assertEqual(st["no_valid_post_price"], 1)
        self.assertEqual(C.summarize(ev, "time60")["no_valid_post_price"], 1)
        self.assertEqual(C.summarize(ev, "time60")["pre_mean"], -1.0)
        self.assertEqual(C.summarize(ev, "time60_a")["pre_mean"], 0.0)

    def test_nvp_both_variants_reported_and_verdict_uses_b(self):
        mk = lambda tok, nvp: {"token": tok, "cost": 0.0, "no_valid_post": nvp,
                               **{f"ret_{x}": (-1.0 if nvp else 0.05) for x in C.EXITS},
                               **{f"ret_{x}_a": 0.05 if not nvp else 0.0 for x in C.EXITS},
                               **{f"why_{x}": "x" for x in C.EXITS}, **{f"why_{x}_a": "x" for x in C.EXITS}}
        c4 = [mk(f"c{i}", i < 3) for i in range(40)]
        rnd = [mk(f"r{i}", False) for i in range(40)]
        for e in rnd:
            for x in C.EXITS: e[f"ret_{x}"] = e[f"ret_{x}_a"] = -0.05
        r = C.evaluate(c4, rnd, "train", 268)["exits"]["tp10_sl5_h60"]
        self.assertIn("variant_a", r)
        self.assertLess(r["c4"]["net_mean"], 0)                     # (b) drags mean below 0
        self.assertGreater(r["variant_a"]["c4"]["net_mean"], 0)     # (a) would be positive
        self.assertFalse(r["pass"])                                 # verdict follows (b)
        nr = C.nvp_report(c4); self.assertEqual(nr["no_valid_post_price"], 3); self.assertTrue(nr["warning"])
        self.assertFalse(C.nvp_report([mk("a", True)] + [mk(str(i), False) for i in range(49)])["warning"])  # 2% not > 2%
        # no nvp -> no variant_a block
        self.assertNotIn("variant_a", C.evaluate(rnd, rnd, "train", 268)["exits"]["tp10_sl5_h60"])

    def test_random_nvp_same_rule(self):
        s = series(); s[82:100] = [dict(r, px=None) for r in s[82:100]]
        d = tempfile.mkdtemp(); path = os.path.join(d, "f.db"); make_db(path, [("p1", "t1", s)])
        pairs = C.load_pairs(C.open_ro(path)); end = C._ts(C.WINDOW_END)
        for seed in range(200):
            ev, st = C.build_events(pairs, "random", end, seed=seed)
            hit = [e for e in ev if e["no_valid_post"]]
            if hit:
                self.assertEqual(hit[0]["ret_tp10_sl5_h60"], -1.0); self.assertEqual(hit[0]["ret_tp10_sl5_h60_a"], 0.0)
                self.assertEqual(st["no_valid_post_price"], len(hit)); return
        self.fail("no random nvp event found")

    def test_collapse_counts_as_valid_minus_100(self):
        s = series(); trigger_at(s, 80)
        for k in range(82, 100): s[k]["liq"] = 5_000.0
        e = self.ev(s)[0]
        self.assertFalse(e["no_valid_post"]); self.assertEqual(e["ret_tp10_sl5_h60"], -1.0)

    def test_no_full_hold_excluded(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "f.db")
        s = series(); trigger_at(s, 95); make_db(path, [("p1", "t1", s)])
        end = T0 + 99 * STEP
        ev, st = C.build_events(C.load_pairs(C.open_ro(path), C.WINDOW_START, iso(end)), "c4", end)
        self.assertEqual(ev, []); self.assertEqual(st["no_full_hold"], 1)


class TestEntryGap(unittest.TestCase):
    def run_c4(self, extra_sec):
        d = tempfile.mkdtemp(); path = os.path.join(d, "f.db")
        con = sqlite3.connect(path); con.execute(SCHEMA)
        s = series(120); trigger_at(s, 80)
        for k, r in enumerate(s):
            ts = T0 + k * STEP + (extra_sec if k > 80 else 0)
            con.execute("INSERT INTO pair_history(chain_id,pair_address,token_address,recorded_at,price_usd,liquidity_usd,tier,fdv,txns_m5_buys,txns_m5_sells,volume_m5) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        ("solana", "p1", "t1", iso(ts), r["px"], r["liq"], r["tier"], r["px"] * 1e9, r["buys"], r["sells"], r["vm5"]))
        con.commit(); con.close()
        return C.build_events(C.load_pairs(C.open_ro(path)), "c4", C._ts(C.WINDOW_END))

    def test_gap_exactly_10min_ok(self):
        ev, st = self.run_c4(300)          # entry 600s after trigger
        self.assertEqual(len(ev), 1); self.assertEqual(st["invalid_gap"], 0); self.assertEqual(st["triggers"], 1)

    def test_gap_over_10min_skipped_and_counted(self):
        ev, st = self.run_c4(301)          # 601s
        self.assertEqual(ev, []); self.assertEqual(st["invalid_gap"], 1); self.assertEqual(st["triggers"], 1)
        self.assertTrue(C.gap_report(st)["warning"])

    def random_with_spacing(self, step, n=300):
        d = tempfile.mkdtemp(); path = os.path.join(d, "f.db")
        con = sqlite3.connect(path); con.execute(SCHEMA)
        for k in range(n):
            con.execute("INSERT INTO pair_history(chain_id,pair_address,token_address,recorded_at,price_usd,liquidity_usd,tier,fdv,txns_m5_buys,txns_m5_sells,volume_m5) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        ("solana", "p1", "t1", iso(T0 + k * step), 1.0, 2e5, "hot", 1e9, 10, 10, 100.0))
        con.commit(); con.close()
        return C.build_events(C.load_pairs(C.open_ro(path)), "random", C._ts(C.WINDOW_END))

    def test_random_gap_rule(self):
        # random baseline: entry gap exactly 600s kept, 601s skipped + counted
        ev, st = self.random_with_spacing(600)
        self.assertGreater(st["triggers"], 0); self.assertEqual(st["invalid_gap"], 0)
        self.assertGreater(len(ev), 0)
        self.assertTrue(all(e["entry_ts"] - e["trigger_ts"] == 600 for e in ev))
        self.assertFalse(C.gap_report(st)["warning"])
        ev, st = self.random_with_spacing(601)
        self.assertGreater(st["triggers"], 0)
        self.assertEqual(st["invalid_gap"], st["triggers"])     # every random trigger skipped
        self.assertEqual(ev, [])                                # excluded from returns
        g = C.gap_report(st); self.assertTrue(g["warning"]); self.assertEqual(g["invalid_gap_frac"], 1.0)


class TestCostStats(unittest.TestCase):
    def test_tier_cost(self):
        self.assertEqual(C.tier_cost(2e6), 0.006); self.assertEqual(C.tier_cost(1e6), 0.02)
        self.assertEqual(C.tier_cost(5e4), 0.02)
        e = {"ret_x": 0.1, "cost": 0.02}
        self.assertAlmostEqual(C.net(e, "x"), 1.1 * 0.98 - 1); self.assertAlmostEqual(C.net(e, "x", 2.0), 1.1 * 0.96 - 1)

    def test_bonferroni(self):
        self.assertAlmostEqual(C.bonferroni_alpha(268), 0.05 / 268)
        with self.assertRaises(ValueError): C.bonferroni_alpha(0)

    def test_fixed_cut(self):
        cut = C.cut_ts()
        self.assertEqual(cut, C._ts("2026-10-23T02:15:12+08:00"))
        self.assertEqual(C.CUT_TS, C._ts("2026-10-22T18:15:12Z"))
        self.assertFalse(hasattr(C, "CUT_PCT"))
        ev = [{"trigger_ts": cut - 1}, {"trigger_ts": cut}, {"trigger_ts": cut + 1}]
        self.assertEqual(C.segment(ev, cut, "train"), ev[:2]); self.assertEqual(C.segment(ev, cut, "oos"), ev[2:])

    def test_cut_independent_of_events(self):
        # same cut regardless of data -> shared by C4 and random
        self.assertEqual(C.cut_ts(), C._ts(C.CUT_ISO))

    def test_gap_report(self):
        self.assertFalse(C.gap_report({"triggers": 100, "invalid_gap": 5})["warning"])
        g = C.gap_report({"triggers": 100, "invalid_gap": 6})
        self.assertTrue(g["warning"]); self.assertAlmostEqual(g["invalid_gap_frac"], 0.06)
        self.assertFalse(C.gap_report({"triggers": 0, "invalid_gap": 0})["warning"])

    def test_no_hardcoded_pids(self):
        import re
        src = open(C.__file__).read()
        self.assertIsNone(re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}", src))
        self.assertIsNone(re.search(r"0x[0-9a-fA-F]{40}", src))

    def test_cluster_bootstrap_and_p(self):
        a = [{"token": f"a{i%40}"} for i in range(80)]; b = [{"token": f"b{i%40}"} for i in range(80)]
        p, _ = C.diff_pvalue(a, [0.05] * 80, b, [-0.05] * 80, n_boot=500)
        self.assertEqual(p, 0.0)
        p2, _ = C.diff_pvalue(a, [-0.05] * 80, b, [0.05] * 80, n_boot=500)
        self.assertEqual(p2, 1.0)
        # clustering: one token dominating -> wide CI
        ev = [{"token": "x"}] * 50 + [{"token": f"y{i}"} for i in range(30)]
        lo, hi = C.cluster_ci(ev, [1.0] * 50 + [0.0] * 30, n_boot=500)
        self.assertLess(lo, 0.3); self.assertGreater(hi, 0.8)

    def test_validity(self):
        self.assertFalse(C.valid([{"token": "a"}] * 40))
        self.assertTrue(C.valid([{"token": str(i)} for i in range(30)]))
        self.assertFalse(C.valid([{"token": str(i)} for i in range(29)]))


class TestRandomAndRun(unittest.TestCase):
    def make_many(self, n_pairs=80, trig=True, t0=T0):
        d = tempfile.mkdtemp(); path = os.path.join(d, "f.db"); pairs = []
        for k in range(n_pairs):
            s = series(200)
            if trig:
                trigger_at(s, 40 + (k * 7) % 140)
            pairs.append((f"p{k:03d}", f"t{k:03d}", s))
        make_db(path, pairs, t0=t0); return d, path

    def test_random_same_pool_and_seed(self):
        d, path = self.make_many(10)
        pairs = C.load_pairs(C.open_ro(path)); end = C._ts(C.WINDOW_END)
        a, _ = C.build_events(pairs, "random", end); b, _ = C.build_events(pairs, "random", end)
        self.assertEqual(a, b)
        self.assertTrue(len(a) > 0)
        for e in a:  # pool: entry after >=30 prior snapshots (trigger idx>=30)
            self.assertGreaterEqual((e["trigger_ts"] - T0) // STEP, 30)
        c, _ = C.build_events(pairs, "random", end, seed=1)
        self.assertNotEqual(a, c)

    def test_readonly(self):
        d, path = self.make_many(2)
        con = C.open_ro(path)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("INSERT INTO pair_history(pair_address) VALUES ('x')")

    def test_time_check_blinding(self):
        d, path = self.make_many(2)
        with self.assertRaises(SystemExit):
            C.run(path, "train", 268, d, now_ts=C._ts(C.WINDOW_END) - 1)

    def test_train_then_oos_once(self):
        d, path = self.make_many(80)
        tr = C.run(path, "train", 268, d, skip_time_check=True)
        self.assertIn("cut_iso", tr); self.assertTrue(tr["valid_c4"])
        trp = os.path.join(d, "c4_train_result.json")
        # force pass so the OOS path can be exercised on the fixture
        j = json.load(open(trp)); j["exits"]["tp10_sl5_h60"]["pass"] = True; json.dump(j, open(trp, "w"))
        oos = C.run(path, "oos", 268, d, train_result=trp, skip_time_check=True)
        self.assertEqual(oos["segment"], "oos")
        self.assertTrue(os.path.exists(os.path.join(d, "c4_oos.lock")))
        with self.assertRaises(SystemExit):
            C.run(path, "oos", 268, d, train_result=trp, skip_time_check=True)

    def test_oos_refuses_without_train_pass(self):
        d, path = self.make_many(40)
        C.run(path, "train", 268, d, skip_time_check=True)
        trp = os.path.join(d, "c4_train_result.json")
        j = json.load(open(trp))
        for v in j["exits"].values(): v["pass"] = False
        json.dump(j, open(trp, "w"))
        with self.assertRaises(SystemExit):
            C.run(path, "oos", 268, d, train_result=trp, skip_time_check=True)
        self.assertFalse(os.path.exists(os.path.join(d, "c4_oos.lock")))

    def test_split_straddles_fixed_cut_and_reports_gap(self):
        d, path = self.make_many(80, t0=C.cut_ts() - 100 * STEP)
        tr = C.run(path, "train", 268, d, skip_time_check=True)
        self.assertEqual(tr["cut_iso"], "2026-10-22T18:15:12+00:00")
        self.assertIn("entry_gap", tr); self.assertEqual(tr["warnings"], [])
        self.assertIn("sl_exit_gap", tr); self.assertIn("random", tr["sl_exit_gap"])
        self.assertIn("no_valid_post_price", tr); self.assertFalse(tr["no_valid_post_price"]["c4"]["warning"])
        self.assertEqual(tr["entry_gap"]["c4"]["invalid_gap"], 0)
        self.assertGreater(tr["n_c4"], 0); self.assertLess(tr["n_c4"], 80)

    def test_time60_cannot_open_oos(self):
        d, path = self.make_many(80)
        tr = C.run(path, "train", 268, d, skip_time_check=True)
        self.assertEqual(tr["primary_exit"], "tp10_sl5_h60")
        self.assertFalse(tr["exits"]["time60"]["pass"])
        self.assertTrue(tr["exits"]["time60"]["verdict"].startswith("COMPARISON_ONLY"))
        trp = os.path.join(d, "c4_train_result.json")
        j = json.load(open(trp)); j["exits"]["time60"]["pass"] = True; j["exits"]["tp10_sl5_h60"]["pass"] = False
        json.dump(j, open(trp, "w"))
        with self.assertRaises(SystemExit):
            C.run(path, "oos", 268, d, train_result=trp, skip_time_check=True)
        self.assertFalse(os.path.exists(os.path.join(d, "c4_oos.lock")))

    def test_invalid_small_sample(self):
        d, path = self.make_many(10)
        r = C.run(path, "train", 268, d, skip_time_check=True)
        self.assertFalse(r["valid_c4"])
        for v in r["exits"].values(): self.assertFalse(v["pass"])


if __name__ == "__main__":
    unittest.main()
