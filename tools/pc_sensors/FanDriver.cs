// FanDriver (2026-11-22): sets the NCT6701D fan headers from fan_req.json written by the dashboard once a second;
// hands every header back to the BIOS curve when the requests go stale, on exit, and while FanControl runs.
// For Passive / Off the dashboard also sends "fast": the curve of its rad rule. A thread here applies it as a FLOOR
// from the CPU package temperature and power read straight from the CPU 4x a second (owner 2026-11-22: "ramp instant
// to load and high temps"; through the dashboard it took ~4-7 s). Headers get max(request, floor).
using System.Diagnostics;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;
using LibreHardwareMonitor.PawnIo;

sealed class FanDriver
{
    const double STALE_S = 15;              // requests older than this -> every header back to the BIOS curve
    readonly string _path;
    readonly Dictionary<int, ISensor> _controls = new();
    readonly Dictionary<int, double> _applied = new();
    readonly object _lock = new();
    Dictionary<int, double> _want;          // last valid request
    Fast _fast;                             // curve from the dashboard, null = no floor (other programs / modes)
    readonly Dictionary<int, double> _floor = new();
    public string Mode = "idle";
    public double FastT = double.NaN, FastW = double.NaN;
    public string FloorWhy = "";
    long _lastEval;
    public volatile bool Stop;

    // The mode's fan rules, sent by the dashboard in fan_req.json "fast" (2026-11-22: every mode that runs the case
    // fans). Each rule: src (cpu_t, cpu_w, gpu_hot, gpu_mem, gpu_w, load_w = cpu + gpu W, hot = max(cpu_t, gpu_hot),
    // vrm_t, water), lo / hi (start / full), min (% at the start), top (share of the rule's value for the top fan);
    // "c" + "per_c" instead of lo / hi = 60 % + per_c per °C over c; "slow_pct_s" + "instant_w" = rises at slow_pct_s
    // %/s unless the load (cpu + gpu W) is at least instant_w (water roof: no load, slow; load, instant).
    sealed class Rule
    {
        public string Src;
        public double Lo, Hi, Min = 30, TopK = 0.7, C = double.NaN, PerC, Slow = double.NaN, InstantW = double.NaN;
        public double State;                // slow rules: current value
    }

    sealed class Fast
    {
        public int[] Rad, Top;
        public List<Rule> Rules = new();
        public double TopSub = 100, TopMin, Water = double.NaN, GpuK = 1.0;
    }

    public FanDriver(string dir, Computer pc)
    {
        _path = Path.Combine(dir, "fan_req.json");
        foreach (var h in pc.Hardware)
            foreach (var sh in h.SubHardware)
                foreach (var s in sh.Sensors)
                    if (s.SensorType == SensorType.Control && s.Control != null && sh.Name.Contains("NCT67"))
                        _controls[s.Index] = s;
        new Thread(FastLoop) { IsBackground = true, Name = "fan floor", Priority = ThreadPriority.Highest }.Start();
    }

    static long _fcChecked;
    static bool _fcRunning;
    static bool FanControlRunning()                       // process list once every 10 s, not every second
    {
        if (_fcChecked != 0 && Stopwatch.GetElapsedTime(_fcChecked).TotalSeconds < 10) return _fcRunning;
        _fcChecked = Stopwatch.GetTimestamp();
        _fcRunning = Process.GetProcessesByName("FanControl").Length > 0 ||
                     Process.GetProcessesByName("FanControl.Service").Length > 0;
        return _fcRunning;
    }

    public void Release()
    {
        lock (_lock)
        {
            foreach (var ch in _applied.Keys.ToList())
                try { _controls[ch].Control.SetDefault(); } catch { }
            _applied.Clear();
            _want = null;
        }
    }

    void Apply()                                          // under _lock: max(request, floor) on every requested header
    {
        if (_want == null) return;
        foreach (var kv in _want)
        {
            if (!_controls.TryGetValue(kv.Key, out var s)) continue;
            double v = Math.Max(kv.Value, _floor.TryGetValue(kv.Key, out var f) ? f : 0);
            if (_applied.TryGetValue(kv.Key, out var was) && Math.Abs(was - v) < 0.5) continue;
            try { s.Control.SetSoftware((float)v); _applied[kv.Key] = v; } catch { }
        }
    }

    public void Update()
    {
        Dictionary<int, double> want = null;
        Fast fast = null;
        try
        {
            if (File.Exists(_path))
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(_path));
                var r = doc.RootElement;
                double t = r.GetProperty("t").GetDouble();
                if (DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - t <= STALE_S)
                {
                    want = new();
                    foreach (var p in r.GetProperty("duty").EnumerateObject())
                        want[int.Parse(p.Name)] = Math.Clamp(p.Value.GetDouble(), 0, 100);
                    if (r.TryGetProperty("fast", out var f) && f.ValueKind == JsonValueKind.Object)
                    {
                        double D(JsonElement e, string k, double dflt) =>
                            e.TryGetProperty(k, out var x) && x.ValueKind == JsonValueKind.Number ? x.GetDouble() : dflt;
                        fast = new Fast
                        {
                            Rad = f.GetProperty("rad").EnumerateArray().Select(x => x.GetInt32()).ToArray(),
                            Top = f.GetProperty("top").EnumerateArray().Select(x => x.GetInt32()).ToArray(),
                            TopSub = D(f, "top_sub", 100), TopMin = D(f, "top_min", 0), Water = D(f, "water", double.NaN),
                            GpuK = D(f, "gpu_k", 1.0),
                        };
                        foreach (var e in f.GetProperty("rules").EnumerateArray())
                            fast.Rules.Add(new Rule
                            {
                                Src = e.GetProperty("src").GetString(), Lo = D(e, "lo", 0), Hi = D(e, "hi", 1),
                                Min = D(e, "min", 30), TopK = D(e, "top", 0.7), C = D(e, "c", double.NaN),
                                PerC = D(e, "per_c", 0), Slow = D(e, "slow_pct_s", double.NaN),
                                InstantW = D(e, "instant_w", double.NaN),
                            });
                        lock (_lock)                      // keep the slow rules' state across updates
                            if (_fast != null)
                                foreach (var nr in fast.Rules)
                                {
                                    var old = _fast.Rules.FirstOrDefault(o => o.Src == nr.Src && o.Lo == nr.Lo);
                                    if (old != null) nr.State = old.State;
                                }
                    }
                }
            }
        }
        catch { want = null; }

        if (want == null) { Release(); Mode = File.Exists(_path) ? "stale (BIOS curves)" : "idle (BIOS curves)"; return; }
        if (FanControlRunning()) { Release(); Mode = "blocked: FanControl is running"; return; }
        lock (_lock)
        {
            _want = want;
            _fast = fast;
            if (fast == null) _floor.Clear();
            Apply();
            foreach (var ch in _applied.Keys.Where(c => !want.ContainsKey(c)).ToList())
            {
                try { _controls[ch].Control.SetDefault(); } catch { }
                _applied.Remove(ch);
            }
        }
        Mode = fast != null ? "active + instant floor" : "active";
    }

    void FastLoop()
    {
        IntelMsr msr;
        try { msr = new IntelMsr(); } catch { return; }
        var core0 = GroupAffinity.Single(0, 0);
        bool Rd(uint i, out ulong v)
        {
            v = 0;
            try { bool ok = msr.ReadMsr(i, out uint a, out uint d, core0); v = ((ulong)d << 32) | a; return ok; }
            catch { return false; }
        }
        double tj = Rd(0x1A2, out var tt) ? (tt >> 16) & 0xFF : 100;                       // TjMax
        double eu = Rd(0x606, out var pu) ? Math.Pow(0.5, (pu >> 8) & 0x1F) : 1.0 / 16384;  // J per RAPL count
        ulong lastE = 0;
        long lastT = 0;
        while (!Stop)
        {
            try
            {
                if (Rd(0x1B1, out var ts)) FastT = tj - ((ts >> 16) & 0x7F);                   // package readout
                if (Rd(0x611, out var e))
                {
                    long now = Stopwatch.GetTimestamp();
                    if (lastT != 0)
                    {
                        double dt = (now - lastT) / (double)Stopwatch.Frequency;
                        uint de = unchecked((uint)e - (uint)lastE);                            // 32-bit, wraps
                        if (dt > 0) FastW = de * eu / dt;
                    }
                    lastE = e;
                    lastT = now;
                }
                Fast f = _fast;
                if (f != null)
                {
                    long nowT = Stopwatch.GetTimestamp();
                    double dt = _lastEval == 0 ? 0.25 : Math.Min(2.0, (nowT - _lastEval) / (double)Stopwatch.Frequency);
                    _lastEval = nowT;
                    double gpuW = double.IsNaN(Telemetry.GpuW) ? 0 : Telemetry.GpuW * f.GpuK;
                    double load = (double.IsNaN(FastW) ? 0 : FastW) + gpuW;
                    double In(string src) => src switch
                    {
                        "cpu_t" => FastT, "cpu_w" => FastW, "gpu_hot" => Telemetry.GpuHot, "gpu_mem" => Telemetry.GpuMem,
                        "gpu_w" => gpuW, "load_w" => load, "vrm_t" => Telemetry.VrmT, "water" => f.Water,
                        "hot" => Math.Max(double.IsNaN(FastT) ? 0 : FastT, double.IsNaN(Telemetry.GpuHot) ? 0 : Telemetry.GpuHot),
                        _ => double.NaN,
                    };
                    double rad = 0, top = 0;
                    var why = "";
                    foreach (var rule in f.Rules)
                    {
                        double x = In(rule.Src);
                        if (double.IsNaN(x)) continue;
                        double v;
                        if (!double.IsNaN(rule.C))
                            v = x > rule.C ? Math.Min(100, 60 + rule.PerC * (x - rule.C)) : 0;
                        else
                            v = x <= rule.Lo ? 0 : rule.Min + (100 - rule.Min) * Math.Clamp((x - rule.Lo) / (rule.Hi - rule.Lo), 0, 1);
                        if (!double.IsNaN(rule.Slow))       // slow rise unless loaded, immediate fall
                        {
                            bool instant = !double.IsNaN(rule.InstantW) && load >= rule.InstantW;
                            rule.State = v <= rule.State || instant ? v : Math.Min(v, Math.Max(rule.State, rule.Min) + rule.Slow * dt);
                            if (v > 0 && rule.State < rule.Min) rule.State = rule.Min;
                            v = rule.State;
                        }
                        if (v > rad) { rad = v; why = rule.Src; }
                        top = Math.Max(top, v * rule.TopK);
                    }
                    if (rad > 0) top = Math.Max(Math.Max(top, f.TopMin), rad - f.TopSub);
                    lock (_lock)
                    {
                        if (_fast != null)
                        {
                            foreach (var ch in f.Rad) _floor[ch] = rad;
                            foreach (var ch in f.Top) _floor[ch] = top;
                            FloorWhy = rad > 0 ? why : "";
                            Apply();
                        }
                    }
                }
            }
            catch { }
            Thread.Sleep(250);
        }
        try { msr.Close(); } catch { }
    }

    public void Write(Utf8JsonWriter w)
    {
        w.WriteStartObject("fans");
        w.WriteString("mode", Mode);
        if (!double.IsNaN(FastT)) w.WriteNumber("fast_pkg_t", FastT);
        if (!double.IsNaN(FastW)) w.WriteNumber("fast_pkg_w", Math.Round(FastW, 1));
        w.WriteString("floor_why", FloorWhy);
        w.WriteStartArray("channels");
        foreach (var k in _controls.Keys.OrderBy(k => k)) w.WriteNumberValue(k);
        w.WriteEndArray();
        lock (_lock)
        {
            w.WriteStartObject("applied");
            foreach (var kv in _applied) w.WriteNumber(kv.Key.ToString(), Math.Round(kv.Value, 1));
            w.WriteEndObject();
            w.WriteStartObject("floor");
            foreach (var kv in _floor) w.WriteNumber(kv.Key.ToString(), Math.Round(kv.Value, 1));
            w.WriteEndObject();
        }
        w.WriteEndObject();
    }
}
