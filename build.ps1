# Rebuild using Python 3.12. Existing runtime files must not be running.
param([string]$PythonExe = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& $PythonExe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& $PythonExe -m PyInstaller --noconfirm --onedir --windowed --name TaxZero --distpath build-dist --workpath build-work --specpath build-work --collect-all rapidocr_onnxruntime --collect-all onnxruntime --collect-all playwright --hidden-import pywinauto --hidden-import pywinauto.keyboard --exclude-module torch --exclude-module tensorflow --exclude-module matplotlib --exclude-module pandas --exclude-module scipy --exclude-module IPython --exclude-module notebook --exclude-module pytest app.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
New-Item -ItemType Directory -Force -Path runtime | Out-Null
Get-ChildItem -LiteralPath 'build-dist\TaxZero' | Copy-Item -Destination runtime -Recurse -Force
Write-Output 'Build complete: runtime\TaxZero.exe'
