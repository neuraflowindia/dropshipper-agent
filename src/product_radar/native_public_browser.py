"""One page snapshot through an owned native Windows Chrome process.

Reuses the project's proven Chrome/PowerShell approach without the owner's
everyday profile, account action, paid service, or dependency installation.
The caller validates its destination and supplies a read-only DOM extractor.
Only this capture's verified process/profile may be closed after a timeout.

``profile="throwaway"`` (the default) reads hidden in a brand-new empty
profile. ``profile="signed-in"`` reads in the program's one lasting research
profile (see ``signed_in_browser``): a real window started minimised without
taking focus, one at a time, never while the owner's sign-in window is open or
the owner's Windows session is locked, and closed with the browser's own close
command before any forced end, so saved sign-ins survive.
"""

import base64
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile

from .models import SafetyError, timestamp
from .source_deadline import SourceActionDeadline

CHROME = Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe")
POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
MAX_RESULT = 12 * 1024 * 1024
PROFILES = ("throwaway", "signed-in")
# A signed-in read's PowerShell may run this long past ``timeout``: start-up,
# then the browser's own close (3 s connect, 10 s), the window close (5 s) and
# the forced end (5 s). The whole call (lock wait, first profile lookup, that
# run and a timed-out run's graceful cleanup) fits in ``signed_in_budget``.
SIGNED_IN_SCRIPT_MARGIN = 35
SIGNED_IN_OVERHEAD_SECONDS = 5 + 20 + SIGNED_IN_SCRIPT_MARGIN + 18
ARTIFACTS = ("native_result.json", "owned_process.json", "native_dispatch.json", "native_expression.js", "native_ready.js",
             "native_outer_deadline.json", "native_script.ps1")


def signed_in_budget(timeout=45):
    """Longest a ``profile="signed-in"`` call can take, in seconds."""
    return timeout + SIGNED_IN_OVERHEAD_SECONDS


# The command line only reads its request and runs the saved script, so long
# Windows paths can never approach Windows' command-line limit.
BOOTSTRAP = r"""
$ErrorActionPreference='Stop'
$request=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')))
. ([ScriptBlock]::Create([IO.File]::ReadAllText($request.script_file,[Text.Encoding]::UTF8)))
"""

SCRIPT = r"""
$request.expression=[IO.File]::ReadAllText($request.expression_file,[Text.Encoding]::UTF8)
if($request.ready_expression_file){$request.ready_expression=[IO.File]::ReadAllText($request.ready_expression_file,[Text.Encoding]::UTF8)}
$owned=$null;$socket=$null;$sequence=0;$wait=$null;$browserSocket=$null
$deadline=[DateTime]::UtcNow.AddSeconds([int]$request.timeout)
$result=@{state='failed';reason='Native browser did not complete';snapshot=$null;navigation_status=$null;retry_after=$null;navigation_started=$false;stage='browser_launch';requests=@()}
function Invoke-Cdp([string]$method,$parameters) {
  $script:sequence++
  $message=@{id=$script:sequence;method=$method;params=$parameters}|ConvertTo-Json -Compress -Depth 20
  $cancel=New-Object Threading.CancellationTokenSource
  $cancel.CancelAfter([int][Math]::Min(10000,[Math]::Max(1,($deadline-[DateTime]::UtcNow).TotalMilliseconds)))
  try {
    $bytes=[Text.Encoding]::UTF8.GetBytes($message)
    $segment=New-Object 'ArraySegment[byte]' -ArgumentList (,$bytes)
    $socket.SendAsync($segment,[Net.WebSockets.WebSocketMessageType]::Text,$true,$cancel.Token).GetAwaiter().GetResult()
    while([DateTime]::UtcNow -lt $deadline) {
      $stream=New-Object IO.MemoryStream
      do {
        $buffer=New-Object byte[] 65536
        $segment=New-Object 'ArraySegment[byte]' -ArgumentList (,$buffer)
        $received=$socket.ReceiveAsync($segment,$cancel.Token).GetAwaiter().GetResult()
        if($received.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close){throw 'Owned connection closed'}
        $stream.Write($buffer,0,$received.Count)
        if($stream.Length -gt 12582912){throw 'Snapshot limit exceeded'}
      } while(-not $received.EndOfMessage)
      $reply=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString($stream.ToArray()))
      $stream.Dispose()
      if($reply.method -eq 'Network.responseReceived' -and $reply.params.type -eq 'Document') {
        $retry=$reply.params.response.headers.'Retry-After'
        if($retry -isnot [string] -or $retry.Length -gt 256 -or $retry -match '[\x00-\x1f\x7f]'){$retry=$null}
        if($reply.params.frameId -eq $script:result.primary_frame_id){$script:result.navigation_status=[int]$reply.params.response.status;$script:result.retry_after=$retry}
        if($script:result.requests.Count -lt 5){$script:result.requests+=@{url=$reply.params.response.url;status=[int]$reply.params.response.status;frame=$reply.params.frameId;retry_after=$retry}}
      }
      if($reply.id -eq $script:sequence) {
        if($reply.error){throw 'Browser control failed'}
        return $reply.result
      }
    }
    throw 'Native browser deadline'
  } finally {$cancel.Dispose()}
}
try {
SIGNED_IN_START
  if(-not $owned){
  $args=@('--no-first-run','--disable-extensions','--disable-background-networking','--disable-default-apps','--window-size=1440,1100',('--user-data-dir='+$request.profile),'--remote-debugging-port=0','about:blank')
  if(-not $request.headed){$args=@('--headless=new','--disable-gpu')+$args}
  $quoted=($args|ForEach-Object {'"'+($_ -replace '"','\"')+'"'}) -join ' '
  $style=if($request.headed){'Normal'}else{'Hidden'}
  $owned=Start-Process -FilePath $request.browser -ArgumentList $quoted -PassThru -WindowStyle $style
  }
  [IO.File]::WriteAllText($request.ownership,(@{pid=$owned.Id;browser=$request.browser;profile=$request.profile}|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
  $portFile=Join-Path $request.profile 'DevToolsActivePort'
  while(-not (Test-Path $portFile) -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 150}
  if(-not (Test-Path $portFile)){throw 'Owned Chrome control port missing'}
  $port=[int](Get-Content $portFile -TotalCount 1)
  if($port -lt 1 -or $port -gt 65535){throw 'Owned control port invalid'}
  $rest=[int][Math]::Max(1,[Math]::Min(5,($deadline-[DateTime]::UtcNow).TotalSeconds))
  if($request.signed_in){$browserSocket=(Invoke-RestMethod -Uri ('http://127.0.0.1:'+$port+'/json/version') -TimeoutSec $rest).webSocketDebuggerUrl}
  $pages=Invoke-RestMethod -Uri ('http://127.0.0.1:'+$port+'/json/list') -TimeoutSec $rest
  $page=@($pages|Where-Object {$_.type -eq 'page'})[0]
  $socket=New-Object Net.WebSockets.ClientWebSocket
  $cancel=New-Object Threading.CancellationTokenSource
  $cancel.CancelAfter(5000)
  $socket.ConnectAsync([Uri]$page.webSocketDebuggerUrl,$cancel.Token).GetAwaiter().GetResult()
  $cancel.Dispose()
  $null=Invoke-Cdp 'Page.enable' @{}
  $null=Invoke-Cdp 'Network.enable' @{}
  if($request.signed_in){
    # The owner's sign-in window may have saved a maximised placement in the
    # shared profile: keep every read minimised, and record what it is.
    try {
      $window=Invoke-Cdp 'Browser.getWindowForTarget' @{}
      $null=Invoke-Cdp 'Browser.setWindowBounds' @{windowId=$window.windowId;bounds=@{windowState='minimized'}}
      $result.window_state=(Invoke-Cdp 'Browser.getWindowBounds' @{windowId=$window.windowId}).bounds.windowState
    } catch {$result.window_state='unknown'}
  }
  if($request.downloads){$null=Invoke-Cdp 'Browser.setDownloadBehavior' @{behavior='allow';downloadPath=$request.downloads}}
  else {$null=Invoke-Cdp 'Browser.setDownloadBehavior' @{behavior='deny'}}
  $result.stage='navigation';$result.navigation_started=$true;$result.navigation_started_at=[DateTime]::UtcNow.ToString('o')
  [IO.File]::WriteAllText($request.dispatch,(@{url=$request.url;navigation_started=$true;navigation_started_at=$result.navigation_started_at}|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
  $navigation=Invoke-Cdp 'Page.navigate' @{url=$request.url}
  $result.primary_frame_id=$navigation.frameId
  foreach($response in $result.requests){if($response.frame -eq $result.primary_frame_id){$result.navigation_status=$response.status;$result.retry_after=$response.retry_after}}
  do {
    Start-Sleep -Milliseconds 300
    if($request.ready_expression){
      $reply=Invoke-Cdp 'Runtime.evaluate' @{expression=$request.ready_expression;returnByValue=$true}
      $ready=($reply.result.value -eq $true)
    } else {
      $reply=Invoke-Cdp 'Runtime.evaluate' @{expression='JSON.stringify({ready:document.readyState,text:document.body?document.body.innerText.length:0})';returnByValue=$true}
      $state=ConvertFrom-Json $reply.result.value
      $ready=($state.ready -ne 'loading' -and $state.text -ge 100)
    }
  } while(-not $ready -and [DateTime]::UtcNow -lt $deadline.AddSeconds(-8))
  if([DateTime]::UtcNow -lt $deadline.AddSeconds(-5)){Start-Sleep -Milliseconds 1000}
  if($request.signed_in){
    # Evidence that a minimised page really renders (visibility, animation frames).
    try {
      $probe=Invoke-Cdp 'Runtime.evaluate' @{expression='new Promise(function(done){var late=setTimeout(function(){done(JSON.stringify({visibility:document.visibilityState,animation_frame:false}))},1000);requestAnimationFrame(function(){clearTimeout(late);done(JSON.stringify({visibility:document.visibilityState,animation_frame:true}))})})';awaitPromise=$true;returnByValue=$true}
      $seen=ConvertFrom-Json $probe.result.value;$result.page_visibility=[string]$seen.visibility;$result.animation_frame=($seen.animation_frame -eq $true)
    } catch {$result.page_visibility='unknown';$result.animation_frame=$null}
  }
  $result.stage='snapshot'
  $reply=Invoke-Cdp 'Runtime.evaluate' @{expression=$request.expression;returnByValue=$true}
  if($reply.exceptionDetails -or -not $reply.result.value){throw 'DOM extractor failed'}
  $result.snapshot=ConvertFrom-Json $reply.result.value
  if($request.downloads -and $result.snapshot.download_clicked){
    $result.stage='download'
    while(-not (Get-ChildItem $request.downloads -Filter '*.csv') -and [DateTime]::UtcNow -lt $deadline.AddSeconds(-3)){Start-Sleep -Milliseconds 200}
  }
  $result.state='captured';$result.reason='One rendered public page was captured';$result.stage='complete'
} catch {
  if($wait){$result.state='waiting';$result.reason=$wait}
  else {
  $result.state='failed'
  $result.reason='Native browser capture failed'
  $result.exception_type=$_.Exception.GetType().Name
  }
} finally {
  $result.completed_at=[DateTime]::UtcNow.ToString('o')
  if($socket){$socket.Dispose()}
  # Signed-in only, to protect saved sign-ins: the browser's own close
  # command; without it (no control socket, or the send failed) the window's
  # own close; the forced end below runs only if Chrome is still open.
  if($owned -and $request.signed_in){
    $sent=$false;$closer=$null
    if(-not $owned.HasExited -and $browserSocket){
      try {
        $closer=New-Object Net.WebSockets.ClientWebSocket;$cancel=New-Object Threading.CancellationTokenSource;$cancel.CancelAfter(3000)
        $closer.ConnectAsync([Uri]$browserSocket,$cancel.Token).GetAwaiter().GetResult()
        $bytes=[Text.Encoding]::UTF8.GetBytes('{"id":1,"method":"Browser.close"}')
        $closer.SendAsync((New-Object 'ArraySegment[byte]' -ArgumentList (,$bytes)),[Net.WebSockets.WebSocketMessageType]::Text,$true,$cancel.Token).GetAwaiter().GetResult()
        $sent=$true
      } catch {} finally {if($closer){$closer.Dispose()}}
    }
    if($sent){$null=$owned.WaitForExit(10000)}
    if($owned.HasExited){$result.close_method=if($sent){'browser_close'}else{'exited'}}
    else {
      & taskkill.exe /PID $owned.Id /T | Out-Null
      $result.close_method=if($owned.WaitForExit(5000)){'window_close'}else{'forced'}
    }
  }
  if($owned -and -not $owned.HasExited){& taskkill.exe /PID $owned.Id /T /F | Out-Null;$null=$owned.WaitForExit(5000)}
  $result.owned_process_exited=($null -eq $owned -or $owned.HasExited)
  [IO.File]::WriteAllText($request.output,($result|ConvertTo-Json -Depth 24),[Text.UTF8Encoding]::new($false))
}
"""

# Signed-in reads only, in place of the throwaway launch. The owner's session
# must be unlocked; program Chrome left behind is closed first, then the
# owner's plain sign-in window (a browser process without the remote-control
# flag) is never touched; a stale control-port file from an earlier run is
# removed before Chrome starts, so the old port is never mistaken for the new.
SIGNED_IN_START = r"""
  $session=(Get-Process -Id $PID).SessionId
  if($session -eq 0 -or @(Get-Process -Name LogonUI -ErrorAction SilentlyContinue|Where-Object {$_.SessionId -eq $session}).Count -or
     -not @(Get-Process -Name explorer -ErrorAction SilentlyContinue|Where-Object {$_.SessionId -eq $session}).Count){$wait='OWNER_DESKTOP_UNAVAILABLE';throw 'wait'}
CLOSE_LEFTOVERS
  Close-Leftovers $request.profile $deadline
  if((Get-SignInWindow $request.profile).Count){$result.sign_in_window_open=$true;$wait='OWNER_SIGN_IN_WINDOW_OPEN';throw 'wait'}
  $result.sign_in_window_open=$false
  $portFile=Join-Path $request.profile 'DevToolsActivePort'
  Remove-Item -LiteralPath $portFile -Force -ErrorAction SilentlyContinue
  if(Test-Path -LiteralPath $portFile){throw 'Stale control port remains'}
  $flags=@('--no-first-run','--disable-extensions','--disable-default-apps','--disable-sync','--window-size=1440,1100',
    '--disable-renderer-backgrounding','--disable-backgrounding-occluded-windows','--disable-background-timer-throttling',
    ('--user-data-dir='+$request.profile),'--remote-debugging-port=0','about:blank')
  $line='"'+$request.browser+'" '+(($flags|ForEach-Object {'"'+($_ -replace '"','\"')+'"'}) -join ' ')
  Add-Type -TypeDefinition @'
using System;using System.Diagnostics;using System.Runtime.InteropServices;using System.Text;
public static class NeuraFlowMinimisedStart {
  [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct Startup {public int cb;public string reserved,desktop,title;public int x,y,width,height,columns,rows,fill,flags;public short show,reserved2;public IntPtr reserved3,input,output,error;}
  [StructLayout(LayoutKind.Sequential)] struct Started {public IntPtr process,thread;public int id,threadId;}
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcessW(string application,StringBuilder line,IntPtr processAttributes,IntPtr threadAttributes,bool inherit,int creation,IntPtr environment,string directory,ref Startup startup,out Started started);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
  public static Process Start(string application,string line) {
    var startup=new Startup();startup.cb=Marshal.SizeOf(startup);startup.flags=1;startup.show=7;Started started;
    if(!CreateProcessW(application,new StringBuilder(line),IntPtr.Zero,IntPtr.Zero,false,0,IntPtr.Zero,null,ref startup,out started)) throw new System.ComponentModel.Win32Exception();
    // Hold our own process handle until the Process object has one, so the id cannot be reused in between.
    try { return Process.GetProcessById(started.id); } finally { CloseHandle(started.thread);CloseHandle(started.process); }
  }
}
'@
  $owned=[NeuraFlowMinimisedStart]::Start($request.browser,$line)
"""


def _waiting(reason):
    return {"state": "waiting", "reason": reason, "navigation_started": False, "stage": "browser_launch", "snapshot": None}


def _windows_path(path, runner):
    reply = runner(["wslpath", "-w", str(Path(path).absolute())], capture_output=True,
                   text=True, timeout=5, check=True)
    result = reply.stdout.strip()
    if not result or "\n" in result or "\r" in result:
        raise SafetyError("NATIVE_PUBLIC_PATH_INVALID")
    return result


def _command(script):
    return [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
            base64.b64encode(script.encode("utf-16le")).decode()]


def _cleanup_owned(folder, payload, runner):
    path = folder / "owned_process.json"
    if not path.exists():
        return
    if path.is_symlink() or path.stat().st_size > 16384:
        raise SafetyError("NATIVE_PUBLIC_OWNERSHIP_INVALID")
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    if (type(record.get("pid")) is not int or not 1 <= record["pid"] <= 2147483647
            or record.get("browser") != payload["browser"] or record.get("profile") != payload["profile"]):
        raise SafetyError("NATIVE_PUBLIC_OWNERSHIP_INVALID")
    # A signed-in profile is asked to close its window first (saved sign-ins).
    graceful = payload.get("signed_in") is True
    encoded = base64.b64encode(json.dumps(dict(record, graceful=graceful)).encode()).decode()
    script = r"""
$ErrorActionPreference='Stop'
$owned=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')))
$process=Get-CimInstance Win32_Process -Filter ('ProcessId='+$owned.pid)
if(-not $process){exit 0}
if($process.ExecutablePath -ine $owned.browser -or $process.CommandLine.IndexOf($owned.profile,[StringComparison]::OrdinalIgnoreCase) -lt 0 -or
   $process.CommandLine -notmatch '--remote-debugging-port'){exit 2}
if($owned.graceful){
  & taskkill.exe /PID $owned.pid /T | Out-Null
  $soft=[DateTime]::UtcNow.AddSeconds(8)
  do {
    if(-not (Get-CimInstance Win32_Process -Filter ('ProcessId='+$owned.pid))){exit 0}
    Start-Sleep -Milliseconds 200
  } while([DateTime]::UtcNow -lt $soft)
}
& taskkill.exe /PID $owned.pid /T /F | Out-Null
$deadline=[DateTime]::UtcNow.AddSeconds(3)
do {
  $remaining=Get-CimInstance Win32_Process -Filter ('ProcessId='+$owned.pid)
  if(-not $remaining){exit 0}
  Start-Sleep -Milliseconds 100
} while([DateTime]::UtcNow -lt $deadline)
exit 3
""".replace("PAYLOAD", encoded)
    if runner(_command(script), capture_output=True, timeout=18 if graceful else 8).returncode != 0:
        raise SafetyError("NATIVE_PUBLIC_CLEANUP_UNVERIFIED")


def _cleanup_status(folder, payload, runner):
    """Keep failed owned-process cleanup in the result, not an escaping error."""
    try:
        _cleanup_owned(folder, payload, runner)
        return True, None
    except (OSError, ValueError, subprocess.SubprocessError, SafetyError) as error:
        return False, str(error) if isinstance(error, SafetyError) else "NATIVE_PUBLIC_CLEANUP_UNVERIFIED"


@contextmanager
def _outer_deadline_cleanup(folder, url, runner):
    """An outer cancellation cleans only the proved owned native process.

    Path preparation cannot have launched Chrome. After invoking the native
    script, absence of its ownership/dispatch files is uncertainty, not proof
    that it never navigated or that the process exited.
    """
    lifecycle = {"payload": None, "launch_invoked": False}
    try:
        yield lifecycle
    except SourceActionDeadline as error:
        from .storage import atomic_json, load_json
        launched = lifecycle["launch_invoked"]
        payload = lifecycle["payload"]
        if not launched:
            closed, cleanup_reason = True, None
        elif (folder / "owned_process.json").exists():
            closed, cleanup_reason = _cleanup_status(folder, payload, runner)
        else:
            closed, cleanup_reason = None, "NATIVE_PUBLIC_OWNERSHIP_PENDING"
        result = {"state": "failed", "reason": "SOURCE_ACTION_TIMEOUT", "stage": "outer_deadline",
                  "outer_deadline": True, "requested_url": url, "snapshot": None,
                  "navigation_started": None if launched else False, "navigation_started_at": None,
                  "navigation_status": None, "owned_process_exited": closed,
                  "cleanup_reason": cleanup_reason, "completed_at": datetime.now(timezone.utc).isoformat(), "launch_invoked": launched}
        dispatch_path = folder / "native_dispatch.json"
        if launched and dispatch_path.is_file():
            try:
                dispatch = load_json(dispatch_path, 16384)
                if (isinstance(dispatch, dict) and set(dispatch) == {"url", "navigation_started", "navigation_started_at"}
                        and dispatch["url"] == url and dispatch["navigation_started"] is True
                        and timestamp(dispatch["navigation_started_at"]) <= timestamp(result["completed_at"])):
                    result.update(navigation_started=True, navigation_started_at=dispatch["navigation_started_at"])
            except (SafetyError, OSError, TypeError, ValueError):
                result["dispatch_proof_error"] = "NATIVE_PUBLIC_DISPATCH_UNVERIFIED"
        error.native_result = result
        try:
            path = folder / "native_outer_deadline.json"
            atomic_json(path, result)
            error.native_result = dict(result, cleanup_proof_reference=str(path))
        except (SafetyError, OSError):
            # A failed local proof save cannot turn cancellation into another
            # browser attempt or hide the original recovery-required intent.
            error.native_result["proof_save_error"] = "NATIVE_PUBLIC_DEADLINE_PROOF_SAVE_FAILED"
        raise


def native_snapshot(url, expression, folder, *, timeout=45, headed=False, runner=subprocess.run,
                    ready_expression=None, download_folder=None, profile="throwaway", root=None):
    """Read one caller-validated URL, return actual execution/DOM status.

    A signed-in read that cannot start now returns ``state="waiting"`` with
    one of ``signed_in_browser.WAIT_CODES`` and no navigation.
    """
    if (type(timeout) is not int or not 15 <= timeout <= 45 or type(headed) is not bool
            or profile not in PROFILES or profile == "signed-in" and headed
            or profile == "throwaway" and root is not None
            or not isinstance(expression, str) or len(expression) > 20000
            or ready_expression is not None and (not isinstance(ready_expression, str) or not ready_expression or len(ready_expression) > 20000)):
        raise SafetyError("NATIVE_PUBLIC_INPUT_INVALID")
    if not CHROME.is_file() or not POWERSHELL.is_file():
        return {"state": "unavailable", "reason": "NATIVE_PUBLIC_BROWSER_NOT_INSTALLED",
                "navigation_started": False, "stage": "browser_launch", "snapshot": None}
    from .storage import safe_path
    folder = safe_path(folder)
    if profile == "throwaway":
        return _snapshot(url, expression, folder, timeout, headed, runner, ready_expression, download_folder, None)
    from .signed_in_browser import (PROJECT_ROOT, browser_lock, desktop_unavailable, note_desktop_unavailable,
                                    note_sign_in_window, redact_signed_in, research_profile)
    root = PROJECT_ROOT if root is None else root
    if desktop_unavailable(root):
        return _waiting("OWNER_DESKTOP_UNAVAILABLE")
    with ExitStack() as stack:
        try:
            stack.enter_context(browser_lock(root))
        except SafetyError as error:
            if str(error) != "SIGNED_IN_BROWSER_BUSY":
                raise
            return _waiting("SIGNED_IN_BROWSER_BUSY")
        try:
            signed_in = research_profile(runner)
        except SafetyError as error:
            return {"state": "unavailable", "reason": str(error), "navigation_started": False,
                    "stage": "browser_launch", "snapshot": None}
        result = redact_signed_in(_snapshot(url, expression, folder, timeout, False, runner, ready_expression,
                                            download_folder, signed_in))
        _redact_saved(folder / "native_result.json", redact_signed_in)
        if result.get("reason") == "OWNER_DESKTOP_UNAVAILABLE":
            note_desktop_unavailable(root)
        if result.get("reason") == "OWNER_SIGN_IN_WINDOW_OPEN" or result.get("sign_in_window_open") is False:
            note_sign_in_window(root, result.get("reason") == "OWNER_SIGN_IN_WINDOW_OPEN")
        return result


def _redact_saved(path, redact):
    """The saved receipt keeps its own content, minus the account's identity."""
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_RESULT:
        return
    try:
        original = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return
    if redact(original) != original:
        from .storage import atomic_json
        atomic_json(path, redact(original))


def _snapshot(url, expression, folder, timeout, headed, runner, ready_expression, download_folder, signed_in):
    from .storage import safe_path
    if download_folder is not None and safe_path(download_folder) != folder:
        raise SafetyError("NATIVE_PUBLIC_DOWNLOAD_PATH_INVALID")
    if any((folder / name).exists() or (folder / name).is_symlink() for name in ARTIFACTS):
        raise SafetyError("NATIVE_PUBLIC_ARTIFACT_EXISTS")
    # Windows has a short process-command limit. Original DOM instructions
    # stay in this owned folder rather than being repeated inside the command.
    with (folder / "native_expression.js").open("x", encoding="utf-8") as stream:
        stream.write(expression)
    if ready_expression is not None:
        with (folder / "native_ready.js").open("x", encoding="utf-8") as stream:
            stream.write(ready_expression)
    from .signed_in_browser import CLOSE_LEFTOVERS
    with (folder / "native_script.ps1").open("x", encoding="utf-8") as stream:
        stream.write(SCRIPT.replace("SIGNED_IN_START", SIGNED_IN_START.replace("CLOSE_LEFTOVERS", CLOSE_LEFTOVERS)
                                    if signed_in is not None else ""))
    with ExitStack() as stack:
        temporary = None if signed_in else stack.enter_context(tempfile.TemporaryDirectory(
            prefix="owned-native-profile-", dir=folder, ignore_cleanup_errors=True))
        lifecycle = stack.enter_context(_outer_deadline_cleanup(folder, url, runner))
        payload = {"url": url, "expression": "", "timeout": timeout, "headed": headed, "signed_in": signed_in is not None,
                   "browser": _windows_path(CHROME, runner), "profile": signed_in or _windows_path(temporary, runner),
                   "output": _windows_path(folder / "native_result.json", runner),
                   "ownership": _windows_path(folder / "owned_process.json", runner),
                   "dispatch": _windows_path(folder / "native_dispatch.json", runner),
                   "expression_file": _windows_path(folder / "native_expression.js", runner),
                   "ready_expression": None,
                   "ready_expression_file": _windows_path(folder / "native_ready.js", runner) if ready_expression is not None else None,
                   "downloads": _windows_path(folder, runner) if download_folder is not None else None,
                   "script_file": _windows_path(folder / "native_script.ps1", runner)}
        lifecycle["payload"] = payload
        try:
            script = BOOTSTRAP.replace("PAYLOAD", base64.b64encode(json.dumps(payload).encode()).decode())
            lifecycle["launch_invoked"] = True
            # A signed-in read also checks the desktop and closes Chrome gracefully.
            response = runner(_command(script), capture_output=True,
                              timeout=timeout + (SIGNED_IN_SCRIPT_MARGIN if signed_in else 7))
            path = folder / "native_result.json"
            if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_RESULT:
                diagnostic = (response.stderr or b"")
                diagnostic = diagnostic.decode("utf-8", errors="replace") if isinstance(diagnostic, bytes) else diagnostic
                reason = ("NATIVE_PUBLIC_WSL_INTEROP_BLOCKED" if "UtilBindVsockAnyPort" in diagnostic else
                          "NATIVE_PUBLIC_SCRIPT_INVALID" if "ParserError" in diagnostic else "NATIVE_PUBLIC_RESULT_INVALID")
                may_have_navigated = (folder / "owned_process.json").exists()
                closed, cleanup_reason = _cleanup_status(folder, payload, runner)
                return {"state": "failed", "reason": cleanup_reason or reason, "exit_code": response.returncode, "snapshot": None,
                        "navigation_started": None if may_have_navigated else False, "stage": "result_read",
                        "owned_process_exited": closed}
            result = json.loads(path.read_text(encoding="utf-8-sig"))
            result["exit_code"] = response.returncode
            if result.get("owned_process_exited") is not True:
                closed, cleanup_reason = _cleanup_status(folder, payload, runner)
                result.update(state="failed", reason=cleanup_reason or "NATIVE_PUBLIC_CLEANUP_UNVERIFIED", owned_process_exited=closed)
            return result
        except subprocess.TimeoutExpired:
            closed, cleanup_reason = _cleanup_status(folder, payload, runner)
            return {"state": "failed", "reason": cleanup_reason or "NATIVE_PUBLIC_DEADLINE", "snapshot": None,
                    "navigation_started": None, "stage": "deadline", "owned_process_exited": closed}
        except (OSError, ValueError, subprocess.SubprocessError, SafetyError) as error:
            may_have_navigated = (folder / "owned_process.json").exists()
            closed, cleanup_reason = _cleanup_status(folder, payload, runner)
            reason = str(error) if isinstance(error, SafetyError) else "NATIVE_PUBLIC_EXECUTION_FAILED"
            return {"state": "failed", "reason": cleanup_reason or reason,
                    "snapshot": None, "navigation_started": None if may_have_navigated else False,
                    "stage": "result_read" if may_have_navigated else "browser_launch", "owned_process_exited": closed}
