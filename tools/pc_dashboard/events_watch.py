"""Event Log watcher (owner 2026-11-21: "monitor it tonight and log any more funny business").

Every POLL_S reads the Windows System log (no admin needed) for
  - WHEA-Logger (19 = corrected machine check, with the APIC ID; others as they come)
  - WER-SystemErrorReporting 1001 (bugcheck code + parameters + dump path, written at the boot after a crash)
  - Kernel-Power 41 (unexpected restart; BugcheckCode 0 = power cut / reset button)
  - NVIDIA nvlddmkm 13 / 14 / 153 and Display 4101 (GPU driver errors and TDR resets; 2026-11-22)
and appends each new event once to logs/events.csv together with what the core was doing at that second, taken from
logs/cores_YYYYMMDD.csv (clock, effective clock, VID, load). For a bugcheck the snapshot is the last cores row written
before it (the moment the PC went down). The CPU of a 0x101 is its 4th parameter. New events also go to the page's
console.

wevtutil runs with CREATE_NO_WINDOW so the pythonw dashboard never flashes a console.
"""
import csv, datetime as dt, json, os, subprocess, threading, time, xml.etree.ElementTree as ET

POLL_S = 60
LOOKBACK_MS = 45 * 86400 * 1000         # first pass at every start (crashes are logged at boot; seen ones are skipped)
import pcs_paths
DIR = pcs_paths.LOGS
OUT = os.path.join(DIR, "events.csv")
SEEN = os.path.join(DIR, "events_seen.txt")
NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
# logical CPU -> APIC ID, measured with CPUID on 2026-11-19 (logical = HWiNFO core = BIOS CoreN)
APIC = [0, 8, 16, 18, 20, 22, 24, 26, 28, 30, 32, 40, 48, 56, 64, 66, 68, 70, 72, 74, 76, 78, 80, 88]
CORE_OF_APIC = {a: i for i, a in enumerate(APIC)}
_topo_map = {"t": 0, "m": None}


def core_of_apic(a):
    """APIC ID -> core index, from the sensor reader's topology (any CPU, SMT siblings included; 2026-11-23);
    the table above only when the reader has none (it is this PC's)."""
    if time.time() - _topo_map["t"] > 600:
        _topo_map["t"] = time.time()
        try:
            import sensors_src
            topo = (sensors_src.read_raw() or {}).get("topo") or []
            m = {}
            for i, c in enumerate(topo):
                if isinstance(c.get("apic"), int):
                    for k in range(max(1, len(c.get("lp") or [1]))):
                        m[c["apic"] + k] = i
            _topo_map["m"] = m or None
        except Exception:
            pass
    return (_topo_map["m"] or CORE_OF_APIC).get(a)
P_CORES = {0, 1, 10, 11, 12, 13, 22, 23}       # fallback (this PC) while the sensor reader hasn't said which cores are P
try:
    from sensors_src import p_cores as _p_cores  # P-cores from the reader's name-based mapping (2026-11-23)
except Exception:
    _p_cores = None
HEAD = ["logged", "event_time", "kind", "code", "detail", "apic", "core", "core_type",
        "snap_time", "clk", "eclk", "vid", "use", "record"]
QUERY = ("*[System[(Provider[@Name='Microsoft-Windows-WHEA-Logger'] or "
         "(Provider[@Name='Microsoft-Windows-WER-SystemErrorReporting'] and EventID=1001) or "
         "(Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=41) or "
         "(Provider[@Name='nvlddmkm'] and (EventID=13 or EventID=14 or EventID=153)) or "
         "(Provider[@Name='Display'] and EventID=4101)) and "
         "TimeCreated[timediff(@SystemTime) <= {ms}]]]")


def core_name(c):
    if c is None:
        return ""
    try:
        pc = set(_p_cores(P_CORES)) if _p_cores else set(P_CORES)
    except Exception:
        pc = set(P_CORES)
    if c in pc:
        return "P"
    if pc != P_CORES:                            # E clusters known for this PC's layout only
        return "E"
    return "E" + str({2: 1, 6: 2, 14: 3, 18: 4}[next(s for s in (18, 14, 6, 2) if c >= s)])


def query(ms):
    try:
        out = subprocess.run(["wevtutil", "qe", "System", "/q:" + QUERY.format(ms=ms), "/f:xml"],
                             capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)).stdout
    except Exception:
        return []
    evs = []
    for chunk in out.split("</Event>"):
        chunk = chunk.strip()
        if not chunk.startswith("<Event"):
            continue
        try:
            x = ET.fromstring(chunk + "</Event>")
        except ET.ParseError:
            continue
        s = x.find("e:System", NS)
        prov = s.find("e:Provider", NS).get("Name")
        eid = int(s.find("e:EventID", NS).text)
        rec = s.find("e:EventRecordID", NS).text
        t = s.find("e:TimeCreated", NS).get("SystemTime")
        t = dt.datetime.fromisoformat(t.replace("Z", "+00:00")[:26] + "+00:00" if "." in t else t.replace("Z", "+00:00"))
        data = {d.get("Name"): (d.text or "") for d in x.findall("e:EventData/e:Data", NS)}
        data["_vals"] = [(d.text or "").strip() for d in x.findall("e:EventData/e:Data", NS)]
        evs.append((t.astimezone(), prov, eid, rec, data))
    return evs


def snapshot(when, core, before=False):
    """Row of the cores log at `when` (local datetime): the row with that time, or the last one before it."""
    path = os.path.join(DIR, f"cores_{when:%Y%m%d}.csv")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            txt = f.read().replace("\x00", "")
    except OSError:
        return None
    key = when.strftime("%H:%M:%S")
    best = None
    for r in csv.DictReader(txt.splitlines()):
        t = r.get("time") or ""
        if len(t) != 8:
            continue
        if t < key or (t == key and not before):
            best = r
        else:
            break
    if best is None or core is None:
        return best and {"snap_time": best["time"]}
    return {"snap_time": best["time"], "clk": best.get(f"c{core}_clk"), "eclk": best.get(f"c{core}_eclk"),
            "vid": best.get(f"c{core}_v"), "use": best.get(f"c{core}_use")}


def describe(t, prov, eid, data):
    """-> kind, code, detail, core, snapshot_before"""
    if "WHEA" in prov:
        apic = data.get("ApicId")
        core = core_of_apic(int(apic)) if apic and apic.isdigit() else None
        kind = "WHEA corrected" if eid == 19 else f"WHEA {eid}"
        if eid == 3 and data.get("RawData"):          # 2026-11-22: decode the CPER record (cper.py)
            try:
                import cper
                r = cper.decode(data["RawData"])
                if r.get("ok") and r.get("boot") and r.get("severity") == "informational":
                    paths = [s["path"] for s in r["sections"] if s.get("path")]
                    return ("WHEA boot info", "", "firmware boot record, informational, not a CPU error " + " ".join(paths),
                            None, None, False)
                if r.get("ok"):
                    return kind, "", cper.describe(data["RawData"]), apic, core, False
            except Exception:
                pass
        lvl = data.get("MemHierarchyLvl")
        detail = f"type {data.get('ErrorType')} bank {data.get('MCABank')} L{lvl}" if lvl and lvl != "256" else \
            f"type {data.get('ErrorType')} bank {data.get('MCABank')}"
        return kind, "", detail, apic, core, False
    if eid == 1001:
        p = data.get("param1", "")
        code = p.split(" ")[0]
        core = None
        if code.lower().endswith("101"):                     # CLOCK_WATCHDOG_TIMEOUT: 4th parameter = CPU
            try:
                core = int(p.split("(")[1].rstrip(")").split(",")[3], 16)
            except Exception:
                pass
        return "bugcheck", code, p + "  " + data.get("param2", ""), "", core, True
    if eid == 41:
        bc = int(data.get("BugcheckCode") or 0)
        return "unexpected restart", hex(bc), "no bugcheck: power cut or reset button" if bc == 0 else \
            "bugcheck recorded (see the 1001 line)", "", None, True
    if prov == "nvlddmkm" or prov == "Display":
        vals = [v for v in data.get("_vals", []) if v and v != "0"]
        text = vals[1] if len(vals) > 1 else ""
        if prov == "Display":
            return "GPU reset", "TDR", "display driver stopped responding and recovered", "", None, False
        if eid == 153:
            tdr = "TDR" in text
            return ("GPU reset" if tdr else "GPU error"), ("TDR" if tdr else "153"), text.replace(";", ","), "", None, False
        # 13 = graphics exception (Xid), 14 = channel / engine error with a hex payload
        return "GPU error", str(eid), text.replace(";", ",")[:120], "", None, False
    return f"{prov} {eid}", "", "", "", None, False


TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml('<toast scenario="reminder"><visual><binding template="ToastGeneric"><text>TITLE</text><text>BODY</text></binding></visual><actions><action content="Open Analyzer" activationType="protocol" arguments="http://127.0.0.1:8570/analyzer"/><action content="Dismiss" activationType="system" arguments="dismiss"/></actions></toast>')
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""
LABEL = {"bugcheck": "Blue screen", "WHEA corrected": "Silent cache error (WHEA)", "GPU reset": "GPU driver reset",
         "GPU error": "GPU driver error"}


def alerts_enabled():
    """Settings page switch "Crash notifications" (logs/prefs.json, 2026-11-22); on unless switched off."""
    try:
        with open(os.path.join(DIR, "prefs.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("alerts", True))
    except (OSError, ValueError):
        return True


def notify_events(alerts):
    """One Windows notification for a batch of new events (blue screen, WHEA, GPU driver), with an Analyzer button."""
    try:
        import html, subprocess
        kinds = []
        for _t, kind, _c, _w in alerts:
            k = LABEL.get(kind, kind)
            if k not in kinds:
                kinds.append(k)
        title = ", ".join(kinds) + (f" ({len(alerts)})" if len(alerts) > 1 else "")
        t, kind, code, where = alerts[-1]
        body = f"{t:%H:%M:%S} {LABEL.get(kind, kind)} {code} {where}".strip()
        esc = lambda s: html.escape(s, quote=True).replace("'", "&apos;")
        script = TOAST_PS.replace("TITLE", esc("PCS: " + title)).replace("BODY", esc(body))
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    except Exception:
        pass


class Watch:
    def __init__(self, hub=None):
        self.hub = hub
        os.makedirs(DIR, exist_ok=True)
        self.seen = set()
        if os.path.exists(SEEN):
            with open(SEEN, encoding="utf-8") as f:
                self.seen = {l.strip() for l in f if l.strip()}

    def poll(self, ms):
        new = [e for e in query(ms) if e[3] not in self.seen]
        new.sort(key=lambda e: e[0])
        if not new:
            return []
        first = not os.path.exists(OUT)
        lines, alerts = [], []
        with open(OUT, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if first:
                w.writerow(HEAD)
            for t, prov, eid, rec, data in new:
                kind, code, detail, apic, core, before = describe(t, prov, eid, data)
                # a crash is logged at the next boot: its snapshot is the last cores row before the event
                snap = snapshot(t if not before else t - dt.timedelta(seconds=20), core, before) or {}
                w.writerow([dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), t.strftime("%Y-%m-%d %H:%M:%S"),
                            kind, code, detail, apic, "" if core is None else core, core_name(core),
                            snap.get("snap_time", ""), snap.get("clk", ""), snap.get("eclk", ""),
                            snap.get("vid", ""), snap.get("use", ""), rec])
                where = "" if core is None else f" core {core} ({core_name(core)})"
                at = f" at {snap['clk']} MHz, eff {snap['eclk']} MHz, {snap['vid']} V, {snap['use']} %" \
                    if snap.get("clk") else ""
                lines.append(f"# EVENT {t:%m-%d %H:%M:%S} {kind} {code}{where}{at}")
                self.seen.add(rec)
                # Windows notification (owner 2026-11-22): only recent ones (a crash is logged at the next boot,
                # minutes later), never the 45-day history of the first pass; a burst becomes one notification
                if dt.datetime.now().astimezone() - t < dt.timedelta(hours=2) and kind not in ("unexpected restart", "WHEA boot info"):
                    alerts.append((t, kind, code, where.strip()))
        with open(SEEN, "a", encoding="utf-8") as f:
            f.writelines(e[3] + "\n" for e in new)
        if self.hub:
            for l in lines:
                self.hub.add_event(l)
        if alerts and self.hub is not None and alerts_enabled():
            notify_events(alerts)
        return lines

    def run(self, stop):
        ms = LOOKBACK_MS
        while not stop.is_set():
            try:
                self.poll(ms)
            except Exception:
                pass                                         # never take the dashboard down
            ms = 15 * 60 * 1000
            stop.wait(POLL_S)


def start(hub, stop):
    t = threading.Thread(target=Watch(hub).run, args=(stop,), daemon=True)
    t.start()
    return t


if __name__ == "__main__":                                   # py events_watch.py: one pass, print what is new
    for l in Watch().poll(LOOKBACK_MS):
        print(l)
