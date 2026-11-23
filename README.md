PCS
⚠️ Very early alpha. Expect bugs, missing sensors and crashes. Reflex (fan control) is new and has only been tested on a few PCs: watch your temperatures the first time you turn it on, and switch it off in the Fans tab if anything looks wrong. Use at your own risk, no warranty.

PCS is a Windows app that shows what your PC is really doing: every core, the GPU, the motherboard, drives and fans, live. It explains crashes, and it runs your fans from the sensors that matter.

Features
Live sensors
CPU per core: temperature, clock, effective clock, voltage, load, idle states
GPU: core and hot spot temperature, memory, power, clocks, load
Motherboard, drives and every fan header, drawn on a picture of your CPU, GPU and board
No HWiNFO needed. PCS has its own sensor reader, which uses around 4 % of one core.
Reflex
Name your fans, group them, and let each group follow what matters: CPU or GPU temperature, power draw, or any sensor in your PC. Reflex reacts within a quarter of a second and eases back down quietly.

Quiet until / full at: two numbers instead of a curve editor
Identify: spins one header at full speed for a few seconds so you know which fan it is
Safe by default: off until you switch it on. If Reflex is off, a sensor goes quiet, another fan tool takes over or PCS stops, every fan falls back to its BIOS curve.
Analyzer
Blue screens, hard resets, WHEA errors and GPU driver resets in one timeline. For each one, PCS shows which core it hit and what the PC was doing in the 2 minutes before. Turn on the flight recorder for the full picture: every core once a second, kept across reboots.

Updates
PCS checks this page for new versions and offers them in the tray and in Settings. Every download is checked against its published checksum before it runs.

Install
Download the latest PCS-Setup-x.y.z.exe from Releases.
Run it and click Install. Windows asks for administrator rights, which the sensor reader needs to read the CPU and motherboard.
If the PawnIO driver isn't installed yet, setup offers it. It's the signed, open-source driver that FanControl and LibreHardwareMonitor use too.
Requirements: Windows 10 or 11, 64-bit. Intel and AMD CPUs, NVIDIA and AMD GPUs.

Antivirus: PCS isn't code-signed yet, so Windows SmartScreen may warn (More info → Run anyway). Some antivirus tools, especially Norton, may close it. If that happens, add C:\Program Files\PCS and C:\ProgramData\PCS as exclusions.

Uninstall
Settings → Apps → Installed apps → PCS → Uninstall. Every fan goes back to its BIOS curve. PawnIO stays installed because other tools use it; remove it there too if you like.

Privacy
Nothing is sent anywhere. PCS runs entirely on your PC (its service only listens on 127.0.0.1). The only outside connection is the update check against this repository.

Known limits
Some readings need sensors your hardware may not have; those simply don't show.
AMD Ryzen 7000 / 9000 report one temperature per chiplet, not per core.
Reflex drives motherboard fan headers and GPU fans. USB fan hubs and AIO pumps (Corsair, NZXT, Lian Li) aren't supported yet.
Laptops: monitoring only; their fans are usually locked by the manufacturer.
Feedback
Found a bug or a sensor that's wrong? Open an issue with your CPU, GPU and motherboard model and a screenshot.
