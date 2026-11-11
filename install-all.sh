#!/usr/bin/env bash
set -euo pipefail

DMG="${1:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ICP_DIR="$ROOT/iCloud-Keychain-for-Linux"

die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1;36m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }

step "Initializing submodules"
git -C "$ROOT" submodule update --init --recursive

if [[ -n "$DMG" ]]; then
  step "Installing Granola"
  "$ROOT/granola-linux.sh" "$DMG"
else
  info "Skipping Granola install (no .dmg path provided)."
  info "To install Granola too: $0 /path/to/Granola.dmg"
fi

step "Installing iCloud Keychain for Linux (icp)"
command -v python3 >/dev/null || die "'python3' not found. Please install it."
python3 -m venv "$ICP_DIR/.venv"
"$ICP_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
"$ICP_DIR/.venv/bin/pip" install -e "$ICP_DIR"

chmod +x "$ICP_DIR/host/icp-host.sh" "$ICP_DIR/host/install.sh"

step "Done"
printf '\n\033[32m✓ Both tools are installed.\033[0m\n\n'
printf 'Granola:\n'
if [[ -n "$DMG" ]]; then
  printf '  Installed from the provided .dmg (see output above).\n'
else
  printf '  Skipped. Run: ./granola-linux.sh "/path/to/Granola.dmg"\n'
fi
printf '\niCloud Keychain for Linux (icp):\n'
printf '  Activate: source %s/.venv/bin/activate\n' "$ICP_DIR"
printf '  Login:    export ICP_ANISETTE_URL="http://localhost:6969" && icp login\n'
printf '  Browser host: %s/host/install.sh [CHROME_EXTENSION_ID]\n\n' "$ICP_DIR"
