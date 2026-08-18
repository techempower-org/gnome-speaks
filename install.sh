#!/usr/bin/env bash
# ============================================================
# GNOME Speaks — Installer / Uninstaller
# ============================================================
set -euo pipefail

# ---- Colours ------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Colour

# ---- Constants ----------------------------------------------
UUID="gnome-speaks@jphein"
INSTALL_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
PROJECT_DIR="$(dirname "$(realpath "$0")")"
DBUS_DIR="$HOME/.local/share/dbus-1/services"
DBUS_FILE="$DBUS_DIR/org.gnome.Speaks.service"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
SYSTEMD_SERVICE="gnome-speaks.service"
SYSTEMD_FILE="$SYSTEMD_USER_DIR/$SYSTEMD_SERVICE"

# ---- Helpers ------------------------------------------------
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERR]${NC}   $*"; }
step()    { echo -e "${CYAN}${BOLD}==>${NC} ${BOLD}$*${NC}"; }

# ============================================================
# UNINSTALL
# ============================================================
do_uninstall() {
    echo
    step "Uninstalling GNOME Speaks"
    echo

    # Stop and disable systemd user service
    info "Stopping systemd user service..."
    systemctl --user stop "$SYSTEMD_SERVICE" 2>/dev/null && \
        success "Service stopped." || \
        warn "Service was not running."

    info "Disabling systemd user service..."
    systemctl --user disable "$SYSTEMD_SERVICE" 2>/dev/null && \
        success "Service disabled." || \
        warn "Service was not enabled."

    # Remove systemd service file
    if [[ -f "$SYSTEMD_FILE" ]]; then
        info "Removing systemd service file..."
        rm -f "$SYSTEMD_FILE"
        success "Systemd service file removed."
    else
        warn "Systemd service file not found — nothing to remove."
    fi

    # Reload systemd daemon
    info "Reloading systemd user daemon..."
    systemctl --user daemon-reload 2>/dev/null && \
        success "Systemd daemon reloaded." || \
        warn "Could not reload systemd daemon."

    # Remove the DBus service file
    rm -f "$DBUS_DIR/org.gnome.Speaks.Speech.Provider.service"
    if [[ -f "$DBUS_FILE" ]]; then
        info "Removing DBus service file..."
        rm -f "$DBUS_FILE"
        success "DBus service file removed."
    else
        warn "DBus service file not found — nothing to remove."
    fi

    # Disable the extension (ignore errors if already disabled / not found)
    info "Disabling extension ${UUID}..."
    gnome-extensions disable "$UUID" 2>/dev/null && \
        success "Extension disabled." || \
        warn "Extension was not enabled or gnome-extensions not available."

    # Remove the extension directory
    if [[ -d "$INSTALL_DIR" ]]; then
        info "Removing $INSTALL_DIR..."
        rm -rf "$INSTALL_DIR"
        success "Extension files removed."
    else
        warn "Extension directory not found — nothing to remove."
    fi

    echo
    success "Uninstall complete."
    echo -e "  ${YELLOW}Restart GNOME Shell to finish clean-up:${NC}"
    echo -e "    X11  : ${BOLD}Alt+F2${NC}, type ${BOLD}r${NC}, press ${BOLD}Enter${NC}"
    echo -e "    Wayland: Log out and log back in"
    echo
    exit 0
}

# ============================================================
# Parse flags
# ============================================================
if [[ "${1:-}" == "--uninstall" || "${1:-}" == "-u" ]]; then
    do_uninstall
fi

# ============================================================
# INSTALL
# ============================================================
echo
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║       GNOME Speaks — Installer       ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════╝${NC}"
echo

# ---- 1. Verify required source files exist ------------------
step "Checking source files"

REQUIRED_FILES=("extension.js" "metadata.json" "stylesheet.css" "schemas/org.gnome.shell.extensions.gnome-speaks.gschema.xml")
MISSING=0
for f in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "$PROJECT_DIR/$f" ]]; then
        error "Missing: $PROJECT_DIR/$f"
        MISSING=1
    else
        success "Found $f"
    fi
done

if [[ "$MISSING" -eq 1 ]]; then
    echo
    error "Required source files are missing. Aborting."
    exit 1
fi
echo

# ---- 2. Create install directory ----------------------------
step "Installing extension files"

mkdir -p "$INSTALL_DIR"
info "Install directory: $INSTALL_DIR"

cp "$PROJECT_DIR/extension.js"  "$INSTALL_DIR/extension.js"
cp "$PROJECT_DIR/metadata.json" "$INSTALL_DIR/metadata.json"
cp "$PROJECT_DIR/stylesheet.css" "$INSTALL_DIR/stylesheet.css"
[[ -f "$PROJECT_DIR/prefs.js" ]] && cp "$PROJECT_DIR/prefs.js" "$INSTALL_DIR/prefs.js"
success "Copied extension.js, metadata.json, stylesheet.css, prefs.js"
echo

# ---- 3. Compile & install GSettings schemas -----------------
step "Compiling GSettings schemas"

mkdir -p "$INSTALL_DIR/schemas"
cp "$PROJECT_DIR/schemas/org.gnome.shell.extensions.gnome-speaks.gschema.xml" \
   "$INSTALL_DIR/schemas/"

if command -v glib-compile-schemas &>/dev/null; then
    glib-compile-schemas "$INSTALL_DIR/schemas/"
    success "Schemas compiled successfully."
else
    error "glib-compile-schemas not found. Install glib2 development tools."
    error "  Fedora/RHEL : sudo dnf install glib2-devel"
    error "  Debian/Ubuntu: sudo apt install libglib2.0-dev-bin"
    exit 1
fi
echo

# ---- 4. Install systemd user service ------------------------
step "Installing systemd user service"

SERVICE_EXEC="$PROJECT_DIR/gnome-speaks-service.py"

mkdir -p "$SYSTEMD_USER_DIR"

# Copy the service file and update ExecStart to use the actual project path
if [[ -f "$PROJECT_DIR/gnome-speaks.service" ]]; then
    sed "s|^ExecStart=.*|ExecStart=$SERVICE_EXEC|" \
        "$PROJECT_DIR/gnome-speaks.service" > "$SYSTEMD_FILE"
    success "Installed systemd service to $SYSTEMD_FILE"
else
    error "gnome-speaks.service not found in $PROJECT_DIR"
    error "Cannot install systemd service."
    exit 1
fi

# Reload systemd daemon to pick up the new service
info "Reloading systemd user daemon..."
systemctl --user daemon-reload && \
    success "Systemd daemon reloaded." || \
    warn "Could not reload systemd daemon."

# Enable the service
info "Enabling systemd user service..."
systemctl --user enable "$SYSTEMD_SERVICE" 2>/dev/null && \
    success "Service enabled." || \
    warn "Could not enable service."

# Start (or restart) the service
info "Starting systemd user service..."
systemctl --user restart "$SYSTEMD_SERVICE" 2>/dev/null && \
    success "Service started." || \
    warn "Could not start service. Check: systemctl --user status $SYSTEMD_SERVICE"
echo

# ---- 5. Install DBus service file (fallback activation) -----
step "Installing DBus service (fallback activation)"

mkdir -p "$DBUS_DIR"

cat > "$DBUS_FILE" <<DBUS_EOF
[D-BUS Service]
Name=org.gnome.Speaks
Exec=$SERVICE_EXEC
SystemdService=$SYSTEMD_SERVICE
DBUS_EOF

# Spiel speech provider activation (inert unless spiel_provider is enabled
# in config — the name is only owned by a running service when configured).
cat > "$DBUS_DIR/org.gnome.Speaks.Speech.Provider.service" <<DBUS_EOF
[D-BUS Service]
Name=org.gnome.Speaks.Speech.Provider
Exec=$SERVICE_EXEC
SystemdService=$SYSTEMD_SERVICE
DBUS_EOF

success "Created $DBUS_FILE"
echo

# ---- 6. Make the service script executable -------------------
step "Setting permissions"

if [[ -f "$SERVICE_EXEC" ]]; then
    chmod +x "$SERVICE_EXEC"
    success "gnome-speaks-service.py is now executable."
else
    warn "gnome-speaks-service.py not found at $SERVICE_EXEC"
    warn "Make sure to create it before using the extension."
fi
echo

# ---- 7. Python dependencies ---------------------------------
step "Checking Python dependencies"

# Check by IMPORT, not `pip show`: distro packages (python3-requests) and
# a distro python upgrade that orphans old C extensions both make pip's
# metadata lie in opposite directions. The import is the ground truth.
declare -A PIP_IMPORTS=(
    [requests]="requests" [webrtcvad]="webrtcvad"
    [websocket-client]="websocket"
    [numpy]="numpy")  # optional in audio.py but its absence is a silent slow path
NEED_INSTALL=()

for dep in "${!PIP_IMPORTS[@]}"; do
    if python3 -c "import ${PIP_IMPORTS[$dep]}" &>/dev/null; then
        success "$dep is importable."
    else
        warn "$dep is missing."
        NEED_INSTALL+=("$dep")
    fi
done

if [[ ${#NEED_INSTALL[@]} -gt 0 ]]; then
    info "Installing: ${NEED_INSTALL[*]}"
    # PEP 668 (Ubuntu 23.04+) blocks bare pip into the system python;
    # --break-system-packages scopes to --user, which is safe here.
    python3 -m pip install --user "${NEED_INSTALL[@]}" 2>/dev/null || \
        python3 -m pip install --user --break-system-packages "${NEED_INSTALL[@]}" && \
        success "Python dependencies installed." || \
        warn "pip install failed — try: sudo apt install python3-requests python3-websocket && pip install --user --break-system-packages webrtcvad"
fi
echo

# ---- 8. Enable the extension ---------------------------------
step "Enabling extension"

if command -v gnome-extensions &>/dev/null; then
    gnome-extensions enable "$UUID" 2>/dev/null && \
        success "Extension ${UUID} enabled." || \
        warn "Could not enable extension. You may need to restart GNOME Shell first."
else
    warn "gnome-extensions command not found. Enable manually in Extensions app."
fi

# ---- Done! ---------------------------------------------------
echo
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║       Installation complete!          ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════╝${NC}"
echo
echo -e "  ${BOLD}Restart GNOME Shell to load the extension:${NC}"
echo
echo -e "    ${CYAN}X11${NC}     : Press ${BOLD}Alt+F2${NC}, type ${BOLD}r${NC}, press ${BOLD}Enter${NC}"
echo -e "    ${CYAN}Wayland${NC} : Log out and log back in"
echo
echo -e "  ${BOLD}Service management:${NC}"
echo -e "    ${CYAN}Status${NC}  : systemctl --user status $SYSTEMD_SERVICE"
echo -e "    ${CYAN}Logs${NC}    : journalctl --user -u $SYSTEMD_SERVICE -f"
echo -e "    ${CYAN}Restart${NC} : systemctl --user restart $SYSTEMD_SERVICE"
echo
echo -e "  ${BOLD}To uninstall later:${NC}"
echo -e "    ${YELLOW}$0 --uninstall${NC}"
echo
