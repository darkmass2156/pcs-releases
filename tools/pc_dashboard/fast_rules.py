"""Fan rules for the reader's fast path (tools/pc_sensors FanDriver.cs), per mode. 2026-11-22, owner: "fast path for
all fan operation, gpu too; a roof of 28 °C for the water: no load but high temp = slower ramp up, high water temp and
load = instaramp".

The dashboard sends the running mode's rules in fan_req.json ("fast") once a second; the reader evaluates them 4x a
second from the CPU package temperature / power (read from the CPU), GPU hot spot / memory / power (1 s), the VRM
temperature (ASUS EC) and the water temperature sent here, and raises the case fans at once (floor = max of the rules;
the dashboard's own requests stay the base and the slow way down). Rule fields: see FanDriver.cs.
Headers: rads = NCT controls 1 + 2 (rear 120, front 360), top exhaust = 0.
"""
from sensors_src import GPU_K

RAD, TOP = [1, 2], [0]

# water roof (Passive / Off): over WATER_ROOF_C the rads ramp to 100 % at WATER_FULL_C; with CPU + GPU under
# WATER_INSTANT_W they rise WATER_SLOW_PCT_S %/s (30 -> 100 % in ~70 s), with load at once. ASSUMED numbers.
WATER_ROOF_C = 28.0
WATER_FULL_C = 31.0
WATER_SLOW_PCT_S = 1.0
WATER_INSTANT_W = 150.0


def passive(p, water):
    """Passive / Off: the same curves as pcs_prog.passive() (p = the pcs_prog module, for its constants)."""
    rules = [
        {"src": "cpu_t", "lo": p.PAS_CPU_LO, "hi": p.PAS_CPU_HI, "min": p.PAS_RAD_LO},
        {"src": "load_w", "lo": p.PAS_W_LO, "hi": p.PAS_W_HI, "min": p.PAS_RAD_LO},
        {"src": "gpu_hot", "lo": p.PAS_GPU_LO, "hi": p.PAS_GPU_HI, "min": p.PAS_RAD_LO},
        {"src": "hot", "c": p.PAS_COMP_C, "per_c": p.PAS_COMP_PCT_PER_C},
        {"src": "water", "lo": WATER_ROOF_C, "hi": WATER_FULL_C, "min": p.PAS_RAD_LO,
         "slow_pct_s": WATER_SLOW_PCT_S, "instant_w": WATER_INSTANT_W},
    ]
    return {"rad": RAD, "top": TOP, "top_sub": 15, "top_min": 30, "water": water, "gpu_k": GPU_K, "rules": rules}


def daily(d):
    """Daily (Mode 5): the guards that move fast (CPU, GPU hot spot / memory, VRM); chipset / DIMM / SSD / air / return
    stay on the dashboard path. Fans stay off until a guard starts (rule value 0 below its start). d = daily module."""
    g = {k: (lo, hi, kind) for k, _l, _src, lo, hi, kind in d.GUARDS}
    rules = []
    for key, src in (("cpu", "cpu_t"), ("gpu", "gpu_hot"), ("vram", "gpu_mem"), ("vrm", "vrm_t")):
        if key in g:
            lo, hi, kind = g[key]
            rules.append({"src": src, "lo": lo, "hi": hi, "min": d.ON_MIN_PCT,
                          "top": 1.0 if kind == "board" else d.TOP_LOOP_SHARE})
    return {"rad": RAD, "top": TOP, "top_sub": 100, "top_min": d.TOP_MIN_PCT, "gpu_k": GPU_K, "rules": rules}


def overclocking(pkg_c, vrm_c):
    """Mode 4: rads / top at 10 % unless the CPU package or the VRM gets hot (then at least 40 %, full 5 °C later)."""
    rules = [{"src": "cpu_t", "lo": pkg_c - 0.5, "hi": pkg_c + 5, "min": 40, "top": 1.0},
             {"src": "vrm_t", "lo": vrm_c - 0.5, "hi": vrm_c + 5, "min": 40, "top": 1.0}]
    return {"rad": RAD, "top": TOP, "top_sub": 100, "top_min": 30, "gpu_k": GPU_K, "rules": rules}
