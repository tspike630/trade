"""AUTHOR offline R1 regressions; no live transport or production worker."""
import copy,json,unittest
import aave_readonly_candidate as m
import test_candidate_v3_regression as old
from test_t584_fixes import oco,order,balance
OBS=[]

def params(leaf):
    p={'isIsolated':'FALSE'}
    if leaf=='orderList':p['orderListId']='22'
    return p

def containers(row):return [('orderList',row),('allOrderList',[row])]

class OCOConsistency(unittest.TestCase):
    def stopped(self,leaf,body):
        fake=old.FakeHTTP([old.Response(body)])
        s=m.Session(lambda p,q:m.https_get(p,q,'SYNTHETIC_KEY','SYNTHETIC_SECRET',connection_factory=fake));s.verified=True
        caught=None
        try:s.get(m.PREFIX+leaf,params(leaf))
        except m.Failure as ex:caught=m.controlled_error(ex)
        else:self.fail('inconsistent OCO accepted')
        with self.assertRaises(m.Failure):s.get(m.ACCOUNT)
        self.assertEqual(len(fake.calls),1);self.assertTrue(s.stopped);self.assertEqual(s.audit[0]['status'],'stopped')
        self.assertNotIn('SYNTHETIC_SECRET',json.dumps(s.audit))
        OBS.append({'endpoint':m.PREFIX+leaf,'result':'rejected','reason':caught,'stopped':s.stopped,'fake_requests':len(fake.calls),'subsequent_requests':0,'audit':s.audit})
    def test_01_exact_original_sanitize_counterexample(self):
        b={'symbol':'AAVEUSDT','orderListId':1,'listClientOrderId':'SYNTHETIC_OCO','listStatusType':'EXEC_STARTED','listOrderStatus':'EXECUTING','transactionTime':1,'orders':[{'symbol':'OTHERUSDT','orderId':1,'clientOrderId':'SYNTHETIC_LEG'}]}
        with self.assertRaisesRegex(m.Failure,'^oco_leg_symbol_conflict$'):m.sanitize(m.PREFIX+'orderList',b)
    def test_02_original_fakehttp_session_counterexample(self):
        b=oco();b['orders'][0]['symbol']='OTHERUSDT';self.stopped('orderList',b)
    def test_03_array_oco_session_same_guard(self):
        b=oco();b['orders'][0]['symbol']='OTHERUSDT';self.stopped('allOrderList',[b])
    def test_04_all_leg_positions_checked(self):
        for bad_index in (0,1,2):
            b=oco();b['orders']=[{'symbol':'AAVEUSDT','orderId':i+1,'clientOrderId':'LEG_'+str(i)} for i in range(3)];b['orders'][bad_index]['symbol']='OTHERUSDT'
            for leaf,body in containers(b):
                with self.subTest(index=bad_index,leaf=leaf):self.stopped(leaf,body)
    def test_05_invalid_outside_list_rejected_before_filter(self):
        b=oco('OTHERUSDT');b['orders'][0]['symbol']='THIRDUSDT';self.stopped('allOrderList',[oco(),b])
    def test_06_consistent_outside_list_preserves_exclusion(self):
        r=m.sanitize(m.PREFIX+'allOrderList',[oco('OTHERUSDT')]);self.assertEqual(r['rows'],[]);self.assertEqual(r['received_count'],1);self.assertEqual(r['excluded_outside_scope'],1)
    def test_07_valid_multileg_accepts_and_can_continue(self):
        b=oco();b['orders'].append({'symbol':'AAVEUSDT','orderId':124,'clientOrderId':'SECOND_LEG'});b['signature']='SYNTHETIC_SECRET';b['orders'][0]['Authorization']='SYNTHETIC_SECRET'
        for leaf,body in containers(b):
            fake=old.FakeHTTP([old.Response(body),old.Response(balance())]);s=m.Session(lambda p,q:m.https_get(p,q,'SYNTHETIC_KEY','SYNTHETIC_SECRET',connection_factory=fake));s.verified=True
            r=s.get(m.PREFIX+leaf,params(leaf));s.get(m.ACCOUNT)
            self.assertFalse(s.stopped);self.assertEqual(len(fake.calls),2);self.assertEqual([x['orderId'] for x in r['rows'][0]['orders']],['123','124']);self.assertNotIn('SYNTHETIC_SECRET',json.dumps(r));self.assertEqual(r['rows'][0]['symbol'],'AAVEUSDT')
            OBS.append({'endpoint':m.PREFIX+leaf,'result':'valid_accepted','stopped':s.stopped,'fake_requests':len(fake.calls),'sanitized':r})
    def test_08_optional_relationship_fields_matching_accepted(self):
        b=oco();b['isIsolated']=False;b['orders'][0].update(orderListId='22',isIsolated=False)
        for leaf,body in containers(b):
            r=m.sanitize(m.PREFIX+leaf,body);self.assertEqual(r['rows'][0]['orders'][0]['orderListId'],'22');self.assertIs(r['rows'][0]['orders'][0]['isIsolated'],False)
    def test_09_optional_list_id_conflict_stops(self):
        for value in (999,'999',None,True,'unconfirmed'):
            b=oco();b['orders'][0]['orderListId']=value
            for leaf,body in containers(b):
                with self.subTest(value=value,leaf=leaf):self.stopped(leaf,body)
    def test_10_optional_cross_scope_conflict_stops(self):
        for value in (True,'FALSE',None,0):
            b=oco();b['orders'][0]['isIsolated']=value
            for leaf,body in containers(b):
                with self.subTest(value=value,leaf=leaf):self.stopped(leaf,body)
    def test_11_grandchild_container_not_recursively_projected(self):
        b=oco();b['orders'][0]['orders']=[{'symbol':'OTHERUSDT','orderId':999,'clientOrderId':'FOREIGN'}]
        for leaf,body in containers(b):self.stopped(leaf,body)
    def test_12_unknown_order_reports_container_stops(self):
        for at_leg in (False,True):
            b=oco();target=b['orders'][0] if at_leg else b;target['orderReports']=[order('OTHERUSDT')]
            for leaf,body in containers(b):
                with self.subTest(at_leg=at_leg,leaf=leaf):self.stopped(leaf,body)
    def test_13_leg_symbol_unknown_missing_or_wrong_type_stops(self):
        for value in (None,True,1,{},''):
            b=oco();b['orders'][0]['symbol']=value
            for leaf,body in containers(b):
                with self.subTest(value=value,leaf=leaf):self.stopped(leaf,body)
        b=oco();del b['orders'][0]['symbol']
        with self.assertRaises(m.Failure):m.sanitize(m.PREFIX+'orderList',b)
    def test_14_non_oco_order_cannot_smuggle_nested_oco_container(self):
        for name in ('orders','orderReports'):
            b=order();b[name]=[{'symbol':'OTHERUSDT','orderId':999,'clientOrderId':'FOREIGN'}]
            for leaf,body in [('order',b),('allOrders',[b]),('openOrders',[b])]:
                with self.assertRaises(m.Failure):m.sanitize(m.PREFIX+leaf,body)
    def test_15_valid_mixed_scope_array_keeps_original_filter(self):
        r=m.sanitize(m.PREFIX+'allOrderList',[oco('OTHERUSDT'),oco()]);self.assertEqual(r['received_count'],2);self.assertEqual(r['excluded_outside_scope'],1);self.assertEqual(len(r['rows']),1);self.assertEqual(r['rows'][0]['orders'][0]['symbol'],'AAVEUSDT')
    def test_16_foreign_parent_cannot_claim_aave_child(self):
        b=oco('OTHERUSDT');b['orders'][0]['symbol']='AAVEUSDT'
        for leaf,body in containers(b):self.stopped(leaf,body)
    def test_17_input_not_normalized_or_rewritten(self):
        b=oco();saved=copy.deepcopy(b);m.sanitize(m.PREFIX+'orderList',b);self.assertEqual(b,saved)
        b['orders'][0]['symbol']='OTHERUSDT';saved=copy.deepcopy(b)
        with self.assertRaises(m.Failure):m.sanitize(m.PREFIX+'orderList',b)
        self.assertEqual(b,saved)
    def test_18_direct_outside_scope_still_refused(self):
        with self.assertRaisesRegex(m.Failure,'direct_order_scope_unverified'):m.sanitize(m.PREFIX+'orderList',oco('OTHERUSDT'))

if __name__=='__main__':unittest.main(verbosity=2)
