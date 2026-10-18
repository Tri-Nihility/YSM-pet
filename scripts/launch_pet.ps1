# run_pet.bat 的实现：自动找一个能跑桌宠的 Python，再启动 scripts\run_pet.py
#
# 参数（由 run_pet.bat 原样转发）：
#   --console        用 python.exe 启动（保留控制台，能看到报错；默认用 pythonw.exe 无窗口）
#   --list           只列出探测到的解释器和各自缺什么，不启动
#   --redetect       忽略缓存，重新扫描
#   --python <路径>  手动指定解释器（会跳过依赖检查）
#   其余参数原样传给 scripts\run_pet.py（--source / --smoke / --scale / --gaze ...）

$ErrorActionPreference = 'Stop'

$Root  = Split-Path -Parent $PSScriptRoot              # 仓库根（scripts 的上一级）
$Entry = Join-Path $Root 'scripts\run_pet.py'
$Cache = Join-Path $Root '.run_pet_python'             # 记住上次选中的解释器

# 桌宠真正需要的（缺一个就跑不起来）
# ⚠️ 这里**没有** pybullet / torch：
#    pybullet 只为两个相机矩阵函数服务，已换成 src/pet/camera.py（纯 numpy），
#           而且它在 PyPI 上没有 Windows 轮子，pip 装不上；
#    torch   是训练线遗产，桌宠一行都不用。
$Core = @('numpy', 'scipy', 'PySide6', 'PIL', 'yaml')
# 缺了只影响部分功能（py7zr：解 .7z 格式的模型包）
$Opt  = @('py7zr')

# 只做 find_spec 不 import，所以很快；没有引号，避免被 PowerShell 改写
$Probe = 'import importlib.util as u,sys;m=[x for x in sys.argv[1:] if u.find_spec(x) is None];print(sys.version.split()[0],len(m),*m)'

# ---------------------------------------------------------------- 参数
$useConsole = $false
$listOnly   = $false
$redetect   = $false
$forced     = $null
$pass       = @()
for ($i = 0; $i -lt $args.Count; $i++) {
    $a = [string]$args[$i]
    if ($a -match '^--?console$')  { $useConsole = $true; continue }
    if ($a -match '^--?list$')     { $listOnly   = $true; continue }
    if ($a -match '^--?redetect$') { $redetect   = $true; continue }
    if ($a -match '^--?python=')   { $forced = $a.Substring(9); continue }
    if ($a -match '^--?python$')   {
        if ($i + 1 -lt $args.Count) { $forced = [string]$args[$i + 1]; $i++ }
        continue
    }
    $pass += $a
}

# ---------------------------------------------------------------- 候选
$cands = New-Object System.Collections.Generic.List[string]

function Add-Cand($p) {
    if (-not $p) { return }
    $p = ([string]$p).Trim()
    if (-not $p) { return }
    if ($p -match '\\WindowsApps\\') { return }        # 微软商店的假 python，调了只会弹提示
    if (Test-Path -LiteralPath $p -PathType Leaf) {
        if (-not $cands.Contains($p)) { [void]$cands.Add($p) }
    }
}

# 1) 手动指定 / 环境变量 / 上次缓存
Add-Cand $forced
Add-Cand $env:YSM_PYTHON
if (-not $redetect -and (Test-Path -LiteralPath $Cache)) {
    Add-Cand ((Get-Content -LiteralPath $Cache -TotalCount 1))
}

# 2) 仓库自带的虚拟环境
Add-Cand (Join-Path $Root 'venv\Scripts\python.exe')
Add-Cand (Join-Path $Root '.venv\Scripts\python.exe')

# 3) conda（各环境优先于 base）
foreach ($r in @("$env:USERPROFILE\miniconda3", "$env:USERPROFILE\anaconda3",
                 'C:\ProgramData\anaconda3', 'C:\private\yanjiusheng\Anaconda')) {
    if (-not (Test-Path -LiteralPath $r)) { continue }
    $envsDir = Join-Path $r 'envs'
    if (Test-Path -LiteralPath $envsDir) {
        Get-ChildItem -LiteralPath $envsDir -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name | ForEach-Object { Add-Cand (Join-Path $_.FullName 'python.exe') }
    }
    Add-Cand (Join-Path $r 'python.exe')
}

# 4) python.org 官方安装版
$pyDir = Join-Path $env:LOCALAPPDATA 'Programs\Python'
if (Test-Path -LiteralPath $pyDir) {
    Get-ChildItem -LiteralPath $pyDir -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | ForEach-Object { Add-Cand (Join-Path $_.FullName 'python.exe') }
}

# 5) PATH 上的 python
Get-Command python.exe -All -ErrorAction SilentlyContinue |
    ForEach-Object { Add-Cand $_.Source }

# 6) DSH 自带运行时（最后兜底）
$dshRt = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes'
if (Test-Path -LiteralPath $dshRt) {
    Get-ChildItem $dshRt -Recurse -Filter 'python.exe' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\venv\\' } |
        ForEach-Object { Add-Cand $_.FullName }
}

# ---------------------------------------------------------------- 逐个探测
$rows = @()
foreach ($p in $cands) {
    $ver = ''
    $missCore = @()
    $missOpt = @()
    try {
        $out = & $p -c $Probe ($Core + $Opt) 2>$null
        $line = [string]($out | Select-Object -Last 1)
        if ($line -match '^(\S+)\s+(\d+)(?:\s+(.*))?$') {
            $ver = $matches[1]
            $miss = @()
            if ($matches[3]) { $miss = @($matches[3] -split '\s+') }
            $missCore = @($miss | Where-Object { $Core -contains $_ })
            $missOpt  = @($miss | Where-Object { $Opt  -contains $_ })
        }
    } catch { }
    $rows += [pscustomobject]@{
        Path     = $p
        Ver      = $ver
        MissCore = $missCore
        MissOpt  = $missOpt
        Ok       = ($ver -ne '' -and $missCore.Count -eq 0)
    }
}

function Show-Table {
    '{0,-62} {1,-9} {2,-24} {3}' -f '解释器', '版本', '缺（跑不了）', '缺（可选）'
    foreach ($r in $rows) {
        $c = if ($r.MissCore.Count) { $r.MissCore -join ',' } else { '-' }
        $o = if ($r.MissOpt.Count)  { $r.MissOpt  -join ',' } else { '-' }
        $v = if ($r.Ver) { $r.Ver } else { '不可用' }
        $mark = if ($r.Ok) { '*' } else { ' ' }
        '{0}{1,-61} {2,-9} {3,-24} {4}' -f $mark, $r.Path, $v, $c, $o
    }
}

# ---------------------------------------------------------------- 选一个
$chosen = $null
$forcedUsed = $false
if ($forced) {
    $f = $rows | Where-Object { $_.Path -eq $forced.Trim() } | Select-Object -First 1
    if ($f) { $chosen = $f; $forcedUsed = $true }
    else {
        "指定的解释器不存在或没法执行：$forced"
        exit 1
    }
}
if (-not $chosen) {
    $chosen = $rows | Where-Object { $_.Ok } | Select-Object -First 1
}

if ($listOnly) {
    Show-Table
    if ($chosen) { "`n会选中：$($chosen.Path)" } else { "`n（没有一个能跑，见下面建议）" }
    exit 0
}

if (-not $chosen) {
    Show-Table
    $close = $rows | Where-Object { $_.Ver -and $_.MissCore.Count -gt 0 } |
             Sort-Object { $_.MissCore.Count } | Select-Object -First 1
    "`n没找到能跑桌宠的解释器。核心依赖（缺一不可）：$($Core -join ' ')"
    if ($close) {
        "最接近的是 $($close.Path) ($($close.Ver))：只缺 $($close.MissCore -join ',')"
    }
    @"

三条出路：
  A) 给 Anaconda 自带的 3.12 环境装依赖（推荐）：
       conda install -n py312 -c conda-forge scipy
       C:\private\yanjiusheng\Anaconda\envs\py312\python.exe -m pip install PySide6 numpy scipy pillow pyyaml tqdm
     装完直接再跑一次 run_pet.bat（会自动认出来，不用改任何路径）。
  B) 已经把依赖装在别的解释器里：设环境变量 YSM_PYTHON，或 run_pet.bat --python <那个 python.exe>
  C) 先看看探测结果：run_pet.bat --list
"@
    exit 1
}

if (-not $forcedUsed) {
    Set-Content -LiteralPath $Cache -Value $chosen.Path -Encoding ASCII
}

$tag = if ($forcedUsed) { '（手动指定，未检查依赖）' } else { '' }
"用 $($chosen.Path)  [$($chosen.Ver)] 启动桌宠$tag"
if ($chosen.MissOpt.Count) { "  可选依赖没装：$($chosen.MissOpt -join ',')" }
if ($pass.Count) { "  传给 run_pet.py：$($pass -join ' ')" }

# ---------------------------------------------------------------- 启动
$env:PYTHONPATH = Join-Path $Root 'src'
Set-Location -LiteralPath $Root

$pyw = Join-Path (Split-Path -Parent $chosen.Path) 'pythonw.exe'
if ($useConsole -or -not (Test-Path -LiteralPath $pyw)) {
    & $chosen.Path $Entry @pass
    exit $LASTEXITCODE
}

$logDir = Join-Path $Root 'outputs\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$outLog = Join-Path $logDir 'run_pet.out.log'
$errLog = Join-Path $logDir 'run_pet.err.log'

# 自己拼命令行：Start-Process 不保证给带空格的参数加引号
$all = @($Entry) + $pass
$cmd = ($all | ForEach-Object {
    $s = [string]$_
    if ($s -match '[\s"]') { '"' + ($s -replace '"', '\"') + '"' } else { $s }
}) -join ' '

Start-Process -FilePath $pyw -ArgumentList $cmd -WorkingDirectory $Root `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog | Out-Null
"桌宠已启动（无控制台）。日志：outputs\logs\run_pet.out.log / run_pet.err.log"
exit 0
