"""PC sensors from the standalone reader (tools/pc_sensors, LibreHardwareMonitor + PawnIO), in the same shape as
hwinfo.HWiNFO.read(): (status, {PICK key: {"v", "u"}}, detail tree). 2026-11-22 (owner: run without HWiNFO, whose
polling kept one core busy all the time).

Mapping (checked against HWiNFO side by side, logs/sensors/compare_*.csv, `py lhm_compare.py --report`):
  - CPU and GPU by hardware type and sensor NAME, not by LHM id number (2026-11-23, docs/hw_support.md; ids are
    running counters that shift with what a CPU exposes). Per-core values: P-Core #k / E-Core #k / CPU Core #n /
    Core #n in creation order (never by the #k), loads averaged over "CPU Core #n Thread #m"; a list whose length
    doesn't match the core count is dropped rather than misaligned. Core kind from the name, else the reader's topology
    (EfficiencyClass), else P. On this PC the order is APIC order = logical order (P P, 8 E, P P P P, 8 E, P P).
    Effective clock, C0, throttling, limit reasons and the ring clock come from the reader's own MSR block (per
    logical processor, mapped to cores through "topo")
  - fan headers (NCT6701D): #1 Chassis1 (top), #2 CPU (rear 120), #3 Chassis2 (front 360), #6 W_PUMP+ (P1), #7 AIO Pump
  - GPU: the discrete card (largest dedicated memory, then most sensors); memory chips: exactly 8 -> A0 A1 B0 B1 C0 C1
    D0 D1 (this card's layout), else M1..Mn
  - GPU power: the reader has only the card's own total; a shunt mod is corrected with gpu_k, a per-PC setting in
    logs/prefs.json (default 1.0; this PC 1.32: real / reported, 1.30 at idle, ~1.33 expected under load)
  - ASUS EC (cube T_Sensor, VRM): from the registers named in logs/sensors/ec_map.json (`py lhm_compare.py --ec`)
Missing vs HWiNFO: NGU / D2D clocks, VR VCC (SVID) readings, board rails (unscaled), memory timings, WHEA counter.
"""
import json, os, re, time

from hwinfo import cube_cal

HERE = os.path.dirname(os.path.abspath(__file__))
import pcs_paths
NOW = os.path.join(pcs_paths.SENSORS, "now.json")
PREFS = os.path.join(pcs_paths.LOGS, "prefs.json")
FRESH_S = 8                  # under an all-core load a pass can take a few seconds: hold the last values
P_CORES = (0, 1, 10, 11, 12, 13, 22, 23)   # fallback only (this PC) while no reader data has given the real list
GMEM = ["A0", "A1", "B0", "B1", "C0", "C1", "D0", "D1"]


_gk = {"t": 0.0, "v": 1.0}


def gpu_k():
    """GPU power correction for a shunt mod (real / reported), per PC: "gpu_k" in logs/prefs.json, default 1.0.
    Read from the file directly (re-read every 30 s): pcs_dash.prefs_load() only returns its own known keys."""
    if time.time() - _gk["t"] >= 30:
        _gk["t"] = time.time()
        v = 1.0
        try:
            with open(PREFS, encoding="utf-8") as f:
                x = json.load(f).get("gpu_k", 1.0)
            if isinstance(x, (int, float)) and not isinstance(x, bool) and 0.5 <= x <= 3.0:
                v = float(x)
        except (OSError, ValueError, AttributeError):
            pass
        _gk["v"] = v
    return _gk["v"]


GPU_K = gpu_k()              # value at import (fast_rules sends it to the reader's fan path)


# ------------------------------------------------------------------ name-based CPU / GPU mapping (2026-11-23)
_CORE_RE = re.compile(r"^(P-Core|E-Core|CPU Core|Core) #(\d+)$")              # temps / clocks ("Distance to TjMax",
_VID_RE = re.compile(r"^(P-Core|E-Core|CPU Core|Core) #(\d+)(?: VID)?$")      #  "(Effective)" don't match)
_LOAD_RE = re.compile(r"^CPU Core #(\d+)(?: Thread #(\d+))?$")
_CCD_RE = re.compile(r"^CCD ?(\d+) \(Tdie\)$")
_GMEM_RE = re.compile(r"^GPU Memory #(\d+)$")
GPU_PREFIX = ("/gpu-nvidia/", "/gpu-amd/", "/gpu-intel/")
CPU_PREFIX = ("/intelcpu/", "/amdcpu/")
GPU_POWER = ("GPU Package", "GPU Total", "Total Card Power", "GPU Board Power", "Package")


def _hw(sid):
    return "/".join(sid.split("/")[:3])                     # "/intelcpu/0/temperature/2" -> "/intelcpu/0"


def _idx(x):
    try:
        return int(x["id"].rsplit("/", 1)[1])
    except (ValueError, IndexError):
        return 1 << 30


def _typed(lst, typ):
    """Sensors of one type in creation order (LHM id index)."""
    return sorted((x for x in lst if x.get("type") == typ), key=_idx)


def _named(lst, typ, *names):
    """Value of the first sensor of that type whose name is one of names (in that order of preference), or None."""
    for n in names:
        for x in lst:
            if x.get("type") == typ and x.get("name") == n:
                return x.get("v")
    return None


def _group(sens, prefixes):
    """{hardware id: [sensors]} for the hardware whose id starts with one of prefixes, in first-seen order."""
    out = {}
    for x in sens:
        sid = x.get("id", "")
        if sid.startswith(prefixes):
            out.setdefault(_hw(sid), []).append(x)
    return out


def cpu_map(d):
    """Per-core and package CPU values from the reader output d, by name. None when there is no CPU hardware."""
    sens = d.get("sensors") or []
    hws = _group(sens, CPU_PREFIX)
    if not hws:
        return None
    hid, cs = next(iter(hws.items()))
    topo = d.get("topo") if isinstance(d.get("topo"), list) and d.get("topo") else None
    temps = [x for x in _typed(cs, "Temperature") if _CORE_RE.match(x.get("name", ""))]
    clks = [x for x in _typed(cs, "Clock") if _CORE_RE.match(x.get("name", ""))]
    vids = [x for x in _typed(cs, "Voltage") if _VID_RE.match(x.get("name", ""))]
    loads = {}
    for x in _typed(cs, "Load"):
        m = _LOAD_RE.match(x.get("name", ""))
        if m:
            loads.setdefault(int(m.group(1)), []).append(x.get("v"))
    n = len(topo) if topo else (len(loads) or len(temps) or len(clks) or len(vids))

    def aligned(lst):
        return lst if len(lst) == n else None
    temps, clks, vids = aligned(temps), aligned(clks), aligned(vids)
    use = None
    if len(loads) == n:
        use = []
        for k in sorted(loads):
            v = [x for x in loads[k] if x is not None]
            use.append(v[0] if len(v) == 1 else sum(v) / len(v) if v else None)
    # kind: name prefix, else EfficiencyClass (higher = P on hybrid), else P
    kinds = []
    effs = {t.get("eff") for t in topo} if topo else set()
    for i in range(n):
        k = None
        for lst in (temps, clks, vids):
            if lst:
                nm = lst[i].get("name", "")
                k = "P" if nm.startswith("P-Core") else "E" if nm.startswith("E-Core") else None
                if k:
                    break
        if k is None and topo and len(effs) > 1:
            k = "P" if topo[i].get("eff") == max(effs) else "E"
        kinds.append(k or "P")
    lps = [t.get("lp") or [] for t in topo] if topo else None

    def per_core(arr, agg=max):
        """An MSR array (one value per logical processor) folded onto the cores."""
        if not arr:
            return [None] * n
        if lps is not None:
            out = []
            for lp in lps:
                v = [arr[j] for j in lp if isinstance(j, int) and 0 <= j < len(arr) and arr[j] is not None]
                out.append(agg(v) if v else None)
            return out
        return list(arr) if len(arr) == n else [None] * n

    cores = []
    for i in range(n):
        cores.append({"kind": kinds[i],
                      "t": temps[i].get("v") if temps else None, "clk": clks[i].get("v") if clks else None,
                      "v": vids[i].get("v") if vids else None, "use": use[i] if use else None})
    l3s = {t.get("l3") for t in topo} if topo else set()
    if topo and len(l3s) > 1:                                  # several L3s: AMD CCDs (Zen 2: CCXs)
        for i, t in enumerate(topo):
            if isinstance(t.get("l3"), int) and t["l3"] >= 0:
                cores[i]["ccd"] = t["l3"]
    amd = hid.startswith("/amdcpu/")
    vid_cpu = _named(cs, "Voltage", "CPU Core")
    # a real core voltage (AMD SVI2 / SVI3 telemetry or the SMU's VDDCR); Intel's "CPU Core" is a VID
    vcore = _named(cs, "Voltage", "Core (SVI2 TFN)", "Core (SVI3 TFN)", "VDDCR", "VDDCR VDD", "Vcore")
    if vcore is None and amd:
        vcore = vid_cpu
    # AMD SoC rail and the power split (2026-11-24, a friend's Ryzen 7 7700 showed none of it): names as the library
    # uses them across Zen generations (SVI2 telemetry, SMU PM table)
    soc_v = _named(cs, "Voltage", "SoC (SVI2 TFN)", "SoC (SVI3 TFN)", "VDDCR SoC", "VDDCR_SOC", "SoC Voltage", "VSOC", "SoC")
    soc_w = _named(cs, "Power", "SoC (SVI2 TFN)", "SoC (SVI3 TFN)", "SOC Power", "SoC", "VDDCR SoC")
    cores_w = _named(cs, "Power", "CPU Cores", "Cores", "Core (SVI2 TFN)", "Core (SVI3 TFN)", "VDDCR")
    if cores_w is None:                                    # per-core powers (AMD "Core #n (SMU)"): their sum
        pw = [x.get("v") for x in _typed(cs, "Power") if re.match(r"^Core #\d+", x.get("name", "")) and x.get("v") is not None]
        cores_w = sum(pw) if pw else None
    if vid_cpu is None:
        v = [c["v"] for c in cores if c["v"] is not None]
        vid_cpu = sum(v) / len(v) if v else None
    ccd = sorted(((int(m.group(1)), x.get("v")) for x in _typed(cs, "Temperature")
                  if (m := _CCD_RE.match(x.get("name", "")))), key=lambda p: p[0])
    return {
        "hw": hid, "vendor": "amd" if amd else "intel",
        "name": next((x.get("hw") for x in cs if x.get("hw")), None),
        "n": n, "cores": cores, "per_core": per_core,
        "p_cores": [i for i, k in enumerate(kinds) if k == "P"],
        "pkg_t": _named(cs, "Temperature", "CPU Package", "Core (Tctl/Tdie)", "Core (Tdie)", "Core (Tctl)"),
        "max_t": _named(cs, "Temperature", "Core Max", "CCDs Max (Tdie)"),
        "pkg_w": _named(cs, "Power", "CPU Package", "Package"),
        "ia_w": cores_w, "sa_w": soc_w, "soc_v": soc_v,
        "use": _named(cs, "Load", "CPU Total"),
        "bclk": _named(cs, "Clock", "Bus Speed"),
        "vid": vid_cpu, "vcore": vcore,
        "ccd_t": [v for _, v in ccd],
    }


def gpu_map(d):
    """The discrete GPU's values by name (largest dedicated memory, then most sensors). None without a GPU."""
    hws = _group(d.get("sensors") or [], GPU_PREFIX)
    if not hws:
        return None

    def score(item):
        mem = 0.0
        for x in item[1]:
            if x.get("type") in ("Data", "SmallData") and x.get("name") in ("GPU Memory Total", "D3D Dedicated Memory Total") \
                    and isinstance(x.get("v"), (int, float)):
                mem = max(mem, x["v"] * (1048576.0 if x["type"] == "SmallData" else 1.0))
        return mem, len(item[1])
    hid, gs = max(hws.items(), key=score)                     # ties: the first one listed
    chips = [x for x in _typed(gs, "Temperature") if _GMEM_RE.match(x.get("name", ""))]
    names = GMEM if len(chips) == len(GMEM) else [f"M{i + 1}" for i in range(len(chips))]
    return {
        "hw": hid, "name": next((x.get("hw") for x in gs if x.get("hw")), None),
        "t": _named(gs, "Temperature", "GPU Core"), "hot": _named(gs, "Temperature", "GPU Hot Spot"),
        "mem_j": _named(gs, "Temperature", "GPU Memory Junction", "GPU Memory"),
        "v": _named(gs, "Voltage", "GPU Core Voltage", "GPU Core"),
        "clk": _named(gs, "Clock", "GPU Core"), "mem_clk": _named(gs, "Clock", "GPU Memory"),
        "load": _named(gs, "Load", "GPU Core"), "mem_load": _named(gs, "Load", "GPU Memory Controller"),
        "w_raw": _named(gs, "Power", *GPU_POWER),
        "gmem": [(nm, x.get("v")) for nm, x in zip(names, chips)],
    }


_pc = {"t": 0.0, "v": None}


def p_cores(fallback=P_CORES):
    """Indexes of the performance cores in LHM / HWiNFO core order, from the last reader output (any process: the
    dashboard's own read, else shared memory / now.json, cached 60 s). fallback while nothing is known."""
    if _pc["v"] is None and time.time() - _pc["t"] >= 60:
        _pc["t"] = time.time()
        try:
            d = read_raw()
            m = cpu_map(d) if d else None
            if m and m["n"]:
                _pc["v"] = tuple(m["p_cores"])
        except Exception:
            pass
    return _pc["v"] if _pc["v"] is not None else tuple(fallback)
FANS = {"Chassis1": 0, "CPU": 1, "Chassis2": 2, "W_PUMP+": 5, "AIO Pump": 6}
# limit-reason bits (Intel PERF_LIMIT_REASONS, status half) -> HWiNFO's names (the analyzer and the OC panel use them)
LIM_IA = {0: "PROCHOT", 1: "Thermal Event", 4: "Residency State Regulation", 5: "Running Average Thermal Limit",
          6: "VR Thermal Alert", 7: "VR TDC", 8: "Electrical Design Point/Other (ICCmax,PL4,SVID,DDR RAPL)",
          10: "Package-Level RAPL/PBM PL1", 11: "Package-Level RAPL/PBM PL2,PL3", 12: "Max Turbo Limit",
          13: "Turbo Attenuation (MCT)"}
LIM_RING = {0: "PROCHOT", 1: "Thermal Event", 4: "Residency State Regulation", 5: "Running Average Thermal Limit",
            6: "VR Thermal Alert", 7: "VR TDC", 8: "Max VR Voltage, ICCmax, PL4", 10: "Package-Level RAPL/PBM PL1",
            11: "Package-Level RAPL/PBM PL2,PL3"}


RAILS = [("v12", "/lpc/nct6701d/0/voltage/4", 12.0), ("v5", "/lpc/nct6701d/0/voltage/1", 5.0),
         ("v33", "/lpc/nct6701d/0/voltage/3", 1.0), ("bat", "/lpc/nct6701d/0/voltage/8", 1.0)]
EVENTS = os.path.join(pcs_paths.LOGS, "events.csv")
_whea = {"t": 0.0, "n": None}


def whea_since_boot():
    """WHEA errors logged since this boot (events.csv from the event watcher), re-counted every 30 s."""
    if time.time() - _whea["t"] < 30:
        return _whea["n"]
    _whea["t"] = time.time()
    try:
        import csv, ctypes
        k32 = ctypes.windll.kernel32
        k32.GetTickCount64.restype = ctypes.c_uint64         # default int would wrap after 24.8 days
        boot = time.time() - k32.GetTickCount64() / 1000.0
        n = 0
        with open(EVENTS, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("kind", "").startswith("WHEA") and \
                        time.mktime(time.strptime(r["event_time"], "%Y-%m-%d %H:%M:%S")) >= boot:
                    n += 1
        _whea["n"] = n
    except Exception:
        _whea["n"] = None
    return _whea["n"]


SHM_NAME = "Global\\PCS_SENSORS"     # written by PcsSensors every second: [u32 seq][u32 length][JSON]
_shm = {"p": None, "h": None}


def _shm_read():
    """The reader's last JSON from shared memory (sequence lock: retry while a write is in progress), or None."""
    import ctypes
    k = ctypes.windll.kernel32
    if _shm["p"] is None:
        k.OpenFileMappingW.restype = ctypes.c_void_p
        k.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
        h = k.OpenFileMappingW(4, 0, SHM_NAME)          # FILE_MAP_READ
        if not h:
            return None
        p = k.MapViewOfFile(h, 4, 0, 0, 0)
        if not p:
            k.CloseHandle(ctypes.c_void_p(h))
            return None
        _shm["h"], _shm["p"] = h, p
    p = _shm["p"]
    for _ in range(20):
        s1 = ctypes.c_uint32.from_address(p).value
        if s1 & 1:
            time.sleep(0.002)
            continue
        n = ctypes.c_uint32.from_address(p + 4).value
        if not 0 < n < (4 << 20) - 8:
            return None
        raw = ctypes.string_at(p + 8, n)
        if ctypes.c_uint32.from_address(p).value == s1:
            return raw
    return None


def read_raw():
    """The reader's output as a dict: shared memory first, the 5 s debug file as a fallback; None if neither."""
    for _ in range(3):                                       # a read can land on a write: try again shortly
        try:
            raw = _shm_read()
            if raw is not None:
                _shm["last"] = json.loads(raw)
                return _shm["last"]
        except Exception:
            pass
        time.sleep(0.01)
    if _shm.get("last") is not None:                         # the last good copy, never the older 5 s debug file
        return _shm["last"]
    try:
        with open(NOW, "rb") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return None


class Reader:
    def __init__(self):
        self.detail = {}

    def close(self):
        pass

    def read(self):
        d = read_raw()
        if d is None:
            return "sensor reader not running", {}
        age = time.time() - d.get("t", 0)
        if age > FRESH_S:
            return f"sensor reader stale ({age:.0f} s)", {}
        if not d.get("admin"):
            return "sensor reader without admin rights", {}
        s = {x["id"]: x["v"] for x in d.get("sensors", []) if x.get("v") is not None}
        g = lambda i: s.get(i)
        msr = d.get("msr") or {}
        ecv = (d.get("ec") or {}).get("v") or {}

        det = {"core": {}, "cpu": {}, "gpu": {}, "gmem": {}, "dimm": {}, "fan": {}, "oc": {}, "lim": {}, "thr": {},
               "mb": {}, "ssd": {}}
        cm = cpu_map(d)
        if cm:
            eclk = cm["per_core"](msr.get("eclk") or [])
            thr = cm["per_core"]([x if isinstance(x, (int, float)) and x >= 0 else None for x in (msr.get("thr") or [])])
            cst = msr.get("cst") or {}
            c0 = cm["per_core"](cst.get("c0") or [])   # % of time in C0 (MPERF), 2026-11-22
            osc = {k: cm["per_core"](cst.get(k) or [], min) for k in ("c1", "c2", "c3")}   # Windows idle states, 2026-11-23
            for i, mc in enumerate(cm["cores"]):
                c = {"t": mc["t"], "clk": mc["clk"], "v": mc["v"], "use": mc["use"], "eclk": eclk[i], "c0": c0[i],
                     "c1": osc["c1"][i], "c2": osc["c2"][i], "c3": osc["c3"][i]}
                c = {k: round(v, 3) for k, v in c.items() if v is not None}
                c["kind_" + mc["kind"]] = c.get("t", 0)
                if "ccd" in mc:
                    c["ccd"] = mc["ccd"]
                det["core"][str(i)] = c
                if thr[i] is not None:
                    det["thr"][str(i)] = thr[i]
            _pc["v"] = tuple(cm["p_cores"])
        cpu = det["cpu"]
        if cm and cm["name"]:
            cpu["name"] = cm["name"]                       # CPU model, picks the package drawing (2026-11-23)
        for k in ("pkg_t", "max_t", "pkg_w", "ia_w", "sa_w", "soc_v", "use"):
            if cm and cm[k] is not None:
                cpu[k] = round(cm[k], 3)
        bclk = cm["bclk"] if cm else None
        if (msr.get("ring_ratio") or -1) > 0:
            cpu["ring_clk"] = round(msr["ring_ratio"] * (bclk or 100.0), 1)
        if bclk is not None:
            det["oc"]["bclk"] = round(bclk, 3)
        if cm:                                             # new keys (2026-11-23): nothing reads them yet but the analyzer
            cpu["vendor"] = cm["vendor"]
            cpu["p_cores"] = list(cm["p_cores"])
            if cm["vid"] is not None:
                cpu["vid"] = round(cm["vid"], 4)
            if cm["vcore"] is not None:                    # AMD SVI2 / VDDCR only; Intel's comes from HWiNFO (DLVRin)
                cpu["vcore"] = round(cm["vcore"], 4)
            if cm["ccd_t"]:
                cpu["ccd_t"] = [None if v is None else round(v, 2) for v in cm["ccd_t"]]
            ci = d.get("cpu_info") or {}
            for k in ("family", "model", "stepping"):
                if isinstance(ci.get(k), int):
                    cpu[k] = ci[k]
        for bits, names, pre in ((msr.get("lim_ia", -1), LIM_IA, "IA"), (msr.get("lim_ring", -1), LIM_RING, "RING")):
            if bits is not None and bits >= 0:
                for b, n in names.items():
                    det["lim"][f"{pre}: {n}"] = 1 if bits >> b & 1 else 0
        pt = msr.get("pkg_therm", -1)
        if pt is not None and pt >= 0:
            det["lim"]["PKG: Thermal Throttling"] = pt & 1
            det["lim"]["PKG: Power Limit Exceeded"] = pt >> 10 & 1

        gpu = det["gpu"]
        gm = gpu_map(d)
        if gm:
            for k in ("t", "hot", "mem_j", "v", "clk", "mem_clk", "load", "mem_load", "w_raw"):
                if gm[k] is not None:
                    gpu[k] = round(gm[k], 3)
            if gm["name"]:
                gpu["name"] = gm["name"]                   # GPU model, picks the board drawing (2026-11-23)
            if "w_raw" in gpu:
                gpu["w"] = round(gpu["w_raw"] * gpu_k(), 1)
            for chip, v in gm["gmem"]:
                if v is not None:
                    det["gmem"][chip] = v
        for x in d.get("sensors", []):                     # every DIMM with an SPD temperature (any slot, 2026-11-24)
            m = re.match(r"^/memory/dimm/(\d+)/temperature/0$", str(x.get("id", "")))
            if m and x.get("v") is not None:
                det["dimm"][m.group(1)] = {"t": x["v"]}
        if not d.get("ec"):
            # Other PCs (no ASUS EC block, 2026-11-24): board temperatures the library names on the Super I/O / EC.
            # This PC keeps its mapped EC values and HWiNFO; nothing here replaces them.
            board = [x for x in d.get("sensors", []) if x.get("type") == "Temperature" and x.get("v") is not None
                     and str(x.get("id", "")).startswith("/lpc/") and -20 < x["v"] < 150]

            def pick(*pats):
                for p in pats:
                    for x in board:
                        if re.fullmatch(p, x.get("name", ""), re.I):
                            return x["v"]
                return None
            vrm = pick(r"VRM MOS", r"VRM", r"MOS", r"Core VRM", r"MOS CPU", r"CPU VRM")
            if vrm is not None:
                cpu["vrm_t"] = round(vrm, 1)
            pch = [x["v"] for x in board if re.search(r"chipset|pch", x.get("name", ""), re.I)
                   and not re.search(r"fan", x.get("name", ""), re.I)]
            for k, v in zip(("t", "t2", "t3", "t4"), pch):
                det.setdefault("pch", {})[k] = round(v, 1)
            sock = pick(r"CPU Socket", r"CPU", r"CPUTIN")
            if sock is not None:
                det["mb"]["cpu_t"] = round(sock, 1)
        for name, ch in FANS.items():
            if g(f"/lpc/nct6701d/0/fan/{ch}") is not None:
                det["fan"][name] = round(g(f"/lpc/nct6701d/0/fan/{ch}"))
        if not det["fan"]:                                 # another board (any Super I/O): every fan as the chip names it
            for x in d.get("sensors", []):
                if x.get("type") == "Fan" and str(x.get("id", "")).startswith("/lpc/") and x.get("v") is not None:
                    det["fan"][x.get("name") or x["id"]] = round(x["v"])
        for k, i in (("t", "/nvme/0/temperature/0"), ("t2", "/nvme/0/temperature/2"), ("life", "/nvme/0/level/20")):
            if g(i) is not None:
                det["ssd"][k] = g(i)
        # board rails on the NCT6701D (dividers fitted against HWiNFO 2026-11-22; the 1.x V rails are on another chip)
        for k, i, f in RAILS:
            if g(i) is not None:
                det["mb"][k] = round(g(i) * f, 3)
        w = whea_since_boot()
        if w is not None:
            det["whea"] = {"since boot": w}
        # ASUS EC registers (once mapped)
        if "t_sensor" in ecv:
            det["mb"]["tsen"] = round(cube_cal(ecv["t_sensor"]), 2)
        if "vrm_t" in ecv:
            cpu["vrm_t"] = ecv["vrm_t"]
        for k in ("vrm_v", "vrm_a", "vrm_w"):
            if k in ecv:
                det.setdefault("vrm", {})[k[4:]] = ecv[k]
        for k in ("latch_Min", "latch_Max"):
            if k in ecv:
                det["oc"][k] = ecv[k]
        self.detail = {k: v for k, v in det.items() if v}

        out = {}

        def put(key, v, u):
            if v is not None:
                out[key] = {"v": round(v, 2), "u": u}
        put("cpu_t", cpu.get("pkg_t"), "°C")
        put("cpu_w", cpu.get("pkg_w"), "W")
        put("cpu_load", cpu.get("use"), "%")
        put("gpu_t", gpu.get("t"), "°C")
        put("gpu_hot", gpu.get("hot"), "°C")
        put("gpu_w", gpu.get("w"), "W")
        put("gpu_w_raw", gpu.get("w_raw"), "W")
        put("gpu_load", gpu.get("load"), "%")
        put("vrm_t", cpu.get("vrm_t"), "°C")
        if "t_sensor" in ecv:
            out["t_sensor"] = {"v": round(cube_cal(ecv["t_sensor"]), 2), "u": "°C", "raw": ecv["t_sensor"]}
        put("w_pump", det["fan"].get("W_PUMP+"), "RPM")
        put("aio_pump", det["fan"].get("AIO Pump"), "RPM")
        put("cpu_fan", det["fan"].get("CPU"), "RPM")
        put("cha1", det["fan"].get("Chassis1"), "RPM")
        put("cha2", det["fan"].get("Chassis2"), "RPM")
        put("dimm_t", (det["dimm"].get("0") or next(iter(det["dimm"].values()), {})).get("t"), "°C")
        return "ok", out


def merge(primary, fallback):
    """(status, vals, detail) of the reader with anything it lacks taken from HWiNFO (while HWiNFO still runs)."""
    ps, pv, pd = primary
    fs, fv, fd = fallback
    if ps != "ok":
        return fallback if fs == "ok" else primary
    if fs != "ok":
        return primary
    vals = dict(fv)
    vals.update(pv)

    def deep(a, b):                                          # b wins
        out = dict(a)
        for k, v in b.items():
            out[k] = deep(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out
    return "ok", vals, deep(fd, pd)


if __name__ == "__main__":                                   # py sensors_src.py: what the dashboard would get
    st, vals = (r := Reader()).read()
    print(st)
    for k, v in vals.items():
        print(f"{k:10} {v['v']:10.2f} {v['u']}")
    print("detail groups:", {k: len(v) for k, v in r.detail.items()})
