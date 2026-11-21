"""Offline synthetic tests only. Run inside network-unshared bwrap."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import aave_evidence_review as m

class Transport:
    def __init__(self,replies):self.replies=list(replies);self.calls=[]
    def get(self,origin,path,params,timeout):
        self.calls.append((origin,path,dict(params),timeout))
        x=self.replies.pop(0)
        if isinstance(x,Exception):raise x
        return x

def reply(body,status=200,**kw):return m.Reply(status,json.dumps(body).encode(),**kw)
GOOD=reply({'uid':1275399777,'apiKey':'MUST_NOT_EXPORT'})

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='t581-fixtures-');self.p=Path(self.tmp.name)/'fixture.db'
        # Every attempted network connection fails, in addition to outer netns isolation.
        self.no_net=patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden'));self.no_net.start()
    def tearDown(self):self.no_net.stop();self.tmp.cleanup()
    def db(self,op_index=True,op_schema=True):
        c=sqlite3.connect(self.p)
        c.execute('CREATE TABLE intents(intent_id TEXT PRIMARY KEY,state TEXT,updated_ms INTEGER,payload_json TEXT)')
        c.execute('INSERT INTO intents VALUES(?,?,?,?)',(m.INTENT,'protection_blocked',123,'{"secret":"HIDDEN"}'))
        if op_schema:
            c.execute('CREATE TABLE operations(intent_id TEXT,op_type TEXT,client_key TEXT,state TEXT,response_json TEXT)')
            c.execute('INSERT INTO operations VALUES(?,?,?,?,?)',(m.INTENT,'entry','SAFE-CLIENT','confirmed','{"orderId":123,"signature":"HIDDEN"}'))
            if op_index:c.execute('CREATE INDEX op_iid ON operations(intent_id)')
        c.commit();c.close()
    def client(self,replies):
        t=Transport(replies);return m.MockReviewClient(t,client_keys=('SAFE-CLIENT',),order_ids=('123',)),t
    def test_01_default_plan_no_open_network_or_credentials(self):
        with patch.object(sqlite3,'connect',side_effect=AssertionError()),patch.object(os,'getenv',side_effect=AssertionError()):
            out=io.StringIO()
            with contextlib.redirect_stdout(out):self.assertEqual(m.main([]),0)
        self.assertFalse(json.loads(out.getvalue())['live_execution'])
    def test_02_post_refused_before_call(self):
        for method in ('POST','PUT','PATCH','DELETE','HEAD','OPTIONS','get'):
            c,t=self.client([])
            with self.assertRaises(m.Refused):c.request(m.IDENTITY,method=method)
            self.assertEqual(t.calls,[])
    def test_03_unknown_write_endpoint_refused(self):
        c,t=self.client([])
        with self.assertRaises(m.Refused):c.request('/sapi/v1/margin/borrow-repay')
        self.assertEqual(t.calls,[])
    def test_04_custom_url_refused(self):
        c,t=self.client([])
        with self.assertRaises(m.Refused):c.request('https://evil.example/api/v3/account')
        self.assertEqual(t.calls,[])
    def test_05_uid_matching_then_account_minimal(self):
        c,t=self.client([GOOD,reply({'userAssets':[{'asset':'AAVE','free':'1','borrowed':'0.2','secret':'HIDDEN'},{'asset':'OTHER','free':'999'}],'headers':'HIDDEN'})])
        self.assertEqual(c.request(m.IDENTITY),{'uid_verified':True})
        result=c.request(m.ACCOUNT);self.assertEqual(result['assets'][0]['borrowed'],'0.2')
        self.assertNotIn('HIDDEN',json.dumps(result));self.assertEqual(len(result['assets']),1)
        self.assertTrue(all(x[0]==m.ORIGIN for x in t.calls))
    def test_06_uid_missing_then_zero_further_calls(self):
        c,t=self.client([reply({}),GOOD])
        with self.assertRaises(m.Refused):c.request(m.IDENTITY)
        with self.assertRaises(m.Refused):c.request(m.ACCOUNT)
        self.assertEqual(len(t.calls),1)
    def test_07_uid_mismatch_then_zero_further_calls(self):
        c,t=self.client([reply({'uid':1289917075}),GOOD])
        with self.assertRaises(m.Refused):c.request(m.IDENTITY)
        with self.assertRaises(m.Refused):c.request(m.ORDERS,{'symbol':'AAVEUSDT','isIsolated':'FALSE'})
        self.assertEqual(len(t.calls),1)
    def test_08_boolean_uid_refused(self):
        c,t=self.client([reply({'uid':True})])
        with self.assertRaises(m.Refused):c.request(m.IDENTITY)
    def test_09_redirect_status_refused(self):
        c,t=self.client([reply({},302)])
        with self.assertRaises(m.Refused):c.request(m.IDENTITY)
        self.assertEqual(len(t.calls),1)
    def test_10_silent_redirect_origin_refused(self):
        c,t=self.client([reply({'uid':m.UID},origin='https://evil.example')])
        with self.assertRaises(m.Refused):c.request(m.IDENTITY)
    def test_11_429_418_stop_without_retry(self):
        for status in (429,418):
            c,t=self.client([reply({},status),GOOD])
            with self.assertRaises(m.Refused):c.request(m.IDENTITY)
            with self.assertRaises(m.Refused):c.request(m.IDENTITY)
            self.assertEqual(len(t.calls),1)
    def test_12_sensitive_exception_and_body_not_exported(self):
        c,t=self.client([RuntimeError('https://evil/?signature=SECRET Authorization: SECRET')])
        try:c.request(m.IDENTITY)
        except m.Refused as ex:
            self.assertNotIn('SECRET',str(ex));self.assertNotIn('SECRET',json.dumps(m.safe_error(ex)))
        else:self.fail('expected refusal')
    def test_13_mock_action_budget_stop(self):
        now=[0.0]
        class Slow(Transport):
            def get(self,*a):now[0]=6.0;return GOOD
        t=Slow([]);c=m.MockReviewClient(t,clock=lambda:now[0])
        with self.assertRaises(m.Refused):c.request(m.IDENTITY)
        self.assertTrue(c.stopped)
    def test_14_response_size_cap(self):
        c,t=self.client([m.Reply(200,b' '*(m.MAX_BODY+1))])
        with self.assertRaises(m.Refused):c.request(m.IDENTITY)
    def test_15_order_must_use_ledger_client_key(self):
        c,t=self.client([GOOD]);c.request(m.IDENTITY)
        with self.assertRaises(m.Refused):c.request(m.ORDER,{'symbol':'AAVEUSDT','isIsolated':'FALSE'})
        self.assertEqual(len(t.calls),1)
    def test_16_trades_must_have_order_and_limit(self):
        c,t=self.client([GOOD]);c.request(m.IDENTITY)
        with self.assertRaises(m.Refused):c.request(m.TRADES,{'symbol':'AAVEUSDT','isIsolated':'FALSE','limit':'50'})
        self.assertEqual(len(t.calls),1)
    def test_17_filtered_order_read_does_not_infer_safety(self):
        c,t=self.client([GOOD,reply({'symbol':'AAVEUSDT','status':'FILLED','orderId':123,'signature':'HIDDEN'})]);c.request(m.IDENTITY)
        out=c.request(m.ORDER,{'symbol':'AAVEUSDT','isIsolated':'FALSE','origClientOrderId':'SAFE-CLIENT'})
        self.assertNotIn('HIDDEN',json.dumps(out));self.assertIn('no_safety_inference',out['coverage'])
    def test_18_synthetic_indexed_target_projection(self):
        self.db();out=m.read_synthetic_ledger(self.p)
        self.assertEqual(out['intent']['intent_id'],m.INTENT)
        self.assertEqual(out['operations'][0]['orderId'],'123')
        self.assertEqual(out['reason_columns_missing']['intents'],['reason','last_reason'])
        self.assertNotIn('HIDDEN',json.dumps(out))
    def test_19_missing_schema_refused(self):
        self.db(op_schema=False)
        with self.assertRaises(m.Refused):m.read_synthetic_ledger(self.p)
    def test_20_missing_index_refused(self):
        self.db(op_index=False)
        with self.assertRaises(m.Refused):m.read_synthetic_ledger(self.p)
    def test_21_production_paths_refused_without_open(self):
        with patch.object(sqlite3,'connect',side_effect=AssertionError('must not open')):
            for p in ('/home/box/.config/trade-secrets/cross_margin_ledger.db','/workspace/dexscreener-crawler/dexscreener.db','file:/tmp/fixture.db?mode=rw'):
                with self.assertRaises(m.Refused):m.read_synthetic_ledger(p)
    def test_22_wal_failure_no_immutable_or_second_connection(self):
        self.db()
        with patch.object(sqlite3,'connect',side_effect=sqlite3.OperationalError('readonly WAL open failed')) as connect:
            with self.assertRaises(sqlite3.OperationalError):m.read_synthetic_ledger(self.p)
        self.assertEqual(connect.call_count,1)
        self.assertIn('?mode=ro',connect.call_args.args[0]);self.assertNotIn('immutable',connect.call_args.args[0])
    def test_23_readonly_connection_blocks_sql_write(self):
        self.db();real=sqlite3.connect;uris=[]
        def wrapped(database,**kw):
            uris.append(database);c=real(database,**kw)
            with self.assertRaises(sqlite3.OperationalError):c.execute("UPDATE intents SET state='clean_flat'")
            return c
        with patch.object(sqlite3,'connect',side_effect=wrapped):m.read_synthetic_ledger(self.p)
        self.assertEqual(len(uris),1)
    def test_24_synthetic_subprocess_worker_hard_boundary(self):
        self.db();out=m.hard_bounded_process([sys.executable,'-B',m.__file__,'--synthetic-worker',str(self.p)])
        self.assertEqual(out['scope'],'SYNTHETIC_ONLY')
    def test_25_hard_timeout_term_then_kill(self):
        # Only our synthetic Python child; no real-process PID is addressed.
        command=[sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)']
        t=time.monotonic()
        with self.assertRaises(m.Refused):m.hard_bounded_process(command,seconds=0.15)
        self.assertLess(time.monotonic()-t,2)
    def test_26_symlink_and_hardlink_refused(self):
        self.db();other=Path(self.tmp.name)/'alias.db';os.link(self.p,other)
        with self.assertRaises(m.Refused):m.fixture_path(self.p)
        other.unlink();self.p.rename(other);self.p.symlink_to(other)
        with self.assertRaises(m.Refused):m.fixture_path(self.p)
    def test_27_reason_presence_not_unsanitized_reason(self):
        self.db();c=sqlite3.connect(self.p);c.execute('ALTER TABLE intents ADD COLUMN reason TEXT');c.execute("UPDATE intents SET reason='Authorization: HIDDEN'");c.commit();c.close()
        out=m.read_synthetic_ledger(self.p);self.assertTrue(out['intent']['reason_presence']['reason'])
        self.assertNotIn('HIDDEN',json.dumps(out));self.assertNotIn('reason',out['reason_columns_missing']['intents'])
    def test_28_row_cap_fail_not_truncate_silent(self):
        self.db();c=sqlite3.connect(self.p)
        c.executemany('INSERT INTO operations VALUES(?,?,?,?,?)',[(m.INTENT,'entry','k'+str(i),'confirmed','{}') for i in range(33)]);c.commit();c.close()
        with self.assertRaises(m.Refused):m.read_synthetic_ledger(self.p)
    def test_29_expired_budget_zero_transport_calls(self):
        c,t=self.client([]);c.deadline=0
        with self.assertRaises(m.Refused):c.request(m.IDENTITY)
        self.assertEqual(t.calls,[])
    def test_30_no_live_cli_or_secret_argument_echo(self):
        proc=subprocess.run([sys.executable,'-B',m.__file__,'--api-key=SECRET'],capture_output=True)
        self.assertEqual(proc.returncode,2);self.assertNotIn(b'SECRET',proc.stdout+proc.stderr)
    def test_31_no_transport_default(self):
        c=m.MockReviewClient()
        with self.assertRaises(m.Refused):c.request(m.IDENTITY)
        self.assertTrue(c.stopped)

    def test_32_only_static_reason_codes_exported(self):
        self.db();c=sqlite3.connect(self.p);c.execute('ALTER TABLE intents ADD COLUMN reason TEXT');c.execute("UPDATE intents SET reason='interest_rate_mismatch'");c.commit();c.close()
        out=m.read_synthetic_ledger(self.p)
        self.assertEqual(out['intent']['reason_codes'],{'reason':'interest_rate_mismatch'})
        self.assertIn('current_exposure',out['not_inferred'])

    def test_33_out_of_target_client_key_refused(self):
        c,t=self.client([GOOD]);c.request(m.IDENTITY)
        with self.assertRaises(m.Refused):c.request(m.ORDER,{'symbol':'AAVEUSDT','isIsolated':'FALSE','origClientOrderId':'UNAPPROVED'})
        self.assertEqual(len(t.calls),1)

if __name__=='__main__':unittest.main(verbosity=2)
