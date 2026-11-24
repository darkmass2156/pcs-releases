// PCS standalone sensor reader (2026-11-22, owner: "write our own hook instead of using HWiNFO, so we can run standalone").
//
// Reads CPU / board / memory / GPU / storage sensors through LibreHardwareMonitorLib (PawnIO driver for the CPU registers
// and the Super I/O chip, NVML / NvAPI for the GPU) once a second and writes them as JSON to a file the dashboard reads:
//   ..\pc_dashboard\logs\sensors\now.json   {t, admin, poll_ms{hardware: ms}, sensors[{id, hw, type, name, v}]}
// The file is replaced atomically (temp + move), so a reader never sees half a file.
// The time each hardware group takes to update is in poll_ms: that is how the sensor that keeps HWiNFO busy is found.
//
// Usage:  PcsSensors.exe                 run, 1 s period
//         PcsSensors.exe --list          one pass, print every sensor, exit
//         PcsSensors.exe --period 2000   other period (ms)
//         PcsSensors.exe --no-storage --no-memory --no-gpu --no-board --no-cpu   leave a group out
//         PcsSensors.exe --out <file>    other output file
//         PcsSensors.exe --topo          print cpu_info + topo (CPUID / Windows topology only, no sensors), exit
// CPU and board sensors need administrator rights (PawnIO); without them only the GPU and some storage show up.
// 2026-11-23 (portable mapping): the output also carries "cpu_info" (CPUID vendor / family / model / stepping) and
// "topo" (physical cores in LHM's order = APIC id: logical processors, SMT, EfficiencyClass, owning L3), so the
// dashboard maps sensors by name on any PC; the fan-floor inputs are found by sensor name, not by fixed ids.
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;

static class Program
{
    static int Main(string[] args)
    {
        CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture;
        bool list = args.Contains("--list");
        int period = 1000;
        string outPath = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, @"..\..\pc_dashboard\logs\sensors\now.json"));
        for (int i = 0; i < args.Length - 1; i++)
        {
            if (args[i] == "--period") period = Math.Max(250, int.Parse(args[i + 1]));
            if (args[i] == "--out") outPath = Path.GetFullPath(args[i + 1]);
        }
        CpuTopo.Init();
        if (args.Contains("--topo"))
        {
            using var tms = new MemoryStream();
            using (var tw = new Utf8JsonWriter(tms, new JsonWriterOptions { Indented = true }))
            {
                tw.WriteStartObject();
                CpuTopo.Write(tw, null);
                tw.WriteEndObject();
            }
            Console.WriteLine(Encoding.UTF8.GetString(tms.ToArray()));
            return 0;
        }
        bool admin = new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);
        // High priority (2026-11-22): under an all-core load (R23) the per-core register reads waited behind the load
        // for ~12 s, the output went stale and the dashboard lost the CPU data. The dashboard runs High too.
        try { Process.GetCurrentProcess().PriorityClass = ProcessPriorityClass.High; } catch { }

        var pc = new Computer
        {
            IsCpuEnabled = !args.Contains("--no-cpu"),
            IsMotherboardEnabled = !args.Contains("--no-board"),
            IsMemoryEnabled = !args.Contains("--no-memory"),
            IsGpuEnabled = !args.Contains("--no-gpu"),
            IsStorageEnabled = !args.Contains("--no-storage"),
            IsControllerEnabled = false, IsNetworkEnabled = false, IsPsuEnabled = false, IsBatteryEnabled = false,
        };
        var sw = Stopwatch.StartNew();
        pc.Open();
        long openMs = sw.ElapsedMilliseconds;

        if (list)
        {
            var poll = Update(pc);
            Console.WriteLine($"admin={admin}  open {openMs} ms");
            foreach (var kv in poll) Console.WriteLine($"  update {kv.Key}: {kv.Value:F1} ms");
            foreach (var s in All(pc))
                Console.WriteLine($"{s.Hardware.Name,-34} {s.SensorType,-12} {s.Name,-34} {Fmt(s.Value),10}  {s.Identifier}");
            pc.Close();
            return 0;
        }

        var dir = Path.GetDirectoryName(outPath);
        Directory.CreateDirectory(dir);
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; stop = true; };
        AppDomain.CurrentDomain.ProcessExit += (_, _) => stop = true;
        // Intel MSRs only on Intel (2026-11-23, standalone build on an AMD PC): elsewhere only the Windows idle states
        var msr = admin ? new MsrReader(CpuTopo.Vendor == "intel") : null;
        var ec = admin && !args.Contains("--no-ec") ? new EcReader(dir) : null;   // --no-ec: other PCs (standalone build), never touch their EC
        ec?.Start();
        // --user-fans (standalone package, 2026-11-23): the user's own groups from fans.json instead of the dashboard's requests
        var ufans = admin && args.Contains("--user-fans") ? new UserFans(dir, pc) : null;
        var fans = admin && ufans == null && !args.Contains("--no-fans") ? new FanDriver(dir, pc) : null;
        var opts = new JsonWriterOptions { Indented = false };
        while (!stop)
        {
            var t0 = Stopwatch.GetTimestamp();
            try
            {
                var tu = Stopwatch.GetTimestamp();
                var poll = Update(pc);
                poll["all update (PCS)"] = Stopwatch.GetElapsedTime(tu).TotalMilliseconds;
                Pick.Telemetry(pc);                         // fan floor inputs + EC scan references, by name
                var tx = Stopwatch.GetTimestamp();
                try { msr?.Update(); } catch (Exception ex) { msr.Error = ex.Message; }
                poll["msr (PCS)"] = Stopwatch.GetElapsedTime(tx).TotalMilliseconds;
                tx = Stopwatch.GetTimestamp();
                try { ec?.Update(); } catch (Exception ex) { ec.Error = ex.Message; }
                poll["ec (PCS)"] = Stopwatch.GetElapsedTime(tx).TotalMilliseconds;
                tx = Stopwatch.GetTimestamp();
                try { fans?.Update(); } catch (Exception ex) { fans.Mode = "error: " + ex.Message; }
                poll["fans (PCS)"] = Stopwatch.GetElapsedTime(tx).TotalMilliseconds;
                tx = Stopwatch.GetTimestamp();
                using var ms = new MemoryStream();
                using (var w = new Utf8JsonWriter(ms, opts))
                {
                    w.WriteStartObject();
                    w.WriteNumber("t", DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0);
                    w.WriteBoolean("admin", admin);
                    w.WriteNumber("loop_ms", Math.Round(lastLoopMs, 1));
                    w.WriteNumber("move_retries", moveRetries);
                    w.WriteNumber("errors", errorCount);
                    w.WriteString("shm", Shm.Error ?? "ok");
                    if (slowest != null)
                    {
                        w.WriteStartObject("slowest_pass");      // the slowest pass of the last 2 min, by stage
                        foreach (var kv in slowest) w.WriteNumber(kv.Key, Math.Round(kv.Value, 1));
                        w.WriteEndObject();
                    }
                    w.WriteStartObject("poll_ms");
                    foreach (var kv in poll) w.WriteNumber(kv.Key, Math.Round(kv.Value, 1));
                    w.WriteEndObject();
                    w.WriteStartArray("sensors");
                    foreach (var s in All(pc))
                    {
                        w.WriteStartObject();
                        w.WriteString("id", s.Identifier.ToString());
                        w.WriteString("hw", s.Hardware.Name);
                        w.WriteString("type", s.SensorType.ToString());
                        w.WriteString("name", s.Name);
                        if (s.Value is float f && !float.IsNaN(f) && !float.IsInfinity(f)) w.WriteNumber("v", Math.Round(f, 4));
                        else w.WriteNull("v");
                        w.WriteEndObject();
                    }
                    w.WriteEndArray();
                    CpuTopo.Write(w, Pick.Cpu(pc)?.Name);
                    msr?.Write(w);
                    ec?.Write(w);
                    fans?.Write(w);
                    ufans?.Write(w);
                    w.WriteEndObject();
                }
                var bytes = ms.ToArray();
                poll["json (PCS)"] = Stopwatch.GetElapsedTime(tx).TotalMilliseconds;
                tx = Stopwatch.GetTimestamp();
                // shared memory (like HWiNFO): a file replace failed for seconds under load (readers and the virus
                // scanner hold the fresh file), so the dashboard reads Global\PCS_SENSORS; now.json is a debug copy
                Shm.Publish(bytes);
                poll["publish (PCS)"] = Stopwatch.GetElapsedTime(tx).TotalMilliseconds;
                tx0 = Stopwatch.GetTimestamp();
                lastPoll = poll;
                if (Stopwatch.GetElapsedTime(lastFile).TotalSeconds >= 5)
                {
                    lastFile = Stopwatch.GetTimestamp();
                    try
                    {
                        var tmp = outPath + ".tmp";
                        File.WriteAllBytes(tmp, bytes);
                        File.Move(tmp, outPath, true);
                    }
                    catch (Exception e) when (e is IOException || e is UnauthorizedAccessException) { moveRetries++; }
                }
            }
            catch (Exception ex)
            {
                LogError(dir, ex);
            }
            var used = Stopwatch.GetElapsedTime(t0).TotalMilliseconds;
            lastLoopMs = used;
            if (lastPoll != null)
            {
                lastPoll["file (PCS)"] = Stopwatch.GetElapsedTime(tx0).TotalMilliseconds;
                lastPoll["loop"] = used;
                if (used > slowestMs || Stopwatch.GetElapsedTime(slowestAt).TotalSeconds > 120)
                { slowestMs = used; slowest = lastPoll; slowestAt = Stopwatch.GetTimestamp(); }
            }
            Thread.Sleep((int)Math.Max(50, period - used));
        }
        if (ec != null) ec.Stop = true;
        if (fans != null) fans.Stop = true;
        if (ufans != null) ufans.Stop = true;
        try { fans?.Release(); } catch { }                  // headers back to the BIOS curves
        try { ufans?.Release(); } catch { }
        Pick.RestoreGpuFans(pc);
        pc.Close();
        return 0;
    }

    static volatile bool stop;
    static double lastLoopMs, slowestMs;
    static Dictionary<string, double> lastPoll, slowest;
    static long slowestAt, tx0;
    static long lastFile;
    static long _pass;
    static long moveRetries, errorCount;
    static DateTime lastLogged;

    // errors go to reader_errors.log next to now.json (at most one line a second; the task has no console)
    static void LogError(string dir, Exception ex)
    {
        errorCount++;
        if ((DateTime.Now - lastLogged).TotalSeconds < 1) return;
        lastLogged = DateTime.Now;
        try
        {
            var p = Path.Combine(dir, "reader_errors.log");
            if (File.Exists(p) && new FileInfo(p).Length > 1_000_000) File.Delete(p);
            File.AppendAllText(p, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {ex.GetType().Name}: {ex.Message}\n");
        }
        catch { }
    }

    // update every hardware (and its sub-hardware: the Super I/O chips sit under the motherboard), timing each one
    static Dictionary<string, double> Update(Computer pc)
    {
        var d = new Dictionary<string, double>();
        _pass++;
        foreach (var h in pc.Hardware)
        {
            // DIMM SPD temperatures (memory SMBus) took 4-5 s per read under an all-core load: every 10th pass only
            if (h.HardwareType == HardwareType.Memory && h.Name.Contains("(#") && _pass % 10 != 1) continue;
            var t = Stopwatch.GetTimestamp();
            if (h.HardwareType == HardwareType.Cpu) { lock (HwLock.Cpu) h.Update(); }   // UserFans updates the CPU 4x a second too
            else h.Update();
            foreach (var sh in h.SubHardware)
            {
                var t2 = Stopwatch.GetTimestamp();
                sh.Update();
                d[Key(d, sh.Name)] = Stopwatch.GetElapsedTime(t2).TotalMilliseconds;
            }
            d[Key(d, h.Name)] = Stopwatch.GetElapsedTime(t).TotalMilliseconds;
        }
        return d;
    }

    static string Key(Dictionary<string, double> d, string name)
    {
        var k = name; int n = 2;
        while (d.ContainsKey(k)) k = name + " #" + n++;
        return k;
    }

    static IEnumerable<ISensor> All(Computer pc)
    {
        foreach (var h in pc.Hardware)
        {
            foreach (var s in h.Sensors) yield return s;
            foreach (var sh in h.SubHardware)
                foreach (var s in sh.Sensors) yield return s;
        }
    }

    static string Fmt(float? v) => v is float f && !float.IsNaN(f) ? f.ToString("0.###") : "-";
}

// Sensors found by hardware type and name instead of fixed LHM ids (2026-11-23, docs/hw_support.md): ids are running
// counters that shift with what a CPU / GPU exposes, names are stable across generations.
static class Pick
{
    static readonly HardwareType[] GpuTypes = { HardwareType.GpuNvidia, HardwareType.GpuAmd, HardwareType.GpuIntel };

    public static IHardware Cpu(Computer pc) => pc.Hardware.FirstOrDefault(h => h.HardwareType == HardwareType.Cpu);

    // the discrete card: largest dedicated memory, then most sensors (an iGPU next to a Radeon is also "gpu-amd")
    public static IHardware Gpu(Computer pc)
    {
        IHardware best = null; double bestMem = -1; int bestN = -1;
        foreach (var h in pc.Hardware)
        {
            if (!GpuTypes.Contains(h.HardwareType)) continue;
            double mem = 0;
            foreach (var s in h.Sensors)
            {
                var st = s.SensorType.ToString();                 // "Data" = bytes; "SmallData" (MB) in other LHM versions
                if ((st == "Data" || st == "SmallData") && s.Value is float v &&
                    (s.Name == "GPU Memory Total" || s.Name == "D3D Dedicated Memory Total"))
                    mem = Math.Max(mem, st == "SmallData" ? v * 1048576.0 : v);
            }
            int n = h.Sensors.Length;
            if (mem > bestMem || (mem == bestMem && n > bestN)) { best = h; bestMem = mem; bestN = n; }
        }
        return best;
    }

    public static ISensor Find(IHardware h, SensorType type, params string[] names)
    {
        if (h == null) return null;
        foreach (var n in names)
            foreach (var s in h.Sensors)
                if (s.SensorType == type && s.Name == n) return s;
        return null;
    }

    static readonly string[] CpuPower = { "CPU Package", "Package" };
    static readonly string[] CpuVcore = { "CPU Core", "Core (SVI2 TFN)", "VDDCR" };
    static readonly string[] GpuPower = { "GPU Package", "GPU Total", "Total Card Power", "GPU Board Power", "Package" };

    static float Val(ISensor s) => s?.Value is float f ? f : float.NaN;

    // the values the fixed ids gave on this PC: CPU Package W, CPU Core V, GPU Hot Spot, GPU Memory Junction, GPU Package W
    public static void Telemetry(Computer pc)
    {
        var cpu = Cpu(pc); var gpu = Gpu(pc);
        float v;
        if (!float.IsNaN(v = Val(Find(cpu, SensorType.Power, CpuPower)))) global::Telemetry.CpuW = v;
        if (!float.IsNaN(v = Val(Find(cpu, SensorType.Voltage, CpuVcore)))) global::Telemetry.Vcore = v;
        if (!float.IsNaN(v = Val(Find(gpu, SensorType.Temperature, "GPU Hot Spot")))) global::Telemetry.GpuHot = v;
        if (!float.IsNaN(v = Val(Find(gpu, SensorType.Temperature, "GPU Memory Junction", "GPU Memory")))) global::Telemetry.GpuMem = v;
        if (!float.IsNaN(v = Val(Find(gpu, SensorType.Power, GpuPower)))) global::Telemetry.GpuW = v;
    }

    // exit only: LHM's Close() restores GPU fans by array position, not by cooler id, so a fan this process set could
    // stay manual. Every GPU control this process put in software mode goes back to the card's own curve first.
    // Controls this process never set (ControlMode Undefined) are left alone: another program may be driving them.
    public static void RestoreGpuFans(Computer pc)
    {
        try
        {
            foreach (var h in pc.Hardware)
            {
                if (!GpuTypes.Contains(h.HardwareType)) continue;
                foreach (var s in h.Sensors)
                    if (s.SensorType == SensorType.Control && s.Control != null && s.Control.ControlMode == ControlMode.Software)
                        try { s.Control.SetDefault(); } catch { }
            }
        }
        catch { }
    }
}

// CPU identity (CPUID) and physical-core topology (GetLogicalProcessorInformationEx), read once at start.
// Any failure leaves that part out of the output ("cpu_info" / "topo" missing, "topo_error" says why); the reader runs on.
static class CpuTopo
{
    public sealed class Core
    {
        public readonly List<int> Lp = new();     // logical processor numbers (group base + bit)
        public bool Smt;
        public int Eff;                           // EfficiencyClass: higher = faster (P) on hybrid parts
        public int L3 = -1;                       // index of the L3 cache that owns the first logical processor
        public long Apic = -1;                    // lowest APIC id of its threads (LHM sorts cores by it)
    }

    public static List<Core> Cores;
    public static string Vendor, Error;
    public static int Family = -1, Model = -1, Stepping = -1;

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetLogicalProcessorInformationEx(int relationship, IntPtr buffer, ref uint length);
    [DllImport("kernel32.dll")] static extern ushort GetActiveProcessorGroupCount();
    [DllImport("kernel32.dll")] static extern uint GetActiveProcessorCount(ushort group);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentThread();
    [StructLayout(LayoutKind.Sequential)]
    struct GROUP_AFFINITY { public UIntPtr Mask; public ushort Group; public ushort R0, R1, R2; }
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetThreadGroupAffinity(IntPtr thread, ref GROUP_AFFINITY affinity, out GROUP_AFFINITY previous);

    public static void Init()
    {
        try { ReadCpuId(); } catch (Exception ex) { Error = "cpuid: " + ex.Message; }
        try { Cores = ReadTopo(); } catch (Exception ex) { Cores = null; Error = "topo: " + ex.Message; }
    }

    static void ReadCpuId()
    {
        if (!System.Runtime.Intrinsics.X86.X86Base.IsSupported) return;
        var (_, b, c, d) = System.Runtime.Intrinsics.X86.X86Base.CpuId(0, 0);
        var vb = new byte[12];
        BitConverter.GetBytes(b).CopyTo(vb, 0); BitConverter.GetBytes(d).CopyTo(vb, 4); BitConverter.GetBytes(c).CopyTo(vb, 8);
        var raw = Encoding.ASCII.GetString(vb);
        Vendor = raw == "GenuineIntel" ? "intel" : raw == "AuthenticAMD" ? "amd" : raw.Trim('\0', ' ');
        var (a, _, _, _) = System.Runtime.Intrinsics.X86.X86Base.CpuId(1, 0);
        int fam = (a >> 8) & 0xF, mod = (a >> 4) & 0xF;
        Stepping = a & 0xF;
        if (fam == 0xF) fam += (a >> 20) & 0xFF;
        if (fam == 6 || fam >= 0xF) mod += ((a >> 16) & 0xF) << 4;
        Family = fam; Model = mod;
    }

    static List<Core> ReadTopo()
    {
        if (!Environment.Is64BitProcess) throw new PlatformNotSupportedException("32-bit process");
        const int RelationAll = 0xFFFF, RelationProcessorCore = 0, RelationCache = 2;
        int groups = GetActiveProcessorGroupCount();
        var gbase = new int[Math.Max(groups, 1)];
        for (int g = 1; g < gbase.Length; g++) gbase[g] = gbase[g - 1] + (int)GetActiveProcessorCount((ushort)(g - 1));
        uint len = 0;
        GetLogicalProcessorInformationEx(RelationAll, IntPtr.Zero, ref len);
        if (len == 0) throw new InvalidOperationException("no length, error " + Marshal.GetLastWin32Error());
        var buf = Marshal.AllocHGlobal((int)len);
        var cores = new List<Core>();
        var l3 = new List<HashSet<int>>();
        try
        {
            if (!GetLogicalProcessorInformationEx(RelationAll, buf, ref len))
                throw new InvalidOperationException("error " + Marshal.GetLastWin32Error());
            List<int> Lps(IntPtr p, int at, int count)      // GROUP_AFFINITY[count] at offset `at`, 16 bytes each on x64
            {
                var r = new List<int>();
                for (int k = 0; k < count; k++)
                {
                    ulong mask = (ulong)Marshal.ReadInt64(p, at + 16 * k);
                    int grp = (ushort)Marshal.ReadInt16(p, at + 16 * k + 8);
                    int gb = grp < gbase.Length ? gbase[grp] : 64 * grp;
                    for (int bit = 0; bit < 64; bit++) if ((mask >> bit & 1) != 0) r.Add(gb + bit);
                }
                return r;
            }
            for (uint off = 0; off + 8 <= len;)
            {
                var p = buf + (int)off;
                int rel = Marshal.ReadInt32(p), size = Marshal.ReadInt32(p, 4);
                if (size <= 0) break;
                if (rel == RelationProcessorCore)
                {
                    // PROCESSOR_RELATIONSHIP: Flags @8, EfficiencyClass @9, Reserved[20], GroupCount @30, GroupMask[] @32
                    var c = new Core { Smt = (Marshal.ReadByte(p, 8) & 1) != 0, Eff = Marshal.ReadByte(p, 9) };
                    int gc = Math.Max(1, (int)(ushort)Marshal.ReadInt16(p, 30));
                    c.Lp.AddRange(Lps(p, 32, gc));
                    c.Smt |= c.Lp.Count > 1;
                    if (c.Lp.Count > 0) cores.Add(c);
                }
                else if (rel == RelationCache && Marshal.ReadByte(p, 8) == 3)
                {
                    // CACHE_RELATIONSHIP: Level @8, Type @16, Reserved[18], GroupCount @38 (0 on old builds = 1), GroupMask[] @40
                    int gc = Math.Max(1, (int)(ushort)Marshal.ReadInt16(p, 38));
                    l3.Add(new HashSet<int>(Lps(p, 40, gc)));
                }
                off += (uint)size;
            }
        }
        finally { Marshal.FreeHGlobal(buf); }
        if (cores.Count == 0) throw new InvalidOperationException("no cores");
        foreach (var c in cores)
        {
            c.Lp.Sort();
            c.L3 = l3.FindIndex(set => set.Contains(c.Lp[0]));
        }
        // APIC ids: CPUID on each logical processor, on a short-lived thread so no other thread's affinity changes.
        // Without them the Windows order is kept.
        try
        {
            var apic = new Dictionary<int, long>();
            var all = cores.SelectMany(c => c.Lp).ToList();
            var t = new Thread(() => ReadApic(all, apic, gbase)) { IsBackground = true };
            t.Start();
            if (!t.Join(5000)) throw new TimeoutException("APIC read");
            foreach (var c in cores)
                c.Apic = c.Lp.Where(apic.ContainsKey).Select(l => apic[l]).DefaultIfEmpty(-1).Min();
            if (cores.All(c => c.Apic >= 0))
                cores = cores.OrderBy(c => c.Apic).ToList();
        }
        catch (Exception ex) { Error = "apic: " + ex.Message; }
        return cores;
    }

    static void ReadApic(List<int> lps, Dictionary<int, long> apic, int[] gbase)
    {
        if (!System.Runtime.Intrinsics.X86.X86Base.IsSupported) return;
        int maxLeaf = System.Runtime.Intrinsics.X86.X86Base.CpuId(0, 0).Eax;
        var th = GetCurrentThread();
        foreach (var lp in lps)
        {
            int g = gbase.Length - 1;
            while (g > 0 && lp < gbase[g]) g--;
            var aff = new GROUP_AFFINITY { Mask = (UIntPtr)(1UL << (lp - gbase[g])), Group = (ushort)g };
            if (!SetThreadGroupAffinity(th, ref aff, out _)) continue;
            Thread.Sleep(0);                                  // the scheduler moves this thread onto that processor
            long id = maxLeaf >= 0xB
                ? (uint)System.Runtime.Intrinsics.X86.X86Base.CpuId(0xB, 0).Edx                   // x2APIC id
                : (System.Runtime.Intrinsics.X86.X86Base.CpuId(1, 0).Ebx >> 24) & 0xFF;          // initial APIC id
            apic[lp] = id;
        }
    }

    public static void Write(Utf8JsonWriter w, string name)
    {
        try
        {
            if (Vendor != null || name != null)
            {
                w.WriteStartObject("cpu_info");
                if (name != null) w.WriteString("name", name);
                if (Vendor != null) w.WriteString("vendor", Vendor);
                if (Family >= 0) { w.WriteNumber("family", Family); w.WriteNumber("model", Model); w.WriteNumber("stepping", Stepping); }
                w.WriteEndObject();
            }
            if (Cores != null)
            {
                w.WriteStartArray("topo");
                foreach (var c in Cores)
                {
                    w.WriteStartObject();
                    w.WriteStartArray("lp");
                    foreach (var l in c.Lp) w.WriteNumberValue(l);
                    w.WriteEndArray();
                    w.WriteBoolean("smt", c.Smt);
                    w.WriteNumber("eff", c.Eff);
                    w.WriteNumber("l3", c.L3);
                    if (c.Apic >= 0) w.WriteNumber("apic", c.Apic);
                    w.WriteEndObject();
                }
                w.WriteEndArray();
            }
            if (Error != null) w.WriteString("topo_error", Error);
        }
        catch { }
    }
}
