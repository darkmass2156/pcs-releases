// UserFans (2026-11-23, owner: other PCs must be able to control their fans, rename them and bind them to a sensor,
// "just as fast as on my PC"). Portable fan control for the standalone package, started with --user-fans.
// owner's PC keeps FanDriver (fan_req.json from the dashboard); the two never run together.
//
// Config: fans.json next to now.json, written by the service (Fans tab):
//   { "enabled": true,
//     "groups": [ { "id", "name", "headers": [control sensor ids], "src", "lo", "hi", "min", "max", "relax" } ],
//     "identify": { "id": <control id>, "until": <unix s> },        // spin one header at 100 % to find it
//     "names": { <control id>: "Front intake" } }                     // display only, the reader ignores it
// src: cpu_t, cpu_w, gpu_t, gpu_hot, gpu_mem, gpu_w, load_w (CPU + GPU W), hot (max CPU / GPU hot spot),
//      or "sensor:<LHM id>" (any temperature: SSD, chipset, board, ...). Duty = min up to lo, linear to max at hi.
// Fast path: a thread updates the CPU 4x a second (CPU temp / power are what change fastest), applies the rise at once
// and lowers at "relax" %/s. GPU and other sensors come from the main loop (1 s).
// Fail-safe: a group whose source has no value for 5 s, a disabled / missing / broken config, FanControl running,
// and the reader's exit all hand the headers back to the BIOS (SetDefault). A config is persistent: fans keep working
// without the app or the service, as long as the reader task runs.
using System.Diagnostics;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;

static class HwLock
{
    public static readonly object Cpu = new();        // CPU hardware Update(): main loop and the fan thread
}

sealed class UserFans
{
    const double LOST_S = 5;
    readonly string _path;
    readonly Computer _pc;
    readonly IHardware _cpu, _gpu;
    readonly Dictionary<string, ISensor> _controls = new();     // control id -> sensor
    readonly Dictionary<string, ISensor> _byId = new();         // every sensor by id (sources)
    readonly Dictionary<string, double> _applied = new();
    readonly Dictionary<string, string> _why = new();
    readonly object _lock = new();
    List<Group> _groups = new();
    bool _enabled;
    string _identId; double _identUntil;
    DateTime _cfgTime = DateTime.MinValue;
    public string Mode = "starting";
    public string CfgError;
    public double CpuT = double.NaN, CpuW = double.NaN;
    public volatile bool Stop;

    sealed class Group
    {
        public string Id, Name, Src;
        public string[] Headers = Array.Empty<string>();
        public double Lo = 40, Hi = 80, Min = 30, Max = 100, Relax = 0.4;
        public double Duty = double.NaN, Value = double.NaN;
        public long LastGood;
    }

    static readonly string[] CpuTempNames = { "Core (Tctl/Tdie)", "Core (Tctl)", "Tctl", "CPU Package", "Package", "Core (Tdie)", "Core Max" };
    static readonly string[] CpuPowerNames = { "Package", "CPU Package" };

    public UserFans(string dir, Computer pc)
    {
        _path = Path.Combine(dir, "fans.json");
        _pc = pc;
        _cpu = Pick.Cpu(pc);
        _gpu = Pick.Gpu(pc);
        foreach (var h in pc.Hardware)
        {
            Add(h);
            foreach (var sh in h.SubHardware) Add(sh);
        }
        new Thread(Loop) { IsBackground = true, Name = "user fans", Priority = ThreadPriority.Highest }.Start();
    }

    void Add(IHardware h)
    {
        bool board = h.HardwareType == HardwareType.SuperIO || h.HardwareType == HardwareType.EmbeddedController;
        bool gpu = h.HardwareType is HardwareType.GpuNvidia or HardwareType.GpuAmd or HardwareType.GpuIntel;
        foreach (var s in h.Sensors)
        {
            _byId[s.Identifier.ToString()] = s;
            if (s.SensorType == SensorType.Control && s.Control != null && (board || gpu))
                _controls[s.Identifier.ToString()] = s;
        }
    }

    static long _fcChecked;
    static bool _fcRunning;
    static bool OtherFanTool()                             // process list once every 10 s
    {
        if (_fcChecked != 0 && Stopwatch.GetElapsedTime(_fcChecked).TotalSeconds < 10) return _fcRunning;
        _fcChecked = Stopwatch.GetTimestamp();
        _fcRunning = Process.GetProcessesByName("FanControl").Length > 0 || Process.GetProcessesByName("FanControl.Service").Length > 0;
        return _fcRunning;
    }

    void LoadConfig()
    {
        DateTime mt;
        try { mt = File.Exists(_path) ? File.GetLastWriteTimeUtc(_path) : DateTime.MinValue; } catch { return; }
        if (mt == _cfgTime) return;
        _cfgTime = mt;
        if (mt == DateTime.MinValue) { lock (_lock) { _enabled = false; _groups = new(); } CfgError = null; return; }
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(_path));
            var r = doc.RootElement;
            double D(JsonElement e, string k, double d) => e.TryGetProperty(k, out var x) && x.ValueKind == JsonValueKind.Number ? x.GetDouble() : d;
            string S(JsonElement e, string k) => e.TryGetProperty(k, out var x) && x.ValueKind == JsonValueKind.String ? x.GetString() : null;
            var groups = new List<Group>();
            if (r.TryGetProperty("groups", out var ga) && ga.ValueKind == JsonValueKind.Array)
                foreach (var g in ga.EnumerateArray())
                {
                    var ng = new Group
                    {
                        Id = S(g, "id") ?? Guid.NewGuid().ToString("n"), Name = S(g, "name") ?? "", Src = S(g, "src") ?? "cpu_t",
                        Headers = g.TryGetProperty("headers", out var hs) && hs.ValueKind == JsonValueKind.Array
                            ? hs.EnumerateArray().Where(x => x.ValueKind == JsonValueKind.String).Select(x => x.GetString()).ToArray() : Array.Empty<string>(),
                        Lo = D(g, "lo", 40), Hi = D(g, "hi", 80),
                        Min = Math.Clamp(D(g, "min", 30), 0, 100), Max = Math.Clamp(D(g, "max", 100), 0, 100),
                        Relax = Math.Clamp(D(g, "relax", 0.4), 0.05, 20),
                    };
                    if (ng.Hi <= ng.Lo) ng.Hi = ng.Lo + 1;
                    if (ng.Max < ng.Min) ng.Max = ng.Min;
                    groups.Add(ng);
                }
            string identId = null; double identUntil = 0;
            if (r.TryGetProperty("identify", out var id) && id.ValueKind == JsonValueKind.Object)
            { identId = S(id, "id"); identUntil = D(id, "until", 0); }
            lock (_lock)
            {
                foreach (var ng in groups)                 // keep each group's current duty across edits (no jump down)
                {
                    var old = _groups.FirstOrDefault(o => o.Id == ng.Id);
                    if (old != null) { ng.Duty = old.Duty; ng.LastGood = old.LastGood; }
                }
                _groups = groups;
                _enabled = r.TryGetProperty("enabled", out var en) && en.ValueKind == JsonValueKind.True;
                _identId = identId; _identUntil = identUntil;
            }
            CfgError = null;
        }
        catch (Exception ex) { CfgError = ex.Message; lock (_lock) { _enabled = false; } }
    }

    double Val(string id) => _byId.TryGetValue(id, out var s) && s.Value is float f && !float.IsNaN(f) && !float.IsInfinity(f) ? f : double.NaN;
    static double V(ISensor s) => s?.Value is float f && !float.IsNaN(f) && !float.IsInfinity(f) ? f : double.NaN;

    double Source(string src)
    {
        double gpuHot = Telemetry.GpuHot, gpuW = Telemetry.GpuW;
        if (double.IsNaN(gpuHot)) gpuHot = V(Pick.Find(_gpu, SensorType.Temperature, "GPU Core", "GPU Hot Spot"));
        double nz(double x) => double.IsNaN(x) ? 0 : x;
        switch (src)
        {
            case "cpu_t": return CpuT;
            case "cpu_w": return CpuW;
            case "gpu_t": return V(Pick.Find(_gpu, SensorType.Temperature, "GPU Core"));
            case "gpu_hot": return gpuHot;
            case "gpu_mem": return Telemetry.GpuMem;
            case "gpu_w": return gpuW;
            case "load_w": return double.IsNaN(CpuW) && double.IsNaN(gpuW) ? double.NaN : nz(CpuW) + nz(gpuW);
            case "hot": return double.IsNaN(CpuT) && double.IsNaN(gpuHot) ? double.NaN : Math.Max(nz(CpuT), nz(gpuHot));
        }
        if (src != null && src.StartsWith("sensor:")) return Val(src[7..]);
        return double.NaN;
    }

    void Set(string id, double v, string why)               // under _lock
    {
        if (!_controls.TryGetValue(id, out var s)) return;
        _why[id] = why;
        if (_applied.TryGetValue(id, out var was) && Math.Abs(was - v) < 0.5) return;
        try { s.Control.SetSoftware((float)v); _applied[id] = v; } catch { }
    }

    void Free(string id)                                    // under _lock: back to the BIOS / card curve
    {
        _why.Remove(id);
        if (!_applied.Remove(id)) return;
        try { _controls[id].Control.SetDefault(); } catch { }
    }

    public void Release()
    {
        lock (_lock) foreach (var id in _applied.Keys.ToList()) Free(id);
    }

    void Loop()
    {
        long last = Stopwatch.GetTimestamp();
        long cfgAt = 0;
        while (!Stop)
        {
            try
            {
                if (cfgAt == 0 || Stopwatch.GetElapsedTime(cfgAt).TotalSeconds >= 0.5) { LoadConfig(); cfgAt = Stopwatch.GetTimestamp(); }
                if (_cpu != null)
                {
                    lock (HwLock.Cpu) _cpu.Update();
                    CpuT = V(Pick.Find(_cpu, SensorType.Temperature, CpuTempNames));
                    CpuW = V(Pick.Find(_cpu, SensorType.Power, CpuPowerNames));
                }
                long now = Stopwatch.GetTimestamp();
                double dt = Math.Min(2.0, (now - last) / (double)Stopwatch.Frequency);
                last = now;
                Step(dt, now);
            }
            catch { }
            Thread.Sleep(250);
        }
    }

    void Step(double dt, long now)
    {
        lock (_lock)
        {
            if (!_enabled)
            {
                foreach (var id in _applied.Keys.ToList()) Free(id);
                Mode = CfgError != null ? "config error (BIOS curves)" : File.Exists(_path) ? "off (BIOS curves)" : "not set up (BIOS curves)";
                return;
            }
            if (OtherFanTool())
            {
                foreach (var id in _applied.Keys.ToList()) Free(id);
                Mode = "blocked: FanControl is running";
                return;
            }
            var driven = new HashSet<string>();
            double unix = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            foreach (var g in _groups)
            {
                double x = Source(g.Src);
                g.Value = x;
                if (!double.IsNaN(x)) g.LastGood = now;
                bool lost = g.LastGood == 0 || (now - g.LastGood) / (double)Stopwatch.Frequency > LOST_S;
                if (lost) { g.Duty = double.NaN; continue; }               // headers of this group go back to the BIOS
                if (!double.IsNaN(x))
                {
                    double want = g.Min + (g.Max - g.Min) * Math.Clamp((x - g.Lo) / (g.Hi - g.Lo), 0, 1);
                    g.Duty = double.IsNaN(g.Duty) || want >= g.Duty ? want : Math.Max(want, g.Duty - g.Relax * dt);
                }
                foreach (var id in g.Headers)
                {
                    if (driven.Contains(id) && _applied.TryGetValue(id, out var cur) && cur >= g.Duty) continue;   // two groups: the higher wins
                    driven.Add(id);
                    Set(id, g.Duty, g.Name);
                }
            }
            if (_identId != null && unix < _identUntil && _controls.ContainsKey(_identId))
            {
                driven.Add(_identId);
                Set(_identId, 100, "identify");
            }
            foreach (var id in _applied.Keys.Where(k => !driven.Contains(k)).ToList()) Free(id);
            Mode = driven.Count > 0 ? "active" : "on, nothing assigned (BIOS curves)";
        }
    }

    public void Write(Utf8JsonWriter w)
    {
        w.WriteStartObject("ufans");
        w.WriteString("mode", Mode);
        if (CfgError != null) w.WriteString("config_error", CfgError);
        if (!double.IsNaN(CpuT)) w.WriteNumber("cpu_t", Math.Round(CpuT, 1));
        if (!double.IsNaN(CpuW)) w.WriteNumber("cpu_w", Math.Round(CpuW, 1));
        lock (_lock)
        {
            w.WriteStartArray("controls");
            foreach (var kv in _controls.OrderBy(k => k.Key))
            {
                var s = kv.Value;
                w.WriteStartObject();
                w.WriteString("id", kv.Key);
                w.WriteString("hw", s.Hardware.Name);
                w.WriteString("name", s.Name);
                var rpm = RpmFor(s);
                if (rpm != null) { w.WriteString("rpm_id", rpm.Identifier.ToString()); if (!double.IsNaN(V(rpm))) w.WriteNumber("rpm", Math.Round(V(rpm))); }
                if (!double.IsNaN(V(s))) w.WriteNumber("duty", Math.Round(V(s), 1));
                if (_applied.TryGetValue(kv.Key, out var a)) w.WriteNumber("applied", Math.Round(a, 1));
                if (_why.TryGetValue(kv.Key, out var why)) w.WriteString("by", why);
                w.WriteBoolean("gpu", s.Hardware.HardwareType is HardwareType.GpuNvidia or HardwareType.GpuAmd or HardwareType.GpuIntel);
                w.WriteEndObject();
            }
            w.WriteEndArray();
            w.WriteStartArray("groups");
            foreach (var g in _groups)
            {
                w.WriteStartObject();
                w.WriteString("id", g.Id);
                if (!double.IsNaN(g.Duty)) w.WriteNumber("duty", Math.Round(g.Duty, 1));
                if (!double.IsNaN(g.Value)) w.WriteNumber("value", Math.Round(g.Value, 1));
                w.WriteEndObject();
            }
            w.WriteEndArray();
        }
        w.WriteEndObject();
    }

    // the rpm sensor of a control: same hardware, Fan type, same index (Super I/O), else the first fan of a GPU
    static ISensor RpmFor(ISensor c)
    {
        var fans = c.Hardware.Sensors.Where(s => s.SensorType == SensorType.Fan).ToArray();
        return fans.FirstOrDefault(s => s.Index == c.Index) ?? (fans.Length == 1 ? fans[0] : null);
    }
}
