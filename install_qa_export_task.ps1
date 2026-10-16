param(
    [string]$At = '16:00',
    [string]$OutputDir = (Join-Path $PSScriptRoot 'data\qa_exports'),
    [string]$DockerProject = 'dingtalk_gpt_bot',
    [string]$LogPath = ''
)
$ErrorActionPreference = 'Stop'
if ((Get-TimeZone).Id -ne 'China Standard Time') {
    throw '本脚本的每日时间使用北京时间，请先确认 Windows 时区为 China Standard Time。'
}
$exportTime = [datetime]::ParseExact($At, 'HH:mm', [cultureinfo]::InvariantCulture)
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '找不到项目 .venv Python，请先安装项目依赖。' }
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$sourceArgs = if ($LogPath) { '--log-path "' + [IO.Path]::GetFullPath($LogPath) + '"' } else { '--docker-project "' + $DockerProject + '"' }
if ($OutputDir.Contains('"') -or $sourceArgs.Replace('"', '').Contains("`n") -or $DockerProject.Contains('"')) {
    throw '路径和 Compose project 不能包含引号或换行。'
}
$arguments = '-X utf8 "' + (Join-Path $PSScriptRoot 'export_qa_logs.py') + '" ' + $sourceArgs + ' --output-dir "' + $OutputDir + '"'
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $exportTime
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$taskName = 'DingTalk-Knowledge-QA-Excel-' + (Split-Path $PSScriptRoot -Leaf)
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -ne "Daily Q&A Excel export: $PSScriptRoot") {
    throw '同名任务属于其他配置，请先检查，未覆盖。'
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Daily Q&A Excel export: $PSScriptRoot" -Force | Out-Null
Write-Output "已安装任务 $taskName；每天北京时间 $At；导出目录 $OutputDir；当前用户登录期间执行。"
