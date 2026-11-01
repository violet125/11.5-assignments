<#
    招商匹配 Agent 启动脚本（Windows PowerShell）

    用法：
        powershell -ExecutionPolicy Bypass -File scripts/start.ps1
        powershell -ExecutionPolicy Bypass -File scripts/start.ps1 -Port 9000
#>

param(
    [string]$Python = "python",
    [int]$Port = 8765,
    [string]$Host_ = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:AGENT_HOST = $Host_
$env:AGENT_PORT = "$Port"

Write-Host "招商 Agent：http://$Host_`:$Port（仅本机访问）" -ForegroundColor Cyan
Write-Host "按 Ctrl+C 停止服务。"

& $Python app.py
