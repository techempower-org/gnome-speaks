#!/bin/bash
# Build the extensions.gnome.org submission zip (official packer output).
# Only the extension ships to EGO — the companion service installs via
# install.sh; see README.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p dist
gnome-extensions pack . --force -o dist/
echo "→ dist/gnome-speaks@jphein.shell-extension.zip"
unzip -l dist/gnome-speaks@jphein.shell-extension.zip
