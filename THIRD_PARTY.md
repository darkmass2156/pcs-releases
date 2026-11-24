# Third-party software

PCS's own code is GPL-3.0 (see `LICENSE`). The installer bundles or uses the following, each under its own license:

| Component | License | Use |
|---|---|---|
| [LibreHardwareMonitorLib](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) | MPL-2.0 | sensor access in PcsSensors |
| HidSharp, DiskInfoToolkit, RAMSPDToolkit, BlackSharp.Core, NvAPIWrapper | see each project | LibreHardwareMonitorLib dependencies |
| Newtonsoft.Json | MIT | JSON |
| TaskScheduler (Microsoft.Win32.TaskScheduler) | MIT | scheduled task setup |
| Mono.Posix | MIT | LibreHardwareMonitorLib dependency |
| [PawnIO](https://github.com/namazso/PawnIO) | its own license (installer by namazso) | kernel driver for hardware access, installed separately |
| Microsoft WebView2 | Microsoft license | the app window |
| .NET runtime | MIT | self-contained app |
| Python + PyInstaller, pyserial | PSF / GPL with bootloader exception / BSD | the service |
| Plus Jakarta Sans (Google Fonts) | OFL-1.1 | UI font |

Motherboard, CPU and GPU names and drawings are there to identify hardware. The trademarks belong to their owners;
PCS isn't affiliated with any of them.
