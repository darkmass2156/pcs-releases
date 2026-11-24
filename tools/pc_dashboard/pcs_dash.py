#!/usr/bin/env python3
"""PCS dashboard: reads the Mega over USB and shows it in the browser (http://127.0.0.1:8570).

Needs Python 3 and pyserial (py -m pip install pyserial). Nothing else; the page is plain HTML, works offline.
Run:   py pcs_dash.py               (finds the CH340, opens the browser)
       py pcs_dash.py COM4 --log pcs_log.csv   (also appends every CSV row to a file)
       py pcs_dash.py --demo        (fake data, no Mega needed)

Opens the port with DTR/RTS off (no reset) and keeps it open; reconnects if the cable drops.
Switches the Mega to the CSV log on connect and back to txt on a clean exit (Ctrl+C).
Only one program can hold the COM port: close the IDE Serial Monitor and pcs_term.py first.
The page has a command box (same commands as the serial monitor) and the mode buttons (m1/m2/m3).
"""
import argparse
import collections
import csv
import glob
import json
import math
import os
import queue
import random
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hwinfo import HWiNFO
from hwinfo_out import HWiNFOOut, pc_requests, P1_BY_BIOS
from pcs_prog import Prog, fw_ok, core_stats
from daily import DailyFans, FastP1
CUBE_FW = (0, 9, 18)

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None

BAUD = 115200
CH340_VID = 0x1A86
HERE = os.path.dirname(os.path.abspath(__file__))
import pcs_paths
import diag
COLS = ["ms", "mode", "state", "T1", "T2", "T3", "Tout", "Troom", "DP_A", "DP_B", "DP_R", "DP", "Tset",
        "V2deg", "V2mm", "V2out", "fan", "pump", "rpmF1", "rpmF2", "rpmP2", "V1", "fuses", "faults", "alarms",
        "T_A", "RH_A", "T_B", "RH_B", "RH_R",   # FW 0.9.7f: case / room air
        "disp",                                  # FW 0.9.7g: case display on (1) / off (0)
        "pen",                                   # FW 0.9.9c: P2 powered (1) / off (0)
        "cubeM"]                                 # FW 0.9.18: the cube the Mega got from the PC (empty when stale)
NCOLS_OK = (25, 30, 31, 32, 33)                  # older firmware sends fewer columns
OC_P1_MIN_PCT = 68      # owner 2026-11-10: P1 >= 68 % while the Mega runs Mode 4 (BIOS W_PUMP+ is fixed at 68 % too; was 62)
OC_PC_FAN_PCT = 10      # owner 2026-11-10: PC rad + top fans at 10 % in Mode 4 (cold coolant; quiet for OC runs)
OC_FAN_GUARD_PKG_C = 90 # ...unless the CPU package or the VRM gets this hot (then the normal requests)
OC_FAN_GUARD_VRM_C = 85
CUBE_TX_S = 2.0         # cube sent to the Mega this often (FW 0.9.18 keeps it 10 s)
HISTORY = 8 * 3600          # rows kept for the chart (4 h at 2 Hz)
HISTORY_S = 4 * 3600        # chart window reloaded from the 1 s logs at start


class Hub:
    """Latest row, history, event log and the SSE subscribers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rows = []
        self.events = []
        self.info = {"connected": False, "port": None, "fw": None, "demo": False, "released": False}
        self.pc_hist = collections.deque(maxlen=4 * 3600)   # standalone: PC sensors once a second (Home / history)
        self.released = threading.Event()               # "End service": COM port handed to the Arduino IDE for flashing
        self.prog_before = None                          # program to restore on "Start service"
        self.p1_override = None                          # (pct, until) manual P1 test, POST /p1
        self.daily = DailyFans()                         # Mode 5 Daily: PC case fans (daily.py)
        self.fast_p1 = FastP1()                         # instant P1 floor on the current load (daily.py)
        self.cube_tx_at = 0.0                            # last cube<v> sent to the Mega
        self.subs = []
        self.tx = queue.Queue()
        self.pc = {"status": "starting", "v": {}}     # HWiNFO readings (hwinfo.py PICK)
        self.prog = None                                # pcs_prog.Prog (programs run from the PC)

    def publish(self, kind, data):
        msg = f"event: {kind}\ndata: {json.dumps(data)}\n\n"
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    def add_row(self, row):
        with self.lock:
            # cube NTC (Apex T_Sensor, via HWiNFO) = coolant into the PC; kept in the row so the chart and history have it
            pc = self.pc
            ts = (pc.get("v") or {}).get("t_sensor")
            fresh = pc.get("status") in ("ok", "demo") and time.time() - pc.get("t", 0) < 5
            row["Tcube"] = ts["v"] if ts and fresh else None
            row["Tcpu"] = core_stats(pc)["cpu_avg"] if fresh else None   # mean P-core temperature (chart)
            self.rows.append(row)
            del self.rows[:-HISTORY]
        self.publish("row", row)

    def add_event(self, text):
        ev = {"t": time.time(), "text": text}
        with self.lock:
            self.events.append(ev)
            del self.events[:-300]
        self.publish("event", ev)

    def set_pc(self, status, vals, detail=None):
        with self.lock:
            self.pc = {"status": status, "v": vals, "t": time.time(), "d": detail or {}, "req": getattr(self, "req", None),
                       "src": getattr(self, "pc_src", "HWiNFO")}
        self.publish("pc", self.pc)

    def set_info(self, **kw):
        with self.lock:
            self.info.update(kw)
        self.publish("info", self.info)


def num(v):
    if v == "":
        return None
    try:
        return int(v, 16) if v.startswith("0x") else (float(v) if "." in v else int(v))
    except ValueError:
        return v


def parse_row(line):
    f = line.split(",")
    if len(f) not in NCOLS_OK or not f[0].isdigit():
        return None
    row = {k: None for k in COLS}
    row.update({k: (v if k == "state" else num(v)) for k, v in zip(COLS, f)})
    row["t"] = time.time()
    return row


def load_history(hub):
    """Chart history across restarts (owner 2026-11-11): the last HISTORY_S of the 1 s logs (logs/pcs2_*.csv, today and
    yesterday, incl. *_part*.csv) go back into hub.rows, marked "hist" so the programs never take them for live data."""
    d = pcs_paths.LOGS
    t_min = time.time() - HISTORY_S
    out = []
    for back in (1, 0):
        day = time.strftime("%Y%m%d", time.localtime(time.time() - back * 86400))
        files = sorted(glob.glob(os.path.join(d, f"pcs2_{day}_part*.csv"))) + [os.path.join(d, f"pcs2_{day}.csv")]
        for fn in files:
            if not os.path.exists(fn):
                continue
            try:
                with open(fn, encoding="utf-8", newline="") as f:
                    for rec in csv.DictReader(f):
                        try:
                            t = time.mktime(time.strptime(day + " " + rec["time"], "%Y%m%d %H:%M:%S"))
                        except (KeyError, ValueError, TypeError):
                            continue
                        if t < t_min:
                            continue
                        row = {k: None for k in COLS}
                        for k in COLS:
                            v = rec.get(k)
                            if v not in (None, ""):
                                row[k] = v if k == "state" else num(v)
                        row["Tcube"] = num(rec["cube"]) if rec.get("cube") else None
                        row["Tcpu"] = num(rec["cpu_avg"]) if rec.get("cpu_avg") else None
                        row["t"], row["hist"] = t, True
                        out.append(row)
            except OSError:
                pass
    out.sort(key=lambda r: r["t"])
    with hub.lock:
        hub.rows[:0] = out[-HISTORY:]
        del hub.rows[:-HISTORY]
    return len(out)


def find_port():
    ports = list(list_ports.comports())
    for p in ports:
        if p.vid == CH340_VID:
            return p.device
    return ports[0].device if len(ports) == 1 else None


def serial_loop(hub, port_arg, log_path, stop):
    log = open(log_path, "a", encoding="utf-8") if log_path else None
    while not stop.is_set():
        if hub.released.is_set():                    # End service: port closed so the IDE can upload
            if hub.info.get("connected") or not hub.info.get("released"):
                hub.set_info(connected=False, released=True, error="Service ended: COM port free for flashing")
            time.sleep(0.5)
            continue
        name = port_arg or find_port()
        if not name:
            hub.set_info(connected=False, port=None, error="no CH340 found")
            time.sleep(2)
            continue
        try:
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = name, BAUD, 0.2
            s.dtr = False
            s.rts = False
            s.open()
        except serial.SerialException as e:
            hub.set_info(connected=False, port=name, error=f"{name} busy (IDE Serial Monitor open?)")
            hub.add_event(f"[{name}: {e}]")
            time.sleep(3)
            continue
        hub.set_info(connected=True, port=name, error=None)
        hub.add_event(f"[connected to {name}, DTR off]")
        buf = b""
        last_row = 0.0
        csv_sent = 0.0
        try:
            while not stop.is_set() and not hub.released.is_set():
                now = time.time()
                if now - last_row > 4 and now - csv_sent > 4:   # not in CSV mode yet (or just booted): ask
                    s.write(b"csv\n")
                    csv_sent = now
                while not hub.tx.empty():
                    s.write((hub.tx.get_nowait() + "\n").encode())
                buf += s.read(512)
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    row = parse_row(line)
                    if row:
                        last_row = time.time()
                        hub.add_row(row)
                        if log:
                            log.write(line + "\n")
                            log.flush()
                    elif line.startswith("ms,mode"):
                        continue
                    else:
                        if line.startswith("# PCS controller fw"):
                            hub.set_info(fw=line[2:])
                        hub.add_event(line)
            if stop.is_set() or hub.released.is_set():
                time.sleep(0.6)                      # let a program's x / m1 go out first
                while not hub.tx.empty():
                    s.write((hub.tx.get_nowait() + "\n").encode())
                s.write(b"txt\n")       # leave the Mega in its readable default
                s.flush()
                time.sleep(0.3)
                if hub.released.is_set():
                    hub.add_event(f"[service ended: {name} closed, ready for the Arduino IDE upload]")
        except serial.SerialException as e:
            hub.add_event(f"[port lost: {e}]")
        finally:
            try:
                s.close()
            except Exception:
                pass
            hub.set_info(connected=False)
        time.sleep(1)
    if log:
        log.close()


_cube = {"v": None, "t": 0.0}


def demo_detail(k):
    """Fake per-core / per-chip readings shaped like hwinfo.HWiNFO.detail (270K Plus core numbering)."""
    load = 0.5 + 0.5 * math.sin(k / 30)
    core = {}
    for n in range(24):
        p = n in (0, 1, 10, 11, 12, 13, 22, 23)
        t = (45 if p else 38) + (25 if p else 12) * load * (1 + 0.15 * math.sin(n + k / 7)) + random.uniform(-1, 1)
        core[str(n)] = {"t": round(t), "clk": 5700.0 if p else 4900.0, "eclk": round((5300 if p else 4500) * load),
                        "v": round((1.32 if p else 1.22) + random.uniform(-0.01, 0.01), 3),
                        "use": round(100 * load * (0.6 + 0.4 * math.sin(n + k / 5) ** 2), 1),
                        ("kind_P" if p else "kind_E"): 1}
    gpu_w = 120 + 160 * load
    return {
        "core": core,
        "cpu": {"pkg_t": round(50 + 22 * load), "max_t": round(56 + 25 * load), "vr_t": 44.0, "vrm_t": 44.0,
                "pkg_w": round(40 + 110 * load, 1), "ia_w": round(30 + 95 * load, 1), "sa_w": 9.5, "ring_clk": 4200.0,
                "vcore": 1.48, "ring_v": 1.21, "use": round(100 * load * 0.4, 1)},
        "gpu": {"t": round(35 + 12 * load, 1), "hot": round(43 + 16 * load, 1), "mem_j": round(32 + 8 * load),
                "v": 1.14, "hp_v": 12.27, "slot_v": 12.28, "clk": 2970.0, "mem_clk": 1312.7, "load": round(100 * load),
                "mem_load": round(40 * load), "hp_w": round(gpu_w * 0.85 / 1.4, 1), "hp_w_real": round(gpu_w * 0.85, 1),
                "slot_w": round(gpu_w * 0.15, 1), "core_w": round(gpu_w * 0.7, 1), "fb_w": round(gpu_w * 0.15, 1),
                "w": round(gpu_w, 1)},
        "gmem": {c: round(30 + 8 * load + random.uniform(-2, 2)) for c in ("A0", "A1", "B0", "B1", "C0", "C1", "D0", "D1")},
        "dimm": {"0": {"t": 28.8, "w": 1.1, "vdd": 1.53}, "2": {"t": 29.3, "w": 0.9, "vdd": 1.53}},
    }


def demo_cube(hub):
    """Demo cube temp: T2 + a bit (reservoir mixing) with a ~40 s lag (res volume), whole degrees like the Apex."""
    with hub.lock:
        t2 = hub.rows[-1]["T2"] if hub.rows else None
    if t2 is None:
        return 20.0
    now = time.time()
    if _cube["v"] is None:
        _cube["v"] = t2 + 0.6
    else:
        _cube["v"] += (t2 + 0.6 - _cube["v"]) * min(1.0, (now - _cube["t"]) / 40.0)
    _cube["t"] = now
    return float(round(_cube["v"]))


READER_STALE_S = 20.0       # reader data this old -> restart its task (the fans keep their last duty meanwhile)
READER_RETRY_S = 90.0
_rw = {"ok_at": time.time(), "tried": 0.0}


PREFS_FILE = os.path.join(pcs_paths.LOGS, "prefs.json")
PREFS_DEFAULT = {"alerts": True, "board": "", "gpu_k": 1.0}   # board: id from boards.js, "" = not chosen yet (setup screen); gpu_k: GPU power correction (shunt mod), 1.32 on this PC


_board = {}


def board_detect():
    """Motherboard maker / product from Win32_BaseBoard, once per run (PowerShell, no window)."""
    if not _board:
        try:
            import subprocess
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "$b = Get-CimInstance Win32_BaseBoard; $p = Get-CimInstance Win32_Processor | Select-Object -First 1; "
                                  "\"$($b.Manufacturer)|$($b.Product)|$($p.Name)|$($p.SocketDesignation)\""],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)).stdout.strip()
            maker, product, cpu, sock = (out.split("|") + ["", "", "", ""])[:4]
            # CPU name + the socket the firmware reports (SMBIOS type 4: "AM5", "LGA1700", but also "CPU 1" / "U3E1"),
            # the setup page derives the socket from the name when this one isn't a socket (2026-11-24)
            _board.update({"maker": maker.strip(), "product": product.strip(), "cpu": " ".join(cpu.split()),
                           "socket": sock.strip()})
        except Exception as e:
            _board.update({"maker": "", "product": "", "cpu": "", "socket": "", "error": str(e)})
    return dict(_board)


FAN_SRCS = ("cpu_t", "cpu_w", "gpu_t", "gpu_hot", "gpu_mem", "gpu_w", "load_w", "hot")


def fans_config_load():
    """fans.json next to the reader's output (standalone fan control, 2026-11-23). Missing = not set up."""
    try:
        with open(os.path.join(pcs_paths.SENSORS, "fans.json"), "rb") as f:
            cfg = json.loads(f.read())
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def fans_config_save(cfg):
    """Checks and writes fans.json (atomic). The reader (elevated) applies it within 0.5 s; values are clamped here
    and again in the reader, so a bad page can't ask for more than 0..100 %."""
    if not isinstance(cfg, dict):
        raise ValueError("config must be an object")
    num = lambda v, d, lo=-50.0, hi=1000.0: max(lo, min(hi, float(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else d
    groups = []
    for g in (cfg.get("groups") or [])[:16]:
        if not isinstance(g, dict):
            continue
        src = str(g.get("src") or "cpu_t")
        if src not in FAN_SRCS and not src.startswith("sensor:"):
            raise ValueError(f"unknown source {src}")
        lo = num(g.get("lo"), 40.0); hi = num(g.get("hi"), 80.0)
        if hi <= lo:
            hi = lo + 1
        mn = num(g.get("min"), 30.0, 0, 100); mx = num(g.get("max"), 100.0, 0, 100)
        groups.append({"id": str(g.get("id") or os.urandom(4).hex())[:40], "name": str(g.get("name") or "Group")[:40],
                       "headers": [str(h)[:120] for h in (g.get("headers") or []) if isinstance(h, str)][:32],
                       "src": src[:160], "lo": lo, "hi": hi, "min": mn, "max": max(mn, mx),
                       "relax": num(g.get("relax"), 0.4, 0.05, 20)})
    names = {str(k)[:120]: str(v)[:40] for k, v in (cfg.get("names") or {}).items() if isinstance(v, str) and v.strip()}
    out = {"enabled": bool(cfg.get("enabled")), "groups": groups, "names": names, "t": time.time()}
    idn = cfg.get("identify")
    if isinstance(idn, dict) and isinstance(idn.get("id"), str):
        out["identify"] = {"id": idn["id"][:120], "until": time.time() + max(1.0, min(10.0, num(idn.get("s"), 4.0, 1, 10)))}
    os.makedirs(pcs_paths.SENSORS, exist_ok=True)
    p = os.path.join(pcs_paths.SENSORS, "fans.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, p)
    return out


def fans_state():
    """Fans tab (2026-11-23, step 1, read-only): the sensor reader's "fans" block (who drives the headers, request /
    floor / applied per NCT control), every Super I/O header's duty and rpm, and fan_req.json as the dashboard wrote it.
    Reads only; never writes a file or a fan."""
    import re
    out = {"t": time.time(), "reader_t": None, "fans": None, "headers": {}, "req": None, "req_age": None}
    d = None
    try:
        import sensors_src
        d = sensors_src.read_raw()
    except Exception:
        d = None
    if d is None:
        try:
            with open(os.path.join(pcs_paths.SENSORS, "now.json"), "rb") as f:
                d = json.loads(f.read())
        except (OSError, ValueError):
            d = None
    if isinstance(d, dict):
        out["reader_t"] = d.get("t")
        out["fans"] = d.get("fans")
        out["ufans"] = d.get("ufans")              # standalone fan control (reader --user-fans): controls, groups, mode
        out["admin"] = d.get("admin")
        out["temps"] = [{"id": s.get("id"), "hw": s.get("hw"), "name": s.get("name"), "v": s.get("v")}   # sources for a group
                        for s in d.get("sensors") or [] if s.get("type") == "Temperature" and s.get("v") is not None]
        for s in d.get("sensors") or []:
            m = re.match(r"^/lpc/([0-9a-z]+)/\d+/(control|fan)/(\d+)$", str(s.get("id", "")))   # any Super I/O (NCT, ITE, Fintek)
            if m:
                h = out["headers"].setdefault(m.group(3), {"chip": s.get("hw")})
                h["duty" if m.group(2) == "control" else "rpm"] = s.get("v")
                h.setdefault("name", s.get("name"))
    try:
        with open(os.path.join(pcs_paths.SENSORS, "fan_req.json"), "rb") as f:
            out["req"] = json.loads(f.read())
        out["req_age"] = round(time.time() - float(out["req"].get("t", 0)), 1)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return out


def prefs_load():
    """PC-side settings from the Settings page (2026-11-22). Unknown keys are ignored."""
    p = dict(PREFS_DEFAULT)
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            p.update({k: v for k, v in json.load(f).items() if k in PREFS_DEFAULT})
    except (OSError, ValueError):
        pass
    return p


def prefs_save(upd):
    p = prefs_load()
    for k, v in (upd or {}).items():
        if k in PREFS_DEFAULT and isinstance(v, type(PREFS_DEFAULT[k])):
            p[k] = v
    os.makedirs(os.path.dirname(PREFS_FILE), exist_ok=True)
    with open(PREFS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=1)
    return p


def open_event_viewer(records):
    """Event Viewer on exactly these System log records (Analyzer button, 2026-11-22): a custom view file with an
    EventRecordID query, opened with eventvwr /v:. Record numbers only (ints), so nothing from the page reaches a
    command line."""
    d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.dirname(os.path.abspath(__file__)), "PCS", "views")
    os.makedirs(d, exist_ok=True)
    q = " or ".join(f"EventRecordID={int(r)}" for r in records)
    name = "PCS event " + ", ".join(str(int(r)) for r in records[:4]) + (" ..." if len(records) > 4 else "")
    xml = ("<ViewerConfig><QueryConfig><QueryParams><UserQuery /></QueryParams><QueryNode>"
           f"<Name>{name}</Name><Description>Opened from the PCS Analyzer</Description>"
           "<QueryList><Query Id=\"0\" Path=\"System\">"
           f"<Select Path=\"System\">*[System[({q})]]</Select></Query></QueryList>"
           "</QueryNode></QueryConfig></ViewerConfig>")
    path = os.path.join(d, f"pcs_{int(records[0])}.xml")
    with open(path, "w", encoding="utf-16") as f:
        f.write(xml)
    # eventvwr.exe asks for elevation (CreateProcess fails with 740 from the non-elevated service): start the
    # snap-in through the shell like a double-click does
    import ctypes
    win = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "mmc.exe")
    rc = ctypes.windll.shell32.ShellExecuteW(None, "open", win, f'eventvwr.msc /v:"{path}"', None, 1)
    if rc <= 32:
        raise OSError(f"ShellExecute failed ({rc})")


def reader_watchdog(hub, ok):
    """Restart the standalone sensor reader (task "PCS Sensors") when its data has been stale for READER_STALE_S
    (2026-11-22: a dead reader leaves the case fans at their last duty). Start / stop works without admin."""
    now = time.time()
    if ok:
        _rw["ok_at"] = now
        return
    if now - _rw["ok_at"] < READER_STALE_S or now - _rw["tried"] < READER_RETRY_S:
        return
    _rw["tried"] = now
    hub.add_event(f"# SENSORS reader stale for {now - _rw['ok_at']:.0f} s: restarting its task")
    try:
        import subprocess
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                          "Stop-ScheduledTask -TaskName 'PCS Sensors'; Start-Sleep 2; Start-ScheduledTask -TaskName 'PCS Sensors'"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        with open(os.path.join(pcs_paths.LOGS, "watchdog.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " sensor reader stale: task restarted by the dashboard" + chr(10))
    except Exception as e:
        hub.add_event(f"# SENSORS restart failed: {e}")


def hwinfo_loop(hub, stop, demo, publish):
    """HWiNFO shared memory in, once a second (HWiNFO itself polls every 2 s by default), and the PCS values out
    as HWiNFO custom sensors for FanControl (real Mega data only, never the demo)."""
    hw = HWiNFO()
    try:                         # standalone reader (tools/pc_sensors, 2026-11-22): first choice, HWiNFO fills the gaps
        import sensors_src
        rd = sensors_src.Reader()
    except Exception:
        sensors_src = rd = None
    out = HWiNFOOut() if publish and not demo else None
    while not stop.is_set():
        if out:
            with hub.lock:
                row = hub.rows[-1] if hub.rows else None
            out.publish(row, time.time() - row["t"] if row else 1e9)
        if demo:
            k = time.time()
            vals = {"cpu_t": {"v": round(55 + 8 * math.sin(k / 30), 1), "u": "°C"},
                    "cpu_w": {"v": round(60 + 40 * math.sin(k / 30), 1), "u": "W"},
                    "cpu_load": {"v": round(20 + 15 * math.sin(k / 30), 1), "u": "%"},
                    "gpu_t": {"v": round(45 + 10 * math.sin(k / 50), 1), "u": "°C"},
                    "gpu_hot": {"v": round(55 + 12 * math.sin(k / 50), 1), "u": "°C"},
                    "gpu_w": {"v": round(150 + 100 * math.sin(k / 50), 1), "u": "W"},
                    "gpu_hp_w": {"v": round(110 + 70 * math.sin(k / 50), 1), "u": "W"},
                    "gpu_slot_w": {"v": 34.0, "u": "W"},
                    "gpu_load": {"v": round(60 + 30 * math.sin(k / 50), 1), "u": "%"},
                    "t_sensor": {"v": demo_cube(hub), "u": "°C"}, "vrm_t": {"v": 46.0, "u": "°C"},
                    "w_pump": {"v": 1800.0, "u": "RPM"}, "aio_pump": {"v": 960.0, "u": "RPM"},
                    "cpu_fan": {"v": 650.0, "u": "RPM"}, "cha1": {"v": 940.0, "u": "RPM"},
                    "cha2": {"v": 655.0, "u": "RPM"}, "dimm_t": {"v": 33.5, "u": "°C"}}
            hub.set_pc("demo", vals, demo_detail(k))
        else:
            status, vals = hw.read()
            hw_det = hw.detail if status == "ok" else {}
            with hub.lock:               # HWiNFO as read, for the side-by-side check (lhm_compare.py)
                hub.pc_hw = {"status": status, "v": vals, "t": time.time(), "d": hw_det}
            if rd is not None:
                try:
                    rs, rv = rd.read()
                    reader_watchdog(hub, rs == "ok")
                    status, vals, hw_det = sensors_src.merge((rs, rv, rd.detail), (status, vals, hw_det))
                    hub.pc_src = "reader" + (" + HWiNFO" if hub.pc_hw["status"] == "ok" else "") if rs == "ok" else \
                        ("HWiNFO" if hub.pc_hw["status"] == "ok" else "none") + f" (reader: {rs})"
                except Exception as e:
                    hub.pc_src = f"HWiNFO (reader error: {e})"
            hub.set_pc(status, vals, hw_det)
            with hub.lock:
                hub.pc["src"] = getattr(hub, "pc_src", "HWiNFO")
                if hub.info.get("standalone") and status == "ok":   # no controller rows: the charts use these (4 h)
                    g = lambda k: (vals.get(k) or {}).get("v") if isinstance(vals.get(k), dict) else None
                    hub.pc_hist.append({"t": time.time(), "Tcpu": g("cpu_t"), "Tgpu": g("gpu_hot") or g("gpu_t"),
                                        "Wcpu": g("cpu_w"), "Wgpu": g("gpu_w"), "Lcpu": g("cpu_load"), "Lgpu": g("gpu_load")})
            if out:
                with hub.lock:
                    pc = dict(hub.pc)
                prog = hub.prog.requests() if hub.prog and hub.prog.p in ("auto", "bench", "passive", "off") else None
                req = pc_requests(pc, prog)
                with hub.lock:
                    last = hub.rows[-1] if hub.rows else None
                fresh_mega = last is not None and time.time() - last["t"] < 3
                if fresh_mega and last.get("mode") == 4:      # Overclocking: P1 at least 68 %, as the BIOS sets it
                    if req.get("req_p1") is not None:         # P1_BY_BIOS: the BIOS curve runs P1
                        req["req_p1"] = max(req["req_p1"], float(OC_P1_MIN_PCT))
                    vv = pc.get("v") or {}
                    pkg = (vv.get("cpu_t") or {}).get("v")
                    vrm = (vv.get("vrm_t") or {}).get("v")
                    hot = (pkg is not None and pkg >= OC_FAN_GUARD_PKG_C) or (vrm is not None and vrm >= OC_FAN_GUARD_VRM_C)
                    if not hot:
                        req["req_rad"] = req["req_top"] = float(OC_PC_FAN_PCT)
                        req["low_floor"] = OC_PC_FAN_PCT   # hwinfo_out: floors of rad / top drop to this
                    req["oc_fan_hot"] = hot
                elif fresh_mega and last.get("mode") == 5:    # Daily (FW 0.9.23): case fans off unless a guard wants air
                    rad, top = hub.daily.update(pc, last, hub.prog.cpu_s if hub.prog else None)
                    req["req_rad"], req["req_top"] = rad, top
                    req["low_floor"] = 0.0
                    p1 = hub.daily.p1_update(pc, hub.prog.cpu_s if hub.prog else None,
                                             hub.prog.heat_avg if hub.prog else None)
                    if p1 is not None and req.get("req_p1") is not None:
                        req["req_p1"] = max(req["req_p1"], p1)
                    req["daily"] = hub.daily.info
                ts = (pc.get("v") or {}).get("t_sensor")
                if (status == "ok" and ts and fresh_mega and hub.info.get("connected") and not hub.released.is_set()
                        and fw_ok(hub.info, CUBE_FW) and time.time() - hub.cube_tx_at >= CUBE_TX_S):
                    hub.tx.put(f"cube{ts['v']:.1f}")              # silent, like lnk: the Mega shows / alarms on it
                    hub.cube_tx_at = time.time()
                fp = hub.fast_p1.update(pc)                   # instant P1 floor on the current load, every mode
                if fp and req.get("req_p1") is not None:
                    req["req_p1"] = max(req["req_p1"], fp)
                req["fast_p1"], req["fast_p1_why"] = fp, hub.fast_p1.why if fp else ""
                ov = hub.p1_override
                if ov and P1_BY_BIOS:
                    hub.p1_override = None
                elif ov and time.time() < ov[1]:          # manual P1 test (POST /p1), expires by itself
                    req["req_p1"] = float(ov[0])
                elif ov:
                    hub.p1_override = None
                    hub.add_event("# P1 override expired")
                # the reader's fast path: this mode's fan rules (fast_rules.py), evaluated there 4x a second
                import fast_rules, daily as _daily
                if fresh_mega and last.get("mode") == 4:
                    req["fast"] = fast_rules.overclocking(OC_FAN_GUARD_PKG_C, OC_FAN_GUARD_VRM_C)
                elif fresh_mega and last.get("mode") == 5:
                    req["fast"] = fast_rules.daily(_daily)
                else:
                    req["fast"] = hub.prog.fast_params() if hub.prog else None
                out.publish_req(req)   # PC fan / P1 requests for FanControl
                hub.req = {k: req.get(k) for k in ("req_rad", "req_top", "req_p1", "req_psu", "oc_fan_hot", "daily", "fast_p1", "fast_p1_why")}
        stop.wait(1.0)
    hw.close()
    if out:
        out.failsafe()               # curves on PCS values go to their top while nobody feeds them


def demo_loop(hub, stop):
    """Fake controller: Mode 2 regulating with slow outdoor drift; also fakes test mode (t/x/f/p/o/k/s) and scr0/1."""
    hub.set_info(connected=True, port="DEMO", fw="PCS controller fw demo", demo=True, error=None)
    hub.add_event("# PCS controller fw demo (fake data)")
    t0 = time.time() - 1800      # back-fill 30 min of history, then run in real time (2 Hz like the firmware)
    dt = 0.5
    k = -dt
    v2, v2_target = 30.0, None
    mode, test, disp = 2, False, 1
    fan_set, pump_set, pump_on = 10, 0, False
    slug = 0.0
    while not stop.is_set():
        k += dt
        if t0 + k > time.time():
            time.sleep(dt)
        while not hub.tx.empty():
            cmd = hub.tx.get_nowait()
            if cmd in ("m1", "m2", "m3"):
                mode, test, v2_target = int(cmd[1]), False, None
                hub.add_event(f"# MODE {mode} (serial)")
            elif cmd in ("lnk", "lnk0"):
                pass
            elif cmd in ("scr0", "scr1"):
                disp = int(cmd[3])
                hub.add_event(f"# display {'ON' if disp else 'OFF'}")
            elif cmd == "t":
                if mode == 1:
                    test, fan_set, pump_set, pump_on, v2_target = True, 10, 0, False, None
                    hub.add_event("# TEST mode ON")
                else:
                    hub.add_event("# TEST refused: needs Mode 1 (BTN1), idle, 12V present and no fault")
            elif cmd == "x":
                if test:
                    test, v2_target = False, 0.0
                    hub.add_event("# TEST mode OFF, V2 parking")
            elif test and cmd.startswith("pd") and cmd[2:].isdigit():
                pump_set, pump_on = max(5, min(100, int(cmd[2:]))), True
                hub.add_event(f"# TEST P2 on at {pump_set} %")
            elif test and cmd[:1] in ("f", "p") and cmd[1:].isdigit():
                if cmd[0] == "f":
                    fan_set = min(100, int(cmd[1:]))
                else:
                    pump_set = min(100, int(cmd[1:]))
                    pump_on = pump_set > 0
                hub.add_event(f"# TEST fans {fan_set} % | P2 {pump_set} %")
            elif test and cmd.startswith("o") and cmd.endswith("%"):
                pct = max(0.0, min(100.0, float(cmd[1:-1] or 0)))
                v2_target = 23.2 + pct / 100 * (90 - 23.2)
                hub.add_event(f"# V2 -> {v2_target:.1f} deg")
            elif test and cmd.startswith("dg"):
                v2_target = max(0.0, min(90.0, float(cmd[2:] or 0)))
                hub.add_event(f"# V2 -> {v2_target:.1f} deg")
            elif test and cmd[:1] in "+-" and cmd[1:].isdigit():
                v2_target = max(0.0, min(90.0, v2 + (1 if cmd[0] == "+" else -1) * int(cmd[1:]) * 90 / 5050))
                hub.add_event(f"# TEST V2 jog {cmd} ms")
            elif test and cmd == "k":
                v2_target = 0.0
                hub.add_event("# TEST V2 park")
            elif test and cmd == "s":
                v2_target = v2
                hub.add_event("# TEST V2 stop")
            else:
                hub.add_event(f"# demo ignores: {cmd}")
        t3 = -3 + 1.5 * math.sin(k / 400)            # like 2026-11-08: outdoor water well below DP
        t1 = 26 + 1.5 * math.sin(k / 90) + random.gauss(0, 0.05)
        dp = 9.0 + 0.3 * math.sin(k / 300)
        tset = max(23.1 + 3, dp + 4) if mode == 2 else dp + 4
        run = mode != 1
        if test or (not run and v2_target is not None):
            if v2_target is not None:
                step = 90 / 5.05 * dt
                v2 += max(-step, min(step, v2_target - v2))
            fan, pump = (fan_set, pump_set if pump_on else 0) if test else (10, 0)
        elif run:
            v2 += (t1 - 8 - tset) * 0.2
            v2 = min(90, max(23.2, v2))
            fan, pump = (10 if v2 < 81 else 30), 80
        else:
            v2, fan, pump = 0.0, 10, 0
        share = max(0, (v2 - 23.2) / (90 - 23.2))
        flow = 0.4 + 0.6 * min(1.0, pump / 60) if pump else 0.0     # P2 speed matters (no check valve)
        slug = slug + (1.0 - slug) * dt / 60 if share > 0 and flow > 0 else max(0.0, slug - dt / 600)
        # rad outlet: outside air (t3 above) plus what the fans can't take out; stagnant water starts at air temperature
        t3 = t3 + (t1 - t3) * math.exp(-(0.4 + fan / 35)) * slug
        t2 = t1 - min(1.0, share * 2.2) * (t1 - t3) * flow + random.gauss(0, 0.05)
        state = "TEST" if test else ("RUN" if run else "IDLE")
        row = {"ms": int(k * 1000), "mode": mode, "state": state,
               "T1": round(t1, 2), "T2": round(t2, 2), "T3": round(t3, 2), "Tout": round(t3, 2), "Troom": 23.1,
               "DP_A": round(dp, 2), "DP_B": round(dp - 0.8, 2), "DP_R": round(dp - 3.1, 2), "DP": round(dp, 2),
               "Tset": round(tset, 2) if mode != 1 else None, "V2deg": round(v2, 1),
               "V2mm": round(max(0, v2 - 23.2) * 0.15, 1), "V2out": round(share * 100), "fan": fan,
               "pump": pump, "rpmF1": None, "rpmF2": fan * 30 + random.randint(-10, 10),
               "rpmP2": (1800 + pump * 30) if (pump or (test and pump_on)) else 0, "V1": 0, "fuses": 7, "faults": 0, "alarms": 0,
               "t": t0 + k, "T_A": 30.9, "RH_A": 26.0, "T_B": 29.8, "RH_B": 25.5, "RH_R": 31.8, "disp": disp,
               "pen": 1 if (pump or (test and pump_on)) else 0}
        hub.add_row(row)


def prog_loop(hub, stop):
    """PC programs (pcs_prog.py) + their state to the page once a second."""
    t = threading.Thread(target=hub.prog.run, args=(stop,), daemon=True)
    t.start()
    while not stop.is_set():
        hub.publish("prog", hub.prog.state())
        stop.wait(1.0)
    t.join(timeout=2)


class Handler(BaseHTTPRequestHandler):
    hub = None

    def log_message(self, *a):
        pass

    def handle_one_request(self):                   # diag: a request stuck > 10 s dumps every thread's stack
        diag.begin(threading.current_thread().name)
        try:
            super().handle_one_request()
        finally:
            diag.end()

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        an = getattr(self.hub, "analyzer", None)
        if path in ("/analyzer", "/analyzer.html"):
            with open(os.path.join(HERE, "analyzer.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/analyzer/state":
            if not an:
                return self._send(503, b'{"error": "analyzer not running"}', "application/json")
            return self._send(200, json.dumps(an.summary()).encode(), "application/json")
        if path == "/analyzer/incident":
            iid = dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("id", "")
            d = an.detail(iid) if an else None
            return self._send(200 if d else 404, json.dumps(d).encode(), "application/json")
        pages = {"/home": "home.html", "/settings": "settings.html", "/setup": "setup.html",   # 2026-11-22 facelift
                 "/fans": "fans.html"}                                                       # 2026-11-23 Fans tab, step 1
        if path == "/fans/config":
            return self._send(200, json.dumps(fans_config_load()).encode(), "application/json")
        if path == "/fans/state":                      # Fans tab: reader fans block + header duty / rpm + fan_req.json (read-only)
            return self._send(200, json.dumps(fans_state()).encode(), "application/json")
        if path in pages:
            with open(os.path.join(HERE, pages[path]), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path.startswith("/boards/") and path.endswith((".png", ".webp", ".jpg", ".svg", ".html", ".json")):   # board photos + tracing aids
            name = os.path.basename(path)
            fp = os.path.join(HERE, "boards", name)
            if "/" in path[8:] or not os.path.isfile(fp):
                return self._send(404, b"not found", "text/plain")
            with open(fp, "rb") as f:
                ct = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg", "svg": "image/svg+xml", "html": "text/html; charset=utf-8",
                      "json": "application/json"}[name.rsplit(".", 1)[1]]
                return self._send(200, f.read(), ct)
        if path in ("/theme.css", "/theme.js", "/boards.js"):
            with open(os.path.join(HERE, path[1:]), "rb") as f:
                return self._send(200, f.read(), "text/css" if path.endswith("css") else "application/javascript")
        if path == "/prefs":
            return self._send(200, json.dumps(prefs_load()).encode(), "application/json")
        if path == "/board/detect":                    # motherboard maker / model from Windows (setup screen)
            return self._send(200, json.dumps(board_detect()).encode(), "application/json")
        if path == "/pcdetail":                        # the PC sensor detail tree (setup preview of the board)
            with self.hub.lock:
                det = (self.hub.pc or {}).get("d") or {}
                body = json.dumps(det)
            return self._send(200, body.encode(), "application/json")
        if path == "/history":                         # Home charts: last N minutes, ~180 points (2026-11-22)
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            try:
                mins = max(5, min(240, int(q.get("min", "60"))))
            except ValueError:
                mins = 60
            cut = time.time() - mins * 60
            sa = self.hub.info.get("standalone")
            with self.hub.lock:
                src = list(self.hub.pc_hist)[-(mins * 60 + 120):] if sa else self.hub.rows[-(mins * 60 + 120):]
                rows = [r for r in src if (r.get("t") or 0) >= cut]
            step = max(1, len(rows) // 180)
            keys = ("Tcpu", "Tgpu", "Wcpu", "Wgpu", "Lcpu", "Lgpu") if sa else                 ("T1", "T2", "T3", "DP", "Tcube", "Tcpu", "fan", "pump", "V2out")
            out = {"t": [], **{k: [] for k in keys}}
            for i in range(0, len(rows), step):
                chunk = rows[i:i + step]
                out["t"].append(round(chunk[-1].get("t") or 0))
                for k in keys:
                    v = [c.get(k) for c in chunk if isinstance(c.get(k), (int, float))]
                    out[k].append(round(sum(v) / len(v), 2) if v else None)
            return self._send(200, json.dumps(out).encode(), "application/json")
        if self.path in ("/", "/index.html", "/full", "/full/"):   # clean page / full page (HWiNFO extras)
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/status":                   # small summary for the PCS desktop app's tray (2026-11-22)
            with self.hub.lock:
                r = dict(self.hub.rows[-1]) if self.hub.rows else {}
                info = dict(self.hub.info)
                pc = dict(self.hub.pc or {})
            ps = self.hub.prog.state()
            mode = r.get("mode") if r and not r.get("hist") else None
            active = {4: "oc", 5: "daily"}.get(mode) if ps.get("p") == "off" else None
            pick = lambda k: r.get(k) if r and not r.get("hist") else None
            pvals = pc.get("v") or {}
            pv = lambda k: (pvals.get(k) or {}).get("v") if isinstance(pvals.get(k), dict) else None
            body = json.dumps({"connected": info.get("connected"), "standalone": bool(info.get("standalone")), "fw": info.get("fw"), "error": info.get("error"),
                               "port": info.get("port"), "released": info.get("released"), "disp": pick("disp"),
                               "program": active or ps.get("p"), "name": ps.get("name"), "phase": ps.get("phase"),
                               "mode": mode, "state": pick("state"), "T1": pick("T1"), "T2": pick("T2"),
                               "T3": pick("T3"), "DP": pick("DP"), "Tset": pick("Tset"),
                               "faults": pick("faults"), "alarms": pick("alarms"),
                               "cube": pv("t_sensor"), "cpu_t": pv("cpu_t"), "cpu_w": pv("cpu_w"), "gpu_hot": pv("gpu_hot") or pv("gpu_t"),
                               "cpu_load": pv("cpu_load"), "gpu_load": pv("gpu_load"),
                               "gpu_w": pv("gpu_w"), "p1_rpm": pv("w_pump"), "pc_src": pc.get("src"),
                               "fan": pick("fan"), "pump": pick("pump"), "v2out": pick("V2out"), "rpmF2": pick("rpmF2"),
                               "pen": pick("pen"), "T_A": pick("T_A"), "T_B": pick("T_B"), "Troom": pick("Troom"),
                               "RH_A": pick("RH_A"), "RH_B": pick("RH_B"), "RH_R": pick("RH_R"), "DP_A": pick("DP_A"),
                               "DP_B": pick("DP_B"), "DP_R": pick("DP_R"), "t2set": ps.get("t2set"),
                               "req": ps.get("req") or (pc.get("req") if isinstance(pc.get("req"), dict) else None)})
            self._send(200, body.encode(), "application/json")
        elif self.path == "/state":
            with self.hub.lock:
                body = json.dumps({"rows": self.hub.rows, "events": self.hub.events, "info": self.hub.info,
                                   "pc": self.hub.pc})
            body = body[:-1] + ', "prog": ' + json.dumps(self.hub.prog.state()) + "}"
            self._send(200, body.encode(), "application/json")
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = queue.Queue(maxsize=500)
            with self.hub.lock:
                self.hub.subs.append(q)
                # current status first: a page that reconnects after a server restart otherwise kept showing
                # "Dashboard server stopped" until something changed (2026-11-08)
                q.put_nowait("event: info\ndata: " + json.dumps(self.hub.info) + "\n\n")
            try:
                while True:
                    try:
                        msg = q.get(timeout=15)
                    except queue.Empty:
                        msg = ": ping\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                with self.hub.lock:
                    self.hub.subs.remove(q)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/prefs":                       # {"alerts": bool, ...}: PC-side settings (Settings page)
            n = int(self.headers.get("Content-Length", 0))
            try:
                upd = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                upd = {}
            return self._send(200, json.dumps(prefs_save(upd)).encode(), "application/json")
        if self.path == "/eventvwr":                    # {"records": [123, ...]}: open Event Viewer on those System events
            n = int(self.headers.get("Content-Length", 0))
            try:
                recs = [int(r) for r in (json.loads(self.rfile.read(n) or b"{}").get("records") or [])][:50]
            except (ValueError, TypeError):
                recs = []
            if not recs:
                return self._send(400, b"no records", "text/plain")
            try:
                open_event_viewer(recs)
            except Exception as e:
                return self._send(500, str(e).encode(), "text/plain")
            return self._send(200, b"ok", "text/plain")
        if self.path == "/fans/config":                 # Fans tab: the user's groups / names (reader --user-fans)
            n = int(self.headers.get("Content-Length", 0))
            try:
                cfg = fans_config_save(json.loads(self.rfile.read(n) or b"{}"))
            except (ValueError, TypeError) as e:
                return self._send(400, str(e).encode(), "text/plain")
            except OSError as e:
                return self._send(500, f"could not write fans.json: {e}".encode(), "text/plain")
            return self._send(200, json.dumps(cfg).encode(), "application/json")
        if self.hub.info.get("standalone") and self.path in ("/prog", "/sweep", "/p1", "/service", "/cmd"):
            return self._send(409, b"standalone monitor: no controller on this PC", "text/plain")
        if self.path == "/analyzer":                    # {"on": true | false}: flight recorder, kept across reboots
            n = int(self.headers.get("Content-Length", 0))
            try:
                on = bool(json.loads(self.rfile.read(n) or b"{}").get("on"))
            except ValueError:
                return self._send(400, b"bad request", "text/plain")
            an = getattr(self.hub, "analyzer", None)
            if not an:
                return self._send(503, b"analyzer not running", "text/plain")
            an.set_enabled(on)
            return self._send(200, json.dumps(an.summary()).encode(), "application/json")
        if self.path == "/prog":
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(self.rfile.read(n) or b"{}").get("p", "")
            except ValueError:
                p = ""
            ok, msg = self.hub.prog.select(p)
            body = json.dumps({"ok": ok, "msg": msg, "prog": self.hub.prog.state()}).encode()
            return self._send(200 if ok else 409, body, "application/json")
        if self.path == "/sweep":                       # P1 sweep in Benchmark: {"on": true | false}
            n = int(self.headers.get("Content-Length", 0))
            try:
                on = bool(json.loads(self.rfile.read(n) or b"{}").get("on"))
            except ValueError:
                on = False
            ok, msg = self.hub.prog.sweep_cmd(on)
            body = json.dumps({"ok": ok, "msg": msg, "prog": self.hub.prog.state()}).encode()
            return self._send(200 if ok else 409, body, "application/json")
        if self.path == "/p1":                          # {"pct": 62} for 30 min, {"pct": null} clears
            n = int(self.headers.get("Content-Length", 0))
            if P1_BY_BIOS:
                self.rfile.read(n)
                return self._send(409, b"P1 is run by the BIOS curve (P1_BY_BIOS in hwinfo_out.py)", "text/plain")
            try:
                pct = json.loads(self.rfile.read(n) or b"{}").get("pct")
            except ValueError:
                pct = None
            if pct is None:
                self.hub.p1_override = None
                self.hub.add_event("# P1 override cleared")
            else:
                pct = max(30.0, min(100.0, float(pct)))
                self.hub.p1_override = (pct, time.time() + 30 * 60)
                self.hub.add_event(f"# P1 override {pct:.0f} % for 30 min")
            return self._send(200, b"ok", "text/plain")
        if self.path == "/service":                     # {"on": false} End service (free the COM port) / true Start
            n = int(self.headers.get("Content-Length", 0))
            try:
                on = bool(json.loads(self.rfile.read(n) or b"{}").get("on"))
            except ValueError:
                on = False
            hub = self.hub
            if not on and not hub.released.is_set():
                p = hub.prog.p
                hub.prog_before = p if p in ("passive", "auto", "bench") else None
                if p in ("auto", "bench", "manual"):    # a PC program drives the Mega through test mode: stop it (x, m1)
                    hub.prog.select("off")
                    hub.add_event("# SERVICE ENDED from the page: PC program stopped (P2 off, V2 parking), COM port released")
                elif p == "passive":                    # branch already off: nothing to send
                    hub.prog.select("off")
                    hub.add_event("# SERVICE ENDED from the page: COM port released")
                else:                                   # firmware mode (e.g. Mode 4 Overclocking): leave it running
                    hub.add_event("# SERVICE ENDED from the page: the Mega keeps its own mode running, COM port released")
                hub.released.set()
            elif on and hub.released.is_set():
                hub.released.clear()
                hub.set_info(released=False, error=None)
                hub.add_event("# SERVICE STARTED from the page: reopening the COM port")
                if hub.prog_before:
                    hub.prog.select(hub.prog_before)
                    hub.add_event(f"# program {hub.prog_before.upper()} restored")
            body = json.dumps({"ok": True, "info": hub.info}).encode()
            return self._send(200, body, "application/json")
        if self.path != "/cmd":
            return self._send(404, b"not found", "text/plain")
        n = int(self.headers.get("Content-Length", 0))
        try:
            cmd = json.loads(self.rfile.read(n) or b"{}").get("cmd", "").strip()
        except ValueError:
            cmd = ""
        if not cmd or len(cmd) > 40 or any(c in cmd for c in "\r\n"):
            return self._send(400, b"bad command", "text/plain")
        self.hub.tx.put(cmd)
        self.hub.add_event(f"> {cmd}")
        self._send(200, b"ok", "text/plain")


def main():
    ap = argparse.ArgumentParser(description="PCS dashboard (browser UI for the controller)")
    ap.add_argument("port", nargs="?", help="COM port, e.g. COM4 (default: find the CH340)")
    ap.add_argument("--http", type=int, default=8570, help="web port (default 8570)")
    ap.add_argument("--log", help="append every CSV row to this file")
    ap.add_argument("--demo", action="store_true", help="fake data, no Mega needed")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    ap.add_argument("--no-hwinfo", action="store_true", help="don't read HWiNFO shared memory (also no publishing)")
    ap.add_argument("--standalone", action="store_true",
                    help="monitor only, for other PCs (2026-11-23): no COM port / Mega, no programs, no fan requests, "
                         "no HWiNFO publishing; the default of the packaged exe (--mega turns it off there)")
    ap.add_argument("--mega", action="store_true", help="packaged exe: run with the controller as on owner's PC")
    ap.add_argument("--no-fancontrol", action="store_true",
                    help="don't publish the PCS values as HWiNFO custom sensors (FanControl source)")
    a = ap.parse_args()
    diag.setup(os.path.join(pcs_paths.LOGS, "service.log"), pcs_paths.FROZEN)
    standalone = a.standalone or (pcs_paths.FROZEN and not a.mega)
    if standalone:
        a.no_fancontrol = True
    if not a.demo and not standalone and serial is None:
        sys.exit("pyserial missing: run  py -m pip install pyserial")

    if sys.platform == "win32":     # stay responsive while benchmarks load every core (R23 starved us 7 s, 2026-11-08)
        # 2026-11-13 (owner: the pump didn't react under full CPU load): HIGH priority and no power throttling (EcoQoS),
        # so Windows doesn't park us on busy E-cores. The old call passed the pseudo-handle as a 32-bit int and failed
        # silently: the dashboard ran at the task's BelowNormal.
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            me = k32.GetCurrentProcess()
            if not k32.SetPriorityClass(me, 0x00000080):             # HIGH_PRIORITY_CLASS
                k32.SetPriorityClass(me, 0x00008000)                 # fallback: ABOVE_NORMAL_PRIORITY_CLASS

            class PPTS(ctypes.Structure):                            # PROCESS_POWER_THROTTLING_STATE
                _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]
            k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            st = PPTS(1, 0x1, 0x0)                                   # EXECUTION_SPEED controlled, throttling off
            k32.SetProcessInformation(me, 4, ctypes.byref(st), ctypes.sizeof(st))   # 4 = ProcessPowerThrottling
        except Exception:
            pass
    hub = Hub()
    Handler.hub = hub
    hub.info["standalone"] = standalone
    if not a.demo and not standalone:
        n = load_history(hub)
        if n:
            hub.add_event(f"# chart history: {n} rows reloaded from the logs (last {HISTORY_S // 3600} h)")
    stop = threading.Event()
    worker = pgt = None
    if not standalone:             # standalone: no controller, never touch a COM port (another PC's CH340 is not ours)
        worker = threading.Thread(target=demo_loop if a.demo else serial_loop,
                                  args=(hub, stop) if a.demo else (hub, a.port, a.log, stop), daemon=True)
        worker.start()
    else:
        hub.add_event("# standalone monitor: no controller, PC sensors only")
    hub.prog = Prog(hub, demo=a.demo)
    if not standalone:
        pgt = threading.Thread(target=prog_loop, args=(hub, stop), daemon=True)
        pgt.start()
    hwt = None
    if not a.no_hwinfo:
        hwt = threading.Thread(target=hwinfo_loop, args=(hub, stop, a.demo, not a.no_fancontrol), daemon=True)
        hwt.start()
    if not a.demo:                 # WHEA / bugcheck / unexpected restart -> logs/events.csv (2026-11-21)
        try:
            import events_watch
            events_watch.start(hub, stop)
        except Exception as e:
            hub.add_event(f"# event watcher not started: {e}")
        try:                       # stability analyzer: flight recorder + incidents (Analyzer tab, 2026-11-21)
            import analyzer
            hub.analyzer = analyzer.start(hub, stop)
            if hub.analyzer.st.get("enabled"):
                hub.add_event("# ANALYZER resumed: flight recorder running")
        except Exception as e:
            hub.analyzer = None
            hub.add_event(f"# analyzer not started: {e}")
        if not standalone:
            try:                   # standalone sensor reader vs HWiNFO, side by side for 48 h (2026-11-22)
                import lhm_compare
                lhm_compare.start(hub, stop)
            except Exception as e:
                hub.add_event(f"# sensor comparison not started: {e}")
    srv =ThreadingHTTPServer(("127.0.0.1", a.http), Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{a.http}/"
    print(f"PCS dashboard on {url}  (Ctrl+C quits)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    stop.set()
    if pgt:
        pgt.join(timeout=2)        # leaves test mode (x) if a program was driving the Mega
        time.sleep(0.3)            # let the serial thread send it
    if worker:
        worker.join(timeout=2)
    if hwt:
        hwt.join(timeout=2)
    print("closed")


if __name__ == "__main__":
    main()
