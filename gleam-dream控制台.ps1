# ============================================================================
# 文件级注释：浮光造梦 · GleamDream Studio 一键启停控制台脚本
#
# 功能：
#   start   - 按序启动全部服务：环境检查 -> 依赖安装(幂等) -> 数据库初始化(幂等)
#             -> 后端 FastAPI(:8100) -> 前端 Vite(:5175) -> 打开浏览器
#   stop    - 读取 PID 文件安全终止前后端进程树，并确认端口释放（无残留进程）
#   status  - 表格化展示后端/前端进程、端口监听与健康检查结果
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\gleam-dream控制台.ps1 start|stop|status
#
# 设计约束：
#   - 幂等可重复执行：start 前检测端口占用，避免重复启动；
#   - 全部进程信息落 PID 文件（backend\.run\backend.pid / frontend.pid），
#     stop 按 PID 精确终止进程树（taskkill /T），不误伤其他应用；
#   - 不依赖 Redis/Celery/Docker（本项目的本地开发形态）。
# ============================================================================

[CmdletBinding()]
param(
    # 动作：start / stop / status
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "status"
)

# ---- 全局常量 ----
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path   # 脚本所在目录即项目根
$BackendDir = Join-Path $ProjectRoot "backend"
$FrontendDir = Join-Path $ProjectRoot "frontend"
$VenvDir = Join-Path $BackendDir ".venv"
$RunDir = Join-Path $BackendDir ".run"                          # PID 文件目录
$BackendPort = 8100
$FrontendPort = 5175
$BackendUrl = "http://127.0.0.1:$BackendPort"
$FrontendUrl = "http://127.0.0.1:$FrontendPort"

# ---- 工具函数 ----

<#
.SYNOPSIS
    写带颜色与时间戳的控制台日志。
#>
function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $color = switch ($Level) {
        "INFO" { "Cyan" } "OK" { "Green" } "WARN" { "Yellow" } "ERROR" { "Red" } default { "White" }
    }
    Write-Host ("[{0}] [{1}] {2}" -f (Get-Date -Format "HH:mm:ss"), $Level, $Message) -ForegroundColor $color
}

<#
.SYNOPSIS
    检测指定端口是否被监听。
#>
function Test-Port {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        return ($null -ne $conn)
    } catch { return $false }
}

<#
.SYNOPSIS
    判定后端服务是否"真正就绪"（端口监听 + 健康检查 code=0）。

.DESCRIPTION
    不能用"端口被占用"等同于"后端已就绪"：8100 可能被本机其他程序占用，
    此时浏览器/接口实际不可用。故必须同时满足端口监听且 /api/v1/health
    返回 code=0，才认定就绪。

.RETURN
    [bool] $true 表示后端健康可用；$false 表示未就绪（含端口未监听、被占用或健康检查失败）。
#>
function Test-BackendReady {
    if (-not (Test-Port $BackendPort)) { return $false }
    try {
        $resp = Invoke-RestMethod -Uri "$BackendUrl/api/v1/health" -TimeoutSec 3 -ErrorAction Stop
        return ($resp.code -eq 0)
    } catch {
        # 服务未响应/超时/返回非 JSON：一律视为未就绪
        return $false
    }
}

<#
.SYNOPSIS
    判定前端服务是否"真正就绪"（HTTP 探测首页返回 200）。

.DESCRIPTION
    不能用"端口被占用"等同于"前端已就绪"：5175 可能被本机其他项目的
    Vite 实例等其他程序占用，此时浏览器打开只会得到错误页面。
    故需真实发起 HTTP 请求，确认首页返回 200 才认定就绪。
    使用 -UseBasicParsing 规避 PowerShell 5.1 下 IE 引擎未初始化的问题。

.RETURN
    [bool] $true 表示前端页面可访问；$false 表示未就绪（含端口未监听、被占用或页面无响应）。
#>
function Test-FrontendReady {
    if (-not (Test-Port $FrontendPort)) { return $false }
    try {
        $resp = Invoke-WebRequest -Uri $FrontendUrl -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        return ($resp.StatusCode -eq 200)
    } catch {
        # 连接被拒/超时/返回非 200：一律视为未就绪
        return $false
    }
}

<#
.SYNOPSIS
    读取 PID 文件；不存在返回 $null。
#>
function Read-PidFile {
    param([string]$Name)
    $path = Join-Path $RunDir "$Name.pid"
    if (Test-Path $path) {
        try { return [int](Get-Content $path -Raw).Trim() } catch { return $null }
    }
    return $null
}

<#
.SYNOPSIS
    写 PID 文件（自动创建 .run 目录）。
#>
function Write-PidFile {
    param([string]$Name, [int]$ProcId)
    if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir -Force | Out-Null }
    Set-Content -Path (Join-Path $RunDir "$Name.pid") -Value $ProcId -Encoding ASCII
}

<#
.SYNOPSIS
    检测 PID 对应进程是否存活。
#>
function Test-ProcessAlive {
    param([int]$ProcId)
    return ($null -ne (Get-Process -Id $ProcId -ErrorAction SilentlyContinue))
}

<#
.SYNOPSIS
    选择可用的 Python 启动器命令（返回命令数组或 $null）。
#>
function Get-PythonLauncher {
    # 1) 项目 venv 已存在则直接用（最稳）
    if (Test-Path (Join-Path $VenvDir "Scripts\python.exe")) {
        return @((Join-Path $VenvDir "Scripts\python.exe"))
    }
    # 2) py launcher 依次尝试 3.12 / 3.11 / 默认
    $candidates = @("-V:Astral/CPython3.12.13", "-3.12", "-3.11", "")
    foreach ($c in $candidates) {
        try {
            $null = (& py $c --version) 2>$null
            if ($LASTEXITCODE -eq 0) {
                if ($c -eq "") { return @("py") } else { return @("py", $c) }
            }
        } catch { }
    }
    return $null
}

<#
.SYNOPSIS
    确保 Python 虚拟环境与依赖就绪（幂等）。
#>
function Ensure-BackendEnv {
    if (Test-Path (Join-Path $VenvDir "Scripts\python.exe")) {
        Write-Log "后端虚拟环境已存在（backend/.venv）" "OK"
        return $true
    }
    $launcher = Get-PythonLauncher
    if ($null -eq $launcher) {
        Write-Log "未找到 Python 3.11+ 运行时，请先安装 Python（3.12 推荐）" "ERROR"
        return $false
    }
    Write-Log "创建后端虚拟环境（backend/.venv）..."
    & $launcher[0] $launcher[1..($launcher.Length - 1)] -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Log "venv 创建失败" "ERROR"; return $false }
    Write-Log "安装后端依赖（可能需要数分钟）..."
    & (Join-Path $VenvDir "Scripts\python.exe") -m pip install -r (Join-Path $BackendDir "requirements.txt") -i https://pypi.tuna.tsinghua.edu.cn/simple --quiet
    if ($LASTEXITCODE -ne 0) { Write-Log "依赖安装失败" "ERROR"; return $false }
    Write-Log "后端依赖安装完成" "OK"
    return $true
}

<#
.SYNOPSIS
    确保前端 node_modules 就绪（幂等）。
#>
function Ensure-FrontendEnv {
    if (Test-Path (Join-Path $FrontendDir "node_modules")) {
        Write-Log "前端依赖已存在（frontend/node_modules）" "OK"
        return $true
    }
    Write-Log "安装前端依赖（可能需要数分钟）..."
    Push-Location $FrontendDir
    # npm 进度信息走 stderr；临时切回 Continue 避免被 ErrorActionPreference=Stop 误判为异常
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        npm install --registry=https://registry.npmmirror.com 2>&1 | ForEach-Object { Write-Host "  $_" }
        if ($LASTEXITCODE -ne 0) { Write-Log "npm install 失败" "ERROR"; return $false }
    } finally {
        $ErrorActionPreference = $prevEap
        Pop-Location
    }
    Write-Log "前端依赖安装完成" "OK"
    return $true
}

<#
.SYNOPSIS
    初始化数据库（幂等）：建库 -> Alembic 迁移 -> 种子数据。
#>
function Invoke-DatabaseInit {
    Write-Log "初始化数据库（幂等：建库/迁移/种子）..."
    $initScript = Join-Path $BackendDir "scripts\init_db.py"
    if (-not (Test-Path $initScript)) { Write-Log "缺少 scripts/init_db.py" "ERROR"; return $false }
    Push-Location $BackendDir
    # alembic 的 INFO 日志走 stderr；临时切回 Continue 避免被 ErrorActionPreference=Stop 误判为异常
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & (Join-Path $VenvDir "Scripts\python.exe") scripts\init_db.py 2>&1 | Select-Object -Last 5 | ForEach-Object { Write-Host "  $_" }
        if ($LASTEXITCODE -ne 0) { Write-Log "数据库初始化失败" "ERROR"; return $false }
    } finally {
        $ErrorActionPreference = $prevEap
        Pop-Location
    }
    Write-Log "数据库就绪" "OK"
    return $true
}

<#
.SYNOPSIS
    启动后端服务（uvicorn :8100，隐藏窗口，PID 落盘）。
#>
function Start-Backend {
    # 已就绪（端口监听且健康检查 code=0）才跳过，避免"端口被别的程序占用"时误判为已运行
    if (Test-BackendReady) {
        Write-Log "后端已在运行且健康（端口 $BackendPort），跳过启动" "OK"
        return $true
    }
    if (Test-Port $BackendPort) {
        Write-Log "端口 $BackendPort 已被其他程序占用且健康检查未通过，请先释放该端口（可执行 stop）" "ERROR"
        return $false
    }
    Write-Log "启动后端服务（FastAPI :$BackendPort）..."
    $py = Join-Path $VenvDir "Scripts\python.exe"
    # --loop 指定自定义事件循环工厂：uvicorn 0.36+ 在 Windows 上硬编码
    # ProactorEventLoop（无视 policy 设置），会导致 psycopg 异步池
    # （LangGraph checkpointer）无法建连，必须使用 SelectorEventLoop。
    $proc = Start-Process -FilePath $py `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$BackendPort", "--loop", "app.loop_factory:selector_loop_factory" `
        -WorkingDirectory $BackendDir -WindowStyle Hidden -PassThru
    Write-PidFile -Name "backend" -ProcId $proc.Id
    # 等待健康检查通过（最多 30 秒）
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 1000
        try {
            $resp = Invoke-RestMethod -Uri "$BackendUrl/api/v1/health" -TimeoutSec 2 -ErrorAction Stop
            if ($resp.code -eq 0) { $ok = $true; break }
        } catch { }
        if (-not (Test-ProcessAlive $proc.Id)) { Write-Log "后端进程意外退出" "ERROR"; return $false }
    }
    if ($ok) { Write-Log "后端已就绪：$BackendUrl（PID $($proc.Id)，文档 $BackendUrl/docs）" "OK"; return $true }
    Write-Log "后端健康检查超时" "ERROR"
    return $false
}

<#
.SYNOPSIS
    启动前端服务（Vite :5175，隐藏窗口，PID 落盘）。
#>
function Start-Frontend {
    # 已就绪（首页 HTTP 200）才跳过，避免"端口被别的程序占用"时误判为已运行
    if (Test-FrontendReady) {
        Write-Log "前端已在运行且页面可访问（端口 $FrontendPort），跳过启动" "OK"
        return $true
    }
    if (Test-Port $FrontendPort) {
        Write-Log "端口 $FrontendPort 已被其他程序占用且页面无响应，请先释放该端口（可执行 stop）" "ERROR"
        return $false
    }
    Write-Log "启动前端服务（Vite :$FrontendPort）..."
    $npmCmd = (Get-Command npm.cmd -ErrorAction SilentlyContinue)
    if ($null -eq $npmCmd) { $npmCmd = (Get-Command npm -ErrorAction SilentlyContinue) }
    if ($null -eq $npmCmd) { Write-Log "未找到 npm，请先安装 Node.js" "ERROR"; return $false }
    $proc = Start-Process -FilePath $npmCmd.Source `
        -ArgumentList "run", "dev" `
        -WorkingDirectory $FrontendDir -WindowStyle Hidden -PassThru
    Write-PidFile -Name "frontend" -ProcId $proc.Id
    # 等待前端真正可访问（首页 HTTP 200，最多 45 轮；含进程意外退出检测）
    $ok = $false
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Milliseconds 1000
        if (Test-FrontendReady) { $ok = $true; break }
        if (-not (Test-Port $FrontendPort) -and -not (Test-ProcessAlive $proc.Id)) {
            Write-Log "前端进程意外退出" "ERROR"
            return $false
        }
    }
    if ($ok) { Write-Log "前端已就绪：$FrontendUrl（npm PID $($proc.Id)）" "OK"; return $true }
    Write-Log "前端启动超时" "ERROR"
    return $false
}

<#
.SYNOPSIS
    终止指定服务的进程树并清理 PID 文件。
#>
function Stop-Service {
    param([string]$Name, [int]$Port)
    $pid_ = Read-PidFile -Name $Name
    $killed = $false
    if ($null -ne $pid_ -and (Test-ProcessAlive $pid_)) {
        Write-Log "停止 $Name（PID $pid_，含子进程树）..."
        try {
            & taskkill /PID $pid_ /T /F 2>$null | Out-Null
            $killed = $true
        } catch { }
    }
    # 兜底：PID 文件失效但端口仍被本项目占用时，按端口定位监听进程
    if (-not $killed -and (Test-Port $Port)) {
        $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            $owner = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
            if ($null -ne $owner -and $owner.ProcessName -match "python|node|npm") {
                Write-Log "按端口 $Port 兜底停止 $($owner.ProcessName)（PID $($owner.Id)）..." "WARN"
                & taskkill /PID $owner.Id /T /F 2>$null | Out-Null
                $killed = $true
            }
        }
    }
    Remove-Item (Join-Path $RunDir "$Name.pid") -ErrorAction SilentlyContinue
    if ($killed) {
        Start-Sleep -Milliseconds 800
        if (Test-Port $Port) { Write-Log "$Name 端口 $Port 仍被占用" "WARN" }
        else { Write-Log "$Name 已停止，端口 $Port 已释放" "OK" }
    } else {
        Write-Log "$Name 未在运行" "INFO"
    }
}

<#
.SYNOPSIS
    展示各服务运行状态表格。
#>
function Show-Status {
    Write-Host ""
    Write-Host "==== GleamDream Studio 服务状态 ====" -ForegroundColor White
    $rows = @()
    foreach ($svc in @(@("后端 FastAPI", "backend", $BackendPort), @("前端 Vite", "frontend", $FrontendPort))) {
        $label = $svc[0]; $name = $svc[1]; $port = $svc[2]
        $pid_ = Read-PidFile -Name $name
        $alive = ($null -ne $pid_ -and (Test-ProcessAlive $pid_))
        $listening = Test-Port $Port
        # 状态不再只看"端口监听"，而是以真实可用性（后端健康检查 / 前端 HTTP 200）为准，
        # 避免端口被其他程序占用时显示为"运行中"的误判
        $health = "-"
        $ready = $false
        if ($name -eq "backend") {
            if ($listening) {
                try {
                    $r = Invoke-RestMethod -Uri "$BackendUrl/api/v1/health" -TimeoutSec 2 -ErrorAction Stop
                    $health = "code=$($r.code) $($r.data.status)"
                    $ready = ($r.code -eq 0)
                } catch { $health = "无响应" }
            }
        } elseif ($listening) {
            if (Test-FrontendReady) { $health = "HTTP 200"; $ready = $true } else { $health = "页面无响应" }
        }
        $state = if ($ready) { "运行中" } elseif ($listening) { "端口被占用/异常" } elseif ($alive) { "启动中/异常" } else { "已停止" }
        $rows += [PSCustomObject]@{
            服务 = $label; 状态 = $state; PID = $(if ($alive) { $pid_ } else { "-" })
            端口 = $port; 健康检查 = $health
        }
    }
    $rows | Format-Table -AutoSize | Out-Host
    Write-Host ("访问入口：前端 {0}  |  API 文档 {1}/docs" -f $FrontendUrl, $BackendUrl) -ForegroundColor DarkGray
    Write-Host ""
}

# ---- 主流程 ----
switch ($Action) {
    "start" {
        Write-Log "==== 浮光造梦 · GleamDream Studio 启动 ===="
        # 就绪判定不只看端口监听：端口可能被本机其他程序占用而实际不可用。
        # 分别做后端健康检查与前端 HTTP 探测，避免"误判已运行"而跳过启动。
        $backendReady = Test-BackendReady
        $frontendReady = Test-FrontendReady
        if ($backendReady -and $frontendReady) {
            Write-Log "前后端均已就绪，无需重复启动（如需重启请先 stop）" "WARN"
            Write-Log "正在打开浏览器：$FrontendUrl"
            Start-Process $FrontendUrl
            Show-Status
            break
        }
        if (-not (Ensure-BackendEnv)) { break }
        if (-not (Ensure-FrontendEnv)) { break }
        if (-not (Invoke-DatabaseInit)) { break }
        $backendOk = Start-Backend
        $frontendOk = Start-Frontend
        if ($backendOk -and $frontendOk) {
            Write-Log "全部服务启动完成，正在打开浏览器..." "OK"
            Start-Process $FrontendUrl
        } else {
            Write-Log "部分服务启动失败，请查看上方日志（可执行 stop 后重试）" "ERROR"
        }
        Show-Status
    }
    "stop" {
        Write-Log "==== 浮光造梦 · GleamDream Studio 停止 ===="
        Stop-Service -Name "frontend" -Port $FrontendPort
        Stop-Service -Name "backend" -Port $BackendPort
        Write-Log "全部服务已停止" "OK"
    }
    "status" {
        Show-Status
    }
}
