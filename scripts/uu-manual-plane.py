#!/usr/bin/env python3
"""Present only the authenticated SDL relay on the private root; keep UU mapped."""
import argparse
import ctypes as C
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time
from Xlib import display as xdisplay
D,U,I=C.c_void_p,C.c_ulong,C.c_int
class Visual(C.Structure):
    _fields_ = [('ext_data',D),('visualid',U),('klass',I),('red_mask',U),('green_mask',U),('blue_mask',U),('bits_per_rgb',I),('map_entries',I)]

class Attributes(C.Structure):
    _fields_ = [('x',I),('y',I),('width',I),('height',I),('border',I),('depth',I),('visual',C.POINTER(Visual)),('root',U),('klass',I),('bit_gravity',I),('win_gravity',I),('backing_store',I),('backing_planes',U),('backing_pixel',U),('save_under',I),('colormap',U),('map_installed',I),('map_state',I),('all_event_masks',C.c_long),('your_event_mask',C.c_long),('do_not_propagate',C.c_long),('override_redirect',I),('screen',D)]

def bind(lib,name,args,result):
    f=getattr(lib,name);f.argtypes=args;f.restype=result;return f

def attributes(display,window):
    a=Attributes()
    if not x.XGetWindowAttributes(display,window,C.byref(a)):raise RuntimeError('X attributes unavailable')
    return a

class Error(C.Structure):
    _fields_=[('type',I),('display',D),('resource',U),('serial',U),('code',C.c_ubyte),('request',C.c_ubyte),('minor',C.c_ubyte)]
class Direct(C.Structure):
    _fields_=[(n,C.c_short) for n in ['red','red_mask','green','green_mask','blue','blue_mask','alpha','alpha_mask']]
class Color(C.Structure):
    _fields_=[(n,C.c_ushort) for n in ['red','green','blue','alpha']]
class Format(C.Structure):
    _fields_=[('id',U),('type',I),('depth',I),('direct',Direct),('colormap',U)]


def private_record(path):
    s=path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o077 or s.st_size>8192:
        raise ValueError('Private plane record ownership/type/size mismatch')
    return json.loads(path.read_text())


def write_record(path,value):
    temporary=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as out:json.dump(value,out);out.write('\n')
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


def same_owner(a,b):
    return all(a[n]==b[n] for n in ('uid','cgroup','netns','mntns'))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--display',required=True);ap.add_argument('--prefix',required=True,type=Path)
    ap.add_argument('--x-pid',type=int);ap.add_argument('--x-start')
    ap.add_argument('--parent',required=True,type=int);ap.add_argument('--nonce')
    ap.add_argument('--ready',required=True,type=Path);ap.add_argument('--target',type=Path)
    ap.add_argument('--prefix-stopped',type=Path);ap.add_argument('--fps',type=int,default=60)
    ap.add_argument('--check-ready',action='store_true')
    ap.add_argument('--check-redirected',action='store_true');ap.add_argument('--expected-owner',type=int)
    args=ap.parse_args()
    if args.check_ready or args.check_redirected:
        previous=private_record(args.ready)
        if args.x_pid is None:args.x_pid=previous['xserver']['pid']
        if args.x_start is None:args.x_start=previous['xserver']['start']
        if args.nonce is None:args.nonce=previous['nonce']
    elif args.x_pid is None or args.x_start is None or args.nonce is None or args.target is None or args.prefix_stopped is None:
        raise ValueError('Complete private owner startup is required')
    module_path=Path(__file__).with_name('uu-full-ready.py')
    spec=importlib.util.spec_from_file_location('uurb_ready',module_path)
    ready=importlib.util.module_from_spec(spec);spec.loader.exec_module(ready)
    if not ready.re.fullmatch(r':[1-9][0-9]*',args.display) or os.environ.get('DISPLAY')!=args.display or os.environ.get('WINEPREFIX')!=str(args.prefix) or not 1<=args.fps<=120 or not ready.re.fullmatch('[0-9a-f]{64}',args.nonce):
        raise ValueError('Invalid private plane display/prefix/cadence/startup')
    parent=ready.process_identity(args.parent);xserver=ready.process_identity(args.x_pid)
    if parent['uid']!=os.getuid() or not same_owner(parent,xserver) or xserver['start']!=args.x_start or (Path('/proc')/str(args.x_pid)/'comm').read_text().strip()!='Xvfb':
        raise ValueError('Owned X server/bridge identity mismatch')
    if args.check_ready or args.check_redirected:
        if args.ready.with_name(args.ready.name+'.failure').exists():raise ValueError('Manual owner failed')
        record=private_record(args.ready)
        if record['nonce']!=args.nonce or record['display']!=args.display or record['prefix']!=str(args.prefix) or record['phase']!=('REDIRECTED' if args.check_redirected else 'ACTIVE') or record['xserver']!=xserver or record['bridge']!=parent:
            raise ValueError('Manual owner is not active/fresh for this bridge')
        actual=ready.process_identity(record['owner']['pid'])
        if actual!=record['owner'] or not same_owner(actual,parent) or (args.expected_owner is not None and actual['pid']!=args.expected_owner):raise ValueError('Manual owner changed')
        if not args.check_redirected:
            if not 0<=time.monotonic()-record['last_paint']<=3:raise ValueError('Manual repaint stale')
            if ready.process_identity(record['sdl']['pid'])!=record['sdl']:raise ValueError('Manual SDL changed')
        return 0
    own=ready.process_identity(os.getpid())
    record={'nonce':args.nonce,'display':args.display,'prefix':str(args.prefix),'owner':own,'bridge':parent,'xserver':xserver,'phase':'STARTING','requested_fps':args.fps}
    if not same_owner(own,parent):raise ValueError('Plane outside bridge ownership')
    global x
    x=C.CDLL('libX11.so.6');comp=C.CDLL('libXcomposite.so.1');render=C.CDLL('libXrender.so.1')
    for name,a,r in [('XOpenDisplay',[C.c_char_p],D),('XCloseDisplay',[D],I),('XDefaultRootWindow',[D],U),('XSync',[D,I],I),('XGetWindowAttributes',[D,U,C.POINTER(Attributes)],I),('XQueryTree',[D,U,C.POINTER(U),C.POINTER(U),C.POINTER(C.POINTER(U)),C.POINTER(C.c_uint)],I),('XFree',[D],I),('XCreatePixmap',[D,U,C.c_uint,C.c_uint,C.c_uint],U),('XFreePixmap',[D,U],I)]:bind(x,name,a,r)
    for name,a,r in [('XCompositeQueryVersion',[D,C.POINTER(I),C.POINTER(I)],I),('XCompositeRedirectSubwindows',[D,U,I],None),('XCompositeUnredirectSubwindows',[D,U,I],None),('XCompositeNameWindowPixmap',[D,U],U)]:bind(comp,name,a,r)
    for name,a,r in [('XRenderFindVisualFormat',[D,D],C.POINTER(Format)),('XRenderCreatePicture',[D,U,C.POINTER(Format),U,D],U),('XRenderFreePicture',[D,U],None),('XRenderComposite',[D,I,U,U,U,I,I,I,I,I,I,C.c_uint,C.c_uint],None),('XRenderFillRectangle',[D,I,U,C.POINTER(Color),I,I,C.c_uint,C.c_uint],None)]:bind(render,name,a,r)
    errors=[];stopping=False
    handler_type=C.CFUNCTYPE(I,D,C.POINTER(Error))
    @handler_type
    def handler(display,event):
        e=event.contents;errors.append({n:int(getattr(e,n)) for n in ['code','request','minor','resource','serial']});return 0
    bind(x,'XSetErrorHandler',[handler_type],D)(handler)
    def stopped(signum,frame):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stopped);signal.signal(signal.SIGINT,stopped)
    d=x.XOpenDisplay(args.display.encode());properties=None;redirected=False;root_picture=cache_picture=cache=0;last_paint=0;last_report=0;failed=None;target=None;canvas=None;resize_until=None
    def sync():
        x.XSync(d,0)
        if errors:raise RuntimeError('Private X error '+json.dumps(errors))
    def black(width,height):
        c=Color(0,0,0,65535);render.XRenderFillRectangle(d,1,root_picture,C.byref(c),0,0,width,height);sync()
    def source_window(expected,width,height):
        process=ready.process_identity(expected['linux_identity']['pid'])
        if process!=expected['linux_identity'] or not same_owner(process,parent):raise ValueError('SDL identity changed')
        proc=Path('/proc')/str(process['pid']);environment=(proc/'environ').read_bytes().split(b'\0')
        if ('WINEPREFIX='+str(args.prefix)).encode() not in environment or ('DISPLAY='+args.display).encode() not in environment:raise ValueError('SDL prefix/display changed')
        clients=properties.screen().root.get_full_property(properties.intern_atom('_NET_CLIENT_LIST'),0)
        if not clients or clients.format!=32 or clients.property_type!=properties.intern_atom('WINDOW') or len(clients.value)>512 or len(set(clients.value))!=len(clients.value):raise ValueError('Managed client list invalid')
        matches=[]
        for value in clients.value:
            window=properties.create_resource_object('window',int(value));pid=window.get_full_property(properties.intern_atom('_NET_WM_PID'),0)
            if not pid or pid.format!=32 or list(pid.value)!=[process['pid']]:continue
            title=window.get_full_property(properties.intern_atom('_NET_WM_NAME'),0);classes=window.get_wm_class()
            if not title or title.property_type!=properties.intern_atom('UTF8_STRING') or title.format!=8 or bytes(title.value)!=b'Ubuntu-Desktop-Relay' or not classes or 'sdl-freerdp.exe' not in [c.lower() for c in classes]:continue
            matches.append(int(value))
        if len(matches)!=1:raise ValueError('Expected one owned SDL relay window')
        client=matches[0];frame=client
        for depth in range(8):
            rr,pp=U(),U();children=C.POINTER(U)();count=C.c_uint()
            if not x.XQueryTree(d,frame,C.byref(rr),C.byref(pp),C.byref(children),C.byref(count)):raise ValueError('SDL frame tree unavailable')
            if children:x.XFree(children)
            sync()
            if pp.value==root:break
            if not pp.value or pp.value==frame:raise ValueError('SDL frame parent invalid')
            frame=pp.value
        else:raise ValueError('SDL frame tree depth exceeded')
        a=attributes(d,client);f=attributes(d,frame);sync()
        if a.map_state!=2 or f.map_state!=2:raise ValueError('SDL relay is not mapped')
        if (a.width,a.height)!=(width,height) or (f.x,f.y,f.width,f.height)!=(0,0,width,height):return None
        fmt=render.XRenderFindVisualFormat(d,f.visual)
        if not fmt or fmt.contents.depth!=24:raise ValueError('SDL source Visual depth unsupported')
        return frame,f,fmt
    try:
        if not d:raise ValueError('Private display unavailable')
        properties=xdisplay.Display(args.display);root=x.XDefaultRootWindow(d);a=attributes(d,root)
        major,minor=I(),I()
        if not comp.XCompositeQueryVersion(d,C.byref(major),C.byref(minor)) or (major.value,minor.value)<(0,4):raise ValueError('Composite0.4 required')
        comp.XCompositeRedirectSubwindows(d,root,1);sync();redirected=True
        fmt=render.XRenderFindVisualFormat(d,a.visual)
        if a.depth!=24 or not fmt or fmt.contents.depth!=24:raise ValueError('Root Visual depth unsupported')
        root_picture=render.XRenderCreatePicture(d,root,fmt,0,None);sync()
        record={'nonce':args.nonce,'display':args.display,'prefix':str(args.prefix),'owner':own,'bridge':parent,'xserver':xserver,'phase':'REDIRECTED','requested_fps':args.fps,'composite_version':[major.value,minor.value]}
        write_record(args.ready,record)
        while not stopping:
            if ready.process_identity(args.parent)!=parent or ready.process_identity(args.x_pid)!=xserver:raise ValueError('Bridge/X ownership changed')
            a=attributes(d,root)
            if a.depth!=24 or (a.width,a.height) not in ((1280,720),(1920,1080),(2560,1440),(3840,2160),(5120,2880)):
                raise ValueError('Root actual canvas unsupported')
            width,height=a.width,a.height
            if canvas!=(width,height):
                if cache_picture:render.XRenderFreePicture(d,cache_picture)
                if cache:x.XFreePixmap(d,cache)
                cache=x.XCreatePixmap(d,root,width,height,24);cache_picture=render.XRenderCreatePicture(d,cache,fmt,0,None);sync()
                canvas=(width,height);resize_until=time.monotonic()+3;black(width,height)
                record.update(phase='RESIZING' if target is not None else 'REDIRECTED',width=width,height=height);write_record(args.ready,record)
            if target is None:
                if not args.target.exists():time.sleep(.02);continue
                target=private_record(args.target)
                if target['role']!='sdl' or target['nonce']!=args.nonce or target['display']!=args.display or target['prefix']!=str(args.prefix) or target['bridge_identity']!=parent:raise ValueError('SDL target startup binding invalid')
            selected=source_window(target,width,height)
            if selected is None:
                if resize_until is None or time.monotonic()>=resize_until:raise ValueError('SDL fullscreen does not match current root')
                time.sleep(.01);continue
            frame,f,sourcefmt=selected
            delay=1/args.fps-(time.monotonic()-last_paint)
            if delay>0:time.sleep(delay)
            named=comp.XCompositeNameWindowPixmap(d,frame);sync();picture=render.XRenderCreatePicture(d,named,sourcefmt,0,None)
            try:
                render.XRenderComposite(d,1,picture,0,cache_picture,0,0,0,0,0,0,width,height)
                render.XRenderComposite(d,1,cache_picture,0,root_picture,0,0,0,0,0,0,width,height);sync()
            finally:
                render.XRenderFreePicture(d,picture);x.XFreePixmap(d,named);sync()
            last_paint=time.monotonic();resize_until=None
            if last_paint-last_report>=1 or record['phase']!='ACTIVE':
                record.update(phase='ACTIVE',last_paint=last_paint,width=width,height=height,sdl=target['linux_identity'],sdl_window=frame,source_visual=int(f.visual.contents.visualid))
                write_record(args.ready,record);last_report=last_paint
    except BaseException as error:
        failed=str(error)
        try:write_record(args.ready.with_name(args.ready.name+'.failure'),{'error':failed,'x_errors':errors,'monotonic':time.monotonic(),'owner':own})
        except OSError:pass
        if d and root_picture:
            try:black(*canvas)
            except BaseException:pass
    finally:
        # Bridge publishes this only after exact prefix helper quiescence.
        # Stop or failure alone never makes a live Wine desktop visible again.
        if redirected:
            record.update(phase='FAILED_HELD' if failed else 'STOPPING_HELD')
            try:write_record(args.ready,record)
            except OSError:pass
            while True:
                try:
                    marker=private_record(args.prefix_stopped)
                    if marker.get('nonce')!=args.nonce or marker.get('bridge')!=parent or marker.get('prefix')!=str(args.prefix) or marker.get('owned_wine_absent') is not True:raise ValueError('Prefix stop marker mismatch')
                    break
                except (OSError,ValueError):time.sleep(.02)
        if d:
            if cache_picture:render.XRenderFreePicture(d,cache_picture)
            if cache:x.XFreePixmap(d,cache)
            if root_picture:render.XRenderFreePicture(d,root_picture)
            if redirected:comp.XCompositeUnredirectSubwindows(d,root,1)
            x.XSync(d,0);x.XCloseDisplay(d)
        if properties:properties.close()
        if args.ready.exists():args.ready.unlink()
    return 1 if failed else 0

if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,RuntimeError) as error:
        print('ERROR: '+str(error),file=sys.stderr);sys.exit(1)
