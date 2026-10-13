<# Install/update the one project launcher and desktop shortcut.
Preserves the original task's owner, triggers, conditions, privileges and
research flags. Only the executable action changes. No research is started.
#>
[CmdletBinding()]
param(
    [string] $RepositoryPath,
    [ValidatePattern('^[A-Za-z0-9_.-]{1,80}$')] [string] $Distribution = 'Ubuntu',
    [switch] $Install
)
$ErrorActionPreference = 'Stop'
if (-not $RepositoryPath) { $RepositoryPath = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent }
$RepositoryPath = (Resolve-Path -LiteralPath $RepositoryPath).Path
$taskName = 'NeuraFlow-Dropshipper-Research'
$executable = Join-Path $RepositoryPath 'dist\windows\NeuraFlow Dropshipper.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'Build the desktop app before installing it.' }
$linuxRepository = '/' + ($RepositoryPath.Substring(0,1).ToLowerInvariant() | ForEach-Object { 'mnt/' + $_ }) + $RepositoryPath.Substring(2).Replace('\','/')
if ($RepositoryPath -notmatch '^[A-Za-z]:\\' -or $RepositoryPath.Contains('"')) { throw 'A local drive project path is required.' }
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$folder = $scheduler.GetFolder('\')
$task = $folder.GetTask($taskName)
$originalXml = $task.Xml
[xml] $document = $originalXml
$namespace = New-Object System.Xml.XmlNamespaceManager($document.NameTable)
$namespace.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
$actions = $document.SelectNodes('/t:Task/t:Actions/t:Exec', $namespace)
if ($actions.Count -ne 1 -or $document.SelectSingleNode('/t:Task/t:Actions', $namespace).ChildNodes.Count -ne 1) { throw 'The existing research task has unexpected actions. It was not changed.' }
$action = $actions[0]
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$owner = $document.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:UserId', $namespace).InnerText
$resolvedOwner = if ($owner.StartsWith('S-1-')) { $owner } else { ([System.Security.Principal.NTAccount]::new($owner)).Translate([System.Security.Principal.SecurityIdentifier]).Value }
if ($resolvedOwner -ne $currentSid -or $document.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:LogonType', $namespace).InnerText -ne 'InteractiveToken') {
    throw 'The research task does not belong to this signed-in owner. It was not changed.'
}
$oldCommand = $action.SelectSingleNode('t:Command', $namespace).InnerText
$oldArguments = $action.SelectSingleNode('t:Arguments', $namespace).InnerText
$configPath = Join-Path (Split-Path $executable -Parent) 'launcher.json'
if ([System.IO.Path]::GetFileName($oldCommand) -ieq 'wsl.exe') {
    $expected = '--distribution ' + $Distribution + ' --cd "' + $linuxRepository + '" --exec "' + $linuxRepository + '/.venv/bin/python" "' + $linuxRepository + '/scripts/run_programme.py" --tick --execute'
    if (-not $oldArguments.StartsWith($expected, [StringComparison]::Ordinal)) { throw 'The existing WSL action is not this project''s expected research command.' }
    $suffix = $oldArguments.Substring($expected.Length)
    $flags = @($suffix.Split(' ', [StringSplitOptions]::RemoveEmptyEntries))
    $allowed = @('--live-model', '--funded-quickcommerce-fallback', '--cooperative-turns')
    if (@($flags | Where-Object { $_ -notin $allowed }).Count -gt 0 -or @($flags | Select-Object -Unique).Count -ne $flags.Count) { throw 'Unexpected research flags. Nothing was changed.' }
    $researchArguments = @('--tick', '--execute') + $flags
} elseif ($oldCommand -ieq $executable -and $oldArguments -eq '--worker' -and (Test-Path -LiteralPath $configPath)) {
    $oldConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($oldConfig.WindowsRepository -ine $RepositoryPath -or $oldConfig.LinuxRepository -cne $linuxRepository -or $oldConfig.Distribution -cne $Distribution) { throw 'The installed launcher settings describe a different project.' }
    $researchArguments = @($oldConfig.ResearchArguments)
} else { throw 'A different action is installed under the research task name. It was not replaced.' }
$allowedArguments = @('--tick', '--execute', '--live-model', '--funded-quickcommerce-fallback', '--cooperative-turns')
if (@($researchArguments | Where-Object { $_ -notin $allowedArguments }).Count -gt 0 -or
    '--tick' -notin $researchArguments -or '--execute' -notin $researchArguments -or
    @($researchArguments | Select-Object -Unique).Count -ne $researchArguments.Count) {
    throw 'The saved research options are invalid. No task or shortcut was changed.'
}
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'NeuraFlow Dropshipper.lnk'
$shell = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $shortcutPath) {
    $existingShortcut = $shell.CreateShortcut($shortcutPath)
    if ($existingShortcut.TargetPath -ine $executable) { throw 'A different desktop shortcut already uses this name. It was not replaced.' }
}
$config = @{ WindowsRepository=$RepositoryPath; LinuxRepository=$linuxRepository; Distribution=$Distribution; ResearchArguments=$researchArguments }
$action.SelectSingleNode('t:Command', $namespace).InnerText = $executable
$action.SelectSingleNode('t:Arguments', $namespace).InnerText = '--worker'
$working = $action.SelectSingleNode('t:WorkingDirectory', $namespace)
if (-not $working) { $working = $document.CreateElement('WorkingDirectory', $namespace.LookupNamespace('t')); $action.AppendChild($working) | Out-Null }
$working.InnerText = $RepositoryPath
$definition = $scheduler.NewTask(0)
$definition.XmlText = $document.OuterXml
# Validate with Windows before touching the saved switch/task/shortcut.
$folder.RegisterTaskDefinition($taskName, $definition, 1, $owner, $null, 3, $null) | Out-Null
if (-not $Install) {
    [pscustomobject]@{Ready=$true; Executable=$executable; Shortcut=$shortcutPath; Task=$taskName; StartsResearch=$false; Config=$config} | ConvertTo-Json -Depth 4
    return
}
$operationDirectory = Join-Path $RepositoryPath 'data\operations\desktop'
New-Item -ItemType Directory -Force -Path $operationDirectory | Out-Null
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
[System.IO.File]::WriteAllText((Join-Path $operationDirectory "task-before-$stamp.xml"), $originalXml, [System.Text.Encoding]::UTF8)
[System.IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 4), [System.Text.UTF8Encoding]::new($false))
if ($folder.GetTask($taskName).Xml -cne $originalXml) { throw 'The task changed during installation. Its action was not updated.' }
$folder.RegisterTaskDefinition($taskName, $definition, 4, $owner, $null, 3, $null) | Out-Null
$readback = $folder.GetTask($taskName)
[xml] $checked = $readback.Xml
$checkedNamespace = New-Object System.Xml.XmlNamespaceManager($checked.NameTable)
$checkedNamespace.AddNamespace('t', $namespace.LookupNamespace('t'))
$readAction = $checked.SelectSingleNode('/t:Task/t:Actions/t:Exec', $checkedNamespace)
if ($readAction.Command -ine $executable -or $readAction.Arguments -ne '--worker' -or $readAction.WorkingDirectory -ine $RepositoryPath) { throw 'The task update needs review. No research was started.' }
# Full non-action XML must remain identical after Windows normalisation.
function Get-NonActionXml([xml] $xml) {
    $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable); $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $node = $xml.SelectSingleNode('/t:Task/t:Actions', $ns); $node.ParentNode.RemoveChild($node) | Out-Null
    return $xml.OuterXml
}
if ((Get-NonActionXml ([xml] $originalXml)) -cne (Get-NonActionXml ([xml] $readback.Xml))) { throw 'A non-action task setting changed unexpectedly. Review the saved original.' }
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $executable; $shortcut.WorkingDirectory = $RepositoryPath
$shortcut.Description = 'NeuraFlow product research — Start, Stop and tray controls'
$shortcut.IconLocation = "$executable,0"; $shortcut.WindowStyle = 1; $shortcut.Save()
$receipt = @{ InstalledAt=[DateTime]::UtcNow.ToString('o'); Task=$taskName; Executable=$executable; Shortcut=$shortcutPath;
    SHA256=(Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash; PreviousTask="task-before-$stamp.xml";
    PreservedNonActionSettings=$true; ResearchStarted=$false; Config=$config }
[System.IO.File]::WriteAllText((Join-Path $operationDirectory 'installation.json'), ($receipt | ConvertTo-Json -Depth 5), [System.Text.UTF8Encoding]::new($false))
$receipt | ConvertTo-Json -Depth 5
