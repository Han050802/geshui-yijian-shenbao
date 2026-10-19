# Build both portable Windows launchers with Python 3.12 x64.
param([string]$PythonExe = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($env:OS -ne 'Windows_NT') { throw 'Build this Windows application on Windows.' }
& $PythonExe -c "import sys,struct; assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8, 'Python 3.12 x64 is required'"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 is required' }

New-Item -ItemType Directory -Force -Path 'build-work', 'build-reports' | Out-Null
$Python = Join-Path $PSScriptRoot 'build-work\venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    & $PythonExe -m venv 'build-work\venv'
    if ($LASTEXITCODE -ne 0) { throw 'Could not create isolated build environment' }
}
& $Python -m pip install --disable-pip-version-check -r requirements.txt -r wechat/requirements.txt -r requirements-bridge.txt 'wxauto4==41.1.7' 'winocr==0.0.15'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency validation failed' }
& $Python -m pip freeze | Set-Content -LiteralPath 'build-reports\dependencies.txt' -Encoding utf8
$env:PLAYWRIGHT_BROWSERS_PATH = '0'
& $Python -m playwright install chromium --no-shell
if ($LASTEXITCODE -ne 0) { throw 'Bundled Chromium download failed' }

& $Python -m unittest discover -s tests -v 2>&1 | Tee-Object -FilePath 'build-reports\unit-tests.txt'
if ($LASTEXITCODE -ne 0) { throw 'Offline unit tests failed' }
$Common = @('--noconfirm', '--clean', '--onedir', '--windowed',
    '--distpath', 'build-dist', '--workpath', 'build-work\pyinstaller',
    '--specpath', 'build-work\specs',
    '--exclude-module', 'torch', '--exclude-module', 'tensorflow',
    '--exclude-module', 'matplotlib', '--exclude-module', 'pandas',
    '--exclude-module', 'scipy', '--exclude-module', 'IPython',
    '--exclude-module', 'notebook', '--exclude-module', 'pytest')
New-Item -ItemType Directory -Force -Path 'build-work\specs' | Out-Null
$MainArgs = $Common + @('--name', 'TaxZero',
    '--collect-all', 'rapidocr_onnxruntime', '--collect-all', 'onnxruntime',
    '--collect-all', 'playwright', '--collect-submodules', 'pywinauto',
    '--collect-all', 'cryptography',
    '--hidden-import', 'pywinauto.keyboard', '--hidden-import', 'pywinauto.mouse',
    '--hidden-import', 'win32timezone', 'app.py')
& $Python -m PyInstaller @MainArgs
if ($LASTEXITCODE -ne 0) { throw 'Main application build failed' }
$WeChatArgs = $Common + @('--name', 'WeChatReceipt', '--paths', 'wechat',
    '--collect-all', 'wxauto4', '--collect-all', 'winrt', '--collect-submodules', 'winrt',
    '--collect-all', 'httpx', '--collect-all', 'comtypes', '--collect-all', 'psutil', '--collect-all', 'PIL',
    '--collect-submodules', 'pywinauto', '--hidden-import', 'winocr',
    '--hidden-import', 'pyperclip', '--hidden-import', 'tenacity', '--hidden-import', 'colorama',
    '--hidden-import', 'comtypes.stream', '--hidden-import', 'win32com.client',
    '--hidden-import', 'win32ui', '--hidden-import', 'win32timezone',
    '--hidden-import', 'wechat_report', '--hidden-import', 'wechat_a11y',
    '--hidden-import', 'group_mapping', '--collect-all', 'openpyxl')
$WeChatModules = & $Python -c "from wechat_launcher import DEPENDENCY_MODULES; print('\n'.join(DEPENDENCY_MODULES))"
if ($LASTEXITCODE -ne 0) { throw 'Cannot read WeChat runtime dependency list' }
foreach ($Module in $WeChatModules) { $WeChatArgs += @('--hidden-import', $Module) }
$WeChatArgs += 'wechat_launcher.py'
& $Python -m PyInstaller @WeChatArgs
if ($LASTEXITCODE -ne 0) { throw 'WeChat launcher build failed' }

# Preserve old runtimes if rebuilding locally; keep settings and filing state.
foreach ($Pair in @(@('TaxZero', 'runtime'), @('WeChatReceipt', 'runtime-wechat'))) {
    $Built = Join-Path 'build-dist' $Pair[0]
    $Destination = $Pair[1]
    if (Test-Path -LiteralPath $Destination) {
        $Backup = $Destination + '.previous-' + [guid]::NewGuid().ToString('N')
        Move-Item -LiteralPath $Destination -Destination $Backup
    }
    Copy-Item -LiteralPath $Built -Destination $Destination -Recurse
}
& $Python tools/windows_smoke.py
if ($LASTEXITCODE -ne 0) { throw 'Packaged executable self-test failed' }
Write-Output 'Build complete: runtime\TaxZero.exe and runtime-wechat\WeChatReceipt.exe'

