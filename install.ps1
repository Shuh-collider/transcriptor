# Transcriptor installer for Windows.
#
# Install or update with one command in PowerShell:
#   irm https://raw.githubusercontent.com/Shuh-collider/transcriptor/main/install.ps1 | iex
# or run install.bat inside a downloaded copy of the repository.
#
# What it does: downloads the code (git or ZIP), installs uv, detects your NVIDIA GPU, installs the
# matching PyTorch build (CUDA 12.8, CUDA 12.6 for older cards, or CPU), creates .env and a desktop shortcut.
#
# Optional environment variables:
#   TRANSCRIPTOR_DIR       install folder (default: %USERPROFILE%\transcriptor)
#   TRANSCRIPTOR_TORCH     force the PyTorch build: cu128 | cu126 | cpu
#   TRANSCRIPTOR_NO_SHORTCUT=1   do not create the desktop shortcut
#
# This file is ASCII on purpose: Windows PowerShell 5.1 misreads UTF-8 scripts without a BOM.

function Install-Transcriptor {
    $ErrorActionPreference = "Stop"
    $Repo = "Shuh-collider/transcriptor"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
    function Note($msg) { Write-Host "    $msg" }
    function Warn($msg) { Write-Host "    ! $msg" -ForegroundColor Yellow }

    # ---------- 1. Code ----------
    $local = $PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pyproject.toml"))
    if ($local) {
        $Dir = $PSScriptRoot
        Say "Installing in $Dir"
    } else {
        $Dir = if ($env:TRANSCRIPTOR_DIR) { $env:TRANSCRIPTOR_DIR } else { Join-Path $env:USERPROFILE "transcriptor" }
        $git = Get-Command git -ErrorAction SilentlyContinue
        if (Test-Path (Join-Path $Dir ".git")) {
            if ($git) {
                Say "Updating $Dir (git pull)"
                git -C $Dir pull --ff-only
                if ($LASTEXITCODE -ne 0) { throw "git pull failed. Commit or discard local changes in $Dir and run again." }
            } else {
                Warn "Git is not installed, skipping the code update."
            }
        } elseif ($git -and -not (Test-Path (Join-Path $Dir "pyproject.toml"))) {
            Say "Downloading Transcriptor to $Dir (git clone)"
            git clone --depth 1 "https://github.com/$Repo.git" $Dir
            if ($LASTEXITCODE -ne 0) { throw "git clone failed." }
        } else {
            Say "Downloading Transcriptor to $Dir (ZIP)"
            $zip = Join-Path $env:TEMP "transcriptor-main.zip"
            $unpacked = Join-Path $env:TEMP "transcriptor-main-unpacked"
            Invoke-WebRequest -UseBasicParsing "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip
            if (Test-Path $unpacked) { Remove-Item -Recurse -Force $unpacked }
            Expand-Archive -Path $zip -DestinationPath $unpacked -Force
            $src = Get-ChildItem $unpacked -Directory | Select-Object -First 1
            New-Item -ItemType Directory -Force $Dir | Out-Null
            # .env, data and .venv are not in the archive, so they survive an update
            Copy-Item -Path (Join-Path $src.FullName "*") -Destination $Dir -Recurse -Force
            Remove-Item -Recurse -Force $unpacked, $zip
        }
    }

    # ---------- 2. uv ----------
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        foreach ($candidate in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe", "$env:APPDATA\Python\Python311\Scripts\uv.exe")) {
            if (Test-Path $candidate) { $uv = $candidate; break }
        }
    }
    if (-not $uv) {
        Say "Installing uv (Python package manager from astral.sh)"
        powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        $uv = "$env:USERPROFILE\.local\bin\uv.exe"
        if (-not (Test-Path $uv)) { throw "uv was not installed. See https://docs.astral.sh/uv/getting-started/installation/" }
    }
    if ($uv -isnot [string]) { $uv = $uv.Source }

    # ---------- 3. GPU ----------
    Say "Checking the graphics card"
    $torch = "cpu"
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi -and (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe")) { $smi = "$env:SystemRoot\System32\nvidia-smi.exe" }
    if ($smi) {
        if ($smi -isnot [string]) { $smi = $smi.Source }
        $line = & $smi --query-gpu=name,compute_cap,driver_version,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1
        if (-not $line) {
            # very old drivers do not know compute_cap
            $line = (& $smi --query-gpu=name,driver_version,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
            if ($line) { $p = $line.Split(","); $line = "$($p[0]),,$($p[1]),$($p[2])" }
        }
        if ($line) {
            $f = $line.Split(",") | ForEach-Object { $_.Trim() }
            $name, $capText, $driver, $memMb = $f[0], $f[1], $f[2], $f[3]
            $cap = 0.0
            [void][double]::TryParse($capText, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$cap)
            if ($cap -eq 0) {
                # no compute capability: guess by the name
                $cap = if ($name -match "GTX (9|10)\d\d|Quadro (M|P)|Tesla (M|P)") { 6.1 } else { 7.5 }
            }
            Note "$name, compute capability $cap, driver $driver, $memMb MB"
            if ($cap -ge 7.0) { $torch = "cu128" }
            elseif ($cap -ge 5.0) { $torch = "cu126" }
            else { Warn "This GPU is too old for PyTorch; using the CPU." }
            $driverMajor = 0
            [void][int]::TryParse(($driver -split "\.")[0], [ref]$driverMajor)
            if ($torch -ne "cpu" -and $driverMajor -gt 0 -and $driverMajor -lt 560) {
                Warn "The NVIDIA driver $driver is old. If the GPU is not used after installation, update the driver: https://www.nvidia.com/drivers"
            }
            $mem = 0
            [void][int]::TryParse($memMb, [ref]$mem)
            if ($torch -ne "cpu" -and $mem -gt 0 -and $mem -lt 7500) {
                Warn "The GPU has $mem MB of memory; 8 GB or more is recommended. Long videos may run out of memory."
            }
        }
    } else {
        Note "No NVIDIA graphics card found."
    }
    if ($env:TRANSCRIPTOR_TORCH) { $torch = $env:TRANSCRIPTOR_TORCH; Note "TRANSCRIPTOR_TORCH=$torch" }
    switch ($torch) {
        "cu128" { Note "PyTorch build: CUDA 12.8" }
        "cu126" { Note "PyTorch build: CUDA 12.6 (for older cards)" }
        "cpu" { Warn "PyTorch build: CPU only. It works, but recognition is many times slower than on an NVIDIA GPU." }
        default { throw "TRANSCRIPTOR_TORCH must be cu128, cu126 or cpu." }
    }

    # ---------- 4. Python packages ----------
    Say "Installing Python 3.11 and packages (the first time downloads about 5 GB)"
    Push-Location $Dir
    try {
        if ($torch -eq "cu128") { & $uv sync } else { & $uv sync --no-default-groups --group $torch }
        if ($LASTEXITCODE -ne 0) { throw "uv sync failed (see the messages above)." }

        $check = & ".venv\Scripts\python.exe" -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
        Note "PyTorch: $check"
        if ($torch -ne "cpu" -and $check -notmatch "True") {
            Warn "PyTorch does not see the GPU. Update the NVIDIA driver and run the installer again."
        }

        if (-not (Test-Path ".env")) {
            Copy-Item ".env.example" ".env"
            Note "Created .env for your keys."
        }
    } finally {
        Pop-Location
    }

    # ---------- 5. Shortcut ----------
    if (-not $env:TRANSCRIPTOR_NO_SHORTCUT) {
        try {
            $desktop = [Environment]::GetFolderPath("Desktop")
            $shell = New-Object -ComObject WScript.Shell
            $link = $shell.CreateShortcut((Join-Path $desktop "Transcriptor.lnk"))
            $link.TargetPath = Join-Path $Dir "start.bat"
            $link.WorkingDirectory = $Dir
            $link.Description = "Transcriptor: transcribe YouTube videos"
            $link.Save()
            Note "Desktop shortcut: Transcriptor"
        } catch {
            Warn "Could not create the desktop shortcut: $($_.Exception.Message)"
        }
    }

    Say "Done"
    Note "Folder: $Dir"
    Note "Keys (optional, all free): open $Dir\.env and add"
    Note "  HF_TOKEN=...        who is speaking   https://huggingface.co/settings/tokens"
    Note "                      (accept the terms at https://huggingface.co/pyannote/speaker-diarization-community-1)"
    Note "  GEMINI_API_KEY=...  summaries         https://aistudio.google.com/apikey"
    Note "Start: double-click the Transcriptor shortcut or $Dir\start.bat"
    Note "Update: run the same install command again."
}

try {
    Install-Transcriptor
} catch {
    Write-Host ""
    Write-Host "Installation failed: $($_.Exception.Message)" -ForegroundColor Red
}
