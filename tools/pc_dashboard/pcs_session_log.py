"""Session logger: records the running dashboard's live stream (Mega CSV rows + HWiNFO values) into one CSV.

Doesn't touch the COM port: it reads http://127.0.0.1:8570/stream, so the dashboard keeps running.
    py pcs_session_log.py cs2          -> logs/session_cs2_YYYYmmdd_HHMMSS.csv, Ctrl+C to stop
One line per Mega row (2 Hz); the HWiNFO columns are the latest reading (1 s).
--keepalive: while the Mega is in serial test mode, re-send the current fan % (f<n>) every 10 min so test mode
doesn't time out (TEST_TIMEOUT_MS 30 min). Same value = no change, no start boost.
"""
import csv
import json
import os
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8570/stream"
CMD_URL = "http://127.0.0.1:8570/cmd"
KEEPALIVE_S = 600
MEGA = ["ms", "mode", "state", "T1", "T2", "T3", "Tout", "Troom", "DP_A", "DP_B", "DP_R", "DP", "Tset",
        "V2deg", "V2mm", "V2out", "fan", "pump", "rpmF1", "rpmF2", "rpmP2", "V1", "fuses", "faults", "alarms",
        "T_A", "RH_A", "T_B", "RH_B", "RH_R", "disp", "pen"]
PC = ["t_sensor", "cpu_t", "cpu_w", "cpu_load", "gpu_t", "gpu_hot", "gpu_w", "gpu_load", "w_pump", "vrm_t"]


def hexs(v):
    return f"0x{v:X}" if isinstance(v, int) else v


def send(cmd):
    req = urllib.request.Request(CMD_URL, data=json.dumps({"cmd": cmd}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep = "--keepalive" in sys.argv
    tag = args[0] if args else "session"
    last_keep = 0.0                    # first keepalive right away (the test timer may already be running down)
    here = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(__import__("pcs_paths").LOGS, exist_ok=True)
    path = os.path.join(__import__("pcs_paths").LOGS, f"session_{tag}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    f = open(path, "w", newline="", encoding="utf-8")
    w = csv.writer(f)
    w.writerow(["time"] + MEGA + ["cube"] + PC[1:] + ["heat_w"])
    print(f"logging to {path}  (Ctrl+C to stop)")
    pc, n = {}, 0
    while True:
        try:
            with urllib.request.urlopen(URL, timeout=10) as s:
                kind = None
                for raw in s:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("event: "):
                        kind = line[7:]
                    elif line.startswith("data: ") and kind in ("row", "pc"):
                        d = json.loads(line[6:])
                        if kind == "pc":
                            pc = {k: v["v"] for k, v in (d.get("v") or {}).items()} if d.get("status") in ("ok", "demo") else {}
                            continue
                        vals = [pc.get(k) for k in PC]
                        heat = (pc.get("cpu_w") or 0) + (pc.get("gpu_w") or 0) if pc else None
                        w.writerow([time.strftime("%H:%M:%S", time.localtime(d.get("t", time.time())))] +
                                   [hexs(d.get(k)) if k in ("fuses", "faults", "alarms") else d.get(k) for k in MEGA] +
                                   vals + [round(heat, 1) if heat is not None else None])
                        n += 1
                        if (keep and d.get("state") == "TEST" and d.get("fan") is not None
                                and time.time() - last_keep >= KEEPALIVE_S):
                            last_keep = time.time()
                            try:
                                send(f"f{d['fan']}")
                            except Exception as e:
                                print(f"keepalive failed: {e}")
                                continue
                            print(f"{time.strftime('%H:%M:%S')}  keepalive f{d['fan']} (test mode timer reset)")
                        if n % 20 == 0:
                            f.flush()
                        if n % 120 == 0:
                            print(f"{time.strftime('%H:%M:%S')}  {n} rows  T1 {d.get('T1')}  T2 {d.get('T2')}  "
                                  f"cube {pc.get('t_sensor')}  heat {heat and round(heat)} W  V2 {d.get('V2out')} %")
        except KeyboardInterrupt:
            break
        except Exception as e:          # dashboard restarted / not up yet
            print(f"[{e}] retrying in 3 s")
            f.flush()
            try:
                time.sleep(3)
            except KeyboardInterrupt:
                break
    f.close()
    print(f"{n} rows -> {path}")


if __name__ == "__main__":
    main()
