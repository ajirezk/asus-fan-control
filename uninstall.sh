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

ok()   { printf "  %s✓%s %s\n" "$C_G" "$C_RST" "$1"; }
skip() { printf "  %s·%s %s%s%s\n" "$C_DIM" "$C_RST" "$C_DIM" "$1" "$C_RST"; }

trap 'printf "\n%s%s ✗ Uninstall failed%s at line %d\n\n" "$C_R" "$C_BOLD" "$C_RST" "$LINENO" >&2; exit 1' ERR

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    printf "Run as root: %ssudo ./uninstall.sh%s\n" "$C_BOLD" "$C_RST" >&2
    exit 1
fi

printf "\n%s%s┌──────────────────────────────────────────────┐%s\n" "$C_M" "$C_BOLD" "$C_RST"
printf "%s%s│%s   %sasus-fan-control%s — uninstall  by ajirezk   %s%s│%s\n" \
    "$C_M" "$C_BOLD" "$C_RST" "$C_BOLD" "$C_RST" "$C_M$C_BOLD" "" "$C_RST"
printf "%s%s└──────────────────────────────────────────────┘%s\n\n" "$C_M" "$C_BOLD" "$C_RST"

# stop + disable service (works for both curve and simple installs)
if systemctl is-active --quiet asus-fan-control.service 2>/dev/null; then
    systemctl stop asus-fan-control.service || true
    ok "stopped asus-fan-control.service"
fi
if systemctl is-enabled --quiet asus-fan-control.service 2>/dev/null; then
    systemctl disable asus-fan-control.service >/dev/null 2>&1 || true
    ok "disabled asus-fan-control.service"
fi

removed=0

# common files (both install paths)
for f in \
    /usr/local/bin/asusfan \
    /etc/systemd/system/asus-fan-control.service \
    /usr/lib/systemd/system-sleep/asus-fan-control
do
    if [ -e "$f" ]; then rm -f "$f"; ok "removed $f"; removed=1
    else                 skip "not present: $f"; fi
done

# curve install: app dir
if [ -d /opt/asus-fan-control ]; then
    rm -rf /opt/asus-fan-control
    ok "removed /opt/asus-fan-control"
    removed=1
else
    skip "not present: /opt/asus-fan-control"
fi

# state dir — keep unless --purge
if [ "${1:-}" = "--purge" ]; then
    if [ -d /etc/asus-fan ]; then
        rm -rf /etc/asus-fan
        ok "removed /etc/asus-fan (--purge)"
        removed=1
    fi
else
    if [ -d /etc/asus-fan ]; then
        skip "kept /etc/asus-fan (run with --purge to remove saved state)"
    fi
fi

# Restore factory auto if curve hwmon is present
for d in /sys/class/hwmon/hwmon*; do
    [ -f "$d/name" ] || continue
    if [ "$(cat "$d/name" 2>/dev/null)" = "asus_custom_fan_curve" ]; then
        for fan in 1 2; do
            printf '3' > "$d/pwm${fan}_enable" 2>/dev/null || true
        done
        ok "restored factory automatic fan mode (pwm_enable=3)"
        break
    fi
done

# Also restore platform_profile to balanced (no-op if curve backend)
if [ -f /sys/firmware/acpi/platform_profile ]; then
    printf 'balanced' > /sys/firmware/acpi/platform_profile 2>/dev/null || true
    ok "restored platform_profile = balanced"
fi

systemctl daemon-reload || true

trap - ERR

if [ "$removed" -eq 0 ]; then
    printf "\n%s! Nothing to remove%s — asus-fan-control was not installed.\n" "$C_Y$C_BOLD" "$C_RST"
    printf "  %s(Did you run %ssudo ./install.sh%s yet?)%s\n\n" "$C_DIM" "$C_C" "$C_DIM" "$C_RST"
else
    printf "\n%s%s ✓ Uninstalled%s\n\n" "$C_G" "$C_BOLD" "$C_RST"
fi
