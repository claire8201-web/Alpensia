# Alpensia

Alpensia golf reservation desktop tool with Selenium automation and a GitHub Releases launcher for free updates.

## Main files

- `Alpensia_V4.1.1.py`: main reservation app
- `AlpensiaLauncher.py`: update launcher
- `Prepare-AlpensiaRelease.ps1`: copies release metadata into `dist/`

## Build

```powershell
pyinstaller "Alpensia_V4_1_1.spec" --noconfirm
pyinstaller "AlpensiaLauncher.spec" --noconfirm
powershell -ExecutionPolicy Bypass -File ".\Prepare-AlpensiaRelease.ps1"
```
