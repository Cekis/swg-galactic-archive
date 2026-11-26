[CmdletBinding()]
param(
    [int]$Port = 8768,
    [Alias('Sources')]
    [string]$SourcesConfig,
    [string]$GameRoot,
    [string]$ClientData,
    [string]$ClientTools,
    [switch]$Rebuild,
    [switch]$Background
)

$ErrorActionPreference = 'Stop'
if ($GameRoot) { $env:SWG_GAME_ROOT = (Resolve-Path -LiteralPath $GameRoot).Path }
$assetArgs = @()
if ($SourcesConfig) { $assetArgs += @('--sources', (Resolve-Path -LiteralPath $SourcesConfig).Path) }
if ($ClientData) { $assetArgs += @('--clientdata', (Resolve-Path -LiteralPath $ClientData).Path) }
if ($ClientTools) { $assetArgs += @('--client-tools', (Resolve-Path -LiteralPath $ClientTools).Path) }

$buildArgs = $assetArgs
if (-not $Rebuild) { $buildArgs += '--if-needed' }
& python -B (Join-Path $PSScriptRoot 'build_catalog.py') @buildArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$serveScript = Join-Path $PSScriptRoot 'serve.py'
if ($Background) {
    $pythonPath = Get-Command python -CommandType Application -All | Select-Object -First 1 -ExpandProperty Source
    $runtime = Join-Path $PSScriptRoot '.local'
    $serverArgs = @('-B', '-X', 'utf8', $serveScript, '--port', [string]$Port) + $assetArgs
    $serverArguments = $serverArgs | ForEach-Object {
        if ($_ -match '\s') { '"' + ($_ -replace '(\\+)$', '$1$1') + '"' } else { $_ }
    }
    $stdout = Join-Path $runtime 'server.stdout.log'
    $stderr = Join-Path $runtime 'server.stderr.log'
    $worker = Start-Process -FilePath $pythonPath -ArgumentList $serverArguments -WorkingDirectory $PSScriptRoot `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    $address = "http://127.0.0.1:$Port"
    $expiry = [DateTime]::UtcNow.AddSeconds(15)
    do {
        if ($worker.HasExited) {
            Get-Content -LiteralPath $stderr
            throw "Archive process exited with code $($worker.ExitCode)."
        }
        try {
            $health = Invoke-RestMethod "$address/api/health" -TimeoutSec 1
            if ($health.status -eq 'ready' -and (Select-String -LiteralPath $stdout -SimpleMatch "Local: $address/")) {
                Set-Content -LiteralPath (Join-Path $runtime 'server.pid') -Value $worker.Id
                Write-Output "$address/ (process $($worker.Id))"
                exit 0
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    } while ([DateTime]::UtcNow -lt $expiry)
    throw "Could not verify archive process $($worker.Id) at $address. See $stderr."
}
& python -B $serveScript --port $Port @assetArgs
exit $LASTEXITCODE
