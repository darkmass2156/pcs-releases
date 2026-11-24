"""Publish PCS values as HWiNFO custom sensors, so FanControl (HWInfo plugin) can use them as curve sources.

HWiNFO reads custom sensors from HKCU\\Software\\HWiNFO64\\Sensors\\Custom\\<sensor>\\<Type><n> (Name, Value, Unit).
They show in HWiNFO as a sensor called "PCS" and go out through its shared memory like any other reading.
HWiNFO builds its sensor list at start: restart HWiNFO once after the first run so "PCS" appears.

Fail-safe: when the Mega data is stale (> STALE_S) or the dashboard exits cleanly, the temperatures are set to
FAILSAFE_C (curves on them go to their top), the percentages to 100. A hard crash leaves the last values: build the
FanControl curves as a Mix (max) with a CPU-temperature curve so P1 never depends on PCS alone.
The fan / P1 requests (REQ) have their own defaults, fail-safe values and floors, see below.
"""
import json
import os
import sys
import time

if sys.platform == "win32":
    import winreg

ROOT = r"Software\HWiNFO64\Sensors\Custom\PCS"
STALE_S = 10
FAILSAFE_C = 99.0

# PC fan / P1 requests (2026-11-08): what PCS commands, 0..100 %. FanControl has two profiles (owner 2026-11-08):
# "Standalone" (owner's own curves, PCS ignored) and "PCS", where each control follows its request 1:1 (a Linear curve
# 0 -> 0 %, 100 -> 100 %). So in the PCS profile these are the real commands: they must always be safe on their own.
#  - DEFAULT: what we send while no PCS program wants anything else.
#  - FAILSAFE: dashboard exits cleanly (a hard kill or HWiNFO stopping leaves the last values: switch FanControl back
#    to Standalone when PCS isn't running).
#  - FLOOR: never sent below this, whatever a program asks (P1 is the PC's only pump).
# FanControl's HWInfo plugin reads HWiNFO's gadget values only (HKCU\Software\HWiNFO64\VSB) and offers only °C ones
# as curve sources, so the requests are TEMPERATURE readings whose value is the % (Other3..6 held them first: removed).
REQ = [  # key, slot, name, default %, fail-safe %, floor %
    ("req_rad", "Temp9",  "PCS req rad fans % (Fan2+3)", 40, 70, 20),
    ("req_top", "Temp10", "PCS req top fans % (Fan1)",   30, 70, 30),   # owner: top never 0
    ("req_p1",  "Temp11", "PCS req P1 % (Fan6)",         40, 70, 30),   # owner: ~40 idle, never < 30 (noise)
    ("req_psu", "Temp12", "PCS req PSU fan % (Fan7)",    40, 80, 40),
]
OBSOLETE = ["Other3", "Other4", "Other5", "Other6"]
PSU_LO_W = 400           # PSU fan: default % up to PSU_LO_W estimated system W, linear to PSU_HI_PCT at PSU_HI_W
PSU_HI_W = 800           # (2026-11-08 a step at 800 W; ramp since 2026-11-09)
PSU_HI_PCT = 80
REST_W = 80              # board, RAM, drives, pumps, fans: added to CPU + GPU (HWiNFO has no PSU input power)
P1_HOT_CUBE_C = 25.0     # cube above this -> P1 request at least P1_HOT_CUBE_MIN_PCT, in every program (and none)
P1_HOT_CUBE_MIN_PCT = 45
LOW_FLOOR_PCT = 10       # rad / top floor while the Mega runs Mode 4 (owner 2026-11-10)
# owner 2026-11-13: P1 (W_PUMP+) is run by the BIOS curve, FanControl leaves that header unmanaged. The P1 request
# (Temp11) is no longer published (its registry slot is removed), the sweep and the /p1 override are off.
# Set False to give P1 back to the software (and point FanControl's Fan #6 at "PCS req P1" again).
P1_BY_BIOS = True    # 2026-11-21 (owner): BIOS curve 87 % -> 100 % at 80 C; FanControl was not driving Fan #6 anyway


def pc_requests(pc, prog=None):
    """Requests: defaults, the PSU rule, then whatever the running PCS program asks (prog: {key: %})."""
    v = (pc.get("v") or {}) if pc and pc.get("status") == "ok" else {}
    g = lambda k: v[k]["v"] if k in v else None
    cpu, gpu = g("cpu_w"), g("gpu_w")
    sys_w = (cpu or 0) + (gpu or 0) + REST_W if (cpu is not None or gpu is not None) else None
    req = {key: float(dflt) for key, _s, _n, dflt, _f, _fl in REQ}
    if sys_w is not None and sys_w > PSU_LO_W:            # ramp (owner 2026-11-09: 600 W stayed at 40 % with the old step)
        f = min(1.0, (sys_w - PSU_LO_W) / (PSU_HI_W - PSU_LO_W))
        req["req_psu"] = max(req["req_psu"], round(req["req_psu"] + (PSU_HI_PCT - req["req_psu"]) * f, 1))
    for k, val in (prog or {}).items():
        if k in req and val is not None:
            req[k] = max(req[k], float(val)) if k == "req_psu" else float(val)
    cube = g("t_sensor")                                  # coolant into the PC (Apex T_Sensor, whole °C)
    if cube is not None and cube > P1_HOT_CUBE_C:         # owner 2026-11-09: P1 never under 45 % with the cube over 25 °C
        req["req_p1"] = max(req["req_p1"], float(P1_HOT_CUBE_MIN_PCT))
    if P1_BY_BIOS:
        req["req_p1"] = None
    req["sys_w"] = sys_w
    return req

# key in the CSV row -> (HWiNFO slot, name shown in HWiNFO / FanControl, unit for Other)
OUT = [
    ("T1",     "Temp0",  "PCS T1 loop return", None),
    ("T2",     "Temp1",  "PCS T2 mixing valve", None),
    ("T3",     "Temp2",  "PCS T3 outdoor rad", None),
    ("DP",     "Temp3",  "PCS dew point (max)", None),
    ("margin", "Temp4",  "PCS T2 - dew point", None),
    ("case_t", "Temp5",  "PCS case air", None),
    ("Troom",  "Temp6",  "PCS room air", None),
    ("Tset",   "Temp7",  "PCS setpoint", None),
    ("V2out",  "Other0", "PCS V2 outdoor share", "%"),
    ("fan",    "Other1", "PCS rad fans", "%"),
    ("pump",   "Other2", "PCS P2", "%"),
    ("cube_margin", "Temp8", "PCS cube - dew point", None),   # coolant into the PC vs DP (Apex T_Sensor - DP)
]


def derived(r):
    """Row -> values to publish (None = not available)."""
    v = dict(r)
    v["margin"] = r["T2"] - r["DP"] if r.get("T2") is not None and r.get("DP") is not None else None
    v["cube_margin"] = r["Tcube"] - r["DP"] if r.get("Tcube") is not None and r.get("DP") is not None else None
    a, b = r.get("T_A"), r.get("T_B")
    v["case_t"] = max(x for x in (a, b) if x is not None) if (a is not None or b is not None) else None
    return v


# The standalone reader (tools/pc_sensors, 2026-11-22) drives the NCT6701D headers itself from this file, so FanControl
# and HWiNFO's custom sensors are no longer needed. Header = NCT control index (0-based): Fan #1 top exhaust, #2 rear
# 120 rad, #3 front 360 rad, #6 P1 (only while P1_BY_BIOS is False), #7 PSU fan. The reader ignores the file while
# FanControl runs and gives every header back to the BIOS curve when the file is older than 15 s or missing.
import pcs_paths
FAN_REQ = os.path.join(pcs_paths.SENSORS, "fan_req.json")
FAN_CH = {"req_top": [0], "req_rad": [1, 2], "req_p1": [5], "req_psu": [6]}


def fan_req_write(final, fast=None):
    duty = {}
    for key, chs in FAN_CH.items():
        if final.get(key) is not None and not (key == "req_p1" and P1_BY_BIOS):
            for ch in chs:
                duty[str(ch)] = round(float(final[key]), 1)
    try:
        os.makedirs(os.path.dirname(FAN_REQ), exist_ok=True)
        tmp = FAN_REQ + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "duty": duty, "fast": fast}, f)
        os.replace(tmp, FAN_REQ)
    except OSError:
        pass


def fan_req_clear():
    try:
        os.remove(FAN_REQ)
    except OSError:
        pass


class HWiNFOOut:
    def __init__(self):
        self.ok = sys.platform == "win32"
        self.made = False
        self.req_made = False
        self.last = {}
        if self.ok:
            for slot in OBSOLETE + (["Temp11"] if P1_BY_BIOS else []):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, ROOT + "\\" + slot)
                except OSError:
                    pass

    def _write(self, slot, name, value, unit, force_name=False):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ROOT + "\\" + slot) as k:
            if not self.made or force_name:
                winreg.SetValueEx(k, "Name", 0, winreg.REG_SZ, name)
                if unit:
                    winreg.SetValueEx(k, "Unit", 0, winreg.REG_SZ, unit)
            winreg.SetValueEx(k, "Value", 0, winreg.REG_SZ, f"{value:.2f}")

    def publish_req(self, req):
        """req: {key: %} from pc_requests(); None = fail-safe values. Floors always apply."""
        if not self.ok:
            return
        final = {}
        for key, slot, name, dflt, fs, floor in REQ:
            if key == "req_p1" and P1_BY_BIOS:
                continue
            v = fs if req is None else (req.get(key) if req.get(key) is not None else dflt)
            lf = None if req is None else req.get("low_floor")
            if lf is not None and lf is not False and key in ("req_rad", "req_top"):
                # Mode 4: PC fans may go to 10 %, Mode 5 Daily: off (pcs_dash.py). True = LOW_FLOOR_PCT.
                floor = min(floor, LOW_FLOOR_PCT if lf is True else float(lf))
            v = max(float(floor), min(100.0, float(v)))
            final[key] = v
        if req is None:
            fan_req_clear()          # clean exit: the reader hands the headers back to the BIOS curves
        else:
            fan_req_write(final, req.get("fast"))
        for key, slot, name, dflt, fs, floor in REQ:
            if key not in final:
                continue
            v = final[key]
            if self.last.get(slot) == round(v, 2) and self.req_made:
                continue
            try:
                self._write(slot, name, v, None, force_name=not self.req_made)
                self.last[slot] = round(v, 2)
            except OSError:
                return
        self.req_made = True

    def publish(self, row, age_s):
        """row: latest CSV row (dict) or None; age_s: seconds since it arrived."""
        if not self.ok:
            return
        stale = row is None or age_s > STALE_S
        vals = {} if stale else derived(row)
        for key, slot, name, unit in OUT:
            v = vals.get(key)
            if stale or v is None:
                v = 100.0 if unit == "%" else FAILSAFE_C if key not in ("margin", "cube_margin") else 0.0
            if self.made and self.last.get(slot) == round(v, 2):
                continue
            try:
                self._write(slot, name, v, unit)
                self.last[slot] = round(v, 2)
            except OSError:
                return
        self.made = True

    def failsafe(self):
        if self.ok:
            self.made = True
            self.publish(None, 0)
            self.publish_req(None)


if __name__ == "__main__":   # py hwinfo_out.py : write the fail-safe values once (creates the "PCS" sensor)
    o = HWiNFOOut()
    o.publish(None, 0)
    o.publish_req(None)
    print("PCS custom sensor written to HKCU\\" + ROOT + " (fail-safe values). Restart HWiNFO to see it.")
    time.sleep(0.1)
