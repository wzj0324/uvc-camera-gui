import ctypes as c
from ctypes import wintypes as w
import json, subprocess, struct, datetime, pathlib, sys

VID, PID = 0x32e4, 0x3415
BASE = pathlib.Path(__file__).resolve().parent
OUT = BASE / 'docs' / 'usb_link_diagnostic.json'
ps = r'''$d = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match '^USB\\VID_32E4&PID_3415\\' }); if ($d.Count -ne 1) { throw "Expected exactly one composite USB device, got $($d.Count)" }; $id=$d[0].InstanceId; $p=Get-PnpDeviceProperty -InstanceId $id; [pscustomobject]@{instance_id=$id; parent=($p | Where-Object KeyName -eq 'DEVPKEY_Device_Parent').Data; address=($p | Where-Object KeyName -eq 'DEVPKEY_Device_Address').Data; location=($p | Where-Object KeyName -eq 'DEVPKEY_Device_LocationInfo').Data} | ConvertTo-Json -Compress'''
class GUID(c.Structure):
    _fields_ = [('Data1', w.DWORD), ('Data2', w.WORD), ('Data3', w.WORD), ('Data4', c.c_ubyte * 8)]
class DEVINFO(c.Structure):
    _fields_ = [('cbSize', w.DWORD), ('ClassGuid', GUID), ('DevInst', w.DWORD), ('Reserved', c.c_size_t)]
class IFACE(c.Structure):
    _fields_ = [('cbSize', w.DWORD), ('InterfaceClassGuid', GUID), ('Flags', w.DWORD), ('Reserved', c.c_size_t)]
setup = c.WinDLL('setupapi', use_last_error=True)
kernel = c.WinDLL('kernel32', use_last_error=True)
setup.SetupDiGetClassDevsW.argtypes = [c.POINTER(GUID), w.LPCWSTR, w.HWND, w.DWORD]
setup.SetupDiGetClassDevsW.restype = w.HANDLE
setup.SetupDiEnumDeviceInterfaces.argtypes = [w.HANDLE, c.c_void_p, c.POINTER(GUID), w.DWORD, c.POINTER(IFACE)]
setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [w.HANDLE, c.POINTER(IFACE), c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.POINTER(DEVINFO)]
setup.SetupDiGetDeviceInstanceIdW.argtypes = [w.HANDLE, c.POINTER(DEVINFO), w.LPWSTR, w.DWORD, c.POINTER(w.DWORD)]
setup.SetupDiDestroyDeviceInfoList.argtypes = [w.HANDLE]
kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
kernel.CreateFileW.restype = w.HANDLE
kernel.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, c.c_void_p, w.DWORD, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
kernel.CloseHandle.argtypes = [w.HANDLE]
INVALID = c.c_void_p(-1).value

def hub_path(parent):
    guid = GUID(0xf18a0e88, 0xc30c, 0x11d0, (c.c_ubyte * 8)(0x88,0x15,0x00,0xa0,0xc9,0x06,0xbe,0xd8))
    info = setup.SetupDiGetClassDevsW(c.byref(guid), None, None, 0x12)
    if info == INVALID: raise c.WinError(c.get_last_error())
    try:
        for i in range(1024):
            iface = IFACE(); iface.cbSize = c.sizeof(iface)
            if not setup.SetupDiEnumDeviceInterfaces(info, None, c.byref(guid), i, c.byref(iface)):
                if c.get_last_error() == 259: break
                raise c.WinError(c.get_last_error())
            needed = w.DWORD()
            setup.SetupDiGetDeviceInterfaceDetailW(info, c.byref(iface), None, 0, c.byref(needed), None)
            buf = c.create_string_buffer(needed.value)
            c.cast(buf, c.POINTER(w.DWORD))[0] = 8 if c.sizeof(c.c_void_p) == 8 else 6
            dev = DEVINFO(); dev.cbSize = c.sizeof(dev)
            if not setup.SetupDiGetDeviceInterfaceDetailW(info, c.byref(iface), buf, len(buf), c.byref(needed), c.byref(dev)): raise c.WinError(c.get_last_error())
            name = c.create_unicode_buffer(1024)
            if not setup.SetupDiGetDeviceInstanceIdW(info, c.byref(dev), name, len(name), None): raise c.WinError(c.get_last_error())
            if name.value.upper() == parent.upper(): return c.wstring_at(c.addressof(buf) + 4)
        raise RuntimeError('Parent hub interface not found: ' + str(parent))
    finally: setup.SetupDiDestroyDeviceInfoList(info)

def ioctl(handle, code, data):
    buf = c.create_string_buffer(data, max(len(data), 4096))
    got = w.DWORD()
    if not kernel.DeviceIoControl(handle, code, buf, len(data), buf, len(buf), c.byref(got), None): raise c.WinError(c.get_last_error())
    return buf.raw[:got.value]

def main():
    result = {'timestamp': datetime.datetime.now().astimezone().isoformat(), 'method': 'read-only hub IOCTL; no video stream'}
    try:
        result.update(json.loads(subprocess.check_output(['powershell', '-NoProfile', '-Command', ps], text=True, encoding='utf-8-sig')))
        port = int(result['address']); path = hub_path(result['parent']); result['hub_interface_path'] = path
        h = kernel.CreateFileW(path, 0, 3, None, 3, 0, None)
        if h == INVALID: raise c.WinError(c.get_last_error())
        try:
            raw = ioctl(h, 0x220448, struct.pack('<I', port) + bytes(4092))
            if len(raw) < 35: raise RuntimeError('EX response too short: %d' % len(raw))
            vid, pid = struct.unpack_from('<HH', raw, 12)
            speed = raw[23]
            result['ex'] = {'bytes_returned':len(raw), 'connection_index':struct.unpack_from('<I', raw)[0], 'vendor_id':f'{vid:04X}', 'product_id':f'{pid:04X}', 'bcdUSB':f'{struct.unpack_from("<H", raw, 6)[0]:04X}', 'speed_raw':speed, 'speed':{0:'low',1:'full',2:'high',3:'super'}.get(speed,'unknown'), 'connection_status_raw':struct.unpack_from('<I',raw,31)[0], 'raw_prefix_hex':raw[:36].hex()}
            if (vid,pid) != (VID,PID): raise RuntimeError('Hub port VID/PID does not match target')
            try:
                v2 = ioctl(h, 0x22045c, struct.pack('<IIII', port,16,4,0))
                if len(v2)<16: raise RuntimeError('V2 response too short')
                index,length,protocols,flags=struct.unpack_from('<IIII',v2)
                result['v2']={'bytes_returned':len(v2),'connection_index':index,'length':length,'supported_usb_protocols_raw':protocols,'flags_raw':flags,'operating_at_superspeed_or_higher':bool(flags&1),'operating_at_superspeedplus_or_higher':bool(flags&4),'raw_hex':v2[:16].hex()}
            except Exception as e: result['v2_error']=str(e)
            if result['ex']['connection_index'] != port or result['ex']['connection_status_raw'] != 1: raise RuntimeError('Port index mismatch or device is not connected')
            result['conclusion'] = {'low':'USB 1.x low speed','full':'USB 1.x full speed','high':'USB 2.0 high speed','super':'USB 3.x SuperSpeed'}.get(result['ex']['speed'],'unknown')
            if result.get('v2',{}).get('operating_at_superspeedplus_or_higher'): result['conclusion']='USB 3.x SuperSpeedPlus'
            elif result.get('v2',{}).get('operating_at_superspeed_or_higher'): result['conclusion']='USB 3.x SuperSpeed'
        finally: kernel.CloseHandle(h)
    except Exception as e: result['error'] = str(e)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2)); print('JSON:',OUT)
    return 1 if 'error' in result else 0
if __name__ == '__main__': sys.exit(main())

