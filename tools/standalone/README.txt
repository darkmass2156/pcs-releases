PCS for other PCs
=================

VERY EARLY ALPHA. Expect bugs, missing sensors and crashes. Reflex (fan control) is new and has not been tested on many
PCs: watch your temperatures the first time you turn it on, and switch it off (Fans tab) if anything looks wrong.
Use at your own risk; no warranty.

Install: run PCS-Setup-<version>.exe and click Install. Windows asks for administrator rights (the sensor reader
needs them to read the CPU and motherboard and to set fan speeds). If the PawnIO driver is missing, setup offers it
(its own signed installer window).

What you get:
- Live CPU (per core), GPU, motherboard, drives, fans.
- Reflex (Fans tab): name your fans, group them, and let each group follow what matters: CPU or GPU temperature,
  power draw, or any sensor in your PC. Reacts within a quarter of a second and eases back down quietly.
  Off by default: switch Reflex on in the Fans tab. When it's off, a sensor goes quiet, FanControl runs or PCS stops,
  every fan falls back to its BIOS curve.
- Analyzer: blue screens, WHEA errors and GPU driver resets with what the PC was doing just before.
- Updates: PCS checks GitHub for new versions and offers them (tray notification, Settings -> Updates).

Remove: Settings -> Apps -> Installed apps -> PCS -> Uninstall. PawnIO stays installed (other tools use it).

Antivirus: PCS isn't code-signed yet. If Norton or another antivirus closes it, add C:\Program Files\PCS and
C:\ProgramData\PCS as exclusions.

Nothing is sent anywhere. The PCS service listens on 127.0.0.1 only; the only outside contact is the update check.
