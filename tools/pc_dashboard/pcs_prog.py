"""PCS programs run from the PC (2026-11-08, owner): Auto, Benchmark (and Extreme, locked), plus Manual.

The Mega is driven through its serial test mode (t, f<n>, pd<n>/p0, o<n>%, k, x) and kept alive with the link
heartbeat `lnk` (FW 0.9.10: once armed, 10 s without one -> the Mega leaves test mode, P2 off, V2 parks).

Goal: cube (Apex T_Sensor, coolant into the PC) - DP (highest valid dew point) at the program target.
  Auto      +10 C, program limit +6 C
  Benchmark  +5 C, program limit +3 C
  Extreme    +2 C: locked (alpha), needs more data / a calibrated cube sensor

Scheme (2026-11-08, owner: "the fans are the quick authority outside, keep the outdoor coolant in check so the inside
needs adjusting less often"; Heaven run: P2 at ~90 % of P1's rpm sent little water outside) -- the outdoor loop is run
like a chiller:
  1. Fast loop: the OUTDOOR FANS hold T2 (water back into the res, 0.1 C NTC) at T2set = DP + target + trim.
     Incremental PI, 10..70 % (owner: never above 70, noise). Held at minimum while T3 < DP + T3_FLOOR_C.
  2. Slow trim from the cube: T2set moves by at most +-TRIM_MAX_C so the cube itself ends on target (absorbs the Apex
     offset and any res mixing). Starts after START_SETTLE_S (cube lag).
  3. Inside mostly fixed: V2 at V2_OP_PCT (45 %, owner), P2 at P2_RUN_PCT, P1 quiet at P1_RUN_PCT. P2 may run at or above
     the match point now (decided 2026-11-08): with T2 regulated the short circuit through the 5-way block only makes
     cube ~= T2, which is what we want; it was only dangerous while the outdoor water temperature was uncontrolled.
     Inside trims only when the fans are pinned for SAT_S: fans at minimum and still too cold -> V2 down 5 % (to its
     bottom), then P2 down 5 %; fans at maximum and still too warm -> V2 back up to 45 %, then P2 up 5 % (to P2_MAX).
     Fans comfortably high (>= FAN_RESTORE_PCT) for SAT_S -> V2 / P2 restored toward the operating point.
  4. Start: V2 opens from its bottom (10 % with T3 <= DP .. 30 % at T3 >= DP + 10) by 5 % per 30 s, fans at minimum.
     A cold slug (T2 more than FAST_COLD_C under T2set with the fans at minimum) closes V2 by 5 % every 10 s.
  5. Branch off: fans at minimum, V2 at its bottom, P2 at its minimum and still too cold for OFF_HOLD_S.
Safety on the colder of cube and T2 (T2 sees outdoor water ~30 s before the cube):
  margin < program limit -> outdoor fans to minimum, V2 down 10 %, PC rad fans 100 % (room air warms the coolant);
  margin < limit - EMERG_OFF_C -> branch off at once. The Mega's own T2 < DP + 1 trip stays armed.
PC rad fans otherwise: 20 % while T1 <= room air (they'd only add heat), +4 %/C above, max 60 %; top 0.7 x rad.

Requires the link: Mega data < 3 s old, HWiNFO ok with the cube < 5 s old, DP valid. Without it the program parks
the branch (x) and waits. A case button press (firmware Mode 2/3) hands control back to the firmware.
All numbers below are first guesses (ASSUMED) until the step tests on the loop.
"""
import csv
import os
import re
import threading
import time

from hwinfo_out import P1_BY_BIOS

PROGRAMS = {
    "auto":  {"name": "Auto", "target": 10.0, "limit": 6.0},
    "bench": {"name": "Benchmark", "target": 5.0, "limit": 3.0, "p1": 65,
              "fan_max": 80},   # owner 2026-11-09: fans <= 80 %. V2 stays capped at V2_OP_PCT (45 %): past it the
                                # T-port opens all three ports and the bypass mixes back in (V2 to 100 % was a mistake)   # P1 sweep 2026-11-09: knee at 65 %
}
def control_dp(r):
    """Dew point for start / regulation: the room SHT30 and the lower of the two case SHT31s (see LIMIT_PAD_C).
    Never above the full DP; falls back to it when none of the three is there."""
    dp = r["DP"]
    case = [x for x in (r.get("DP_A"), r.get("DP_B")) if x is not None]
    c = [x for x in (r.get("DP_R"), min(case) if case else None) if x is not None]
    return min(dp, max(c)) if c else dp


EXTREME_MSG = ("Extreme (cube 2 °C over dew point) is in Alpha: it needs more data from Auto / Benchmark and a "
               "proven cube calibration before it can be enabled.")

CTRL_S = 5.0                  # controller period
LNK_S = 2.0                   # heartbeat period (Mega times out after 10 s)
START_E_C = 1.0               # branch starts only when the cube is this far above its goal
# Control DP (2026-11-08, owner: "can do it but it must still respect the DP limit"). SHT31 A's dew point follows the case
# air temperature (slope 0.82 vs the room over the day; 17:24-17:37 DP_A 5.9 -> 15.8 while the room stayed ~9). The
# case pulls room air, so its real DP is ~the room's. Start and regulation aim at
#   goal = max(DPc + target, DP + limit + LIMIT_PAD_C),  DPc = max(DP_R, lower of DP_A / DP_B)
# so a high case sensor can't keep the branch in standby, but the goal never comes closer to the full DP (max of all
# valid, the Mega's) than the program limit + pad. The limits themselves still act on the full DP.
LIMIT_PAD_C = 0.5
# fast loop: outdoor fans hold T2
KF_P = 3.0                    # fan % per °C change of the T2 error           (ASSUMED)
KF_I = 0.08                   # fan % per °C per second of T2 error (ASSUMED: 1 °C -> +5 %/min). Was 0.25: under Heaven
                              # 2026-11-08 the fans ran 10 -> 70 in ~1 min and cycled against the T3 floor
FAN_STEP_MAX = 4.0            # max fan change per control period (was 8)
# T3 floor as a second, smooth loop: it caps the fans so T3 settles at DP + T3_FLOOR_C instead of the fans being
# slammed to minimum (that on/off rule made the 10 <-> 70 % cycle). fan = min(T2 loop, T3 cap)
KC_P = 4.0                    # cap % per °C change of (T3 - floor)
KC_I = 0.3                    # cap % per °C per second below / above the floor
FAN_MIN_PCT, FAN_MAX_PCT = 10, 70  # owner 2026-11-08: never above 70 % (noise)
T3_FLOOR_C = 1.5              # outdoor fans held at minimum while T3 < DP + this (indoor blue line / V2 body dry)
# slow trim of the T2 setpoint from the cube
K_TRIM = 0.002                # °C of setpoint per °C of cube error per second (1 °C -> 0.12 °C/min)
TRIM_MAX_C = 3.0
START_SETTLE_S = 180.0        # no trim for this long after a start (the cube lags T2)
# inside: operating point and trims
V2_OP_PCT = 45                # owner 2026-11-08: ~45 % outdoor works; 90 deg mixes all three
V2_MIN_PCT = 30               # V2 bottom with warm outdoor water (owner: below 30 useless)
V2_BOT_COLD_PCT = 10          # V2 bottom when the outdoor water is very cold (T3 <= DP); first run: 30 % was too much
V2_BOT_SPAN_C = 10.0          # bottom rises linearly to V2_MIN_PCT at T3 = DP + this
V2_STEP_PCT = 5               # V2 opens at most this much per V2_MIN_GAP_S (closing is never limited)
V2_MIN_GAP_S = 30.0
P2_RUN_PCT = 40               # P2 operating point (ASSUMED at/above the match point with P1 at P1_RUN_PCT)
P2_MIN_PCT, P2_MAX_PCT = 25, 60
P2_STEP_PCT = 5
P1_IDLE_PCT = 40              # owner 2026-11-08: P1 ~40 % idle, never below 30, only as high as needed (noise)
P1_RUN_PCT = 45               # P1 floor in Auto (P1 no longer has to out-run P2); Benchmark: PROGRAMS["bench"]["p1"]
# P1 by load in Auto and Passive (owner 2026-11-09, after the P1 sweep: 45 % starves the blocks under load, 65 % is
# the knee, more buys ~0.6 °C per +10 %). CPU + GPU W averaged (heat_avg, EMA 60 s) so it glides, no jumps:
#   P1_LOAD_LO % at P1_W_LO .. P1_LOAD_HI % at >= P1_W_HI (linear). Passive starts lower: P1_IDLE_PCT at
#   <= P1_W_IDLE, joining the ramp at P1_W_LO. The CPU-temperature boost adds on top.
P1_W_IDLE, P1_W_LO, P1_W_HI = 100.0, 150.0, 400.0
P1_LOAD_LO, P1_LOAD_HI = 45.0, 65.0
SAT_S = 120.0                 # fans pinned this long -> one inside trim step
FAST_COLD_C = 2.0             # T2 this far under T2set with the fans at minimum -> V2 closes 5 % every 10 s (cold slug)
FAN_RESTORE_PCT = 40          # fans at or above this for SAT_S -> V2 / P2 back toward the operating point
OFF_HOLD_S = 300.0            # everything at minimum and still too cold this long -> branch off
# limits / PC fans
EMERG_OFF_C = 1.5             # margin below limit - this -> branch off at once
RAD_EMERG_PCT = 100           # PC rad fan request near the limit
RAD_MIN_PCT, RAD_MAX_PCT = 20, 60  # PC rad fans (Fan #2 / #3) in normal regulation
RAD_PCT_PER_C = 4.0           # +4 % per °C the loop return (T1) is above the room air (colder coolant: they only add heat)
# Case air vs coolant (owner 2026-11-08: while cooling, the case air sits far above the coolant). The intake rads are then
# the coldest thing in the case: run them a little so the case air (VRM, RAM, SSDs) is cooled through them too. Costs a
# little heat into the coolant, which the outdoor branch takes. owner 2026-11-08: the case air should sit halfway
# between the coolant (T1) and the room air. So the rad fans run a slow integrating loop on
#   e = case air (warmer SHT31) - (T1 + room air) / 2      (> 0: case too warm -> more airflow through the cold rads)
# RAD_EQ_KI % per °C per second, at most RAD_EQ_STEP % per control period, within RAD_MIN_PCT..RAD_MAX_PCT. The older
# "T1 above room" rule still sets a floor. ASSUMED gains: 1 °C off the midpoint moves the fans ~3 % per 30 s.
# owner 2026-11-08 (later): too loud (rad 55 % / top 41 % at idle in Auto vs 33 / 30 in Passive). The coolant is regulated
# outdoors; the rad fans are the handbrake. So the case-air loop is now only a limiter: it acts once the case air is more
# than CASE_ABOVE_ROOM_C over the room air, and stops at RAD_EQ_MAX_PCT. Near the DP limit the rad fans still go to 100 %.
CASE_ABOVE_ROOM_C = 4.0
RAD_EQ_MAX_PCT = 40
RAD_EQ_KI = 0.1
RAD_EQ_STEP = 3.0
LINK_ROW_S, LINK_PC_S = 3.0, 5.0
LINK_GRACE_S = 10.0           # link must stay bad this long before the program parks the branch. R23 run 2026-11-08:
                              # the PC starved the dashboard for 4.9 + 7.1 s (Mega rows arrived in bursts, its own
                              # uptime continuous) and the 3 s check sent x mid-run. Meanwhile the outputs just hold;
                              # the Mega's own 10 s lnk watchdog and its T2 / T1 trips stay the backstop.
LOG_S = 1.0
# Passive program (owner 2026-11-08): branch off like Off (Mega Mode 1, P2 off, V2 parked), but the PC fans and P1
# follow the load instead of sitting at their flat defaults. All curves ASSUMED, tune from the logs.
#   liquid = the warmer of cube and T1 (branch off: both ~ the loop temperature); d = liquid - room air
#   rad fans (Fan #2 / #3): PAS_RAD_LO % at d <= PAS_D_LO .. 100 % at d >= PAS_D_HI; CPU package or GPU hot spot over
#     PAS_COMP_C push them to at least 60 % + PAS_COMP_PCT_PER_C per °C above
#   P1: p1_load() (40 % idle .. 45 % at 150 W .. 65 % at 400 W, averaged W); at least P1_LOAD_HI when d >= PAS_D_HI
#   top fans 0.7 x rad (>= 30 %); PSU fan by the usual 800 W rule
#   requests rise fast (PAS_UP per period) and fall slowly (PAS_DOWN), so the fans don't pump with game load spikes
#   no HWiNFO data -> rad 60 / P1 60 (blind, but safe)
# owner 2026-11-11: Passive let the CPU idle at 50+ °C (the old AIO did better), the fans didn't follow the CPU and
# P1 dipped to 45 % with the CPU at 50 °C. (Part of it: from the SHT swap until FW 0.9.20 "room" read the case air, so
# the loop looked as warm as the room and the rad fans sat at 30 %.) Now the CPU itself drives the fans and P1:
#   cpu_s = CPU package smoothed (rises with tau CPU_S_UP_S, falls with CPU_S_DOWN_S): a short spike barely moves it
#   rad fans: the highest of the loop rule (now 2 -> 10 °C over room, was 4 -> 14), the CPU rule (PAS_RAD_LO % at
#     cpu_s <= PAS_CPU_LO .. 100 % at PAS_CPU_HI) and the hot-component rule
#   P1: the highest of the load rule and the CPU rule (45 % at cpu_s <= PAS_P1_CPU_LO .. 91 % at PAS_P1_CPU_HI)
#   P1 flag (all programs): cpu_s >= P1_FLAG_CPU_S_C also means no 45 % (floor 62) for P1_FLAG_HOLD_S
# Physics: at idle the package sits ~12-17 °C over the cube, and in Passive the cube can only get down to about the
# room temperature, so ~35-40 °C idle is what Passive can reach; 30 °C needs the outdoor branch (Auto / Mode 4).
# Curves ASSUMED, tune from the logs.
PAS_D_LO, PAS_D_HI = 2.0, 10.0
PAS_RAD_LO = 30
PAS_CPU_LO, PAS_CPU_HI = 45.0, 75.0   # owner 2026-11-15 (was 35 -> 55: full fans at a 55 °C idle on warm water)
PAS_P1_CPU_LO, PAS_P1_CPU_HI = 38.0, 63.0
CPU_S_UP_S, CPU_S_DOWN_S = 40.0, 20.0
P1_FLAG_CPU_S_C = 42.0
PAS_COMP_C, PAS_COMP_PCT_PER_C = 80.0, 8.0
PAS_UP, PAS_DOWN = 100.0, 2.0  # % per CTRL_S (5 s); up: instant since 2026-11-22 (owner: "ramp instant to load and high
                               # temps"; was 20, before 10); down stays 2 % / 5 s so a game's spikes don't pump the fans
PAS_PERIOD_S = 1.0             # Passive / Off fan rule runs every second (was every CTRL_S = 5 s)
PAS_BLIND_PCT = 60
# owner 2026-11-15 (CS2 in summer-like conditions, branch off: ~470 W, cube 39 °C): the case fans must ramp with the
# load itself, CPU and GPU, not only with temperatures that lag. Two more rad-fan rules (the highest rule wins):
#   load: CPU + GPU W, smoothed (rises tau PAS_W_UP_S, falls PAS_W_DOWN_S): PAS_RAD_LO % at PAS_W_LO .. 100 % at PAS_W_HI
#   GPU:  hot spot PAS_GPU_LO .. PAS_GPU_HI °C -> PAS_RAD_LO .. 100 % (the GPU shares the water and the CPU sits after it)
# Room reference = the lower of the room sensor and the case air: the room SHT read 34-37 °C in a 20-30 °C room
# (sitting in warm air), which zeroed the loop rule. ASSUMED curves.
# owner 2026-11-15: "remove room air for now, temp sens are fucked". The room SHT is not used by any control rule until the
# SHT45s are in: Passive's loop rule runs on the absolute water temperature (warmer of cube / T1, PAS_LIQ_LO .. PAS_LIQ_HI
# -> 30 .. 100 %), Auto / Benchmark take ROOM_FIXED_C as the room. The page and the log still show the room reading.
USE_ROOM_SHT = False
ROOM_FIXED_C = 22.0
PAS_LIQ_LO, PAS_LIQ_HI = 26.0, 34.0   # owner 2026-11-21, was 30 -> 40 (idled the water at 33 C)
PAS_W_LO, PAS_W_HI = 120.0, 500.0
PAS_W_UP_S, PAS_W_DOWN_S = 0.0, 45.0   # load average: up instant (0 = follows the load; owner 2026-11-22, was 10)
# owner 2026-11-22 ("yes, faster ramp": a 30-60 s R23 run was nearly over before the rads moved): the Passive rad
# rule uses its own CPU average, rising with PAS_CPU_UP_S instead of the shared 40 s one (P1 / Daily keep theirs)
PAS_CPU_UP_S, PAS_CPU_DOWN_S = 0.0, 20.0   # up instant (0), down tau 20 s
PAS_GPU_LO, PAS_GPU_HI = 55.0, 75.0
# P1 boost on CPU temperature (owner 2026-11-08: "P1 is the fastest acting lever on the CPU, GPU and RAM"). Added on
# top of whatever P1 the program asks for (Passive, Auto, Benchmark), up to 100 %:
#   CPU package > P1B_ON_C:  want = P1B_JUMP x load + (P1B_K_LO .. P1B_K_HI by load) x (CPU - P1B_ON_C)
#     load = 0 at <= P1B_W_LO .. 1 at >= P1B_W_HI of CPU + GPU (instantaneous W, not the average)
#     the boost rises toward want at P1B_UP_LO + P1B_UP_LOAD x load %/s: ~2 % per 5 s idle, ~20 %/s (at once) under load
#   CPU P1B_HOLD_C .. P1B_ON_C: held;  below P1B_HOLD_C want = 0. The boost follows want down at P1B_DOWN %/s (slow, no
#   pumping with load spikes), also above P1B_ON_C when the CPU cools (the boost is not a peak hold).
# So low load + hot (e.g. a single-thread burst) gets a few % slowly, a hot CPU under a game gets a bump at once. ASSUMED.
# Examples: 70 °C at 80 W -> +10 % after ~25 s; 70 °C at 400 W -> +30 % at once; 80 °C at 400 W -> +70 %.
P1B_ON_C, P1B_HOLD_C = 65.0, 63.0
P1B_W_LO, P1B_W_HI = 150.0, 400.0
P1B_K_LO, P1B_K_HI = 2.0, 4.0      # % per °C over P1B_ON_C
P1B_JUMP = 10.0                    # % added straight away at full load
P1B_UP_LO = 0.4                    # %/s at no load (= 2 % per 5 s)
P1B_UP_LOAD = 20.0                 # extra %/s per unit of load
P1B_DOWN = 0.2                     # %/s (= 12 %/min)
# Load-engaged Auto (owner 2026-11-08): Passive switches itself to Auto when the PC makes real heat, and back when it
# has been quiet for a while, so the room isn't chilled for nothing at idle. Long hold times so a game's menu / loading
# screens don't toggle it. Heat = CPU package + GPU (shunt-corrected) W, averaged with a time constant (EMA).
#   engage:  average > ENG_ON_W                                  (avg tau ENG_AVG_S: ~2 min of load)
#   release: in Auto >= ENG_MIN_S  and  average < ENG_OFF_W for ENG_OFF_S
# Only when Passive was the choice; Auto picked by hand stays Auto. Any button press / case button ends it. ASSUMED.
# owner 2026-11-22: "passive goes to auto over 250 W. it shouldnt" -> off. Passive stays Passive under any load;
# Auto only when picked by hand. True brings the engage / release rule back unchanged.
LOAD_ENGAGE = False
ENG_ON_W, ENG_OFF_W = 200.0, 150.0
ENG_AVG_S = 60.0
ENG_MIN_S = 10 * 60.0  # owner 2026-11-08: 10 min (was 20)
ENG_OFF_S = 3 * 60.0   # owner 2026-11-09: 3 min (was 10); Auto kept chilling the room after the load ended
ENG_FALL_S = 10.0      # engage / release average falls with this tau (rises with ENG_AVG_S); P1 keeps the smooth one


# P1 sweep (owner 2026-11-08: "at 45 % not enough flow reaches the fins"). Benchmark only, under a steady load: P1 is
# stepped through SWEEP_LEVELS, SWEEP_DWELL_S each; the last SWEEP_AVG_S of each step are averaged (CPU package, GPU
# hot spot, cube, W, P1 rpm). The cube stays regulated by Benchmark, so CPU - cube is what P1 alone buys. The last
# level repeats the first to show drift. The P1 boost is ignored while it runs. Results: console + logs/p1sweep_*.csv.
SWEEP_LEVELS = [45, 55, 65, 75, 85, 100, 45]
# Heavy CPU load (owner 2026-11-09: "you can't do 45 on the CPU test", R23 at ~345 W reached 81 °C at P1 45 %):
# above SWEEP_CPU_HEAVY_W of CPU power at the start the sweep begins at the Benchmark P1 instead.
SWEEP_CPU_HEAVY_W = 150.0
SWEEP_LEVELS_HEAVY = [65, 75, 85, 100, 65]
SWEEP_CPU_MAX_C = 85.0        # CPU package above this, or GPU hot spot above SWEEP_HOT_MAX_C -> sweep aborts at once
SWEEP_HOT_MAX_C = 95.0
SWEEP_DWELL_S = 120.0         # owner 2026-11-09: faster (was 180); CPU - cube settles in < 60 s
SWEEP_AVG_S = 60.0
SWEEP_KNEE_C = 0.5            # CPU / GPU hot spot gain per +10 % P1 below this = past the sweet spot

# P1 quiet steps (owner 2026-11-09: the D5 is quietest at these duties; jump between them on need). The program's P1
# request (+ boost) is snapped to the lowest step >= request - P1_STEP_SLACK (Benchmark 65 -> 62); up at once, down only
# after the lower step has been enough for P1_STEP_DOWN_S.
#   flag: CPU package >= P1_FLAG_CPU_C or the averaged CPU + GPU load > P1_FLAG_W -> no 45 %, floor P1_FLAG_FLOOR, held
#         P1_FLAG_HOLD_S after the last trigger (hold time ASSUMED)
#   fans first: without the flag P1 stays at the lowest step while the PC rad fans are asked less than P1_FANS_FIRST_PCT
P1_STEPS = [45, 50, 62, 76, 83, 91, 100]   # 100 only when even 91 is not enough; 50 added (owner 2026-11-14)
P1_STEP_SLACK = 3.0
P1_STEP_DOWN_S = 30.0
P1_FLAG_CPU_C = 65.0          # instantaneous package temperature (a spike to 65 °C sets the flag)
P1_FLAG_W = 200.0
P1_FLAG_HOLD_S = 10 * 60.0
P1_FLAG_FLOOR = 62
P1_FANS_FIRST_PCT = 55

MIN_FW = (0, 9, 10)            # lnk heartbeat


def fw_ok(info, need=None):
    """True when the Mega's firmware is at least `need` (default MIN_FW: knows lnk). Demo always ok."""
    if info.get("demo"):
        return True
    m = re.search(r"fw (\d+)\.(\d+)\.(\d+)", info.get("fw") or "")
    return bool(m) and tuple(int(x) for x in m.groups()) >= (need or MIN_FW)


OC_FW = (0, 9, 11)            # firmware Mode 4 Overclocking (T2 held at 16 °C, band 15..17, by the Mega alone)
DAILY_FW = (0, 9, 23)         # firmware Mode 5 Daily (T2 17 °C, P2 40 %, outdoor fans <= 45 %)
# Last program chosen on the page (owner 2026-11-10): Overclocking is resumed at dashboard start if it was the last
# choice and the Mega came up in Mode 1; any other last choice keeps the normal start (Passive / firmware mode).
LAST_PROG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_program.txt")


def last_program():
    try:
        with open(LAST_PROG_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def remember_program(p):
    try:
        with open(LAST_PROG_FILE, "w", encoding="utf-8") as f:
            f.write(p)
    except OSError:
        pass


class Prog:
    def __init__(self, hub, demo=False):
        self.hub = hub
        self.demo = demo
        self.lock = threading.Lock()
        self.p = "passive"        # off | passive | manual | auto | bench. Default Passive (owner 2026-11-08)
        self.boot_check = True    # at start: Mega in a firmware Mode 2/3/4 -> leave it there (Off); last choice OC -> m4
        self.t_start = time.time()
        self.phase = "firmware"
        self.v2_floor = V2_BOT_COLD_PCT
        self.link_bad_since = None  # when the link went bad (LINK_GRACE_S)
        self.pas = {"rad": float(PAS_RAD_LO), "p1": float(P1_IDLE_PCT)}   # Passive: current (rate-limited) requests
        self.cpu_s, self.t_cpu = None, 0.0                                   # smoothed CPU package (see CPU_S_*)
        self.rad_eq = float(RAD_MIN_PCT)  # Auto / Benchmark: rad fans from the case-air limiter (case air <= room + 4)
        self.case_set = None
        self.eng = False            # Auto engaged by load from Passive
        self.eng_at = 0.0
        self.low_since = None
        self.heat_avg = None        # CPU + GPU W, EMA (ENG_AVG_S), for the P1 ramp
        self.eng_w = None           # same, but falls fast (ENG_FALL_S), for engaging / releasing Auto
        self.t_heat = 0.0
        self.p1_boost = 0.0         # % added to the P1 request (CPU temperature rule)
        self.p1_step = None         # P1 request snapped to P1_STEPS (None: no program request)
        self.p1_lower_since = None
        self.p1_flag_until = 0.0
        self.p1_flag_why = ""
        self.t_p1b = 0.0
        self.sweep = None           # P1 sweep running
        self.sweep_last = None      # finished / aborted sweep: {"res", "best", "why", "file"}
        self.reset_ctrl()
        self.cmd = {}             # last sent: fan, p2 (0 = off), v2 (% outdoor, None = parked)
        self.t_cmd = {}           # when each was last sent
        self.t_lnk = self.t_ctrl = self.t_log = self.t_mode = 0.0
        self.started = False      # program saw the Mega in mode 1 after it was selected
        self.req = None           # PC fan / P1 requests (None = defaults)
        self.note = ""
        self.logf = None
        self.logday = None
        self.logw = None

    # ---------- API (HTTP thread) ----------
    def select(self, p, eng=False):
        """eng=True: Passive engaging / releasing Auto by itself (load); False: a choice on the page."""
        if p == "extreme":
            return False, EXTREME_MSG
        if p in ("oc", "daily"):                        # hand over to the firmware's Mode 4 / 5 (works without Windows)
            if p == "daily" and not fw_ok(self.hub.info, DAILY_FW):
                return False, ("Daily mode lives in the Mega (FW 0.9.23 or newer: T2 17 °C, P2 40 %, outdoor fans <= 45 %). "
                               f"It reports: {self.hub.info.get('fw') or 'unknown'}.")
            if not fw_ok(self.hub.info, OC_FW):
                return False, ("Overclocking mode lives in the Mega (FW 0.9.11 or newer, so it keeps running in the BIOS or "
                               f"after a crash). It reports: {self.hub.info.get('fw') or 'unknown'}.")
            with self.lock:
                old = self.p
                self.p = "off"
                self.reset_ctrl()
                self.started = False
                self.cmd, self.t_cmd, self.req = {}, {}, None
                self.eng = False
            if old in ("auto", "bench", "manual"):
                self.send("x")
            if p == "daily":
                self.send("m5")
                self.hub.add_event("# DAILY: firmware Mode 5, T2 held at 17 °C (never under DP + 4), P2 40 %, outdoor fans <= 45 %; "
                                   "case fans off unless VRM / chipset / DIMM / SSD / CPU / GPU / case air need them")
            else:
                self.send("m4")
                self.hub.add_event("# OVERCLOCKING: firmware Mode 4, T2 held at 12.5 °C (12-13, never under DP + 4) by the Mega (survives BIOS / crashes)")
            remember_program(p)
            return True, "ok"
        if p not in ("off", "passive", "manual", "auto", "bench"):
            return False, "unknown program"
        if p in ("auto", "bench") and not fw_ok(self.hub.info):
            return False, ("The Mega needs FW 0.9.10 or newer (PC link heartbeat) before a program may drive it. "
                           f"It reports: {self.hub.info.get('fw') or 'unknown'}.")
        with self.lock:
            old = self.p
            self.p = p
            self.reset_ctrl()
            self.started = False
            self.cmd, self.t_cmd, self.req = {}, {}, None
            self.t_mode = 0.0
            self.eng = eng and p == "auto"
            self.eng_at = time.time()
            self.low_since = None
        if p in ("off", "passive"):
            if old not in ("off", "passive"):
                self.send("x")
            if old != "passive" or p != "off":
                self.send("m1")
            self.pas = {"rad": float(PAS_RAD_LO), "p1": float(P1_IDLE_PCT)}
        elif p == "manual":
            if old in ("auto", "bench"):
                self.send("x")          # park first; manual starts from a clean test session
        if not eng:
            self.hub.add_event(f"# PROGRAM {p.upper()} (dashboard)")
            remember_program(p)
        return True, "ok"

    def state(self):
        with self.lock:
            return {"p": self.p, "name": PROGRAMS.get(self.p, {}).get("name", self.p.capitalize()),
                    "pas": dict(self.pas),
                    "phase": self.phase, "u": round(self.u, 3), "active": self.active,
                    "target": PROGRAMS.get(self.p, {}).get("target"), "limit": PROGRAMS.get(self.p, {}).get("limit"),
                    "link": self.link_info(), "cmd": self.cmd, "req": self.req, "note": self.note,
                    "t2set": None if self.t2set is None else round(self.t2set, 2), "trim": round(self.trim, 2),
                    "case_set": None if self.case_set is None else round(self.case_set, 2), "rad_eq": round(self.rad_eq, 1),
                    "eng": self.eng_info(), "p1_boost": round(self.p1_boost, 1),
                    "p1_bios": P1_BY_BIOS,
                    "p1": {"step": self.p1_step, "flag": time.time() < self.p1_flag_until, "why": self.p1_flag_why,
                           "flag_s": round(max(0.0, self.p1_flag_until - time.time()))},
                    "sweep": self.sweep_info(), "sweep_last": self.sweep_last}

    def eng_info(self):
        """Load-engaged Auto, for the page: heat average, thresholds and the release countdown."""
        now = time.time()
        d = {"on": self.eng, "w": None if self.eng_w is None else round(self.eng_w),
             "on_w": ENG_ON_W, "off_w": ENG_OFF_W}
        if self.eng:
            hold = max(0.0, ENG_MIN_S - (now - self.eng_at))
            low = max(0.0, ENG_OFF_S - (now - self.low_since)) if self.low_since else None
            d.update(hold_s=round(hold), low_s=None if low is None else round(low))
        return d

    def p1_boost_update(self, pc, now):
        """P1 boost from the CPU package temperature and the load (see P1B_*)."""
        dt = min(2.0, now - self.t_p1b) if self.t_p1b else 0.0
        self.t_p1b = now
        v = pc.get("v") or {}
        fresh = pc.get("status") in ("ok", "demo") and now - pc.get("t", 0) < LINK_PC_S
        cpu_t = (v.get("cpu_t") or {}).get("v") if fresh else None
        if cpu_t is None:
            return                                           # no data: hold (Passive goes blind 60 % anyway)
        w = ((v.get("cpu_w") or {}).get("v") or 0) + ((v.get("gpu_w") or {}).get("v") or 0)
        load = min(1.0, max(0.0, (w - P1B_W_LO) / (P1B_W_HI - P1B_W_LO)))
        if cpu_t > P1B_ON_C:
            want = P1B_JUMP * load + (P1B_K_LO + (P1B_K_HI - P1B_K_LO) * load) * (cpu_t - P1B_ON_C)
        elif cpu_t >= P1B_HOLD_C:
            want = self.p1_boost                             # hysteresis band: hold
        else:
            want = 0.0
        if want > self.p1_boost:
            self.p1_boost = min(want, self.p1_boost + (P1B_UP_LO + P1B_UP_LOAD * load) * dt)
        else:
            self.p1_boost = max(want, self.p1_boost - P1B_DOWN * dt)
        self.p1_boost = min(100.0, self.p1_boost)

    # ---------- P1 sweep ----------
    def sweep_cmd(self, on):
        if not on:
            if self.sweep:
                self.sweep_end("stopped on the page")
            return True, "ok"
        if P1_BY_BIOS:
            return False, "P1 is run by the BIOS curve now (P1_BY_BIOS in hwinfo_out.py), so the sweep can't set it."
        if self.p != "bench" or not self.active:
            return False, "The P1 sweep runs in Benchmark while the branch is running (phase starting / regulating)."
        if self.sweep:
            return False, "A sweep is already running."
        _r, pc = self.latest()
        cpu_w = ((pc.get("v") or {}).get("cpu_w") or {}).get("v") or 0.0
        levels = SWEEP_LEVELS_HEAVY if cpu_w > SWEEP_CPU_HEAVY_W else SWEEP_LEVELS
        self.sweep = {"i": 0, "t0": time.time(), "acc": {}, "w": [], "res": [], "t_s": 0.0, "levels": levels,
                      "started": time.strftime("%Y%m%d_%H%M")}
        self.sweep_last = None
        n = len(levels)
        self.hub.add_event(f"# P1 SWEEP start: {', '.join(str(x) for x in levels)} %"
                           f"{' (heavy CPU load: starts at 65)' if levels is SWEEP_LEVELS_HEAVY else ''}, "
                           f"{SWEEP_DWELL_S / 60:.0f} min each (~{n * SWEEP_DWELL_S / 60:.0f} min). Keep the load steady.")
        return True, "ok"

    def sweep_info(self):
        sw = self.sweep
        if not sw:
            return None
        left = max(0.0, SWEEP_DWELL_S - (time.time() - sw["t0"]))
        lv = sw["levels"]
        return {"i": sw["i"], "n": len(lv), "level": lv[sw["i"]], "left_s": round(left),
                "total_left_s": round(left + SWEEP_DWELL_S * (len(lv) - sw["i"] - 1)), "res": sw["res"]}

    def sweep_step(self, r, pc, now):
        sw = self.sweep
        if self.p != "bench" or not self.active:
            return self.sweep_end("Benchmark stopped regulating (program change, branch off or link)")
        v = {k: x["v"] for k, x in (pc.get("v") or {}).items()} if pc.get("status") in ("ok", "demo") else {}
        fresh = bool(v) and now - pc.get("t", 0) < LINK_PC_S
        cpu_t, hot = (v.get("cpu_t") if fresh else None), (v.get("gpu_hot") if fresh else None)
        if (cpu_t or 0) > SWEEP_CPU_MAX_C or (hot or 0) > SWEEP_HOT_MAX_C:
            return self.sweep_end(f"too hot at P1 {sw['levels'][sw['i']]} % (CPU {cpu_t or 0:.0f} °C, "
                                  f"GPU hot spot {hot or 0:.0f} °C)")
        el = now - sw["t0"]
        if el >= SWEEP_DWELL_S - SWEEP_AVG_S and fresh and r is not None and now - sw["t_s"] >= 0.9:
            sw["t_s"] = now
            vals = {"p1_rpm": v.get("w_pump"), "cpu_t": v.get("cpu_t"), "gpu_hot": v.get("gpu_hot"),
                    "gpu_t": v.get("gpu_t"), "cube": v.get("t_sensor"), "T1": r.get("T1"), "T2": r.get("T2"),
                    "cpu_w": v.get("cpu_w"), "gpu_w": v.get("gpu_w")}
            for k, x in vals.items():
                if x is not None:
                    a = sw["acc"].setdefault(k, [0.0, 0])
                    a[0] += x
                    a[1] += 1
            sw["w"].append((vals["cpu_w"] or 0) + (vals["gpu_w"] or 0))
        if el < SWEEP_DWELL_S:
            return
        lvl = sw["levels"][sw["i"]]
        m = {k: round(a[0] / a[1], 2) for k, a in sw["acc"].items() if a[1]}
        ws = sw["w"]
        wavg = sum(ws) / len(ws) if ws else None
        row = {"p1": lvl, **m, "w": None if wavg is None else round(wavg),
               "w_sd": round((sum((x - wavg) ** 2 for x in ws) / len(ws)) ** 0.5) if ws else None, "n": len(ws)}
        if m.get("cpu_t") is not None and m.get("cube") is not None:
            row["cpu_cube"] = round(m["cpu_t"] - m["cube"], 2)
        if m.get("gpu_hot") is not None and m.get("cube") is not None:
            row["hot_cube"] = round(m["gpu_hot"] - m["cube"], 2)
        sw["res"].append(row)
        f = lambda k, fmt: format(row[k], fmt) if row.get(k) is not None else "--"
        self.hub.add_event(f"# P1 SWEEP {lvl} %: P1 {f('p1_rpm', '.0f')} rpm · CPU {f('cpu_t', '.1f')} °C "
                           f"(cube +{f('cpu_cube', '.1f')}) · GPU hot {f('gpu_hot', '.1f')} (+{f('hot_cube', '.1f')}) · "
                           f"cube {f('cube', '.1f')} · {f('w', 'd')} W ±{f('w_sd', 'd')}")
        sw["i"] += 1
        sw["t0"], sw["acc"], sw["w"] = now, {}, []
        if sw["i"] >= len(sw["levels"]):
            self.sweep_end(None)

    def sweep_end(self, why):
        sw, self.sweep = self.sweep, None
        if sw is None:
            return
        res = sw["res"]
        best = sweep_knee(res)
        path = None
        if res:
            d = __import__("pcs_paths").LOGS
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"p1sweep_{sw['started']}{'_demo' if self.demo else ''}.csv")
            cols = ["p1", "p1_rpm", "cpu_t", "cpu_cube", "gpu_hot", "hot_cube", "gpu_t", "cube", "T1", "T2",
                    "cpu_w", "gpu_w", "w", "w_sd", "n"]
            with open(path, "w", newline="", encoding="utf-8") as fh:
                cw = csv.writer(fh)
                cw.writerow(cols)
                for row in res:
                    cw.writerow([row.get(c) for c in cols])
        self.sweep_last = {"res": res, "best": best, "why": why, "file": path and os.path.basename(path)}
        if why:
            self.hub.add_event(f"# P1 SWEEP aborted: {why} ({len(res)} of {len(sw["levels"])} steps done)")
        else:
            self.hub.add_event(f"# P1 SWEEP done: sweet spot ~{best} % (less than {SWEEP_KNEE_C} °C per +10 % above it)"
                               if best else "# P1 SWEEP done: no clear knee, see the table")
        if path:
            self.hub.add_event(f"# P1 SWEEP results: logs/{os.path.basename(path)}")

    def p1_raw(self):
        """The program's P1 request plus the boost (before the quiet steps); None without a program request."""
        if not self.req or self.req.get("req_p1") is None:
            return None
        return min(100.0, self.req["req_p1"] + (self.p1_boost if self.p1_boost > 0.5 else 0.0))

    def p1_step_update(self, pc, now):
        """Quiet P1 steps with the 65 °C / 200 W flag and fans first (see P1_STEPS)."""
        v = pc.get("v") or {}
        fresh = pc.get("status") in ("ok", "demo") and now - pc.get("t", 0) < LINK_PC_S
        cpu_t = (v.get("cpu_t") or {}).get("v") if fresh else None
        why = []
        if cpu_t is not None and cpu_t >= P1_FLAG_CPU_C:
            why.append(f"CPU {cpu_t:.0f} °C")
        elif self.cpu_s is not None and self.cpu_s >= P1_FLAG_CPU_S_C:
            why.append(f"CPU {self.cpu_s:.0f} °C (avg)")
        if self.heat_avg is not None and self.heat_avg > P1_FLAG_W:
            why.append(f"{self.heat_avg:.0f} W")
        if why:
            self.p1_flag_until = now + P1_FLAG_HOLD_S
            self.p1_flag_why = " · ".join(why)
        flag = now < self.p1_flag_until
        raw = self.p1_raw()
        if raw is None:
            self.p1_step, self.p1_lower_since = None, None
            return
        rad = (self.req or {}).get("req_rad") or 0
        want = raw if (flag or rad >= P1_FANS_FIRST_PCT) else P1_STEPS[0]   # fans first
        target = next((s for s in P1_STEPS if s >= want - P1_STEP_SLACK), P1_STEPS[-1])
        if flag:
            target = max(target, P1_FLAG_FLOOR)
        cur = self.p1_step
        if cur is None or target >= cur:
            self.p1_step, self.p1_lower_since = target, None
        else:
            self.p1_lower_since = self.p1_lower_since or now
            if now - self.p1_lower_since >= P1_STEP_DOWN_S:
                self.p1_step, self.p1_lower_since = target, None

    def fast_params(self):
        """Passive / Off (Mode 1): the rad rule's curve for the reader's instant floor (tools/pc_sensors FanDriver,
        owner 2026-11-22: ramp instantly). None in every other program / firmware mode (they keep their own fans)."""
        if not (self.phase or "").startswith(("passive (branch off)", "off (passive")):
            return None
        import fast_rules, sys
        return fast_rules.passive(sys.modules[__name__], self.pas.get("liquid"))

    def requests(self):
        """PC fan / P1 requests for FanControl: the program's own, P1 on the quiet steps (or the sweep level)."""
        if self.p == "off" and not self.req:
            return None                                      # Off in a firmware mode: that mode's own fan / P1 rules
        req = dict(self.req) if self.req else {}
        if self.sweep:
            req["req_p1"] = self.sweep["levels"][self.sweep["i"]]
            return req
        if self.p1_step is not None:
            req["req_p1"] = self.p1_step
        return req or None

    def cpu_update(self, pc, now):
        """Smoothed CPU package temperature: slow up (a short spike barely moves it), faster down."""
        v = pc.get("v") or {}
        fresh = pc.get("status") in ("ok", "demo") and now - pc.get("t", 0) < LINK_PC_S
        t = (v.get("cpu_t") or {}).get("v") if fresh else None
        if t is None:
            self.cpu_s = None
            return
        dt = min(5.0, now - self.t_cpu) if self.t_cpu else 0.0
        self.t_cpu = now
        if self.cpu_s is None:
            self.cpu_s = t
        else:
            tau = CPU_S_UP_S if t > self.cpu_s else CPU_S_DOWN_S
            self.cpu_s += min(1.0, dt / tau) * (t - self.cpu_s)

    def heat_update(self, pc, now):
        """EMA of CPU + GPU W; None while HWiNFO is missing / stale."""
        v = pc.get("v") or {}
        fresh = pc.get("status") in ("ok", "demo") and now - pc.get("t", 0) < LINK_PC_S
        if not fresh or (v.get("cpu_w") is None and v.get("gpu_w") is None):
            self.heat_avg = self.eng_w = None
            return None
        w = ((v.get("cpu_w") or {}).get("v") or 0) + ((v.get("gpu_w") or {}).get("v") or 0)
        dt = min(5.0, now - self.t_heat) if self.t_heat else 0.0
        self.t_heat = now
        a = dt / ENG_AVG_S
        self.heat_avg = w if self.heat_avg is None else self.heat_avg + min(1.0, a) * (w - self.heat_avg)
        b = dt / (ENG_AVG_S if (self.eng_w is None or w > self.eng_w) else ENG_FALL_S)
        self.eng_w = w if self.eng_w is None else self.eng_w + min(1.0, b) * (w - self.eng_w)
        return self.eng_w

    # ---------- helpers ----------
    def send(self, c):
        self.hub.tx.put(c)
        if c != "lnk":
            self.hub.add_event(f"> {c} (program)")

    def latest(self):
        with self.hub.lock:
            r = self.hub.rows[-1] if self.hub.rows else None
            pc = dict(self.hub.pc)
        if r is not None and r.get("hist"):        # history reloaded from the log at start: not live Mega data
            r = None
        return r, pc

    def link_info(self):
        r, pc = self.latest()
        now = time.time()
        row_ok = r is not None and now - r["t"] < LINK_ROW_S
        ts = (pc.get("v") or {}).get("t_sensor")
        pc_ok = pc.get("status") in ("ok", "demo") and ts is not None and now - pc.get("t", 0) < LINK_PC_S
        dp_ok = r is not None and r.get("DP") is not None
        why = [] if row_ok else ["no Mega data"]
        if not pc_ok:
            why.append("no cube (HWiNFO)")
        if row_ok and not dp_ok:
            why.append("no dew point")
        return {"ok": row_ok and pc_ok and dp_ok, "why": ", ".join(why),
                "cube": ts["v"] if ts else None}

    def put(self, key, cmd, val, now, min_gap=0.0, tol=0):
        """Send a set-point command when it changed by more than tol (and not more often than min_gap)."""
        old = self.cmd.get(key, "unset")
        if old != "unset" and (old == val or (old is not None and val is not None and abs(old - val) <= tol)):
            return
        if now - self.t_cmd.get(key, 0) < min_gap and old != "unset":
            return
        self.send(cmd)
        self.cmd[key] = val
        self.t_cmd[key] = now

    # ---------- loop ----------
    def run(self, stop):
        while not stop.is_set():
            try:
                self.step(time.time())
            except Exception as e:          # never let the program thread die silently
                self.hub.add_event(f"[program error: {e}]")
            stop.wait(0.5)
        if self.p in ("auto", "bench", "manual"):
            self.send("x")
        if self.logf:
            self.logf.close()

    def step(self, now):
        r, pc = self.latest()
        lk = self.link_info()
        with self.lock:
            p = self.p
        heat = self.heat_update(pc, now)
        self.cpu_update(pc, now)
        self.p1_boost_update(pc, now)
        self.p1_step_update(pc, now)
        if p != "auto" and self.eng:                         # case button / link loss handed it elsewhere
            self.eng = False
        if r is not None and now - self.t_log >= LOG_S and now - (r.get("t") or 0) < LINK_ROW_S:   # no stale repeats
            self.t_log = now
            self.log(r, pc, lk)
        if now - getattr(self, "t_corelog", 0) >= LOG_S:
            self.t_corelog = now
            try:
                self.core_log(pc)
            except Exception:
                pass                                         # the per-core log must never stop the controller
        if self.sweep:
            self.sweep_step(r, pc, now)
        if p == "passive" and self.boot_check and r is not None:
            lp = last_program()
            if r["mode"] == 1 and lp in ("oc", "daily"):    # resume Overclocking / Daily, the last choice on the page
                if not self.hub.info.get("fw") and now - self.t_start < 15:
                    return                                   # wait for the firmware banner first
                self.boot_check = False
                ok, msg = self.select(lp)
                name = "OVERCLOCKING" if lp == "oc" else "DAILY"
                self.hub.add_event(f"# {name} resumed at start (last choice)" if ok else f"# {name} not resumed: {msg}")
                return
            self.boot_check = False
            if r["mode"] != 1:                               # don't override a case-button mode at dashboard start
                with self.lock:
                    self.p = p = "off"
                self.hub.add_event(f"# PROGRAM OFF at start: the Mega is in firmware Mode {r['mode']} (case button)")
            else:
                self.hub.add_event("# PROGRAM PASSIVE (default at start)")
        if p == "passive":
            if now - self.t_ctrl >= PAS_PERIOD_S:
                self.t_ctrl = now
                self.passive(r, pc)
                if (LOAD_ENGAGE and heat is not None and heat > ENG_ON_W and self.started and r is not None and r["mode"] == 1
                        and fw_ok(self.hub.info) and lk["ok"]):
                    self.hub.add_event(f"# PROGRAM AUTO engaged by load: CPU+GPU {heat:.0f} W average > "
                                       f"{ENG_ON_W:.0f} W (from Passive, at least {ENG_MIN_S / 60:.0f} min)")
                    self.select("auto", eng=True)
                    return
            if r is not None and r["mode"] == 1:
                self.started = True                          # m1 has landed; from now on a mode change is a button
            elif r is not None and self.started:            # case button: firmware mode takes over
                with self.lock:
                    self.p = "off"
                self.phase, self.req = "firmware", None
                self.hub.add_event("# PROGRAM OFF: case button pressed, firmware mode takes over")
            return
        if p == "off" and r is not None and r["mode"] == 1:
            # owner 2026-11-15: Off = Passive's case fans / P1 (branch off in Mode 1), but never engages Auto by load.
            # In a firmware mode (2-5) Off leaves the fans to that mode's own rules (req None).
            if now - self.t_ctrl >= PAS_PERIOD_S:
                self.t_ctrl = now
                self.passive(r, pc)
                self.phase = "off (passive fans, no Auto)"
            return
        if p == "off" or r is None:
            self.phase = "firmware" if p == "off" else "waiting for the Mega"
            self.req = None
            return
        # a case button (Mode 2/3) after the program took over hands control back to the firmware
        if r["mode"] != 1 and self.started:
            with self.lock:
                self.p = "off"
            self.phase, self.req = "firmware", None
            self.hub.add_event("# PROGRAM OFF: case button pressed, firmware mode takes over")
            return
        test = r["state"] == "TEST"
        if test and now - self.t_lnk >= LNK_S and fw_ok(self.hub.info):     # heartbeat (manual too: if the dashboard dies the Mega parks)
            self.t_lnk = now
            self.send("lnk")
        if p == "manual":
            self.req = None
            self.phase = "manual" if test else "entering manual"
            self.enter_test(r, now)
            return
        if self.eng:                                         # load-engaged Auto: back to Passive once quiet
            low = heat is None or heat < ENG_OFF_W
            self.low_since = (self.low_since or now) if low else None
            if now - self.eng_at >= ENG_MIN_S and self.low_since and now - self.low_since >= ENG_OFF_S:
                self.hub.add_event(f"# PROGRAM PASSIVE again: CPU+GPU under {ENG_OFF_W:.0f} W for "
                                   f"{ENG_OFF_S / 60:.0f} min")
                self.select("passive", eng=True)
                return
        prog = PROGRAMS[p]
        if lk["ok"]:
            self.link_bad_since = None
        if not lk["ok"]:
            self.link_bad_since = self.link_bad_since or now
            if test and self.active and now - self.link_bad_since < LINK_GRACE_S:
                self.phase = f"link hiccup ({lk['why']}), holding {LINK_GRACE_S - (now - self.link_bad_since):.0f} s"
                return
            if test:
                self.send("x")
            self.phase, self.cmd = f"waiting for link ({lk['why']})", {}
            self.reset_ctrl()
            self.req = None
            return
        if not test:
            self.phase = "entering test mode"
            self.cmd = {}
            self.reset_ctrl()
            self.enter_test(r, now)
            return
        if now - self.t_ctrl < CTRL_S:
            return
        dt = now - self.t_ctrl if self.t_ctrl else CTRL_S
        self.t_ctrl = now
        w = (pc.get("v") or {}).get("w_pump")
        self.control(r, lk["cube"], prog, min(dt, 3 * CTRL_S), now, w["v"] if w else None)

    def passive(self, r, pc):
        """Passive: branch off, PC rad fans / top / P1 follow the loop temperature and the CPU / GPU load."""
        v = {k: x["v"] for k, x in (pc.get("v") or {}).items()} if pc.get("status") in ("ok", "demo") else {}
        fresh = v and time.time() - pc.get("t", 0) < LINK_PC_S
        if not fresh:
            self.req = {"req_rad": PAS_BLIND_PCT, "req_top": PAS_BLIND_PCT * 0.7, "req_p1": PAS_BLIND_PCT}
            self.phase, self.note = "passive (no HWiNFO: fans / P1 at 60 %)", ""
            return
        cube, t1 = v.get("t_sensor"), r.get("T1") if r else None
        liquid = max(x for x in (cube, t1) if x is not None) if (cube is not None or t1 is not None) else None

        def ramp(x, lo, hi):
            return PAS_RAD_LO + (100 - PAS_RAD_LO) * min(1.0, max(0.0, (x - lo) / (hi - lo)))
        if USE_ROOM_SHT:
            airs = [x for x in ((r.get("Troom"), r.get("T_A"), r.get("T_B")) if r else ()) if x is not None]
            troom = min(airs) if airs else 23.0              # a room reading above the case air is warm air, not room
            dl = (liquid - troom) if liquid is not None else PAS_D_HI
            rad = ramp(dl, PAS_D_LO, PAS_D_HI)
        else:                                                # room SHT not trusted (2026-11-15): absolute water temp
            dl = (liquid - ROOM_FIXED_C) if liquid is not None else PAS_D_HI
            rad = PAS_RAD_LO                                  # the water roof below replaces the 26 -> 34 °C ramp
        why = "base"
        now_ = time.time()
        dt = min(5.0, now_ - self.pas["t"]) if self.pas.get("t") else PAS_PERIOD_S
        self.pas["t"] = now_
        self.pas["liquid"] = liquid
        # water roof (owner 2026-11-22): over 28 °C the rads cool the water; with no load they rise slowly, with load
        # at once (the reader's fast path runs the same rule, fast_rules.py)
        if not USE_ROOM_SHT:
            import fast_rules as fr
            wt = 0.0 if liquid is None or liquid <= fr.WATER_ROOF_C else ramp(liquid, fr.WATER_ROOF_C, fr.WATER_FULL_C)
            heat_now = (v.get("cpu_w") or 0) + (v.get("gpu_w") or 0)
            st = self.pas.get("water", 0.0)
            if wt <= st or heat_now >= fr.WATER_INSTANT_W:
                st = wt
            else:
                st = min(wt, max(st, PAS_RAD_LO) + fr.WATER_SLOW_PCT_S * dt)
            self.pas["water"] = st
            if st > rad:
                rad, why = st, "water"
        ct = v.get("cpu_t")
        pc_ = self.pas.get("cpu")
        if ct is not None:
            tau_c = PAS_CPU_UP_S if (pc_ is None or ct > pc_) else PAS_CPU_DOWN_S
            pc_ = ct if (pc_ is None or tau_c <= 0) else pc_ + min(1.0, dt / tau_c) * (ct - pc_)
            self.pas["cpu"] = pc_
        cs = pc_ if pc_ is not None else self.cpu_s           # fast CPU average for the rad rule
        heat = (v.get("cpu_w") or 0) + (v.get("gpu_w") or 0)
        pw = self.pas.get("w")
        tau = PAS_W_UP_S if (pw is None or heat > pw) else PAS_W_DOWN_S
        pw = heat if (pw is None or tau <= 0) else pw + min(1.0, dt / tau) * (heat - pw)
        self.pas["w"] = pw
        rules = [("CPU", ramp(cs, PAS_CPU_LO, PAS_CPU_HI) if cs is not None else 0.0),   # the CPU itself (2026-11-11)
                 ("load", ramp(pw, PAS_W_LO, PAS_W_HI)),
                 ("GPU", ramp(v.get("gpu_hot") or 0, PAS_GPU_LO, PAS_GPU_HI))]
        comp = max(v.get("cpu_t") or 0, v.get("gpu_hot") or 0)
        if comp > PAS_COMP_C:
            rules.append(("hot", min(100.0, 60 + PAS_COMP_PCT_PER_C * (comp - PAS_COMP_C))))
        for name, x in rules:
            if x > rad:
                rad, why = x, name
        p1 = p1_load(self.heat_avg, P1_IDLE_PCT)
        if dl >= PAS_D_HI:
            p1 = max(p1, P1_LOAD_HI)
        if self.cpu_s is not None:                           # P1 keeps the shared (slower) CPU average
            fp = min(1.0, max(0.0, (self.cpu_s - PAS_P1_CPU_LO) / (PAS_P1_CPU_HI - PAS_P1_CPU_LO)))
            p1 = max(p1, P1_LOAD_LO + (P1_STEPS[-2] - P1_LOAD_LO) * fp)
        for k, target in (("rad", rad), ("p1", p1)):           # fast up, slow down
            cur = self.pas[k]
            up, down = PAS_UP * dt / CTRL_S, PAS_DOWN * dt / CTRL_S     # rates are per CTRL_S, the rule runs every second
            self.pas[k] = min(target, cur + up) if target > cur else max(target, cur - down)
        # top exhaust: 0.7 x rad, but at most 15 % under the rads (both rads are intakes: at 100 % they push warm air in)
        top = max(30.0, self.pas["rad"] * 0.7, self.pas["rad"] - 15.0)
        self.req = {"req_rad": round(self.pas["rad"]), "req_top": round(top),
                    "req_p1": round(self.pas["p1"])}
        self.phase = "passive (branch off)"
        self.note = (f"CPU {cs if cs is not None else float('nan'):.0f} °C avg · "
                     f"loop {liquid if liquid is not None else float('nan'):.1f} °C · "
                     f"CPU+GPU {heat:.0f} W (avg {pw:.0f}) · rad {self.pas['rad']:.0f} % ({why}) · "
                     f"P1 {self.pas['p1']:.0f} %")

    def v2_bottom(self, t3, dp):
        """Lowest useful outdoor share for this outdoor water temperature."""
        if t3 is None:
            return V2_BOT_COLD_PCT
        f = min(1.0, max(0.0, (t3 - dp) / V2_BOT_SPAN_C))
        return V2_BOT_COLD_PCT + (V2_MIN_PCT - V2_BOT_COLD_PCT) * f

    def put_v2(self, v2, now):
        """V2 target: opening limited to V2_STEP_PCT per V2_MIN_GAP_S, closing straight away."""
        old = self.cmd.get("v2")
        if old is not None and v2 > old:
            if now - self.t_cmd.get("v2", 0) < V2_MIN_GAP_S:
                return
            v2 = min(v2, old + V2_STEP_PCT)
        elif old is None and self.cmd.get("v2", "unset") is None:
            v2 = min(v2, self.v2_floor)              # opening from parked: start at the bottom
        self.put("v2", f"o{round(v2)}%", round(v2), now, tol=1 if (old is not None and v2 < old) else 2)

    def enter_test(self, r, now):
        if now - self.t_mode < 5:
            return
        if r["mode"] != 1:
            self.t_mode = now
            self.send("m1")
        elif r["state"] == "IDLE" and not r.get("faults"):
            self.t_mode = now
            self.started = True
            self.send("t")
        elif r["mode"] == 1:
            self.started = True

    def reset_ctrl(self):
        self.active = False
        self.fan = float(FAN_MIN_PCT)
        self.trim = 0.0
        self.e2_prev = None
        self.e3_prev = None
        self.fan_pi = float(FAN_MIN_PCT)
        self.fan_cap = float(FAN_MAX_PCT)
        self.v2_goal = float(V2_OP_PCT)
        self.p2_set = P2_RUN_PCT
        self.start_at = None
        self.cold_since = self.hot_since = self.restore_since = self.off_since = None
        self.t_fast = 0.0
        self.t2set = None
        self.u = 0.0

    def branch_off(self, why):
        if self.active or self.cmd.get("p2") not in (None, 0):
            self.send("p0")
            self.send("k")
            self.cmd.update(p2=0, v2=None)
        self.hub.add_event(f"# PROGRAM branch off: {why}")
        self.reset_ctrl()

    def control(self, r, cube, prog, dt, now, p1_rpm=None):
        dp, t3, t2, t1, troom = r["DP"], r["T3"], r["T2"], r.get("T1"), r.get("Troom")
        if not USE_ROOM_SHT:
            troom = ROOM_FIXED_C                             # room SHT not trusted (2026-11-15)
        self.v2_floor = self.v2_bottom(t3, dp)
        # safety: the colder of cube and T2 -- but only while P2 runs. With P2 off T2 is standing water in the box and
        # says nothing about the PC (2026-11-08: T2 19 °C stale vs cube 32 °C kept Auto in standby under Heaven).
        flowing = bool(r.get("pen"))
        margin = min(cube, t2) - dp if (t2 is not None and flowing) else cube - dp
        target, limit = prog["target"], prog["limit"]
        dpc = control_dp(r)
        goal = max(dpc + target, dp + limit + LIMIT_PAD_C)   # cube goal (°C); limits below still use the full DP
        cube_e = cube - goal                                  # > 0: cube warmer than its goal
        cube_m = cube - dp
        above = (t1 - troom) if t1 is not None and troom is not None else 0.0
        rad = min(RAD_MAX_PCT, max(RAD_MIN_PCT, RAD_MIN_PCT + RAD_PCT_PER_C * above))
        case_air = max((x for x in (r.get("T_A"), r.get("T_B")) if x is not None), default=None)
        if case_air is not None and t1 is not None and troom is not None:
            self.case_set = troom + CASE_ABOVE_ROOM_C
            e_case = case_air - self.case_set
            d = max(-RAD_EQ_STEP, min(RAD_EQ_STEP, RAD_EQ_KI * e_case * dt))
            self.rad_eq = min(float(RAD_EQ_MAX_PCT), max(float(RAD_MIN_PCT), self.rad_eq + d))
            rad = max(rad, self.rad_eq)
        rad = round(rad)
        p1 = prog.get("p1") or round(p1_load(self.heat_avg, P1_RUN_PCT))   # Benchmark fixed, Auto by load
        self.req = {"req_rad": rad, "req_top": max(30.0, rad * 0.7), "req_p1": p1}

        # ---- limits
        if margin < limit - EMERG_OFF_C:
            self.req = {"req_rad": RAD_EMERG_PCT, "req_top": RAD_EMERG_PCT * 0.7, "req_p1": p1}
            if self.active:
                self.branch_off(f"margin {margin:+.1f} °C under limit {limit:+.0f}")
            self.phase = "emergency: branch off"
            self.note = f"margin {margin:+.1f} °C"
            self.put("fan", f"f{FAN_MIN_PCT}", FAN_MIN_PCT, now)
            return
        if margin < limit and self.active:
            self.req = {"req_rad": RAD_EMERG_PCT, "req_top": RAD_EMERG_PCT * 0.7, "req_p1": p1}
            self.fan = self.fan_pi = float(FAN_MIN_PCT)
            self.e2_prev = None
            if now - self.t_fast >= 10:
                self.t_fast = now
                self.v2_goal = max(self.v2_floor, self.v2_goal - 2 * V2_STEP_PCT)
            self.put("fan", f"f{FAN_MIN_PCT}", FAN_MIN_PCT, now)
            self.put_v2(self.v2_goal, now)
            self.phase = "near limit: fans min, V2 closing, PC rad fans up"
            self.note = f"margin {margin:+.1f} °C, limit {limit:+.0f}"
            return

        # ---- start / standby
        if not self.active:
            start_m = (min(cube, t2) if (t2 is not None and flowing) else cube) - goal
            if self.eng and (self.eng_w is None or self.eng_w < ENG_OFF_W):   # load-engaged Auto, load gone: no late start
                self.phase = "standby (load gone, back to Passive soon)"
                self.note = f"CPU+GPU {self.eng_w or 0:.0f} W average < {ENG_OFF_W:.0f} W: the branch stays off"
                self.put("fan", f"f{FAN_MIN_PCT}", FAN_MIN_PCT, now)
                return
            if start_m < START_E_C:
                self.phase = "standby (warm enough)"
                self.note = f"cube {cube:.1f} → starts at {goal + START_E_C:.1f} °C (DP {dp:.1f}, control DP {dpc:.1f})"
                self.put("fan", f"f{FAN_MIN_PCT}", FAN_MIN_PCT, now)
                return
            self.reset_ctrl()
            self.active, self.start_at = True, now
            self.cmd["v2"] = None                        # parked: put_v2 starts at the bottom

        # ---- slow trim of the T2 setpoint from the cube
        if now - self.start_at >= START_SETTLE_S:
            self.trim = max(-TRIM_MAX_C, min(TRIM_MAX_C, self.trim - K_TRIM * cube_e * dt))
        self.t2set = goal + self.trim
        e2 = t2 - self.t2set                              # > 0: too warm, more fan

        # ---- fast loop: outdoor fans hold T2, capped by a smooth T3-floor loop
        fan_max = prog.get("fan_max", FAN_MAX_PCT)               # Auto 70 % (noise), Benchmark 80 %
        v2_max = prog.get("v2_max", V2_OP_PCT)                    # how far V2 may open when the fans run out
        lim = lambda x: min(float(fan_max), max(float(FAN_MIN_PCT), x))
        if self.e2_prev is not None:
            d = KF_P * (e2 - self.e2_prev) + KF_I * e2 * dt
            self.fan_pi = lim(self.fan_pi + max(-FAN_STEP_MAX, min(FAN_STEP_MAX, d)))
        self.e2_prev = e2
        if t3 is not None:
            e3 = t3 - (dp + T3_FLOOR_C)                       # < 0: outdoor water too cold, the cap comes down
            if self.e3_prev is not None:
                d = KC_P * (e3 - self.e3_prev) + KC_I * e3 * dt
                self.fan_cap = lim(self.fan_cap + max(-FAN_STEP_MAX, min(FAN_STEP_MAX, d)))
            self.e3_prev = e3
        blocked = self.fan_cap < self.fan_pi - 0.5                # T3 floor is what limits the fans
        self.fan_pi = min(self.fan_pi, self.fan_cap + 5.0)        # anti-windup against the cap
        self.fan = min(self.fan_pi, self.fan_cap)
        fan_lo = self.fan <= FAN_MIN_PCT + 0.5                   # fans can't take any cooling out
        if e2 < -0.5:
            self.v2_goal = min(self.v2_goal, max(self.v2_floor, (self.cmd.get("v2") or self.v2_floor)))  # stop opening
        fan_hi = self.fan >= fan_max - 0.5 or blocked             # out of fan authority: at the cap or held by T3

        # ---- inside trims (only when the fans run out of authority)
        if fan_lo and e2 < -FAST_COLD_C and now - self.t_fast >= 10:     # cold slug / very cold outside
            self.t_fast = now
            self.v2_goal = max(self.v2_floor, self.v2_goal - V2_STEP_PCT)
        cold = fan_lo and e2 < -0.5
        hot = fan_hi and e2 > 0.5
        self.cold_since = (self.cold_since or now) if cold else None
        self.hot_since = (self.hot_since or now) if hot else None
        self.restore_since = (self.restore_since or now) if (self.fan >= FAN_RESTORE_PCT and not hot) else None
        if self.cold_since and now - self.cold_since >= SAT_S:
            self.cold_since = now
            if self.v2_goal > self.v2_floor + 0.5:
                self.v2_goal = max(self.v2_floor, self.v2_goal - V2_STEP_PCT)
            else:
                self.p2_set = max(P2_MIN_PCT, self.p2_set - P2_STEP_PCT)
        hot_wait = 30.0 if e2 > 2.0 else SAT_S                   # far too warm: open V2 every 30 s (its own rate limit)
        if self.hot_since and now - self.hot_since >= hot_wait:
            self.hot_since = now
            if self.v2_goal < v2_max:                             # V2 first (Benchmark: up to all ports open)
                self.v2_goal = min(v2_max, self.v2_goal + V2_STEP_PCT)
            else:
                self.p2_set = min(P2_MAX_PCT, self.p2_set + P2_STEP_PCT)
        if self.restore_since and now - self.restore_since >= SAT_S:
            self.restore_since = now
            if self.v2_goal < V2_OP_PCT:
                self.v2_goal = min(V2_OP_PCT, self.v2_goal + V2_STEP_PCT)
            elif self.p2_set < P2_RUN_PCT:
                self.p2_set = min(P2_RUN_PCT, self.p2_set + P2_STEP_PCT)

        # ---- branch off when even the minimum is too much
        floor = cold and self.v2_goal <= self.v2_floor + 0.5 and self.p2_set <= P2_MIN_PCT
        self.off_since = (self.off_since or now) if floor else None
        if self.off_since and now - self.off_since >= OFF_HOLD_S:
            self.branch_off("everything at minimum and still too cold")
            self.phase = "standby (too cold outside)"
            return

        self.put("p2", f"pd{self.p2_set}", self.p2_set, now)
        self.put_v2(self.v2_goal, now)
        self.put("fan", f"f{round(self.fan)}", round(self.fan), now, tol=1)
        self.u = (self.fan - FAN_MIN_PCT) / (fan_max - FAN_MIN_PCT)
        start = now - self.start_at < START_SETTLE_S
        self.phase = "starting" if start else ("regulating" + (" · fans held (T3 near DP)" if blocked else ""))
        self.note = (f"T2 {t2:.1f} → {self.t2set:.1f} °C (trim {self.trim:+.1f}) · cube {cube_m:+.1f} over dew"
                     f"{f' (control DP {dpc:.1f})' if dpc < dp - 0.2 else ''} · "
                     f"fans {self.fan:.0f} % · V2 {self.v2_goal:.0f} % · P2 {self.p2_set} % · P1 {p1:.0f} %")

    # ---------- 1 s log (always, also with no program) ----------
    def log(self, r, pc, lk):
        day = time.strftime("%Y%m%d")
        if day != self.logday:
            if self.logf:
                self.logf.close()
            d = __import__("pcs_paths").LOGS
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"pcs2_{day}{'_demo' if self.demo else ''}.csv")   # pcs2: chiller scheme columns
            new = not os.path.exists(path)
            if not new:                          # columns changed (2026-11-11 core stats): keep the old part aside
                with open(path, encoding="utf-8") as f:
                    head = f.readline().strip().split(",")
                if head != LOG_HEAD:
                    n = 1
                    while os.path.exists(path[:-4] + f"_part{n}.csv"):
                        n += 1
                    os.replace(path, path[:-4] + f"_part{n}.csv")
                    new = True
            self.logf = open(path, "a", newline="", encoding="utf-8")
            self.logw = csv.writer(self.logf)
            self.logday = day
            if new:
                self.logw.writerow(LOG_HEAD)
        v = {k: x["v"] for k, x in (pc.get("v") or {}).items()} if pc.get("status") in ("ok", "demo") else {}
        cube = v.get("t_sensor")
        margin = cube - r["DP"] if cube is not None and r.get("DP") is not None else None
        prog = PROGRAMS.get(self.p, {})
        req = self.requests() or {}
        self.logw.writerow([time.strftime("%H:%M:%S")] +
                           [hexs(r.get(k)) if k in ("fuses", "faults", "alarms") else r.get(k) for k in MEGA] +
                           [v.get(k) for k in PC] +
                           [self.p, self.phase, int(lk["ok"]), None if margin is None else round(margin, 2),
                            prog.get("target"), round(self.u, 3), self.cmd.get("fan"), self.cmd.get("p2"),
                            self.cmd.get("v2"), req.get("req_rad"), req.get("req_top"), None if P1_BY_BIOS else req.get("req_p1"),
                            None if self.t2set is None else round(self.t2set, 2), round(self.trim, 2),
                            round(self.fan, 1)] + [core_stats(pc)[k] for k in CORE_COLS])
        self.logf.flush()

    # ---------- per-core log (owner 2026-11-19: which bin was cluster 4 at when the WHEA 19s came?) ----------
    def core_log(self, pc):
        """logs/cores_YYYYMMDD.csv, 1 s: clock / effective clock (MHz), VID (V) and load (%) of all 24 cores.
        Independent of the Mega link; match the rows with the WHEA times in the Event Log."""
        if pc.get("status") != "ok" or time.time() - pc.get("t", 0) > 3:
            return
        core = (pc.get("d") or {}).get("core") or {}
        if not core:
            return
        day = time.strftime("%Y%m%d")
        if day != getattr(self, "coreday", None):
            if getattr(self, "coref", None):
                self.coref.close()
            d = __import__("pcs_paths").LOGS
            path = os.path.join(d, f"cores_{day}.csv")
            new = not os.path.exists(path)
            self.coref = open(path, "a", newline="", encoding="utf-8")
            self.corew = csv.writer(self.coref)
            self.coreday = day
            if new:
                self.corew.writerow(["time"] + [f"c{i}_{k}" for i in range(24) for k in ("clk", "eclk", "v", "use")])
        row = [time.strftime("%H:%M:%S")]
        for i in range(24):
            c = core.get(str(i)) or core.get(i) or {}
            row += [None if c.get("clk") is None else round(c["clk"]),
                    None if c.get("eclk") is None else round(c["eclk"]),
                    None if c.get("v") is None else round(c["v"], 3),
                    None if c.get("use") is None else round(c["use"], 1)]
        self.corew.writerow(row)
        self.coref.flush()


MEGA = ["ms", "mode", "state", "T1", "T2", "T3", "Tout", "Troom", "DP_A", "DP_B", "DP_R", "DP", "Tset",
        "V2deg", "V2mm", "V2out", "fan", "pump", "rpmF1", "rpmF2", "rpmP2", "V1", "fuses", "faults", "alarms",
        "T_A", "RH_A", "T_B", "RH_B", "RH_R", "disp", "pen"]
PC = ["t_sensor", "cpu_t", "cpu_w", "cpu_load", "gpu_t", "gpu_hot", "gpu_w", "gpu_load", "w_pump", "vrm_t"]
# Contact / liquid-metal watch (owner 2026-11-11: "I believe the LM is migrating downwards, P0-P1 look a little hotter").
# cpu_avg = mean of the 8 P-core temperatures; p0_rest / p1_rest = that core minus the mean of the other 6 P-cores;
# core_cube = hottest core (Core Max) minus the cube. A growing p1_rest / core_cube at the same load = migration.
CORE_P = [0, 1, 10, 11, 12, 13, 22, 23]
CORE_COLS = ["cpu_avg", "p0_rest", "p1_rest", "core_cube"]


def core_stats(pc):
    """{cpu_avg, p0_rest, p1_rest, core_cube} from the HWiNFO detail (None where missing)."""
    out = dict.fromkeys(CORE_COLS)
    if pc.get("status") not in ("ok", "demo"):
        return out
    core = (pc.get("d") or {}).get("core") or {}
    t = {c: (core.get(str(c)) or core.get(c) or {}).get("t") for c in CORE_P}
    if any(v is None for v in t.values()):
        return out
    out["cpu_avg"] = round(sum(t.values()) / len(t), 1)
    for c, k in ((0, "p0_rest"), (1, "p1_rest")):
        rest = [t[x] for x in CORE_P if x not in (0, 1)]
        out[k] = round(t[c] - sum(rest) / len(rest), 1)
    mx = ((pc.get("d") or {}).get("cpu") or {}).get("max_t")
    cube = ((pc.get("v") or {}).get("t_sensor") or {}).get("v")
    if mx is not None and cube is not None:
        out["core_cube"] = round(mx - cube, 1)
    return out


LOG_HEAD = (["time"] + MEGA + ["cube"] + PC[1:] +
            ["prog", "phase", "link", "margin", "target", "u", "c_fan", "c_p2", "c_v2",
             "req_rad", "req_top", "req_p1", "t2set", "trim", "fan_pi"] + CORE_COLS)


def p1_load(heat, idle):
    """P1 % from the averaged CPU + GPU W: idle (P1_IDLE_PCT / P1_RUN_PCT) .. P1_LOAD_LO at P1_W_LO .. P1_LOAD_HI."""
    if heat is None:
        return max(idle, P1_LOAD_LO)
    if heat >= P1_W_LO:
        return P1_LOAD_LO + (P1_LOAD_HI - P1_LOAD_LO) * min(1.0, (heat - P1_W_LO) / (P1_W_HI - P1_W_LO))
    if idle >= P1_LOAD_LO:
        return idle
    return idle + (P1_LOAD_LO - idle) * max(0.0, (heat - P1_W_IDLE) / (P1_W_LO - P1_W_IDLE))


def sweep_knee(res):
    """Lowest P1 % after which the next step buys < SWEEP_KNEE_C per +10 % on both CPU and GPU hot spot (vs cube)."""
    up = []
    for row in res:                      # ascending part only (the last step repeats the first)
        if up and row["p1"] <= up[-1]["p1"]:
            break
        up.append(row)
    for a, b in zip(up, up[1:]):
        k = 10.0 / (b["p1"] - a["p1"])
        gains = [(a[c] - b[c]) * k for c in ("cpu_cube", "hot_cube") if a.get(c) is not None and b.get(c) is not None]
        if gains and max(gains) < SWEEP_KNEE_C:
            return a["p1"]
    return None


def hexs(v):
    return f"0x{v:X}" if isinstance(v, int) else v
