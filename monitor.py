# -*- coding: utf-8 -*-
"""
Сбор данных для виджета:
  • температура и загрузка CPU
  • температура и загрузка GPU
  • заряд Bluetooth-устройств (через свойство PnP "Battery Level", Win 10+)

Источники температуры:
  1. LibreHardwareMonitor (веб-сервер на http://127.0.0.1:8085) — самый точный.
     Включается в LHM: Опции -> Веб-сервер (порт 8085), запускать от администратора.
  2. NVIDIA: pynvml (если установлен драйвер/модуль).
  3. Резерв: ACPI Thermal Zone (примерная температура, есть не на всех машинах).
"""

import ctypes
import json
import re
import subprocess
import time
import urllib.request
import uuid
from ctypes import wintypes

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

# ---------------------------------------------------------------- Загрузка CPU

def cpu_load():
    if psutil:
        try:
            return float(psutil.cpu_percent(interval=None))
        except Exception:
            pass
    return None

# ------------------------------------------------- LibreHardwareMonitor (LHM)

LHM_URL = "http://127.0.0.1:8085/data.json"

_lhm_cache = {"t": 0.0, "v": None, "ok": False}


def _lhm_sensors():
    now = time.time()
    fresh = now - _lhm_cache["t"]
    if _lhm_cache["ok"] and fresh < 1.5:
        return _lhm_cache["v"]
    if not _lhm_cache["ok"] and fresh < 8.0:
        return []
    data = None
    try:
        with urllib.request.urlopen(LHM_URL, timeout=0.6) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        data = None
    sensors = []
    if data:
        for parent, node in _walk(data):
            if _node_type(node):
                sensors.append((parent or "", node))
    _lhm_cache.update(t=now, v=sensors, ok=bool(data))
    return sensors


def _node_name(node):
    return node.get("Name") or node.get("Text") or ""


def _node_type(node):
    return node.get("SensorType") or node.get("Type") or ""


def _node_value(node):
    return node.get("Value", node.get("RawValue", ""))


def _walk(node, hardware=""):
    """Обходит дерево, возвращая (имя_железа, узел) — как в LHM, так и в старом формате."""
    name = _node_name(node)
    if node.get("HardwareId"):
        hardware = name
    yield hardware, node
    for child in node.get("Children") or []:
        yield from _walk(child, hardware)


_CPU_PARENTS = ("cpu", "ryzen", "core i", "athlon", "pentium", "celeron")
_GPU_PARENTS = ("nvidia", "geforce", "radeon", "arc", "uhd", "iris", "ati")


def _has(text, words):
    text = (text or "").lower()
    return any(w in text for w in words)


def _num(node):
    """Парсит значения вида '53,0 °C', '17,6 %', '43.0'."""
    import re
    text = str(_node_value(node) or "").replace(",", ".")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None


def _pick(sensors, stype, name_words=(), parent_words=(), exclude_words=()):
    best = None
    for parent, node in sensors:
        if _node_type(node) != stype:
            continue
        name = _node_name(node)
        if _has(name, exclude_words) or _has(parent, exclude_words):
            continue
        if (name_words and _has(name, name_words)) or (not name_words and _has(parent, parent_words)):
            val = _num(node)
            if val is not None:
                if best is None or len(_node_name(node)) < len(_node_name(best[1])):
                    best = (val, node)
    if best is None and parent_words:
        for parent, node in sensors:
            if _node_type(node) == stype and _has(parent, parent_words):
                val = _num(node)
                if val is not None:
                    return val
    return best[0] if best else None


def _lhm_values():
    sensors = _lhm_sensors()
    if not sensors:
        return {}

    cpu_temp = _pick(sensors, "Temperature", name_words=("cpu",))
    if cpu_temp is None:
        cpu_temp = _pick(sensors, "Temperature", parent_words=_CPU_PARENTS)

    gpu_temp = _pick(sensors, "Temperature", name_words=("gpu",))
    if gpu_temp is None:
        gpu_temp = _pick(sensors, "Temperature", parent_words=_GPU_PARENTS,
                         exclude_words=_CPU_PARENTS)

    gpu_load = _pick(sensors, "Load", name_words=("gpu core", "gpu"))
    if gpu_load is None:
        gpu_load = _pick(sensors, "Load", parent_words=_GPU_PARENTS,
                         exclude_words=_CPU_PARENTS)

    out = {"cpu_temp": cpu_temp, "gpu_temp": gpu_temp, "gpu_load": gpu_load}
    return {k: v for k, v in out.items() if v is not None}

# --------------------------------------------------------------------- NVIDIA

_nvml_state = None  # None / module / "failed"


def _nvidia():
    global _nvml_state
    if _nvml_state == "failed":
        return None
    if _nvml_state is None:
        try:
            import pynvml
            pynvml.nvmlInit()
            _nvml_state = pynvml
        except Exception:
            _nvml_state = "failed"
            return None
    try:
        handle = _nvml_state.nvmlDeviceGetHandleByIndex(0)
        temp = _nvml_state.nvmlDeviceGetTemperature(handle, _nvml_state.NVML_TEMPERATURE_GPU)
        util = _nvml_state.nvmlDeviceGetUtilizationRates(handle)
        return float(temp), float(util.gpu)
    except Exception:
        return None

# --------------------------------------------- Резерв: ACPI Thermal Zone (WMI)

_thermal_cache = {"t": 0.0, "v": None}


def thermal_zone_temp():
    now = time.time()
    if now - _thermal_cache["t"] < 20:
        return _thermal_cache["v"]
    val = None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
             "| Measure-Object CurrentTemperature -Average).Average"],
            capture_output=True, text=True, timeout=6,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.strip()
        val = float(out) / 10.0 - 273.15
        if not (0.0 < val < 120.0):
            val = None
    except Exception:
        val = None
    _thermal_cache.update(t=now, v=val)
    return val

# ------------------------------------------------------- Bluetooth-батареи PnP

_cfg32 = ctypes.WinDLL("cfgmgr32")


class _PROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_byte * 16), ("pid", wintypes.DWORD)]


def _key(guid, pid):
    return _PROPKEY((ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(guid).bytes_le), pid)


DEVPKEY_Device_BatteryLevel = _key("104ea319-6ee2-4701-bd47-8ddbf425bbe5", 2)  # BYTE, 0..100
DEVPKEY_Device_FriendlyName = _key("a45c254e-df1c-4efd-8020-67d146a850e0", 14)
DEVPKEY_Device_ClassGuid = _key("a45c254e-df1c-4efd-8020-67d146a850e0", 1)

BATTERY_CLASS_GUID = "72631e54-78a4-4d32-b14a-477c08b10182"  # класс Battery (внутр. АКБ)
BT_CLASS_GUID = "e0cbf06c-cd8b-4647-bb8a-263b43f0f974"       # класс Bluetooth

DEVPROP_TYPE_STRING = 18
DEVPROP_TYPE_UINT32 = 7
DEVPROP_TYPE_BYTE = 3
CM_GETIDLIST_FILTER_ENUMERATOR = 2

_setupapi = ctypes.WinDLL("setupapi")
DIGCF_PRESENT = 0x2
DIGCF_ALLCLASSES = 0x4
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", ctypes.c_byte * 16),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
    ]


_SetupDiGetClassDevsW = _setupapi.SetupDiGetClassDevsW
_SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                  wintypes.HWND, wintypes.DWORD]
_SetupDiGetClassDevsW.restype = ctypes.c_void_p

_SetupDiEnumDeviceInfo = _setupapi.SetupDiEnumDeviceInfo
_SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(_SP_DEVINFO_DATA)]
_SetupDiEnumDeviceInfo.restype = wintypes.BOOL

_SetupDiDestroyDeviceInfoList = _setupapi.SetupDiDestroyDeviceInfoList
_SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
_SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

_CM_Get_Device_IDW = _cfg32.CM_Get_Device_IDW
_CM_Get_Device_IDW.argtypes = [wintypes.DWORD, ctypes.c_void_p,
                               wintypes.ULONG, wintypes.ULONG]
_CM_Get_Device_IDW.restype = wintypes.ULONG


def _device_ids(class_guid=None, present=True):
    """PnP-устройства через SetupAPI (надёжно). Можно фильтровать по классу
    и отключить фильтр присутствия (тогда видны и офлайн-устройства)."""
    flags = DIGCF_ALLCLASSES | (DIGCF_PRESENT if present else 0)
    guid_addr = 0
    if class_guid:
        guid_buf = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(class_guid).bytes_le)
        guid_addr = ctypes.addressof(guid_buf)
    result = []
    handle = _SetupDiGetClassDevsW(guid_addr, None, None, flags)
    if not handle or handle == INVALID_HANDLE_VALUE:
        return result
    try:
        index = 0
        data = _SP_DEVINFO_DATA()
        data.cbSize = ctypes.sizeof(data)
        while _SetupDiEnumDeviceInfo(handle, index, ctypes.byref(data)):
            index += 1
            buf = ctypes.create_unicode_buffer(512)
            if _CM_Get_Device_IDW(data.DevInst, buf, 512, 0) == 0:
                result.append(buf.value)
    finally:
        _SetupDiDestroyDeviceInfoList(handle)
    return result

_ListW = _cfg32.CM_Get_Device_ID_ListW
_ListW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                   ctypes.POINTER(wintypes.ULONG), wintypes.ULONG]
_ListW.restype = wintypes.ULONG
_LocateW = _cfg32.CM_Locate_DevNodeW
_LocateW.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG]
_LocateW.restype = wintypes.ULONG

_GetPropW = _cfg32.CM_Get_DevNode_PropertyW
_GetPropW.argtypes = [wintypes.DWORD, ctypes.POINTER(_PROPKEY),
                      ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p,
                      ctypes.POINTER(wintypes.ULONG), wintypes.ULONG]
_GetPropW.restype = wintypes.ULONG


def _get_prop(devnode, key, want_type):
    """Читает свойство узла. Windows может вернуть некритичный код ошибки,
    но при этом корректно заполнить буфер — поэтому коды не проверяем."""
    ptype = wintypes.ULONG(0)
    size = wintypes.ULONG(0)
    _GetPropW(devnode, ctypes.byref(key), ctypes.byref(ptype), None,
              ctypes.byref(size), 0)
    if size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    _GetPropW(devnode, ctypes.byref(key), ctypes.byref(ptype), buf,
              ctypes.byref(size), 0)
    if ptype.value != want_type or size.value == 0:
        return None
    return buf.raw[:size.value]


def _prop_string(raw):
    return raw.decode("utf-16-le", "ignore").rstrip("\x00") if raw else ""


def _clean_name(name):
    name = re.sub(r"\s+(Hands-Free AG|Hands-Free|Avrcp Transport)$", "", name,
                  flags=re.IGNORECASE).strip()
    if len(name) > 38:
        name = name[:37] + "…"
    return name


# корневые узлы спаренных Bluetooth-устройств: BTHENUM\DEV_<MAC>\..., BTHLE\DEV_<MAC>\...
_ROOT_RE = re.compile(r"^(BTHENUM|BTHLE)\\DEV_([0-9A-Fa-f]{12})\\", re.I)
_MAC_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{12}(?![0-9A-Fa-f])")


def _scan_bluetooth(connected_macs=None):
    """[{name, level, online}] — онлайн-устройства сверху, офлайн в конце.

    Статус подключения (без WinRT, быстро):
      • классический Bluetooth (наушники и т.п.) — BluetoothFindFirstDevice,
        флаг fConnected (bluetoothapis, ~1 мс);
      • BLE-устройства — наличие активного HID-узла (HID\\{00001812-…}),
        который Windows создаёт только при подключении;
      • запасной признак — читаемый уровень батареи (отдаётся только
        подключёнными устройствами).
    """
    present_ids = _device_ids(present=True)

    # BLE HID-узлы, присутствующие сейчас (признак подключения BLE-устройства)
    hid_macs = set()
    for dev_id in present_ids:
        if not dev_id.upper().startswith("HID\\"):
            continue
        for mac in _MAC_RE.findall(dev_id.upper()):
            hid_macs.add(mac)

    # классический Bluetooth: fConnected из bluetoothapis
    classic = _bt_classic()          # {MAC: fConnected}

    # все спаренные устройства (включая офлайн) — по классу Bluetooth
    paired = {}
    for dev_id in _device_ids(class_guid=BT_CLASS_GUID, present=False):
        m = _ROOT_RE.match(dev_id)
        if not m:
            continue
        mac = m.group(2).upper()
        if mac in paired:
            continue
        dn = wintypes.DWORD(0)
        if _LocateW(ctypes.byref(dn), dev_id, 0) != 0:
            continue
        name = _prop_string(_get_prop(dn.value, DEVPKEY_Device_FriendlyName,
                                      DEVPROP_TYPE_STRING))
        paired[mac] = _clean_name(name) if name else ""

    # заряд отдают только подключённые устройства
    batteries = []
    for dev_id in present_ids:
        if "bth" not in dev_id.lower():
            continue
        dn = wintypes.DWORD(0)
        if _LocateW(ctypes.byref(dn), dev_id, 0) != 0:
            continue
        raw = _get_prop(dn.value, DEVPKEY_Device_BatteryLevel, DEVPROP_TYPE_BYTE)
        if raw is None or len(raw) < 1 or not (1 <= raw[0] <= 100):
            continue
        name = _prop_string(_get_prop(dn.value, DEVPKEY_Device_FriendlyName,
                                      DEVPROP_TYPE_STRING))
        batteries.append((set(_MAC_RE.findall(dev_id.upper())), raw[0],
                          _clean_name(name) if name else ""))

    devices = []
    for mac, root_name in paired.items():
        online = bool(classic.get(mac)) or mac in hid_macs
        level, name = None, root_name
        if online:
            for macs, lvl, bname in batteries:
                if mac in macs:
                    level = lvl
                    if bname:
                        name = bname
                    break
        devices.append({"name": name or "Устройство Bluetooth",
                        "level": level, "online": online})
    devices.sort(key=lambda d: (not d["online"], d["name"].lower()))
    return devices


# ------------------------------------------------- BluetoothFindFirstDevice

class _BtSearch(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("fReturnAuthenticated", wintypes.BOOL),
        ("fReturnRemembered", wintypes.BOOL),
        ("fReturnUnknown", wintypes.BOOL),
        ("fReturnConnected", wintypes.BOOL),
        ("fIssueQuery", wintypes.BOOL),
        ("cTimeoutMultiplier", ctypes.c_ubyte),
        ("hRadio", wintypes.HANDLE),
    ]


class _SysTime(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ushort) for n in
                ("wYear", "wMonth", "wDayOfWeek", "wDay",
                 "wHour", "wMinute", "wSecond", "wMilliseconds")]


class _BtInfo(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("Address", ctypes.c_ulonglong),
        ("ulClassofDevice", wintypes.ULONG),
        ("fConnected", wintypes.BOOL),
        ("fRemembered", wintypes.BOOL),
        ("fAuthenticated", wintypes.BOOL),
        ("stLastSeen", _SysTime),
        ("stLastUsed", _SysTime),
        ("szName", ctypes.c_wchar * 248),
    ]


_bthprops = ctypes.WinDLL("bthprops.cpl")
_FindFirstDevice = _bthprops.BluetoothFindFirstDevice
_FindFirstDevice.argtypes = [ctypes.POINTER(_BtSearch), ctypes.POINTER(_BtInfo)]
_FindFirstDevice.restype = wintypes.HANDLE
_FindNextDevice = _bthprops.BluetoothFindNextDevice
_FindNextDevice.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BtInfo)]
_FindNextDevice.restype = wintypes.BOOL
_BtClose = ctypes.windll.kernel32.CloseHandle
_BtClose.argtypes = [wintypes.HANDLE]
_BtClose.restype = wintypes.BOOL


def _bt_classic():
    """{MAC: fConnected} — классические Bluetooth-устройства (быстро)."""
    out = {}
    params = _BtSearch()
    params.dwSize = ctypes.sizeof(params)
    params.fReturnAuthenticated = True
    params.fReturnRemembered = True
    params.fIssueQuery = False            # только кэш радио — без опроса эфира
    info = _BtInfo()
    info.dwSize = ctypes.sizeof(info)
    handle = _FindFirstDevice(ctypes.byref(params), ctypes.byref(info))
    if not handle:
        return out
    try:
        while True:
            mac = format(info.Address, "012X")
            out[mac] = bool(info.fConnected)
            if not _FindNextDevice(handle, ctypes.byref(info)):
                break
    finally:
        _BtClose(handle)
    return out


_bt_cache = {"t": 0.0, "v": []}


def bt_devices(timeout=None):
    """[{name, level, online}] — скан быстрый, кэш на 5 секунд."""
    now = time.time()
    if now - _bt_cache["t"] < 5.0:
        return _bt_cache["v"]
    try:
        result = _scan_bluetooth()
    except Exception:
        result = _bt_cache["v"]
    _bt_cache.update(t=now, v=result)
    return result


# -------------------------------------------------------------------- Сводка

def snapshot():
    """Все данные одним вызовом."""
    lhm = _lhm_values()
    data = {
        "cpu_temp": lhm.get("cpu_temp"),
        "cpu_load": cpu_load(),
        "gpu_temp": None,
        "gpu_load": lhm.get("gpu_load"),
        "bt": bt_devices(),
    }
    nv = _nvidia()
    if nv:
        data["gpu_temp"], data["gpu_load"] = nv
    else:
        data["gpu_temp"] = lhm.get("gpu_temp")
    if data["cpu_temp"] is None:
        data["cpu_temp"] = thermal_zone_temp()
    for k in ("cpu_temp", "gpu_temp"):
        if data[k] is not None:
            data[k] = round(data[k])
    for k in ("cpu_load", "gpu_load"):
        if data[k] is not None:
            data[k] = int(round(data[k]))
    return data


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
