param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "status",
    [switch]$KeepInfra
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SrcDir = Join-Path $ProjectRoot "src"
$RunDir = Join-Path $ProjectRoot ".run"
$LogDir = Join-Path $ProjectRoot "logs"
$ComposeFile = Join-Path $ProjectRoot "docker-compose.yml"

$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

$Services = @(
    @{ Name = "profile_service"; Module = "profile_service.main" },
    @{ Name = "bot_service"; Module = "bot_service.main" }
)

function Ensure-Dir([string]$Path) {
    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Get-PidFile([string]$ServiceName) {
    return Join-Path $RunDir "$ServiceName.pid"
}

function Get-LogOut([string]$ServiceName) {
    return Join-Path $LogDir "$ServiceName.out.log"
}

function Get-LogErr([string]$ServiceName) {
    return Join-Path $LogDir "$ServiceName.err.log"
}

function Get-RunningProcessByPidFile([string]$ServiceName) {
    $pidFile = Get-PidFile $ServiceName
    if (-not (Test-Path $pidFile)) {
        return $null
    }

    $rawPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if (-not $rawPid) {
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        return $null
    }

    $proc = Get-Process -Id $rawPid -ErrorAction SilentlyContinue
    if (-not $proc) {
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        return $null
    }

    return $proc
}

function Stop-ByModuleFallback([string]$ModuleName) {
    $escaped = [Regex]::Escape("-m $ModuleName")
    $escapedScript = [Regex]::Escape("\$ModuleName".Replace("\", "\\"))
    $candidates = Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -eq "python.exe" -and (
                $_.CommandLine -match $escaped -or
                $_.CommandLine -match $escapedScript
            )
        } |
        Select-Object -ExpandProperty ProcessId

    foreach ($pid in $candidates) {
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    }
}

function Get-RunningProcessByModule([string]$ModuleName) {
    $escaped = [Regex]::Escape("-m $ModuleName")
    $escapedScript = [Regex]::Escape("$ModuleName".Replace("\", "\\"))
    $candidate = Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -eq "python.exe" -and (
                $_.CommandLine -match $escaped -or
                $_.CommandLine -match $escapedScript
            )
        } |
        Select-Object -First 1

    if (-not $candidate) {
        return $null
    }

    return Get-Process -Id $candidate.ProcessId -ErrorAction SilentlyContinue
}

function Start-Service([string]$ServiceName, [string]$ModuleName) {
    $already = Get-RunningProcessByPidFile $ServiceName
    if ($already) {
        Write-Host "[$ServiceName] already running (PID $($already.Id))"
        return
    }

    $moduleProc = Get-RunningProcessByModule $ModuleName
    if ($moduleProc) {
        Ensure-Dir $RunDir
        $moduleProc.Id | Set-Content (Get-PidFile $ServiceName)
        Write-Host "[$ServiceName] already running (PID $($moduleProc.Id), detected by module)"
        return
    }

    Ensure-Dir $RunDir
    Ensure-Dir $LogDir

    $outLog = Get-LogOut $ServiceName
    $errLog = Get-LogErr $ServiceName

    $process = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList @("-m", $ModuleName) `
        -WorkingDirectory $SrcDir `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog `
        -PassThru

    $process.Id | Set-Content (Get-PidFile $ServiceName)
    Write-Host "[$ServiceName] started (PID $($process.Id))"
}

function Stop-Service([string]$ServiceName, [string]$ModuleName) {
    $proc = Get-RunningProcessByPidFile $ServiceName
    if ($proc) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Write-Host "[$ServiceName] stopped (PID $($proc.Id))"
    } else {
        Write-Host "[$ServiceName] not running by pid file"
    }

    Remove-Item (Get-PidFile $ServiceName) -ErrorAction SilentlyContinue
    Stop-ByModuleFallback $ModuleName
}

function Show-Status {
    foreach ($svc in $Services) {
        $proc = Get-RunningProcessByPidFile $svc.Name
        if (-not $proc) {
            $proc = Get-RunningProcessByModule $svc.Module
            if ($proc) {
                Ensure-Dir $RunDir
                $proc.Id | Set-Content (Get-PidFile $svc.Name)
            }
        }
        if ($proc) {
            Write-Host "[$($svc.Name)] running (PID $($proc.Id))"
        } else {
            Write-Host "[$($svc.Name)] stopped"
        }
    }
}

function Start-Infra {
    & docker compose -f $ComposeFile up -d | Out-Null
    Write-Host "[infra] docker compose up -d done"
}

function Stop-Infra {
    if ($KeepInfra) {
        Write-Host "[infra] keeping docker services up"
        return
    }
    & docker compose -f $ComposeFile down | Out-Null
    Write-Host "[infra] docker compose down done"
}

switch ($Action) {
    "start" {
        Start-Infra
        foreach ($svc in $Services) {
            Start-Service -ServiceName $svc.Name -ModuleName $svc.Module
        }
        Show-Status
    }
    "stop" {
        foreach ($svc in ($Services | Sort-Object -Property Name -Descending)) {
            Stop-Service -ServiceName $svc.Name -ModuleName $svc.Module
        }
        Stop-Infra
        Show-Status
    }
    "restart" {
        foreach ($svc in ($Services | Sort-Object -Property Name -Descending)) {
            Stop-Service -ServiceName $svc.Name -ModuleName $svc.Module
        }
        Start-Infra
        foreach ($svc in $Services) {
            Start-Service -ServiceName $svc.Name -ModuleName $svc.Module
        }
        Show-Status
    }
    "status" {
        Show-Status
    }
}
