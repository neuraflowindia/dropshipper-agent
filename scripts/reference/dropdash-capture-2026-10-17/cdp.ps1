param([string]$Mode,[string]$JobFile)
# Read-only Dropdash capture helper. Modes: launch | run <job.json> | close.
# Never reads cookies, storage, headers or tokens. Records only response bodies
# of product-list API calls, DOM product cards and category links.
$ErrorActionPreference='Stop'
$profile=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'NeuraFlowDropshipper\research-browser-profile'
$chrome='C:\Program Files\Google\Chrome\Application\chrome.exe'
$portFile=Join-Path $profile 'DevToolsActivePort'
function Get-ProfileBrowsers { @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'"|Where-Object {$_.CommandLine -and $_.CommandLine.IndexOf($profile,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and $_.CommandLine -notmatch '--type='}) }

if($Mode -eq 'status'){
  foreach($p in @(Get-ProfileBrowsers)){ Write-Output ("pid=" + $p.ProcessId + " debug=" + ($p.CommandLine -match '--remote-debugging-port')) }
  Write-Output ("count=" + @(Get-ProfileBrowsers).Count)
  exit 0
}
if($Mode -eq 'launch'){
  if(@(Get-ProfileBrowsers).Count){ Write-Output 'ALREADY_RUNNING'; exit 3 }
  Remove-Item -LiteralPath $portFile -Force -ErrorAction SilentlyContinue
  $flags=@('--no-first-run','--disable-extensions','--disable-default-apps','--disable-sync','--window-size=1440,1100',
    '--disable-renderer-backgrounding','--disable-backgrounding-occluded-windows','--disable-background-timer-throttling',
    ('--user-data-dir='+$profile),'--remote-debugging-port=0','about:blank')
  $quoted=($flags|ForEach-Object {'"'+($_ -replace '"','\"')+'"'}) -join ' '
  $p=Start-Process -FilePath $chrome -ArgumentList $quoted -PassThru -WindowStyle Minimized
  $until=[DateTime]::UtcNow.AddSeconds(30)
  while(-not (Test-Path $portFile) -and [DateTime]::UtcNow -lt $until){Start-Sleep -Milliseconds 200}
  if(-not (Test-Path $portFile)){ Write-Output 'NO_PORT'; exit 4 }
  Write-Output ("LAUNCHED pid=" + $p.Id + " port=" + (Get-Content $portFile -TotalCount 1))
  exit 0
}

$port=[int](Get-Content $portFile -TotalCount 1)
$script:seq=0;$script:pending=$null;$script:buf=$null;$script:stream=New-Object IO.MemoryStream
$script:replies=@{};$script:net=@{};$script:finished=New-Object System.Collections.ArrayList

function Connect-Ws([string]$url){
  $s=New-Object Net.WebSockets.ClientWebSocket
  $s.Options.KeepAliveInterval=[TimeSpan]::FromSeconds(20)
  $s.ConnectAsync([Uri]$url,[Threading.CancellationToken]::None).Wait(8000)|Out-Null
  if($s.State -ne 'Open'){throw 'ws connect failed'}
  return $s
}
function Handle-Message($m){
  if($null -ne $m.id){ $script:replies[[int]$m.id]=$m; return }
  switch($m.method){
    'Network.responseReceived' {
      $t=[string]$m.params.type
      if($t -eq 'XHR' -or $t -eq 'Fetch' -or $t -eq 'Document'){
        $script:net[[string]$m.params.requestId]=@{url=[string]$m.params.response.url;status=[int]$m.params.response.status;type=$t;mime=[string]$m.params.response.mimeType;t=[DateTime]::UtcNow.ToString('o')}
      }
    }
    'Network.loadingFinished' { if($script:net.ContainsKey([string]$m.params.requestId)){ [void]$script:finished.Add([string]$m.params.requestId) } }
  }
}
# Drain incoming messages for up to $ms milliseconds without cancelling the socket.
function Pump([int]$ms){
  $until=[DateTime]::UtcNow.AddMilliseconds($ms)
  while($true){
    if($null -eq $script:pending){
      $script:buf=New-Object byte[] 262144
      $script:pending=$script:ws.ReceiveAsync((New-Object 'ArraySegment[byte]' -ArgumentList (,$script:buf)),[Threading.CancellationToken]::None)
    }
    $left=[int][Math]::Max(0,($until-[DateTime]::UtcNow).TotalMilliseconds)
    if(-not $script:pending.Wait($left)){ return }
    $r=$script:pending.Result;$script:pending=$null
    if($r.MessageType -eq 'Close'){throw 'socket closed'}
    $script:stream.Write($script:buf,0,$r.Count)
    if($r.EndOfMessage){
      $text=[Text.Encoding]::UTF8.GetString($script:stream.ToArray());$script:stream.SetLength(0)
      Handle-Message (ConvertFrom-Json $text)
    }
    if([DateTime]::UtcNow -ge $until){ return }
  }
}
function Cdp([string]$method,$params,[int]$timeoutMs=30000){
  $script:seq++;$id=$script:seq
  $msg=@{id=$id;method=$method;params=$params}|ConvertTo-Json -Compress -Depth 20
  $bytes=[Text.Encoding]::UTF8.GetBytes($msg)
  $script:ws.SendAsync((New-Object 'ArraySegment[byte]' -ArgumentList (,$bytes)),'Text',$true,[Threading.CancellationToken]::None).Wait(8000)|Out-Null
  $until=[DateTime]::UtcNow.AddMilliseconds($timeoutMs)
  while(-not $script:replies.ContainsKey($id)){
    if([DateTime]::UtcNow -ge $until){throw ('cdp timeout '+$method)}
    Pump 500
  }
  $rep=$script:replies[$id];$script:replies.Remove($id)
  if($rep.error){throw ('cdp error '+$method+' '+$rep.error.message)}
  return $rep.result
}
function Eval([string]$expr){
  $r=Cdp 'Runtime.evaluate' @{expression=$expr;returnByValue=$true;awaitPromise=$true}
  if($r.exceptionDetails){throw ('eval failed: '+$r.exceptionDetails.text)}
  return $r.result.value
}

if($Mode -eq 'close'){
  $v=Invoke-RestMethod -Uri ('http://127.0.0.1:'+$port+'/json/version') -TimeoutSec 5
  $script:ws=Connect-Ws $v.webSocketDebuggerUrl
  try { $null=Cdp 'Browser.close' @{} 5000 } catch {}
  Start-Sleep -Seconds 3
  Write-Output ("remaining=" + @(Get-ProfileBrowsers).Count)
  exit 0
}

if($Mode -eq 'eval'){
  $pages=Invoke-RestMethod -Uri ('http://127.0.0.1:'+$port+'/json/list') -TimeoutSec 5
  $page=@($pages|Where-Object {$_.type -eq 'page'})[0]
  $script:ws=Connect-Ws $page.webSocketDebuggerUrl
  $expr=[IO.File]::ReadAllText($JobFile,[Text.Encoding]::UTF8)
  Write-Output (Eval $expr)
  $script:ws.Dispose()
  exit 0
}

# ---- run a job ----
$job=Get-Content -Raw -Encoding UTF8 $JobFile | ConvertFrom-Json
$js=[IO.File]::ReadAllText($job.js_file,[Text.Encoding]::UTF8)
$log=New-Object System.Collections.ArrayList
function Log([string]$s){ [void]$log.Add(([DateTime]::UtcNow.ToString('o')+' '+$s)) }
$out=@{job=$job.name;url=$job.url;started_at=[DateTime]::UtcNow.ToString('o');state='failed';loads=@();bodies=@();signed_out=$false;stop_reason=$null}
$name=$null
try {
  $pages=Invoke-RestMethod -Uri ('http://127.0.0.1:'+$port+'/json/list') -TimeoutSec 5
  $page=@($pages|Where-Object {$_.type -eq 'page'})[0]
  $script:ws=Connect-Ws $page.webSocketDebuggerUrl
  $null=Cdp 'Page.enable' @{}
  $null=Cdp 'Network.enable' @{maxResourceBufferSize=20000000;maxTotalBufferSize=200000000}
  try {$null=Cdp 'Emulation.setFocusEmulationEnabled' @{enabled=$true}} catch {}
  try {$null=Cdp 'Emulation.setDeviceMetricsOverride' @{width=1280;height=640;deviceScaleFactor=1;mobile=$false}} catch {}
  $null=Cdp 'Browser.setDownloadBehavior' @{behavior='deny'}
  Log 'navigate'
  $out.navigated_at=[DateTime]::UtcNow.ToString('o')
  $null=Cdp 'Page.navigate' @{url=$job.url}
  # wait for cards or a settled page
  $until=[DateTime]::UtcNow.AddSeconds(40)
  do { Pump 1000; $st=Eval "JSON.stringify({u:location.href,r:document.readyState,n:document.querySelectorAll('a[href*=`"productDetails`"]').length,len:(document.body?document.body.innerText.length:0)})" | ConvertFrom-Json }
  while(($st.r -eq 'loading' -or ($st.n -lt 1 -and $st.len -lt 400) -or ($job.expect_cards -and $st.n -lt 1)) -and [DateTime]::UtcNow -lt $until)
  Pump 2500
  $state=Eval ($js+"`n;JSON.stringify(ddState())") | ConvertFrom-Json
  $out.landed=$state.url;$out.greeted=$state.greeted;$out.page_title=$state.title;$out.flags=$state.flags
  $name=$state.who
  if($state.signin){ $out.signed_out=$true;$out.stop_reason='SIGNED_OUT';$out.signed_out_at=[DateTime]::UtcNow.ToString('o');throw 'signed out' }
  if($state.blocked){ $out.stop_reason='BLOCKED';throw 'blocked' }
  $snap=Eval ($js+"`n;JSON.stringify(ddCollect())") | ConvertFrom-Json
  $out.loads+=@{at=[DateTime]::UtcNow.ToString('o');total=[int]$snap.total}
  Log ('initial cards '+$snap.total)
  if($job.scroll){
    $flat=0;$i=0
    while($flat -lt [int]$job.flat_stop -and $i -lt [int]$job.max_loads){
      $i++
      $null=Eval ($js+"`n;ddScroll()")
      $wait=Get-Random -Minimum 4000 -Maximum 8001
      Pump $wait
      $state=Eval ($js+"`n;JSON.stringify(ddState())") | ConvertFrom-Json
      if($state.signin){ $out.signed_out=$true;$out.stop_reason='SIGNED_OUT';$out.signed_out_at=[DateTime]::UtcNow.ToString('o');break }
      if($state.blocked){ $out.stop_reason='BLOCKED';break }
      $before=[int]$snap.total
      $snap=Eval ($js+"`n;JSON.stringify(ddCollect())") | ConvertFrom-Json
      $out.loads+=@{at=[DateTime]::UtcNow.ToString('o');total=[int]$snap.total;wait_ms=$wait}
      if([int]$snap.total -gt $before){$flat=0}else{$flat++}
    }
    if(-not $out.stop_reason){ $out.stop_reason=if($i -ge [int]$job.max_loads){'MAX_LOADS'}else{'NO_GROWTH'} }
  } else { $out.stop_reason='NO_SCROLL' }
  Pump 1500
  $out.dom=Eval ($js+"`n;JSON.stringify(ddFinal())") | ConvertFrom-Json
  $out.state='captured'
} catch {
  if(-not $out.stop_reason){$out.stop_reason='ERROR'}
  $out.error=[string]$_.Exception.Message
} finally {
  # Response bodies of the app's own data requests (never headers).
  $keep=[regex]'(?i)product|categor|home|section|banner|listing|search|catalog'
  $skip=[regex]'(?i)user|profile|account|auth|login|signin|otp|wallet|order|cart|payment|plan|subscri|token|session|notification|kyc|bank|address'
  foreach($rid in @($script:finished)){
    $n=$script:net[$rid]
    $u=[Uri]$n.url
    if($n.type -eq 'Document'){ if($u.Host -eq 'app.dropdash.co'){ $out.bodies+=@{url=$n.url;status=$n.status;type=$n.type;mime=$n.mime;body=$null} }; continue }
    $path=$u.AbsolutePath
    if($u.Host -ne 'api.dropdash.co' -or -not $keep.IsMatch($path) -or $skip.IsMatch($path)){ continue }
    $entry=@{url=$n.url;status=$n.status;type=$n.type;mime=$n.mime;t=$n.t;body=$null;kept=$false}
    if($keep.IsMatch($path) -and -not $skip.IsMatch($path) -and $n.mime -match 'json'){
      try { $b=Cdp 'Network.getResponseBody' @{requestId=$rid} 15000; if(-not $b.base64Encoded){$entry.body=[string]$b.body;$entry.kept=$true} } catch { $entry.body_error=[string]$_.Exception.Message }
    }
    $out.bodies+=$entry
  }
  $out.finished_at=[DateTime]::UtcNow.ToString('o')
  $out.log=$log
  if($script:ws){ try{$script:ws.Dispose()}catch{} }
  $text=$out|ConvertTo-Json -Depth 30 -Compress
  # Strip query values that look like credentials from recorded addresses.
  $text=[regex]::Replace($text,'(?i)([?&](?:token|access_token|auth|key|apikey|api_key|session|sig|signature|jwt)=)[^&"\\]*','$1[removed]')
  # The account holder's name (from the page greeting) and phone-like numbers never reach the file.
  if($name){
    $parts=@($name) + @(($name -split '\s+')|Where-Object {$_.Length -ge 3})
    foreach($p in $parts){ $text=[regex]::Replace($text,('(?i)\b'+[regex]::Escape($p)+'\b'),'[name removed]') }
  }
  $text=[regex]::Replace($text,'(?<![0-9])(?:\+?91[\s-]?)?[6-9][0-9]{9}(?![0-9])','[phone removed]')
  $name=$null
  [IO.File]::WriteAllText($job.output,$text,[Text.UTF8Encoding]::new($false))
  Write-Output ('DONE '+$out.state+' '+$out.stop_reason)
}
