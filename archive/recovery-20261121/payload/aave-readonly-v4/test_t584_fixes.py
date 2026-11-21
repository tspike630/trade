import json,os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import aave_readonly_candidate as m
import test_candidate_v3_regression as old
import synthetic_harness as harness

def order(symbol='AAVEUSDT'):
    return {'symbol':symbol,'orderId':123,'clientOrderId':'SYNTH_ENTRY','status':'FILLED','side':'SELL','origQty':'1','executedQty':'1','price':'150','time':12345}
def oco(symbol='AAVEUSDT'):
    return {'symbol':symbol,'orderListId':22,'listClientOrderId':'SYNTH_OCO','listStatusType':'ALL_DONE','listOrderStatus':'ALL_DONE','transactionTime':12345,'orders':[{'symbol':symbol,'orderId':123,'clientOrderId':'SYNTH_EXIT'}]}
def trade():return {'symbol':'AAVEUSDT','orderId':123,'id':99,'qty':'1','price':'150','commission':'0.01','commissionAsset':'BNB','time':12345,'isBuyer':False,'isMaker':True}
def loan():return {'asset':'AAVE','txId':1,'timestamp':12345,'amount':'1','status':'CONFIRMED','type':'MANUAL'}
def interest():return {'asset':'AAVE','type':'ON_BORROW','interestAccuredTime':12345,'principal':'1','interest':'0.001','interestRate':'0.001'}
def balance():return {'accountType':'MARGIN_1','marginLevel':'2','userAssets':[{'asset':'AAVE','free':'0','locked':'0','borrowed':'1','interest':'0.001','netAsset':'-1.001'}]}
def targets():return {'targets':{'client_keys':[],'order_ids':[],'oco_keys':[],'oco_ids':[]}}

class F1(unittest.TestCase):
    def stop(self,leaf,body):
        endpoint=m.PREFIX+leaf;fake=old.FakeHTTP([old.Response(body)])
        params={'isIsolated':'FALSE','symbol':'AAVEUSDT'}
        if leaf in {'allOrderList','orderList'}:params.pop('symbol')
        if leaf=='order':params['orderId']='123'
        if leaf=='orderList':params['orderListId']='22'
        s=m.Session(lambda p,q:m.https_get(p,q,'SYNTH_KEY','SYNTH_SECRET',connection_factory=fake));s.verified=True
        with self.assertRaises(m.Failure):s.get(endpoint,params)
        with self.assertRaises(m.Failure):s.get(m.ACCOUNT)
        self.assertTrue(s.stopped);self.assertEqual(len(fake.calls),1);self.assertEqual(s.audit[0]['status'],'stopped')
    def test_01_unknown_array_object_stops_zero_followup(self):
        for leaf in ('openOrders','allOrders','myTrades','allOrderList'):
            with self.subTest(leaf=leaf):self.stop(leaf,{'unexpected':'SYNTHETIC_SCHEMA_CHANGE'})
    def test_02_http_twohundred_error_stops_zero_followup(self):
        for leaf in ('openOrders','allOrders','myTrades','allOrderList','order','orderList'):
            with self.subTest(leaf=leaf):self.stop(leaf,{'code':-2015,'msg':'SYNTHETIC_SECRET_BAD_KEY'})
    def test_03_outside_scope_must_still_have_valid_shape(self):
        for leaf in ('openOrders','allOrders','myTrades','allOrderList'):
            with self.subTest(leaf=leaf):self.stop(leaf,[{'symbol':'OTHERUSDT'}])
    def test_04_malformed_rows_in_valid_array(self):
        for row in ({},None,'bad',{'symbol':'AAVEUSDT','orderId':123}):
            with self.subTest(row=row):self.stop('openOrders',[row])
    def test_05_legitimate_empty_arrays_are_empty_not_error(self):
        for leaf in ('openOrders','allOrders','myTrades','allOrderList'):
            self.assertEqual(m.sanitize(m.PREFIX+leaf,[])['rows'],[])
    def test_06_valid_oco_and_outside_filter(self):
        r=m.sanitize(m.PREFIX+'allOrderList',[oco(),oco('OTHERUSDT')]);self.assertEqual(len(r['rows']),1);self.assertEqual(r['excluded_outside_scope'],1);self.assertEqual(r['rows'][0]['orderListId'],'22')
    def test_07_missing_or_wrong_order_field_refused(self):
        for key,value in [('status',None),('origQty','not_a_number'),('side','UNKNOWN'),('time',True)]:
            row=order();row[key]=value
            with self.assertRaises(m.Failure):m.sanitize(m.PREFIX+'order',row)
    def test_08_incomplete_balance_not_silent_null(self):
        b=balance();del b['userAssets'][0]['borrowed']
        with self.assertRaises(m.Failure):m.sanitize(m.ACCOUNT,b)
        with self.assertRaises(m.Failure):m.sanitize(m.ACCOUNT,{'code':-1,'userAssets':[]})
    def test_09_unknown_history_and_bad_total_refused(self):
        for b in ({'unexpected':[]},{'rows':[{}],'total':1},{'rows':[],'total':'bad'},{'code':0,'rows':[],'total':0}):
            with self.assertRaises(m.Failure):m.sanitize(m.PREFIX+'interestHistory',b)
    def test_10_necessary_fields_kept_secret_fields_dropped(self):
        t=trade();t.update({'signature':'SYNTH_SECRET','headers':{'Authorization':'SYNTH_SECRET'},'url':'https://evil'})
        r=m.sanitize(m.PREFIX+'myTrades',[t]);self.assertEqual(r['rows'][0]['commission'],'0.01');self.assertEqual(r['rows'][0]['orderId'],'123');self.assertEqual(r['rows'][0]['time'],'12345');self.assertNotIn('SYNTH_SECRET',json.dumps(r));self.assertNotIn('https',json.dumps(r))
    def test_11_real_session_orchestration_stops_at_first_shape_failure(self):
        fake=old.FakeHTTP([old.uid(),old.Response(balance()),old.Response({'unknown':True})]);s=m.Session(lambda p,q:m.https_get(p,q,'SYNTH_KEY','SYNTH_SECRET',connection_factory=fake));out={}
        with self.assertRaises(m.Failure):m.account_evidence(s,old.config(),targets(),out)
        self.assertEqual(len(fake.calls),3);self.assertTrue(out['uid_verified']);self.assertIn('account',out);self.assertNotIn('allOrders',out)
    def test_12_server_exceeding_requested_page_cap_refused(self):
        with self.assertRaises(m.Failure):m.sanitize(m.PREFIX+'allOrders',[order(),order()],{'limit':'1'})
        with self.assertRaises(m.Failure):m.sanitize(m.PREFIX+'borrow-repay',{'rows':[loan()]*101,'total':101},{'size':'100'})

class F2(unittest.TestCase):
    def scenario(self,which,page_failure=2):
        calls=[]
        class S:
            def get(self,p,q=None):
                calls.append((p,dict(q or {})))
                if p==m.IDENTITY:return {'uid_verified':True}
                if p==m.ACCOUNT:return {'assets':[]}
                if p.endswith(('borrow-repay','interestHistory')):
                    name=q.get('type','interestHistory');page=int(q['current'])
                    if name==which:
                        if page==page_failure:raise m.Failure('rate_limit_stop')
                        return {'rows':[{'asset':'AAVE','txId':str(page*100+i)} for i in range(100)],'received_count':100,'excluded_outside_scope':0,'total':250}
                    return {'rows':[],'received_count':0,'excluded_outside_scope':0,'total':0}
                return {'rows':[],'received_count':0,'excluded_outside_scope':0,'total':None}
        out={}
        with self.assertRaises(m.Failure):m.account_evidence(S(),old.config(),targets(),out)
        r=out[which];self.assertEqual(r['coverage'],'partial');self.assertEqual(len(r['rows']),100*(page_failure-1));self.assertEqual(r['rows_retained'],100*(page_failure-1));self.assertEqual(r['successful_pages'],page_failure-1);self.assertEqual(r['failed_page'],page_failure);self.assertEqual(r['error'],'rate_limit_stop');self.assertEqual(r['pages'][-1]['status'],'failed')
        self.assertEqual(calls[-1][1].get('type','interestHistory'),which);self.assertEqual(int(calls[-1][1]['current']),page_failure)
        return out
    def test_13_borrow_second_page_preserved(self):self.assertNotIn('REPAY',self.scenario('BORROW'))
    def test_14_repay_second_page_preserved(self):self.assertIn('BORROW',self.scenario('REPAY'))
    def test_15_interest_third_page_preserved(self):self.assertIn('REPAY',self.scenario('interestHistory',3))
    def test_16_bad_http_page_preserves_earlier_and_stops_session(self):
        fake=old.FakeHTTP([old.Response({'rows':[loan()]*100,'total':150}),old.Response({'code':-1,'msg':'SYNTH_SECRET'})]);s=m.Session(lambda p,q:m.https_get(p,q,'SYNTH_KEY','SYNTH_SECRET',connection_factory=fake));s.verified=True;out={}
        with self.assertRaises(m.Failure):m.history_pages(s,m.PREFIX+'borrow-repay',{'asset':'AAVE','type':'BORROW','startTime':'1000','endTime':'2000'},out)
        with self.assertRaises(m.Failure):s.get(m.ACCOUNT)
        self.assertEqual(len(out['rows']),100);self.assertEqual(len(fake.calls),2);self.assertTrue(s.stopped);self.assertNotIn('SYNTH_SECRET',json.dumps(out))
    def test_17_first_page_failure_explicit_empty_partial(self):self.scenario('BORROW',1)
    def test_18_three_page_cap_not_full_claim(self):
        class S:
            def get(self,p,q):return {'rows':[{'asset':'AAVE'}]*100,'received_count':100,'excluded_outside_scope':0,'total':400}
        r=m.history_pages(S(),m.PREFIX+'interestHistory',{});self.assertEqual(len(r['rows']),300);self.assertEqual(r['coverage'],'partial');self.assertTrue(r['truncated_or_total_unconfirmed'])

class F3(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='t584-source-');self.root=Path(self.tmp.name);self.db=self.root/'fixture.db';self.keep=[];old.V2.fixture(self)
        self.other=self.root/'foreign.db';c=sqlite3.connect(self.other);c.execute('CREATE TABLE intents(intent_id TEXT PRIMARY KEY,state TEXT)');c.execute('INSERT INTO intents VALUES(?,?)',(m.IID,'synthetic_unapproved_target'));c.commit();c.close()
    def tearDown(self):
        for c in self.keep:c.close()
        self.tmp.cleanup()
    def native(self,action=None):
        pins=m.pin.Pins(self.db)
        try:return harness.native_read(pins,action=action)
        finally:pins.close()
    def test_19_original_symlink_swap_at_sqlite_connect_closed(self):
        def action():self.db.rename(self.root/'held.db');self.db.symlink_to(self.other)
        r=self.native(action);self.assertEqual(r['opened_state'],'protection_blocked',r);self.assertFalse(r['ok'],r);self.assertEqual(r['failure'],'source_generation_changed')
    def test_20_regular_replacement_at_sqlite_connect_closed(self):
        def action():self.db.rename(self.root/'held.db');self.other.rename(self.db)
        r=self.native(action);self.assertEqual(r['opened_state'],'protection_blocked',r);self.assertFalse(r['ok'],r)
    def test_21_transient_path_aba_cannot_redirect_sqlite_io(self):
        def action():
            self.db.rename(self.root/'held.db');self.db.symlink_to(self.other);self.db.unlink();(self.root/'held.db').rename(self.db)
        r=self.native(action);self.assertTrue(r['ok'],r);self.assertEqual(r['opened_state'],'protection_blocked')
    def test_22_ancestor_swap_cannot_redirect_and_is_detected(self):
        directory=self.root/'directory';directory.mkdir();target=directory/'ledger.db';self.db.rename(target);pins=m.pin.Pins(target)
        def action():directory.rename(self.root/'old-directory');directory.mkdir();self.other.rename(target)
        try:
            r=harness.native_read(pins,action=action);self.assertEqual(r['opened_state'],'protection_blocked',r)
            with self.assertRaises((m.pin.PinError,OSError)):pins.check_names()
        finally:pins.close()
    def test_23_preexisting_sidecar_symlink_refused(self):
        Path(str(self.db)+'-wal').symlink_to(self.other)
        with self.assertRaises((m.pin.PinError,OSError)):m.pin.Pins(self.db)
    def test_24_sidecar_replacement_after_pin_never_followed(self):
        c=sqlite3.connect(self.db);self.keep.append(c);c.execute('PRAGMA journal_mode=WAL');c.execute("UPDATE intents SET state='wal_committed'");c.commit()
        def action():
            wal=Path(str(self.db)+'-wal');wal.rename(self.root/'held-wal');wal.symlink_to(self.other)
        r=self.native(action);self.assertFalse(r['ok'],r);self.assertNotEqual(r['opened_state'],'synthetic_unapproved_target');self.assertIn(r['failure'],{'source_generation_changed','sqlite_read_failed_no_fallback'})
    def test_25_new_wal_after_pin_not_hidden_as_success(self):
        def action():
            c=sqlite3.connect(self.db,check_same_thread=False);self.keep.append(c);c.execute('PRAGMA journal_mode=WAL');c.execute("UPDATE intents SET state='new_wal'");c.commit()
        r=self.native(action);self.assertFalse(r['ok'],r);self.assertNotEqual(r['opened_state'],'synthetic_unapproved_target')
    def test_26_existing_valid_wal_includes_committed_frames(self):
        c=sqlite3.connect(self.db);self.keep.append(c);c.execute('PRAGMA journal_mode=WAL');c.execute("UPDATE intents SET state='wal_committed'");c.execute('INSERT INTO operations VALUES(?,?,?,?,?)',(m.IID,'entry','SECOND','confirmed','{}'));c.commit()
        r=self.native();self.assertTrue(r['ok'],r);self.assertEqual(r['result']['intent']['state'],'wal_committed');self.assertEqual(len(r['result']['operations']),2);self.assertEqual(r['connect_calls'],1)
    def test_27_ancestor_symlink_and_hardlink_refused(self):
        alias=self.root/'alias';alias.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises((m.pin.PinError,OSError)):m.pin.Pins(alias/'fixture.db')
        os.link(self.db,self.root/'hardlink.db')
        with self.assertRaises((m.pin.PinError,OSError)):m.pin.Pins(self.db)
    def test_28_small_file_cap_and_special_sidecar_refused(self):
        large=self.root/'sparse.db'
        with large.open('wb') as f:f.truncate(17*1024*1024)
        with self.assertRaises(m.pin.PinError):m.pin.Pins(large)
        os.mkfifo(str(self.db)+'-shm')
        with self.assertRaises(m.pin.PinError):m.pin.Pins(self.db)
    def test_29_descriptor_fd_mounts_and_no_network(self):
        pins=m.pin.Pins(self.db)
        try:
            cmd=m.pin.command(pins,['TEST_ONLY']);self.assertIn('--ro-bind-fd',cmd);self.assertIn('--remount-ro',cmd);self.assertNotIn('--share-net',cmd);self.assertNotIn('immutable',json.dumps(cmd));self.assertIn(pins.files[''],pins.pass_fds())
        finally:pins.close()
    def test_30_object_binding_is_not_only_python_path_recheck(self):
        r=self.native();self.assertTrue(r['ok'],r);proof=r['result']['readonly_proof'];self.assertTrue(proof['descriptor_bound_private_view']);self.assertFalse(proof['native_syscalls_traced']);self.assertFalse(proof['immutable_fallback']);self.assertTrue(r['production_paths_absent'])

if __name__=='__main__':unittest.main(verbosity=2)
