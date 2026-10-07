#!/usr/bin/env python3
"""Bind typed Windows readiness to one owned Wine process; never equate PIDs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time


def process_identity(pid, proc_root=Path('/proc')):
    proc=proc_root/str(pid)
    before=(proc/'stat').read_text().rsplit(')',1)[1].split()
    if before[0] in ('Z','X','x'):raise ValueError('Process is not live')
    result={'pid':pid,'start':before[19],'uid':proc.stat().st_uid,
            'cgroup':(proc/'cgroup').read_text().strip(),
            'netns':os.readlink(proc/'ns/net'),'mntns':os.readlink(proc/'ns/mnt')}
    after=(proc/'stat').read_text().rsplit(')',1)[1].split()
    if before[19]!=after[19] or not before[19].isdigit():raise ValueError('Process identity changed')
    return result


def parse_ready(raw,nonce,role):
    if not re.fullmatch('[0-9a-f]{64}',nonce) or role not in ('broker','sdl'):
        raise ValueError('Invalid ready authority')
    match=re.fullmatch(rb'UURB_FULL_READY_V1 ([0-9a-f]{64}) (broker|sdl) ([1-9][0-9]{0,9}) ([1-9][0-9]{0,19})\n',raw)
    if not match or match[1].decode()!=nonce or match[2].decode()!=role:
        raise ValueError('Ready record does not match this startup/role')
    pid,start=int(match[3]),int(match[4])
    if pid>0xffffffff or start>0xffffffffffffffff:raise ValueError('Windows identity out of range')
    return {'windows_pid':pid,'windows_creation_filetime':start,'role':role,'nonce':nonce}


def normalize_argument(value):
    return value.strip('"').replace('\\','/').casefold()


def owned_candidates(exe,windows_exe,prefix,display,nonce,parent,proc_root=Path('/proc')):
    result=[]
    expected={normalize_argument(str(exe)),normalize_argument(windows_exe)}
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():continue
        try:
            if proc.stat().st_uid!=parent['uid']:continue
            command=(proc/'cmdline').read_bytes().split(b'\0')
            if not any(normalize_argument(a.decode('utf-8','strict')) in expected for a in command if a):continue
            if (proc/'comm').read_text().strip().casefold()!=exe.name[:15].casefold():continue
            current=process_identity(int(proc.name),proc_root)
            if any(current[n]!=parent[n] for n in ('uid','cgroup','netns','mntns')):raise ValueError('Named Wine executable is outside the owned bridge')
            environment=dict(a.split(b'=',1) for a in (proc/'environ').read_bytes().split(b'\0') if b'=' in a)
            if environment.get(b'WINEPREFIX')!=str(prefix).encode() or environment.get(b'DISPLAY')!=display.encode() or environment.get(b'UURB_FULL_READY_NONCE')!=nonce.encode():
                raise ValueError('Named Wine executable has wrong prefix/display/startup')
            # /proc/exe is the actual Wine host, not the PE executable.
            if not Path(os.readlink(proc/'exe')).name.startswith('wine'):raise ValueError('Named executable is not hosted by Wine')
            if process_identity(int(proc.name),proc_root)!=current:raise ValueError('Wine process changed while binding')
            result.append(current)
        except (FileNotFoundError,ProcessLookupError):continue
    return result


def bind_ready(path,nonce,role,exe,windows_exe,prefix,display,parent_pid):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>256:
        raise ValueError('Private ready file ownership/type/mode/size mismatch')
    raw=path.read_bytes();record=parse_ready(raw,nonce,role)
    exe_info=exe.lstat()
    if not stat.S_ISREG(exe_info.st_mode) or exe_info.st_uid!=os.getuid():raise ValueError('Approved Wine executable missing/foreign/symlink')
    parent=process_identity(parent_pid)
    if parent['uid']!=os.getuid():raise ValueError('Bridge owner mismatch')
    candidates=owned_candidates(exe,windows_exe,prefix,display,nonce,parent)
    if len(candidates)!=1:raise ValueError('Expected exactly one owned Wine executable')
    actual=candidates[0]
    # These are two independent identities. Named-pipe Windows PID+FILETIME
    # authentication belongs to broker/plugin; native ownership binds Linux PID.
    record.update(linux_identity=actual,bridge_identity=parent,display=display,prefix=str(prefix),
                  executable=str(exe),executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
                  ready_sha256=hashlib.sha256(raw).hexdigest())
    if path.read_bytes()!=raw or process_identity(actual['pid'])!=actual or process_identity(parent_pid)!=parent:
        raise ValueError('Ready/process binding changed')
    return record


def mark_stopped(prefix,parent_pid,nonce,output):
    parent=process_identity(parent_pid)
    if parent['uid']!=os.getuid():raise ValueError('Bridge owner mismatch')
    until=time.monotonic()+3
    while time.monotonic()<until:
        remaining=[]
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():continue
            try:
                if proc.stat().st_uid!=parent['uid'] or (proc/'cgroup').read_text().strip()!=parent['cgroup']:continue
                if not Path(os.readlink(proc/'exe')).name.startswith('wine'):continue
                env=(proc/'environ').read_bytes().split(b'\0')
                if ('WINEPREFIX='+str(prefix)).encode() in env:remaining.append(process_identity(int(proc.name)))
            except (FileNotFoundError,ProcessLookupError):continue
        if not remaining:
            if process_identity(parent_pid)!=parent:raise ValueError('Bridge changed before prefix quiescence marker')
            fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as out:
                json.dump({'nonce':nonce,'bridge':parent,'prefix':str(prefix),'owned_wine_absent':True},out);out.write('\n')
            return 0
        time.sleep(.05)
    raise RuntimeError('Owned Wine prefix remains after exact helper')



def fresh_private_log(path,archive):
    if path.exists() or path.is_symlink():
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():raise ValueError('Historical hook log is foreign/nonregular')
        with path.open('rb') as old:
            old.seek(max(0,info.st_size-1048576));tail=old.read(1048576)
        fd=os.open(archive,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as saved:saved.write(tail)
        path.unlink()
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    return 0


def windows_path(value):
    if not isinstance(value,str) or not value.startswith('C:\\') or len(value)>=260 or any(c in value for c in '\0\r\n'):
        raise ValueError('Invalid bounded C: configuration path')
    if ':' in value[2:] or '/' in value or any(part in ('','.', '..') for part in value.split('\\')[1:]):raise ValueError('C: path has traversal/empty component')
    return value


def public_config_bytes(record,bootstrap,broker_ready,source_exe,log):
    nonce=record['nonce']
    if not re.fullmatch('[0-9a-f]{64}',nonce) or not re.fullmatch('[0-9a-f]{64}',bootstrap) or record['role']!='broker':
        raise ValueError('Invalid exact config startup authority')
    pid,start=record['windows_pid'],record['windows_creation_filetime']
    if not isinstance(pid,int) or isinstance(pid,bool) or not 0<pid<=0xffffffff or not isinstance(start,int) or isinstance(start,bool) or not 0<start<=0xffffffffffffffff:
        raise ValueError('Invalid actual broker Windows identity')
    value=('UURB_PUBLIC_SESSION_V1\nnonce='+nonce+'\nbootstrap='+bootstrap+'\nbroker_ready='+windows_path(broker_ready)+
           '\nsource_exe='+windows_path(source_exe)+'\nlog='+windows_path(log)+'\nbroker_pid='+str(pid)+'\nbroker_start='+str(start)+'\n').encode('utf-8','strict')
    if len(value)>4095:raise ValueError('Configuration exceeds reader bound')
    return value


def write_public_config(args):
    record=bind_ready(args.ready,args.nonce,'broker',args.exe,args.exe_windows,args.prefix,args.display,args.parent)
    source=args.prefix/'drive_c/Program Files/Netease/GameViewer/bin/GameViewerServer.exe'
    info=source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():raise ValueError('Audited source image missing/foreign/symlink')
    if args.output!=args.prefix/'drive_c/Program Files/FreeRDP/uu-input-public-session.conf':raise ValueError('Config is outside exact C:-mapped FreeRDP path')
    if args.source_windows!=r'C:\Program Files\Netease\GameViewer\bin\GameViewerServer.exe':raise ValueError('Config source differs from audited module mapping')
    value=public_config_bytes(record,args.bootstrap,args.broker_ready_windows,args.source_windows,args.log_windows)
    temporary=args.output.with_name(args.output.name+'.'+str(os.getpid())+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as out:out.write(value);out.flush();os.fsync(out.fileno())
        if process_identity(record['linux_identity']['pid'])!=record['linux_identity'] or process_identity(args.parent)!=record['bridge_identity']:
            raise ValueError('Config broker/bridge identity changed')
        os.replace(temporary,args.output)
    finally:
        if temporary.exists():temporary.unlink()
    return 0


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--mark-stopped',action='store_true')
    ap.add_argument('--write-public-config',action='store_true')
    ap.add_argument('--fresh-public-log',action='store_true');ap.add_argument('--archive',type=Path)
    ap.add_argument('--bootstrap');ap.add_argument('--broker-ready-windows')
    ap.add_argument('--source-windows');ap.add_argument('--log-windows')
    ap.add_argument('--ready',type=Path);ap.add_argument('--nonce',required=True)
    ap.add_argument('--role',choices=('broker','sdl'));ap.add_argument('--exe',type=Path)
    ap.add_argument('--exe-windows');ap.add_argument('--prefix',required=True,type=Path)
    ap.add_argument('--display');ap.add_argument('--parent',required=True,type=int)
    ap.add_argument('--output',required=True,type=Path);ap.add_argument('--timeout',type=float,default=15)
    a=ap.parse_args()
    if not re.fullmatch('[0-9a-f]{64}',a.nonce) or not a.prefix.is_absolute() or a.parent<=0 or not a.output.is_absolute():raise ValueError('Invalid exact startup authority')
    if sum((a.mark_stopped,a.write_public_config,a.fresh_public_log))>1:raise ValueError('Choose one helper operation')
    if a.fresh_public_log:
        if a.archive is None or not a.archive.is_absolute() or a.output.name!='uu-input-bridge.log' or not a.output.is_relative_to(a.prefix/'drive_c/users'):raise ValueError('Exact private log/archive paths required')
        return fresh_private_log(a.output,a.archive)
    if a.mark_stopped:return mark_stopped(a.prefix,a.parent,a.nonce,a.output)
    if a.ready is None or a.role is None or a.exe is None or a.exe_windows is None or a.display is None:raise ValueError('Complete typed startup arguments required')
    if not 0<a.timeout<=30 or not a.prefix.is_absolute() or not re.fullmatch(r':[1-9][0-9]*(?:\.0)?',a.display):raise ValueError('Invalid private startup bounds')
    if a.write_public_config:
        if any(value is None for value in (a.bootstrap,a.broker_ready_windows,a.source_windows,a.log_windows)):raise ValueError('Complete config authority required')
        return write_public_config(a)
    until=time.monotonic()+a.timeout;last='not yet written'
    while time.monotonic()<until:
        try:
            record=bind_ready(a.ready,a.nonce,a.role,a.exe,a.exe_windows,a.prefix,a.display,a.parent)
            fd=os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as out:json.dump(record,out);out.write('\n')
            print(record['windows_pid'],record['windows_creation_filetime'],record['linux_identity']['pid'],record['linux_identity']['start'])
            return 0
        except FileNotFoundError:last='ready/process not yet present'
        except ValueError as error:last=str(error)
        time.sleep(.05)
    raise RuntimeError('Typed Wine readiness failed: '+last)

if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,RuntimeError) as error:
        print('ERROR: '+str(error),file=sys.stderr);sys.exit(1)
