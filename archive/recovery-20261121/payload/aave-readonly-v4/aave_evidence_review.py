#!/usr/bin/env python3
"""T581 REVIEW SKELETON. NO live DB/API/credentials execution adapter exists.
Only plan output and explicitly synthetic, bounded SQLite fixture reads supported.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time

UID = '1275399777'
INTENT = 'dl-149741'
ORIGIN = 'https://api.binance.com'
# Candidates for document/identity-capability review, NOT verified live API coverage.
IDENTITY = '/api/v3/account'
ACCOUNT = '/sapi/v1/margin/account'
ORDERS = '/sapi/v1/margin/openOrders'
ORDER = '/sapi/v1/margin/order'
TRADES = '/sapi/v1/margin/myTrades'
ALLOW = {IDENTITY: frozenset(), ACCOUNT: frozenset(),
         ORDERS: frozenset({'symbol','isIsolated'}),
         ORDER: frozenset({'symbol','isIsolated','origClientOrderId'}),
         TRADES: frozenset({'symbol','isIsolated','orderId','limit'})}
MAX_BODY = 262144
MAX_OPS = 32
MAX_ACTION_SEC = 2.0
REASON_CODES = frozenset({
    'long_free_base_mismatch','protection_qty_off_step','interest_rate_mismatch',
    'interest_query_failed','same_asset_external_liability_conflict',
    'unattributed_interest_liability_conflict','interest_attribution_query_failed',
    'borrow_principal_unverified','base_asset_missing','asset_interest_rate_missing',
    'cover_exceeds_max_base_over_liability','stop_not_above_reference',
    'stop_not_below_reference','quote_cannot_fund_stop_buy',
    'buy_liquidity_insufficient','protection_not_placed'})

class Refused(Exception):
    """Only controlled codes, never raw URL/SQL/header/error content."""

@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    origin: str = ORIGIN
    redirected: bool = False

class NoLiveTransport:
    def get(self, origin, endpoint, params, timeout):
        raise Refused('live_transport_not_implemented')

def safe_error(_exc):
    # Deliberately do not stringify external exceptions; this is not regex redaction.
    return {'status':'not_run_or_failed','reason':'operation_refused_or_failed',
            'raw_exception_suppressed':True}

def ident(x):
    return isinstance(x,(str,int)) and not isinstance(x,bool) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(x)))

def decimal_text(x):
    return str(x) if isinstance(x,(str,int)) and not isinstance(x,bool) and re.fullmatch(r'-?\d{1,32}(?:\.\d{1,24})?',str(x)) else None

class MockReviewClient:
    """GET policy under injectable OFFLINE transport; not a signing/network client.
    Future real adapter must hard-bound DNS/TLS/connect/read/body/whole action time,
    reject redirects/proxies, sign internally, and prove UID response authenticity.
    """
    def __init__(self, transport=None, budget=5.0, clock=time.monotonic, *, client_keys=(), order_ids=()):
        if len(client_keys)>MAX_OPS or len(order_ids)>MAX_OPS or not all(ident(x) for x in (*client_keys,*order_ids)):
            raise Refused('approved_target_ids_refused')
        self.client_keys=frozenset(str(x) for x in client_keys)
        self.order_ids=frozenset(str(x) for x in order_ids)
        self.transport = transport or NoLiveTransport()
        self.clock = clock
        self.deadline = clock() + min(max(float(budget),0),5.0)
        self.verified = False
        self.stopped = False
        self.calls = 0

    def request(self, endpoint, params=None, method='GET'):
        params = dict(params or {})
        if self.stopped: raise Refused('session_stopped')
        if method != 'GET' or endpoint not in ALLOW or set(params)-ALLOW[endpoint]:
            self.stopped=True; raise Refused('method_endpoint_or_parameter_refused')
        if endpoint != IDENTITY and not self.verified:
            self.stopped=True; raise Refused('uid_not_verified')
        if self.calls >= 8 or self.clock() >= self.deadline:
            self.stopped=True; raise Refused('budget_exhausted')
        for key,value in params.items():
            if not ident(value): self.stopped=True; raise Refused('invalid_parameter')
        if endpoint in {ORDERS,ORDER,TRADES} and (params.get('symbol')!='AAVEUSDT' or params.get('isIsolated')!='FALSE'):
            self.stopped=True; raise Refused('scope_refused')
        if endpoint==ORDER and str(params.get('origClientOrderId')) not in self.client_keys:
            self.stopped=True; raise Refused('client_key_required')
        if endpoint==TRADES and (str(params.get('orderId')) not in self.order_ids or params.get('limit')!='50'):
            self.stopped=True; raise Refused('order_filter_required')
        self.calls += 1
        try:
            reply=self.transport.get(ORIGIN,endpoint,params,min(1.0,max(0,self.deadline-self.clock())))
            if self.clock() >= self.deadline: raise Refused('budget_exhausted')
            if reply.origin != ORIGIN or reply.redirected or 300 <= reply.status < 400:
                raise Refused('redirect_refused')
            if reply.status in {418,429}: raise Refused('rate_stop')
            if reply.status != 200 or len(reply.body)>MAX_BODY: raise Refused('response_refused')
            body=json.loads(reply.body)
        except Exception:
            self.stopped=True
            raise Refused('transport_or_response_refused') from None
        if endpoint==IDENTITY:
            actual=body.get('uid') if isinstance(body,dict) else None
            if isinstance(actual,bool) or not isinstance(actual,(str,int)) or str(actual)!=UID:
                self.stopped=True
                raise Refused('uid_missing_or_mismatched')
            self.verified=True
            return {'uid_verified':True}
        if endpoint==ACCOUNT:
            assets=body.get('userAssets') if isinstance(body,dict) else None
            if not isinstance(assets,list) or len(assets)>1024:
                self.stopped=True; raise Refused('account_shape_refused')
            return {'uid_verified':True,'assets':[
                {'asset':x['asset'],**{k:decimal_text(x.get(k)) for k in ('free','locked','borrowed','interest','netAsset')}}
                for x in assets if isinstance(x,dict) and x.get('asset') in {'AAVE','USDT','BNB'}]}
        rows=body if isinstance(body,list) else [body]
        if len(rows)>50: self.stopped=True; raise Refused('pagination_not_supported')
        allowed=('symbol','orderId','clientOrderId','orderListId','status','side','type',
                 'qty','executedQty','price','commission','commissionAsset','time')
        out=[]
        for row in rows:
            if not isinstance(row,dict) or row.get('symbol')!='AAVEUSDT':
                self.stopped=True; raise Refused('symbol_or_shape_refused')
            out.append({k:str(row[k]) for k in allowed if k in row and ident(row[k])} |
                       {k:decimal_text(row[k]) for k in ('qty','executedQty','price','commission') if k in row})
        return {'uid_verified':True,'rows':out,'coverage':'single_response_no_safety_inference'}


def fixture_path(raw):
    p=Path(raw)
    # No live path, symlink, hardlink, URL, arbitrary URI or large file accepted.
    if not p.is_absolute() or len(p.parts)!=4 or p.parts[1]!='tmp' or not p.parts[2].startswith('t581-fixtures-') or p.name!='fixture.db':
        raise Refused('synthetic_fixture_path_required')
    for parent in [p,*p.parents]:
        if parent.is_symlink(): raise Refused('symlink_refused')
    st=p.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size>4*1024*1024:
        raise Refused('fixture_file_refused')
    return p


def columns(conn,table):
    rows=conn.execute('PRAGMA table_info("'+table+'")').fetchmany(129)
    if not rows or len(rows)>128: raise Refused('schema_missing_or_unbounded')
    return {row[1] for row in rows}


def bounded_select(conn,table,cols,cap):
    have=columns(conn,table)
    required={'intent_id','state'} | ({'op_type','client_key'} if table=='operations' else set())
    if not required<=have: raise Refused('required_columns_missing')
    selected=[x for x in cols if x in have]
    # Every identifier comes from our constant projection, not database input.
    projection=','.join(('substr("'+x+'",1,8192) AS "'+x+'"') if x.endswith('_json') else '"'+x+'"' for x in selected)
    sql='SELECT '+projection+' FROM "'+table+'" WHERE intent_id = ? LIMIT ?'
    indexes=conn.execute('PRAGMA index_list("'+table+'")').fetchmany(65)
    if len(indexes)>64: raise Refused('index_metadata_unbounded')
    indexed=False
    for row in indexes:
        # PRAGMA table-valued form uses a value binding for untrusted index name.
        info=conn.execute('SELECT name FROM pragma_index_info(?) ORDER BY seqno LIMIT 2',(row[1],)).fetchall()
        if info and info[0][0]=='intent_id' and not row[4]: indexed=True
    if not indexed: raise Refused('intent_id_leading_index_required')
    plans=conn.execute('EXPLAIN QUERY PLAN '+sql,(INTENT,cap+1)).fetchmany(9)
    if len(plans)!=1 or 'SEARCH ' not in plans[0][3].upper() or 'SCAN ' in plans[0][3].upper():
        raise Refused('bounded_indexed_search_required')
    rows=conn.execute(sql,(INTENT,cap+1)).fetchmany(cap+1)
    if len(rows)>cap: raise Refused('target_row_cap_exceeded')
    return [dict(x) for x in rows],have


def read_synthetic_ledger(raw):
    p=fixture_path(raw)
    # No immutable fallback, no backup, no journal-mode change; OS RO is separate.
    c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=0.1)
    c.row_factory=sqlite3.Row
    steps=[0]
    def progress():
        steps[0]+=1
        return int(steps[0]>200)
    c.set_progress_handler(progress,1000)
    try:
        c.execute('PRAGMA query_only=ON')
        intents,ic=bounded_select(c,'intents',('intent_id','state','created_ms','updated_ms','reason','last_reason'),1)
        ops,oc=bounded_select(c,'operations',('intent_id','op_type','client_key','state','created_ms','updated_ms','reason','last_reason','response_json'),MAX_OPS)
        if len(intents)!=1: raise Refused('target_intent_missing')
        result={'scope':'SYNTHETIC_ONLY','intent':{},'operations':[],
                'reason_columns_missing':{t:[x for x in ('reason','last_reason') if x not in cs] for t,cs in [('intents',ic),('operations',oc)]},
                'not_inferred':['current_exposure','OCO_absence','account_flat','NULL_equals_zero','ALL_DONE_equals_safe'],
                'api_execution':'not_implemented'}
        for row,dest in [(intents[0],result['intent'])]+[(row,{}) for row in ops]:
            for k in ('intent_id','op_type','client_key','state','created_ms','updated_ms'):
                if ident(row.get(k)):dest[k]=str(row[k])
            # Reason text can contain URL/token/account data: disclose presence only.
            dest['reason_presence']={k:row.get(k) is not None for k in ('reason','last_reason') if k in row}
            dest['reason_codes']={k:row[k] for k in ('reason','last_reason') if row.get(k) in REASON_CODES}
            dest['reason_text']='not_exported_pending_review'
            if 'op_type' in row:
                # No raw request/response/payload, signatures or URLs exported.
                try: body=json.loads(row.get('response_json') or '{}')
                except (TypeError,ValueError):body={}
                if isinstance(body,dict) and ident(body.get('orderId')):dest['orderId']=str(body['orderId'])
                result['operations'].append(dest)
        return result
    finally:c.close()


def hard_bounded_process(command,seconds=MAX_ACTION_SEC):
    """For synthetic worker only. Separate process group, TERM then KILL group.
    Generic helper is not exposed as a user-command CLI. Caller code is trusted.
    """
    if not (0<float(seconds)<=MAX_ACTION_SEC):raise Refused('invalid_action_budget')
    proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                          start_new_session=True,env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'})
    try:
        out,_=proc.communicate(timeout=seconds)
        if len(out)>1048576 or proc.returncode:raise Refused('worker_failed')
        return json.loads(out)
    except subprocess.TimeoutExpired:
        try:os.killpg(proc.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:proc.communicate(timeout=0.1)
        except subprocess.TimeoutExpired:pass
        raise Refused('worker_timeout') from None
    finally:
        # Also kills any descendants left after the worker exits/handles TERM.
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.wait()
        if proc.stdout is not None:proc.stdout.close()


def plan():
    return {'mode':'REVIEW_PLAN_ONLY','live_execution':False,'credential_access':False,
            'official_ledger':'/home/box/.config/trade-secrets/cross_margin_ledger.db',
            'outer_ledger':'NOT_A_SUBSTITUTE_DO_NOT_OPEN','intent_id':INTENT,
            'steps':['separately authorize execution and approved environment binding',
                     'OS read-only sandbox; target schema/index/EXPLAIN first',
                     'bounded intent/operations; missing reasons explicitly reported',
                     'verify authenticated UID response before any subsequent account request'],
            'api_candidates_pending_docs':list(ALLOW),
            'unsupported':['live SQLite execution','network/signing/credential adapter',
                           'trusted UID endpoint capability confirmation','OCO list readback',
                           'borrow/repay/interest history endpoints and pagination',
                           'deprecated /sapi/v1/margin/loan endpoint','reason text redaction review',
                           'five spot trades and volume investigation'],
            'credential_source':'NOT_CONFIGURED; no env names/secret reads inferred',
            'requires':['new explicit execution instruction','reviewed executable adapter'],
            'coverage':'offline policy and synthetic SQLite only; NOT production accepted'}


class QuietParser(argparse.ArgumentParser):
    def error(self,message):
        print(json.dumps({'status':'not_run','reason':'arguments_refused'}),file=sys.stderr)
        raise SystemExit(2)

def main(argv=None):
    parser=QuietParser(description=__doc__)
    parser.add_argument('--plan',action='store_true')
    parser.add_argument('--synthetic-worker',metavar='FIXTURE',help=argparse.SUPPRESS)
    parser.add_argument('--offline-ledger-fixture',metavar='FIXTURE')
    args=parser.parse_args(argv)
    try:
        if args.synthetic_worker:
            import resource
            resource.setrlimit(resource.RLIMIT_AS,(268435456,268435456))
            resource.setrlimit(resource.RLIMIT_CPU,(2,2))
            doc=read_synthetic_ledger(args.synthetic_worker)
        elif args.offline_ledger_fixture:
            fixture_path(args.offline_ledger_fixture)
            doc=hard_bounded_process([sys.executable,'-B',str(Path(__file__).resolve()),'--synthetic-worker',args.offline_ledger_fixture])
        else:doc=plan()
    except Exception as exc:
        print(json.dumps(safe_error(exc)));return 2
    print(json.dumps(doc,ensure_ascii=False,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
