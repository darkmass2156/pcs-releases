r"""Where the service keeps its data (2026-11-23, standalone build).

From source (this PC): tools/pc_dashboard/logs, as always. Packaged (PyInstaller, the build sent to other PCs):
%LOCALAPPDATA%\PCS\data, because the exe's own folder is read-only (Program Files) or a temp extraction.
PCS_DATA overrides both. SENSORS = the folder the sensor reader writes now.json to (PCS_SENSORS_DIR, else LOGS\sensors).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = getattr(sys, "frozen", False)
if os.environ.get("PCS_DATA"):
    LOGS = os.environ["PCS_DATA"]
elif FROZEN:
    LOGS = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "PCS", "data")
else:
    LOGS = os.path.join(HERE, "logs")
SENSORS = os.environ.get("PCS_SENSORS_DIR") or (
    os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "PCS", "sensors") if FROZEN   # the installed reader's --out
    else os.path.join(LOGS, "sensors"))
os.makedirs(LOGS, exist_ok=True)
