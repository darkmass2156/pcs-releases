"""Daily profile, PC side (owner 2026-11-13): the quiet everyday mode.

The Mega runs firmware Mode 5 on its own (T2 held at max(17 °C, DP + 4), P2 40 %, outdoor fans <= 45 %, FW 0.9.23).
This module runs the PC case fans while the Mega is in Mode 5: they stay OFF while everything is comfortable and
come on (smoothly, from a spin-up floor) only when something needs air. Every guard maps its reading to 0..1 between
a start and a full temperature; the fans follow the highest one:
  rads (Fan #2 rear 120 + #3 front 360): every guard
  top exhaust (Fan #1): every guard; board / air guards at full strength, coolant / CPU / GPU guards at 0.7
Board parts (VRM, chipset, DIMM, SSD) and the case air need the top exhaust; the coolant / CPU / GPU guards need the
rads (only useful while the coolant is warmer than the case air: with T2 at 17 °C the rads would only heat it).

Anti-flap: on as soon as a guard is over its start; off only after every guard has stayed OFF_HYST_C under its start
for OFF_HOLD_S. While on, never under ON_MIN_PCT (spin-up). Up at once, down DOWN_PCT_PER_S.
No HWiNFO data -> NO_DATA_PCT (fans on: nothing is watched). Thresholds ASSUMED (first guess, tune on the logs).

Heater (owner 2026-11-13: "use the fans in the PC instead of closing the valve"): with T2 at 17 °C the case air is
warmer than the coolant, so the case rads can put heat INTO the loop. When the outdoor fans are already at their
minimum and T2 sinks HEAT_START_C under its target, the rad fans ramp up (from ON_MIN_PCT, HEAT_KI per °C·s, at most
HEAT_MAX_PCT) instead of the Mega closing V2 (in Mode 5 it only closes from DAILY_V2_CLOSE_C, FW 0.9.24); they come
down again over target and stop HEAT_RELEASE_S after T2 is HEAT_STOP_C over it. Only while the case air is at least
HEAT_AIR_MIN_C warmer than T1 (otherwise the rads would cool the water). Top exhaust not used for this.

A hard kill of the dashboard leaves FanControl at its last request (possibly 0 %): the scheduled task restarts the
dashboard on failure, and a clean exit sends the 70 % fail-safe (hwinfo_out.py).
"""
import time

from pcs_prog import P1_STEPS, P1_STEP_SLACK, P1_STEP_DOWN_S, p1_load

# key, label, where the value comes from, start °C, full °C, which fans ("board" = top at 1.0, "loop" = top at 0.7)
GUARDS = [
    ("vrm",  "VRM",          ("v", "vrm_t"),        60.0, 80.0, "board"),
    ("pch",  "Chipset",      ("d", "mb", "pch_sio"), 62.0, 75.0, "board"),
    ("dimm", "DIMM",         ("v", "dimm_t"),       50.0, 60.0, "board"),
    ("ssd",  "SSD",          ("d", "ssd", "t"),     60.0, 70.0, "board"),
    ("cpu",  "CPU (avg)",    ("cpu_s",),            68.0, 85.0, "loop"),
    ("gpu",  "GPU hot spot", ("v", "gpu_hot"),      78.0, 92.0, "loop"),
    ("vram", "GPU memory",   ("d", "gpu", "mem_j"), 80.0, 95.0, "loop"),
    ("air",  "Case air",     ("air",),               6.0, 10.0, "board"),   # warmer case SHT minus room air, °C
    ("ret",  "Return T1",    ("ret",),               3.0, 10.0, "loop"),    # T1 minus case air, °C: only when T2
]                                                                          # can't hold its target (outdoor capped)
ON_MIN_PCT = 30.0          # spin-up floor while on
OFF_HYST_C = 3.0
OFF_HOLD_S = 120.0
DOWN_PCT_PER_S = 0.4       # ~2 % per 5 s, like Passive
NO_DATA_PCT = 40.0
T2_BEHIND_C = 2.0          # T2 more than this over its target ...
T2_BEHIND_S = 300.0        # ... for this long: the capped outdoor loop can't keep up -> the return guard may act
TOP_LOOP_SHARE = 0.7
HEAT_START_C = 0.2         # T2 this far under its target (outdoor fans at minimum) -> case rads warm the coolant
HEAT_STOP_C = 0.3          # ... until T2 is this far over it for HEAT_RELEASE_S
HEAT_RELEASE_S = 60.0
HEAT_KI = 0.3              # %/s per °C off target (0.5 °C under -> +0.15 %/s, 30 -> 60 % in ~3 min)
HEAT_MAX_PCT = 60.0        # quiet cap
HEAT_AIR_MIN_C = 1.0       # case air at least this much warmer than T1, or the rads can't warm the water
OUTDOOR_FAN_MIN = 10       # the Mega's FAN_PWM_MIN: outdoor fans at their minimum
# P1 in Daily (owner 2026-11-13 evening: P1 back from the BIOS to software, "the GPU might overheat"): the highest of
#   45 % idle, the load rule (CPU + GPU W 150 -> 400 W: 45 -> 65 %), GPU hot spot (smoothed) 60 -> 85 °C: 45 -> 91 %,
#   CPU package (smoothed) 45 -> 70 °C: 45 -> 91 %; snapped up to the quiet steps (45 / 62 / 76 / 83 / 91 / 100),
#   up at once, down one step after P1_STEP_DOWN_S. ASSUMED curves.
P1_IDLE = 45.0
P1_GPU_LO, P1_GPU_HI = 60.0, 85.0
P1_CPU_LO, P1_CPU_HI = 45.0, 70.0
P1_HI = 91.0
GPU_TAU_S = 15.0
# owner 2026-11-13: the top exhaust always turns a little, and the whole case is flushed briefly every 10 min
TOP_MIN_PCT = 20.0         # top exhaust never under this in Daily (10 % turned ~220 rpm in Mode 4)
FLUSH_EVERY_S = 600.0      # rads + top at FLUSH_PCT for FLUSH_S every FLUSH_EVERY_S ...
FLUSH_S = 60.0
FLUSH_PCT = 40.0           # ... unless the rads already ran at >= FLUSH_PCT since the last one (timer restarts)


def _get(src, path):
    x = src
    for k in path:
        if not isinstance(x, dict):
            return None
        x = x.get(k)
    if isinstance(x, dict):
        x = x.get("v")
    return x if isinstance(x, (int, float)) else None


class DailyFans:
    def __init__(self):
        self.rad = self.top = 0.0
        self.on = False
        self.calm_since = None
        self.behind_since = None
        self.t = time.time()
        self.info = {}
        self.heat = 0.0            # heater request, % (0 = off)
        self.heat_ok_since = None  # T2 back over target since
        self.gpu_s = None          # smoothed GPU hot spot
        self.p1 = None             # current P1 step
        self.p1_lower_since = None
        self.p1_why = ""
        self.t_p1 = 0.0
        self.last_flush = time.time()   # first flush 10 min after start
        self.flush_until = 0.0

    def p1_update(self, pc, cpu_s, heat_w, now=None):
        """P1 request in Daily (%), see the P1_* notes above. None without HWiNFO (defaults apply)."""
        now = now or time.time()
        dt = max(0.0, min(5.0, now - self.t_p1)) if self.t_p1 else 0.0
        self.t_p1 = now
        if not pc or pc.get("status") != "ok":
            return None
        gh = _get({"v": pc.get("v") or {}}, ("v", "gpu_hot"))
        if gh is None:
            self.gpu_s = None
        else:
            self.gpu_s = gh if self.gpu_s is None else self.gpu_s + min(1.0, dt / GPU_TAU_S) * (gh - self.gpu_s)
        ramp = lambda v, lo, hi: P1_IDLE + (P1_HI - P1_IDLE) * min(1.0, max(0.0, (v - lo) / (hi - lo))) if v is not None else P1_IDLE
        parts = {"load": p1_load(heat_w, P1_IDLE), "GPU": ramp(self.gpu_s, P1_GPU_LO, P1_GPU_HI),
                 "CPU": ramp(cpu_s, P1_CPU_LO, P1_CPU_HI)}
        want = max(P1_IDLE, *parts.values())
        step = next((s for s in P1_STEPS if s >= want - P1_STEP_SLACK), P1_STEPS[-1])
        if self.p1 is None or step > self.p1:
            self.p1, self.p1_lower_since = step, None
        elif step < self.p1:
            self.p1_lower_since = self.p1_lower_since or now
            if now - self.p1_lower_since >= P1_STEP_DOWN_S:
                lower = [s for s in P1_STEPS if s < self.p1]
                self.p1 = max(step, lower[-1] if lower else step)
                self.p1_lower_since = now
        else:
            self.p1_lower_since = None
        top = max(parts, key=parts.get)
        self.p1_why = "idle" if want <= P1_IDLE + 0.5 else f"{top} ({'GPU hot spot %.0f °C' % self.gpu_s if top == 'GPU' else 'CPU %.0f °C' % cpu_s if top == 'CPU' else '%.0f W' % (heat_w or 0)})"
        return float(self.p1)

    def update(self, pc, row, cpu_s, now=None):
        """pc: hub.pc ({"status", "v", "d"}), row: latest Mega row, cpu_s: smoothed CPU package (Prog.cpu_s).
        Returns (rad %, top %); self.info holds the readings for the page."""
        now = now or time.time()
        dt = max(0.0, min(5.0, now - self.t))
        self.t = now
        ok = bool(pc) and pc.get("status") == "ok"
        if not ok:
            self.rad = self.top = NO_DATA_PCT
            self.on = True
            self.info = {"on": True, "rad": NO_DATA_PCT, "top": NO_DATA_PCT, "why": "no HWiNFO data", "g": []}
            return self.rad, self.top
        src = {"v": pc.get("v") or {}, "d": pc.get("d") or {}, "cpu_s": cpu_s}
        ta, tb, troom = row.get("T_A"), row.get("T_B"), row.get("Troom")
        case_air = max(x for x in (ta, tb) if x is not None) if (ta is not None or tb is not None) else None
        # room SHT not trusted (owner 2026-11-15): the "case air - room" guard is off until the SHT45s are in
        src["air"] = None
        t2, tset, t1 = row.get("T2"), row.get("Tset"), row.get("T1")
        behind = t2 is not None and tset is not None and t2 > tset + T2_BEHIND_C
        self.behind_since = (self.behind_since or now) if behind else None
        lagging = self.behind_since is not None and now - self.behind_since >= T2_BEHIND_S
        ref = case_air if case_air is not None else troom
        src["ret"] = t1 - ref if (lagging and t1 is not None and ref is not None) else None

        # heater: case rads warm the coolant before the Mega closes V2
        heat_why = ""
        can_heat = (row.get("state") == "RUN" and row.get("pen") == 1 and t2 is not None and tset is not None
                    and t1 is not None and case_air is not None and case_air - t1 >= HEAT_AIR_MIN_C)
        if not can_heat:
            self.heat, self.heat_ok_since = 0.0, None
        else:
            err = tset - t2                                  # > 0: too cold
            outdoor_min = (row.get("fan") or 0) <= OUTDOOR_FAN_MIN
            if self.heat == 0.0:
                if err >= HEAT_START_C and outdoor_min:
                    self.heat = ON_MIN_PCT
            else:
                self.heat = max(ON_MIN_PCT, min(HEAT_MAX_PCT, self.heat + HEAT_KI * err * dt))
                if err <= -HEAT_STOP_C:
                    self.heat_ok_since = self.heat_ok_since or now
                    if now - self.heat_ok_since >= HEAT_RELEASE_S:
                        self.heat, self.heat_ok_since = 0.0, None
                else:
                    self.heat_ok_since = None
                if not outdoor_min and err < 0:              # outdoor fans regulating again: not needed
                    self.heat, self.heat_ok_since = 0.0, None
            if self.heat > 0:
                heat_why = f"warming coolant (T2 {t2 - tset:+.1f} °C, case air {case_air - t1:+.1f} over T1)"

        want_rad = want_top = 0.0
        hot = []
        calm = True
        g = []
        for key, label, path, lo, hi, kind in GUARDS:
            val = _get(src, path) if path[0] in ("v", "d") else src.get(path[0])
            if val is None:
                continue
            f = (val - lo) / (hi - lo)
            f = 0.0 if f < 0 else 1.0 if f > 1 else f
            if val > lo - OFF_HYST_C:
                calm = False
            if val > lo:
                hot.append(f"{label} {val:.0f}")
                pct = ON_MIN_PCT + f * (100.0 - ON_MIN_PCT)
                want_rad = max(want_rad, pct)
                want_top = max(want_top, pct if kind == "board" else max(ON_MIN_PCT, pct * TOP_LOOP_SHARE))
            g.append({"k": key, "l": label, "v": round(val, 1), "lo": lo, "hi": hi, "hot": val > lo})

        if hot:
            self.on = True
            self.calm_since = None
        elif self.on:
            self.calm_since = self.calm_since or now if calm else None
            if self.calm_since is not None and now - self.calm_since >= OFF_HOLD_S:
                self.on = False
        if self.on:
            want_rad = max(want_rad, ON_MIN_PCT)
            want_top = max(want_top, ON_MIN_PCT)
        else:
            want_rad = want_top = 0.0
        if self.heat > want_rad:                             # heater: rads only, follows its own ramp
            want_rad = self.heat
        # up at once, down slowly (but straight to 0 once released)
        for attr, want in (("rad", want_rad), ("top", want_top)):
            cur = getattr(self, attr)
            if want >= cur or want == 0.0 or (attr == "rad" and self.heat > 0):
                cur = want
            else:
                cur = max(want, cur - DOWN_PCT_PER_S * dt)
            setattr(self, attr, round(cur, 1))
        # flush + top floor (after the ramp, so a flush ends at once)
        if now < self.flush_until:
            flushing = True
        elif self.rad >= FLUSH_PCT:
            self.last_flush, flushing = now, False                  # the air is moving anyway
        elif now - self.last_flush >= FLUSH_EVERY_S:
            self.last_flush, self.flush_until, flushing = now, now + FLUSH_S, True
        else:
            flushing = False
        rad_out = max(self.rad, FLUSH_PCT) if flushing else self.rad
        top_out = max(self.top, FLUSH_PCT if flushing else TOP_MIN_PCT)
        flush_txt = (f"flushing the case ({max(0, self.flush_until - now):.0f} s)" if flushing
                     else f"next flush in {max(0, FLUSH_EVERY_S - (now - self.last_flush)) / 60:.0f} min")
        why = ", ".join(hot + ([heat_why] if heat_why else [])) if (hot or heat_why) else ("cooling down, off in %d s" % max(0, OFF_HOLD_S - (now - self.calm_since))
                                          if self.on and self.calm_since else "settling" if self.on else "all cool")
        if flushing:
            why = flush_txt + ("" if why == "all cool" else ", " + why)
        self.info = {"p1": self.p1, "p1_why": self.p1_why, "on": self.on or self.heat > 0 or flushing, "heat": round(self.heat, 1),
                     "rad": rad_out, "top": top_out, "why": why, "g": g, "flush": flushing, "flush_txt": flush_txt,
                     "behind": lagging, "behind_s": round(now - self.behind_since) if self.behind_since else 0}
        return rad_out, top_out


# ---------------------------------------------------------------------------------------------------------------
# Instant P1 floor, every mode (owner 2026-11-13: "the pump must respond instantly to load, else we cook shit").
# The program rules work on smoothed values (60 s load average, CPU smoothed 40 s), so a load took ~60 s to move P1.
# This floor uses the CURRENT CPU + GPU W and GPU hot spot: up in the same second, down one step per HOLD_S after the
# load is gone. The final P1 request is max(program / Daily rule, this floor).
FAST_W_STEPS = [(450.0, 91.0), (350.0, 83.0), (250.0, 76.0), (150.0, 62.0), (120.0, 50.0)]   # CPU + GPU W -> P1 %
FAST_HOT_STEPS = [(75.0, 91.0), (68.0, 83.0), (60.0, 76.0)]   # GPU hot spot °C -> P1 % (owner 2026-11-14, was 85/78/70)
FAST_HOLD_S = 30.0


class FastP1:
    def __init__(self):
        self.pct = 0.0
        self.lower_since = None
        self.why = ""

    def update(self, pc, now=None):
        now = now or time.time()
        v = (pc or {}).get("v") or {} if (pc or {}).get("status") == "ok" else {}
        g = lambda k: (v.get(k) or {}).get("v")
        w = (g("cpu_w") or 0.0) + (g("gpu_w") or 0.0)
        hot = g("gpu_hot")
        want, why = 0.0, ""
        for lim, pct in FAST_W_STEPS:
            if w >= lim:
                want, why = pct, f"{w:.0f} W"
                break
        for lim, pct in FAST_HOT_STEPS:
            if hot is not None and hot >= lim:
                if pct > want:
                    want, why = pct, f"GPU hot spot {hot:.0f} °C"
                break
        if want >= self.pct:
            self.pct, self.lower_since = want, None
            if want:
                self.why = why
        else:
            self.lower_since = self.lower_since or now
            if now - self.lower_since >= FAST_HOLD_S:              # one step down per hold
                lower = [p for _, p in FAST_W_STEPS if p < self.pct]
                self.pct = max(want, max(lower) if lower else 0.0)
                self.lower_since = now
        return self.pct
