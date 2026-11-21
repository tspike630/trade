#!/usr/bin/env python3
"""T586 V4 AUTHOR candidate. Default PLAN ONLY. No production acceptance is implied."""
from __future__ import annotations
from decimal import Decimal
import argparse, hashlib, hmac, http.client, json, os, re, signal, sqlite3, ssl, stat, subprocess, sys, time
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
import aave_evidence_review as core
import pinned_ledger as pin
BASE=Path(__file__).resolve().parent
OFFICIAL=Path('/home/box/.config/trade-secrets/cross_margin_ledger.db')
HOST='api.binance.com'
UID='1275399777'
IID='dl-149741'
IDENTITY='/api/v3/account'
ACCOUNT='/sapi/v1/margin/account'
PREFIX='/sapi/v1/margin/'
ALLOW={IDENTITY:set(),ACCOUNT:set(),
 PREFIX+'openOrders':{'symbol','isIsolated'},
 PREFIX+'order':{'symbol','isIsolated','orderId','origClientOrderId'},
 PREFIX+'allOrders':{'symbol','isIsolated','startTime','endTime','limit'},
 PREFIX+'allOrderList':{'isIsolated','startTime','endTime','limit'},
 PREFIX+'orderList':{'isIsolated','orderListId','origClientOrderId'},
 PREFIX+'myTrades':{'symbol','isIsolated','startTime','endTime','limit'},
 PREFIX+'borrow-repay':{'asset','type','startTime','endTime','current','size'},
 PREFIX+'interestHistory':{'asset','startTime','endTime','current','size'}}
CAP=524288
ACTION_SEC=5.0
MAX_CALLS=32
class Failure(Exception):pass

def require(test,code):
    if not test:raise Failure(code)

def controlled_error(exc):
    # No external message, URL, query, headers or body is ever formatted.
    if isinstance(exc,sqlite3.Error):return 'sqlite_read_failed_no_fallback'
    return str(exc) if isinstance(exc,(Failure,core.Refused)) and re.fullmatch('[a-z_]{1,80}',str(exc)) else 'operation_failed_details_suppressed'

def plan():
    return {'mode':'PLAN_ONLY','production_executed':False,'identity':{'endpoint':IDENTITY,'field':'uid','expected_match':UID,'fail_closed':True},'official_ledger':str(OFFICIAL),'intent_id':IID,'supported_get_paths':list(ALLOW),'credential_access':False,'requires':['new explicit execution authorization','--execute-readonly','reviewed approved config with explicitly named HMAC env sources'],'no_inference':['NULL is not zero','ALL_DONE is not proof of safe/flat','protection_blocked is not proof of current naked exposure']}

CFG_KEYS={'approval_reference','expected_uid','api_key_env','api_secret_env','credential_scheme','start_time_ms','end_time_ms','execute_account_reads','output_dir','approved_client_keys','approved_order_ids','approved_oco_ids'}
def config_check(c,now_ms=None):
    require(isinstance(c,dict) and set(c)==CFG_KEYS,'config_keys_refused')
    require(core.ident(c['approval_reference']) and c['approval_reference']!='REPLACE_AFTER_APPROVAL','approval_reference_required')
    require(c['expected_uid']==UID and c['credential_scheme']=='HMAC_SHA256','identity_or_credential_scheme_refused')
    require(type(c['execute_account_reads']) is bool,'config_boolean_required')
    for k in ('api_key_env','api_secret_env'):
        require(isinstance(c[k],str) and bool(re.fullmatch(r'[A-Z][A-Z0-9_]{2,79}',c[k])),'approved_env_name_required')
    require(c['api_key_env']!=c['api_secret_env'],'distinct_credential_sources_required')
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    a,b=c['start_time_ms'],c['end_time_ms']
    require(type(a) is int and type(b) is int and 0<a<b<=now_ms and b-a<=86400000,'approved_time_window_refused')
    out=Path(c['output_dir'])
    require(out.is_absolute() and out.parent==BASE and out.name.startswith('evidence-') and out.name not in {'.','..'},'output_directory_refused')
    for name in ('approved_client_keys','approved_order_ids','approved_oco_ids'):
        require(isinstance(c[name],list) and len(c[name])<=8 and all(core.ident(x) for x in c[name]),'target_ids_refused')
    require(all(str(x).isdigit() and int(x)>0 for k in ('approved_order_ids','approved_oco_ids') for x in c[k]),'positive_order_ids_required')
    return c

def mount_rows(text):
    out=[]
    for line in text.splitlines():
        left=line.split(' - ',1)[0].split()
        require(len(left)>=6,'mountinfo_invalid')
        path=re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),left[4])
        out.append((Path(path),'ro' in left[5].split(',')))
    return out

def underneath(path,parent):return path==parent or parent in path.parents

def assert_readonly_mounts(text,target):
    rows=mount_rows(text);target=Path(target);parent=target.parent
    require(any(p==Path('/') and ro for p,ro in rows),'root_mount_not_readonly')
    for p,ro in rows:
        if not any(underneath(p,Path(x)) for x in ('/proc','/dev','/tmp')):
            require(ro,'business_submount_not_readonly')
        if underneath(p,parent):require(ro,'ledger_submount_not_readonly')
    applicable=[x for x in rows if underneath(target,x[0])]
    require(bool(applicable) and max(applicable,key=lambda x:len(x[0].parts))[1],'ledger_mount_not_readonly')

def check_official_path(path):
    p=Path(path);require(p==OFFICIAL,'official_path_required')
    # Check names only; no scanning or fallback to the outer ledger or main DB.
    for q in (p,*p.parents):require(not q.is_symlink(),'ledger_symlink_refused')
    s=p.stat();require(stat.S_ISREG(s.st_mode) and s.st_size<=16777216 and s.st_nlink==1,'ledger_file_refused')
    return p

def _launch_pinned(pins,config):
    program=['/usr/bin/python3','-I','-B',str(Path(__file__).resolve()),'--execute-readonly','--worker','pinned-db']
    return bounded_child(pin.command(pins,program),{'config':config,'pin':pins.job()},seconds=3.5,pass_fds=pins.pass_fds())

def ledger_read(path,*,mountinfo,config=None):
    p=check_official_path(path);assert_readonly_mounts(mountinfo,p)
    pins=None
    try:
        pins=pin.Pins(p);pins.check_names()
        result=_launch_pinned(pins,config)
        pins.check_names()
        return result
    except pin.PinError as ex:raise Failure(str(ex)) from None
    except OSError:raise Failure('ledger_pin_open_refused') from None
    finally:
        if pins is not None:pins.close()

def query_pinned(job,*,mountinfo):
    # This function is reached only in a descriptor-mounted sealed namespace.
    assert_readonly_mounts(mountinfo,Path(pin.VIEW))
    try:pin.check_view(job)
    except pin.PinError as ex:raise Failure(str(ex)) from None
    p=Path(pin.VIEW)
    # mode=ro may fail on WAL/SHM. Such failures are reported; NEVER immutable.
    conn=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=.1)
    conn.row_factory=sqlite3.Row
    steps=[0]
    def progress():
        steps[0]+=1;return int(steps[0]>200)
    conn.set_progress_handler(progress,1000)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        intents,ic=core.bounded_select(conn,'intents',('intent_id','state','created_ms','updated_ms','reason','last_reason','last_error','baseline_base','payload_json'),1)
        ops,oc=core.bounded_select(conn,'operations',('intent_id','op_type','client_key','state','created_ms','updated_ms','reason','last_reason','last_error','request_json','response_json'),32)
        require(len(intents)==1,'intent_missing')
        target={'client_keys':[],'order_ids':[],'oco_ids':[],'oco_keys':[]}
        result={'intent':{},'operations':[],'coverage':'partial','scope':IID,
                'reason_missing':{t:[x for x in ('reason','last_reason','last_error') if x not in cs] for t,cs in [('intents',ic),('operations',oc)]},
                'not_inferred':plan()['no_inference'],'targets':target}
        for row,dest in [(intents[0],result['intent'])]+[(x,{}) for x in ops]:
            for k in ('intent_id','op_type','client_key','state','created_ms','updated_ms'):
                if core.ident(row.get(k)):dest[k]=str(row[k])
            dest['reason_presence']={k:row.get(k) is not None for k in ('reason','last_reason','last_error') if k in row}
            dest['known_reason_codes']={k:row[k] for k in ('reason','last_reason','last_error') if row.get(k) in core.REASON_CODES}
            dest['unknown_reason_present']=any(row.get(k) is not None and row.get(k) not in core.REASON_CODES for k in ('reason','last_reason','last_error'))
            if 'op_type' not in row:
                payload=parse_object(row.get('payload_json'))
                intent=payload.get('intent') if isinstance(payload.get('intent'),dict) else {}
                require(intent.get('symbol')=='AAVEUSDT','ledger_symbol_unverified')
                if 'baseline_base' in row:dest['baseline_base']=core.decimal_text(row['baseline_base'])
                for k in ('quantity','entry_price','stop_price','take_profit_price'):
                    if k in intent:dest[k]=core.decimal_text(intent[k])
                snapshot=payload.get('snapshot') if isinstance(payload.get('snapshot'),dict) else {}
                dest['stored_snapshot_summary']={k:core.decimal_text(snapshot[k]) for k in ('reference_price','fee_rate') if k in snapshot}
                rates=snapshot.get('hourly_interest_rate')
                if isinstance(rates,dict):dest['stored_snapshot_summary']['hourly_interest_rate']={k:core.decimal_text(rates[k]) for k in ('AAVE','USDT') if k in rates}
                for k in ('symbol','action','base_asset','quote_asset'):
                    if core.ident(intent.get(k)):dest[k]=str(intent[k])
                continue
            op=str(row.get('op_type'));key=row.get('client_key')
            if core.ident(key) and op=='entry':target['client_keys'].append(str(key))
            if core.ident(key) and op=='oco':target['oco_keys'].append(str(key))
            for col in ('request_json','response_json'):
                body=parse_object(row.get(col))
                # Only ledger-bound IDs, not API-returned URLs or new instructions.
                for k,bucket in [('orderId','order_ids'),('orderListId','oco_ids')]:
                    if str(body.get(k,'')).isdigit() and int(body[k])>0:
                        target[bucket].append(str(body[k]));dest[k]=str(body[k])
                for k in ('executedQty','origQty','price'):
                    if k in body:dest[k]=core.decimal_text(body[k])
            result['operations'].append(dest)
        for k in target:target[k]=list(dict.fromkeys(target[k]));require(len(target[k])<=8,'ledger_target_cap_exceeded')
        result['readonly_proof']={'whole_business_mounts_ro':True,'sqlite_mode':'ro','query_plan':'indexed SEARCH checked','native_syscalls_traced':False,'descriptor_bound_private_view':True,'source_generation_checks':'before_and_after','immutable_fallback':False}
        result['coverage_notes']=['indexed exact-id ledger snapshot only','unknown/free-text reasons not exported','no current account inference','truncated or nested JSON IDs may be unavailable']
        try:pin.check_view(job)
        except pin.PinError as ex:raise Failure(str(ex)) from None
        return result
    finally:conn.close()

def parse_object(raw):
    try:x=json.loads(raw or '{}')
    except (ValueError,TypeError):return {}
    return x if isinstance(x,dict) else {}

NUMERIC={'free','locked','borrowed','interest','netAsset','qty','origQty','executedQty','price','commission','interestRate','principal','amount','marginLevel'}
TEXT={'asset','symbol','orderId','clientOrderId','orderListId','listClientOrderId','status','listOrderStatus','listStatusType','side','type','id','txId','tranId','commissionAsset','time','timestamp','transactionTime','interestAccuredTime','accountType','isBuyer','isMaker','isIsolated','created_ms','updated_ms'}
def project(row):
    require(isinstance(row,dict),'response_shape_invalid')
    out={}
    for k,v in row.items():
        if k in NUMERIC:out[k]=core.decimal_text(v)
        elif k in TEXT:
            if type(v) is bool:out[k]=v
            elif core.ident(v):out[k]=str(v)
    if isinstance(row.get('orders'),list):
        require(len(row['orders'])<=8,'oco_leg_cap_exceeded');out['orders']=[project(x) for x in row['orders']]
    return out

def schema_row(row,kind):
    require(isinstance(row,dict) and not ({'code','msg'} & set(row)),'response_shape_invalid')
    def text(k):require(isinstance(row.get(k),str) and core.ident(row[k]),'response_field_invalid')
    def integer(k):require(type(row.get(k)) in (int,str) and str(row[k]).isdigit() and len(str(row[k]))<=20,'response_field_invalid')
    def number(k,negative=False):
        v=core.decimal_text(row.get(k));require(v is not None and (negative or Decimal(v)>=0),'response_field_invalid')
    # Nested order containers have endpoint-specific semantics; never recurse
    # through an unvalidated container just because project() knows its key.
    require('orderReports' not in row and (kind=='oco' or 'orders' not in row),'nested_order_container_refused')
    if kind=='asset':
        text('asset')
        for k in ('free','locked','borrowed','interest','netAsset'):number(k,k=='netAsset')
    elif kind=='order':
        for k in ('symbol','clientOrderId','status','side'):text(k)
        integer('orderId');integer('time')
        require(row['side'] in {'BUY','SELL'},'response_field_invalid')
        require(row['status'] in {'NEW','PENDING_NEW','PARTIALLY_FILLED','FILLED','CANCELED','PENDING_CANCEL','REJECTED','EXPIRED','EXPIRED_IN_MATCH'},'response_field_invalid')
        for k in ('origQty','executedQty','price'):number(k)
    elif kind=='oco':
        for k in ('symbol','listClientOrderId','listStatusType','listOrderStatus'):text(k)
        integer('orderListId');integer('transactionTime')
        require(row['listStatusType'] in {'RESPONSE','EXEC_STARTED','ALL_DONE'} and row['listOrderStatus'] in {'EXECUTING','ALL_DONE','REJECT'},'response_field_invalid')
        require(isinstance(row.get('orders'),list) and 1<=len(row['orders'])<=8,'oco_legs_invalid')
        for leg in row['orders']:
            require(isinstance(leg,dict),'oco_legs_invalid')
            require(isinstance(leg.get('symbol'),str) and core.ident(leg['symbol']) and type(leg.get('orderId')) in (int,str) and str(leg['orderId']).isdigit() and isinstance(leg.get('clientOrderId'),str) and core.ident(leg['clientOrderId']),'oco_legs_invalid')
            require('orders' not in leg and 'orderReports' not in leg,'nested_order_container_refused')
            require(leg['symbol']==row['symbol'],'oco_leg_symbol_conflict')
            # Optional projected relationship fields may not contradict the list.
            if 'orderListId' in leg:
                require(type(leg['orderListId']) in (str,int) and str(leg['orderListId']).isdigit() and len(str(leg['orderListId']))<=20 and int(leg['orderListId'])==int(row['orderListId']),'oco_leg_list_conflict')
            if 'isIsolated' in leg:
                require(type(leg['isIsolated']) is bool and leg['isIsolated']==row.get('isIsolated',False),'oco_leg_scope_conflict')
            for k in NUMERIC & set(leg):
                v=core.decimal_text(leg[k]);require(v is not None and (k=='netAsset' or Decimal(v)>=0),'response_field_invalid')
    elif kind=='trade':
        for k in ('symbol','commissionAsset'):text(k)
        for k in ('orderId','id','time'):integer(k)
        for k in ('qty','price','commission'):number(k)
        for k in ('isBuyer','isMaker'):require(type(row.get(k)) is bool,'response_field_invalid')
    elif kind=='loan':
        for k in ('asset','status','type'):text(k)
        integer('timestamp');integer('txId');number('amount')
        for k in ('principal','interest'):
            if k in row:number(k)
    elif kind=='interest':
        for k in ('asset','type'):text(k)
        integer('interestAccuredTime')
        for k in ('principal','interest','interestRate'):number(k)
    else:raise Failure('response_kind_refused')
    # Check optional known numeric fields too: malformed fields are not sanitized to NULL.
    for k in NUMERIC & set(row):number(k,k=='netAsset')
    if 'isIsolated' in row:require(type(row['isIsolated']) is bool,'response_field_invalid')

def sanitize(endpoint,body,request_params=None):
    require(endpoint in ALLOW,'request_policy_refused')
    require(not (isinstance(body,dict) and ({'code','msg'} & set(body))),'api_error_object_refused')
    if endpoint==IDENTITY:
        actual=body.get('uid') if isinstance(body,dict) else None
        require(type(actual) in (str,int) and str(actual)==UID,'uid_missing_or_mismatched')
        return {'uid_verified':True}
    if endpoint==ACCOUNT:
        require(isinstance(body,dict) and isinstance(body.get('userAssets'),list) and len(body['userAssets'])<=2048,'account_shape_invalid')
        require(core.decimal_text(body.get('marginLevel')) is not None and isinstance(body.get('accountType'),str) and core.ident(body['accountType']),'account_shape_invalid')
        for row in body['userAssets']:schema_row(row,'asset')
        names=[x['asset'] for x in body['userAssets']];require(len(names)==len(set(names)),'account_duplicate_asset')
        return {'assets':[project(x) for x in body['userAssets'] if x['asset'] in {'AAVE','USDT','BNB'}], 'missing_requested_assets':sorted({'AAVE','USDT','BNB'}-set(names)), 'marginLevel':core.decimal_text(body['marginLevel']),'accountType':body['accountType']}
    leaf=endpoint[len(PREFIX):]
    paged=leaf in {'borrow-repay','interestHistory'}
    arrays=leaf in {'openOrders','allOrders','myTrades','allOrderList'}
    if paged:
        require(isinstance(body,dict) and isinstance(body.get('rows'),list),'history_response_shape_invalid')
        total=body.get('total')
        require(total is None or (type(total) is int and total>=0),'history_total_invalid')
        rows=body['rows']
    elif arrays:
        require(isinstance(body,list),'array_response_shape_invalid');rows=body;total=None
    else:
        require(isinstance(body,dict),'direct_response_shape_invalid');rows=[body];total=None
    cap=int((request_params or {}).get('size' if paged else 'limit',100 if paged else 1000))
    require(len(rows)<=cap,'response_rows_cap_exceeded')
    kind={'borrow-repay':'loan','interestHistory':'interest','allOrderList':'oco','orderList':'oco','myTrades':'trade'}.get(leaf,'order')
    retained=[];excluded=0
    for row in rows:
        schema_row(row,kind) # Validate BEFORE filtering, even out-of-scope records.
        if paged:
            if row['asset']!='AAVE' or row.get('isolatedSymbol') not in (None,''):
                excluded+=1;continue
        elif row['symbol']!='AAVEUSDT':
            require(arrays,'direct_order_scope_unverified');excluded+=1;continue
        else:require(row.get('isIsolated') is not True,'isolated_response_refused')
        retained.append(project(row))
    return {'rows':retained,'received_count':len(rows),'excluded_outside_scope':excluded,'total':total}

def validate_request(endpoint,params,method='GET'):
    require(method=='GET' and endpoint in ALLOW and isinstance(params,dict) and not(set(params)-ALLOW[endpoint]),'request_policy_refused')
    require(all(core.ident(v) for v in params.values()),'request_values_refused')
    if endpoint not in {IDENTITY,ACCOUNT}:
        if endpoint not in {PREFIX+'allOrderList',PREFIX+'orderList',PREFIX+'borrow-repay',PREFIX+'interestHistory'}:
            require(params.get('symbol')=='AAVEUSDT','request_symbol_refused')
        if endpoint not in {PREFIX+'borrow-repay',PREFIX+'interestHistory'}:require(params.get('isIsolated')=='FALSE','cross_scope_required')
        else:require(params.get('asset')=='AAVE','request_asset_refused')
    if endpoint==PREFIX+'order':require(bool(params.get('orderId') or params.get('origClientOrderId')),'order_id_required')
    if endpoint==PREFIX+'orderList':require(bool(params.get('orderListId') or params.get('origClientOrderId')),'oco_id_required')
    if endpoint==PREFIX+'borrow-repay':require(params.get('type') in {'BORROW','REPAY'},'borrow_type_required')
    for k in ('startTime','endTime','limit','current','size'):
        if k in params:require(str(params[k]).isdigit(),'numeric_parameter_required')
    if 'limit' in params:require(1<=int(params['limit'])<=100,'page_size_refused')
    if 'size' in params:require(int(params['size'])==100,'page_size_refused')
    if 'current' in params:require(1<=int(params['current'])<=3,'page_number_refused')
    if 'startTime' in params or 'endTime' in params:
        a,b=int(params.get('startTime',0)),int(params.get('endTime',0))
        require(0<a<b and b-a<=86400000,'request_time_window_refused')

def sign_query(params,secret,timestamp):
    from urllib.parse import urlencode
    q=urlencode(sorted({**params,'timestamp':str(timestamp),'recvWindow':'5000'}.items()))
    return q+'&signature='+hmac.new(secret.encode(),q.encode(),hashlib.sha256).hexdigest()

def https_get(endpoint,params,key,secret,*,connection_factory=None):
    validate_request(endpoint,params)
    require(isinstance(key,str) and 0<len(key)<=4096 and isinstance(secret,str) and 0<len(secret)<=4096,'credential_format_refused')
    factory=connection_factory or http.client.HTTPSConnection
    conn=None;deadline=time.monotonic()+4.0
    try:
        # http.client does not inherit HTTP(S)_PROXY and does not follow redirects.
        conn=factory(HOST,443,timeout=2.0,context=ssl.create_default_context())
        query=sign_query(params,secret,int(time.time()*1000))
        conn.request('GET',endpoint+'?'+query,headers={'X-MBX-APIKEY':key,'Accept':'application/json','Accept-Encoding':'identity'})
        response=conn.getresponse()
        if response.status in (418,429):raise Failure('rate_limit_stop')
        require(not 300<=response.status<400,'redirect_refused')
        require(response.status==200,'http_status_refused')
        require(response.getheader('Content-Encoding','identity').lower()=='identity','content_encoding_refused')
        length=response.getheader('Content-Length')
        if length is not None:require(length.isdigit() and int(length)<=CAP,'response_size_refused')
        data=bytearray()
        while True:
            require(time.monotonic()<deadline,'network_action_timeout')
            chunk=response.read(min(32768,CAP+1-len(data)))
            if not chunk:break
            data.extend(chunk);require(len(data)<=CAP,'response_size_refused')
        return sanitize(endpoint,json.loads(data),params)
    except Failure:raise
    except Exception:raise Failure('network_or_parse_error_suppressed') from None
    finally:
        if conn is not None:
            try:conn.close()
            except Exception:pass

def api_job(job,*,connection_factory=None):
    # Recheck UID inside EVERY isolated action. Even direct worker invocation
    # cannot issue a non-identity account read before a matching live UID.
    creds=job['credentials'];endpoint=job['endpoint'];params=job['params']
    validate_request(endpoint,params)
    verified=https_get(IDENTITY,{},creds['key'],creds['secret'],connection_factory=connection_factory)
    if endpoint==IDENTITY:return verified
    return https_get(endpoint,params,creds['key'],creds['secret'],connection_factory=connection_factory)

def bounded_child(command,payload,seconds=ACTION_SEC,*,pass_fds=()):
    require(0<float(seconds)<=ACTION_SEC,'action_budget_refused')
    proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True,pass_fds=pass_fds,env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'})
    try:
        raw=json.dumps(payload).encode();require(len(raw)<=65536,'worker_input_too_large')
        out,_=proc.communicate(raw,timeout=seconds)
        require(proc.returncode==0 and len(out)<=1048576,'worker_exit_or_output_refused')
        obj=json.loads(out);require(isinstance(obj,dict),'worker_shape_invalid')
        if obj.get('error'):raise Failure(obj['error'] if re.fullmatch('[a-z_]{1,80}',str(obj['error'])) else 'worker_failed')
        return obj
    except subprocess.TimeoutExpired:raise Failure('worker_hard_timeout') from None
    finally:
        try:os.killpg(proc.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:proc.wait(timeout=.1)
        except subprocess.TimeoutExpired:pass
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.wait()
        for stream in (proc.stdin,proc.stdout):
            if stream is not None:stream.close()

def install_db_audit():
    writes=[]
    def audit(event,args):
        if event=='open' and type(args[0]) in (str,bytes) and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
            writes.append({'write_flag':True});raise Failure('python_write_open_refused')
        if event=='socket.connect':raise Failure('db_network_refused')
    sys.addaudithook(audit);return writes

def worker_command(kind):
    require(kind in {'db','api'},'worker_kind_refused')
    args=['bwrap','--unshare-all','--die-with-parent']
    if kind=='api':args+=['--share-net']
    args+=['--ro-bind','/','/','--proc','/proc','--dev','/dev','--tmpfs','/tmp','--clearenv','--setenv','PATH','/usr/bin:/bin','--setenv','PYTHONDONTWRITEBYTECODE','1','--chdir',str(BASE),'/usr/bin/python3','-I','-B',str(Path(__file__).resolve()),'--execute-readonly','--worker',kind]
    return args

class Session:
    def __init__(self,call,clock=time.monotonic):self.call=call;self.verified=False;self.stopped=False;self.count=0;self.clock=clock;self.deadline=clock()+120;self.audit=[]
    def get(self,endpoint,params=None):
        require(not self.stopped,'session_stopped')
        require(self.count<MAX_CALLS and self.clock()<self.deadline,'session_budget_exhausted')
        params=params or {};validate_request(endpoint,params)
        require(endpoint==IDENTITY or self.verified,'uid_not_verified')
        self.count+=1
        event={'endpoint':endpoint,'status':'started','started_at_ms':int(time.time()*1000)};self.audit.append(event)
        try:
            result=self.call(endpoint,params)
            if endpoint==IDENTITY:
                require(result.get('uid_verified') is True,'uid_missing_or_mismatched');self.verified=True
            require(self.clock()<=self.deadline,'session_budget_exhausted')
            event['status']='complete';event['finished_at_ms']=int(time.time()*1000);return result
        except Exception as ex:
            event['status']='stopped';event['reason']=controlled_error(ex)
            self.stopped=True;raise

def history_pages(session,endpoint,params,result=None):
    # Install this mutable document in the parent's output BEFORE the first call.
    if result is None:result={}
    result.update({'rows':[],'pages':[],'coverage':'partial','truncated_or_total_unconfirmed':True,'successful_pages':0})
    for page in range(1,4):
        try:
            r=session.get(endpoint,{**params,'current':str(page),'size':'100'})
            result['rows'].extend(r['rows'])
            result['pages'].append({'page':page,'status':'complete','received_count':r['received_count'],'excluded':r['excluded_outside_scope'],'total':r['total']})
            result['successful_pages']+=1
            if r['received_count']<100:
                complete=r['total'] is not None and sum(x['received_count'] for x in result['pages'])>=r['total'] and sum(x['excluded'] for x in result['pages'])==0
                result['coverage']='bounded_window_complete' if complete else 'partial'
                result['truncated_or_total_unconfirmed']=not complete
                break
        except Exception as ex:
            result.update({'coverage':'partial','error':controlled_error(ex),'failed_page':page,'rows_retained':len(result['rows'])})
            result['pages'].append({'page':page,'status':'failed','error':controlled_error(ex)})
            raise # No retry, later endpoint, or lost earlier successful page.
    return result

def account_evidence(session,config,ledger,result=None):
    if result is None:result={}
    result.update({'coverage':'partial','no_safety_conclusion':True,'window_ms':[config['start_time_ms'],config['end_time_ms']]})
    session.get(IDENTITY);result['uid_verified']=True
    result['account']=session.get(ACCOUNT)
    cross={'symbol':'AAVEUSDT','isIsolated':'FALSE'}
    result['openOrders']=session.get(PREFIX+'openOrders',cross)
    times={'startTime':str(config['start_time_ms']),'endTime':str(config['end_time_ms'])}
    for name in ('allOrders','myTrades','allOrderList'):
        params={**times,'isIsolated':'FALSE','limit':'100'}
        if name!='allOrderList':params['symbol']='AAVEUSDT'
        response=session.get(PREFIX+name,params)
        response['coverage']='partial' if response['received_count']>=100 else 'bounded_window_only'
        response['truncated_or_pagination_uncertain']=response['received_count']>=100
        result[name]=response
    t=ledger['targets']
    order_keys=list(dict.fromkeys(t['client_keys']+config['approved_client_keys']))
    order_ids=list(dict.fromkeys(t['order_ids']+config['approved_order_ids']))
    oco_ids=list(dict.fromkeys(t['oco_ids']+config['approved_oco_ids']))
    require(len(order_keys)+len(order_ids)+len(oco_ids)+len(t['oco_keys'])<=8,'direct_query_id_budget_exceeded')
    result['linkedOrders']=[];result['linkedOCO']=[]
    for key in order_keys:result['linkedOrders'].append(session.get(PREFIX+'order',{**cross,'origClientOrderId':key}))
    for oid in order_ids:result['linkedOrders'].append(session.get(PREFIX+'order',{**cross,'orderId':oid}))
    for oid in oco_ids:result['linkedOCO'].append(session.get(PREFIX+'orderList',{'isIsolated':'FALSE','orderListId':oid}))
    for key in t['oco_keys']:result['linkedOCO'].append(session.get(PREFIX+'orderList',{'isIsolated':'FALSE','origClientOrderId':key}))
    for typ in ('BORROW','REPAY'):
        result[typ]={};history_pages(session,PREFIX+'borrow-repay',{**times,'asset':'AAVE','type':typ},result[typ])
    result['interestHistory']={};history_pages(session,PREFIX+'interestHistory',{**times,'asset':'AAVE'},result['interestHistory'])
    result['coverage_notes']=['current account snapshot and bounded history are not atomic','OCO history cross request has no symbol parameter; AAVE filtered locally','array histories stop at one 100-row page and disclose possible truncation','history pages capped at 3x100','API order IDs do not authorize new follow-up requests','myTrades is time-bounded AAVE history, not a claim of unique intent attribution','timestamps/fees preserved; no archive, protect, flatten or repay actions']
    return result

def create_output(path):
    p=Path(path);require(p.parent==BASE and p.name.startswith('evidence-'),'output_directory_refused')
    for q in [BASE,*BASE.parents]:require(not q.is_symlink(),'output_symlink_refused')
    p.mkdir(mode=0o700) # Existing directories refused, no overwrites.
    return os.open(p,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)

def save_output(fd,doc):
    data=json.dumps(doc,ensure_ascii=False,indent=2).encode()
    require(len(data)<=2097152,'result_size_refused')
    out=os.open('evidence.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
    try:
        with os.fdopen(out,'wb') as f:f.write(data)
    except Exception:raise Failure('evidence_write_failed') from None

def load_approved_config(path,digest):
    path=Path(path)
    require(path.parent==BASE and not path.is_symlink(),'config_path_refused')
    require(isinstance(digest,str) and bool(re.fullmatch('[0-9a-f]{64}',digest)),'approved_config_digest_required')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        meta=os.fstat(fd)
        require(stat.S_ISREG(meta.st_mode) and meta.st_uid==os.geteuid() and not(meta.st_mode & 0o022) and meta.st_size<=16384,'config_file_refused')
        raw=os.read(fd,16385)
    finally:os.close(fd)
    require(len(raw)<=16384 and hashlib.sha256(raw).hexdigest()==digest,'approved_config_digest_mismatch')
    return json.loads(raw)

def execute(config):
    c=config_check(config);fd=create_output(c['output_dir']);result={'coverage':'partial','production_executed':True,'started_at_ms':int(time.time()*1000),'approval_reference':c['approval_reference'],'actions':[],'no_safety_conclusion':True}
    try:
        ledger=bounded_child(worker_command('db'),{'config':c});result['ledger']=ledger;result['actions'].append('official_indexed_ledger_read')
        if not c['execute_account_reads']:
            result['account']={'coverage':'not_run','reason':'account_reads_not_enabled'};return result
        # Only this explicit execute branch reads explicitly configured credential names.
        key=os.environ.get(c['api_key_env']);secret=os.environ.get(c['api_secret_env'])
        require(bool(key) and bool(secret),'approved_credentials_unavailable')
        session=Session(lambda endpoint,params:bounded_child(worker_command('api'),{'config':c,'endpoint':endpoint,'params':params,'credentials':{'key':key,'secret':secret}},seconds=min(ACTION_SEC,max(.001,session.deadline-time.monotonic()))))
        result['account']={};result['account_audit']=session.audit
        account_evidence(session,c,ledger,result['account'])
        result['actions'].append('uid_gated_account_reads')
        return result
    except Exception as ex:
        result['error']=controlled_error(ex);return result
    finally:
        result['finished_at_ms']=int(time.time()*1000)
        save_output(fd,result);os.close(fd)

class QuietParser(argparse.ArgumentParser):
    def error(self,message):raise Failure('arguments_refused')

def main(argv=None):
    try:
        p=QuietParser(description=__doc__);p.add_argument('--execute-readonly',action='store_true');p.add_argument('--approved-config');p.add_argument('--approved-config-sha256');p.add_argument('--worker',choices=['db','api','pinned-db'],help=argparse.SUPPRESS)
        a=p.parse_args(argv)
        if not a.execute_readonly:
            require(not a.worker and not a.approved_config and not a.approved_config_sha256,'execution_flag_required');print(json.dumps(plan(),indent=2));return 0
        if a.worker:
            require(not a.approved_config and not a.approved_config_sha256,'worker_config_stdin_required')
            import resource
            resource.setrlimit(resource.RLIMIT_AS,(268435456,268435456));resource.setrlimit(resource.RLIMIT_CPU,(4,4))
            raw=sys.stdin.buffer.read(65537);require(len(raw)<=65536,'worker_input_too_large');job=json.loads(raw);config_check(job['config'])
            if a.worker=='db':
                mounts=Path('/proc/self/mountinfo').read_text();writes=install_db_audit();doc=ledger_read(OFFICIAL,mountinfo=mounts,config=job['config']);doc['python_write_flag_opens']=writes
            elif a.worker=='pinned-db':
                writes=install_db_audit();doc=query_pinned(job['pin'],mountinfo=Path('/proc/self/mountinfo').read_text());doc['python_write_flag_opens']=writes
            else:
                require(job['config']['execute_account_reads'],'account_reads_not_enabled');assert_readonly_mounts(Path('/proc/self/mountinfo').read_text(),OFFICIAL);doc=api_job(job)
        else:
            require(a.approved_config is not None,'approved_config_required')
            doc=execute(load_approved_config(a.approved_config,a.approved_config_sha256))
        print(json.dumps(doc,ensure_ascii=False,indent=2));return 2 if doc.get('error') else 0
    except Exception as ex:
        print(json.dumps({'coverage':'not_run_or_partial','error':controlled_error(ex)}));return 2
if __name__=='__main__':raise SystemExit(main())
