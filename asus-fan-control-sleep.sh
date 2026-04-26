#!/usr/bin/env bash
# Install to: /usr/lib/systemd/system-sleep/asus-fan-control
# Re-applies the fan curve after resume, since the EC often resets on wake.
# Author: ajirezk

set -euo pipefail

hwmon_ready() {
    local f
    for f in /sys/class/hwmon/hwmon*/name; do
        [[ -e "$f" ]] || continue
        if [[ "$(cat "$f" 2>/dev/null || true)" == "asus_custom_fan_curve" ]]; then
            return 0
        fi
    done
    return 1
}

case "${1:-}" in
    post)
        for _ in $(seq 1 20); do
            if hwmon_ready; then
                break
            fi
            sleep 0.25
        done

        if systemctl is-active --quiet asus-fan-control.service; then
            systemctl restart asus-fan-control.service || true
        fi
        ;;
esac

exit 0
