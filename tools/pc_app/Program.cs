// PCS desktop app (2026-11-22, owner): the PCS dashboard in its own window with a tray icon, instead of a browser tab.
// The service (pcs_dash.py) keeps running on its own; this is only the front end. Portable: it talks to the service
// over HTTP (default http://127.0.0.1:8570, settings.json in %LOCALAPPDATA%\PCS), nothing of this PC is hard-coded.
//   PCS.exe          open the window
//   PCS.exe --tray   start in the tray only (used for "Start with Windows")
using System.Net.Http;
using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using Microsoft.Win32;

namespace PcsApp;

static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        using var mutex = new Mutex(true, @"Local\PCS-desktop-app", out bool first);
        if (!first) { Ipc.SignalShow(); return; }          // second start: bring the running one to the front
        Log.Write($"start {Environment.ProcessPath} {string.Join(' ', args)} (app {typeof(Program).Assembly.GetName().Version}, " +
                  $"{Environment.OSVersion}, {Environment.ProcessorCount} CPUs)");
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
        Application.ThreadException += (_, e) => Log.Write("UI exception: " + e.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, e) => Log.Write("unhandled: " + e.ExceptionObject);
        TaskScheduler.UnobservedTaskException += (_, e) => { Log.Write("task exception: " + e.Exception); e.SetObserved(); };
        ApplicationConfiguration.Initialize();
        Application.Run(new AppContext(args.Contains("--tray")));
    }
}

/// Settings next to the WebView2 profile in %LOCALAPPDATA%\PCS (per user, not in the exe folder).
sealed class Settings
{
    public string Url { get; set; } = "http://127.0.0.1:8570";
    public int X { get; set; } = -1;
    public int Y { get; set; } = -1;
    public int W { get; set; } = 1400;
    public int H { get; set; } = 900;
    public bool Maximized { get; set; }
    public bool CloseToTray { get; set; } = true;
    public string StartPage { get; set; } = "/home";

    public static string Dir => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PCS");
    static string File_ => Path.Combine(Dir, "settings.json");

    public static Settings Load()
    {
        try { if (File.Exists(File_)) return JsonSerializer.Deserialize<Settings>(File.ReadAllText(File_)) ?? new(); }
        catch { }
        return new();
    }
    public void Save()
    {
        try { Directory.CreateDirectory(Dir); File.WriteAllText(File_, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true })); }
        catch { }
    }
}

/// Diagnostics (2026-11-23: the standalone build hung on a friend's PC): %LOCALAPPDATA%\PCS\app.log, < 1 MB.
static class Log
{
    static readonly object _lock = new();
    public static string File_ => Path.Combine(Settings.Dir, "app.log");
    public static void Write(string msg)
    {
        try
        {
            lock (_lock)
            {
                Directory.CreateDirectory(Settings.Dir);
                var fi = new FileInfo(File_);
                if (fi.Exists && fi.Length > 1_000_000) File.Move(File_, File_ + ".old", true);
                File.AppendAllText(File_, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff ") + msg + Environment.NewLine);
            }
        }
        catch { }
    }

    /// Logs when the window thread doesn't answer for > 3 s (and when it's back), so "Not responding" shows up here.
    public static void WatchUi(SynchronizationContext ui)
    {
        new Thread(() =>
        {
            while (true)
            {
                var done = new ManualResetEventSlim(false);
                var t0 = DateTime.Now;
                ui.Post(_ => done.Set(), null);
                if (!done.Wait(3000))
                {
                    Write("UI thread blocked > 3 s");
                    done.Wait();
                    Write($"UI thread back after {(DateTime.Now - t0).TotalSeconds:0.0} s");
                }
                Thread.Sleep(2000);
            }
        }) { IsBackground = true, Name = "ui-watch" }.Start();
    }
}

/// "Show yourself" from a second start: a named event the first instance waits on.
static class Ipc
{
    const string Name = @"Local\PCS-desktop-app-show";
    public static void SignalShow()
    {
        try { using var e = EventWaitHandle.OpenExisting(Name); e.Set(); } catch { }
    }
    public static void Listen(Action onShow)
    {
        var e = new EventWaitHandle(false, EventResetMode.AutoReset, Name);
        new Thread(() => { while (true) { e.WaitOne(); onShow(); } }) { IsBackground = true }.Start();
    }
}

/// Start with Windows: HKCU Run key (per user, no admin, works on any PC).
static class Autostart
{
    const string Key = @"Software\Microsoft\Windows\CurrentVersion\Run", Value = "PCS";
    static string Cmd => $"\"{Environment.ProcessPath}\" --tray";
    public static bool Enabled
    {
        get { using var k = Registry.CurrentUser.OpenSubKey(Key); return k?.GetValue(Value) is string s && s.Length > 0; }
        set
        {
            using var k = Registry.CurrentUser.CreateSubKey(Key);
            if (value) k.SetValue(Value, Cmd); else k.DeleteValue(Value, false);
        }
    }
    public static void RefreshPath()                     // the exe moved: keep the entry pointing at this copy
    {
        using var k = Registry.CurrentUser.OpenSubKey(Key, true);
        if (k?.GetValue(Value) is string s && s.Length > 0 && s != Cmd) k.SetValue(Value, Cmd);
    }
}

// Standalone package (2026-11-23, owner: a build for a friend's PC): service\PcsService.exe next to PCS.exe is the
// monitor-only service (PyInstaller build of pcs_dash.py). The app starts it hidden when nothing answers on the service
// address, and stops the one it started on Exit. On owner's PC there is no such file: the logon task runs the service.
static class BundledService
{
    static System.Diagnostics.Process _proc;
    public static string Exe => Path.Combine(Path.GetDirectoryName(Environment.ProcessPath) ?? "", "service", "PcsService.exe");
    public static bool Present => File.Exists(Exe);

    public static async Task EnsureRunning(HttpClient http, string url, bool wait = false)
    {
        if (!Present) return;
        try { using var r = await http.GetAsync(url.TrimEnd('/') + "/status"); if (r.IsSuccessStatusCode) return; } catch { }
        if (_proc != null && !_proc.HasExited) return;
        if (_proc != null) Log.Write($"service exited (code {_proc.ExitCode}), restarting");
        try
        {
            _proc = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(Exe, "--no-browser")
            {
                UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Path.GetDirectoryName(Exe),
            });
            Log.Write($"service started, pid {_proc?.Id}");
        }
        catch (Exception ex) { Log.Write("service start failed: " + ex.Message); return; }
        var t0 = DateTime.Now;
        for (int i = 0; wait && i < 40; i++)                // up to 20 s for the first answer
        {
            await Task.Delay(500);
            try
            {
                using var r = await http.GetAsync(url.TrimEnd('/') + "/status");
                if (r.IsSuccessStatusCode) { Log.Write($"service answered after {(DateTime.Now - t0).TotalSeconds:0.0} s"); return; }
            }
            catch { }
        }
        if (wait) Log.Write("service did not answer within 20 s");
    }

    public static void Stop()
    {
        try { if (_proc != null && !_proc.HasExited) { _proc.Kill(true); _proc.WaitForExit(3000); } } catch { }
    }
}

// Updates (2026-11-23, owner: push updates out remotely, the app looks for them; GitHub). Only in the installed package:
// update.json next to PCS.exe names the public releases repo ({"repo": "owner/name"}); without it nothing is checked.
// Latest release: tag vX.Y.Z, assets PCS-Setup-X.Y.Z.exe + PCS-Setup-X.Y.Z.exe.sha256. Checked 1 min after start and every
// 6 h. Installing = download, SHA-256 must match the .sha256 asset, then the setup runs with /update (Windows asks for
// administrator rights), stops PCS, replaces the files and starts PCS again.
sealed class Updater
{
    public string Repo, State = "idle", Version, Notes, Error, Url, ShaUrl;
    public int Pct;
    public long Checked;
    public event Action Changed;
    readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(60) };
    readonly Version _me = typeof(Updater).Assembly.GetName().Version ?? new Version(0, 0, 0);

    public Updater()
    {
        try
        {
            var f = Path.Combine(Path.GetDirectoryName(Environment.ProcessPath) ?? "", "update.json");
            if (File.Exists(f))
            {
                using var d = JsonDocument.Parse(File.ReadAllText(f));
                if (d.RootElement.TryGetProperty("repo", out var r) && r.GetString() is string s && System.Text.RegularExpressions.Regex.IsMatch(s, @"^[\w.-]+/[\w.-]+$"))
                    Repo = s;
            }
        }
        catch (Exception ex) { Log.Write("update.json: " + ex.Message); }
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("PCS-app/" + _me.ToString(3));
        _http.DefaultRequestHeaders.Accept.ParseAdd("application/vnd.github+json");
    }
    public bool Enabled => Repo != null;

    void Set(string state) { State = state; try { Changed?.Invoke(); } catch { } }

    public async Task Check()
    {
        if (!Enabled || State is "checking" or "downloading") return;
        Set("checking");
        try
        {
            using var r = await _http.GetAsync($"https://api.github.com/repos/{Repo}/releases/latest");
            if (r.StatusCode == System.Net.HttpStatusCode.NotFound) { Checked = DateTimeOffset.UtcNow.ToUnixTimeSeconds(); Set("idle"); return; }   // no release yet
            r.EnsureSuccessStatusCode();
            using var d = JsonDocument.Parse(await r.Content.ReadAsStringAsync());
            var tag = d.RootElement.GetProperty("tag_name").GetString()?.TrimStart('v', 'V') ?? "";
            Checked = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            if (!System.Version.TryParse(tag, out var v) || Cmp(v, _me) <= 0) { Set("idle"); return; }
            string exe = null, sha = null;
            foreach (var a in d.RootElement.GetProperty("assets").EnumerateArray())
            {
                var n = a.GetProperty("name").GetString() ?? "";
                var u = a.GetProperty("browser_download_url").GetString();
                if (n.Equals($"PCS-Setup-{tag}.exe", StringComparison.OrdinalIgnoreCase)) exe = u;
                if (n.Equals($"PCS-Setup-{tag}.exe.sha256", StringComparison.OrdinalIgnoreCase)) sha = u;
            }
            if (exe == null || sha == null) { Error = "release " + tag + " has no setup / checksum"; Set("error"); return; }
            Version = tag; Url = exe; ShaUrl = sha;
            Notes = d.RootElement.TryGetProperty("body", out var b) ? b.GetString() : null;
            Log.Write($"update available: {tag}");
            Set("available");
        }
        catch (Exception ex) { Error = ex.Message; Log.Write("update check: " + ex.Message); Set("error"); }
    }

    static int Cmp(Version a, Version b) => new Version(a.Major, a.Minor, Math.Max(0, a.Build)).CompareTo(new Version(b.Major, b.Minor, Math.Max(0, b.Build)));

    /// downloads, checks, starts the setup; returns false (with Error) when it did not start
    public async Task<bool> Install()
    {
        if (State != "available" || Url == null) return false;
        Set("downloading");
        try
        {
            var dir = Path.Combine(Path.GetTempPath(), "PCS-update");
            Directory.CreateDirectory(dir);
            var file = Path.Combine(dir, $"PCS-Setup-{Version}.exe");
            var want = (await _http.GetStringAsync(ShaUrl)).Trim().Split(' ', '\t', '\n')[0].ToLowerInvariant();
            if (want.Length != 64) throw new Exception("bad checksum file");
            using (var r = await _http.GetAsync(Url, HttpCompletionOption.ResponseHeadersRead))
            {
                r.EnsureSuccessStatusCode();
                long total = r.Content.Headers.ContentLength ?? 0, got = 0;
                await using var src = await r.Content.ReadAsStreamAsync();
                await using var dst = File.Create(file);
                var buf = new byte[1 << 16];
                int n, last = -1;
                while ((n = await src.ReadAsync(buf)) > 0)
                {
                    await dst.WriteAsync(buf.AsMemory(0, n));
                    got += n;
                    Pct = total > 0 ? (int)(got * 100 / total) : 0;
                    if (Pct / 5 != last) { last = Pct / 5; Set("downloading"); }
                }
            }
            string have;
            using (var fs = File.OpenRead(file)) have = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(fs)).ToLowerInvariant();
            if (have != want) { File.Delete(file); throw new Exception("checksum mismatch: download damaged or not the published file"); }
            Log.Write($"update {Version} downloaded, checksum ok, starting setup");
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(file, "/update") { UseShellExecute = true, Verb = "runas" });
            return true;
        }
        catch (System.ComponentModel.Win32Exception) { Error = "administrator rights were declined"; Set("available"); return false; }
        catch (Exception ex) { Error = ex.Message; Log.Write("update install: " + ex.Message); Set("error"); return false; }
    }

    public static Updater Current;
    public object Info() => !Enabled ? null : new { state = State, version = Version, notes = Notes, error = Error, pct = Pct, @checked = Checked };
}

sealed class AppContext : ApplicationContext
{
    readonly Settings _cfg = Settings.Load();
    readonly NotifyIcon _tray;
    readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(3) };
    readonly System.Windows.Forms.Timer _poll = new() { Interval = 3000 };
    readonly SynchronizationContext _ui;
    MainForm _form;
    JsonElement? _status;
    bool _busy;

    // tray menu: program choices as the page's buttons (POST /prog {"p": ...})
    static readonly (string id, string label)[] Programs =
    {
        ("passive", "Passive"), ("daily", "Daily"), ("oc", "Overclocking"),
        ("auto", "Auto"), ("bench", "Benchmark"), ("off", "Off"),
    };
    readonly Dictionary<string, ToolStripMenuItem> _progItems = new();
    readonly ToolStripMenuItem _statusItem, _autostartItem, _updItem;
    readonly Updater _upd = Updater.Current = new Updater();
    readonly System.Windows.Forms.Timer _updTimer = new() { Interval = 60_000 };
    string _balloonFor, _toldVersion;

    public AppContext(bool tray)
    {
        _ui = SynchronizationContext.Current ?? new WindowsFormsSynchronizationContext();
        Autostart.RefreshPath();
        var menu = new ContextMenuStrip();
        _statusItem = new ToolStripMenuItem("Connecting…") { Enabled = false };
        menu.Items.Add(_statusItem);
        menu.Items.Add(new ToolStripSeparator());
        foreach (var (id, label) in BundledService.Present ? Array.Empty<(string, string)>() : Programs)   // monitor build: no programs
        {
            var it = new ToolStripMenuItem(label) { Tag = id };
            it.Click += async (_, _) => await SelectProgram(id, label);
            _progItems[id] = it;
            menu.Items.Add(it);
        }
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Open PCS", null, (_, _) => ShowWindow(""));
        menu.Items.Add("Dashboard", null, (_, _) => ShowWindow("/"));
        menu.Items.Add("Analyzer", null, (_, _) => ShowWindow("/analyzer"));
        menu.Items.Add("Settings", null, (_, _) => ShowWindow("/settings"));
        _autostartItem = new ToolStripMenuItem("Start with Windows") { CheckOnClick = true, Checked = Autostart.Enabled };
        _autostartItem.Click += (_, _) => { try { Autostart.Enabled = _autostartItem.Checked; } catch (Exception ex) { Notify("Start with Windows", ex.Message); } };
        menu.Items.Add(_autostartItem);
        _updItem = new ToolStripMenuItem("Update") { Visible = false, Font = new Font(menu.Font, FontStyle.Bold) };
        _updItem.Click += async (_, _) => await DoUpdate();
        menu.Items.Add(_updItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Exit", null, (_, _) => ExitApp());
        menu.Opening += (_, _) => { _autostartItem.Checked = Autostart.Enabled; _ = Poll(); };

        _tray = new NotifyIcon { Icon = AppIcon.Get(), Text = "PCS", Visible = true, ContextMenuStrip = menu };
        _tray.MouseClick += (_, e) => { if (e.Button == MouseButtons.Left) ShowWindow(null); };

        _poll.Tick += async (_, _) => await Poll();
        _poll.Start();
        Ipc.Listen(() => _ui.Post(_ => ShowWindow(null), null));
        Log.WatchUi(_ui);
        if (_upd.Enabled)                                  // installed package: GitHub releases, 1 min after start, then every 6 h
        {
            _upd.Changed += () => _ui.Post(_ => UpdateChanged(), null);
            _tray.BalloonTipClicked += async (_, _) => { if (_balloonFor == "update") await DoUpdate(); };
            _updTimer.Tick += async (_, _) => { _updTimer.Interval = 6 * 3600_000; await _upd.Check(); };
            _updTimer.Start();
        }
        _ = Startup(tray);
    }

    async Task Startup(bool tray)
    {
        await BundledService.EnsureRunning(_http, _cfg.Url, wait: true);   // standalone package: service first, then the page
        await Poll();
        if (!tray) ShowWindow("");
    }

    // Settings page → app (WebView2 message): autostart, closeToTray, startPage, url
    void OnAppSetting(string key, JsonElement value)
    {
        try
        {
            switch (key)
            {
                case "autostart": Autostart.Enabled = value.GetBoolean(); _autostartItem.Checked = Autostart.Enabled; break;
                case "closeToTray": _cfg.CloseToTray = value.GetBoolean(); break;
                case "startPage": var sp = value.GetString(); if (sp is "/home" or "/") _cfg.StartPage = sp; break;
                case "url":
                    var u = value.GetString();
                    if (Uri.TryCreate(u, UriKind.Absolute, out var uri) && uri.Scheme is "http" or "https") _cfg.Url = u.TrimEnd('/');
                    break;
            }
            _cfg.Save();
        }
        catch (Exception ex) { Notify("PCS settings", ex.Message); }
    }

    void ShowWindow(string path)
    {
        if (_form == null || _form.IsDisposed)
        {
            _form = new MainForm(_cfg, OnAppSetting, AppCommand);
            _form.FormClosing += (_, e) =>
            {
                if (e.CloseReason == CloseReason.UserClosing && _cfg.CloseToTray && !_exiting)
                {
                    e.Cancel = true;
                    _form.Hide();
                }
            };
        }
        if (path != null) _form.Navigate(path);
        _form.Show();
        if (_form.WindowState == FormWindowState.Minimized) _form.WindowState = _cfg.Maximized ? FormWindowState.Maximized : FormWindowState.Normal;
        _form.Activate();
        _form.BringToFront();
    }

    bool _exiting;
    void ExitApp()
    {
        _exiting = true;
        _poll.Stop();
        _form?.SaveBounds();
        _form?.Close();
        _tray.Visible = false;
        _tray.Dispose();
        BundledService.Stop();
        ExitThread();
    }

    void UpdateChanged()
    {
        bool av = _upd.State == "available";
        _updItem.Visible = av || _upd.State == "downloading";
        _updItem.Enabled = av;
        _updItem.Text = _upd.State == "downloading" ? $"Downloading PCS {_upd.Version}… {_upd.Pct} %" : $"Update to PCS {_upd.Version}";
        if (av && _toldVersion != _upd.Version)
        {
            _toldVersion = _upd.Version;
            Notify($"PCS {_upd.Version} is available", "Click here to update. PCS restarts by itself.", "update");
        }
        if (_upd.State == "error" && _upd.Error != null && _upd.Url != null) Notify("PCS update failed", _upd.Error);
        _form?.SendAppStateIfReady();
    }

    async Task DoUpdate()
    {
        if (await _upd.Install()) ExitApp();               // the setup stops what is left and starts PCS again
        else if (_upd.Error != null) Notify("PCS update", _upd.Error);
    }

    public void AppCommand(string cmd)
    {
        if (cmd == "app-update") _ = DoUpdate();
        else if (cmd == "app-check-update") _ = _upd.Check();
    }

    void Notify(string title, string text, string tag = null)
    {
        _balloonFor = tag;
        _tray.BalloonTipTitle = title;
        _tray.BalloonTipText = text;
        _tray.ShowBalloonTip(4000);
    }

    async Task SelectProgram(string id, string label)
    {
        try
        {
            var body = new StringContent(JsonSerializer.Serialize(new { p = id }), Encoding.UTF8, "application/json");
            using var r = await _http.PostAsync(_cfg.Url.TrimEnd('/') + "/prog", body);
            var txt = await r.Content.ReadAsStringAsync();
            string msg = null;
            try { using var d = JsonDocument.Parse(txt); if (d.RootElement.TryGetProperty("msg", out var m)) msg = m.GetString(); } catch { }
            if (!r.IsSuccessStatusCode) Notify("PCS: " + label + " refused", msg ?? txt);
        }
        catch (Exception ex) { Notify("PCS: service not reachable", ex.Message); }
        await Poll();
    }

    async Task Poll()
    {
        if (_busy) return;
        _busy = true;
        try
        {
            await BundledService.EnsureRunning(_http, _cfg.Url);       // restarts the bundled service if it died
            var txt = await _http.GetStringAsync(_cfg.Url.TrimEnd('/') + "/status");
            using var d = JsonDocument.Parse(txt);
            _status = d.RootElement.Clone();
        }
        catch { _status = null; }
        finally { _busy = false; }
        UpdateTray();
    }

    static string Num(JsonElement s, string k, string fmt = "0.0") =>
        s.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble().ToString(fmt) : "--";
    static string Str(JsonElement s, string k) =>
        s.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    void UpdateTray()
    {
        string prog = null;
        string tip, line;
        if (_status is not JsonElement s)
        {
            tip = "PCS: service not running";
            line = "Service not running (http " + _cfg.Url + ")";
        }
        else
        {
            prog = Str(s, "program");
            var label = Programs.FirstOrDefault(p => p.id == prog).label ?? prog ?? "?";
            bool conn = s.TryGetProperty("connected", out var c) && c.ValueKind == JsonValueKind.True;
            int faults = s.TryGetProperty("faults", out var f) && f.ValueKind == JsonValueKind.Number ? f.GetInt32() : 0;
            line = conn ? $"{label} · water {Num(s, "T2")} °C · DP {Num(s, "DP")} °C" : $"{label} · controller not connected";
            tip = conn ? $"PCS {label}\nMix {Num(s, "T2")} °C  Return {Num(s, "T1")} °C\nDew point {Num(s, "DP")} °C" + (faults != 0 ? "\nFAULT" : "")
                       : $"PCS {label}\nController not connected";
        }
        _tray.Text = tip.Length > 127 ? tip[..127] : tip;
        _statusItem.Text = line;
        foreach (var (id, it) in _progItems)
        {
            it.Checked = id == prog;
            it.Enabled = _status != null;
        }
    }
}

sealed class MainForm : Form
{
    readonly Settings _cfg;
    readonly Action<string, JsonElement> _onSetting;
    readonly Action<string> _onCmd;
    readonly WebView2 _web = new() { Dock = DockStyle.Fill };
    string _pending = "";
    bool _ready;

    public MainForm(Settings cfg, Action<string, JsonElement> onSetting, Action<string> onCmd)
    {
        _cfg = cfg;
        _onSetting = onSetting;
        _onCmd = onCmd;
        Text = "PCS";
        Icon = AppIcon.Get();
        BackColor = Color.FromArgb(16, 18, 22);
        StartPosition = FormStartPosition.Manual;
        var area = Screen.FromPoint(new Point(Math.Max(cfg.X, 0), Math.Max(cfg.Y, 0))).WorkingArea;
        Size = new Size(Math.Clamp(cfg.W, 600, 10000), Math.Clamp(cfg.H, 400, 10000));
        Location = cfg.X >= 0 && area.Contains(cfg.X + 50, cfg.Y + 20) ? new Point(cfg.X, cfg.Y)
                 : new Point(area.Left + (area.Width - Width) / 2, area.Top + (area.Height - Height) / 2);
        if (cfg.Maximized) WindowState = FormWindowState.Maximized;
        Controls.Add(_web);
        ResizeEnd += (_, _) => SaveBounds();
        FormClosing += (_, _) => SaveBounds();
        _ = Init();
    }

    async Task Init()
    {
        try
        {
            var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(Settings.Dir, "WebView2"));
            await _web.EnsureCoreWebView2Async(env);
            var w = _web.CoreWebView2;
            w.Settings.IsStatusBarEnabled = false;
            w.Settings.AreDefaultContextMenusEnabled = true;
            w.Settings.IsZoomControlEnabled = true;
            w.DocumentTitleChanged += (_, _) => Text = string.IsNullOrEmpty(w.DocumentTitle) ? "PCS" : "PCS · " + w.DocumentTitle;
            w.NewWindowRequested += (_, e) =>                 // links that open a new tab stay in this window
            {
                e.Handled = true;
                if (Uri.TryCreate(e.Uri, UriKind.Absolute, out var u) && u.Host is "127.0.0.1" or "localhost") w.Navigate(e.Uri);
                else System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(e.Uri) { UseShellExecute = true });
            };
            Log.Write($"WebView2 {env.BrowserVersionString} ready");
            w.NavigationCompleted += (_, e) =>
            {
                if (!e.IsSuccess) { Log.Write($"navigation failed: {e.WebErrorStatus} {w.Source}"); ShowOffline(); }
            };
            // a page that hangs (script stuck) or a crashed renderer: log it and load the page again
            w.ProcessFailed += (_, e) =>
            {
                Log.Write($"WebView2 process failed: {e.ProcessFailedKind} {e.Reason} exit {e.ExitCode} {e.ProcessDescription} at {w.Source}");
                if (e.ProcessFailedKind is CoreWebView2ProcessFailedKind.RenderProcessUnresponsive
                    or CoreWebView2ProcessFailedKind.RenderProcessExited)
                    BeginInvoke(() => { try { w.Reload(); } catch { } });
            };
            w.WebMessageReceived += (_, e) => OnMessage(e.WebMessageAsJson);
            _ready = true;
            Navigate(_pending);
        }
        catch (Exception ex)
        {
            Log.Write("WebView2 init failed: " + ex);
            Controls.Clear();
            Controls.Add(new Label
            {
                Dock = DockStyle.Fill, ForeColor = Color.White, TextAlign = ContentAlignment.MiddleCenter, Font = new Font("Segoe UI", 11),
                Text = "The Microsoft Edge WebView2 Runtime is needed (it comes with Windows 11).\n\n" + ex.Message
            });
        }
    }

    // Settings page bridge: {"cmd":"app-get"} → {"type":"app",...}; {"cmd":"app-set","key":..,"value":..}
    void OnMessage(string json)
    {
        try
        {
            using var d = JsonDocument.Parse(json);
            var r = d.RootElement;
            var cmd = r.TryGetProperty("cmd", out var c) ? c.GetString() : null;
            // only the PCS service's own pages may change app settings
            if (!Uri.TryCreate(_web.CoreWebView2.Source, UriKind.Absolute, out var src) ||
                !src.GetLeftPart(UriPartial.Authority).Equals(new Uri(_cfg.Url).GetLeftPart(UriPartial.Authority), StringComparison.OrdinalIgnoreCase))
                return;
            if (cmd == "theme")
            {
                bool dark = r.TryGetProperty("mode", out var md) && md.GetString() == "dark";
                TitleBar.Apply(Handle, dark, ParseColor(r, "bg"), ParseColor(r, "fg"));
                BackColor = ParseColor(r, "bg") ?? BackColor;
                return;
            }
            if (cmd is "app-update" or "app-check-update") { _onCmd(cmd); SendAppState(); return; }
            if (cmd == "app-set" && r.TryGetProperty("key", out var k) && r.TryGetProperty("value", out var v))
                _onSetting(k.GetString(), v.Clone());
            if (cmd is "app-get" or "app-set") SendAppState();
        }
        catch { }
    }

    static Color? ParseColor(JsonElement r, string key)
    {
        if (!r.TryGetProperty(key, out var e) || e.ValueKind != JsonValueKind.String) return null;
        var t = e.GetString()?.Trim();
        if (t == null || !t.StartsWith("#") || (t.Length != 7 && t.Length != 4)) return null;
        try { return ColorTranslator.FromHtml(t); } catch { return null; }
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        TitleBar.Apply(Handle, TitleBar.WindowsIsDark(), null, null);   // until the page reports its own theme
    }

    public void SendAppStateIfReady() { if (_ready) try { SendAppState(); } catch { } }

    void SendAppState()
    {
        var ver = typeof(MainForm).Assembly.GetName().Version;
        _web.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(new
        {
            type = "app", autostart = Autostart.Enabled, closeToTray = _cfg.CloseToTray, startPage = _cfg.StartPage,
            url = _cfg.Url, version = ver == null ? null : $"{ver.Major}.{ver.Minor}.{ver.Build}",
            update = Updater.Current?.Info(),
        }));
    }

    public void Navigate(string path)
    {
        if (path == null) return;
        if (path == "") path = string.IsNullOrEmpty(_cfg.StartPage) ? "/home" : _cfg.StartPage;
        _pending = path;
        if (!_ready) return;
        _web.CoreWebView2.Navigate(_cfg.Url.TrimEnd('/') + path);
    }

    void ShowOffline()
    {
        var url = _cfg.Url.TrimEnd('/');
        _web.CoreWebView2.NavigateToString($$"""
            <html><body style="background:#101216;color:#e8e8ea;font:15px Segoe UI;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
            <div style="text-align:center"><div style="font-size:22px;font-weight:600;margin-bottom:10px">PCS service not running</div>
            <div style="opacity:.7">Nothing answers at {{url}}. Start the PCS service, this page retries every 5 s.</div></div>
            <script>setInterval(()=>fetch('{{url}}/status',{mode:'no-cors'}).then(()=>location.href='{{url}}{{(_cfg.StartPage ?? "/home")}}').catch(()=>{}),5000)</script>
            </body></html>
            """);
    }

    public void SaveBounds()
    {
        _cfg.Maximized = WindowState == FormWindowState.Maximized;
        if (WindowState == FormWindowState.Normal) { _cfg.X = Left; _cfg.Y = Top; _cfg.W = Width; _cfg.H = Height; }
        _cfg.Save();
    }
}

static class AppIcon
{
    static Icon _icon;
    public static Icon Get()
    {
        if (_icon != null) return _icon;
        using var s = typeof(AppIcon).Assembly.GetManifestResourceStream("PcsApp.pcs.ico");
        _icon = s != null ? new Icon(s) : SystemIcons.Application;
        return _icon;
    }
}

/// Title bar colours (Windows 10 1809+ dark mode, Windows 11 caption / text colour). Follows the page theme.
static class TitleBar
{
    [System.Runtime.InteropServices.DllImport("dwmapi.dll")]
    static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);
    const int DWMWA_USE_IMMERSIVE_DARK_MODE = 20, DWMWA_CAPTION_COLOR = 35, DWMWA_TEXT_COLOR = 36;

    public static bool WindowsIsDark()
    {
        try
        {
            using var k = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
            return k?.GetValue("AppsUseLightTheme") is int v && v == 0;
        }
        catch { return false; }
    }

    static int Ref(Color c) => c.R | (c.G << 8) | (c.B << 16);    // COLORREF 0x00BBGGRR

    public static void Apply(IntPtr hwnd, bool dark, Color? caption, Color? text)
    {
        if (hwnd == IntPtr.Zero) return;
        int d = dark ? 1 : 0;
        DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ref d, 4);
        if (caption is Color c) { int v = Ref(c); DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR, ref v, 4); }
        if (text is Color t) { int v = Ref(t); DwmSetWindowAttribute(hwnd, DWMWA_TEXT_COLOR, ref v, 4); }
    }
}
