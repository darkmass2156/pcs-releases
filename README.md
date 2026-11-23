<p align="center">
  <img src="images/banner.svg" alt="PCS" width="100%">
</p>

<p align="center">
  <a href="../../releases/latest"><img src="https://img.shields.io/github/v/release/darkmass2156/pcs-releases?include_prereleases&label=download&color=3B82F6&style=for-the-badge" alt="Download"></a>
  <img src="https://img.shields.io/badge/Windows-10%20%7C%2011-22D3EE?style=for-the-badge&logo=windows&logoColor=white" alt="Windows 10 | 11">
  <img src="https://img.shields.io/badge/status-early%20alpha-F0A23B?style=for-the-badge" alt="Early alpha">
</p>

<p align="center">
  <b>See what your PC is really doing.</b><br>
  Every core, the GPU, the board and every fan, live. Fans that react in a quarter of a second.<br>
  And when it crashes, PCS tells you where and why.
</p>

> [!WARNING]
> **Very early alpha.** Expect bugs, missing sensors and crashes. Reflex (fan control) has only been tested on a few
> PCs: watch your temperatures the first time you turn it on, and switch it off in the Fans tab if anything looks
> wrong. Use at your own risk, no warranty.

<p align="center">
  <img src="images/home.png" alt="PCS home screen" width="100%">
</p>

## Live sensors

Every core with its temperature, clock, voltage and load, drawn on the chip itself. The GPU with its memory chips, the
board with every fan header, and the drives. No HWiNFO needed: PCS has its own sensor reader, which uses around 4 % of
one core.

<p align="center">
  <img src="images/dashboard.png" alt="Dashboard: CPU, GPU and motherboard" width="100%">
</p>

## Reflex

<table>
  <tr>
    <td width="50%">
      <p>Name your fans, group them, and let each group follow what matters: CPU or GPU temperature, power draw, or
      any sensor in your PC.</p>
      <p><b>Fast.</b> Reflex reads the CPU four times a second and ramps up in the same moment the load hits, then eases
      back down quietly.</p>
      <p><b>Simple.</b> Two numbers instead of a curve editor: <i>quiet until</i> and <i>full at</i>.</p>
      <p><b>Identify.</b> Spins one header at full speed for a few seconds, so you know which fan is which.</p>
      <p><b>Safe.</b> Off until you switch it on. If a sensor goes quiet, another fan tool takes over or PCS stops,
      every fan falls back to its BIOS curve.</p>
    </td>
    <td width="50%"><img src="images/reflex.png" alt="Reflex fan groups"></td>
  </tr>
</table>

## Crash analyzer

Blue screens, hard resets, silent CPU cache errors and GPU driver resets in one timeline. For each one PCS shows which
core it hit and what the PC was doing in the two minutes before. Turn on the flight recorder for the full picture:
every core, once a second, kept across reboots. Made for overclockers chasing that last unstable core.

<p align="center">
  <img src="images/analyzer.png" alt="Crash analyzer" width="100%">
</p>

## Install

1. Download **`PCS-Setup-x.y.z.exe`** from **[Releases](../../releases/latest)**.
2. Run it and click **Install**. Windows asks for administrator rights, which the sensor reader needs to read the CPU
   and motherboard.
3. If the **PawnIO** driver isn't installed yet, setup offers it. It's the signed, open-source driver that FanControl
   and LibreHardwareMonitor use too.

PCS updates itself from this page: you get a notification in the tray when a new version is out.

**Requirements:** Windows 10 or 11, 64-bit. Intel and AMD CPUs, NVIDIA and AMD GPUs.

<details>
<summary><b>Windows or my antivirus blocks it</b></summary>

PCS isn't code-signed yet. Windows SmartScreen may warn: click **More info → Run anyway**. Some antivirus tools,
especially Norton, may close it. Add `C:\Program Files\PCS` and `C:\ProgramData\PCS` as exclusions.
</details>

<details>
<summary><b>Uninstall</b></summary>

Settings → Apps → Installed apps → **PCS** → Uninstall. Every fan goes back to its BIOS curve. PawnIO stays
installed because other tools use it; remove it there too if you like.
</details>

<details>
<summary><b>Known limits</b></summary>

- Some readings need sensors your hardware may not have; those simply don't show.
- AMD Ryzen 7000 / 9000 report one temperature per chiplet, not per core.
- Reflex drives motherboard fan headers and GPU fans. USB fan hubs and AIO pumps (Corsair, NZXT, Lian Li) aren't
  supported yet.
- Laptops: monitoring only; their fans are usually locked by the manufacturer.
</details>

## Privacy

Nothing is sent anywhere. PCS runs entirely on your PC; its service only listens on `127.0.0.1`. The only outside
connection is the update check against this page.

## Feedback

Found a bug or a sensor that's wrong? Open an **[issue](../../issues)** with your CPU, GPU and motherboard model and a
screenshot.
