// Reader v2 (2026-11-22): what LibreHardwareMonitor's sensor list does not give, read through the same PawnIO driver.
//   MsrReader  - per core effective clock (APERF / wall time), thermal throttling (IA32_THERM_STATUS bit 0),
//                package / IA / ring limit reasons (0x1B1, 0x64F, 0x6B1), ring ratio (0x621).
//   EcReader   - ASUS embedded controller: a raw dump of banks 0 and 1 (512 registers) every EC_DUMP_S while
//                ec_dump.on exists next to now.json (for mapping), and the registers named in ec_map.json every second.
//   FanDriver  - sets the NCT6701D fan headers from fan_req.json written by the dashboard; hands every header back to
//                the BIOS curve when the requests go stale, on exit, and while FanControl is running (never fights it).
using System.Diagnostics;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;
using LibreHardwareMonitor.Hardware.Motherboard.Lpc.EC;
using LibreHardwareMonitor.PawnIo;

// Global\PCS_SENSORS, 4 MB: [u32 seq][u32 length][JSON]. seq is odd while a write is in progress; a reader copies the
// JSON and accepts it only when seq was even and unchanged across the copy (sequence lock, no OS lock needed).
static class Shm
{
    const string NAME = "Global\\PCS_SENSORS";
    const int SIZE = 4 << 20;
    static System.IO.MemoryMappedFiles.MemoryMappedFile _mmf;
    static System.IO.MemoryMappedFiles.MemoryMappedViewAccessor _view;
    static uint _seq;
    static IntPtr _handle;                                // kept open: the block lives while the reader runs
    public static string Error;

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    struct SECURITY_ATTRIBUTES { public int nLength; public IntPtr lpSecurityDescriptor; public int bInheritHandle; }

    [System.Runtime.InteropServices.DllImport("advapi32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(string sddl, uint rev, out IntPtr sd, IntPtr size);

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    static extern IntPtr CreateFileMappingW(IntPtr file, ref SECURITY_ATTRIBUTES sa, uint protect, uint sizeHigh, uint sizeLow, string name);

    public static void Publish(byte[] json)
    {
        try
        {
            if (_view == null)
            {
                // the reader runs elevated: by default only administrators could open the block, so create it with
                // read for authenticated users (the dashboard runs non-elevated), full access for SYSTEM / admins
                if (!ConvertStringSecurityDescriptorToSecurityDescriptorW("D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GR;;;AU)", 1,
                                                                          out var sd, IntPtr.Zero))
                    throw new Exception("SDDL " + System.Runtime.InteropServices.Marshal.GetLastWin32Error());
                var sa = new SECURITY_ATTRIBUTES { nLength = System.Runtime.InteropServices.Marshal.SizeOf<SECURITY_ATTRIBUTES>(),
                                                   lpSecurityDescriptor = sd, bInheritHandle = 0 };
                _handle = CreateFileMappingW(new IntPtr(-1), ref sa, 0x04 /* PAGE_READWRITE */, 0, SIZE, NAME);
                if (_handle == IntPtr.Zero)
                    throw new Exception("CreateFileMapping " + System.Runtime.InteropServices.Marshal.GetLastWin32Error());
                _mmf = System.IO.MemoryMappedFiles.MemoryMappedFile.OpenExisting(NAME,
                    System.IO.MemoryMappedFiles.MemoryMappedFileRights.ReadWrite);
                _view = _mmf.CreateViewAccessor();
            }
            if (json.Length > SIZE - 8) return;
            _view.Write(0, ++_seq | 1u);                 // odd: writing
            Thread.MemoryBarrier();
            _view.Write(4, (uint)json.Length);
            _view.WriteArray(8, json, 0, json.Length);
            Thread.MemoryBarrier();
            _seq = (_seq | 1u) + 1;                       // even: done
            _view.Write(0, _seq);
            Error = null;
        }
        catch (Exception ex) { Error = ex.Message; }
    }
}

static class Telemetry
{
    public static volatile float CpuW = float.NaN, Vcore = float.NaN;   // set by the main loop, read by the EC thread
    public static volatile float GpuHot = float.NaN, GpuW = float.NaN, GpuMem = float.NaN, VrmT = float.NaN;   // fan floor inputs
}

// Windows idle states per logical CPU (2026-11-23, roadblock item 2): PDH "\Processor(*)\% C1 / C2 / C3 Time". These are
// the ACPI C-states Windows asks for (_CST), not the hardware C6 / C10 counters (PawnIO's MSR allow list lacks those);
// on this board C3 is the deepest state Windows offers. % of the last interval, null until the second sample.
sealed class OsIdle
{
    [System.Runtime.InteropServices.DllImport("pdh.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    static extern uint PdhOpenQueryW(string src, IntPtr user, out IntPtr q);
    [System.Runtime.InteropServices.DllImport("pdh.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    static extern uint PdhAddEnglishCounterW(IntPtr q, string path, IntPtr user, out IntPtr c);
    [System.Runtime.InteropServices.DllImport("pdh.dll")]
    static extern uint PdhCollectQueryData(IntPtr q);
    [System.Runtime.InteropServices.DllImport("pdh.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    static extern uint PdhGetFormattedCounterArrayW(IntPtr c, uint fmt, ref uint size, out uint count, IntPtr buf);

    const uint PDH_FMT_DOUBLE = 0x200, PDH_FMT_NOCAP100 = 0x8000, PDH_MORE_DATA = 0x800007D2;
    readonly IntPtr _q;
    readonly IntPtr[] _c = new IntPtr[3];
    public readonly double[][] Pct;              // [state C1..C3][logical cpu]
    public string Error;

    public OsIdle(int n)
    {
        Pct = new double[3][];
        for (int k = 0; k < 3; k++) Pct[k] = Enumerable.Repeat(double.NaN, n).ToArray();
        try
        {
            if (PdhOpenQueryW(null, IntPtr.Zero, out _q) != 0) { Error = "PdhOpenQuery"; _q = IntPtr.Zero; return; }
            for (int k = 0; k < 3; k++)
                if (PdhAddEnglishCounterW(_q, $@"\Processor(*)\% C{k + 1} Time", IntPtr.Zero, out _c[k]) != 0)
                    Error = $"PdhAddCounter C{k + 1}";
            PdhCollectQueryData(_q);
        }
        catch (Exception ex) { Error = ex.Message; _q = IntPtr.Zero; }
    }

    public void Update()
    {
        if (_q == IntPtr.Zero) return;
        if (PdhCollectQueryData(_q) != 0) return;
        for (int k = 0; k < 3; k++)
        {
            if (_c[k] == IntPtr.Zero) continue;
            uint size = 0;
            if (PdhGetFormattedCounterArrayW(_c[k], PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, ref size, out _, IntPtr.Zero) != PDH_MORE_DATA || size == 0) continue;
            var buf = System.Runtime.InteropServices.Marshal.AllocHGlobal((int)size);
            try
            {
                if (PdhGetFormattedCounterArrayW(_c[k], PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, ref size, out var count, buf) != 0) continue;
                int stride = IntPtr.Size == 8 ? 24 : 20;   // { wchar* name; { u32 status; [pad]; double } }
                int valOff = IntPtr.Size == 8 ? 16 : 12;
                for (int j = 0; j < count; j++)
                {
                    var item = buf + j * stride;
                    var name = System.Runtime.InteropServices.Marshal.PtrToStringUni(System.Runtime.InteropServices.Marshal.ReadIntPtr(item));
                    if (!int.TryParse(name, out var cpu) || cpu < 0 || cpu >= Pct[k].Length) continue;   // skips _Total
                    uint st = (uint)System.Runtime.InteropServices.Marshal.ReadInt32(item + IntPtr.Size);
                    double v = BitConverter.Int64BitsToDouble(System.Runtime.InteropServices.Marshal.ReadInt64(item + valOff));
                    Pct[k][cpu] = st <= 1 ? Math.Clamp(v, 0, 100) : double.NaN;   // PDH_CSTATUS_VALID_DATA / NEW_DATA
                }
            }
            finally { System.Runtime.InteropServices.Marshal.FreeHGlobal(buf); }
        }
    }
}

sealed class MsrReader
{
    const uint IA32_MPERF = 0xE7, IA32_APERF = 0xE8, IA32_THERM_STATUS = 0x19C, IA32_PACKAGE_THERM_STATUS = 0x1B1;
    const uint CORE_PERF_LIMIT_REASONS = 0x64F, RING_PERF_LIMIT_REASONS = 0x6B1, UNCORE_PERF_STATUS = 0x621;
    // Core C0 residency (2026-11-22, owner: why 60x needs package C10): MPERF ticks at the TSC rate only in C0, so
    // C0 % = MPERF delta / (wall time x TSC rate), TSC rate = PLATFORM_INFO (0xCE) max non-turbo ratio x 100 MHz.
    // The deeper residency counters (core C1 0x660 / C6 0x3FD / C7 0x3FE, package 0x60D..0x632) and the TSC MSR read
    // back 0 through PawnIO (not on its MSR allow list), so only C0 is available here.
    const uint PLATFORM_INFO = 0xCE;
    double _tscHz = double.NaN;
    readonly long[] _cstTick;
    readonly ulong[] _mperf;
    public readonly double[] C0;

    readonly IntelMsr _msr;
    readonly OsIdle _os;
    readonly int _n;
    readonly ulong[] _aperf;
    readonly long[] _tick;
    public readonly double[] EffMhz;
    public readonly int[] Throttle;
    public long LimIa = -1, LimRing = -1, PkgTherm = -1, RingRatio = -1;
    public readonly Dictionary<string, bool> Readable = new();
    public string Error;

    public MsrReader(bool intel)
    {
        _n = Environment.ProcessorCount;
        _aperf = new ulong[_n];
        _tick = new long[_n];
        EffMhz = Enumerable.Repeat(double.NaN, _n).ToArray();
        Throttle = Enumerable.Repeat(-1, _n).ToArray();
        _cstTick = new long[_n]; _mperf = new ulong[_n];
        C0 = Enumerable.Repeat(double.NaN, _n).ToArray();
        if (!intel) Error = "not an Intel CPU: MSR block off";
        else
        {
            try { _msr = new IntelMsr(); }
            catch (Exception ex) { Error = "IntelMsr: " + ex.Message; }
        }
        _os = new OsIdle(_n);
    }

    bool Read(uint index, int cpu, out ulong v)
    {
        v = 0;
        if (_msr == null) return false;
        bool ok;
        try { ok = _msr.ReadMsr(index, out uint eax, out uint edx, GroupAffinity.Single(0, cpu)); v = ((ulong)edx << 32) | eax; }
        catch { ok = false; }
        var key = "0x" + index.ToString("X");
        Readable[key] = Readable.TryGetValue(key, out var was) ? was || ok : ok;
        return ok;
    }

    public void Update()
    {
        try { _os.Update(); } catch (Exception ex) { _os.Error = ex.Message; }
        if (_msr == null) return;
        for (int i = 0; i < _n; i++)
        {
            var t = Stopwatch.GetTimestamp();
            if (Read(IA32_APERF, i, out var a))
            {
                if (_tick[i] != 0 && a >= _aperf[i])
                {
                    double s = (t - _tick[i]) / (double)Stopwatch.Frequency;
                    EffMhz[i] = s > 0 ? (a - _aperf[i]) / s / 1e6 : double.NaN;
                }
                _aperf[i] = a; _tick[i] = t;
            }
            Throttle[i] = Read(IA32_THERM_STATUS, i, out var th) ? (int)(th & 1) : -1;
            Residency(i);
        }
        LimIa = Read(CORE_PERF_LIMIT_REASONS, 0, out var li) ? (long)(li & 0xFFFF) : -1;
        LimRing = Read(RING_PERF_LIMIT_REASONS, 0, out var lr) ? (long)(lr & 0xFFFF) : -1;
        PkgTherm = Read(IA32_PACKAGE_THERM_STATUS, 0, out var pt) ? (long)(pt & 0xFFFF) : -1;
        RingRatio = Read(UNCORE_PERF_STATUS, 0, out var rr) ? (long)(rr & 0x7F) : -1;
    }

    static double Pct(ulong now, ulong was, ulong dt) => was != 0 && dt > 0 && now >= was ? Math.Min(100, 100.0 * (now - was) / dt) : double.NaN;

    void Residency(int i)
    {
        if (double.IsNaN(_tscHz))
        {
            if (!Read(PLATFORM_INFO, 0, out var pi) || ((pi >> 8) & 0xFF) == 0) return;
            _tscHz = ((pi >> 8) & 0xFF) * 100e6;
        }
        long tick = Stopwatch.GetTimestamp();
        bool first = _cstTick[i] == 0;
        ulong dt = first ? 0 : (ulong)((tick - _cstTick[i]) / (double)Stopwatch.Frequency * _tscHz);
        if (Read(IA32_MPERF, i, out var m)) { C0[i] = first ? double.NaN : Pct(m, _mperf[i], dt); _mperf[i] = m; }
        _cstTick[i] = tick;
    }

    static void Arr(Utf8JsonWriter w, string name, double[] a)
    {
        w.WriteStartArray(name);
        foreach (var x in a) { if (double.IsNaN(x)) w.WriteNullValue(); else w.WriteNumberValue(Math.Round(x, 1)); }
        w.WriteEndArray();
    }

    public void Write(Utf8JsonWriter w)
    {
        w.WriteStartObject("msr");
        if (Error != null) w.WriteString("error", Error);
        w.WriteStartArray("eclk");
        foreach (var x in EffMhz) { if (double.IsNaN(x)) w.WriteNullValue(); else w.WriteNumberValue(Math.Round(x, 1)); }
        w.WriteEndArray();
        w.WriteStartArray("thr");
        foreach (var x in Throttle) w.WriteNumberValue(x);
        w.WriteEndArray();
        w.WriteNumber("lim_ia", LimIa);
        w.WriteNumber("lim_ring", LimRing);
        w.WriteNumber("pkg_therm", PkgTherm);
        w.WriteNumber("ring_ratio", RingRatio);
        w.WriteStartObject("cst");          // % of the last interval, per logical CPU (= core on Arrow Lake)
        if (!double.IsNaN(_tscHz)) w.WriteNumber("tsc_mhz", _tscHz / 1e6);
        Arr(w, "c0", C0);
        Arr(w, "c1", _os.Pct[0]);          // Windows idle states (ACPI C1 / C2 / C3), 2026-11-23
        Arr(w, "c2", _os.Pct[1]);
        Arr(w, "c3", _os.Pct[2]);
        if (_os.Error != null) w.WriteString("os_error", _os.Error);
        w.WriteEndObject();
        w.WriteStartObject("readable");
        foreach (var kv in Readable) w.WriteBoolean(kv.Key, kv.Value);
        w.WriteEndObject();
        w.WriteEndObject();
    }
}

sealed class EcReader
{
    const double EC_DUMP_S = 5;
    readonly string _dir;
    long _lastDump;
    public byte[] Dump;                     // last full dump (banks 0 and 1), null until the first one
    public double DumpAge = double.NaN;
    public string Error;
    List<(string name, ushort reg, int size, double factor, double offset, bool signed)> _map = new();
    DateTime _mapTime;
    public readonly Dictionary<string, double> Values = new();

    public EcReader(string dir) { _dir = dir; }

    void LoadMap()
    {
        var path = Path.Combine(_dir, "ec_map.json");
        try
        {
            if (!File.Exists(path)) { _map.Clear(); return; }
            var t = File.GetLastWriteTimeUtc(path);
            if (t == _mapTime) return;
            _mapTime = t;
            var list = new List<(string, ushort, int, double, double, bool)>();
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            foreach (var e in doc.RootElement.EnumerateArray())
                list.Add((e.GetProperty("name").GetString(),
                          Convert.ToUInt16(e.GetProperty("reg").GetString(), 16),
                          e.TryGetProperty("size", out var sz) ? sz.GetInt32() : 1,
                          e.TryGetProperty("factor", out var f) ? f.GetDouble() : 1.0,
                          e.TryGetProperty("offset", out var o) ? o.GetDouble() : 0.0,
                          e.TryGetProperty("signed", out var sg) && sg.GetBoolean()));
            _map = list;
        }
        catch (Exception ex) { Error = "ec_map.json: " + ex.Message; }
    }

    // The EC answers slowly and is shared with the BIOS / HWiNFO: a 512-register dump in one go stalled the main loop
    // for 10-20 s (2026-11-22). So it runs on its own thread, in chunks of CHUNK registers with a pause between, and
    // the main loop only copies the last results.
    const int CHUNK = 32;
    readonly object _lock = new();
    byte[] _sweep = new byte[0x200];
    int _sweepPos;
    Thread _thread;
    public volatile bool Stop;
    public long Passes, SlowMs;

    public void Start()
    {
        _thread = new Thread(Loop) { IsBackground = true, Name = "EC", Priority = ThreadPriority.AboveNormal };   // it holds the shared bus lock: never let a load starve it (fan writes waited 5-9 s at BelowNormal)
        _thread.Start();
    }

    byte[] ReadRegs(ushort[] regs)
    {
        var data = new byte[regs.Length];
        var t = Stopwatch.GetTimestamp();
        using (var io = new WindowsEmbeddedControllerIO()) io.Read(regs, data);
        SlowMs = Math.Max(SlowMs, (long)Stopwatch.GetElapsedTime(t).TotalMilliseconds);
        return data;
    }

    void Loop()
    {
        while (!Stop)
        {
            var t0 = Stopwatch.GetTimestamp();
            try
            {
                LoadMap();
                // mapped registers, every second
                if (_map.Count > 0)
                {
                    var regs = _map.SelectMany(m => Enumerable.Range(m.reg, m.size).Select(r => (ushort)r)).Distinct().OrderBy(r => r).ToArray();   // ascending: the bank switch expects it
                    var data = ReadRegs(regs);
                    var at = new Dictionary<ushort, byte>();
                    for (int i = 0; i < regs.Length; i++) at[regs[i]] = data[i];
                    var vals = Decode(at);
                    lock (_lock) { Values.Clear(); foreach (var kv in vals) Values[kv.Key] = kv.Value; }
                    if (vals.TryGetValue("vrm_t", out var vt)) Telemetry.VrmT = (float)vt;
                }
                // mapping dump: one chunk per pass, a full sweep of banks 0 and 1 every 0x200 / CHUNK passes.
                // ec_scan.on also logs each chunk with the CPU power / voltage of that second (ec_scan.csv), so the
                // registers that move with the load (VRM out, current, power) can be found without HWiNFO.
                bool scan = File.Exists(Path.Combine(_dir, "ec_scan.on"));
                if (scan || File.Exists(Path.Combine(_dir, "ec_dump.on")))
                {
                    var regs = Enumerable.Range(_sweepPos, CHUNK).Select(r => (ushort)r).ToArray();
                    float w0 = Telemetry.CpuW, v0 = Telemetry.Vcore;
                    var data = ReadRegs(regs);
                    if (scan)
                        File.AppendAllText(Path.Combine(_dir, "ec_scan.csv"),
                            $"{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0:F2},{_sweepPos},{Convert.ToHexString(data)}," +
                            $"{w0:F2},{v0:F4},{Telemetry.CpuW:F2},{Telemetry.Vcore:F4}\n");
                    Array.Copy(data, 0, _sweep, _sweepPos, CHUNK);
                    _sweepPos += CHUNK;
                    if (_sweepPos >= 0x200)
                    {
                        lock (_lock) { Dump = (byte[])_sweep.Clone(); _lastDump = Stopwatch.GetTimestamp(); }
                        _sweepPos = 0;
                    }
                }
                Error = null;
                Passes++;
            }
            catch (Exception ex) { Error = "EC: " + ex.GetType().Name + " " + ex.Message; }
            var used = Stopwatch.GetElapsedTime(t0).TotalMilliseconds;
            Thread.Sleep((int)Math.Max(100, 330 - used));           // ~3 chunks a second: a sweep every ~5 s
        }
    }

    public void Update() { }                                       // all the work is on the EC thread

    Dictionary<string, double> Decode(Dictionary<ushort, byte> at)
    {
        var values = new Dictionary<string, double>();
        foreach (var m in _map)
        {
            long raw = 0;
            bool ok = true;
            for (int k = 0; k < m.size; k++)
            {
                if (!at.TryGetValue((ushort)(m.reg + k), out var b)) { ok = false; break; }
                raw = (raw << 8) | b;                        // ASUS EC: big endian
            }
            if (!ok) continue;
            if (m.signed && raw >= 1L << (8 * m.size - 1)) raw -= 1L << (8 * m.size);
            values[m.name] = raw * m.factor + m.offset;
        }
        return values;
    }

    public void Write(Utf8JsonWriter w)
    {
        w.WriteStartObject("ec");
        if (Error != null) w.WriteString("error", Error);
        w.WriteNumber("passes", Passes);
        w.WriteNumber("slowest_ms", SlowMs);
        lock (_lock)
        {
            w.WriteStartObject("v");
            foreach (var kv in Values) w.WriteNumber(kv.Key, Math.Round(kv.Value, 3));
            w.WriteEndObject();
            if (Dump != null)
            {
                w.WriteString("dump", Convert.ToHexString(Dump));
                w.WriteNumber("dump_age_s", Math.Round(Stopwatch.GetElapsedTime(_lastDump).TotalSeconds, 1));
            }
        }
        w.WriteEndObject();
    }
}

