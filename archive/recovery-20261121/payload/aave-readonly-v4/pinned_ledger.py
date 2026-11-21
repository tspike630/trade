"""Small-ledger descriptor pinning. No credentials, network, copy, backup or immutable."""
import os,stat,hashlib
from pathlib import Path
ROOT='/tmp/aave-pinned'
VIEW=ROOT+'/db/ledger.db'
SOURCE=ROOT+'/source'
SUFFIXES=('', '-wal', '-shm', '-journal')
MAX_SMALL=16*1024*1024
class PinError(Exception):pass
def need(ok,code):
    if not ok:raise PinError(code)
def ident(s):return [s.st_dev,s.st_ino]
def small_regular(s):return stat.S_ISREG(s.st_mode) and 0<=s.st_size<=MAX_SMALL and s.st_nlink==1
def digest(fd):
    s=os.fstat(fd);need(small_regular(s),'pinned_file_refused')
    h=hashlib.sha256();offset=0
    while True:
        chunk=os.pread(fd,min(1048576,MAX_SMALL+1-offset),offset)
        if not chunk:break
        h.update(chunk);offset+=len(chunk);need(offset<=MAX_SMALL,'pinned_file_size_exceeded')
    return h.hexdigest()
class Pins:
    def __init__(self,path):
        self.fds=[];self.files={};self.meta={};self.path=Path(path)
        try:
            need(self.path.is_absolute() and '..' not in self.path.parts,'pin_path_refused')
            fd=os.open('/',os.O_PATH|os.O_DIRECTORY|os.O_CLOEXEC);self.fds.append(fd)
            for part in self.path.parent.parts[1:]:
                before=os.stat(part,dir_fd=fd,follow_symlinks=False)
                new=os.open(part,os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
                self.fds.append(new);need(ident(os.fstat(new))==ident(before),'ancestor_changed');fd=new
            self.parent=fd
            self.ancestors=[ident(os.fstat(x)) for x in self.fds]
            for suf in SUFFIXES:
                name=self.path.name+suf
                try:before=os.stat(name,dir_fd=fd,follow_symlinks=False)
                except FileNotFoundError:
                    need(suf!='','pinned_main_missing');self.meta[suf]=None;continue
                need(small_regular(before),'pinned_file_refused')
                item=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=fd)
                self.fds.append(item);s=os.fstat(item)
                need(small_regular(s) and ident(s)==ident(before),'pin_object_changed')
                self.files[suf]=item;self.meta[suf]=ident(s)
            self.hash=digest(self.files[''])
        except BaseException:self.close();raise
    def check_names(self):
        # Supplemental generation check, NOT the SQLite object-binding mechanism.
        opened=[]
        try:
            fd=os.open('/',os.O_PATH|os.O_DIRECTORY|os.O_CLOEXEC);opened.append(fd)
            need(ident(os.fstat(fd))==self.ancestors[0],'source_ancestor_changed')
            for index,part in enumerate(self.path.parent.parts[1:],1):
                fd=os.open(part,os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd);opened.append(fd)
                need(ident(os.fstat(fd))==self.ancestors[index],'source_ancestor_changed')
            for suf,wanted in self.meta.items():
                try:s=os.stat(self.path.name+suf,dir_fd=fd,follow_symlinks=False)
                except FileNotFoundError:
                    need(wanted is None,'source_generation_changed');continue
                need(wanted is not None and small_regular(s) and ident(s)==wanted,'source_generation_changed')
        finally:
            for fd in reversed(opened):os.close(fd)
    def job(self):return {'source_name':self.path.name,'objects':self.meta,'main_sha256':self.hash,'parent':ident(os.fstat(self.parent))}
    def pass_fds(self):return tuple([self.parent]+list(self.files.values()))
    def close(self):
        for fd in reversed(self.fds):
            try:os.close(fd)
            except OSError:pass
        self.fds=[]

def command(pins,program):
    # Every actual SQLite file open resolves only inside a sealed private VFS view.
    # The visible source directory is retained for generation checks, NOT SQLite I/O.
    args=['bwrap','--unshare-all','--die-with-parent','--ro-bind','/','/','--proc','/proc','--dev','/dev','--tmpfs','/tmp','--dir',ROOT,'--tmpfs',ROOT+'/db','--dir',SOURCE,'--ro-bind-fd',str(pins.parent),SOURCE]
    for suf,fd in pins.files.items():args+=['--ro-bind-fd',str(fd),VIEW+suf]
    args+=['--remount-ro',ROOT+'/db','--clearenv','--setenv','PATH','/usr/bin:/bin','--setenv','PYTHONDONTWRITEBYTECODE','1']
    return args+list(program)

def check_view(job):
    need(isinstance(job,dict) and set(job)=={'source_name','objects','main_sha256','parent'},'pin_job_refused')
    name=job['source_name'];need(isinstance(name,str) and name not in {'.','..'} and '/' not in name and name,'pin_job_refused')
    need(set(job['objects'])==set(SUFFIXES),'pin_job_refused')
    need(ident(os.stat(SOURCE,follow_symlinks=False))==job['parent'],'source_parent_changed')
    for suf,wanted in job['objects'].items():
        for p in (SOURCE+'/'+name+suf,VIEW+suf):
            try:s=os.stat(p,follow_symlinks=False)
            except FileNotFoundError:
                need(wanted is None,'source_generation_changed');continue
            need(wanted is not None and small_regular(s) and ident(s)==wanted,'source_generation_changed')
    fd=os.open(VIEW,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        need(ident(os.fstat(fd))==job['objects'][''],'pinned_main_changed')
        need(digest(fd)==job['main_sha256'],'source_main_content_changed')
    finally:os.close(fd)
