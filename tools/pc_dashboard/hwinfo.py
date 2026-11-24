"""Read HWiNFO's sensor shared memory (Windows only, stdlib ctypes; nothing to install).

HWiNFO: Settings > "Shared Memory Support" on. The free version turns it off again after 12 h (re-tick it, or Pro).
Layout (HWiNFO_SENS_SM2, version 2): 44-byte header, then sensor records and entry (reading) records.

PICK below chooses what the dashboard shows. Each line: key -> (sensor name starts with, reading label, type).
Labels are what HWiNFO shows in its sensor window; change them here if a board/driver names a reading differently.
"""
import ctypes
import re
import struct
import sys

TYPES = ["none", "temp", "volt", "fan", "current", "power", "clock", "usage", "other"]

PICK = {
    "cpu_t":    ("CPU [#0]", "CPU Package", "temp"),
    "cpu_w":    ("CPU [#0]", "CPU Package Power", "power"),
    "cpu_load": ("CPU [#0]", "Total CPU Usage", "usage"),
    "gpu_t":    ("dGPU", "GPU Temperature", "temp"),
    "gpu_hot":  ("dGPU", "GPU Hot Spot Temperature", "temp"),
    "gpu_w":    ("dGPU", "GPU Power", "power"),          # raw total; replaced by the shunt-corrected sum below
    "gpu_hp_w": ("dGPU", "GPU 12VHPWR Power", "power"),  # 16-pin input (shunt-modded)
    "gpu_slot_w": ("dGPU", "GPU PCIe +12V Input Power", "power"),   # PCIe slot input (stock shunt)
    "gpu_load": ("dGPU", "GPU Core Load", "usage"),
    "t_sensor": ("ASUS EC", "T_Sensor", "temp"),          # Apex T_SENSOR header (the cube NTC)
    "vrm_t":    ("ASUS EC", "VRM", "temp"),
    "w_pump":   ("ASUS ROG", "W_PUMP+", "fan"),
    "aio_pump": ("ASUS ROG", "AIO Pump", "fan"),
    "cpu_fan":  ("ASUS ROG", "CPU", "fan"),
    "cha1":     ("ASUS ROG", "Chassis1", "fan"),
    "cha2":     ("ASUS ROG", "Chassis2", "fan"),
    "dimm_t":   ("DDR5 DIMM", "SPD Hub Temperature", "temp"),   # prefix match: first DIMM
}

# GPU shunt mod (owner 2026-11-08): R005 soldered over the stock R002 on the 16-pin (12VHPWR) input only; the PCIe slot
# shunt is stock. 2 || 5 mOhm = 1.4286 mOhm, the card still assumes 2 mOhm, so it reports 0.714 of the real 16-pin
# current. Real GPU power = 12VHPWR x 1.4 + PCIe slot (2026-11-08: GPU Power 218.9 = 185.8 + 34.6 W as read).
# "gpu_w" becomes that corrected sum; "gpu_w_raw" keeps the card's own figure. If a rail is missing, raw x 1 is used.
# Cube NTC (Apex T_SENSOR, whole °C, rounded): correction added to every cube reading (programs, cards, log, FanControl
# rules). 2026-11-13 (owner), thermometer on the cube fitting: right at ~20.5 °C (20 -> 21 at 20.5-20.6 on the meter),
# ~1.5 °C low around 14 °C (Apex steps 15/14/13 at meter 16.0 / 15.8 / 15.0, Mode 4 cool-down; the Apex also read
# below T2, which a cube fed by T2 + T1 water can't be). The error grows below 20.5: NTC curve mismatch, so linear
# +CUBE_SLOPE per °C under CUBE_CAL_C, none above (no data there). Was a flat +1.0 (2026-11-10, a guess from T1 vs T2).
CUBE_OFFSET_C = 0.0
CUBE_CAL_C = 20.5
CUBE_SLOPE = 0.23


def cube_cal(raw):
    return raw + CUBE_OFFSET_C + max(0.0, CUBE_SLOPE * (CUBE_CAL_C - raw))

SHUNT_STOCK_MOHM, SHUNT_ADDED_MOHM = 2.0, 5.0
GPU_SHUNT_FACTOR = SHUNT_STOCK_MOHM / (SHUNT_STOCK_MOHM * SHUNT_ADDED_MOHM / (SHUNT_STOCK_MOHM + SHUNT_ADDED_MOHM))

# Detail readings for the hardware (PCB) cards, 2026-11-08: (sensor name starts with, type, label regex, path).
# Path fields "{1}" / "{2}" are filled from the regex groups, "{s}" from the "[#n]" in the sensor name. First match wins
# (HWiNFO lists some labels twice, e.g. CPU Package). Core numbers are HWiNFO's: on the 270K Plus the P-cores are
# 0, 1, 10-13, 22, 23 and the E-cores 2-9, 14-21 (clusters of 4: 2-5, 6-9, 14-17, 18-21).
DETAIL = [
    ("CPU [#0]", "temp",  r"^([PE])-core (\d+)\*?$",                 "core.{2}.t"),
    ("CPU [#0]", "clock", r"^([PE])-core (\d+) Clock\*?$",           "core.{2}.clk"),
    ("CPU [#0]", "clock", r"^([PE])-core (\d+) Effective Clock$",    "core.{2}.eclk"),
    ("CPU [#0]", "volt",  r"^([PE])-core (\d+) Voltage\*?$",         "core.{2}.v"),
    ("CPU [#0]", "usage", r"^([PE])-core (\d+) Usage$",              "core.{2}.use"),
    ("CPU [#0]", "temp",  r"^CPU Package$",                          "cpu.pkg_t"),
    ("CPU [#0]", "temp",  r"^Core Max$",                             "cpu.max_t"),
    ("CPU [#0]", "temp",  r"^VR VCC Temperature \(SVID\)$",          "cpu.vr_t"),
    ("CPU [#0]", "power", r"^CPU Package Power$",                    "cpu.pkg_w"),
    ("CPU [#0]", "power", r"^IA Cores Power$",                       "cpu.ia_w"),
    ("CPU [#0]", "power", r"^System Agent Power$",                   "cpu.sa_w"),
    ("CPU [#0]", "clock", r"^Ring/LLC Clock$",                       "cpu.ring_clk"),
    ("CPU [#0]", "usage", r"^Total CPU Usage$",                      "cpu.use"),
    ("ASUS ROG", "volt",  r"^CPU DLVRin Vcore$",                     "cpu.vcore"),
    ("ASUS ROG", "volt",  r"^Ring DLVR$",                            "cpu.ring_v"),
    ("ASUS EC",  "temp",  r"^VRM$",                                  "cpu.vrm_t"),
    ("dGPU",     "temp",  r"^GPU Temperature$",                      "gpu.t"),
    ("dGPU",     "temp",  r"^GPU Hot Spot Temperature$",             "gpu.hot"),
    ("dGPU",     "temp",  r"^GPU Memory Junction Temperature$",      "gpu.mem_j"),
    ("dGPU",     "temp",  r"^GPU Memory ([A-H]\d) Temperature$",     "gmem.{1}"),
    ("dGPU",     "volt",  r"^GPU Core Voltage$",                     "gpu.v"),
    ("dGPU",     "volt",  r"^GPU 12VHPWR Voltage$",                  "gpu.hp_v"),
    ("dGPU",     "volt",  r"^GPU PCIe \+12V Input Voltage$",         "gpu.slot_v"),
    ("dGPU",     "clock", r"^GPU Clock$",                            "gpu.clk"),
    ("dGPU",     "clock", r"^GPU Memory Clock$",                     "gpu.mem_clk"),
    ("dGPU",     "usage", r"^GPU Core Load$",                        "gpu.load"),
    ("dGPU",     "usage", r"^GPU Memory Controller Load$",           "gpu.mem_load"),
    ("dGPU",     "power", r"^GPU 12VHPWR Power$",                    "gpu.hp_w"),
    ("dGPU",     "power", r"^GPU PCIe \+12V Input Power$",           "gpu.slot_w"),
    ("dGPU",     "power", r"^GPU Core \(NVVDD\) Input Power \(sum\)$", "gpu.core_w"),
    ("dGPU",     "power", r"^GPU FBVDD Input Power$",                "gpu.fb_w"),
    ("dGPU",     "power", r"^GPU Power$",                            "gpu.w_raw"),
    ("DDR5 DIMM", "temp", r"^SPD Hub Temperature",                   "dimm.{s}.t"),
    ("DDR5 DIMM", "power", r"^Total Power$",                         "dimm.{s}.w"),
    ("DDR5 DIMM", "volt", r"^VDD \(SWA\) Voltage$",                  "dimm.{s}.vdd"),
    # motherboard card (2026-11-09): ROG Maximus Z890 Apex, Nuvoton NCT6701D + ASUS EC + Intel PCH
    ("ASUS ROG", "temp",  r"^Motherboard$",                          "mb.t"),
    ("ASUS ROG", "temp",  r"^CPU$",                                  "mb.cpu_t"),      # socket thermistor
    ("ASUS ROG", "temp",  r"^PCH$",                                  "mb.pch_sio"),
    ("ASUS ROG", "temp",  r"^PCH Temperature(\d?)$",                 "pch.t{1}"),
    ("ASUS ROG", "volt",  r"^\+12V$",                                "mb.v12"),
    ("ASUS ROG", "volt",  r"^\+5V$",                                 "mb.v5"),
    ("ASUS ROG", "volt",  r"^3VCC$",                                 "mb.v33"),
    ("ASUS ROG", "volt",  r"^VCCSA$",                                "mb.vccsa"),
    ("ASUS ROG", "volt",  r"^IMC VDD$",                              "mb.imc"),
    ("ASUS ROG", "volt",  r"^VCCIO 1\.25V$",                         "mb.vccio"),
    ("ASUS ROG", "volt",  r"^BAT_3V$",                               "mb.bat"),
    ("ASUS ROG", "fan",   r"^(.+)$",                                 "fan.{1}"),       # Chassis1, CPU, W_PUMP+, ...
    ("ASUS EC",  "fan",   r"^(CPU_OPT)$",                            "fan.{1}"),
    ("ASUS EC",  "temp",  r"^T_Sensor$",                             "mb.tsen"),
    ("ASUS EC",  "temp",  r"^DIMM Thermistor$",                      "mb.dimm_th"),
    ("ASUS EC",  "volt",  r"^VRM Vcore Voltage \(VOUT\)$",           "vrm.v"),
    ("ASUS EC",  "current", r"^VRM Vcore Current \(IOUT\)$",         "vrm.a"),
    ("ASUS EC",  "power", r"^VRM Vcore Power \(POUT\)$",             "vrm.w"),
    ("ASUS EC",  "other", r"^Accumulated CPU Energy$",               "mb.kwh"),
    ("Memory Timings", "clock", r"^Memory Clock$",                   "mem.clk"),
    ("Memory Timings", "other", r"^(Tcas|Trcd|Trp|Tras|Trc|Trfc|Command Rate|Gear Mode)$", "mem.{1}"),
    ("Memory Timings", "volt", r"^(VccCLK|VccDDQ|VccIOG)$",         "mem.{1}"),
    # SSD (Kingston Fury Renegade 2 TB, C:). HWiNFO only shares readings that are not hidden in its sensor window.
    ("S.M.A.R.T.", "temp", r"^Drive Temperature$",                  "ssd.t"),
    ("S.M.A.R.T.", "temp", r"^Drive Temperature 2$",                "ssd.t2"),
    ("S.M.A.R.T.", None,   r"^Drive Remaining Life$",               "ssd.life"),
    ("Drive:",     None,   r"^Read Rate$",                          "ssd.rd"),
    ("Drive:",     None,   r"^Write Rate$",                         "ssd.wr"),
    # overclocking view (Benchmark): limits, VR, clocks, voltages, memory, WHEA
    ("CPU [#0]", None,     r"^(IA|RING): (.+)$",                    "lim.{1}: {2}"),
    ("CPU [#0]", None,     r"^Package/Ring (Thermal Throttling|Critical Temperature|Power Limit Exceeded)$", "lim.PKG: {1}"),
    ("CPU [#0]", None,     r"^([PE])-core (\d+) Thermal Throttling$", "thr.{2}"),
    ("CPU [#0]", "power",  r"^PL1 Power Limit \(Static\)$",         "oc.pl1"),
    ("CPU [#0]", "power",  r"^PL2 Power Limit \(Static\)$",         "oc.pl2"),
    ("CPU [#0]", "current", r"^VR VCC Current \(SVID IOUT\)$",      "oc.vr_a"),
    ("CPU [#0]", "power",  r"^VR VCC Power \(SVID POUT\)$",         "oc.vr_w"),
    ("CPU [#0]", "volt",   r"^Uncore VID$",                         "oc.uncore_vid"),
    ("CPU [#0]", "clock",  r"^Bus Clock$",                          "oc.bclk"),
    ("CPU [#0]", "clock",  r"^Average Effective Clock$",            "oc.avg_eclk"),
    ("CPU [#0]", "other",  r"^IA OC Ratio Limit$",                  "oc.ia_ratio"),
    ("CPU [#0]", "other",  r"^CLR \(CBo/LLC/Ring\) OC Ratio Limit$", "oc.ring_ratio"),
    ("CPU [#0]", "clock",  r"^NGU Clock$",                          "oc.ngu_clk"),       # Overclocking view (2026-11-10)
    ("CPU [#0]", "clock",  r"^D2D Clock$",                          "oc.d2d_clk"),
    ("CPU [#0]", "other",  r"^Uncore Ratio$",                       "oc.uncore_ratio"),
    ("CPU [#0]", "other",  r"^NGU OC Ratio Limit$",                 "oc.ngu_ratio"),
    ("CPU [#0]", "other",  r"^MemSS OC Ratio Limit$",               "oc.memss_ratio"),
    ("CPU [#0]", "volt",   r"^(IA|CLR \(CBo/LLC/Ring\)|SA|MemSS|NGU) Voltage Offset$", "voff.{1}"),
    ("CPU [#0]", "temp",   r"^CPU IA Cores$",                       "cpu.ia_t"),
    ("CPU [#0]", "usage",  r"^On-Demand Clock Modulation$",         "oc.odcm"),
    ("ASUS ROG", "volt",   r"^SOC 1\.8V$",                          "mb.soc18"),
    ("ASUS ROG", "volt",   r"^VNNAON 0\.77V$",                      "mb.vnnaon"),
    ("ASUS EC",  "volt",   r"^VCore Latch (Min|Max)$",              "oc.latch_{1}"),
    ("ASUS EC",  "volt",   r"^VCore Latch Delta",                   "oc.latch_delta"),
    ("ASUS",     "volt",   r"^(P-core \d+|E-core Cluster\d) DLVR$", "dlvr.{1}"),
    ("DDR5 DIMM", "volt",  r"^VDDQ \(SWB\) Voltage$",               "dimm.{s}.vddq"),
    ("DDR5 DIMM", "volt",  r"^VPP \(SWC\) Voltage$",                "dimm.{s}.vpp"),
    ("DDR5 DIMM", "other", r"^PMIC (High Temperature|Over Voltage|Under Voltage)$", "dimm.{s}.pmic {1}"),
    ("Windows Hardware Errors", None, r"^(.+)$",                    "whea.{1}"),
]
_DETAIL_RX = [(sp, t, re.compile(rx), path) for sp, t, rx, path in DETAIL]

FILE_MAP_READ = 4
NAME = "Global\\HWiNFO_SENS_SM2"


class HWiNFO:
    def __init__(self):
        self.ok = sys.platform == "win32"
        self.h = None
        self.p = None
        self.index = None       # key -> entry number, rebuilt when the layout changes
        self.dindex = None      # [(path, entry number)] for the detail readings
        self.detail = {}        # nested: {"core": {"0": {"t":..}}, "cpu": {..}, "gpu": {..}, "gmem": {..}, "dimm": {..}}
        self.layout = None
        if self.ok:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.OpenFileMappingW.restype = ctypes.c_void_p
            k.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
            k.MapViewOfFile.restype = ctypes.c_void_p
            k.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
            k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            self.k = k

    def close(self):
        if self.p:
            self.k.UnmapViewOfFile(self.p)
        if self.h:
            self.k.CloseHandle(self.h)
        self.h = self.p = self.index = self.layout = None

    def _open(self):
        self.h = self.k.OpenFileMappingW(FILE_MAP_READ, 0, NAME)
        if not self.h:
            return False
        self.p = self.k.MapViewOfFile(self.h, FILE_MAP_READ, 0, 0, 0)
        if not self.p:
            self.close()
            return False
        return True

    def _view_size(self):
        """Bytes readable from the mapped view (VirtualQuery region size), 0 if unknown."""
        class MBI(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", ctypes.c_uint32), ("PartitionId", ctypes.c_uint16),
                        ("RegionSize", ctypes.c_size_t), ("State", ctypes.c_uint32), ("Protect", ctypes.c_uint32),
                        ("Type", ctypes.c_uint32)]
        mbi = MBI()
        q = self.k.VirtualQuery
        q.restype = ctypes.c_size_t
        q.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        if not self.p or not q(self.p, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            return 0
        return mbi.RegionSize if mbi.State == 0x1000 else 0   # MEM_COMMIT: same-state pages from the view start

    def read(self):
        """Returns (status, {key: {"v": value, "u": unit, "label": ...}})."""
        if not self.ok:
            return "not Windows", {}
        if not self.p and not self._open():
            return "HWiNFO not running or Shared Memory Support off", {}
        try:
            hdr = ctypes.string_at(self.p, 44)
            sig, ver, rev, _poll, so, ss, sn, eo, es, en = struct.unpack("<IIIqIIIIII", hdr)
            if sig != 0x53695748:            # "HWiS"; "DEAD" when HWiNFO stops sharing
                self.close()
                return "HWiNFO shared memory stopped (free version: 12 h limit)", {}
            need = eo + es * en
            # 2026-11-14: the dashboard died with 0xC0000005 at logon. HWiNFO grows the block while it adds sensors in
            # its first seconds; the view mapped earlier is smaller, and reading past it is an access violation Python
            # can't catch. Check the mapped size first, re-map once if it grew, else skip this second.
            if need > self._view_size():
                self.close()
                if not self._open() or need > self._view_size():
                    return "HWiNFO shared memory still growing (starting up)", {}
            data = ctypes.string_at(self.p, need)
        except OSError:
            self.close()
            return "HWiNFO shared memory lost", {}
        layout = (so, ss, sn, eo, es, en)
        if layout != self.layout:
            self.layout = layout
            self.index = self._build_index(data, so, ss, sn, eo, es, en)
            self.dindex = self._build_detail(data, so, ss, sn, eo, es, en)
        out = {}
        for key, i in self.index.items():
            b = eo + i * es
            unit = data[b + 268:b + 284].split(b"\0")[0].decode("latin-1").replace("\xb0", "°")
            val = struct.unpack("<d", data[b + 284:b + 292])[0]
            out[key] = {"v": round(val, 2), "u": unit}
        det = {}
        for path, i in self.dindex:
            b = eo + i * es
            val = struct.unpack("<d", data[b + 284:b + 292])[0]
            node = det
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = round(val, 3)
        g = det.get("gpu", {})
        if "hp_w" in g:                                     # 16-pin shunt-modded: real = x 1.4, slot stock
            g["hp_w_real"] = round(g["hp_w"] * GPU_SHUNT_FACTOR, 1)
            g["w"] = round(g["hp_w_real"] + g.get("slot_w", 0.0), 1)
        if "t_sensor" in out:                               # cube NTC calibration (applies everywhere the cube is used)
            out["t_sensor"]["raw"] = out["t_sensor"]["v"]
            out["t_sensor"]["v"] = round(cube_cal(out["t_sensor"]["v"]), 2)
        if "tsen" in det.get("mb", {}):
            det["mb"]["tsen"] = round(cube_cal(det["mb"]["tsen"]), 2)
        self.detail = det
        if "gpu_w" in out:
            out["gpu_w_raw"] = dict(out["gpu_w"])
            if "gpu_hp_w" in out and "gpu_slot_w" in out:
                out["gpu_w"] = {"v": round(out["gpu_hp_w"]["v"] * GPU_SHUNT_FACTOR + out["gpu_slot_w"]["v"], 2), "u": "W"}
        return "ok", out

    @staticmethod
    def _build_detail(data, so, ss, sn, eo, es, en):
        names = [data[so + i * ss + 8:so + i * ss + 136].split(b"\0")[0].decode("latin-1") for i in range(sn)]
        out, seen = [], set()
        for i in range(en):
            b = eo + i * es
            typ, si, _eid = struct.unpack("<III", data[b:b + 12])
            label = data[b + 140:b + 268].split(b"\0")[0].decode("latin-1")
            for sp, t, rx, path in _DETAIL_RX:
                if (t and TYPES[typ] != t) or not names[si].startswith(sp):
                    continue
                m = rx.match(label)
                if not m:
                    continue
                sm = re.search(r"\[#(\d+)\]", names[si])
                key = path.replace("{1}", m.group(1) if m.groups() else "")                           .replace("{2}", m.group(2) if len(m.groups()) > 1 else "")                           .replace("{s}", sm.group(1) if sm else "0")
                if key not in seen:
                    seen.add(key)
                    out.append((key, i))
                    if key.startswith("core."):            # remember P / E
                        out.append((key.rsplit(".", 1)[0] + ".kind_" + m.group(1), i))
                break
        return out

    @staticmethod
    def _build_index(data, so, ss, sn, eo, es, en):
        names = [data[so + i * ss + 8:so + i * ss + 136].split(b"\0")[0].decode("latin-1") for i in range(sn)]
        idx = {}
        for i in range(en):
            b = eo + i * es
            typ, si, _eid = struct.unpack("<III", data[b:b + 12])
            label = data[b + 140:b + 268].split(b"\0")[0].decode("latin-1")
            for key, (sens, lab, t) in PICK.items():
                if key in idx or TYPES[typ] != t or not names[si].startswith(sens):
                    continue
                if label == lab or (key == "dimm_t" and label.startswith(lab)):
                    idx[key] = i
        return idx


if __name__ == "__main__":       # py hwinfo.py : print what the dashboard would show
    st, vals = HWiNFO().read()
    print(st)
    for k, v in vals.items():
        print(f"{k:9} {v['v']:10.1f} {v['u']}")
