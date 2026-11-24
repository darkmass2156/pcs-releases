"""Side-by-side check of the standalone sensor reader (tools/pc_sensors, LibreHardwareMonitor + PawnIO) against HWiNFO.

2026-11-22 (owner: "write our own hook instead of using HWiNFO, so we can run standalone").
While logs/sensors/now.json is fresh (the reader runs), every SAMPLE_S this records
  - every HWiNFO value the dashboard uses (PICK keys + the DETAIL tree, flattened: "v.cpu_w", "d.core.0.t", ...)
  - every reader sensor (by its LibreHardwareMonitor id)
into logs/sensors/compare_YYYYMMDD.csv (long format: time, source, key, value), for at most RUN_H hours from the first
sample (state in logs/sensors/compare_state.json; delete it to record again).

`py lhm_compare.py --report` pairs each HWiNFO reading with the reader sensor that follows it best (same unit family,
lowest median absolute difference, correlation over the whole recording) and prints what the reader covers, what it
misses, the reader's update time per hardware group and the HWiNFO readings with no counterpart.
"""
import csv, glob, json, os, statistics, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
import pcs_paths
DIR = pcs_paths.SENSORS
NOW = os.path.join(DIR, "now.json")
STATE = os.path.join(DIR, "compare_state.json")
SAMPLE_S = 15                # ~20 KB per sample: ~115 MB a day
RUN_H = 48
FRESH_S = 5


def read_now():
    """The reader's last output, or None when missing / stale."""
    from sensors_src import read_raw                         # shared memory first (the file is a 5 s debug copy)
    d = read_raw()
    if d is None:
        return None
    if time.time() - d.get("t", 0) > FRESH_S:
        return None
    return d


def flat_hwinfo(pc):
    out = {}
    for k, x in (pc.get("v") or {}).items():
        v = x.get("v") if isinstance(x, dict) else x
        if isinstance(v, (int, float)):
            out["v." + k] = v

    def walk(node, path):
        for k, x in node.items():
            p = path + "." + k
            if isinstance(x, dict):
                walk(x, p)
            elif isinstance(x, (int, float)) and not isinstance(x, bool):
                out[p] = x
    walk(pc.get("d") or {}, "d")
    return out


class Compare:
    SKIP_TYPES = ("Data", "Factor", "Level", "Throughput", "Timing", "SmallData")   # counters / static: not compared

    def __init__(self, hub):
        self.hub = hub
        self.named = set()                                   # reader ids whose description is already in today's file
        self.day = None
        os.makedirs(DIR, exist_ok=True)
        try:
            with open(STATE, encoding="utf-8") as f:
                self.st = json.load(f)
        except (OSError, ValueError):
            self.st = {}

    def done(self):
        s = self.st.get("start")
        return s is not None and time.time() - s > RUN_H * 3600

    def sample(self):
        if self.done():
            return
        now = read_now()
        if not now or not now.get("admin"):
            return                                           # not running, or without admin (no CPU / board data)
        with self.hub.lock:
            pc = dict(getattr(self.hub, "pc_hw", None) or self.hub.pc)   # HWiNFO itself, not the merged source
        if pc.get("status") != "ok" or time.time() - pc.get("t", 0) > 3:
            return
        if "start" not in self.st:
            self.st["start"] = time.time()
            with open(STATE, "w", encoding="utf-8") as f:
                json.dump(self.st, f)
            self.hub.add_event(f"# SENSORS comparison started: reader vs HWiNFO for {RUN_H} h")
        t = time.strftime("%H:%M:%S")
        path = os.path.join(DIR, f"compare_{time.strftime('%Y%m%d')}.csv")
        new = not os.path.exists(path)
        if self.day != path:
            self.day, self.named = path, set()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "src", "key", "v", "info"])
            for k, v in flat_hwinfo(pc).items():
                if not k.startswith(("d.mem.T", "d.mem.Command", "d.mem.Gear")):   # timings: static
                    w.writerow([t, "H", k, v, ""])
            for s in now.get("sensors", []):
                if s.get("v") is None or s["type"] in self.SKIP_TYPES:
                    continue
                desc = ""
                if s["id"] not in self.named:                # the description once per file, not every sample
                    self.named.add(s["id"])
                    desc = f"{s['hw']} | {s['type']} | {s['name']}"
                w.writerow([t, "L", s["id"], s["v"], desc])
            for hw, ms in (now.get("poll_ms") or {}).items():
                w.writerow([t, "P", hw, ms, ""])
            # reader v2 extras: effective clocks, limit bits, ring clock (as reader sensors), the raw EC dump
            m = now.get("msr") or {}
            for i, x in enumerate(m.get("eclk") or []):
                if x is not None:
                    w.writerow([t, "L", f"msr/eclk/{i}", x, "" if f"msr/eclk/{i}" in self.named else "PCS | Clock | eclk"])
                    self.named.add(f"msr/eclk/{i}")
            if (m.get("ring_ratio") or -1) > 0:
                w.writerow([t, "L", "msr/ring_clk", m["ring_ratio"] * 100.0, "PCS | Clock | ring"])
            ec = (now.get("ec") or {}).get("dump")
            if ec and (now.get("ec") or {}).get("dump_age_s", 99) < 40:
                w.writerow([t, "E", "dump", "", ec])
            # the Mega's water temperatures: the reference for the EC's T_Sensor (the cube sits between T2 and T1)
            with self.hub.lock:
                row = self.hub.rows[-1] if self.hub.rows else None
            if row and not row.get("hist") and time.time() - row.get("t", 0) < 5:
                for k in ("T1", "T2"):
                    if isinstance(row.get(k), (int, float)):
                        w.writerow([t, "H", "m." + k, row[k], ""])

    def run(self, stop):
        while not stop.is_set():
            try:
                self.sample()
            except Exception:
                pass
            if self.done():
                return
            stop.wait(SAMPLE_S)


def start(hub, stop):
    c = Compare(hub)
    if not c.done():
        threading.Thread(target=c.run, args=(stop,), daemon=True).start()
    return c


# ------------------------------------------------------------------------------------------------ report
# unit family of a HWiNFO path / a LibreHardwareMonitor sensor type, so only like is paired with like
def fam_h(key):
    k = key.lower()
    last = k.rsplit(".", 1)[-1]
    if k.startswith(("d.gmem.", "d.pch.")) or last in (
            "t", "t2", "hot", "mem_j", "tsen", "dimm_th", "pch_sio", "cpu_t", "pkg_t", "max_t", "vr_t", "vrm_t", "ia_t",
            "t_sensor", "gpu_t", "gpu_hot", "dimm_t"):
        return "temp"
    if last in ("w", "w_raw", "hp_w", "hp_w_real", "slot_w", "core_w", "fb_w", "pkg_w", "ia_w", "sa_w", "cpu_w", "gpu_w",
                "gpu_w_raw", "gpu_hp_w", "gpu_slot_w", "vr_w", "pl1", "pl2"):
        return "power"
    if last.endswith("clk"):
        return "clock"
    if k.startswith(("d.dlvr.", "d.voff.")) or "latch" in last or last in (
            "v", "hp_v", "slot_v", "vcore", "ring_v", "vccsa", "imc", "vccio", "bat", "v12", "v5", "v33", "soc18",
            "vnnaon", "vdd", "vddq", "vpp", "uncore_vid", "vccclk", "vccddq", "vcciog"):
        return "volt"
    if last in ("use", "load", "cpu_load", "gpu_load", "mem_load", "odcm"):
        return "load"
    if k.startswith("d.fan.") or last in ("w_pump", "aio_pump", "cpu_fan", "cha1", "cha2"):
        return "fan"
    if last in ("a", "vr_a"):
        return "current"
    return "other"


FAM_L = {"Temperature": "temp", "Power": "power", "Clock": "clock", "Voltage": "volt", "Load": "load", "Fan": "fan",
         "Current": "current", "Control": "control"}

# known pairs (HWiNFO path -> reader id). Cores by position: the reader lists them in logical order like HWiNFO
# (P P, 8 E, P P P P, 8 E, P P): temperatures from index 2, clocks / voltages from 1, loads ("CPU Core #n") from 2.
EXPECT = {}
for _i in range(24):
    EXPECT[f"d.core.{_i}.t"] = f"/intelcpu/0/temperature/{_i + 2}"
    EXPECT[f"d.core.{_i}.clk"] = f"/intelcpu/0/clock/{_i + 1}"
    EXPECT[f"d.core.{_i}.v"] = f"/intelcpu/0/voltage/{_i + 1}"
    EXPECT[f"d.core.{_i}.use"] = f"/intelcpu/0/load/{_i + 2}"
EXPECT.update({
    "v.cpu_t": "/intelcpu/0/temperature/26", "d.cpu.pkg_t": "/intelcpu/0/temperature/26",
    "d.cpu.max_t": "/intelcpu/0/temperature/0", "v.cpu_w": "/intelcpu/0/power/0", "d.cpu.pkg_w": "/intelcpu/0/power/0",
    "d.cpu.ia_w": "/intelcpu/0/power/1", "v.cpu_load": "/intelcpu/0/load/0", "d.cpu.use": "/intelcpu/0/load/0",
    "d.oc.bclk": "/intelcpu/0/clock/0", "d.cpu.ring_clk": "msr/ring_clk",
    **{f"d.core.{_i}.eclk": f"msr/eclk/{_i}" for _i in range(24)},
    "v.gpu_t": "/gpu-nvidia/0/temperature/0", "d.gpu.t": "/gpu-nvidia/0/temperature/0",
    "v.gpu_hot": "/gpu-nvidia/0/temperature/2", "d.gpu.hot": "/gpu-nvidia/0/temperature/2",
    "d.gpu.mem_j": "/gpu-nvidia/0/temperature/3", "v.gpu_load": "/gpu-nvidia/0/load/0", "d.gpu.load": "/gpu-nvidia/0/load/0",
    "d.gpu.v": "/gpu-nvidia/0/voltage/0", "d.gpu.clk": "/gpu-nvidia/0/clock/0", "d.gpu.mem_clk": "/gpu-nvidia/0/clock/4",
    "d.gpu.mem_load": "/gpu-nvidia/0/load/1", "v.gpu_w_raw": "/gpu-nvidia/0/power/0", "d.gpu.w_raw": "/gpu-nvidia/0/power/0",
    "v.w_pump": "/lpc/nct6701d/0/fan/5", "d.fan.W_PUMP+": "/lpc/nct6701d/0/fan/5",
    "d.dimm.0.t": "/memory/dimm/0/temperature/0", "d.dimm.2.t": "/memory/dimm/2/temperature/0",
    "v.dimm_t": "/memory/dimm/0/temperature/0",
    "d.ssd.t": "/nvme/0/temperature/0", "d.ssd.t2": "/nvme/0/temperature/2", "d.mb.v33": "/lpc/nct6701d/0/voltage/3",
})


def report(days=None):
    files = sorted(glob.glob(os.path.join(DIR, "compare_*.csv")))
    if days:
        files = files[-days:]
    if not files:
        print("no comparison data yet (logs/sensors/compare_*.csv): is the reader running as admin?")
        return
    H, L, P, info = {}, {}, {}, {}
    for fp in files:
        day = os.path.basename(fp)[8:16]
        with open(fp, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    v = float(r["v"])
                except (TypeError, ValueError):
                    continue
                t = day + r["time"]
                d = {"H": H, "L": L, "P": P}.get(r["src"])
                if d is None:
                    continue
                d.setdefault(r["key"], {})[t] = v
                if r["src"] == "L" and r["info"]:
                    info[r["key"]] = r["info"]
    n = len({t for s in H.values() for t in s})
    print(f"{n} samples, {len(H)} HWiNFO readings, {len(L)} reader sensors\n")
    print("Reader update time per hardware group (median / max ms):")
    for hw, s in sorted(P.items(), key=lambda kv: -statistics.median(kv[1].values())):
        vals = list(s.values())
        print(f"  {hw:40} {statistics.median(vals):7.1f} {max(vals):8.1f}")

    def corr(a, b):
        if len(a) < 10:
            return 0.0
        ma, mb = statistics.fmean(a), statistics.fmean(b)
        sa = sum((x - ma) ** 2 for x in a) ** .5
        sb = sum((y - mb) ** 2 for y in b) ** .5
        if sa == 0 or sb == 0:
            return 1.0 if sa == sb else 0.0
        return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)

    def stats(hs, ls):
        ts = [t for t in hs if t in ls]
        if len(ts) < 10:
            return None
        a = [hs[t] for t in ts]
        b = [ls[t] for t in ts]
        return statistics.median(abs(x - y) for x, y in zip(a, b)), corr(a, b), statistics.pstdev(a)

    print("\nKnown pairs (median |diff|, correlation; r means nothing while a reading is flat):")
    for hk, lk in EXPECT.items():
        if hk not in H:
            continue
        st = stats(H[hk], L.get(lk, {}))
        if st is None:
            print(f"  {hk:22} {'(reader has no value)' if lk not in L else '(too few samples)'}  {lk}")
        else:
            print(f"  {hk:22} <- {info.get(lk, lk)[:56]:56} diff {st[0]:.3g}  r {st[1]:.2f}"
                  f"{'  flat' if st[2] < 1e-6 else ''}")

    lfam = {k: FAM_L.get(info.get(k, "").split(" | ")[1] if " | " in info.get(k, "") else "", "other") for k in L}
    found, missing, flat = [], [], []
    for hk, hs in sorted(H.items()):
        if hk in EXPECT or ".kind_" in hk:
            continue
        if statistics.pstdev(list(hs.values())) < 1e-6:
            flat.append(hk)                                  # constant so far: any constant sensor would "match"
            continue
        fh = fam_h(hk)
        best = None
        for lk, ls in L.items():
            if lfam[lk] != fh or fh in ("other", "control"):
                continue
            ts = [t for t in hs if t in ls]
            if len(ts) < 10:
                continue
            a = [hs[t] for t in ts]
            b = [ls[t] for t in ts]
            mad = statistics.median(abs(x - y) for x, y in zip(a, b))
            scale = max(1e-6, statistics.median(abs(x) for x in a))
            c = corr(a, b)
            score = mad / scale - 0.5 * max(0.0, c)
            if best is None or score < best[0]:
                best = (score, lk, mad, c, len(ts))
        ok = best and best[2] <= {"temp": 2.0, "power": max(2.0, 0.05 * abs(statistics.median(hs.values()))),
                                  "clock": 60.0, "volt": 0.02, "load": 5.0, "fan": 60.0, "current": 3.0}.get(fh, 0.5) \
            and (best[3] > 0.8 or statistics.pstdev(list(hs.values())) < 1e-6)
        (found if ok else missing).append((hk, best))
    print(f"\nConstant so far, not judged ({len(flat)}): " + ", ".join(flat[:60]) + (" ..." if len(flat) > 60 else ""))
    print(f"\nAuto-matched (moving readings only): {len(found)}")
    for hk, b in found:
        print(f"  {hk:34} <- {info.get(b[1], b[1])[:70]:70}  diff {b[2]:.3g}  r {b[3]:.2f}")
    print(f"\nMoving, no good match (lose or need a closer look): {len(missing)}")
    for hk, b in missing:
        hint = f"closest {info.get(b[1], b[1])[:60]}  diff {b[2]:.3g} r {b[3]:.2f}" if b else "no sensor of that kind"
        print(f"  {hk:34}  {hint}")


# ------------------------------------------------------------------------------------------------ EC map
# HWiNFO's "ASUS EC" readings we want from our own reader, and how they may be stored (ASUS: big endian)
EC_TARGETS = {
    "v.t_sensor": "temp", "d.cpu.vrm_t": "temp", "d.mb.dimm_th": "temp",
    "d.vrm.v": "any", "d.vrm.a": "any", "d.vrm.w": "any", "d.oc.latch_Min": "any", "d.oc.latch_Max": "any",
    "d.fan.CPU_OPT": "any",
}


def ec_map(min_samples=20):
    """For each EC target: the register (u8) or register pair (u16, big endian) that follows it best."""
    files = sorted(glob.glob(os.path.join(DIR, "compare_*.csv")))
    H, E = {}, {}
    for fp in files:
        day = os.path.basename(fp)[8:16]
        with open(fp, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                t = day + r["time"]
                if r["src"] == "E":
                    E[t] = bytes.fromhex(r["info"])
                elif r["src"] == "H" and (r["key"] in EC_TARGETS or r["key"].startswith("m.")):
                    try:
                        H.setdefault(r["key"], {})[t] = float(r["v"])
                    except ValueError:
                        pass
    print(f"{len(E)} EC dumps")
    if len(E) < min_samples:
        print("too few dumps yet")
        return
    ts = sorted(E)

    def fit(xs, ys):
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx == 0:
            return None
        a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        b = my - a * mx
        res = [y - (a * x + b) for x, y in zip(xs, ys)]
        syy = sum((y - my) ** 2 for y in ys)
        r2 = 1 - sum(e * e for e in res) / syy if syy else 0.0
        return a, b, r2, max(abs(e) for e in res)

    for key, kind in EC_TARGETS.items():
        hs = H.get(key)
        if not hs:
            print(f"\n{key}: no HWiNFO data")
            continue
        common = [t for t in ts if t in hs]
        if len(common) < min_samples:
            print(f"\n{key}: only {len(common)} samples")
            continue
        ys = [hs[t] for t in common]
        spread = max(ys) - min(ys)
        cands = []
        for reg in range(512):
            xs = [E[t][reg] for t in common]
            if kind == "temp":                               # stored as whole °C: must equal it (±1, rounding)
                err = max(abs(x - y) for x, y in zip(xs, ys))
                if err <= 1.0:
                    cands.append((err, f"0x{reg:03X} u8", 1.0, 0.0, 1.0))
                continue
            f1 = fit(xs, ys)
            if f1 and f1[2] > 0.9:
                cands.append((1 - f1[2], f"0x{reg:03X} u8", f1[0], f1[1], f1[2]))
            if reg < 511:
                xs2 = [E[t][reg] * 256 + E[t][reg + 1] for t in common]
                f2 = fit(xs2, ys)
                if f2 and f2[2] > 0.9:
                    cands.append((1 - f2[2], f"0x{reg:03X} u16", f2[0], f2[1], f2[2]))
        cands.sort(key=lambda c: c[0])
        flat = " (FLAT so far: needs the value to move before it can be trusted)" if spread < (2 if kind == "temp" else 1e-3) else ""
        print(f"\n{key}: {len(common)} samples, range {min(ys):g} .. {max(ys):g}{flat}")
        for c in cands[:6]:
            print(f"   {c[1]:10}  y = {c[2]:.6g} * x + {c[3]:.4g}   r2 {c[4]:.4f}")
        if not cands:
            print("   no register follows it")
    if "m.T1" in H and "v.t_sensor" in H:
        d = [H["v.t_sensor"][t] - H["m.T1"][t] for t in H["v.t_sensor"] if t in H["m.T1"]]
        if d:
            print(f"\ncube - T1 (HWiNFO vs Mega): median {statistics.median(d):+.2f} °C over {len(d)} samples")


def ec_scan(top=8):
    """logs/sensors/ec_scan.csv (reader with ec_scan.on): each 32-register chunk with the CPU package power and the
    CPU core voltage of that second. Registers / big-endian pairs that follow them are the VRM out / current / power."""
    path = os.path.join(DIR, "ec_scan.csv")
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.strip().split(",")
            if len(p) < 7:
                continue
            try:
                rows.append((float(p[0]), int(p[1]), bytes.fromhex(p[2]), (float(p[3]) + float(p[5])) / 2,
                             (float(p[4]) + float(p[6])) / 2))
            except ValueError:
                continue
    print(f"{len(rows)} chunks")
    series = {}                                              # (reg, kind) -> [(x, cpu_w, vcore)]
    for t, start, data, w, v in rows:
        for i in range(len(data)):
            series.setdefault((start + i, "u8"), []).append((data[i], w, v))
            if i + 1 < len(data):
                series.setdefault((start + i, "u16"), []).append((data[i] * 256 + data[i + 1], w, v))

    def corr(a, b):
        n = len(a)
        ma, mb = sum(a) / n, sum(b) / n
        sa = sum((x - ma) ** 2 for x in a) ** .5
        sb = sum((y - mb) ** 2 for y in b) ** .5
        return 0.0 if sa == 0 or sb == 0 else sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)
    for ref, idx in (("CPU package W", 1), ("CPU core V", 2)):
        res = []
        for (reg, kind), s in series.items():
            if len(s) < 20 or len({x[0] for x in s}) < 4:
                continue
            xs, ys = [x[0] for x in s], [x[idx] for x in s]
            res.append((abs(corr(xs, ys)), reg, kind, corr(xs, ys), min(xs), max(xs), len(s)))
        res.sort(reverse=True)
        print(f"\nfollows {ref}:")
        for r in res[:top]:
            print(f"   0x{r[1]:03X} {r[2]:4}  r {r[3]:+.3f}  raw {r[4]} .. {r[5]}  ({r[6]} samples)")


if __name__ == "__main__":
    if "--ec-scan" in sys.argv:
        ec_scan()
    elif "--ec" in sys.argv:
        ec_map()
    elif "--report" in sys.argv:
        report()
    else:
        d = read_now()
        print("reader not running / stale" if not d else
              f"reader ok, admin={d.get('admin')}, {len(d.get('sensors', []))} sensors, poll {d.get('poll_ms')}")
