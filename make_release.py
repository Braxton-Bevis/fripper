"""Build dist/FRipper.zip: one download for Windows and macOS friends.

Only whitelisted app files are packaged. Personal state (.local: queue,
listening history, TIDAL session), .venv, vendor and downloads never are.
Run: .venv\\Scripts\\python.exe make_release.py
"""

from __future__ import annotations

from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
TOP_LEVEL = ("*.py", "*.ps1", "*.cmd", "*.command", "*.vbs", "README.md", "README.txt", "SOURCE.txt", ".gitignore")
FOLDERS = {"assets": ("*.png", "*.ico", "*.txt", "*.ps1"), "tests": ("*.py",), "web": ("*.html", "*.webmanifest")}
SKIP = {"Launch Lucida.cmd", "Launch Lucida.vbs"}  # Superseded by the FRipper launchers.
EXECUTABLE = {".command", ".sh"}
PRIVATE_MARKERS = (b"refresh_token", b"access_token\":", b"TIDAL-DPAPI")


def files():
    chosen = {path for pattern in TOP_LEVEL for path in ROOT.glob(pattern) if path.name not in SKIP}
    for folder, patterns in FOLDERS.items():
        chosen.update(path for pattern in patterns for path in (ROOT / folder).glob(pattern))
    return sorted(path for path in chosen if path.is_file())


def main():
    DIST.mkdir(exist_ok=True)
    target = DIST / "FRipper.zip"
    selected = files()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in selected:
            data = path.read_bytes()
            if path.suffix in {".py", ".txt", ".ps1"} and any(marker in data for marker in PRIVATE_MARKERS if path.name != "make_release.py") \
                    and path.name not in {"tidal_account.py", "tidal_direct.py"} and path.parent.name != "tests":
                raise SystemExit(f"Refusing to package {path.name}: it looks like it holds session data.")
            relative = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(f"FRipper/{relative}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so macOS keeps the executable bit below.
            mode = 0o755 if path.suffix in EXECUTABLE else 0o644
            info.external_attr = (0o100000 | mode) << 16
            if path.suffix in EXECUTABLE:
                data = data.replace(b"\r\n", b"\n")  # bash rejects CRLF scripts.
            archive.writestr(info, data)
    print(f"Wrote {target} ({target.stat().st_size // 1024} KB, {len(selected)} files)")
    for path in selected:
        print("  ", path.relative_to(ROOT).as_posix())


if __name__ == "__main__":
    main()
