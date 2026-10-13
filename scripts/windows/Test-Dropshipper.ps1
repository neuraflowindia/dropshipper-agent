<# Installed acceptance test. -ExerciseResearchControls really presses Start,
Stop and Exit and leaves research stopped. The ordinary --self-test uses fake
research instead. No other task, WSL distribution or application is stopped.
#>
[CmdletBinding()]
param([string] $RepositoryPath, [switch] $ExerciseResearchControls, [switch] $CloseOnly)
$ErrorActionPreference = 'Stop'
if (-not $RepositoryPath) { $RepositoryPath = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent }
$RepositoryPath = (Resolve-Path -LiteralPath $RepositoryPath).Path
if (-not $ExerciseResearchControls -and -not $CloseOnly) { throw 'Choose -ExerciseResearchControls for the actual Start/Stop test, or -CloseOnly to press Exit before rebuilding.' }
if ($ExerciseResearchControls -and $CloseOnly) { throw 'Choose the test or close-only action, not both.' }
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -ReferencedAssemblies Accessibility -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class DropshipperTestWindows {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
    [DllImport("oleacc.dll")] private static extern int AccessibleObjectFromWindow(IntPtr window, uint id, ref Guid iid,
        [MarshalAs(UnmanagedType.Interface)] out object accessible);
    public static void ClickControl(IntPtr window) {
        Guid iid = new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
        object accessible;
        int error = AccessibleObjectFromWindow(window, unchecked((uint)-4), ref iid, out accessible);
        if (error != 0) Marshal.ThrowExceptionForHR(error);
        ((Accessibility.IAccessible)accessible).accDoDefaultAction(0);
    }
}
'@
$shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'NeuraFlow Dropshipper.lnk'
$executable = Join-Path $RepositoryPath 'dist\windows\NeuraFlow Dropshipper.exe'
$shell = New-Object -ComObject WScript.Shell
$checks = [ordered]@{}
$phase = 'opening shortcut'
trap {
    $failure = $_
    # A failed interactive test must not leave test-started research running.
    # Use the same bounded local backend and no shell command string.
    try {
        $config = Get-Content (Join-Path $RepositoryPath 'dist\windows\launcher.json') -Raw | ConvertFrom-Json
        $info = [Diagnostics.ProcessStartInfo]::new()
        $info.FileName = Join-Path $env:WINDIR 'System32\wsl.exe'
        $info.Arguments = '--distribution ' + $config.Distribution + ' --cd "' + $config.LinuxRepository + '" --exec "' +
            $config.LinuxRepository + '/.venv/bin/python" "' + $config.LinuxRepository + '/scripts/desktop_research_control.py" stop'
        $info.UseShellExecute = $false; $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true
        $cleanup = [Diagnostics.Process]::Start($info)
        $output = $cleanup.StandardOutput.ReadToEndAsync(); $errors = $cleanup.StandardError.ReadToEndAsync()
        $completed = $cleanup.WaitForExit(20000)
        $cleanupState = if ($completed -and $cleanup.ExitCode -eq 0) { $output.Result | ConvertFrom-Json } else { @{state='unknown'} }
    } catch { $cleanupState = @{state='unknown'} }
    $failed = @{Passed=$false; Phase=$phase; CheckedAt=[DateTime]::UtcNow.ToString('o'); Checks=$checks; CleanupState=$cleanupState}
    $failurePath = Join-Path $RepositoryPath 'data\operations\desktop\installed-test-failure.json'
    [System.IO.File]::WriteAllText($failurePath, ($failed | ConvertTo-Json -Depth 6), [System.Text.UTF8Encoding]::new($false))
    throw $failure
}
function Wait-Until([scriptblock] $Condition, [int] $Seconds = 45) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (& $Condition) { return }
        Start-Sleep -Milliseconds 200
    }
    throw 'An installed launcher check timed out; no successful result was assumed.'
}
function Get-Window {
    $process = @(Get-Process -Name 'NeuraFlow Dropshipper' -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle.ToInt64() -ne 0 })
    if ($process.Count -ne 1) { return $null }
    return [System.Windows.Automation.AutomationElement]::FromHandle($process[0].MainWindowHandle)
}
function Find-Control([string] $Name) {
    $window = Get-Window
    if (-not $window) { return $null }
    # Framework WinForms exposes native handle IDs, not Control.Name, through
    # this Windows UI provider. Find the actual visible controls instead.
    if ($Name -eq 'ResearchState') {
        return @($window.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition) | Where-Object {
            $_.Current.Name -match '^(Research is |Controls need attention$|Checking research|Starting research|Stopping research)'
        })[0]
    }
    $label = @{StartResearch='Start'; StopResearch='Stop'; ExitResearch='Exit'}[$Name]
    $condition = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty, $label)
    return $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
}
function Invoke-Control([string] $Name) {
    $control = Find-Control $Name
    if (-not $control -or -not $control.Current.IsEnabled) { throw "The expected button is not enabled: $Name" }
    $pattern = $null
    if ($control.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { $pattern.Invoke() }
    else { [DropshipperTestWindows]::ClickControl([IntPtr]$control.Current.NativeWindowHandle) }
}
function Is-Stopped {
    $start = Find-Control 'StartResearch'; $stop = Find-Control 'StopResearch'
    return $start -and $stop -and $start.Current.IsEnabled -and -not $stop.Current.IsEnabled
}
if ($CloseOnly) {
    if (@(Get-Process -Name 'NeuraFlow Dropshipper' -ErrorAction SilentlyContinue).Count -eq 0) { return }
    $shell.Run(('"' + $shortcut + '"'), 1, $false) | Out-Null
    Wait-Until { $null -ne (Get-Window) }
    $process = @(Get-Process -Name 'NeuraFlow Dropshipper' | Where-Object {$_.MainWindowHandle.ToInt64() -ne 0})[0]
    Invoke-Control 'ExitResearch'
    Wait-Until { $process.HasExited } 90
    Write-Output 'The existing app stopped research and exited normally.'
    return
}
$shell.Run(('"' + $shortcut + '"'), 1, $false) | Out-Null
Wait-Until { $null -ne (Get-Window) }
Wait-Until { $heading = Find-Control 'ResearchState'; $heading -and $heading.Current.Name -ne 'Checking research…' }
$checks.DesktopShortcutOpensGraphicalWindow = $true
$phase = 'stopping the previous research run'
$firstProcess = @(Get-Process -Name 'NeuraFlow Dropshipper' | Where-Object {$_.MainWindowHandle.ToInt64() -ne 0})[0]
$stop = Find-Control 'StopResearch'
if ($stop.Current.IsEnabled) { Invoke-Control 'StopResearch' }
Wait-Until { Is-Stopped } 90
$checks.ExistingResearchStoppedThroughButton = $true
$phase = 'starting research'
Invoke-Control 'StartResearch'
Wait-Until { $heading = Find-Control 'ResearchState'; $heading -and $heading.Current.Name -eq 'Research is running' } 45
$checks.StartReallyStartsResearch = $true
$checks.StartDisabledWhileRunning = -not (Find-Control 'StartResearch').Current.IsEnabled
$phase = 'stopping the newly started research'
Invoke-Control 'StopResearch'
Wait-Until { Is-Stopped } 90
$checks.StopReallyStopsResearch = $true
$checks.StopDisabledWhileStopped = -not (Find-Control 'StopResearch').Current.IsEnabled
$phase = 'tray and repeat-shortcut behavior'
$handle = [IntPtr](Get-Window).Current.NativeWindowHandle
[DropshipperTestWindows]::ShowWindow($handle, 6) | Out-Null
Wait-Until { $null -eq (Get-Window) }
$checks.MinimizeRemovesTaskbarWindow = -not $firstProcess.HasExited
$shell.Run(('"' + $shortcut + '"'), 1, $false) | Out-Null
Wait-Until { $null -ne (Get-Window) }
$checks.SecondShortcutRestoresSameInstance = @(Get-Process -Name 'NeuraFlow Dropshipper' | Where-Object {$_.MainWindowHandle.ToInt64() -ne 0})[0].Id -eq $firstProcess.Id
$handle = [IntPtr](Get-Window).Current.NativeWindowHandle
[DropshipperTestWindows]::PostMessage($handle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
Wait-Until { $null -eq (Get-Window) }
$checks.XHidesInsteadOfExiting = -not $firstProcess.HasExited
$phase = 'Exit and scheduled stopped wake'
$shell.Run(('"' + $shortcut + '"'), 1, $false) | Out-Null
Wait-Until { $null -ne (Get-Window) }
Invoke-Control 'ExitResearch'
Wait-Until { $firstProcess.HasExited } 90
$checks.ExitStopsAndCloses = $true
# Invoke the real installed task manually while stopped. It must be invisible
# and stay off. This checks the task action, not a clock/logon trigger firing;
# that ordinary scheduled-wake evidence is recorded separately.
$schedule = New-Object -ComObject 'Schedule.Service'; $schedule.Connect()
$task = $schedule.GetFolder('\').GetTask('NeuraFlow-Dropshipper-Research')
$task.Run($null) | Out-Null
Wait-Until { $task = $schedule.GetFolder('\').GetTask('NeuraFlow-Dropshipper-Research'); $task.State -ne 4 } 45
$checks.ScheduledStoppedLaunchReturnsSuccessfully = $task.LastTaskResult -eq 0
$checks.ScheduledTaskUsesGraphicalExecutable = ([xml]$task.Xml).Task.Actions.Exec.Command -ieq $executable
$shell.Run(('"' + $shortcut + '"'), 1, $false) | Out-Null
Wait-Until { Is-Stopped }
$checks.ScheduleDidNotUndoStop = $true
$all = @(Get-CimInstance Win32_Process)
$related = @($all | Where-Object { $_.Name -eq 'wsl.exe' -and $_.CommandLine -match 'desktop_research_control\.py' })
$visibleConsoles = @($related | Where-Object { $process=Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; $process -and $process.MainWindowHandle.ToInt64() -ne 0 })
$checks.NoVisibleResearchCommandWindow = $visibleConsoles.Count -eq 0
$checks.ExecutableAndShortcutHaveCustomIcon = $shell.CreateShortcut($shortcut).IconLocation -ieq "$executable,0"
$passed = @($checks.Values | Where-Object { $_ -ne $true }).Count -eq 0
$result = [ordered]@{Passed=$passed; CheckedAt=[DateTime]::UtcNow.ToString('o'); Checks=$checks;
    ResearchState='stopped'; WindowLeftOpen=$true; ActualStartStopExercised=$true;
    ExecutableSHA256=(Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash;
    ProviderCalls='Not assumed zero: this test briefly starts the approved real runner.'}
$receipt = Join-Path $RepositoryPath 'data\operations\desktop\installed-test.json'
$encoded = $result | ConvertTo-Json -Depth 5
$history = Join-Path $RepositoryPath ('data\operations\desktop\installed-test-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '.json')
[System.IO.File]::WriteAllText($history, $encoded, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($receipt, $encoded, [System.Text.UTF8Encoding]::new($false))
$result | ConvertTo-Json -Depth 5
if (-not $passed) { exit 1 }
