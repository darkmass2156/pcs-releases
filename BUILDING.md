# Building PCS

The source is in `tools/`:

| Folder | What | Built with |
|---|---|---|
| `pc_app` | PCS.exe: tray app + window (WinForms + WebView2) | .NET 10 SDK |
| `pc_sensors` | PcsSensors.exe: sensor reader and Reflex fan control | .NET 10 SDK |
| `pc_setup` | the installer / uninstaller | .NET Framework 4.8 |
| `pc_dashboard` | the service (`pcs_dash.py`) and every page (HTML / JS / CSS), board drawings in `boards/` | Python 3 + PyInstaller |
| `standalone` | `build.ps1` builds everything into one setup exe, `publish.ps1` makes a GitHub release | PowerShell |

## Dependencies the repo doesn't ship

- **`tools/pc_sensors/lib/`**: LibreHardwareMonitorLib 0.9.6 and the DLLs it needs (the list is in
  `PcsSensors.csproj`). Take them from a LibreHardwareMonitor or FanControl install, or build
  [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor).
- **`tools/standalone/vendor/PawnIO_setup.exe`**: the official signed installer from
  [namazso/PawnIO.Setup](https://github.com/namazso/PawnIO.Setup). `build.ps1` checks its signature.
- Python: `py -m pip install pyserial pyinstaller`.

## Build

```powershell
powershell -ExecutionPolicy Bypass -File tools\standalone\build.ps1
```

Output: `tools\standalone\out\PCS-Setup-<version>.exe` and its `.sha256`. The version comes from
`tools\pc_app\PcsApp.csproj`.

`tools/standalone/release.json` names the GitHub repo the app checks for updates. Change it in a fork, or delete it to
build without update checks.

## Running from source

```powershell
py tools\pc_dashboard\pcs_dash.py --standalone
```

This starts the service on http://127.0.0.1:8570. It needs the sensor reader running as administrator for live data.

Some files (`pcs_prog.py`, `daily.py`, `hwinfo_out.py`, `priority_boost.ps1`, `install_*.ps1`) belong to the author's
own setup, where an Arduino Mega runs an outdoor water-cooling loop. The standalone build doesn't use them.
