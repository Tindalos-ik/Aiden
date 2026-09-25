<#
.SYNOPSIS
为本地 embedding 服务准备独立 Python 环境。

.DESCRIPTION
BGE-M3 只是模型权重，不会响应 HTTP 请求；本脚本搭好运行它所需的环境，再由
backend/scripts/embedding_server.py 把它包成 OpenAI 兼容的 /v1/embeddings 服务。

本项目按 CPU 推理部署，不需要显卡，也不需要安装 CUDA。脚本默认安装 CPU 版 torch
（约 0.3 GB）。几十到几百块知识库在 CPU 上整库向量化只需几秒，速度足够；只有在知识库
规模大幅增长、或需要频繁重建时，才需要改用 GPU。

为什么单独建环境：torch 及推理依赖体积大（CPU 版约 0.3 GB，CUDA 版约 2.5 GB），且与后端
FastAPI 运行时无关，放进 backend/.venv 会拖慢并污染后端依赖，所以这里用独立 venv。
后端 requirements.txt 不因此改变。

依赖与模型默认都放 D 盘（C 盘空间通常更紧张）：
  * venv：D:\bge-m3-env
  * 模型缓存：D:\hf-cache（通过用户级 HF_HOME 设置，一次性，可用 -NoPersistEnv 跳过）

.PARAMETER Gpu
可选。安装 CUDA 版 torch（约 2.5 GB）以启用 GPU 推理。不加此开关则装 CPU 版，这是本项目的
默认部署方式。改用它之后，启动服务时可以加 --fp16 进一步降低显存占用。

.PARAMETER EnvDir
venv 目录，默认 D:\bge-m3-env。

.PARAMETER HfHome
模型缓存目录，默认 D:\hf-cache。

.PARAMETER NoPersistEnv
只在本进程设置 HF_HOME，不写入用户级环境变量。

.PARAMETER HfEndpoint
模型下载源，默认 https://hf-mirror.com。国内网络直连 huggingface.co 时，网页能打开但
Python 客户端会在 API 握手阶段被重置连接（WinError 10054），必须走镜像才能下载权重。
传空字符串可恢复官方地址。

.EXAMPLE
# 常规用法：CPU 版环境，一次装好
.\backend\scripts\setup_embedding_server.ps1

.EXAMPLE
# 可选：改用 GPU 推理
.\backend\scripts\setup_embedding_server.ps1 -Gpu
#>
#requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$Gpu,
    [string]$EnvDir = 'D:\bge-m3-env',
    [string]$HfHome = 'D:\hf-cache',
    [string]$HfEndpoint = 'https://hf-mirror.com',
    [switch]$NoPersistEnv
)

$ErrorActionPreference = 'Stop'

function Write-Step {
    param([string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

# ---------------------------------------------------------------------------
# 1. 解释器：优先用本机已有的 Python 3.10+（后端 venv 里那个即可）
# ---------------------------------------------------------------------------
Write-Step '检查 Python 解释器'
$basePython = $null
foreach ($candidate in @(
    (Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'),
    (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
)) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) { $basePython = (Resolve-Path -LiteralPath $candidate).Path; break }
}
if (-not $basePython) { throw '找不到 python.exe，请先安装 Python 3.10 或更高版本。' }
& $basePython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
if ($LASTEXITCODE -ne 0) { throw "需要 Python 3.10+，当前：$(& $basePython -V)" }
Write-Host "使用解释器：$basePython（$(& $basePython -V)）"

# ---------------------------------------------------------------------------
# 2. 模型缓存位置：D 盘更宽裕，一次性写用户级变量
# ---------------------------------------------------------------------------
Write-Step "设置模型缓存目录：$HfHome"
New-Item -ItemType Directory -Path $HfHome -Force | Out-Null
$env:HF_HOME = $HfHome
if ($HfEndpoint) {
    $env:HF_ENDPOINT = $HfEndpoint
    Write-Host "模型下载源：$HfEndpoint"
} else {
    Remove-Item Env:\HF_ENDPOINT -ErrorAction SilentlyContinue
    Write-Host '模型下载源：HuggingFace 官方地址'
}
if (-not $NoPersistEnv) {
    [Environment]::SetEnvironmentVariable('HF_HOME', $HfHome, 'User')
    [Environment]::SetEnvironmentVariable('HF_ENDPOINT', $HfEndpoint, 'User')
    # 缓存目录所在磁盘若不支持符号链接，huggingface_hub 会反复告警，这里直接静音。
    [Environment]::SetEnvironmentVariable('HF_HUB_DISABLE_SYMLINKS_WARNING', '1', 'User')
    Write-Host "已写入用户级 HF_HOME=$HfHome、HF_ENDPOINT=$HfEndpoint"
    Write-Host "撤销方法：[Environment]::SetEnvironmentVariable('HF_ENDPOINT',`$null,'User')"
} else {
    Write-Host '仅在本进程生效（-NoPersistEnv）。'
}

# ---------------------------------------------------------------------------
# 3. 建独立 venv
# ---------------------------------------------------------------------------
Write-Step "准备独立虚拟环境：$EnvDir"
$venvPython = Join-Path $EnvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $basePython -m venv $EnvDir
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败。' }
    Write-Host '已创建虚拟环境。'
} else {
    Write-Host '虚拟环境已存在，复用。'
}

& $venvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip 升级失败。' }

# ---------------------------------------------------------------------------
# 4. 安装依赖
#    transformers 固定 <5：FlagEmbedding 1.4.2 尚未适配 transformers 5.x 的 API。
# ---------------------------------------------------------------------------
Write-Step '安装推理依赖（CPU 版约 0.3 GB，请耐心等待）'
& $venvPython -m pip install 'FlagEmbedding==1.4.2' 'transformers>=4.44,<5' 'fastapi>=0.115,<1.0' 'uvicorn>=0.30,<1.0'
if ($LASTEXITCODE -ne 0) { throw '安装 FlagEmbedding 等依赖失败。' }

Write-Step '安装 PyTorch'
if ($Gpu) {
    # CUDA 12.1 wheel 自带所需运行库，本机无需另装 CUDA Toolkit。
    & $venvPython -m pip install torch --index-url https://download.pytorch.org/whl/cu121
} else {
    & $venvPython -m pip install torch --index-url https://download.pytorch.org/whl/cpu
}
if ($LASTEXITCODE -ne 0) { throw '安装 torch 失败。' }

# ---------------------------------------------------------------------------
# 5. 自检：确认关键依赖可导入，并报告推理设备
# ---------------------------------------------------------------------------
Write-Step '自检'
& $venvPython -c @'
import torch, transformers, FlagEmbedding, fastapi, uvicorn
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("FlagEmbedding:", FlagEmbedding.__version__ if hasattr(FlagEmbedding, "__version__") else "ok")
if torch.cuda.is_available():
    print("推理设备: GPU -", torch.cuda.get_device_name(0))
else:
    print("推理设备: CPU（本项目默认部署方式，几十到几百块知识库的向量化只需几秒）")
'@
if ($LASTEXITCODE -ne 0) { throw '依赖自检失败。' }

Write-Host ''
Write-Host '环境准备完成。下一步（模型权重在首次启动时自动下载）：' -ForegroundColor Green
Write-Host "  # 先用小模型验证链路（约 0.2 GB）"
Write-Host "  $venvPython backend\scripts\embedding_server.py --model BAAI/bge-small-zh-v1.5"
Write-Host "  # 再用真实 bge-m3（约 2.3 GB）"
Write-Host "  $venvPython backend\scripts\embedding_server.py --model BAAI/bge-m3"
Write-Host ''
Write-Host '注意：CPU 推理不要传 --fp16，该参数只在 GPU 上有效。'
