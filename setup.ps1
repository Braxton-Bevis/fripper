# FRipper setup: installs dependencies when needed and adds Start menu / Desktop shortcuts
# that Windows can pin to the taskbar. Safe to run again to repair or update.
#
#   setup.ps1                 Install (from a shared copy) or repair this folder, then add shortcuts
#   setup.ps1 -ShortcutsOnly  Only (re)create shortcuts for this folder; installs nothing
#   setup.ps1 -Uninstall      Remove the shortcuts. Music, queue and sign-ins are left in place.
param([switch]$ShortcutsOnly, [switch]$Uninstall, [switch]$NoDesktopShortcut,
      [string]$InstallDir = '', [switch]$NoShortcuts, [switch]$NoLaunch)

$ErrorActionPreference = 'Stop'
$AppName = 'FRipper'
$AppId = 'FRipper.Desktop'   # Must match APP_ID in gui.py so the window groups with the pinned icon.
$Source = $PSScriptRoot
if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName" }
$StartMenuLink = Join-Path ([Environment]::GetFolderPath('Programs')) "$AppName.lnk"
$DesktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Find-Python {
    $check = 'import sys, tkinter, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1); '
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in '-3.12', '-3.13', '-3.11', '-3.10', '-3') { $candidates += , @('py', $v) }
    }
    foreach ($dir in Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending) {
        $candidates += , @((Join-Path $dir.FullName 'python.exe'))
    }
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    # Includes Microsoft Store Python; the Store's placeholder stub simply fails the check below.
    if ($onPath) { $candidates += , @($onPath.Source) }
    foreach ($c in $candidates) {
        try {
            $exe = $c[0]; $rest = @($c | Select-Object -Skip 1)
            & $exe @rest -c ($check + 'print(sys.executable)') *> $null
            if ($LASTEXITCODE -eq 0) {
                return (& $exe @rest -c 'import sys; print(sys.executable)').Trim()
            }
        } catch { }
    }
    return $null
}

function Install-WithWinget($id, $label) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "$label is required. Install it, then run this setup again. (winget was not found to install it automatically.)"
    }
    Write-Host "Installing $label with winget. Approve any Windows prompt that appears."
    & winget install --id $id -e --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        & winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements
    }
    Refresh-Path
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace FRipperSetup {
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder f, int cch, IntPtr fd, uint flags);
        void GetIDList(out IntPtr pidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int cch);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string s);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int cch);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string s);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int cch);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string s);
        void GetHotkey(out short k);
        void SetHotkey(short k);
        void GetShowCmd(out int c);
        void SetShowCmd(int c);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int cch, out int i);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string s, int i);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string s, uint r);
        void Resolve(IntPtr hwnd, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string s);
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    struct PropertyKey { public Guid fmtid; public uint pid; }

    [StructLayout(LayoutKind.Explicit, Size = 24)]
    struct PropVariant { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr ptr; }

    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        void GetCount(out uint c);
        void GetAt(uint i, out PropertyKey k);
        void GetValue(ref PropertyKey k, out PropVariant v);
        void SetValue(ref PropertyKey k, ref PropVariant v);
        void Commit();
    }

    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class ShellLink { }

    public static class Shortcut {
        public static void Create(string path, string target, string args, string workdir, string icon, string description, string appId) {
            var link = (IShellLinkW)new ShellLink();
            link.SetPath(target);
            link.SetArguments(args);
            link.SetWorkingDirectory(workdir);
            link.SetIconLocation(icon, 0);
            link.SetDescription(description);
            var store = (IPropertyStore)link;
            var key = new PropertyKey { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
            var value = new PropVariant { vt = 31, ptr = Marshal.StringToCoTaskMemUni(appId) };
            try { store.SetValue(ref key, ref value); store.Commit(); }
            finally { Marshal.FreeCoTaskMem(value.ptr); }
            ((IPersistFile)link).Save(path, true);
        }
    }
}
'@

function New-AppShortcut($path, $root) {
    $pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
    $gui = Join-Path $root 'gui.py'
    $icon = Join-Path $root 'assets\fripper.ico'
    [FRipperSetup.Shortcut]::Create($path, $pythonw, "`"$gui`"", $root, $icon, 'FRipper music downloader', $AppId)
    Write-Host "Shortcut: $path"
}

if ($Uninstall) {
    foreach ($link in $StartMenuLink, $DesktopLink) { if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link; Write-Host "Removed $link" } }
    Write-Host "Shortcuts removed. App files, downloads and saved sign-ins were not touched."
    exit 0
}

# An existing working folder (like the developer copy) is used where it is.
# A fresh shared copy (no .venv yet) is installed into the user's Programs folder.
$Root = $Source
if (-not $ShortcutsOnly -and -not (Test-Path -LiteralPath (Join-Path $Source '.venv\Scripts\pythonw.exe'))) {
    $Root = $InstallDir
    if ((Resolve-Path -LiteralPath $Source).Path.TrimEnd('\') -ne $InstallDir.TrimEnd('\')) {
        Step "Copying $AppName to $InstallDir"
        New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
        $skip = '.local', '.venv', 'vendor', 'downloads', 'dist', '__pycache__'
        Get-ChildItem -LiteralPath $Source -Force | Where-Object { $skip -notcontains $_.Name } | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $InstallDir -Recurse -Force
        }
        Get-ChildItem -LiteralPath $InstallDir -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
    }
}

if (-not $ShortcutsOnly) {
    $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Step 'Checking for Python 3.10+'
        $python = Find-Python
        if (-not $python) {
            Install-WithWinget 'Python.Python.3.12' 'Python 3.12'
            $python = Find-Python
            if (-not $python) { throw 'Python installed but could not be found. Close this window and run setup again.' }
        }
        Write-Host "Using $python"
        & $python -m venv (Join-Path $Root '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
    }
    Step 'Checking for Git'
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Install-WithWinget 'Git.Git' 'Git'
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git installed but could not be found. Close this window and run setup again.' }
    }
    Step 'Installing download engines (this can take several minutes the first time)'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'install.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. The error is shown above.' }
}

if (-not (Test-Path -LiteralPath (Join-Path $Root '.venv\Scripts\pythonw.exe'))) {
    throw "No Python environment in $Root. Run setup without -ShortcutsOnly first."
}
if ($NoShortcuts) { Write-Host "Installed in $Root (shortcuts skipped)."; exit 0 }
Step 'Adding shortcuts'
New-AppShortcut $StartMenuLink $Root
if (-not $NoDesktopShortcut) { New-AppShortcut $DesktopLink $Root }

Write-Host "`n$AppName is ready." -ForegroundColor Green
if ($Root -ne $Source -and -not $NoLaunch) { Start-Process -FilePath $StartMenuLink }
Write-Host "Open it from the Start menu or Desktop. To pin it: right-click $AppName in Start, then Pin to taskbar"
Write-Host "(or right-click its taskbar icon while it is open, then Pin to taskbar)."
