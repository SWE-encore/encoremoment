#!/usr/bin/env bash
# Add "Encore test bench" to the app menu (Ubuntu/GNOME and other freedesktop desktops).
# The shortcut runs scripts/encore-bench from this checkout, so keep the checkout where it is.
#
#   scripts/install-bench-shortcut.sh            add or update the shortcut
#   scripts/install-bench-shortcut.sh --remove   remove it
set -eu

here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
apps=${XDG_DATA_HOME:-$HOME/.local/share}/applications
file=$apps/encore-bench.desktop

if [ "${1:-}" = "--remove" ]; then
    rm -f "$file"
    echo "Removed $file"
    exit 0
fi

case "$here" in
    *[\"\`\$\\]*) echo "This checkout's path contains \" \` \$ or \\; move it somewhere simpler." >&2; exit 1 ;;
esac

mkdir -p "$apps"
cat >"$file" <<EOF
[Desktop Entry]
Type=Application
Name=Encore test bench
Comment=Try Encore Moment reaction detection with the microphone or an audio file
Exec="$here/encore-bench"
Icon=audio-input-microphone
Terminal=false
Categories=AudioVideo;Audio;
StartupWMClass=encore-bench
EOF
chmod 644 "$file"
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$apps" 2>/dev/null || true
fi
echo "Added \"Encore test bench\" to the app menu ($file)."
echo "It runs $here/encore-bench. To pin it, right-click it in the dock or app grid and choose Pin to Dash."
