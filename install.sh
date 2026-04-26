#!/usr/bin/env bash
# Author: ajirezk
set -euo pipefail

if [ -t 1 ] && [ "${NO_COLOR:-}" = "" ]; then
    C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'
    C_M=$'\033[35m'; C_C=$'\033[36m'
    C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
    C_R=""; C_G=""; C_Y=""; C_M=""; C_C=""; C_BOLD=""; C_DIM=""; C_RST=""
fi

TOTAL=6
step()  { printf "\n%s[%d/%d]%s %s%s%s\n" "$C_C$C_BOLD" "$1" "$TOTAL" "$C_RST" "$C_BOLD" "$2" "$C_RST"; }
ok()    { printf "      %s✓%s %s\n" "$C_G"  "$C_RST" "$1"; }
warn()  { printf "      %s!%s %s\n" "$C_Y"  "$C_RST" "$1"; }
err()   { printf "      %s✗%s %s\n" "$C_R"  "$C_RST" "$1" >&2; }
hint()  { printf "        %s%s%s\n" "$C_DIM" "$1" "$C_RST"; }

banner() {
    printf "\n%s%s┌──────────────────────────────────────────────┐%s\n" "$C_M" "$C_BOLD" "$C_RST"
    printf "%s%s│%s   %sasus-fan-control%s — installer  by ajirezk   %s%s│%s\n" \
        "$C_M" "$C_BOLD" "$C_RST" "$C_BOLD" "$C_RST" "$C_M$C_BOLD" "" "$C_RST"
    printf "%s%s└──────────────────────────────────────────────┘%s\n" "$C_M" "$C_BOLD" "$C_RST"
}

fatal() {
    printf "\n%s%s ✗ Installation failed.%s %s\n\n" "$C_R" "$C_BOLD" "$C_RST" "$1" >&2
    exit 1
}
trap 'fatal "Unexpected error at line $LINENO. Run with bash -x ./install.sh for details."' ERR

banner

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    err "this installer must run as root"
    hint "it writes to /opt, /usr/local/bin, /etc/systemd/system, /usr/lib/systemd/system-sleep"
    hint "re-run:  sudo ./install.sh"
    exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_DIR="/opt/asus-fan-control"
BIN_PATH="/usr/local/bin/asusfan"

# ── step 1: backend detection ─────────────────────────────────────────────────
step 1 "Detecting fan-control backend"

BACKEND=""
BACKEND_PATH=""

for d in /sys/class/hwmon/hwmon*; do
    [ -f "$d/name" ] || continue
    if [ "$(cat "$d/name" 2>/dev/null)" = "asus_custom_fan_curve" ]; then
        BACKEND="curve"; BACKEND_PATH="$d"
        break
    fi
done

if [ -z "$BACKEND" ] \
   && [ -f /sys/firmware/acpi/platform_profile ] \
   && [ -f /sys/firmware/acpi/platform_profile_choices ]; then
    BACKEND="platform_profile"
    BACKEND_PATH="/sys/firmware/acpi/platform_profile"
fi

if [ -z "$BACKEND" ] && [ -f /sys/devices/platform/asus-nb-wmi/throttle_thermal_policy ]; then
    BACKEND="throttle"
    BACKEND_PATH="/sys/devices/platform/asus-nb-wmi/throttle_thermal_policy"
fi

case "$BACKEND" in
    curve)
        ok "asus_custom_fan_curve detected at $BACKEND_PATH"
        ok "→ TUI will run in continuous mode (full % slider, RPM, EC zones)"
        ;;
    platform_profile)
        ok "platform_profile detected at $BACKEND_PATH"
        ok "→ TUI will run in quantized mode (slider snaps to quiet/balanced/performance)"
        hint "for continuous % control, your kernel needs to expose 'asus_custom_fan_curve'"
        hint "(usually means upgrade to kernel 6.1+ with asus-armoury / asus-wmi)"
        ;;
    throttle)
        ok "throttle_thermal_policy detected at $BACKEND_PATH"
        ok "→ TUI will run in quantized mode (slider snaps to quiet/balanced/performance)"
        hint "this is the legacy ASUS interface; kernel 6.1+ may unlock the curve backend"
        ;;
    *)
        err "no supported ASUS fan-control interface found"
        hint "Tried (in order):"
        hint "  1. asus_custom_fan_curve hwmon  — not present"
        hint "  2. /sys/firmware/acpi/platform_profile  — not present"
        hint "  3. /sys/devices/platform/asus-nb-wmi/throttle_thermal_policy  — not present"
        hint ""
        hint "Make sure you are on kernel 5.15+ and an ASUS driver is loaded:"
        hint "  lsmod | grep -E 'asus_wmi|asus_nb_wmi|asus_armoury'"
        hint "If nothing matches, try:  sudo modprobe asus-nb-wmi"
        fatal "Unsupported hardware (no known ASUS fan interface)."
        ;;
esac

# ── step 2: dependencies ──────────────────────────────────────────────────────
step 2 "Checking dependencies"

if command -v systemctl >/dev/null 2>&1; then
    ok "systemd found"
else
    err "systemctl not found — this installer requires systemd"
    fatal "Non-systemd init not supported."
fi

if command -v python3 >/dev/null 2>&1; then
    ok "python3 found ($(python3 --version 2>&1))"
else
    err "python3 not found"
    hint "Arch:    sudo pacman -S python"
    hint "Debian:  sudo apt install python3"
    hint "Fedora:  sudo dnf install python3"
    fatal "Missing python3."
fi

# ── step 3: install python files ──────────────────────────────────────────────
step 3 "Installing Python sources to $APP_DIR"

install -d -m 0755 "$APP_DIR"
chown root:root "$APP_DIR"

# Python modules — readable by everyone (fan_hwmon.py is also executable as a
# CLI helper, so 0755).
for f in app_state.py fan_daemon.py fan_hwmon.py tui.py; do
    install -m 0644 -o root -g root "$SRC_DIR/$f" "$APP_DIR/$f"
done
chmod 0755 "$APP_DIR/fan_hwmon.py"
ok "installed Python sources (root-owned, world-readable)"

# Optional: plotext for in-TUI graphs
if python3 -c "import plotext" >/dev/null 2>&1; then
    ok "python module 'plotext' already available (TUI graphs enabled)"
elif command -v pip >/dev/null 2>&1; then
    if pip install --quiet --break-system-packages plotext >/dev/null 2>&1 \
       || pip install --quiet plotext >/dev/null 2>&1; then
        ok "installed python module 'plotext' (TUI graphs enabled)"
    else
        warn "could not install 'plotext' via pip — graphs will fall back to text"
    fi
else
    warn "pip not found — graphs will fall back to text"
fi

# ── step 4: launcher ──────────────────────────────────────────────────────────
step 4 "Installing 'asusfan' launcher"

sed "s|@APP_DIR@|$APP_DIR|g" "$SRC_DIR/asusfan.in" > "$BIN_PATH"
chown root:root "$BIN_PATH"
chmod 0755 "$BIN_PATH"
ok "installed $BIN_PATH (executable by all users)"

# ── step 5: systemd service + sleep hook + state dir ──────────────────────────
step 5 "Installing systemd service, sleep hook, and state dir"

sed "s|@APP_DIR@|$APP_DIR|g" "$SRC_DIR/asus-fan-control.service.in" \
    > /etc/systemd/system/asus-fan-control.service
chown root:root /etc/systemd/system/asus-fan-control.service
chmod 0644 /etc/systemd/system/asus-fan-control.service
ok "installed /etc/systemd/system/asus-fan-control.service"

install -d -m 0755 /usr/lib/systemd/system-sleep
install -m 0755 -o root -g root "$SRC_DIR/asus-fan-control-sleep.sh" \
    /usr/lib/systemd/system-sleep/asus-fan-control
ok "installed /usr/lib/systemd/system-sleep/asus-fan-control"

# State dir — used by daemon and quantized backend to persist saved profile
install -d -m 0755 -o root -g root /etc/asus-fan
ok "ensured /etc/asus-fan exists"

# ── step 6: enable and start ──────────────────────────────────────────────────
step 6 "Enabling and starting daemon"

systemctl daemon-reload
systemctl enable asus-fan-control.service >/dev/null 2>&1
systemctl restart asus-fan-control.service
ok "asus-fan-control.service is enabled and running"

trap - ERR

printf "\n%s%s ✓ Installation complete%s\n\n" "$C_G" "$C_BOLD" "$C_RST"

cat <<EOF
${C_BOLD}Quick start:${C_RST}
  ${C_C}asusfan${C_RST}                              open the TUI window (will sudo for you)
  ${C_C}asusfan info${C_RST}                         quick status without UI (no sudo)
  ${C_C}asusfan 50${C_RST}                           set both fans to 50%
  ${C_C}asusfan -f 20${C_RST}                        20%, bypass EC safety floor
  ${C_C}asusfan auto${C_RST}                         restore factory automatic mode
  ${C_C}asusfan --help${C_RST}                       full help

${C_BOLD}Daemon:${C_RST}
  ${C_C}sudo systemctl status asus-fan-control${C_RST}    health
  ${C_C}sudo journalctl -u asus-fan-control -f${C_RST}    live log

${C_BOLD}Uninstall:${C_RST}
  ${C_C}sudo ./uninstall.sh${C_RST}

EOF

if [ "$BACKEND" != "curve" ]; then
    printf "%s%sNote:%s This laptop only exposes %s — the slider in the\n" "$C_Y" "$C_BOLD" "$C_RST" "$BACKEND"
    printf "TUI works visually but actual fan speed snaps to one of 3 levels:\n"
    printf "  ${C_DIM}<30%% → quiet  ·  30-69%% → balanced  ·  ≥70%% → performance${C_RST}\n\n"
fi
