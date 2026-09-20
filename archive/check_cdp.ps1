<#
  验证 YonWork (Electron) 的 CDP 远程调试端口。
  用法:
    powershell -ExecutionPolicy Bypass -File .\check_cdp.ps1 -ProbeOnly   # 只探测，不动现有进程
    powershell -ExecutionPolicy Bypass -File .\check_cdp.ps1              # 杀干净后带参数重启验证
#>
param(
    [string]$AppPath = "D:\yonwork\YonWork.exe",
    [int]$Port = 9222,
    [int]$TimeoutSeconds = 30,
    [switch]$ProbeOnly,     # 只跑第 1-2 步，不终止任何进程
    [switch]$AllowOrigins   # WebSocket 握手报 403 时加这个开关
)

$ErrorActionPreference = "Stop"
function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }

function Test-CdpPort([int]$p) {
    try { return Invoke-RestMethod "http://127.0.0.1:$p/json/version" -TimeoutSec 3 }
    catch { return $null }
}

$name = [IO.Path]::GetFileNameWithoutExtension($AppPath)

# ---------- 1. Electron 特征 ----------
Step 1 "检查 Electron 特征文件"
if (-not (Test-Path $AppPath)) { Say "找不到 $AppPath" Red; exit 2 }
$appDir = Split-Path $AppPath -Parent
$markers = @(
    "resources\app.asar", "icudtl.dat", "v8_context_snapshot.bin",
    "resources.pak", "chrome_100_percent.pak", "ffmpeg.dll", "libEGL.dll"
)
$hitCount = 0
foreach ($m in $markers) {
    $ok = Test-Path (Join-Path $appDir $m)
    if ($ok) { $hitCount++ }
    Say ("  {0} {1}" -f $(if ($ok) { "[+]" } else { "[ ]" }), $m) $(if ($ok) { "Green" } else { "DarkGray" })
}
Say $(if ($hitCount -ge 2) { "  => Electron/Chromium 应用" } else { "  => 特征不足" }) $(if ($hitCount -ge 2) { "Green" } else { "Yellow" })

# ---------- 2. 探测现有实例是否已经开着 CDP ----------
# 日志里 "[external-cdp] ... skipping appendSwitch" 说明不带 argv 时它会自己加端口，
# 所以先探测，别急着杀进程。
Step 2 "探测当前 $name 实例的 LISTEN 端口"
$running = Get-Process -Name $name -ErrorAction SilentlyContinue
if (-not $running) {
    Say "  当前没有运行中的实例，跳过探测" DarkGray
} else {
    Say ("  发现 {0} 个进程: {1}" -f $running.Count, (($running.Id) -join ", ")) DarkGray
    $ports = $running | ForEach-Object {
        Get-NetTCPConnection -OwningProcess $_.Id -State Listen -ErrorAction SilentlyContinue
    } | Select-Object -ExpandProperty LocalPort -Unique | Sort-Object
    if (-not $ports) { Say "  没有监听端口" Yellow }
    $found = @()
    foreach ($p in $ports) {
        $v = Test-CdpPort $p
        if ($v) {
            Say ("  [CDP] {0,-6} -> {1}" -f $p, $v.Browser) Green
            $found += $p
        } else {
            Say ("  [   ] {0}" -f $p) DarkGray
        }
    }
    if ($found.Count -gt 0) {
        Say ("`n  ==== 现有实例已开 CDP，端口: {0} ====" -f ($found -join ", ")) Green
        foreach ($p in $found) {
            $targets = @(Invoke-RestMethod "http://127.0.0.1:$p/json/list" -TimeoutSec 5)
            $pages = @($targets | Where-Object { $_.type -eq "page" })
            Say ("  端口 {0}: target {1} 个，page {2} 个" -f $p, $targets.Count, $pages.Count)
            foreach ($t in $targets) {
                Say ("    [{0}] {1}" -f $t.type, $t.title) White
                Say ("          {0}" -f $t.url) DarkGray
            }
        }
        Say "`n  不用重启，直接 connect_over_cdp 就行" Green
        if ($ProbeOnly) { exit 0 }
    }
}

# DevToolsActivePort：Chromium 自己写下的端口记录
Step "2b" "查找 DevToolsActivePort 文件"
$dtap = Get-ChildItem "$env:APPDATA", "$env:LOCALAPPDATA" -Depth 2 -Filter DevToolsActivePort -ErrorAction SilentlyContinue
if ($dtap) {
    foreach ($f in $dtap) {
        Say ("  {0}" -f $f.FullName) White
        Say ("    {0}" -f ((Get-Content $f.FullName -Raw) -replace "`r?`n", " | ")) Green
    }
} else { Say "  未找到" DarkGray }

if ($ProbeOnly) { Say "`n-ProbeOnly 模式结束，未改动任何进程" Cyan; exit 0 }

# ---------- 3. 杀干净残留进程 ----------
Step 3 "终止所有残留进程（单实例锁会让 argv 被直接丢弃）"
if ($running) {
    Say ("  终止 {0} 个进程" -f $running.Count) Yellow
    $running | Stop-Process -Force
    Start-Sleep -Seconds 3
} else { Say "  无残留进程" Green }
if (Get-Process -Name $name -ErrorAction SilentlyContinue) {
    Say "  仍有进程存活 —— 请手动退出托盘图标后重跑" Red; exit 3
}

$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) { Say ("  端口 $Port 被 PID {0} 占用，换端口: -Port 9333" -f $busy[0].OwningProcess) Red; exit 4 }

# ---------- 4. 带调试参数启动 ----------
Step 4 "带 --remote-debugging-port=$Port 启动"
$launchArgs = @("--remote-debugging-port=$Port")
if ($AllowOrigins) { $launchArgs += "--remote-allow-origins=*" }
Say ("  {0} {1}" -f $AppPath, ($launchArgs -join " ")) DarkGray
Start-Process -FilePath $AppPath -ArgumentList $launchArgs | Out-Null

# ---------- 5. 轮询端口 ----------
Step 5 "等待端口监听（最多 ${TimeoutSeconds}s）"
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$listening = $false
while ((Get-Date) -lt $deadline) {
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { $listening = $true; break }
    Start-Sleep -Milliseconds 800
}
if (-not $listening) {
    Say "  端口始终未监听" Red
    Say "  下一步: python extract_asar.py grep <app.asar> -p external-cdp" Yellow
    exit 5
}
Say "  端口已监听" Green

# ---------- 6. 查询 CDP ----------
Step 6 "查询 CDP 端点（用 127.0.0.1，localhost 可能解析到 ::1）"
$ver = Test-CdpPort $Port
if (-not $ver) { Say "  /json/version 失败，端口可能被别的程序占用，换 -Port 9333" Red; exit 6 }
Say "  Browser  : $($ver.Browser)" Green
Say "  Protocol : $($ver.'Protocol-Version')" Green

$targets = @(Invoke-RestMethod "http://127.0.0.1:$Port/json/list" -TimeoutSec 10)
$pages = @($targets | Where-Object { $_.type -eq "page" })
Say ("`n  target {0} 个，page {1} 个" -f $targets.Count, $pages.Count)
foreach ($t in $targets) {
    Say ("    [{0}] {1}" -f $t.type, $t.title) White
    Say ("          {0}" -f $t.url) DarkGray
}

Write-Host ""
if ($pages.Count -gt 0) {
    Say "==== CDP 可用，能拿到渲染进程 ====" Green
    Say "下一步: playwright connect_over_cdp('http://127.0.0.1:$Port')" Green
    exit 0
} else {
    Say "==== 端口通但无 page target，回退 pywinauto ====" Yellow
    exit 7
}
