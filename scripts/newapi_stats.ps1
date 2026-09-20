param(
    [Parameter(Mandatory=$true)]
    [string]$StartTime,

    [Parameter(Mandatory=$true)]
    [string]$EndTime,

    [string]$TokenName = "yonwork"
)

# ===== 配置走环境变量，别再把 token 写进文件 =====
# 令牌在 NewAPI「个人设置 → 生成系统访问令牌」里生成，然后：
#   PowerShell : $env:NEWAPI_ACCESS_TOKEN = "..."
#   从 WSL 调用: WSLENV=NEWAPI_ACCESS_TOKEN NEWAPI_ACCESS_TOKEN=... powershell.exe ...
#   （WSLENV 不声明的话 Windows 进程里读不到，见 CLAUDE.md 坑 3）
$BaseUrl     = if ($env:NEWAPI_BASE_URL) { $env:NEWAPI_BASE_URL } else { "http://127.0.0.1:3000" }
$UserId      = if ($env:NEWAPI_USER_ID)  { $env:NEWAPI_USER_ID }  else { "1" }
$AccessToken = $env:NEWAPI_ACCESS_TOKEN

if (-not $AccessToken) {
    throw "缺少 NEWAPI_ACCESS_TOKEN：在 NewAPI「个人设置 → 生成系统访问令牌」生成后设进环境变量。"
}


# Power Automate 传来的本地时间
$start = [DateTime]::Parse($StartTime)
$end   = [DateTime]::Parse($EndTime)

$startOffset = [DateTimeOffset]::new($start)
$endOffset   = [DateTimeOffset]::new($end)

# 前后留几秒余量，防止日志落库稍有延迟
$startTs = $startOffset.ToUnixTimeSeconds() - 3
$endTs   = $endOffset.ToUnixTimeSeconds() + 10

$headers = @{
    "Authorization" = "Bearer $AccessToken"
    "New-Api-User"  = $UserId
}

$encodedTokenName = [Uri]::EscapeDataString($TokenName)

$allLogs = @()
$page = 1

do {
    $url =
        "$BaseUrl/api/log/self" +
        "?p=$page" +
        "&page_size=100" +
        "&start_timestamp=$startTs" +
        "&end_timestamp=$endTs" +
        "&token_name=$encodedTokenName"

    $response = Invoke-RestMethod `
        -Uri $url `
        -Method Get `
        -Headers $headers

    if (-not $response.success) {
        throw "New API查询失败：$($response.message)"
    }

    $items = @($response.data.items)
    $allLogs += $items

    $total = [int]$response.data.total
    $page++

} while ($allLogs.Count -lt $total -and $items.Count -gt 0)


# 2 = 消费日志
# 4 = 错误日志
$consumeLogs = @($allLogs | Where-Object { $_.type -eq 2 })
$errorLogs   = @($allLogs | Where-Object { $_.type -eq 4 })

$apiCalls = $consumeLogs.Count + $errorLogs.Count
$errorCalls = $errorLogs.Count


$inputTokens = (
    $consumeLogs |
    Measure-Object -Property prompt_tokens -Sum
).Sum

$outputTokens = (
    $consumeLogs |
    Measure-Object -Property completion_tokens -Sum
).Sum

$apiUseTime = (
    $consumeLogs |
    Measure-Object -Property use_time -Sum
).Sum


if ($null -eq $inputTokens)  { $inputTokens = 0 }
if ($null -eq $outputTokens) { $outputTokens = 0 }
if ($null -eq $apiUseTime)   { $apiUseTime = 0 }

$totalTokens = $inputTokens + $outputTokens


# 输出给 Power Automate
# 格式：
# Calls|Errors|Input|Output|Total|APIUseTime
Write-Output "$apiCalls|$errorCalls|$inputTokens|$outputTokens|$totalTokens|$apiUseTime"