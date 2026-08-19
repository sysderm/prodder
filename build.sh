#!/bin/sh
# Build the native macOS menu-bar launcher (Prodder.app).
#
# The app bundle's source lives in the repo — macapp/Prodder.swift, plus
# Prodder.app/Contents/{Info.plist,Resources/prodder.icns}. Only the compiled
# binary is gitignored (it is arch-specific); this script regenerates it.
#
# Usage:  ./build.sh
set -e
cd "$(dirname "$0")"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "swiftc not found — install the Xcode command-line tools:" >&2
  echo "    xcode-select --install" >&2
  exit 1
fi

mkdir -p Prodder.app/Contents/MacOS
swiftc macapp/Prodder.swift -O \
  -o Prodder.app/Contents/MacOS/prodder \
  -framework Cocoa

# Refresh Launch Services so the Dock/Finder pick up the icon + Info.plist.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f Prodder.app 2>/dev/null || true

echo "Built Prodder.app — double-click it, or: open Prodder.app"
