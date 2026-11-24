"""Stability analyzer (owner 2026-11-21): BSOD / OC instability analyser for the dashboard's Analyzer tab.

Two parts:
  - Flight recorder (on / off from the page, kept in logs/analyzer/state.json, so it survives reboots and crashes:
    the dashboard autostarts and resumes it). While on, once a second: all 24 cores (clock, effective clock, VID, load,
    temperature) + package / ring / VR / memory / limits / GPU / cooling into logs/analyzer/flight_YYYYMMDD.csv.
    Files older than KEEP_DAYS are deleted.
  - Incidents: built from logs/events.csv (events_watch.py, always on): a bugcheck (+ its Kernel-Power 41) = crash,
    a Kernel-Power 41 without a bugcheck = hard reset / power loss, a WHEA corrected error = silent fault. Each gets the
    implicated core (WHEA APIC ID, the 0x101 CPU, or the crash dump when a copy is readable), the 120 s lead-up from the
    flight log (or, before it existed, from the per-core + 1 s dashboard logs), what the core was doing at the moment it
    failed, and plain-language findings (froze first, load step, ratio drop, temperatures, ...). Computed once and
    cached in logs/analyzer/incidents.json.
"""
import csv, datetime as dt, glob, json, os, re, struct, threading, time

try:
    import procs
except Exception:                                    # not on Windows / ntdll missing: no process column
    procs = None
try:
    from sensors_src import p_cores as _p_cores      # P-cores from the sensor reader's mapping (2026-11-23)
except Exception:
    _p_cores = None

HERE = os.path.dirname(os.path.abspath(__file__))
import pcs_paths
LOGS = pcs_paths.LOGS
DIR = os.path.join(LOGS, "analyzer")
STATE = os.path.join(DIR, "state.json")
CACHE = os.path.join(DIR, "incidents.json")
EVENTS = os.path.join(LOGS, "events.csv")
KEEP_DAYS = 10
LEAD_S = 120
CACHE_VER = 9

P_CORES = [0, 1, 10, 11, 12, 13, 22, 23]   # fallback (this PC) while the reader hasn't said which cores are P
CLUSTERS = {1: [2, 3, 4, 5], 2: [6, 7, 8, 9], 3: [14, 15, 16, 17], 4: [18, 19, 20, 21]}
PROC_S = 10                  # seconds of process data summarised before the failure
CORE_FIELDS = ["clk", "eclk", "v", "use", "t", "c0", "c3"]   # c0: % in C0 (2026-11-22), c3: % in Windows C3 (deepest, 2026-11-23)

# globals in the flight log: name -> (path in the HWiNFO data, rounding)
GLOB = [
    ("cpu_w", "v.cpu_w", 1), ("cpu_load", "v.cpu_load", 1), ("pkg_t", "d.cpu.pkg_t", 0), ("max_t", "d.cpu.max_t", 0),
    ("ia_w", "d.cpu.ia_w", 1), ("sa_w", "d.cpu.sa_w", 1), ("ring_clk", "d.cpu.ring_clk", 0),
    ("ring_v", "d.cpu.ring_v", 3), ("uncore_vid", "d.oc.uncore_vid", 3), ("vcore", "d.cpu.vcore", 3),
    ("vr_v", "d.vrm.v", 3), ("vr_a", "d.vrm.a", 1), ("vr_w", "d.vrm.w", 1), ("vr_t", "d.cpu.vr_t", 0),
    ("vrm_t", "d.cpu.vrm_t", 0), ("latch_min", "d.oc.latch_Min", 3), ("latch_max", "d.oc.latch_Max", 3),
    ("bclk", "d.oc.bclk", 2), ("ngu_clk", "d.oc.ngu_clk", 0), ("d2d_clk", "d.oc.d2d_clk", 0),
    ("vccsa", "d.mb.vccsa", 3), ("imc", "d.mb.imc", 3), ("v12", "d.mb.v12", 3),
    ("mem_clk", "d.mem.clk", 0), ("vddq", "d.mem.VccDDQ", 3),
    ("gpu_w", "v.gpu_w", 0), ("gpu_hot", "v.gpu_hot", 0), ("gpu_load", "v.gpu_load", 0), ("cube", "v.t_sensor", 1),
]
FLIGHT_HEAD = (["time", "ts"] + [g[0] for g in GLOB] + ["limits", "thr", "mode", "prog"] +
               [f"c{i}_{k}" for i in range(24) for k in CORE_FIELDS] + ["procs"])

BUGCHECKS = {
    0x0A: ("IRQL_NOT_LESS_OR_EQUAL", "The kernel read memory it must not touch at that moment. On an overclock this "
           "is usually a core that computed a wrong address, not a real driver bug."),
    0x1A: ("MEMORY_MANAGEMENT", "Windows found its memory bookkeeping corrupted: RAM / memory controller first, CPU second."),
    0x1E: ("KMODE_EXCEPTION_NOT_HANDLED", "Kernel code crashed on an exception nobody caught: corrupted data or a bad instruction result."),
    0x3B: ("SYSTEM_SERVICE_EXCEPTION", "The kernel crashed with an access violation while serving a program. With no "
           "third-party driver on the stack it points to the core miscomputing."),
    0x4E: ("PFN_LIST_CORRUPT", "Windows' page list got corrupted: typical of unstable RAM / memory controller."),
    0x50: ("PAGE_FAULT_IN_NONPAGED_AREA", "Code touched memory that does not exist: a bad pointer, often from a bit flip."),
    0x7E: ("SYSTEM_THREAD_EXCEPTION_NOT_HANDLED", "A system thread crashed on an exception: corrupted data or code."),
    0x7F: ("UNEXPECTED_KERNEL_MODE_TRAP", "The CPU raised a trap the kernel cannot handle, often a double fault from instability."),
    0x9C: ("MACHINE_CHECK_EXCEPTION", "The CPU itself reported an uncorrectable hardware error (core, cache or fabric)."),
    0x101: ("CLOCK_WATCHDOG_TIMEOUT", "One core stopped answering the timer interrupt: it hung. The classic sign of a "
            "core that is not stable at its clock / voltage."),
    0x109: ("CRITICAL_STRUCTURE_CORRUPTION", "Protected kernel data changed: memory corruption or a bit flip."),
    0x116: ("VIDEO_TDR_FAILURE", "The graphics driver stopped responding and could not be reset. On this PC it has "
            "followed a full system freeze, so the GPU driver is usually the victim, not the cause."),
    0x117: ("VIDEO_TDR_TIMEOUT_DETECTED", "The graphics driver stopped responding (recovered)."),
    0x124: ("WHEA_UNCORRECTABLE_ERROR", "A hardware error the CPU could not correct: core, cache, ring or memory controller."),
    0x133: ("DPC_WATCHDOG_VIOLATION", "A driver or core spent too long at high priority: a hang."),
    0x139: ("KERNEL_SECURITY_CHECK_FAILURE", "A kernel data structure failed its integrity check: corruption."),
    0x153: ("KERNEL_STACK_LOCKED_AT_EXIT", "A thread ended with its stack still locked: corrupted kernel state."),
    0x162: ("KERNEL_AUTO_BOOST_INVALID_LOCK_RELEASE", "A thread released a lock it never held: corrupted kernel state."),
    0xEF: ("CRITICAL_PROCESS_DIED", "A process Windows cannot live without died, often after a silent corruption."),
}
WHEA_TYPES = {9: "cache hierarchy (L2/L3)", 10: "TLB", 11: "bus / interconnect", 12: "memory controller", 2: "memory"}


def _path(src, p):
    o = src
    for part in p.split("."):
        if not isinstance(o, dict):
            return None
        o = o.get(part)
    if isinstance(o, dict):
        o = o.get("v")
    return o


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def p_cores():
    """The performance cores (core indexes) from the sensor mapping, else P_CORES."""
    try:
        return set(_p_cores(P_CORES)) if _p_cores else set(P_CORES)
    except Exception:
        return set(P_CORES)


def core_label(c):
    if c is None:
        return None
    pc = p_cores()
    if c in pc:
        return f"P-core {c}"
    if pc == set(P_CORES):                           # E clusters known for this PC's layout only
        for k, cs in CLUSTERS.items():
            if c in cs:
                return f"E-core {c} (cluster {k})"
        return f"core {c}"
    return f"E-core {c}"


# ---------------------------------------------------------------- crash dump (only a copy the user made is readable)
def dump_info(name):
    """CPU, faulting module and process from a kernel minidump copied into logs/ or logs/dumps/ (None if not there)."""
    if not name:
        return None
    base = os.path.basename(name.replace("\\", "/"))
    for p in (os.path.join(LOGS, "dumps", base), os.path.join(LOGS, base), name):
        try:
            with open(p, "rb") as f:
                b = f.read()
            break
        except OSError:
            continue
    else:
        return None
    try:
        if b[:8] != b"PAGEDU64":
            return None
        u32 = lambda o: struct.unpack_from("<I", b, o)[0]
        u64 = lambda o: struct.unpack_from("<Q", b, o)[0]
        params = [u64(0x40 + 8 * i) for i in range(4)]
        f = struct.unpack_from("<18I", b, 0x2000)
        cso, csz, prcb, proc, dlo, dcnt = f[10], f[11], f[7], f[8], f[12], f[13]

        def dstr(off):
            if off <= 0 or off + 4 > len(b):
                return None
            n = u32(off)
            if not 0 < n < 260 or off + 4 + 2 * n > len(b):
                return None
            try:
                return b[off + 4: off + 4 + 2 * n].decode("utf-16le")
            except UnicodeDecodeError:
                return None
        mods = []
        for stride in range(0x60, 0x140, 8):
            tmp = []
            for i in range(dcnt):
                e = dlo + i * stride
                if e + 0x50 > len(b):
                    tmp = None; break
                nm, base_, sz = dstr(u32(e)), u64(e + 0x38), u32(e + 0x48)
                if not nm or base_ >> 44 != 0xFFFFF or not 0x1000 <= sz < 0x8000000:
                    tmp = None; break
                tmp.append((base_, sz, nm))
            if tmp:
                mods = tmp; break

        def where(a):
            for b0, sz, nm in mods:
                if b0 <= a < b0 + sz:
                    return f"{nm}+0x{a - b0:x}"
            return None
        stack, seen = [], set()
        if cso and csz and cso + csz <= len(b):
            for o in range(cso, cso + csz - 7, 8):
                w = where(u64(o))
                if w:
                    m = w.split("+")[0]
                    if m not in seen:
                        seen.add(m); stack.append(m)
        procs = re.findall(rb"[A-Za-z0-9_.\-]{3,14}\.exe", b[proc:proc + 0x1000]) if proc else []
        return {"cpu": struct.unpack_from("<H", b, prcb + 0x24)[0] if prcb else None,
                "fault": next((where(x) for x in params if where(x)), None),
                "modules": stack[:8], "process": procs[0].decode() if procs else None}
    except Exception:
        return None


# ---------------------------------------------------------------- recorder
class Analyzer:
    def __init__(self, hub=None):
        self.hub = hub
        os.makedirs(DIR, exist_ok=True)
        os.makedirs(os.path.join(LOGS, "dumps"), exist_ok=True)   # where the page tells you to copy crash dumps
        self.lock = threading.Lock()
        self.blk = threading.Lock()                          # incident building / cache (page + background thread)
        self.st = {"enabled": False, "since": None}
        try:
            with open(STATE, encoding="utf-8") as f:
                self.st.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.f = self.w = None
        self.day = None
        self.last_sample = None
        self.samples = 0
        try:
            with open(CACHE, encoding="utf-8") as f:
                c = json.load(f)
            self.cache = c.get("items", {}) if c.get("ver") == CACHE_VER else {}
        except (OSError, ValueError):
            self.cache = {}
        self.pruned = None
        self.sampler = procs.Sampler() if procs else None

    # -- on / off
    def set_enabled(self, on):
        with self.lock:
            if bool(on) != bool(self.st["enabled"]):
                self.st["enabled"] = bool(on)
                self.st["since"] = time.strftime("%Y-%m-%d %H:%M:%S") if on else self.st.get("since")
                if on:
                    self.st["started"] = self.st["since"]
                else:
                    self.st["stopped"] = time.strftime("%Y-%m-%d %H:%M:%S")
                tmp = STATE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.st, f)
                os.replace(tmp, STATE)
            if not on and self.f:
                self.f.close(); self.f = None; self.day = None
        if self.hub:
            self.hub.add_event(f"# ANALYZER {'ON: flight recorder running' if on else 'OFF'}")

    def record(self):
        if not self.st["enabled"] or not self.hub:
            return
        with self.hub.lock:
            pc = dict(self.hub.pc)
            row = self.hub.rows[-1] if self.hub.rows else None
        if pc.get("status") != "ok" or time.time() - pc.get("t", 0) > 3:
            return
        d = pc.get("d") or {}
        src = {"v": pc.get("v") or {}, "d": d}
        out = [time.strftime("%H:%M:%S"), int(time.time())]
        for name, p, nd in GLOB:
            x = _f(_path(src, p))
            out.append(None if x is None else round(x, nd))
        lim = d.get("lim") or {}
        out.append("|".join(k for k, x in lim.items() if _f((x or {}).get("v") if isinstance(x, dict) else x)
                            and k != "IA: Max Turbo Limit"))
        thr = d.get("thr") or {}
        out.append("|".join(k for k, x in thr.items() if _f((x or {}).get("v") if isinstance(x, dict) else x)))
        fresh = row is not None and not row.get("hist") and time.time() - row.get("t", 0) < 5
        out.append(row.get("mode") if fresh else None)
        out.append(getattr(getattr(self.hub, "prog", None), "p", None))
        core = d.get("core") or {}
        for i in range(24):
            c = core.get(str(i)) or {}
            for k, nd in (("clk", 0), ("eclk", 0), ("v", 3), ("use", 1), ("t", 0), ("c0", 1), ("c3", 1)):
                x = _f(c.get(k))
                out.append(None if x is None else round(x, nd))
        out.append(procs.fmt(self.sampler.top()) if self.sampler else "")
        day = time.strftime("%Y%m%d")
        if day != self.day:
            if self.f:
                self.f.close()
            path = os.path.join(DIR, f"flight_{day}.csv")
            if os.path.exists(path):                         # older column set: start a new file, keep the old one
                with open(path, encoding="utf-8", errors="ignore") as f:
                    head = f.readline().strip().split(",")
                if head != FLIGHT_HEAD:
                    n = 1
                    while os.path.exists(os.path.join(DIR, f"flight_{day}_part{n}.csv")):
                        n += 1
                    os.replace(path, os.path.join(DIR, f"flight_{day}_part{n}.csv"))
            new = not os.path.exists(path)
            self.f = open(path, "a", newline="", encoding="utf-8")
            self.w = csv.writer(self.f)
            self.day = day
            if new:
                self.w.writerow(FLIGHT_HEAD)
            self.prune()
        self.w.writerow(out)
        self.f.flush()
        self.last_sample = time.time()
        self.samples += 1

    def prune(self):
        cut = time.time() - KEEP_DAYS * 86400
        for n in os.listdir(DIR):
            if n.startswith("flight_") and os.path.getmtime(os.path.join(DIR, n)) < cut:
                try:
                    os.remove(os.path.join(DIR, n))
                except OSError:
                    pass

    def run(self, stop):
        last_warm = 0.0
        while not stop.is_set():
            t0 = time.time()
            try:
                self.record()
            except Exception:
                pass
            if t0 - last_warm > 20:                          # analyse new incidents in the background, one per pass
                last_warm = t0
                try:
                    todo = [i["id"] for i in self.incidents() if i["id"] not in self.cache]
                    if todo:
                        self.detail(todo[-1])
                except Exception:
                    pass
            stop.wait(max(0.05, 1.0 - (time.time() - t0)))

    # ------------------------------------------------------------ data access
    @staticmethod
    def _read(path):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                txt = f.read().replace("\x00", "")
        except OSError:
            return []
        return [r for r in csv.DictReader(txt.splitlines()) if len(r.get("time") or "") == 8]

    def window(self, end, seconds=LEAD_S):
        """Samples (dicts) from end - seconds .. end (local datetime), flight log first, else cores + pcs2 logs."""
        start = end - dt.timedelta(seconds=seconds)
        days = sorted({start.strftime("%Y%m%d"), end.strftime("%Y%m%d")})
        out, source = [], "flight"
        for day in days:
            rows = [r for fp in sorted(glob.glob(os.path.join(DIR, f"flight_{day}_part*.csv")))
                    + [os.path.join(DIR, f"flight_{day}.csv")] for r in self._read(fp)]
            for r in rows:
                t = dt.datetime.strptime(day + r["time"], "%Y%m%d%H:%M:%S")
                if start <= t <= end:
                    out.append(self._norm(r, t, flight=True))
        if len(out) < 5:                                     # before the recorder existed: per-core + 1 s logs
            source, out = "basic", []
            for day in days:
                cores = {r["time"]: r for r in self._read(os.path.join(LOGS, f"cores_{day}.csv"))}
                main = {r["time"]: r for r in self._read(os.path.join(LOGS, f"pcs2_{day}.csv"))}
                last_main = {}
                for tk in sorted(set(cores) | set(main)):
                    if tk in main:
                        last_main = main[tk]                 # the 1 s log skips seconds: carry it forward
                    t = dt.datetime.strptime(day + tk, "%Y%m%d%H:%M:%S")
                    if start <= t <= end:
                        r = dict(last_main)
                        r.update(cores.get(tk, {}))
                        out.append(self._norm(r, t, flight=False))
        out.sort(key=lambda s: s["dt"])
        return out, source

    @staticmethod
    def _norm(r, t, flight):
        g = {}
        if flight:
            for name, _, _ in GLOB:
                g[name] = _f(r.get(name))
            g["limits"] = r.get("limits") or ""
            g["thr"] = r.get("thr") or ""
            g["mode"], g["prog"] = r.get("mode"), r.get("prog")
        else:
            for a, b in (("cpu_w", "cpu_w"), ("cpu_load", "cpu_load"), ("pkg_t", "cpu_t"), ("gpu_w", "gpu_w"),
                         ("gpu_hot", "gpu_hot"), ("gpu_load", "gpu_load"), ("cube", "cube"), ("vrm_t", "vrm_t")):
                g[a] = _f(r.get(b))
            g["mode"], g["prog"] = r.get("mode"), r.get("prog")
        c = [[_f(r.get(f"c{i}_{k}")) for k in CORE_FIELDS] for i in range(24)]
        return {"dt": t, "t": t.strftime("%H:%M:%S"), "g": g, "c": c,
                "p": procs.parse(r.get("procs")) if (flight and procs) else []}

    # ------------------------------------------------------------ incidents
    def events(self):
        try:
            with open(EVENTS, encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except OSError:
            return []

    def incidents(self):
        """Grouped incident list (newest last), each with an id; cheap (no window reads)."""
        ev = self.events()
        for e in ev:
            e["_t"] = dt.datetime.strptime(e["event_time"], "%Y-%m-%d %H:%M:%S")
        ev.sort(key=lambda e: (e["_t"], int(e["record"] or 0)))      # the file is in logging order, not event order
        used, out = set(), []
        for e in ev:
            if e["record"] in used:
                continue
            if e["kind"] == "bugcheck":
                kp = next((k for k in ev if k["kind"] == "unexpected restart" and k["record"] not in used
                           and abs((k["_t"] - e["_t"]).total_seconds()) < 120), None)
                used.add(e["record"])
                if kp:
                    used.add(kp["record"])
                out.append({"id": e["record"], "kind": "crash", "ev": e, "boot": (kp or e)["_t"], "kp": kp})
            elif e["kind"] == "unexpected restart":
                bc = next((b for b in ev if b["kind"] == "bugcheck" and b["record"] not in used
                           and abs((b["_t"] - e["_t"]).total_seconds()) < 120), None)
                if bc:
                    continue                                 # handled with its bugcheck
                used.add(e["record"])
                out.append({"id": e["record"], "kind": "reset", "ev": e, "boot": e["_t"]})
            elif e["kind"].startswith("GPU"):
                used.add(e["record"])
                prev = out[-1] if out and out[-1]["kind"] == "gpu" else None
                if prev and (e["_t"] - prev["last"]).total_seconds() <= 90:
                    prev["evs"].append(e); prev["last"] = e["_t"]          # one burst = one incident
                    if e["kind"] == "GPU reset":
                        prev["ev"] = dict(prev["ev"], kind="GPU reset")
                else:
                    out.append({"id": e["record"], "kind": "gpu", "ev": dict(e), "evs": [e], "last": e["_t"],
                                "boot": None})
            elif e["kind"] in ("WHEA boot info", "WHEA 3"):   # firmware boot records (cper.py): not CPU errors
                continue
            elif e["kind"].startswith("WHEA"):
                used.add(e["record"])
                out.append({"id": e["record"], "kind": "silent" if "corrected" in e["kind"] else "whea", "ev": e,
                            "boot": None})
        out.sort(key=lambda i: i["ev"]["_t"])
        return out

    def summary(self):
        inc = self.incidents()
        lst = []
        per_core = {}
        for i in inc:
            d = self.cache.get(i["id"]) or {}
            e = i["ev"]
            core = d.get("core") if d else (int(e["core"]) if (e.get("core") or "").isdigit() else None)
            code = d.get("code") or e.get("code") or ""
            lst.append({"id": i["id"], "kind": i["kind"], "time": d.get("time") or e["event_time"],
                        "code": code, "name": d.get("name") or self._name(i), "core": core,
                        "core_label": core_label(core), "ready": bool(d)})
            if core is not None and i["kind"] != "gpu":
                k = per_core.setdefault(str(core), {"crash": 0, "silent": 0, "reset": 0})
                k["crash" if i["kind"] == "crash" else ("silent" if i["kind"] in ("silent", "whea") else "reset")] += 1
        with self.lock:
            st = dict(self.st)
        return {"enabled": st["enabled"], "since": st.get("since"), "stopped": st.get("stopped"),
                "last_sample": self.last_sample, "samples": self.samples, "incidents": lst, "per_core": per_core,
                "counts": {k: sum(1 for x in lst if x["kind"] == k) for k in ("crash", "reset", "silent", "gpu")},
                "flight_mb": round(sum(os.path.getsize(os.path.join(DIR, n)) for n in os.listdir(DIR)
                                       if n.startswith("flight_")) / 1e6, 1)}

    @staticmethod
    def _name(i):
        e = i["ev"]
        if i["kind"] == "crash":
            try:
                return BUGCHECKS.get(int(e["code"], 16), ("Bugcheck " + e["code"],))[0]
            except ValueError:
                return "Crash"
        if i["kind"] == "reset":
            return "Hard reset / power loss"
        if i["kind"] == "gpu":
            return "GPU driver reset (TDR)" if i["ev"]["kind"] == "GPU reset" else "GPU driver error"
        return "Corrected cache error"

    def detail(self, iid):
        with self.blk:
            return self._detail(iid)

    def _detail(self, iid):
        if iid in self.cache:
            return self.cache[iid]
        inc = next((i for i in self.incidents() if i["id"] == iid), None)
        if not inc:
            return None
        d = self.build(inc)
        # cache only when the lead-up is in the past for sure (a WHEA a few seconds old still lacks its own second)
        if d and (dt.datetime.now() - dt.datetime.strptime(d["time"], "%Y-%m-%d %H:%M:%S")).total_seconds() > 90:
            self.cache[iid] = d
            tmp = CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"ver": CACHE_VER, "items": self.cache}, f)
            os.replace(tmp, CACHE)
        return d

    def build(self, inc):
        e, kind = inc["ev"], inc["kind"]
        code = e.get("code") or ""
        core = int(e["core"]) if (e.get("core") or "").isdigit() else None
        how = None
        if core is not None:
            how = "APIC ID in the WHEA record" if kind in ("silent", "whea") else "the bugcheck names the hung CPU"
        dump = None
        params = []
        dpath = None
        if kind == "crash":
            m = re.search(r"\(([^)]*)\)", e.get("detail") or "")
            params = [p.strip() for p in m.group(1).split(",")] if m else []
            dm = re.search(r"([A-Z]:\\[^ ]+\.dmp)", e.get("detail") or "")
            dpath = dm.group(1) if dm else None
            dump = dump_info(dpath)
            if dump and dump.get("cpu") is not None and core is None:
                core, how = dump["cpu"], "from the crash dump"

        # failure moment
        if kind in ("silent", "whea", "gpu"):
            fail = inc["ev"]["_t"]
            data, source = self.window(fail + dt.timedelta(seconds=1))
        else:
            boot = inc["boot"]
            data, source = self.window(boot - dt.timedelta(seconds=5), seconds=900)
            # the last sample before the reboot gap is the moment it went down
            if data:
                gaps = [i for i in range(1, len(data)) if (data[i]["dt"] - data[i - 1]["dt"]).total_seconds() > 30]
                if gaps:
                    data = data[:gaps[-1]] if (boot - data[-1]["dt"]).total_seconds() < 60 else data
            fail = data[-1]["dt"] if data else boot
            data = [s for s in data if (fail - s["dt"]).total_seconds() <= LEAD_S]
        at = data[-1] if data else None

        # frozen sensors at the end (identical samples)
        frozen = 0
        if kind not in ("silent", "whea", "gpu") and len(data) > 2:
            sig = lambda s: tuple(tuple(x) for x in s["c"]) + (s["g"].get("cpu_w"),)
            last = sig(data[-1])
            for s in reversed(data[:-1]):
                if sig(s) != last:
                    break
                frozen += 1

        # the core to show: the implicated one, else the busiest core over the last 5 s
        show = core
        if show is None and at:
            tail = data[-5:]
            show = max(range(24), key=lambda i: sum((s["c"][i][1] or 0) for s in tail))
        gap = None
        if kind not in ("silent", "whea", "gpu") and at:
            gap = (inc["boot"] - at["dt"]).total_seconds()
        series = []
        for s in data:
            c = s["c"][show] if show is not None else [None] * 5
            series.append({"t": s["t"], "clk": c[0], "eclk": c[1], "v": c[2], "use": c[3], "temp": c[4],
                           "cpu_w": s["g"].get("cpu_w"), "load": s["g"].get("cpu_load"), "pkg_t": s["g"].get("pkg_t"),
                           "ring_clk": s["g"].get("ring_clk"), "ring_v": s["g"].get("ring_v"),
                           "vr_v": s["g"].get("vr_v"), "gpu_w": s["g"].get("gpu_w"),
                           "top": (f"{s['p'][0][0]} {s['p'][0][1]} %" if s.get("p") else None)})

        # busiest programs over the last PROC_S s (100 % = one core): (name, average, peak)
        acc = {}
        tail = data[-PROC_S:]
        for s in tail:
            for n, p in s.get("p") or []:
                a = acc.setdefault(n, [0, 0])
                a[0] += p
                a[1] = max(a[1], p)
        top_procs = sorted(((n, round(a[0] / len(tail)), a[1]) for n, a in acc.items()),
                           key=lambda x: (-x[1], -x[2]))[:8] if tail else []
        top_procs = [x for x in top_procs if x[1] >= 2 or x[2] >= 20]

        findings = []
        name, plain = self._name(inc), ""
        if kind == "crash":
            try:
                name, plain = BUGCHECKS.get(int(code, 16), (name, ""))
            except ValueError:
                pass
        elif kind == "reset":
            plain = ("The PC restarted without a crash screen and without a bugcheck: the reset button, a power cut, or "
                     "the VRM / PSU protection. If the sensors froze first, it was a hard hang that only the reset "
                     "button could end.")
        elif kind == "gpu":
            reset = e["kind"] == "GPU reset"
            plain = ("The NVIDIA driver " + ("stopped getting answers from the GPU and reset it (a TDR). The screen goes "
                     "black for a few seconds and the game usually crashes, but Windows keeps running. " if reset else
                     "logged a GPU error (a channel or engine fault). ") +
                     "Common causes: a GPU core or memory overclock that is not stable, the power delivery to the card, "
                     "or the driver itself. If these keep coming with the CPU at stock, the problem is on the GPU side.")
        else:
            lvl = e.get("detail") or ""
            plain = ("A silent fault: the CPU found a wrong bit in its cache and fixed it on the fly (" + lvl + "). "
                     "Nothing crashed, but it means that part of the chip is at the edge at these settings. A pile-up of "
                     "these usually comes before crashes.")
        if frozen >= 2:
            findings.append(("bad", f"Sensors froze {frozen} s before the end: the whole PC hung before Windows could react."))
        if gap is not None and gap > 240:
            findings.append(("warn", f"Logging stopped {gap / 60:.0f} min before the restart (dashboard or HWiNFO not "
                                     f"running, or the PC hung): the moment shown is the last logged second, not the crash."))
        if at:
            g = at["g"]
            c = at["c"][show] if show is not None else None
            load = g.get("cpu_load")
            cls = None
            if load is not None:
                cls = "idle" if load < 5 else "light load" if load < 25 else "medium load" if load < 60 else "heavy load"
            if c and c[3] is not None and c[3] >= 90:
                findings.append(("info", f"{core_label(show)} was flat out ({c[3]:.0f} % load) at {fmt_clk(c[0])}."))
            elif c and c[3] is not None:
                findings.append(("info", f"{core_label(show)} at {c[3]:.0f} % load, {fmt_clk(c[0])}; the CPU overall at "
                                         f"{'' if load is None else f'{load:.0f} % '}({cls})."))
            # ratio drop on the core in the last 5 s
            recent = data[-6:]
            clks = [s["c"][show][0] for s in recent if show is not None and s["c"][show][0]]
            if len(clks) >= 2 and max(clks) - clks[-1] >= 300:
                findings.append(("warn", f"Its ratio dropped {max(clks) / 100:.0f}x → {clks[-1] / 100:.0f}x in the last "
                                         f"{len(recent)} s (a P-state step under load)."))
            ws = [s["g"].get("cpu_w") for s in data[-6:] if s["g"].get("cpu_w") is not None]
            if len(ws) >= 2 and max(ws) - min(ws[:-1] or ws) > 100 and ws.index(max(ws)) > 0:
                findings.append(("warn", f"Load step: CPU power jumped {min(ws):.0f} → {max(ws):.0f} W within seconds."))
            # hottest core near the end (package temp = hottest core) and the shown core's own temperature
            hot = [max(x for x in (s["g"].get("pkg_t"), s["g"].get("max_t")) if x is not None)
                   for s in data[-15:] if s["g"].get("pkg_t") is not None or s["g"].get("max_t") is not None]
            ct = [s["c"][show][4] for s in data[-15:] if show is not None and s["c"][show][4] is not None]
            water = g.get("cube")
            wtxt = "" if water is None else f", water {water:.0f} °C"
            if hot:
                mx = max(hot)
                own = f"; {core_label(show)} itself {max(ct):.0f} °C" if ct else ""
                if mx >= 85:
                    findings.append(("bad", f"Hot core: {mx:.0f} °C in the last 15 s{own}{wtxt}. A core this hot needs "
                                            "more voltage than the same clock on cold water: a V/F curve tuned at 12–13 °C "
                                            "water has no margin here."))
                elif mx >= 75:
                    findings.append(("warn", f"Warm: hottest core {mx:.0f} °C in the last 15 s{own}{wtxt}."))
                else:
                    findings.append(("ok", f"Temperatures normal: hottest core {mx:.0f} °C in the last 15 s{own}{wtxt}."))
            if g.get("limits"):
                findings.append(("warn", "Limits active: " + g["limits"].replace("|", ", ")))
            if top_procs:
                hog = [(n, p) for n, p, pk in top_procs if pk >= 80]
                if hog:
                    n, p = hog[0]
                    findings.append(("warn", f"{n} was using a whole core (peak {max(pk for nn, _, pk in top_procs if nn == n)} %, "
                                             f"avg {p} % over the last {PROC_S} s): that is the program that loaded the core."))
                findings.append(("info", "Busiest programs (last %d s): " % PROC_S +
                                 ", ".join(f"{n} {p} %" for n, p, _ in top_procs[:5])))
            if (g.get("gpu_load") or 0) > 80:
                findings.append(("info", f"GPU busy ({g['gpu_load']:.0f} %, {g.get('gpu_w') or 0:.0f} W): gaming / 3D load."))
        if kind == "gpu":
            evs = inc.get("evs") or [e]
            span = (evs[-1]["_t"] - evs[0]["_t"]).total_seconds()
            findings.insert(0, ("warn", f"{len(evs)} driver message{'s' if len(evs) > 1 else ''}"
                                        f"{f' over {span:.0f} s' if span >= 1 else ''}: " +
                                "; ".join(sorted({(x.get('detail') or x['code'])[:60] for x in evs}))))
            if at:
                g = at["g"]
                if g.get("gpu_load") is not None and g.get("gpu_load") <= 80:
                    findings.append(("info", f"GPU at that moment: {g.get('gpu_load') or 0:.0f} % load, "
                                             f"{g.get('gpu_w') or 0:.0f} W, hot spot {g.get('gpu_hot') or 0:.0f} °C."))
            later = [i for i in self.incidents() if i["kind"] in ("crash", "reset")
                     and 0 <= (i["boot"] - fail).total_seconds() <= 900]
            if later:
                findings.append(("bad", f"The PC went down {(later[0]['boot'] - fail).total_seconds() / 60:.0f} min "
                                        f"later ({self._name(later[0])})."))
        if kind in ("crash", "reset"):
            gpu_before = [i for i in self.incidents() if i["kind"] == "gpu"
                          and 0 <= (fail - i["ev"]["_t"]).total_seconds() <= 900]
            if gpu_before:
                gb = gpu_before[-1]
                findings.insert(0, ("warn", f"The NVIDIA driver logged {'a reset' if gb['ev']['kind'] == 'GPU reset' else 'an error'} "
                                            f"{(fail - gb['ev']['_t']).total_seconds() / 60:.1f} min before this: "
                                            "the GPU side may be involved."))
        if kind == "crash" and code.lower().endswith("116"):
            findings.append(("info", "A VIDEO_TDR here has followed full-system freezes before: look at the CPU first."))
        if dump:
            if dump.get("process"):
                findings.append(("info", f"Running process: {dump['process']}"))
            if dump.get("modules"):
                third = [m for m in dump["modules"] if m.lower() not in ("ntoskrnl.exe", "hal.dll", "crashdmp.sys")]
                findings.append(("info", "Code on the stack: " + ", ".join(dump["modules"][:6]) +
                                 ("" if third else " (only Windows itself: no third-party driver)")))
        if source == "basic":
            findings.append(("info", "Basic data only (before the flight recorder ran): no temperatures per core, ring or VR."))
        if not data:
            findings.append(("warn", "No logged data around this moment (dashboard or HWiNFO not running)."))

        cores = None
        if at:
            pcs = p_cores()
            cores = [{"i": i, "label": core_label(i), "p": i in pcs, "clk": at["c"][i][0], "eclk": at["c"][i][1],
                      "v": at["c"][i][2], "use": at["c"][i][3], "t": at["c"][i][4],
                      "c0": at["c"][i][5], "c3": at["c"][i][6]} for i in range(24)]
        recs = [x["record"] for x in (inc.get("evs") or [e]) if x.get("record")]     # Event Viewer button
        if inc.get("kp") and inc["kp"].get("record"):
            recs.append(inc["kp"]["record"])
        return {"id": inc["id"], "kind": kind, "code": code, "name": name, "plain": plain, "records": recs,
                "time": fail.strftime("%Y-%m-%d %H:%M:%S"), "logged": e["event_time"],
                "core": core, "core_label": core_label(core), "how": how, "shown_core": show,
                "shown_label": core_label(show), "params": params, "dump_file": dpath,
                "dump": dump, "source": source, "frozen": frozen,
                "at": None if not at else {"t": at["t"], "g": at["g"]}, "series": series, "cores": cores,
                "procs": [{"name": n, "avg": a, "peak": pk} for n, a, pk in top_procs],
                "procs_now": [{"name": n, "pct": p} for n, p in (at.get("p") if at else []) or []],
                "findings": [{"lvl": a, "text": b} for a, b in findings]}


def fmt_clk(c):
    return "?" if not c else f"{c / 100:.0f}x ({c / 1000:.2f} GHz)"


def start(hub, stop):
    a = Analyzer(hub)
    threading.Thread(target=a.run, args=(stop,), daemon=True).start()
    return a
