<#
.SYNOPSIS
一键验证本地 BGE-M3 embedding 服务与 Aiden 离线建库链路。

.DESCRIPTION
按顺序做四件事，任一步失败就停下并给出原因：
  1. 检查服务是否已在监听（未启动则提示如何启动）；
  2. 用 verify_services.py 探测真实向量维度与 Milvus 连通性（可选 --write 验证写入幂等）；
  3. 把实际维度回写到 backend/.env 的 EMBEDDING_DIMENSION 与 EMBEDDING_TOKENIZER_PATH；
  4. 用一个临时库跑完整建库（导入 -> 向量化 -> 自检），不触碰现有 aiden 库。

为什么必须回写维度：应用侧会用 EMBEDDING_DIMENSION 与真实返回值比对，不一致会直接报错，
所以换模型（如 bge-small 512 维 -> bge-m3 1024 维）后必须同步这一项。

.PARAMETER Model
当前正在运行的模型名，用于提示与记录，默认 BAAI/bge-small-zh-v1.5。

.PARAMETER HealthUrl
服务健康检查地址，默认 http://127.0.0.1:8001/health。

.PARAMETER SkipMilvus
只验向量服务，不连 Milvus（Milvus 尚未部署时用）。

.PARAMETER Write
额外做 Milvus 写入幂等验证（会创建并删除临时探测集合）。

.EXAMPLE
.\backend\scripts\verify_embedding_service.ps1
#>
#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$Model = 'BAAI/bge-small-zh-v1.5',
    [string]$HealthUrl = 'http://127.0.0.1:8001/health',
    [switch]$SkipMilvus,
    [switch]$Write
)

$ErrorActionPreference = 'Stop'
$backend = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $backend '.venv\Scripts\python.exe'

function Write-Step {
    param([string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

if (-not (Test-Path -LiteralPath $python)) { throw "找不到后端虚拟环境：$python" }

# ---------------------------------------------------------------------------
# 1. 服务是否在监听
# ---------------------------------------------------------------------------
Write-Step "检查 embedding 服务：$HealthUrl"
$health = $null
try {
    $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
} catch {
    Write-Host "服务未响应：$($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host ''
    Write-Host '请先在另一个终端启动服务（模型权重首次会自动下载）：' -ForegroundColor Yellow
    Write-Host "  D:\bge-m3-env\Scripts\python.exe backend\scripts\embedding_server.py --model $Model"
    Write-Host ''
    Write-Host '若尚未准备环境，先执行：'
    Write-Host '  .\backend\scripts\setup_embedding_server.ps1'
    exit 2
}
Write-Host "服务在线：model=$($health.model) dimension=$($health.dimension)" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 2. 真实维度与 Milvus 探测
# ---------------------------------------------------------------------------
Write-Step '探测真实向量服务与 Milvus'
Push-Location $backend
try {
    if ($SkipMilvus) {
        & $python '.tmp\verify_services.py' 2>&1 | Select-Object -First 12
        Write-Host '（-SkipMilvus：跳过 Milvus 相关断言，因此该步可能报失败，属预期）' -ForegroundColor DarkGray
    } elseif ($Write) {
        & $python '.tmp\verify_services.py' --write
    } else {
        & $python '.tmp\verify_services.py'
    }
    $probeExit = $LASTEXITCODE
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
# 3. 回写真实维度与本地 tokenizer 路径
# ---------------------------------------------------------------------------
Write-Step '把真实维度与 tokenizer 路径写回 backend/.env'
$envFile = Join-Path $backend '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Host "未找到 $envFile，跳过回写。请确认已按 .env.example 配置。" -ForegroundColor Yellow
} else {
    $dimension = $health.dimension
    $content = [System.IO.File]::ReadAllText($envFile)
    $updated = $content
    if ($dimension) {
        if ($updated -match '(?m)^EMBEDDING_DIMENSION=.*$') {
            $updated = $updated -replace '(?m)^EMBEDDING_DIMENSION=.*$', "EMBEDDING_DIMENSION=$dimension"
        } else {
            $updated = $updated.TrimEnd() + "`r`nEMBEDDING_DIMENSION=$dimension`r`n"
        }
        Write-Host "EMBEDDING_DIMENSION -> $dimension"
    }

    # tokenizer.json 就在 HF 缓存里；有了它，切分的长度计量从字符估算升级为精确 token 计数。
    $hfHome = if ($env:HF_HOME) { $env:HF_HOME } else { Join-Path $env:USERPROFILE '.cache\huggingface' }
    $snapshotRoot = Join-Path $hfHome 'hub'
    $tokenizerPath = $null
    if (Test-Path -LiteralPath $snapshotRoot) {
        $modelDirName = 'models--' + ($Model -replace '/', '--')
        $candidateRoot = Join-Path $snapshotRoot "$modelDirName\snapshots"
        if (Test-Path -LiteralPath $candidateRoot) {
            $tokenizerPath = Get-ChildItem -LiteralPath $candidateRoot -Recurse -Filter 'tokenizer.json' -File -ErrorAction SilentlyContinue |
                Select-Object -First 1 -ExpandProperty FullName
        }
    }
    if ($tokenizerPath) {
        $escaped = $tokenizerPath -replace '\\', '\\'
        if ($updated -match '(?m)^EMBEDDING_TOKENIZER_PATH=.*$') {
            $updated = $updated -replace '(?m)^EMBEDDING_TOKENIZER_PATH=.*$', "EMBEDDING_TOKENIZER_PATH=$tokenizerPath"
        } else {
            $updated = $updated.TrimEnd() + "`r`nEMBEDDING_TOKENIZER_PATH=$tokenizerPath`r`n"
        }
        Write-Host "EMBEDDING_TOKENIZER_PATH -> $tokenizerPath"
        Write-Host '（精确 token 计数：切分预算将与模型侧一致）'
    } else {
        Write-Host "未在 $snapshotRoot 找到 $Model 的 tokenizer.json，保持字符估算模式。" -ForegroundColor Yellow
    }

    if ($updated -ne $content) {
        [System.IO.File]::WriteAllText($envFile, $updated)
        Write-Host '已更新 backend/.env（该文件不进版本库）。'
    } else {
        Write-Host 'backend/.env 无需改动。'
    }
}

Write-Step '结论'
if ($probeExit -eq 0) {
    Write-Host '向量服务探测通过。' -ForegroundColor Green
} else {
    Write-Host '向量服务探测存在失败项，请先按上面的提示解决，再跑建库验证。' -ForegroundColor Yellow
}
Write-Host ''
Write-Host '下一步：用临时库跑完整建库（不触碰现有 aiden 库）'
Write-Host "  python backend\.tmp\verify_mysql.py --drop"
Write-Host "  python backend\.tmp\verify_crash_recovery.py"
exit $probeExit
