#!/usr/bin/env python3
"""Bounded public Trends export through an owned native Windows Chrome profile.

No personal profile, cookies, login, CAPTCHA bypass, provider key or paid route.
Chrome is controlled through its built-in connection by existing PowerShell;
no browser-automation dependency is installed. Only the owned PID is closed.
"""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, identifier, utc_now
from product_radar.providers.trends_browser import MAX_BYTES, parse_trends_csv
from product_radar.runtime_storage import runtime_path
from product_radar.storage import atomic_json, safe_path

CHROME = Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe")
POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def explore_url(term):
    if not isinstance(term, str) or not term.strip() or len(term) > 120 or re.search(r"[\x00-\x1f]", term):
        raise SafetyError("TRENDS_TERM_INVALID")
    return "https://trends.google.com/trends/explore?" + urlencode({"hl": "en", "geo": "IN", "date": "today 12-m", "q": term, "cat": "0"})


def verified_scope(snapshot, term):
    """Check actual rendered UI text, independently of the requested URL."""
    text = snapshot.get("text", "")
    if not isinstance(text, str):
        return False
    lines = [re.sub(r"\s+", " ", line).strip().casefold() for line in text.splitlines() if line.strip()]
    return (term.strip().casefold() in lines and "search term" in lines
            and all(label in lines for label in ("india", "past 12 months", "all categories", "web search")))


def _win(path, runner):
    result = runner(["wslpath", "-w", str(Path(path).absolute())], capture_output=True, text=True, check=True, timeout=10)
    value = result.stdout.strip()
    if not value or "\n" in value or "\r" in value:
        raise SafetyError("TRENDS_NATIVE_PATH_INVALID")
    return value


NATIVE_SCRIPT = r"""
$ErrorActionPreference='Stop'
$request=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')))
$owned=$null; $socket=$null; $sequence=0
$deadline=[DateTime]::UtcNow.AddSeconds([int]$request.timeout)
$result=@{state='failed';reason='Native capture did not complete';snapshot=$null;download_clicked=$false;requests=@()}
function Invoke-Cdp([string]$method,$parameters) {
  $script:sequence++
  $message=@{id=$script:sequence;method=$method;params=$parameters}|ConvertTo-Json -Compress -Depth 20
  $cancel=New-Object Threading.CancellationTokenSource
  $remaining=[int][Math]::Min(10000,[Math]::Max(1,($deadline-[DateTime]::UtcNow).TotalMilliseconds))
  $cancel.CancelAfter($remaining)
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
        if($received.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close){throw 'Owned browser connection closed'}
        $stream.Write($buffer,0,$received.Count)
        if($stream.Length -gt 10485760){throw 'Browser response exceeded capture limit'}
      } while(-not $received.EndOfMessage)
      $reply=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString($stream.ToArray()))
      $stream.Dispose()
      if($reply.method -eq 'Network.responseReceived' -and $result.requests.Count -lt 40) {
        $result.requests += @{url=$reply.params.response.url;status=$reply.params.response.status}
      }
      if($reply.id -eq $script:sequence) {
        if($reply.error){throw ('Browser control error: '+$reply.error.message)}
        return $reply.result
      }
    }
    throw 'Native capture deadline exceeded'
  } finally {$cancel.Dispose()}
}
function Save-Snapshot {
  $evaluation=Invoke-Cdp 'Runtime.evaluate' @{expression="JSON.stringify({url:location.href,title:document.title,text:document.body?document.body.innerText.slice(0,50000):'',buttons:Array.from(document.querySelectorAll('button,[role=button]')).slice(0,100).map(e=>({text:e.innerText,label:e.getAttribute('aria-label'),title:e.getAttribute('title')}))})";returnByValue=$true}
  $script:result.snapshot=ConvertFrom-Json $evaluation.result.value
  [IO.File]::WriteAllText($request.snapshot,($result.snapshot|ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
  $image=Invoke-Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
  [IO.File]::WriteAllBytes($request.screenshot,[Convert]::FromBase64String($image.data))
}
try {
  $args=@('--no-first-run','--disable-extensions','--disable-background-networking','--disable-default-apps','--window-size=1440,1100',('--user-data-dir='+$request.profile),'--remote-debugging-port=0','about:blank')
  if(-not $request.headed){$args=@('--headless=new','--disable-gpu')+$args}
  $quoted=($args|ForEach-Object {'"'+($_ -replace '"','\"')+'"'}) -join ' '
  $style=if($request.headed){'Normal'}else{'Hidden'}
  $owned=Start-Process -FilePath $request.browser -ArgumentList $quoted -PassThru -WindowStyle $style
  $result.owned_process_id=$owned.Id
  [IO.File]::WriteAllText($request.ownership,(@{pid=$owned.Id;browser=$request.browser;profile=$request.profile}|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
  $portFile=Join-Path $request.profile 'DevToolsActivePort'
  while(-not (Test-Path $portFile) -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 150}
  if(-not (Test-Path $portFile)){throw 'Owned Chrome did not expose its browser-control port'}
  $port=[int](Get-Content $portFile -TotalCount 1)
  if($port -lt 1 -or $port -gt 65535){throw 'Owned Chrome port is invalid'}
  $pages=Invoke-RestMethod -Uri ('http://127.0.0.1:'+$port+'/json/list') -TimeoutSec 10
  $page=@($pages|Where-Object {$_.type -eq 'page'})[0]
  if(-not $page.webSocketDebuggerUrl){throw 'Owned Chrome page is unavailable'}
  $socket=New-Object Net.WebSockets.ClientWebSocket
  $cancel=New-Object Threading.CancellationTokenSource
  $cancel.CancelAfter(10000)
  $socket.ConnectAsync([Uri]$page.webSocketDebuggerUrl,$cancel.Token).GetAwaiter().GetResult()
  $cancel.Dispose()
  $null=Invoke-Cdp 'Page.enable' @{}
  $null=Invoke-Cdp 'Network.enable' @{}
  $null=Invoke-Cdp 'Browser.setDownloadBehavior' @{behavior='allow';downloadPath=$request.downloads}
  $null=Invoke-Cdp 'Page.navigate' @{url=$request.url}
  do {
    Start-Sleep -Milliseconds 500
    $state=Invoke-Cdp 'Runtime.evaluate' @{expression="JSON.stringify({ready:document.readyState,text:document.body?document.body.innerText:''})";returnByValue=$true}
    $state=ConvertFrom-Json $state.result.value
  } while($state.text.Length -lt 100 -and [DateTime]::UtcNow -lt $deadline.AddSeconds(-15))
  Start-Sleep -Seconds 3
  Save-Snapshot
  $text=$result.snapshot.text
  if($text -match '(?i)unusual traffic|too many requests|too many queries|429|captcha|not a robot|sign in to continue') {
    $result.state='blocked';$result.reason='Rendered Google page shows a rate-limit or access challenge'
  } elseif($result.snapshot.url -notmatch '^https://trends\.google\.com/trends/explore') {
    $result.state='blocked';$result.reason='Google redirected away from public Trends Explore; no login or consent was performed'
  } else {
    $lines=@($text -split "`n"|ForEach-Object {$_.Trim().ToLowerInvariant()})
    $scope=($lines -contains $request.term.ToLowerInvariant()) -and ($lines -contains 'search term') -and ($lines -contains 'india') -and ($lines -contains 'past 12 months') -and ($lines -contains 'all categories') -and ($lines -contains 'web search')
    if(-not $scope){$result.state='insufficient';$result.reason='Rendered UI did not establish every required term and scope setting'}
    else {
      $expression=@'
JSON.stringify((()=>{let heading=Array.from(document.querySelectorAll('*')).find(e=>e.children.length===0&&e.textContent.trim()==='Interest over time');let node=heading;for(let n=0;node&&n<8;n++,node=node.parentElement){let button=Array.from(node.querySelectorAll('button,[role=button]')).find(e=>/download.*csv|csv/i.test((e.getAttribute('aria-label')||'')+' '+(e.getAttribute('title')||'')+' '+e.innerText));if(button){button.click();return{clicked:true,label:button.getAttribute('aria-label')||button.innerText}}}return{clicked:false}})())
'@
      $clicked=Invoke-Cdp 'Runtime.evaluate' @{expression=$expression;returnByValue=$true}
      $clicked=ConvertFrom-Json $clicked.result.value
      $result.download_clicked=$clicked.clicked
      $result.download_button=$clicked.label
      if(-not $clicked.clicked){$result.state='insufficient';$result.reason='Interest-over-time CSV export button was not found in the rendered widget'}
      else {
        while(-not (Get-ChildItem $request.downloads -Filter '*.csv') -and [DateTime]::UtcNow -lt $deadline.AddSeconds(-5)){Start-Sleep -Milliseconds 300}
        $result.state='captured';$result.reason='Interest-over-time export was requested through its rendered widget'
      }
    }
  }
} catch {
  $result.state='failed';$result.reason=$_.Exception.Message
  if($socket){try{Save-Snapshot}catch{}}
} finally {
  $result.completed_at=[DateTime]::UtcNow.ToString('o')
  if($socket){$socket.Dispose()}
  if($owned -and -not $owned.HasExited){& taskkill.exe /PID $owned.Id /T /F | Out-Null;$null=$owned.WaitForExit(5000)}
  $result.owned_process_exited=($null -eq $owned -or $owned.HasExited)
  [IO.File]::WriteAllText($request.native_result,($result|ConvertTo-Json -Depth 20),[Text.UTF8Encoding]::new($false))
}
"""


def _cleanup_owned(folder, payload, runner):
    """Timeout recovery can terminate only a verified task-owned Chrome PID."""
    record = folder / "owned_process.json"
    if not record.exists():
        return
    ownership = json.loads(record.read_text(encoding="utf-8-sig"))
    if (type(ownership.get("pid")) is not int or not 1 <= ownership["pid"] <= 2147483647
            or ownership.get("browser") != payload["browser"] or ownership.get("profile") != payload["profile"]):
        raise SafetyError("TRENDS_OWNED_PROCESS_MISMATCH")
    data = base64.b64encode(json.dumps(ownership).encode()).decode()
    script = r"""
$ErrorActionPreference='Stop'
$owned=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')))
$process=Get-CimInstance Win32_Process -Filter ('ProcessId='+$owned.pid)
if(-not $process){exit 0}
if($process.ExecutablePath -ine $owned.browser -or $process.CommandLine.IndexOf($owned.profile,[StringComparison]::OrdinalIgnoreCase) -lt 0){exit 2}
& taskkill.exe /PID $owned.pid /T /F | Out-Null
""".replace("PAYLOAD", data)
    command = [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
               base64.b64encode(script.encode("utf-16le")).decode()]
    result = runner(command, capture_output=True, timeout=15)
    if result.returncode != 0:
        raise SafetyError("TRENDS_OWNED_PROCESS_CLEANUP_UNVERIFIED")


def capture(term, attempt_id, *, headed=False, timeout=90, runner=subprocess.run):
    identifier(attempt_id)
    url = explore_url(term)
    if type(timeout) is not int or not 20 <= timeout <= 90:
        raise SafetyError("TRENDS_CAPTURE_TIMEOUT_INVALID")
    if not CHROME.is_file() or not POWERSHELL.is_file():
        raise SafetyError("TRENDS_NATIVE_BROWSER_UNAVAILABLE")
    folder = safe_path(runtime_path(ROOT, "media/live/trends"), attempt_id)
    if folder.exists():
        raise SafetyError("TRENDS_CAPTURE_ALREADY_EXISTS")
    folder.mkdir(parents=True)
    started = utc_now()
    receipt = {"schema_version": 1, "route": "owned_native_windows_chrome", "term": term, "geo": "IN",
               "time_range": "today 12-m", "kind": "search_term", "property": "web", "scope_verified": False,
               "started_at": started, "collected_at": started, "source_reference": url, "headed": bool(headed),
               "state": "Failed", "reason": "Native capture did not complete", "provider_requests": 0,
               "provider_cost_usd": None, "csv_path": None, "screenshot_path": str(folder / "page.png")}
    with tempfile.TemporaryDirectory(prefix=".owned-profile-", dir=folder) as temporary:
        payload = {"browser": _win(CHROME, runner), "profile": _win(Path(temporary), runner),
                   "downloads": _win(folder, runner), "snapshot": _win(folder / "page.json", runner),
                   "screenshot": _win(folder / "page.png", runner), "native_result": _win(folder / "native_result.json", runner),
                   "ownership": _win(folder / "owned_process.json", runner),
                   "url": url, "term": term, "headed": bool(headed), "timeout": timeout}
        script = NATIVE_SCRIPT.replace("PAYLOAD", base64.b64encode(json.dumps(payload).encode()).decode())
        command = [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                   base64.b64encode(script.encode("utf-16le")).decode()]
        try:
            completed = runner(command, capture_output=True, timeout=timeout + 30)
            native_file = folder / "native_result.json"
            native = json.loads(native_file.read_text(encoding="utf-8-sig")) if native_file.exists() else {}
            snapshot = native.get("snapshot") or {}
            receipt.update(collected_at=native.get("completed_at", utc_now()), reason=native.get("reason", "Native browser produced no receipt"),
                           state={"blocked": "Blocked", "insufficient": "Insufficient", "failed": "Failed", "captured": "Insufficient"}.get(native.get("state"), "Failed"),
                           exit_code=completed.returncode, actual_url=snapshot.get("url"), scope_verified=verified_scope(snapshot, term),
                           observed_requests=native.get("requests", []), owned_process_id=native.get("owned_process_id"),
                           owned_process_exited=native.get("owned_process_exited"))
            csv_files = list(folder.glob("*.csv"))
            if len(csv_files) == 1 and receipt["scope_verified"]:
                csv_path = csv_files[0]
                if csv_path.is_symlink() or csv_path.stat().st_size > MAX_BYTES:
                    receipt.update(state="Failed", reason="CSV exceeds the capture limit or is an unsafe file")
                else:
                    raw = csv_path.read_bytes()
                    parsed = parse_trends_csv(raw, receipt)
                    atomic_json(folder / "parsed.json", parsed)
                    receipt.update(state=parsed["state"], reason=parsed["reason"], csv_path=str(csv_path),
                                   csv_sha256=hashlib.sha256(raw).hexdigest(), intervals=len(parsed["series"]))
            elif native.get("state") == "captured":
                receipt.update(state="Insufficient", reason="CSV download did not complete with one usable file and verified rendered scope")
        except subprocess.TimeoutExpired:
            receipt.update(state="Failed", reason="Owned native browser capture exceeded its bounded deadline")
            try:
                _cleanup_owned(folder, payload, runner)
                receipt["cleanup_state"] = "verified_owned_process_only"
            except (SafetyError, OSError, ValueError, subprocess.SubprocessError) as error:
                receipt["cleanup_state"] = "unverified"
                receipt["reason"] += "; " + (str(error) if isinstance(error, SafetyError) else type(error).__name__)
        except (OSError, ValueError) as error:
            receipt.update(state="Failed", reason="Native capture artifact could not be read: " + type(error).__name__)
    atomic_json(folder / "receipt.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--term", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(capture(args.term, args.attempt_id, headed=args.headed), sort_keys=True))
        return 0
    except SafetyError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
