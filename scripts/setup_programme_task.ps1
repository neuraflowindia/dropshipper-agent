<#
Print or explicitly install the one recurring owner task. This script does not
run research, change plans, acquire credits, approve reports or alter DNS.
Existing different tasks are never overwritten. An explicit cooperative upgrade
may only add the saved-turn option to the otherwise exact matching owner task.
After the desktop app is installed, use windows/Install-Dropshipper.ps1 instead.
This legacy direct-WSL setup deliberately refuses to overwrite that launcher.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [ValidatePattern('^[A-Za-z0-9_.-]{1,80}$')] [string] $Distribution,
    [Parameter(Mandatory = $true)] [string] $RepositoryPath,
    [switch] $LiveModel,
    [switch] $FundedQuickcommerceFallback,
    [switch] $CooperativeTurns,
    [switch] $UpgradeCooperativeTurns,
    [switch] $Install,
    [switch] $ValidateOnly
)
$ErrorActionPreference = 'Stop'
if ($UpgradeCooperativeTurns -and (-not $Install -or -not $CooperativeTurns -or $ValidateOnly)) {
    throw 'A cooperative upgrade requires -Install and -CooperativeTurns, not validation only.'
}
$taskName = 'NeuraFlow-Dropshipper-Research'
$ownerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$arguments = @('--distribution', $Distribution, '--cd', $RepositoryPath, '--exec', "$RepositoryPath/.venv/bin/python",
    "$RepositoryPath/scripts/run_programme.py", '--task-xml', '--distribution', $Distribution,
    '--windows-user', $ownerSid)
if ($LiveModel) { $arguments += '--live-model' }
if ($FundedQuickcommerceFallback) { $arguments += '--funded-quickcommerce-fallback' }
if ($CooperativeTurns) { $arguments += '--cooperative-turns' }
$xml = (& wsl.exe @arguments) -join "`n"
if ($LASTEXITCODE -ne 0) { throw 'The task definition could not be prepared. No task was installed.' }
[xml] $checked = $xml
if ($ValidateOnly -and $Install) { throw 'Choose validation or installation, not both.' }
if ($ValidateOnly) {
    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $definition = $scheduler.NewTask(0)
    $definition.XmlText = $xml
    # TASK_VALIDATE_ONLY (1) validates without registering or starting a task.
    $scheduler.GetFolder('\').RegisterTaskDefinition($taskName, $definition, 1, $ownerSid, $null, 3, $null) | Out-Null
    Write-Output 'Windows accepted the definition. No task was installed or started.'
    return
}
if (-not $Install) {
    Write-Output $xml
    Write-Output 'Definition only. Use -Install once the programme and owner-computer checks have passed.'
    return
}
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$folder = $scheduler.GetFolder('\')
$existing = $null
try {
    $existing = $folder.GetTask($taskName)
} catch {
    $failure = $_.Exception
    while ($failure.InnerException) { $failure = $failure.InnerException }
    # Only ERROR_FILE_NOT_FOUND proves absence; other read errors do not grant
    # permission to replace or blindly register anything.
    if ($failure.HResult -ne -2147024894) { throw 'The existing task could not be read. No task was changed.' }
}
if ($existing) {
    $existingXml = $existing.Xml
    $old = [xml] $existingXml
    # Windows may export a local StartBoundary without an offset. Interpret it
    # on this owner computer before the namespace-aware semantic comparison.
    function ConvertTo-NormalizedTaskDocument {
        param([xml] $document)
        $manager = New-Object System.Xml.XmlNamespaceManager($document.NameTable)
        $manager.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
        foreach ($node in $document.SelectNodes('/t:Task/t:Triggers/t:TimeTrigger/t:StartBoundary', $manager)) {
            $node.InnerText = ([DateTimeOffset]::Parse($node.InnerText)).ToUniversalTime().ToString('o')
        }
        # Windows may export the logon trigger as an account name while the
        # principal uses the same owner's SID. Resolve both on this computer;
        # never assume that different names mean the same owner or omit this
        # identity from comparison. Only these in-memory copies are changed.
        foreach ($node in $document.SelectNodes('/t:Task/t:Principals/t:Principal/t:UserId | /t:Task/t:Triggers/t:LogonTrigger/t:UserId', $manager)) {
            try {
                $identity = $node.InnerText.Trim()
                if ($identity -match '^S-1-(\d+-)+\d+$') {
                    $node.InnerText = ([System.Security.Principal.SecurityIdentifier]::new($identity)).Value
                } else {
                    $node.InnerText = ([System.Security.Principal.NTAccount]::new($identity)).Translate([System.Security.Principal.SecurityIdentifier]).Value
                }
            } catch {
                throw 'A task owner identity could not be resolved. The task was not changed.'
            }
        }
        return ,$document
    }
    $old = ConvertTo-NormalizedTaskDocument $old
    $checked = ConvertTo-NormalizedTaskDocument $checked
    $payload = @{ actual_xml = $old.OuterXml; expected_xml = $checked.OuterXml } | ConvertTo-Json -Compress
    $compareArguments = @('--distribution', $Distribution, '--cd', $RepositoryPath, '--exec', "$RepositoryPath/.venv/bin/python",
        "$RepositoryPath/scripts/run_programme.py", '--compare-task-xml')
    $comparison = ($payload | & wsl.exe @compareArguments) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw 'The existing task could not be verified. It was not changed.' }
    $meaning = $comparison | ConvertFrom-Json
    if ($meaning.matches -ne $true) {
        if (-not $UpgradeCooperativeTurns) {
            throw ('A different task with this name exists. It was not changed. Differences: ' + ($meaning.differences -join ', '))
        }
        $planArguments = @('--distribution', $Distribution, '--cd', $RepositoryPath, '--exec', "$RepositoryPath/.venv/bin/python",
            "$RepositoryPath/scripts/run_programme.py", '--plan-cooperative-upgrade')
        $planRaw = ($payload | & wsl.exe @planArguments) -join "`n"
        if ($LASTEXITCODE -ne 0) { throw 'The exact cooperative upgrade could not be verified. No task was changed.' }
        $plan = $planRaw | ConvertFrom-Json
        if ($plan.changed -ne $true) { throw 'No exact cooperative change was prepared. No task was changed.' }
        # A final read avoids applying a stale plan after another local edit.
        $latest = $folder.GetTask($taskName)
        if ($latest.Xml -ne $existingXml) { throw 'The existing task changed during verification. No task was changed.' }
        $replacement = $scheduler.NewTask(0)
        $replacement.XmlText = $plan.xml
        # TASK_UPDATE (4) only; never create a second task or start research.
        $folder.RegisterTaskDefinition($taskName, $replacement, 4, $ownerSid, $null, 3, $null) | Out-Null
        $readback = $folder.GetTask($taskName)
        $readbackDocument = ConvertTo-NormalizedTaskDocument ([xml] $readback.Xml)
        $readbackPayload = @{ actual_xml = $readbackDocument.OuterXml; expected_xml = $plan.xml } | ConvertTo-Json -Compress
        $verified = ($readbackPayload | & wsl.exe @compareArguments) -join "`n"
        if ($LASTEXITCODE -ne 0 -or (($verified | ConvertFrom-Json).matches -ne $true)) {
            throw 'The task was updated but its read-back needs review. No manual run was started.'
        }
        Write-Output 'The existing task now uses saved cooperative turns. No manual run was started.'
        return
    }
    Write-Output 'The matching task already exists. It was not replaced or started.'
    return
}
if ($UpgradeCooperativeTurns) { throw 'The original owner task does not exist. No upgrade or new task was installed.' }
$definition = $scheduler.NewTask(0)
$definition.XmlText = $xml
# TASK_CREATE (2), not CREATE_OR_UPDATE: a race-created task is never replaced.
$folder.RegisterTaskDefinition($taskName, $definition, 2, $ownerSid, $null, 3, $null) | Out-Null
Write-Output 'The recurring owner task is installed. No manual first run was started.'
