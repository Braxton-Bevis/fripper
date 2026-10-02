#!/bin/bash
# FRipper installer for macOS. Double-click (first time: right-click > Open).
# Installs into ~/FRipper and adds FRipper.app to ~/Applications. Safe to run again.
set -euo pipefail

APP="FRipper"
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/FRipper"
LUCIDADL_REPO="https://github.com/Jude-A/lucidadl"
LUCIDADL_REV="bf3fa1eb87634b3f60bea787c222351c82ec2f16"
PY_PKG_URL="https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg"

step() { printf '\n\033[36m== %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$1"; read -r -p "Press Return to close." _; exit 1; }
trap 'fail "Setup stopped because of the error above."' ERR

find_python() {
  for py in /Library/Frameworks/Python.framework/Versions/3.1{3,2,1,0}/bin/python3 \
            /opt/homebrew/bin/python3.1{3,2,1,0} /usr/local/bin/python3.1{3,2,1,0} "$(command -v python3 || true)"; do
    [ -x "$py" ] || continue
    if "$py" -c 'import sys, tkinter, venv; tkinter.Tcl(); sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      echo "$py"; return 0
    fi
  done
  return 1
}

step "Copying $APP to $DEST"
mkdir -p "$DEST"
if [ "$SRC" != "$DEST" ]; then
  rsync -a --exclude '.local' --exclude '.venv' --exclude 'vendor' --exclude 'downloads' \
        --exclude 'dist' --exclude '__pycache__' "$SRC/" "$DEST/"
fi
cd "$DEST"

if [ ! -x .venv/bin/python ]; then
  step "Checking for Python 3.10+ with Tk"
  PY="$(find_python || true)"
  if [ -z "$PY" ]; then
    echo "Python 3.12 from python.org is needed. Downloading it now (about 45 MB)."
    curl -fL --progress-bar -o /tmp/fripper-python.pkg "$PY_PKG_URL"
    echo "Enter your Mac password to install Python:"
    sudo installer -pkg /tmp/fripper-python.pkg -target /
    rm -f /tmp/fripper-python.pkg
    PY="$(find_python || true)"
    [ -n "$PY" ] || fail "Python was installed but could not be found. Run this installer again."
  fi
  echo "Using $PY"
  "$PY" -m venv .venv
fi

step "Downloading the Lucida engine source (pinned revision)"
if [ ! -f vendor/lucidadl/pyproject.toml ]; then
  rm -rf vendor/lucidadl && mkdir -p vendor/lucidadl
  curl -fsSL "$LUCIDADL_REPO/archive/$LUCIDADL_REV.tar.gz" | tar -xz -C vendor/lucidadl --strip-components 1
fi

step "Installing download engines (several minutes the first time)"
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install ./vendor/lucidadl 'tidalapi==0.8.11' 'yt-dlp==2026.8.19'
PLAYWRIGHT_BROWSERS_PATH="$DEST/.local/browsers" .venv/bin/python -m playwright install chromium

step "Creating $APP.app"
BUNDLE="$HOME/Applications/$APP.app"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cat > "$BUNDLE/Contents/MacOS/$APP" <<EOF
#!/bin/bash
cd "$DEST"
exec "$DEST/.venv/bin/python" "$DEST/gui.py"
EOF
chmod +x "$BUNDLE/Contents/MacOS/$APP"
ICONSET="$(mktemp -d)/fripper.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z $size $size assets/fripper-icon-1024.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2)); [ $double -le 512 ] && sips -z $double $double assets/fripper-icon-1024.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUNDLE/Contents/Resources/fripper.icns" || true
cat > "$BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$APP</string>
  <key>CFBundleDisplayName</key><string>$APP</string>
  <key>CFBundleIdentifier</key><string>app.fripper.desktop</string>
  <key>CFBundleExecutable</key><string>$APP</string>
  <key>CFBundleIconFile</key><string>fripper</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
touch "$BUNDLE"

trap - ERR
printf '\n\033[32m%s is ready.\033[0m\n' "$APP"
echo "It is in your Applications folder. To keep it in the Dock: open it, then right-click its Dock icon > Options > Keep in Dock."
echo "Note: TIDAL account sign-in is Windows-only for now. SoundCloud and Lucida sources work on Mac."
open "$BUNDLE"
