#!/bin/sh
# SimpleTskMgr installer – works on any Linux distro with Python 3.8+
#
#   curl -fsSL https://raw.githubusercontent.com/RealCat1st/simpletskmgr/main/install.sh | sh
#
# Installs:
#   simpletskmgr   the program
#   stm            short alias (only if no other "stm" command exists)
#   a start-menu entry that opens SimpleTskMgr in a terminal
#
# Options (pass after "sh -s --"):
#   --uninstall   remove SimpleTskMgr
# Run with sudo for a system-wide install (/usr/local).
set -eu

REPO="RealCat1st/simpletskmgr"
BRANCH="main"
BASE="${STM_BASE_URL:-https://raw.githubusercontent.com/$REPO/$BRANCH}"
NAME="simpletskmgr"
ALIAS="stm"

if [ "$(id -u)" -eq 0 ]; then
    PREFIX="/usr/local"
else
    PREFIX="$HOME/.local"
fi
BIN="$PREFIX/bin"
APPS="$PREFIX/share/applications"
ICONS="$PREFIX/share/icons/hicolor/scalable/apps"
ICONS_PNG="$PREFIX/share/icons/hicolor/256x256/apps"

info() { printf '\033[1m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31mError:\033[0m %s\n' "$1" >&2; exit 1; }

refresh_menu() {
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$APPS" 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true
    touch "$PREFIX/share/icons/hicolor" 2>/dev/null || true
    # KDE Plasma caches menu entries and icons
    if [ "$(id -u)" -ne 0 ]; then
        for k in kbuildsycoca6 kbuildsycoca5; do
            command -v "$k" >/dev/null 2>&1 && { "$k" >/dev/null 2>&1 || true; break; }
        done
    fi
}

if [ "${1:-}" = "--uninstall" ]; then
    for p in "$HOME/.local" /usr/local; do
        for f in "$p/bin/$NAME" "$p/share/applications/$NAME.desktop" \
                 "$p/share/icons/hicolor/scalable/apps/$NAME.svg" \
                 "$p/share/icons/hicolor/256x256/apps/$NAME.png"; do
            [ -e "$f" ] && rm -f "$f" && info "Removed $f"
        done
        # remove the alias only if it is ours
        if [ -L "$p/bin/$ALIAS" ] && [ "$(readlink "$p/bin/$ALIAS")" = "$p/bin/$NAME" ]; then
            rm -f "$p/bin/$ALIAS" && info "Removed $p/bin/$ALIAS"
        fi
    done
    exit 0
elif [ -n "${1:-}" ]; then
    fail "Unknown option: $1"
fi

[ "$(uname -s)" = "Linux" ] || fail "SimpleTskMgr only runs on Linux."

command -v python3 >/dev/null 2>&1 || fail "python3 not found. Install it first:
  Arch:          sudo pacman -S python
  Debian/Ubuntu: sudo apt install python3
  Fedora:        sudo dnf install python3
  openSUSE:      sudo zypper install python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
    || fail "Python 3.8 or newer is required."
python3 -c 'import curses' 2>/dev/null \
    || fail "Python curses module missing (e.g. openSUSE: sudo zypper install python3-curses)."

if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -qO "$2" "$1"; }
else
    fail "curl or wget is required."
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

info "Downloading SimpleTskMgr..."
fetch "$BASE/simpletskmgr.py" "$TMP/$NAME"
fetch "$BASE/data/simpletskmgr.desktop" "$TMP/$NAME.desktop"
fetch "$BASE/data/simpletskmgr.svg" "$TMP/$NAME.svg"
fetch "$BASE/data/simpletskmgr.png" "$TMP/$NAME.png"
python3 -m py_compile "$TMP/$NAME" 2>/dev/null || fail "Downloaded file is invalid."

mkdir -p "$BIN" "$APPS" "$ICONS" "$ICONS_PNG"
install -m 755 "$TMP/$NAME" "$BIN/$NAME"
install -m 644 "$TMP/$NAME.svg" "$ICONS/$NAME.svg"
install -m 644 "$TMP/$NAME.png" "$ICONS_PNG/$NAME.png"
# absolute Exec and Icon paths: work even if $BIN is not in PATH and
# without relying on the desktop's icon cache (KDE, GNOME, XFCE, ...)
sed -e "s|^Exec=.*|Exec=$BIN/$NAME|" -e "s|^Icon=.*|Icon=$ICONS_PNG/$NAME.png|" \
    "$TMP/$NAME.desktop" > "$APPS/$NAME.desktop"
chmod 644 "$APPS/$NAME.desktop"
refresh_menu
info "Installed $BIN/$NAME"
info "Added start-menu entry \"SimpleTskMgr\""

# short alias "stm" – only if not taken by something else
CMD="$NAME"
EXISTING="$(command -v "$ALIAS" 2>/dev/null || true)"
OURS=0
if [ -L "$BIN/$ALIAS" ] && [ "$(readlink "$BIN/$ALIAS")" = "$BIN/$NAME" ]; then
    OURS=1
fi
if [ "$OURS" -eq 1 ] || { [ -z "$EXISTING" ] && [ ! -e "$BIN/$ALIAS" ]; }; then
    ln -sf "$BIN/$NAME" "$BIN/$ALIAS"
    CMD="$ALIAS"
    info "Added short command: $ALIAS"
else
    warn "\"$ALIAS\" is already used by ${EXISTING:-$BIN/$ALIAS} – skipping alias, use \"$NAME\" instead."
fi

case ":$PATH:" in
    *":$BIN:"*) info "Run it with: $CMD   (or from your app menu)" ;;
    *) warn "$BIN is not in your PATH. Add it with:"
       echo "    echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
       info "Then run: $CMD   (the app-menu entry works already)" ;;
esac
