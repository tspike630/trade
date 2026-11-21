import contextlib,hashlib,hmac,io,json,os,socket,sqlite3,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import aave_readonly_candidate as m
import synthetic_harness as harness

class Response:
    def __init__(self,body,status=200,headers=None):
        self.data=json.dumps(body).encode();self.status=status;self.headers=headers or {};self.at=0
    def getheader(self,k,default=None):return self.headers.get(k,default)
    def read(self,n):part=self.data[self.at:self.at+n];self.at+=len(part);return part
class FakeHTTP:
    def __init__(self,responses):self.responses=list(responses);self.calls=[];self.connections=[]
    def __call__(self,host,port,**kw):
        self.connections.append((host,port,kw));owner=self
        class C:
            def request(self,method,path,headers):owner.calls.append((method,path,headers))
            def getresponse(self):
                item=owner.responses.pop(0)
                if isinstance(item,Exception):raise item
                return item
            def close(self):pass
        return C()
def uid(value=m.UID):return Response({'uid':value,'secret':'NEVER_EXPORT'})
def config():return {'approval_reference':'SYNTHETIC_APPROVAL','expected_uid':m.UID,'api_key_env':'SYNTHETIC_KEY_ENV','api_secret_env':'SYNTHETIC_SECRET_ENV','credential_scheme':'HMAC_SHA256','start_time_ms':1000,'end_time_ms':2000,'execute_account_reads':True,'output_dir':str(m.BASE/'evidence-SYNTHETIC'),'approved_client_keys':[],'approved_order_ids':[],'approved_oco_ids':[]}
class V2(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='t582-');self.root=Path(self.tmp.name);self.db=self.root/'fixture.db'
        self.net=patch.object(socket.socket,'connect',side_effect=AssertionError('real network forbidden'));self.net.start()
    def tearDown(self):self.net.stop();self.tmp.cleanup()
    def fixture(self,reason=False,index=True):
        c=sqlite3.connect(self.db)
        c.execute('CREATE TABLE intents(intent_id TEXT PRIMARY KEY,state TEXT,created_ms INTEGER,updated_ms INTEGER,payload_json TEXT'+(',reason TEXT' if reason else '')+')')
        vals=[m.IID,'protection_blocked',1000,2000,json.dumps({'intent':{'symbol':'AAVEUSDT','action':'OPEN_SHORT','base_asset':'AAVE','quote_asset':'USDT'},'secret':'HIDDEN'})]
        if reason:vals+=['interest_rate_mismatch']
        c.execute('INSERT INTO intents VALUES('+','.join('?'*len(vals))+')',vals)
        c.execute('CREATE TABLE operations(intent_id TEXT,op_type TEXT,client_key TEXT,state TEXT,response_json TEXT)')
        c.execute('INSERT INTO operations VALUES(?,?,?,?,?)',(m.IID,'entry','LEDGER-ENTRY','confirmed','{"orderId":123,"signature":"HIDDEN"}'))
        if index:c.execute('CREATE INDEX op_idx ON operations(intent_id)')
        c.commit();c.close()
    def read_fixture(self):
        with patch.object(m,'check_official_path',return_value=self.db),patch.object(m,'_launch_pinned',side_effect=harness.launch):return m.ledger_read(m.OFFICIAL,mountinfo='1 0 1:1 / / ro - ext4 x rw\n')
    def test_01_plan_zero_env_db_network(self):
        with patch.object(os,'getenv',side_effect=AssertionError()),patch.dict(os.environ,{},clear=True),patch.object(sqlite3,'connect',side_effect=AssertionError()):
            out=io.StringIO()
            with contextlib.redirect_stdout(out):self.assertEqual(m.main([]),0)
        self.assertEqual(json.loads(out.getvalue())['mode'],'PLAN_ONLY')
    def test_02_config_without_execute_does_not_read_config(self):
        with patch.object(Path,'read_text',side_effect=AssertionError()):
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(m.main(['--approved-config','/anything']),2)
    def test_03_templates_fail_closed(self):
        c=config();self.assertEqual(m.config_check(c,now_ms=3000)['expected_uid'],m.UID)
        c['api_key_env']=None
        with self.assertRaises(m.Failure):m.config_check(c,now_ms=3000)
    def test_04_only_get_and_exact_paths(self):
        for method,path in [('POST',m.PREFIX+'order'),('DELETE',m.PREFIX+'order'),('GET','https://evil.example'),('GET',m.PREFIX+'loan'),('GET',m.PREFIX+'repay')]:
            with self.assertRaises(m.Failure):m.validate_request(path,{},method)
    def test_05_hmac_and_no_secret_in_exception(self):
        q=m.sign_query({'a':'1'},'synthetic-secret',1234);raw,sig=q.split('&signature=')
        self.assertEqual(sig,hmac.new(b'synthetic-secret',raw.encode(),hashlib.sha256).hexdigest())
        self.assertEqual(m.controlled_error(RuntimeError('signature=SECRET Authorization SECRET')),'operation_failed_details_suppressed')
    def test_06_fixed_host_get_tls_and_proxy_not_used(self):
        fake=FakeHTTP([uid()])
        with patch.dict(os.environ,{'HTTPS_PROXY':'http://evil.example'}):r=m.https_get(m.IDENTITY,{},'fake-key','fake-secret',connection_factory=fake)
        self.assertEqual(r,{'uid_verified':True});self.assertEqual(fake.connections[0][:2],('api.binance.com',443));self.assertEqual(fake.calls[0][0],'GET')
        self.assertNotIn('fake-key',json.dumps(r));self.assertTrue(fake.calls[0][1].startswith('/api/v3/account?'))
    def test_07_uid_missing_stops_worker_before_other_call(self):
        fake=FakeHTTP([Response({})]);job={'endpoint':m.ACCOUNT,'params':{},'credentials':{'key':'fake-key','secret':'fake-secret'}}
        with self.assertRaises(m.Failure):m.api_job(job,connection_factory=fake)
        self.assertEqual(len(fake.calls),1)
    def test_08_uid_wrong_stops_worker(self):
        fake=FakeHTTP([uid('1289917075')]);job={'endpoint':m.ACCOUNT,'params':{},'credentials':{'key':'fake-key','secret':'fake-secret'}}
        with self.assertRaises(m.Failure):m.api_job(job,connection_factory=fake)
        self.assertEqual(len(fake.calls),1)
    def test_09_worker_uid_first_then_minimal_balance(self):
        fake=FakeHTTP([uid(),Response({'marginLevel':'2','accountType':'MARGIN_1','userAssets':[{'asset':'AAVE','borrowed':'2','free':'1','locked':'0','interest':'0','netAsset':'-1','secret':'HIDDEN'},{'asset':'OTHER','free':'999','locked':'0','borrowed':'0','interest':'0','netAsset':'999'}]})])
        r=m.api_job({'endpoint':m.ACCOUNT,'params':{},'credentials':{'key':'fake-key','secret':'fake-secret'}},connection_factory=fake)
        self.assertEqual(len(fake.calls),2);self.assertEqual(r['assets'][0]['borrowed'],'2');self.assertNotIn('HIDDEN',json.dumps(r));self.assertEqual(len(r['assets']),1)
    def test_10_redirect_and_rate_stop(self):
        for status in (301,302,307,308,418,429):
            fake=FakeHTTP([Response({},status)])
            with self.assertRaises(m.Failure):m.https_get(m.IDENTITY,{},'fake-key','fake-secret',connection_factory=fake)
            self.assertEqual(len(fake.calls),1)
    def test_11_streaming_size_cap(self):
        response=Response({});response.data=b' '*(m.CAP+1);fake=FakeHTTP([response])
        with self.assertRaises(m.Failure):m.https_get(m.IDENTITY,{},'fake-key','fake-secret',connection_factory=fake)
        self.assertLessEqual(response.at,m.CAP+1)
    def test_12_length_and_compression_refused(self):
        for headers in ({'Content-Length':str(m.CAP+1)},{'Content-Encoding':'gzip'}):
            with self.assertRaises(m.Failure):m.https_get(m.IDENTITY,{},'fake-key','fake-secret',connection_factory=FakeHTTP([Response({},headers=headers)]))
    def test_13_hung_network_action_hard_killed(self):
        # Synthetic child only. Represents DNS/TLS/HTTP hang; no socket is opened.
        command=[sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)']
        start=time.monotonic()
        with self.assertRaises(m.Failure):m.bounded_child(command,{},seconds=.15)
        self.assertLess(time.monotonic()-start,2)
    def test_14_session_failure_stops_all_followup(self):
        calls=[]
        def invoke(p,q):calls.append(p);raise m.Failure('uid_missing_or_mismatched')
        s=m.Session(invoke)
        with self.assertRaises(m.Failure):s.get(m.IDENTITY)
        with self.assertRaises(m.Failure):s.get(m.ACCOUNT)
        self.assertEqual(calls,[m.IDENTITY])
    def test_15_history_paging_discloses_cap(self):
        class S:
            def __init__(self):self.calls=[]
            def get(self,p,q):self.calls.append(q);return {'rows':[{'asset':'AAVE'}]*100,'received_count':100,'excluded_outside_scope':0,'total':400}
        s=S();r=m.history_pages(s,m.PREFIX+'interestHistory',{'asset':'AAVE','startTime':'1000','endTime':'2000'})
        self.assertEqual(len(s.calls),3);self.assertEqual(r['coverage'],'partial');self.assertTrue(r['truncated_or_total_unconfirmed'])
    def test_16_history_total_missing_never_complete(self):
        class S:
            def get(self,p,q):return {'rows':[],'received_count':0,'excluded_outside_scope':0,'total':None}
        self.assertEqual(m.history_pages(S(),m.PREFIX+'borrow-repay',{})['coverage'],'partial')
    def test_17_history_small_known_total_bounded_complete(self):
        class S:
            def get(self,p,q):return {'rows':[{}],'received_count':1,'excluded_outside_scope':0,'total':1}
        self.assertEqual(m.history_pages(S(),m.PREFIX+'borrow-repay',{})['coverage'],'bounded_window_complete')
    def test_18_mount_ro_not_dac_and_child_mount_refused(self):
        good='1 0 1:1 / / ro - ext4 root rw\n'
        m.assert_readonly_mounts(good,m.OFFICIAL)
        with self.assertRaises(m.Failure):m.assert_readonly_mounts(good.replace(' / ro ',' / rw '),m.OFFICIAL)
        with self.assertRaises(m.Failure):m.assert_readonly_mounts(good+'2 1 1:2 / /home/box/.config/trade-secrets rw - ext4 x rw\n',m.OFFICIAL)
    def test_19_other_business_rw_submount_refused(self):
        with self.assertRaises(m.Failure):m.assert_readonly_mounts('1 0 1:1 / / ro - ext4 x rw\n2 1 1:2 / /workspace rw - ext4 x rw\n',m.OFFICIAL)
    def test_20_outer_and_main_paths_refused(self):
        for p in ('/workspace/dexscreener-crawler/dexscreener.db','/workspace/dexscreener-crawler/execution_ledger_live_cross_margin.db'):
            with self.assertRaises(m.Failure):m.check_official_path(p)
    def test_21_schema_variant_missing_reason_explicit(self):
        self.fixture();r=self.read_fixture();self.assertIn('reason',r['reason_missing']['intents']);self.assertEqual(r['targets']['client_keys'],['LEDGER-ENTRY']);self.assertEqual(r['targets']['order_ids'],['123']);self.assertNotIn('HIDDEN',json.dumps(r))
    def test_22_schema_variant_known_reason_extract(self):
        self.fixture(reason=True);r=self.read_fixture();self.assertEqual(r['intent']['known_reason_codes'],{'reason':'interest_rate_mismatch'})
    def test_23_no_index_refused(self):
        self.fixture(index=False)
        with self.assertRaises((m.Failure,m.core.Refused)):self.read_fixture()
    def test_24_wal_failure_no_fallback(self):
        self.fixture();pins=m.pin.Pins(self.db)
        try:r=harness.native_read(pins,inject_failure=True)
        finally:pins.close()
        self.assertFalse(r['ok']);self.assertEqual(r['connect_calls'],1);self.assertNotIn('immutable',r['uris'][0]);self.assertEqual(r['failure'],'sqlite_read_failed_no_fallback')
    def test_25_readonly_sqlite_rejects_write(self):
        self.fixture();c=sqlite3.connect(self.db.as_uri()+'?mode=ro',uri=True)
        with self.assertRaises(sqlite3.OperationalError):c.execute("UPDATE intents SET state='clean_flat'")
        c.close()
    def test_26_orderlist_cross_no_symbol_allowed(self):
        m.validate_request(m.PREFIX+'orderList',{'isIsolated':'FALSE','orderListId':'123'})
        with self.assertRaises(m.Failure):m.validate_request(m.PREFIX+'orderList',{'symbol':'AAVEUSDT','isIsolated':'FALSE','orderListId':'123'})
    def test_27_keep_fees_times_and_drop_secret(self):
        r=m.sanitize(m.PREFIX+'myTrades',[{'symbol':'AAVEUSDT','orderId':123,'id':1,'price':'150','isBuyer':True,'isMaker':False,'qty':'2.50','commission':'0.01','commissionAsset':'BNB','time':12345,'signature':'SECRET','headers':{'Authorization':'SECRET'}}])
        self.assertEqual(r['rows'][0]['commission'],'0.01');self.assertEqual(r['rows'][0]['time'],'12345');self.assertNotIn('SECRET',json.dumps(r))
    def test_28_isolated_history_not_mislabelled_cross(self):
        r=m.sanitize(m.PREFIX+'interestHistory',{'rows':[{'asset':'AAVE','isolatedSymbol':'AAVEUSDT','interest':'1','principal':'100','interestRate':'0.01','type':'PERIODIC','interestAccuredTime':12345}],'total':1})
        self.assertEqual(r['rows'],[]);self.assertEqual(r['excluded_outside_scope'],1)
    def test_29_approved_output_scope_and_no_overwrite(self):
        c=config();c['output_dir']='/opt/evidence-something'
        with self.assertRaises(m.Failure):m.config_check(c,now_ms=3000)
    def test_30_worker_command_uses_ro_parent_net_split(self):
        db=m.worker_command('db');api=m.worker_command('api')
        self.assertIn('--unshare-all',db);self.assertNotIn('--share-net',db);self.assertIn('--share-net',api);self.assertIn('--ro-bind',db)
        self.assertEqual(db[db.index('--ro-bind')+1:db.index('--ro-bind')+3],['/','/'])
    def test_31_wallet_uid_not_assumed(self):
        with self.assertRaises(m.Failure):m.validate_request('/sapi/v1/account/info',{})
        text=(Path(__file__).parent/'docs/binance-official-rest-api.md').read_text();start=text.index('### Account information (USER_DATA)');self.assertIn('"uid":',text[start:start+6500])
    def test_32_no_follow_response_urls_or_new_order_ids(self):
        r=m.sanitize(m.PREFIX+'order',{'symbol':'AAVEUSDT','orderId':456,'clientOrderId':'APPROVED','side':'SELL','origQty':'1','executedQty':'1','price':'150','time':12345,'url':'https://evil','instructions':'SECRET','status':'FILLED'})
        self.assertNotIn('url',json.dumps(r));self.assertNotIn('SECRET',json.dumps(r))
    def test_33_array_full_page_stays_partial(self):
        results={m.IDENTITY:{'uid_verified':True},m.ACCOUNT:{'assets':[]}}
        class S:
            def get(self,p,q=None):
                if p in results:return results[p]
                if p.endswith(('borrow-repay','interestHistory')):return {'rows':[],'received_count':0,'excluded_outside_scope':0,'total':0}
                return {'rows':[],'received_count':100,'excluded_outside_scope':0,'total':None}
        r=m.account_evidence(S(),config(),{'targets':{'client_keys':[],'order_ids':[],'oco_ids':[],'oco_keys':[]}})
        self.assertEqual(r['myTrades']['coverage'],'partial');self.assertTrue(r['allOrders']['truncated_or_pagination_uncertain'])
    def test_34_unknown_reason_presence_not_guessed(self):
        self.fixture(reason=True);c=sqlite3.connect(self.db);c.execute("UPDATE intents SET reason='signature=SECRET'");c.commit();c.close()
        r=self.read_fixture();self.assertTrue(r['intent']['unknown_reason_present']);self.assertNotIn('SECRET',json.dumps(r))
    def test_35_result_preserves_partial_account_before_failure(self):
        part={}
        class S:
            def get(self,p,q=None):
                if p==m.IDENTITY:return {'uid_verified':True}
                if p==m.ACCOUNT:return {'assets':[]}
                raise m.Failure('rate_limit_stop')
        with self.assertRaises(m.Failure):m.account_evidence(S(),config(),{'targets':{}},part)
        self.assertTrue(part['uid_verified']);self.assertIn('account',part)
    def kernel_child(self,code):
        code=code.replace('/ro/fixture.db',str(self.db))
        command=['bwrap','--unshare-all','--ro-bind','/','/','--ro-bind',str(self.root),str(self.root),'--proc','/proc','--dev','/dev','/usr/bin/python3','-B','-c',code]
        return subprocess.run(command,capture_output=True,timeout=3)
    def test_36_kernel_ro_mount_reads_clean_fixture_denies_write(self):
        self.fixture();pins=m.pin.Pins(self.db)
        try:r=harness.native_read(pins,write_probe=True)
        finally:pins.close()
        self.assertTrue(r['ok'],r);self.assertTrue(r['kernel_write_denied']);self.assertTrue(r['production_paths_absent']);self.assertEqual(r['result']['intent']['intent_id'],m.IID)
    def test_37_nonempty_wal_no_shm_on_kernel_ro_fails_no_fallback(self):
        self.fixture();c=sqlite3.connect(self.db);c.execute('PRAGMA journal_mode=WAL');c.execute('INSERT INTO operations VALUES(?,?,?,?,?)',(m.IID,'entry','SECOND','confirmed','{}'));c.commit()
        Path(str(self.db)+'-shm').unlink();pins=m.pin.Pins(self.db)
        try:r=harness.native_read(pins)
        finally:pins.close();c.close()
        self.assertFalse(r['ok'],r);self.assertEqual(r['connect_calls'],1);self.assertNotIn('immutable',r['uris'][0]);self.assertEqual(r['failure'],'sqlite_read_failed_no_fallback')
    def test_38_approved_config_digest_pins_no_secret_names_swap(self):
        p=self.root/'config.json';p.write_text(json.dumps(config()));digest=hashlib.sha256(p.read_bytes()).hexdigest()
        with patch.object(m,'BASE',self.root):
            self.assertEqual(m.load_approved_config(p,digest)['expected_uid'],m.UID)
            p.write_text(p.read_text().replace('SYNTHETIC_KEY_ENV','UNAPPROVED_ENV'))
            with self.assertRaises(m.Failure):m.load_approved_config(p,digest)
    def test_39_schema_missing_required_is_explicit_not_guessed(self):
        self.fixture();c=sqlite3.connect(self.db);c.execute('DROP TABLE operations');c.execute('CREATE TABLE operations(intent_id TEXT,state TEXT)');c.commit();c.close()
        try:self.read_fixture()
        except Exception as ex:self.assertEqual(m.controlled_error(ex),'required_columns_missing')
        else:self.fail('must refuse schema')

    def test_40_unexpected_history_scope_cannot_be_complete(self):
        class S:
            def get(self,p,q):return {'rows':[],'received_count':1,'excluded_outside_scope':1,'total':1}
        self.assertEqual(m.history_pages(S(),m.PREFIX+'interestHistory',{})['coverage'],'partial')
        with self.assertRaises(m.Failure):m.sanitize(m.PREFIX+'order',{'symbol':'OTHERUSDT','orderId':1})
        with self.assertRaises(m.Failure):m.sanitize(m.PREFIX+'borrow-repay',{'code':-1})

if __name__=='__main__':unittest.main(verbosity=2)
