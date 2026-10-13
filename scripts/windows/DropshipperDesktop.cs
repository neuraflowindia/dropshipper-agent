// Native Windows GUI and invisible scheduled entry. No PowerShell at runtime.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: AssemblyTitle("NeuraFlow Dropshipper")]
[assembly: AssemblyDescription("Private product research controls")]
[assembly: AssemblyCompany("NeuraFlow")]
[assembly: AssemblyProduct("NeuraFlow Dropshipper")]
[assembly: AssemblyVersion("1.0.0.0")]

namespace NeuraFlow.Dropshipper {
    internal sealed class Settings {
        public string WindowsRepository { get; set; }
        public string LinuxRepository { get; set; }
        public string Distribution { get; set; }
        public string[] ResearchArguments { get; set; }
        public static Settings Load() {
            string file = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "launcher.json");
            if (new FileInfo(file).Length > 16384) throw new IOException("Launcher settings are too large.");
            Settings value = new JavaScriptSerializer().Deserialize<Settings>(File.ReadAllText(file));
            if (value == null || !Path.IsPathRooted(value.WindowsRepository ?? "") ||
                !Directory.Exists(value.WindowsRepository) ||
                !System.Text.RegularExpressions.Regex.IsMatch(value.Distribution ?? "", "^[A-Za-z0-9_.-]{1,80}$") ||
                String.IsNullOrEmpty(value.LinuxRepository) || !value.LinuxRepository.StartsWith("/") ||
                value.LinuxRepository.Contains("\n") || value.LinuxRepository.Contains("\r") ||
                value.ResearchArguments == null) throw new IOException("Launcher settings need repair.");
            var allowed = new HashSet<string>(new[] { "--tick", "--execute", "--live-model", "--funded-quickcommerce-fallback", "--cooperative-turns" });
            var actual = new HashSet<string>(value.ResearchArguments);
            if (!actual.IsSubsetOf(allowed) || actual.Count != value.ResearchArguments.Length ||
                !actual.Contains("--tick") || !actual.Contains("--execute")) throw new IOException("Research options need repair.");
            return value;
        }
    }

    internal sealed class ResearchStatus {
        public string state { get; set; }
        public bool enabled { get; set; }
        public int active_processes { get; set; }
        public string message { get; set; }
        public string reason { get; set; }
        public string checked_at { get; set; }
    }

    internal interface IControls {
        Task<ResearchStatus> Call(string action);
        void LaunchWorker();
        void OpenLogs();
    }

    internal sealed class LocalControls : IControls {
        private readonly Settings settings;
        internal LocalControls(Settings settings) { this.settings = settings; }
        internal static string Quote(string value) {
            // CommandLineToArgvW quoting, including trailing backslashes.
            // WSL parses its option switches before passing Linux arguments;
            // gratuitously quoted switches can become a Linux shell command.
            if (value.Length > 0 && value.IndexOf('"') < 0 && value.IndexOfAny(new[] { ' ', '\t', '\n', '\r' }) < 0) return value;
            StringBuilder result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char c in value) {
                if (c == '\\') { slashes++; continue; }
                if (c == '"') { result.Append('\\', slashes * 2 + 1); result.Append(c); }
                else { result.Append('\\', slashes); result.Append(c); }
                slashes = 0;
            }
            result.Append('\\', slashes * 2); result.Append('"');
            return result.ToString();
        }
        internal ProcessStartInfo Command(string action) {
            var args = new List<string> { "--distribution", settings.Distribution, "--cd", settings.LinuxRepository,
                "--exec", settings.LinuxRepository + "/.venv/bin/python", "-u",
                settings.LinuxRepository + "/scripts/desktop_research_control.py", action };
            if (action == "worker") args.AddRange(settings.ResearchArguments);
            return new ProcessStartInfo {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "System32", "wsl.exe"),
                Arguments = String.Join(" ", args.ConvertAll(Quote).ToArray()),
                WorkingDirectory = settings.WindowsRepository, UseShellExecute = false,
                CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardOutput = true, RedirectStandardError = true
            };
        }
        public Task<ResearchStatus> Call(string action) {
            return Task.Run(delegate {
                using (var process = new Process { StartInfo = Command(action) }) {
                    process.Start();
                    Task<string> stdout = process.StandardOutput.ReadToEndAsync();
                    Task<string> stderr = process.StandardError.ReadToEndAsync();
                    if (!process.WaitForExit(20000)) {
                        // This is only the short status/control command, never
                        // a research worker. The saved pause remains authoritative.
                        try { process.Kill(); } catch (InvalidOperationException) { }
                        throw new IOException("Windows could not reach the research controls in time. Try again.");
                    }
                    Task.WaitAll(stdout, stderr);
                    string text = stdout.Result.Trim();
                    if (text.Length > 32768 || text.Length == 0) throw new IOException("The research controls did not answer. See the logs.");
                    ResearchStatus result = new JavaScriptSerializer().Deserialize<ResearchStatus>(text);
                    if (result == null || process.ExitCode != 0 ||
                        !new HashSet<string>(new[] { "running", "ready", "stopping", "stopped" }).Contains(result.state))
                        throw new IOException(result != null ? result.message : "The research controls need attention.");
                    return result;
                }
            });
        }
        public void LaunchWorker() {
            using (var process = Process.Start(new ProcessStartInfo {
                FileName = Application.ExecutablePath, Arguments = "--worker",
                WorkingDirectory = settings.WindowsRepository, UseShellExecute = false, CreateNoWindow = true
            })) { if (process == null) throw new IOException("The research worker could not start."); }
        }
        public void OpenLogs() {
            string directory = Path.Combine(settings.WindowsRepository, "data", "operations", "desktop");
            Directory.CreateDirectory(directory);
            Process.Start(new ProcessStartInfo { FileName = directory, UseShellExecute = true });
        }
        internal int RunWorker() {
            string directory = Path.Combine(settings.WindowsRepository, "data", "operations", "desktop");
            Directory.CreateDirectory(directory);
            string file = Path.Combine(directory, "windows-" + DateTime.UtcNow.ToString("yyyy-MM-dd") + ".log");
            object gate = new object();
            using (var log = new StreamWriter(new FileStream(file, FileMode.Append, FileAccess.Write, FileShare.ReadWrite)))
            using (var child = new Process { StartInfo = Command("worker") }) {
                log.AutoFlush = true;
                Action<string, string> write = delegate(string kind, string line) {
                    if (line == null) return;
                    // The existing programme emits compact public-research
                    // progress. Never copy environment variables or config files.
                    try { lock (gate) { log.WriteLine(DateTime.UtcNow.ToString("o") + " " + kind + " " + line); } }
                    catch (IOException) { /* Keep draining output if diagnostic storage fails. */ }
                };
                child.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { write("research", e.Data); };
                child.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { write("error", e.Data); };
                write("launcher", "worker_requested");
                child.Start(); child.BeginOutputReadLine(); child.BeginErrorReadLine();
                child.WaitForExit();
                write("launcher", "worker_exit=" + child.ExitCode.ToString());
                return child.ExitCode;
            }
        }
    }

    internal sealed class ControlWindow : Form {
        private readonly IControls controls;
        internal readonly Button StartButton, StopButton, ExitButton;
        internal readonly NotifyIcon Tray;
        private readonly ToolStripMenuItem startMenu, stopMenu;
        private readonly Label heading, detail, checkedAt;
        private readonly System.Windows.Forms.Timer timer;
        private readonly EventWaitHandle restoreEvent;
        private bool refreshing, changing, exitRequested, allowClose;
        private int generation;
        internal ResearchStatus Latest;
        internal CloseReason LastCloseReason { get; private set; }
        internal ControlWindow(IControls controls, EventWaitHandle restoreEvent) {
            this.controls = controls; this.restoreEvent = restoreEvent;
            Text = "NeuraFlow Dropshipper";
            Name = "NeuraFlowDropshipperWindow";
            AutoScaleMode = AutoScaleMode.Dpi;
            ClientSize = new Size(520, 330); MinimumSize = new Size(536, 369);
            FormBorderStyle = FormBorderStyle.FixedSingle; MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(249, 246, 240); ForeColor = Color.FromArgb(32, 49, 62);
            Font = new Font("Segoe UI", 10F);
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            var brand = new Label { Text = "NEURAFLOW  /  PRODUCT RESEARCH", Location = new Point(28, 25),
                Size = new Size(460, 25), ForeColor = Color.FromArgb(117, 37, 57), Font = new Font("Segoe UI", 10F, FontStyle.Bold) };
            heading = new Label { Text = "Checking research…", Location = new Point(27, 68), Size = new Size(465, 42),
                Font = new Font("Segoe UI", 22F, FontStyle.Bold), Name = "ResearchState" };
            detail = new Label { Text = "Your saved work stays in this project.", Location = new Point(29, 120),
                Size = new Size(460, 47), Name = "ResearchExplanation" };
            checkedAt = new Label { Location = new Point(29, 174), Size = new Size(460, 22),
                Font = new Font("Segoe UI", 8.5F), ForeColor = Color.FromArgb(89, 102, 108) };
            StartButton = MakeButton("Start", "StartResearch", 29, Color.FromArgb(117, 37, 57), Color.White);
            StopButton = MakeButton("Stop", "StopResearch", 184, Color.FromArgb(229, 232, 223), ForeColor);
            ExitButton = MakeButton("Exit", "ExitResearch", 339, Color.FromArgb(229, 232, 223), ForeColor);
            var hint = new Label { Text = "Minimize or close this window to keep it in the tray.\nClick its tray icon once to bring it back. Exit stops research.",
                Location = new Point(29, 270), Size = new Size(465, 44), Font = new Font("Segoe UI", 8.5F) };
            Controls.AddRange(new Control[] { brand, heading, detail, checkedAt, StartButton, StopButton, ExitButton, hint });
            var menu = new ContextMenuStrip();
            menu.Items.Add("Open window", null, delegate { RestoreWindow(); });
            startMenu = new ToolStripMenuItem("Start", null, async delegate { await Change("start"); });
            stopMenu = new ToolStripMenuItem("Stop", null, async delegate { await Change("stop"); });
            menu.Items.Add(startMenu); menu.Items.Add(stopMenu);
            menu.Items.Add("Open logs", null, delegate {
                try { controls.OpenLogs(); } catch (Exception) { detail.Text = "The log folder could not be opened."; }
            });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("Exit (stop research)", null, async delegate { await ExitResearch(); });
            Tray = new NotifyIcon { Icon = Icon, Visible = true, Text = "NeuraFlow Dropshipper — checking", ContextMenuStrip = menu };
            Tray.MouseClick += TrayMouseClick;
            StartButton.Click += async delegate { await Change("start"); };
            StopButton.Click += async delegate { await Change("stop"); };
            ExitButton.Click += async delegate { await ExitResearch(); };
            Resize += delegate { if (WindowState == FormWindowState.Minimized) HideToTray(); };
            FormClosing += delegate(object sender, FormClosingEventArgs e) {
                LastCloseReason = e.CloseReason;
                // WinForms classifies an external WM_CLOSE as TaskManagerClosing.
                // Treat window-close requests as hide, but allow real Windows
                // shutdown, Application.Exit and our explicit Exit to close.
                if (!allowClose && (e.CloseReason == CloseReason.UserClosing ||
                    e.CloseReason == CloseReason.TaskManagerClosing || e.CloseReason == CloseReason.None)) {
                    e.Cancel = true; HideToTray();
                }
            };
            FormClosed += delegate { timer.Stop(); Tray.Visible = false; Tray.Dispose(); };
            timer = new System.Windows.Forms.Timer { Interval = 4000 };
            timer.Tick += async delegate {
                if (restoreEvent != null && restoreEvent.WaitOne(0)) RestoreWindow();
                await RefreshStatus();
            };
            Shown += async delegate { timer.Start(); await RefreshStatus(); };
            SetButtons(false, false);
        }
        private Button MakeButton(string text, string name, int x, Color background, Color foreground) {
            return new Button { Text = text, Name = name, Location = new Point(x, 211), Size = new Size(143, 43),
                FlatStyle = FlatStyle.Flat, BackColor = background, ForeColor = foreground, Cursor = Cursors.Hand,
                Font = new Font("Segoe UI", 10F, FontStyle.Bold), TabStop = true };
        }
        private void SetButtons(bool start, bool stop) {
            StartButton.Enabled = startMenu.Enabled = start;
            StopButton.Enabled = stopMenu.Enabled = stop;
            StartButton.BackColor = start ? Color.FromArgb(117, 37, 57) : Color.FromArgb(229, 232, 223);
            StartButton.ForeColor = start ? Color.White : Color.FromArgb(139, 145, 145);
            StopButton.ForeColor = stop ? ForeColor : Color.FromArgb(139, 145, 145);
        }
        private void Apply(ResearchStatus state) {
            Latest = state;
            heading.Text = state.state == "running" ? "Research is running" : state.state == "ready" ? "Research is on" :
                state.state == "stopping" ? "Stopping research…" : "Research is stopped";
            detail.Text = state.message;
            checkedAt.Text = "Checked at " + DateTime.Now.ToString("h:mm:ss tt") + " on this computer.";
            Tray.Text = "NeuraFlow Dropshipper — " + state.state;
            SetButtons(!changing && !exitRequested && !state.enabled && state.active_processes == 0,
                       !changing && !exitRequested && state.enabled);
            if (exitRequested && !state.enabled && state.active_processes == 0) {
                allowClose = true; Close();
            }
        }
        internal async Task RefreshStatus() {
            if (refreshing || changing || IsDisposed) return;
            refreshing = true; int observed = generation;
            try {
                var result = await controls.Call("status");
                if (!IsDisposed && observed == generation && !changing) Apply(result);
            } catch (Exception error) {
                if (!IsDisposed && observed == generation) ShowFailure(error);
            } finally { refreshing = false; }
        }
        private void ShowFailure(Exception error) {
            heading.Text = "Controls need attention";
            detail.Text = error.Message;
            checkedAt.Text = "Could not confirm the current state. This is not a successful Stop.";
            Tray.Text = "NeuraFlow Dropshipper — check controls";
            SetButtons(false, !changing);
            if (exitRequested) { exitRequested = false; ExitButton.Enabled = true; }
        }
        internal async Task Change(string action) {
            if (changing) return;
            changing = true; generation++; SetButtons(false, false);
            heading.Text = action == "start" ? "Starting research…" : "Stopping research…";
            try {
                ResearchStatus result = await controls.Call(action);
                if (action == "start") controls.LaunchWorker();
                changing = false;
                Apply(result);
            } catch (Exception error) {
                changing = false; ShowFailure(error);
            }
        }
        internal async Task ExitResearch() {
            if (exitRequested || changing) return;
            exitRequested = true; ExitButton.Enabled = false;
            await Change("stop");
        }
        internal void HideToTray() { ShowInTaskbar = false; Hide(); }
        internal void TrayMouseClick(object sender, MouseEventArgs e) {
            if (e.Button == MouseButtons.Left) RestoreWindow();
        }
        internal void RestoreWindow() {
            ShowInTaskbar = true; WindowState = FormWindowState.Normal; Show();
            NativeMethods.ShowWindow(Handle, 9); NativeMethods.SetForegroundWindow(Handle); Activate();
        }
        internal void DisposeForTest() { allowClose = true; Close(); }
    }

    internal static class NativeMethods {
        [DllImport("user32.dll")] internal static extern bool SetForegroundWindow(IntPtr handle);
        [DllImport("user32.dll")] internal static extern bool ShowWindow(IntPtr handle, int command);
        [DllImport("user32.dll")] internal static extern IntPtr SendMessage(IntPtr handle, uint message, IntPtr wParam, IntPtr lParam);
        [DllImport("kernel32.dll")] internal static extern IntPtr GetConsoleWindow();
    }

    internal static class Program {
        [STAThread]
        private static int Main(string[] args) {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            try {
                if (args.Length == 2 && args[0] == "--self-test") return SelfTests.Run(args[1]);
                Settings settings = Settings.Load();
                var controls = new LocalControls(settings);
                if (args.Length == 1 && args[0] == "--worker") return controls.RunWorker();
                if (args.Length != 0) return 2;
                string identity = "Local\\NeuraFlow.Dropshipper." + WindowsIdentity.GetCurrent().User.Value;
                bool first;
                using (var mutex = new Mutex(true, identity, out first))
                using (var restore = new EventWaitHandle(false, EventResetMode.AutoReset, identity + ".Show")) {
                    if (!first) { restore.Set(); return 0; }
                    try { Application.Run(new ControlWindow(controls, restore)); }
                    finally { mutex.ReleaseMutex(); }
                }
                return 0;
            } catch (Exception error) {
                // Scheduled workers never display a dialog/console on failure.
                if (args.Length == 1 && args[0] == "--worker") return 1;
                MessageBox.Show(error.Message, "NeuraFlow Dropshipper", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return 1;
            }
        }
    }

    internal sealed class FakeControls : IControls {
        internal bool Enabled, Active, Fail;
        internal int Starts, Stops, Workers;
        public Task<ResearchStatus> Call(string action) {
            if (Fail) throw new IOException("Controlled connection failure.");
            if (action == "start") { Enabled = true; Active = true; Starts++; }
            if (action == "stop") { Enabled = false; Active = false; Stops++; }
            return Task.FromResult(new ResearchStatus { enabled = Enabled, active_processes = Active ? 1 : 0,
                state = Enabled ? "running" : "stopped", message = Enabled ? "Research is running." : "Research is stopped. Your saved work is kept." });
        }
        public void LaunchWorker() { Workers++; }
        public void OpenLogs() { }
    }

    internal static class SelfTests {
        internal static int Run(string output) {
            var checks = new Dictionary<string, bool>();
            string failure = null;
            var controls = new FakeControls();
            var window = new ControlWindow(controls, null);
            window.Shown += async delegate {
                try {
                    await window.RefreshStatus();
                    checks["wsl_options_not_gratuitously_quoted"] = LocalControls.Quote("--distribution") == "--distribution" &&
                        LocalControls.Quote("/mnt/d/a project") == "\"/mnt/d/a project\"";
                    checks["native_executable_has_no_console"] = NativeMethods.GetConsoleWindow() == IntPtr.Zero;
                    checks["stopped_buttons"] = window.StartButton.Enabled && !window.StopButton.Enabled;
                    window.StartButton.PerformClick();
                    checks["start_button_action"] = controls.Starts == 1 && controls.Workers == 1;
                    checks["running_buttons"] = !window.StartButton.Enabled && window.StopButton.Enabled;
                    window.WindowState = FormWindowState.Minimized;
                    checks["minimize_to_tray"] = !window.Visible && !window.ShowInTaskbar && window.Tray.Visible;
                    window.TrayMouseClick(window.Tray, new MouseEventArgs(MouseButtons.Right, 1, 0, 0, 0));
                    checks["right_click_does_not_restore_window"] = !window.Visible;
                    window.TrayMouseClick(window.Tray, new MouseEventArgs(MouseButtons.Left, 1, 0, 0, 0));
                    checks["tray_restore_handler"] = window.Visible && window.ShowInTaskbar && window.WindowState == FormWindowState.Normal;
                    NativeMethods.SendMessage(window.Handle, 0x0010, IntPtr.Zero, IntPtr.Zero);
                    checks["windows_close_message_keeps_tray_running"] = !window.IsDisposed && !window.Visible && window.Tray.Visible;
                    if (!checks["windows_close_message_keeps_tray_running"])
                        throw new InvalidOperationException("Windows close did not hide: " + window.LastCloseReason.ToString());
                    window.RestoreWindow();
                    window.Close();
                    checks["x_keeps_tray_running"] = !window.IsDisposed && !window.Visible && window.Tray.Visible;
                    window.RestoreWindow(); window.StopButton.PerformClick();
                    checks["stop_button_action"] = controls.Stops == 1 && !controls.Enabled;
                    checks["stopped_again_buttons"] = window.StartButton.Enabled && !window.StopButton.Enabled;
                    controls.Fail = true; await window.RefreshStatus();
                    checks["connection_failure_not_reported_as_stopped"] = window.Controls["ResearchState"].Text == "Controls need attention";
                    controls.Fail = false; await window.RefreshStatus();
                    checks["connection_recovery"] = window.StartButton.Enabled && !window.StopButton.Enabled;
                    using (var bitmap = new Bitmap(window.Width, window.Height)) {
                        window.DrawToBitmap(bitmap, new Rectangle(0, 0, bitmap.Width, bitmap.Height));
                        bitmap.Save(Path.ChangeExtension(output, ".png"), System.Drawing.Imaging.ImageFormat.Png);
                    }
                    window.ExitButton.PerformClick();
                    checks["exit_stops_and_closes"] = controls.Stops == 2 && window.IsDisposed;
                } catch (Exception error) { failure = error.ToString(); }
                finally { if (!window.IsDisposed) window.DisposeForTest(); }
            };
            Application.Run(window);
            bool passed = failure == null && checks.Count == 15;
            foreach (bool check in checks.Values) passed &= check;
            File.WriteAllText(output, new JavaScriptSerializer().Serialize(new { passed = passed, checks = checks, failure = failure,
                actual_research_started = false, provider_calls = 0, tested_at = DateTime.UtcNow.ToString("o") }));
            return passed ? 0 : 1;
        }
    }
}
