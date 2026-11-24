// PCS setup (2026-11-23, owner: "install it without a blue cmd prompt, it looks sketchy"; updates pushed from GitHub).
//   PCS-Setup-<ver>.exe            wizard: PawnIO yes / no, start with Windows, install, open PCS
//   PCS-Setup-<ver>.exe /update    no questions: small progress window, replaces the files, restarts PCS (the app runs this)
//   Uninstall PCS.exe              (no payload) asks, then removes PCS; keeps PawnIO, asks about settings / logs
// Payload (embedded payload.zip): app\ (PCS.exe), service\ (PcsService.exe), sensors\ (PcsSensors.exe),
// PawnIO_setup.exe (official, signature checked before it runs), update.json (where the app looks for updates).
// What it does: C:\Program Files\PCS, task "PCS Sensors" (the reader, elevated at logon: --user-fans --no-ec),
// %ProgramData%\PCS\sensors (users may write fans.json there: the Fans tab), Start Menu shortcut, Apps & features entry,
// HKCU Run "--tray" when chosen. Log: %ProgramData%\PCS\setup.log.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Win32;

namespace PcsSetup
{
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            var a = args.Select(x => x.ToLowerInvariant().TrimStart('/', '-')).ToArray();
            try
            {
                if (!Installer.HasPayload || a.Contains("uninstall")) { Application.Run(new UninstallForm(a.Contains("quiet"))); return 0; }
                Application.Run(new SetupForm(a.Contains("update")));
                return 0;
            }
            catch (Exception ex)
            {
                Installer.Log("fatal: " + ex);
                MessageBox.Show(ex.Message, "PCS setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }
    }

    static class Ui
    {
        public static readonly Color Bg = Color.FromArgb(13, 17, 23), Card = Color.FromArgb(22, 27, 34), Fg = Color.FromArgb(230, 237, 243),
            Fg2 = Color.FromArgb(150, 160, 170), Accent = Color.FromArgb(59, 130, 246);
        public static Font F(float size, FontStyle st = FontStyle.Regular) => new Font("Segoe UI", size, st);
        public static Icon Icon()
        {
            using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream("pcs.ico"))
                return s != null ? new Icon(s) : SystemIcons.Application;
        }
        public static Button Btn(string text, bool primary)
        {
            var b = new Button { Text = text, FlatStyle = FlatStyle.Flat, Height = 34, Width = 120, Font = F(10, FontStyle.Bold), Cursor = Cursors.Hand,
                                 BackColor = primary ? Accent : Card, ForeColor = primary ? Color.White : Fg };
            b.FlatAppearance.BorderColor = primary ? Accent : Color.FromArgb(48, 54, 61);
            return b;
        }
        public static Label L(string text, float size, Color c, FontStyle st = FontStyle.Regular) =>
            new Label { Text = text, Font = F(size, st), ForeColor = c, AutoSize = false, BackColor = Color.Transparent };
        public static void Dark(Form f)
        {
            f.BackColor = Bg; f.ForeColor = Fg; f.Icon = Icon(); f.FormBorderStyle = FormBorderStyle.FixedSingle; f.MaximizeBox = false;
            f.StartPosition = FormStartPosition.CenterScreen; f.Font = F(10);
            f.HandleCreated += (_, __) => { int on = 1; DwmSetWindowAttribute(f.Handle, 20, ref on, 4); };
        }
        [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr h, int a, ref int v, int s);
    }

    // ------------------------------------------------------------------ install / update
    sealed class SetupForm : Form
    {
        readonly bool _update;
        readonly Panel _welcome = new Panel(), _work = new Panel(), _done = new Panel();
        readonly CheckBox _pawn, _auto;
        CheckBox _open;
        readonly ProgressBar _bar = new ProgressBar { Style = ProgressBarStyle.Continuous, Maximum = 100 };
        readonly Label _step;
        readonly Button _next, _cancel;
        string _result;

        public SetupForm(bool update)
        {
            _update = update;
            Ui.Dark(this);
            Text = update ? "Updating PCS" : "PCS setup";
            ClientSize = update ? new Size(460, 150) : new Size(560, 380);
            foreach (var p in new[] { _welcome, _work, _done }) { p.Dock = DockStyle.Fill; p.Visible = false; Controls.Add(p); }

            // welcome
            var title = Ui.L("PCS " + Installer.Version, 20, Ui.Fg, FontStyle.Bold); title.SetBounds(32, 26, 480, 40);
            var sub = Ui.L("Sensors, crash analysis and Reflex fan control for your PC.", 10.5f, Ui.Fg2); sub.SetBounds(34, 68, 480, 24);
            var what = Ui.L("Installs to " + Installer.Dir + ", a sensor reader that starts with Windows (administrator rights, " +
                            "needed to read the CPU and motherboard), and a Start Menu entry. Nothing is sent anywhere; " +
                            "PCS only checks GitHub for new versions.", 9.5f, Ui.Fg2);
            what.SetBounds(34, 104, 490, 66);
            bool hasPawn = Installer.PawnIoVersion() != null;
            _pawn = new CheckBox { Text = hasPawn ? "PawnIO driver " + Installer.PawnIoVersion() + " is installed" : "Install the PawnIO driver (signed, open source; needed for CPU and motherboard sensors and Reflex)",
                                   Checked = !hasPawn, Enabled = !hasPawn, ForeColor = Ui.Fg, Font = Ui.F(9.5f), AutoSize = false };
            _pawn.SetBounds(34, 182, 490, 40);
            _auto = new CheckBox { Text = "Start PCS with Windows (in the tray)", Checked = true, ForeColor = Ui.Fg, Font = Ui.F(9.5f), AutoSize = false };
            _auto.SetBounds(34, 226, 490, 26);
            _next = Ui.Btn("Install", true); _next.SetBounds(ClientSize.Width - 32 - 120, ClientSize.Height - 58, 120, 34);
            _cancel = Ui.Btn("Cancel", false); _cancel.SetBounds(ClientSize.Width - 32 - 250, ClientSize.Height - 58, 120, 34);
            _next.Click += (_, __) => Start();
            _cancel.Click += (_, __) => Close();
            _welcome.Controls.AddRange(new Control[] { title, sub, what, _pawn, _auto, _next, _cancel });

            // working
            _step = Ui.L(update ? "Updating PCS…" : "Installing…", 10.5f, Ui.Fg);
            _step.SetBounds(32, update ? 30 : 140, ClientSize.Width - 64, 26);
            _bar.SetBounds(32, update ? 66 : 176, ClientSize.Width - 64, 10);
            _work.Controls.AddRange(new Control[] { _step, _bar });

            if (update) { _work.Visible = true; Shown += (_, __) => Start(); }
            else _welcome.Visible = true;
        }

        async void Start()
        {
            // a "PCS Sensors" task that runs a reader from somewhere else (owner's own PC: the fan driver of his loop) is
            // not ours to replace without asking
            var other = Installer.ForeignTask();
            if (other != null)
            {
                var a = MessageBox.Show(this, "A different PCS sensor reader is already set up on this PC:\n\n" + other +
                    "\n\nInstalling replaces it with this package's reader. On a PC whose fans or water cooling depend on that " +
                    "reader, choose No.\n\nReplace it?", "PCS setup", MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2);
                if (a != DialogResult.Yes) { Close(); return; }
            }
            _welcome.Visible = false; _work.Visible = true;
            var prog = new Progress<(int, string)>(p => { _bar.Value = Math.Max(0, Math.Min(100, p.Item1)); _step.Text = p.Item2; });
            bool pawn = !_update && _pawn.Checked && _pawn.Enabled, auto = _update ? Installer.AutostartOn() : _auto.Checked;
            try
            {
                _result = await Task.Run(() => Installer.Install(prog, pawn, auto, _update));
            }
            catch (Exception ex)
            {
                Installer.Log("install failed: " + ex);
                MessageBox.Show(this, "Setup failed: " + ex.Message + "\n\nDetails: " + Installer.LogPath, "PCS setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Close();
                return;
            }
            if (_update) { Installer.LaunchAsUser(""); Close(); return; }
            ShowDone();
        }

        void ShowDone()
        {
            _work.Visible = false; _done.Visible = true;
            var t = Ui.L("PCS is installed", 20, Ui.Fg, FontStyle.Bold); t.SetBounds(32, 26, 480, 40);
            var s = Ui.L(_result ?? "", 10, Ui.Fg2); s.SetBounds(34, 74, 490, 120);
            _open = new CheckBox { Text = "Open PCS now", Checked = true, ForeColor = Ui.Fg, Font = Ui.F(10), AutoSize = false };
            _open.SetBounds(34, 214, 300, 28);
            var fin = Ui.Btn("Finish", true); fin.SetBounds(ClientSize.Width - 32 - 120, ClientSize.Height - 58, 120, 34);
            fin.Click += (_, __) => { if (_open.Checked) Installer.LaunchAsUser(""); Close(); };
            _done.Controls.AddRange(new Control[] { t, s, _open, fin });
            AcceptButton = fin;
        }
    }

    sealed class UninstallForm : Form
    {
        public UninstallForm(bool quiet)
        {
            Ui.Dark(this);
            Text = "Uninstall PCS";
            ClientSize = new Size(500, 230);
            // the uninstaller sits in the folder it deletes: run a copy from %TEMP%
            var self = Assembly.GetExecutingAssembly().Location;
            if (self.StartsWith(Installer.Dir, StringComparison.OrdinalIgnoreCase))
            {
                var tmp = Path.Combine(Path.GetTempPath(), "PCS-uninstall-" + Guid.NewGuid().ToString("n").Substring(0, 8) + ".exe");
                File.Copy(self, tmp, true);
                Process.Start(new ProcessStartInfo(tmp, "/uninstall" + (quiet ? " /quiet" : "")) { UseShellExecute = true });
                Load += (_, __) => Close();
                return;
            }
            var t = Ui.L("Remove PCS?", 18, Ui.Fg, FontStyle.Bold); t.SetBounds(28, 22, 440, 36);
            var s = Ui.L("Removes the app, the sensor reader task and the Start Menu entry. Every fan goes back to its BIOS curve. " +
                         "The PawnIO driver stays (other tools use it; remove it in Apps & features if you like).", 9.5f, Ui.Fg2);
            s.SetBounds(30, 64, 440, 60);
            var data = new CheckBox { Text = "Also delete my PCS settings, logs and fan setup", ForeColor = Ui.Fg, Font = Ui.F(9.5f), AutoSize = false };
            data.SetBounds(30, 130, 440, 26);
            var go = Ui.Btn("Remove", true); go.SetBounds(ClientSize.Width - 28 - 120, ClientSize.Height - 54, 120, 34);
            var no = Ui.Btn("Cancel", false); no.SetBounds(ClientSize.Width - 28 - 250, ClientSize.Height - 54, 120, 34);
            no.Click += (_, __) => Close();
            go.Click += async (_, __) =>
            {
                go.Enabled = no.Enabled = data.Enabled = false; go.Text = "Removing…";
                try { await Task.Run(() => Installer.Uninstall(data.Checked)); }
                catch (Exception ex) { MessageBox.Show(this, "Some parts could not be removed: " + ex.Message, "PCS", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
                MessageBox.Show(this, "PCS has been removed.", "PCS", MessageBoxButtons.OK, MessageBoxIcon.Information);
                Close();
            };
            Controls.AddRange(new Control[] { t, s, data, go, no });
            if (quiet) Shown += (_, __) => go.PerformClick();
        }
    }

    // ------------------------------------------------------------------ the work
    static class Installer
    {
        public static readonly string Dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "PCS");
        public static readonly string Data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "PCS");
        public static string Sensors => Path.Combine(Data, "sensors");
        public static string LogPath => Path.Combine(Data, "setup.log");
        const string Task_ = "PCS Sensors";
        const string UninstKey = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PCS";
        const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";

        public static string Version
        {
            get { var v = Assembly.GetExecutingAssembly().GetName().Version; return $"{v.Major}.{v.Minor}.{v.Build}"; }
        }
        public static bool HasPayload => Assembly.GetExecutingAssembly().GetManifestResourceInfo("payload.zip") != null;

        public static void Log(string msg)
        {
            try { Directory.CreateDirectory(Data); File.AppendAllText(LogPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss ") + msg + Environment.NewLine); } catch { }
        }

        public static string PawnIoVersion()
        {
            using (var k = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO"))
                return k == null ? null : (k.GetValue("DisplayVersion") as string ?? "?");
        }

        public static bool AutostartOn()
        {
            using (var k = Registry.CurrentUser.OpenSubKey(RunKey)) return k?.GetValue("PCS") is string s && s.Length > 0;
        }

        /// the command of an existing "PCS Sensors" task when it is not under our install folder, else null
        public static string ForeignTask()
        {
            try
            {
                var p = Process.Start(new ProcessStartInfo("schtasks.exe", $"/Query /TN \"{Task_}\" /XML") { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true });
                var xml = p.StandardOutput.ReadToEnd();
                p.WaitForExit(10000);
                if (p.ExitCode != 0) return null;
                var m = System.Text.RegularExpressions.Regex.Match(xml, "<Command>(.*?)</Command>");
                var cmd = m.Success ? System.Net.WebUtility.HtmlDecode(m.Groups[1].Value).Trim('"') : "?";
                return cmd.StartsWith(Dir, StringComparison.OrdinalIgnoreCase) ? null : cmd;
            }
            catch { return null; }
        }

        static void Run(string exe, string args, int timeoutMs = 60000)
        {
            var p = Process.Start(new ProcessStartInfo(exe, args) { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
                                                                    RedirectStandardOutput = true, RedirectStandardError = true });
            var o = p.StandardOutput.ReadToEndAsync(); var e = p.StandardError.ReadToEndAsync();
            p.WaitForExit(timeoutMs);
            Log($"{Path.GetFileName(exe)} {args} -> {(p.HasExited ? p.ExitCode.ToString() : "timeout")} {o.Result.Trim()} {e.Result.Trim()}");
        }

        static void StopRunning()
        {
            Run("schtasks.exe", $"/End /TN \"{Task_}\"");
            foreach (var name in new[] { "PCS", "PcsService", "PcsSensors" })
                foreach (var p in Process.GetProcessesByName(name))
                {
                    try
                    {
                        string path = null;
                        try { path = p.MainModule?.FileName; } catch { }
                        if (path == null || path.StartsWith(Dir, StringComparison.OrdinalIgnoreCase)) { p.Kill(); p.WaitForExit(5000); }
                    }
                    catch { }
                }
            Thread.Sleep(800);   // the reader hands the fans back to the BIOS on exit; let file handles go
        }

        static void Retry(Action a)
        {
            for (int i = 0; ; i++)
            {
                try { a(); return; }
                catch (Exception) when (i < 8) { Thread.Sleep(500); }
            }
        }

        public static string Install(IProgress<(int, string)> prog, bool pawn, bool autostart, bool update)
        {
            Log($"--- {(update ? "update" : "install")} {Version} by {Environment.UserDomainName}\\{Environment.UserName}");
            var tmp = Path.Combine(Path.GetTempPath(), "PCS-setup-" + Guid.NewGuid().ToString("n").Substring(0, 8));
            Directory.CreateDirectory(tmp);
            try
            {
                prog.Report((5, "Unpacking…"));
                var zip = Path.Combine(tmp, "payload.zip");
                using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
                using (var f = File.Create(zip)) s.CopyTo(f);
                var src = Path.Combine(tmp, "p");
                ZipFile.ExtractToDirectory(zip, src);

                string pawnNote = "";
                if (pawn)
                {
                    prog.Report((25, "Installing the PawnIO driver (its own window)…"));
                    var ps = Path.Combine(src, "PawnIO_setup.exe");
                    if (!Trust.SignedBy(ps, "CN=namazso.eu")) { pawnNote = "PawnIO was NOT installed: its setup is not signed as expected. "; Log("PawnIO signature check failed"); }
                    else
                    {
                        var p = Process.Start(new ProcessStartInfo(ps) { UseShellExecute = true });
                        p.WaitForExit();
                        Log("PawnIO setup exit " + p.ExitCode);
                        pawnNote = PawnIoVersion() != null ? "PawnIO driver installed. " : "PawnIO was not installed: CPU / board sensors and Reflex will be missing. ";
                    }
                }

                prog.Report((45, "Stopping PCS…"));
                StopRunning();

                prog.Report((55, "Copying files…"));
                Directory.CreateDirectory(Dir);
                foreach (var sub in new[] { "service", "sensors" })
                    Retry(() => { var d = Path.Combine(Dir, sub); if (Directory.Exists(d)) Directory.Delete(d, true); });
                CopyDir(Path.Combine(src, "app"), Dir);
                CopyDir(Path.Combine(src, "service"), Path.Combine(Dir, "service"));
                CopyDir(Path.Combine(src, "sensors"), Path.Combine(Dir, "sensors"));
                var uj = Path.Combine(src, "update.json");
                if (File.Exists(uj)) File.Copy(uj, Path.Combine(Dir, "update.json"), true);
                // the uninstaller: this program without its payload, built next to the setup and packed in app\
                foreach (var old in new[] { "Uninstall PCS.cmd", "uninstall.ps1", "diag.ps1", "Collect diagnostics.cmd" })
                    try { File.Delete(Path.Combine(Dir, old)); } catch { }

                prog.Report((75, "Sensor reader…"));
                Directory.CreateDirectory(Sensors);
                AllowUsersToWrite(Sensors);    // the Fans tab (service, normal user) writes fans.json here
                RegisterTask();
                Run("schtasks.exe", $"/Run /TN \"{Task_}\"");

                prog.Report((88, "Start Menu and Apps & features…"));
                Shortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonPrograms), "PCS.lnk"), Path.Combine(Dir, "PCS.exe"));
                using (var k = Registry.CurrentUser.CreateSubKey(RunKey))
                {
                    if (autostart) k.SetValue("PCS", $"\"{Path.Combine(Dir, "PCS.exe")}\" --tray");
                    else k.DeleteValue("PCS", false);
                }
                using (var k = Registry.LocalMachine.CreateSubKey(UninstKey))
                {
                    k.SetValue("DisplayName", "PCS");
                    k.SetValue("DisplayVersion", Version);
                    k.SetValue("Publisher", "PCS");
                    k.SetValue("DisplayIcon", Path.Combine(Dir, "PCS.exe"));
                    k.SetValue("InstallLocation", Dir);
                    k.SetValue("UninstallString", $"\"{Path.Combine(Dir, "Uninstall PCS.exe")}\"");
                    k.SetValue("QuietUninstallString", $"\"{Path.Combine(Dir, "Uninstall PCS.exe")}\" /quiet");
                    k.SetValue("NoModify", 1, RegistryValueKind.DWord);
                    k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                    k.SetValue("EstimatedSize", (int)(DirSize(Dir) / 1024), RegistryValueKind.DWord);
                }
                prog.Report((100, "Done"));
                Thread.Sleep(300);
                Log("done");
                return pawnNote + "The sensor reader is running. Fans keep their BIOS curves until you switch on Reflex in PCS → Fans." +
                       (autostart ? " PCS starts with Windows in the tray." : "");
            }
            finally { try { Directory.Delete(tmp, true); } catch { } }
        }

        public static void Uninstall(bool data)
        {
            Log("--- uninstall");
            StopRunning();
            Run("schtasks.exe", $"/Delete /TN \"{Task_}\" /F");
            try { File.Delete(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonPrograms), "PCS.lnk")); } catch { }
            try { File.Delete(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "PCS.lnk")); } catch { }
            using (var k = Registry.CurrentUser.OpenSubKey(RunKey, true)) k?.DeleteValue("PCS", false);
            Registry.LocalMachine.DeleteSubKeyTree(UninstKey, false);
            Retry(() => { if (Directory.Exists(Dir)) Directory.Delete(Dir, true); });
            if (data)
            {
                try { Directory.Delete(Data, true); } catch { }
                try { Directory.Delete(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PCS"), true); } catch { }
            }
        }

        static void CopyDir(string from, string to)
        {
            Directory.CreateDirectory(to);
            foreach (var d in Directory.GetDirectories(from, "*", SearchOption.AllDirectories))
                Directory.CreateDirectory(Path.Combine(to, d.Substring(from.Length + 1)));
            foreach (var f in Directory.GetFiles(from, "*", SearchOption.AllDirectories))
            {
                var dst = Path.Combine(to, f.Substring(from.Length + 1));
                Retry(() => File.Copy(f, dst, true));
            }
        }

        static long DirSize(string d) => Directory.GetFiles(d, "*", SearchOption.AllDirectories).Sum(f => new FileInfo(f).Length);

        static void AllowUsersToWrite(string dir)
        {
            var sec = Directory.GetAccessControl(dir);
            sec.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null), FileSystemRights.Modify,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            Directory.SetAccessControl(dir, sec);
        }

        static void RegisterTask()
        {
            var user = WindowsIdentity.GetCurrent().Name;      // DOMAIN\user of the person who runs setup
            var exe = Path.Combine(Dir, @"sensors\PcsSensors.exe");
            string X(string s) => System.Security.SecurityElement.Escape(s);
            var xml = $@"<?xml version=""1.0"" encoding=""UTF-16""?>
<Task version=""1.4"" xmlns=""http://schemas.microsoft.com/windows/2004/02/mit/task"">
  <RegistrationInfo><Description>PCS sensor reader (CPU / board / GPU sensors and fan control). Started by PCS setup.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{X(user)}</UserId><Delay>PT15S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id=""Author""><UserId>{X(user)}</UserId><LogonType>S4U</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit><StartWhenAvailable>true</StartWhenAvailable>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Priority>4</Priority>
  </Settings>
  <Actions Context=""Author""><Exec><Command>{X(exe)}</Command>
    <Arguments>--user-fans --no-ec --out ""{X(Path.Combine(Sensors, "now.json"))}""</Arguments>
    <WorkingDirectory>{X(Path.GetDirectoryName(exe))}</WorkingDirectory></Exec></Actions>
</Task>";
            var f = Path.Combine(Path.GetTempPath(), "pcs-task.xml");
            File.WriteAllText(f, xml, System.Text.Encoding.Unicode);
            Run("schtasks.exe", $"/Create /TN \"{Task_}\" /XML \"{f}\" /F");
            try { File.Delete(f); } catch { }
        }

        static void Shortcut(string lnk, string target)
        {
            var t = Type.GetTypeFromProgID("WScript.Shell");
            dynamic sh = Activator.CreateInstance(t);
            dynamic s = sh.CreateShortcut(lnk);
            s.TargetPath = target; s.WorkingDirectory = Path.GetDirectoryName(target); s.Description = "PCS"; s.Save();
            Marshal.FinalReleaseComObject(s); Marshal.FinalReleaseComObject(sh);
        }

        // setup runs elevated; PCS itself must not: explorer.exe starts it as the signed-in user
        public static void LaunchAsUser(string args)
        {
            try { Process.Start(new ProcessStartInfo("explorer.exe", $"\"{Path.Combine(Dir, "PCS.exe")}\"") { UseShellExecute = false }); }
            catch (Exception ex) { Log("launch failed: " + ex.Message); }
        }
    }

    // Authenticode check (WinVerifyTrust: valid chain + untampered file) plus the signer's subject
    static class Trust
    {
        [DllImport("wintrust.dll", CharSet = CharSet.Unicode)]
        static extern int WinVerifyTrust(IntPtr hwnd, ref Guid action, ref WINTRUST_DATA data);
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct WINTRUST_FILE_INFO { public int cbStruct; public string pcwszFilePath; public IntPtr hFile; public IntPtr pgKnownSubject; }
        [StructLayout(LayoutKind.Sequential)]
        struct WINTRUST_DATA
        {
            public int cbStruct; public IntPtr pPolicyCallbackData, pSIPClientData; public int dwUIChoice, fdwRevocationChecks, dwUnionChoice;
            public IntPtr pFile; public int dwStateAction; public IntPtr hWVTStateData; public IntPtr pwszURLReference; public int dwProvFlags, dwUIContext;
            public IntPtr pSignatureSettings;
        }
        public static bool SignedBy(string file, string subject)
        {
            var fi = new WINTRUST_FILE_INFO { cbStruct = Marshal.SizeOf(typeof(WINTRUST_FILE_INFO)), pcwszFilePath = file };
            var pf = Marshal.AllocHGlobal(fi.cbStruct);
            try
            {
                Marshal.StructureToPtr(fi, pf, false);
                var d = new WINTRUST_DATA { cbStruct = Marshal.SizeOf(typeof(WINTRUST_DATA)), dwUIChoice = 2, dwUnionChoice = 1, pFile = pf };
                var g = new Guid("00AAC56B-CD44-11d0-8CC2-00C04FC295EE");   // WINTRUST_ACTION_GENERIC_VERIFY_V2
                if (WinVerifyTrust(IntPtr.Zero, ref g, ref d) != 0) return false;
                var cert = System.Security.Cryptography.X509Certificates.X509Certificate.CreateFromSignedFile(file);
                return cert.Subject.Contains(subject);
            }
            catch { return false; }
            finally { Marshal.FreeHGlobal(pf); }
        }
    }
}
