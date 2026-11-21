"""Offline harness only; never invokes an execution flag or production worker."""
import json,os,sys,tempfile,threading,time
from pathlib import Path
import aave_readonly_candidate as m

def native_read(pins,action=None,inject_failure=False,write_probe=False):
    with tempfile.TemporaryDirectory(prefix='t584-channel-') as tmp:
        channel=Path(tmp);fd=os.open(channel,os.O_PATH|os.O_DIRECTORY)
        issues=[]
        def adversary():
            deadline=time.monotonic()+3
            while not (channel/'ready').exists():
                if time.monotonic()>deadline:issues.append('synthetic_hook_timeout');return
                threading.Event().wait(.005)
            try:action()
            except BaseException as ex:issues.append(type(ex).__name__)
            finally:(channel/'go').write_text('resume')
        thread=threading.Thread(target=adversary,daemon=True) if action else None
        if thread:thread.start()
        program="""import sys,json,os,time,sqlite3
from pathlib import Path
sys.path.insert(0,'/review')
import aave_readonly_candidate as m
job=json.load(sys.stdin)
real=sqlite3.connect;calls=[];opened=None;blocked=None
if job['write_probe']:
 try:
  f=os.open(m.pin.VIEW,os.O_RDWR);os.close(f);blocked=False
 except OSError:blocked=True
def connect(database,**kw):
 global opened
 calls.append(database)
 if job['action']:
  Path('/tmp/channel/ready').write_text('ready')
  deadline=time.monotonic()+3
  while not Path('/tmp/channel/go').exists():
   if time.monotonic()>deadline:raise RuntimeError('fixture_sync_timeout')
   time.sleep(.005)
 if job['inject_failure']:raise sqlite3.OperationalError('SYNTHETIC_WAL_RO_FAILURE')
 conn=real(database,**kw)
 try:opened=conn.execute('SELECT state FROM intents WHERE intent_id=?',(m.IID,)).fetchone()[0]
 except sqlite3.Error:pass
 return conn
sqlite3.connect=connect
try:
 result=m.query_pinned(job['pin'],mountinfo=Path('/proc/self/mountinfo').read_text());out={'ok':True,'result':result}
except Exception as ex:out={'ok':False,'failure':m.controlled_error(ex)}
out.update({'opened_state':opened,'connect_calls':len(calls),'uris':calls,'kernel_write_denied':blocked,'production_paths_absent':not Path('/home/box/.config/trade-secrets').exists() and not Path('/workspace/dexscreener-crawler').exists()})
print(json.dumps(out))
"""
        executable=['/usr/bin/python3','-I','-B','-c',program]
        command=m.pin.command(pins,['--dir','/tmp/channel','--bind-fd',str(fd),'/tmp/channel']+executable)
        assert '--execute-readonly' not in command and '--worker' not in command
        try:
            result=m.bounded_child(command,{'pin':pins.job(),'action':bool(action),'inject_failure':inject_failure,'write_probe':write_probe},seconds=4.5,pass_fds=pins.pass_fds()+(fd,))
        finally:
            os.close(fd)
            if thread:thread.join(timeout=.5)
        assert not issues,issues
        return result

def launch(pins,config=None):
    r=native_read(pins)
    if not r['ok']:raise m.Failure(r['failure'])
    return r['result']
