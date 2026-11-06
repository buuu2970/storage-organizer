$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$build = Join-Path $PSScriptRoot 'build'
New-Item -ItemType Directory -Force $build | Out-Null
$html = Get-Content -Raw -Encoding utf8 dist/index.html
$style = Get-Content -Raw -Encoding utf8 dist/style.css
$script = Get-Content -Raw -Encoding utf8 dist/app.js
$html = $html.Replace('<link rel="stylesheet" href="style.css">',"<style>$style</style>")
$html = $html.Replace('<script src="app.js"></script>',"<script>$script</script>")
Set-Content -Encoding utf8 -LiteralPath build/RecSort.html -Value $html
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /codepage:65001 /optimize+ /reference:System.Web.Extensions.dll '/resource:build\RecSort.html,RecSort.html' '/out:build\RecSort-PC.exe' 'companion\Scan.cs'
if ($LASTEXITCODE) { throw '調査ツールのビルドに失敗しました。' }
Compress-Archive -Force -LiteralPath build/RecSort-PC.exe -DestinationPath dist/RecSort-PC.zip
