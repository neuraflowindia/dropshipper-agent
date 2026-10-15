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
        public SignInStatus sign_in { get; set; }
        public List<SpendLine> spend { get; set; }
        public List<SpendAlert> spend_alerts { get; set; }
    }

    internal sealed class SpendLine {
        public string item { get; set; }
        public string text { get; set; }
        public int? percent { get; set; }
        public List<int> flags { get; set; }
    }

    internal sealed class SpendAlert {
        public string alert_id { get; set; }
        public string kind { get; set; }
        public string message { get; set; }
    }

    internal sealed class SignInNeed {
        public string id { get; set; }
        public string site { get; set; }
        public string label { get; set; }
        public string state { get; set; }
    }

    internal sealed class SignInStatus {
        public bool window_open { get; set; }
        public string line { get; set; }
        public string trends_line { get; set; }
        public string score_line { get; set; }
        public List<string> platform_lines { get; set; }
        public string notice { get; set; }
        public List<SignInNeed> needs { get; set; }
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
        internal readonly Button StartButton, StopButton, ExitButton, SignInButton;
        internal readonly NotifyIcon Tray;
        private readonly ToolStripMenuItem startMenu, stopMenu;
        private readonly Label heading, detail, checkedAt, signInLine, spendLines, hint;
        private readonly HashSet<string> notifiedNeeds = new HashSet<string>();
        private readonly HashSet<string> notifiedAlerts = new HashSet<string>();
        private string signInNotice;
        internal int NotificationsShown { get; private set; }
        internal int SpendNotificationsShown { get; private set; }
        internal string LastSpendNotification { get; private set; }
        private readonly System.Windows.Forms.Timer timer;
        private readonly EventWaitHandle restoreEvent;
        private bool refreshing, changing, exitRequested, allowClose, signingIn;
        private int generation;
        internal ResearchStatus Latest;
        internal CloseReason LastCloseReason { get; private set; }
        internal ControlWindow(IControls controls, EventWaitHandle restoreEvent) {
            this.controls = controls; this.restoreEvent = restoreEvent;
            Text = "NeuraFlow Dropshipper";
            Name = "NeuraFlowDropshipperWindow";
            AutoScaleMode = AutoScaleMode.Dpi;
            ClientSize = new Size(520, 416); MinimumSize = new Size(536, 455);
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
            SignInButton = new Button { Text = "Sign in to research sites", Name = "SignInResearchSites", Location = new Point(29, 268),
                Size = new Size(298, 38), FlatStyle = FlatStyle.Flat, BackColor = Color.FromArgb(229, 232, 223), ForeColor = ForeColor,
                Cursor = Cursors.Hand, Font = new Font("Segoe UI", 10F, FontStyle.Bold), TabStop = true };
            signInLine = new Label { Location = new Point(29, 312), Size = new Size(465, 40), Name = "SignInNeeds",
                Font = new Font("Segoe UI", 9F) };
            // Reserved for the spend and allowance lines (one line per item).
            spendLines = new Label { Location = new Point(29, 356), Size = new Size(465, 0), Name = "SpendLines",
                Font = new Font("Segoe UI", 9F) };
            hint = new Label { Text = "Minimize or close this window to keep it in the tray.\nClick its tray icon once to bring it back. Exit stops research.",
                Location = new Point(29, 360), Size = new Size(465, 44), Font = new Font("Segoe UI", 8.5F) };
            Controls.AddRange(new Control[] { brand, heading, detail, checkedAt, StartButton, StopButton, ExitButton,
                SignInButton, signInLine, spendLines, hint });
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
            SignInButton.Click += async delegate { await SignIn(); };
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
            ApplySignIn(state.sign_in);
            ApplySpend(state);
            if (exitRequested && !state.enabled && state.active_processes == 0) {
                allowClose = true; Close();
            }
        }
        private void ApplySignIn(SignInStatus signIn) {
            // A busy/error notice stays until the owner's next action.
            // The sign-in needs, one line per dropshipping platform needing attention,
            // then the signed-in Google Trends check, then the opportunity score
            // tally (and its one-time calibration alert), each on its own line.
            SetSignInText(signInNotice ?? (signIn == null ? "" : (signIn.line ?? "")
                + (signIn.platform_lines == null ? "" : String.Concat(signIn.platform_lines.FindAll(
                    line => !String.IsNullOrEmpty(line)).ConvertAll(line => "\n" + line).ToArray()))
                + (String.IsNullOrEmpty(signIn.trends_line) ? "" : "\n" + signIn.trends_line)
                + (String.IsNullOrEmpty(signIn.score_line) ? "" : "\n" + signIn.score_line)));
            SignInButton.Enabled = !signingIn && (signIn == null || !signIn.window_open);
            if (signIn == null || signIn.needs == null) return;
            var fresh = new List<SignInNeed>();
            foreach (SignInNeed need in signIn.needs)
                if (need != null && !String.IsNullOrEmpty(need.id) && notifiedNeeds.Add(need.id)) fresh.Add(need);
            if (fresh.Count == 0) return;
            // One Windows notification for the new needs together; a repeat status is quiet.
            NotificationsShown++;
            string text = fresh.Count == 1 && fresh[0].state == "expired" ? "The " + fresh[0].label + " sign-in has expired."
                : "Sign-in needed for " + String.Join(", ", fresh.ConvertAll(need => need.label).ToArray()) + ".";
            Tray.ShowBalloonTip(10000, "Sign-in needed", text + " Open NeuraFlow Dropshipper and click Sign in to research sites.", ToolTipIcon.Info);
        }
        private void SetSignInText(string text) {
            if (signInLine.Text == text) return;
            signInLine.Text = text;
            // The label grows with its lines (needs, platforms, then the Trends check), never clipping them.
            signInLine.Height = Math.Max(20, TextRenderer.MeasureText(text, signInLine.Font,
                new Size(signInLine.Width, 0), TextFormatFlags.WordBreak).Height + 4);
            Relayout();
        }
        private void Relayout() {
            spendLines.Top = signInLine.Bottom + 4;
            hint.Top = spendLines.Top + spendLines.Height + 4;
            ClientSize = new Size(ClientSize.Width, hint.Bottom + 12);
        }
        private void ApplySpend(ResearchStatus state) {
            // One line per spend or allowance item, as the status command words it.
            var lines = new List<string>();
            if (state.spend != null)
                foreach (SpendLine line in state.spend)
                    if (line != null && !String.IsNullOrEmpty(line.text)) lines.Add(line.text);
            string text = String.Join("\n", lines.ToArray());
            if (spendLines.Text != text) {
                spendLines.Text = text;
                int height = lines.Count == 0 ? 0 : TextRenderer.MeasureText(text, spendLines.Font,
                    new Size(spendLines.Width, 0), TextFormatFlags.WordBreak).Height + 4;
                spendLines.Height = height;
                Relayout();
            }
            if (state.spend_alerts == null) return;
            var fresh = new List<SpendAlert>();
            foreach (SpendAlert alert in state.spend_alerts)
                if (alert != null && !String.IsNullOrEmpty(alert.alert_id) && !String.IsNullOrEmpty(alert.message) &&
                    notifiedAlerts.Add(alert.alert_id)) fresh.Add(alert);
            if (fresh.Count == 0) return;
            // One Windows notification for the new spending alerts together; a repeat status is quiet.
            string message = fresh.Count == 1 ? fresh[0].message : fresh.Count.ToString() + " spending updates: " +
                String.Join(" ", fresh.ConvertAll(alert => alert.message).ToArray());
            if (message.Length > 250) message = message.Substring(0, 247) + "...";
            SpendNotificationsShown++; LastSpendNotification = message;
            Tray.ShowBalloonTip(10000, "Spending and allowances", message, ToolTipIcon.Info);
        }
        internal async Task SignIn() {
            if (signingIn || changing) return;
            signingIn = true; SignInButton.Enabled = false; signInNotice = null;
            SetSignInText("Opening the sign-in window…");
            try {
                ResearchStatus result = await controls.Call("sign-in");
                signingIn = false;
                signInNotice = result.sign_in != null ? result.sign_in.notice : null;
                Apply(result);
            } catch (Exception error) {
                // A sign-in problem never reads as a research Stop failure.
                signingIn = false; signInNotice = error.Message; SetSignInText(error.Message); SignInButton.Enabled = true;
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
            changing = true; generation++; SetButtons(false, false); signInNotice = null;
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
            // Restore the size before showing the taskbar button again: recreating the
            // handle while minimized (smaller than MinimumSize) overflows WinForms' stack.
            WindowState = FormWindowState.Normal; ShowInTaskbar = true; Show();
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
        internal bool Enabled, Active, Fail, SignInWindowOpen, Busy;
        internal List<SpendLine> Spend = new List<SpendLine> {
            new SpendLine { item = "ai-codex-10080", text = "AI weekly allowance: 76% used, resets 20 Oct 2026, 05:04 UTC.", percent = 76, flags = new List<int> { 50, 75 } },
            new SpendLine { item = "quickcommerce", text = "Quick-commerce credits: 56 of 100 used, 44 left in 1 active pack(s).", percent = 56, flags = new List<int> { 50 } } };
        internal List<SpendAlert> SpendAlerts = new List<SpendAlert> {
            new SpendAlert { alert_id = "a1", kind = "threshold", message = "AI weekly allowance: 76% used (passed 75%)." },
            new SpendAlert { alert_id = "a2", kind = "notice", message = "A Rs 500 quick-commerce pack (2,500 credits) was bought." } };
        internal int Starts, Stops, Workers, SignIns;
        internal List<SignInNeed> Needs = new List<SignInNeed> {
            new SignInNeed { id = "indiamart:expired:2026-10-14T10:00:00+00:00", site = "indiamart", label = "IndiaMART", state = "expired" },
            new SignInNeed { id = "dropdash:sign_in_needed:never", site = "dropdash", label = "Dropdash", state = "sign_in_needed" } };
        internal List<string> PlatformLines = new List<string>();
        internal string ScoreLine;
        public Task<ResearchStatus> Call(string action) {
            if (Fail) throw new IOException("Controlled connection failure.");
            if (action == "start") { Enabled = true; Active = true; Starts++; }
            if (action == "stop") { Enabled = false; Active = false; Stops++; }
            if (action == "sign-in") { SignIns++; SignInWindowOpen = !Busy; }
            return Task.FromResult(new ResearchStatus { enabled = Enabled, active_processes = Active ? 1 : 0,
                state = Enabled ? "running" : "stopped", message = Enabled ? "Research is running." : "Research is stopped. Your saved work is kept.",
                spend = Spend, spend_alerts = SpendAlerts,
                sign_in = new SignInStatus { window_open = SignInWindowOpen, needs = Needs,
                    notice = action == "sign-in" && Busy ? "A research read is using the browser. Try again in a minute." : null,
                    line = SignInWindowOpen ? "The sign-in window is open." : "Sign-in expired: IndiaMART. Click Sign in to research sites.",
                    platform_lines = PlatformLines, score_line = ScoreLine,
                    trends_line = "Google Trends check: 1 of 3 signed-in reads saved complete results." } });
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
                    checks["sign_in_need_line_and_one_notification"] = window.Controls["SignInNeeds"].Text.StartsWith("Sign-in expired: IndiaMART") &&
                        window.NotificationsShown == 1 && window.SignInButton.Enabled;
                    checks["trends_check_line_under_sign_in"] = window.Controls["SignInNeeds"].Text.EndsWith(
                        "\nGoogle Trends check: 1 of 3 signed-in reads saved complete results.") &&
                        window.Controls["SignInNeeds"].Height >= 2 * window.Controls["SignInNeeds"].Font.Height &&
                        window.Controls["SpendLines"].Top >= window.Controls["SignInNeeds"].Bottom;
                    checks["spend_lines_and_one_combined_notification"] = window.Controls["SpendLines"].Text.StartsWith("AI weekly allowance: 76% used") &&
                        window.Controls["SpendLines"].Height > 0 && window.SpendNotificationsShown == 1 &&
                        window.LastSpendNotification.StartsWith("2 spending updates:");
                    await window.RefreshStatus();
                    checks["repeat_need_not_notified_again"] = window.NotificationsShown == 1;
                    controls.SpendAlerts.Add(new SpendAlert { alert_id = "a3", kind = "paused",
                        message = "AI waiting so it doesn't spend paid Codex credits (plan limit reached; resumes at 20 Oct 2026, 05:04 UTC)." });
                    await window.RefreshStatus();
                    checks["only_new_spend_alert_notified"] = window.SpendNotificationsShown == 2 &&
                        window.LastSpendNotification.StartsWith("AI waiting so it doesn't spend paid Codex credits");
                    controls.Busy = true; window.SignInButton.PerformClick(); await window.RefreshStatus();
                    checks["busy_notice_survives_refresh"] = window.Controls["SignInNeeds"].Text.StartsWith("A research read is using the browser") &&
                        window.SignInButton.Enabled;
                    controls.Busy = false; window.SignInButton.PerformClick();
                    checks["sign_in_button_action"] = controls.SignIns == 2 && !window.SignInButton.Enabled &&
                        window.Controls["SignInNeeds"].Text == "The sign-in window is open.\nGoogle Trends check: 1 of 3 signed-in reads saved complete results." && window.StartButton.Enabled;
                    controls.PlatformLines.Add("Dropdash needs you to sign in (Sign in to research sites).");
                    controls.PlatformLines.Add("Dropship India shows a paid plan or membership page instead of products.");
                    int linesBefore = window.Controls["SignInNeeds"].Height;
                    await window.RefreshStatus();
                    checks["platform_lines_in_sign_in_area"] = window.Controls["SignInNeeds"].Text ==
                        "The sign-in window is open.\nDropdash needs you to sign in (Sign in to research sites)." +
                        "\nDropship India shows a paid plan or membership page instead of products." +
                        "\nGoogle Trends check: 1 of 3 signed-in reads saved complete results." &&
                        window.Controls["SignInNeeds"].Height > linesBefore &&
                        window.Controls["SpendLines"].Top >= window.Controls["SignInNeeds"].Bottom;
                    controls.PlatformLines.Clear();
                    controls.ScoreLine = "Opportunity score: 3 types scored (1 sweet spot, 1 early bet, 0 proven but crowded, 1 too quiet).";
                    await window.RefreshStatus();
                    checks["score_line_after_trends_line"] = window.Controls["SignInNeeds"].Text.EndsWith(
                        "\nGoogle Trends check: 1 of 3 signed-in reads saved complete results.\n" + controls.ScoreLine) &&
                        window.Controls["SpendLines"].Top >= window.Controls["SignInNeeds"].Bottom;
                    controls.ScoreLine = null;
                    await window.RefreshStatus();
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
            bool passed = failure == null && checks.Count == 24;
            foreach (bool check in checks.Values) passed &= check;
            File.WriteAllText(output, new JavaScriptSerializer().Serialize(new { passed = passed, checks = checks, failure = failure,
                actual_research_started = false, provider_calls = 0, tested_at = DateTime.UtcNow.ToString("o") }));
            return passed ? 0 : 1;
        }
    }
}
