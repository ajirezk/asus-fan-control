# asus-fan-control

**Author:** ajirezk

Native terminal UI for **ASUS laptop fan control** on Linux. Runs as `asusfan`,
shows live CPU/GPU temperatures, fan RPM, slider for both fans, fan curves,
factory restore, and a systemd daemon that keeps your manual profile applied
across reboots and suspends.

```
asusfan                    # opens the TUI window
sudo systemctl status asus-fan-control     # daemon status
```

## Features

- Live overview at the top: CPU/GPU temps, fan RPMs, current PWM%, mode.
- Single big slider for both fans (CPU + GPU together).
- Optional split control via `Additional Options` (different CPU/GPU targets).
- Factory automatic mode restore (`R` key).
- EC-derived software safety floor — won't let you go dangerously low while a
  CPU/GPU is hot.
- Systemd daemon (`asus-fan-control.service`) re-applies your settings every
  few seconds while running and after suspend/resume.
- Memory-limited: 100 MB hard cap on the daemon and the TUI itself (set via
  `systemd-run --user`).

## Supported hardware

**One TUI for every supported ASUS laptop.** The Python core auto-detects
which kernel interface is available and adapts:

| Backend (auto-picked) | What you get |
|---|---|
| `asus_custom_fan_curve` hwmon | **Continuous PWM** — slider works pixel-by-pixel, all 8 EC zone points are real, RPMs from `asus` hwmon |
| `/sys/firmware/acpi/platform_profile` | **Quantized** — slider snaps to 3 levels: <30%=quiet, 30-69%=balanced, ≥70%=performance. Temperatures and live updates still work. |
| `/sys/devices/platform/asus-nb-wmi/throttle_thermal_policy` | Same as above, via the legacy ASUS WMI path |

The TUI looks **identical** in both cases — same layout, same Silent /
Balanced / Turbo / Auto presets, same temperature widgets, same hotkeys.
The only behavioural difference on quantized backends is that the slider
result is rounded to the nearest available profile when applied.

**Confirmed model families:**

- ASUS TUF Gaming **A15** (FA506, FA507) — usually quantized backend.
- ASUS TUF Gaming **A17** (FA706, FA707) — curve backend on kernel 6.1+.
- ASUS TUF Gaming **F15 / F17** — depends on kernel.
- ASUS ROG **Strix** G/SCAR series — usually curve.
- ASUS ROG **Zephyrus** G14 / G15 / M16 / Duo — curve on kernel 6.1+.
- ASUS Vivobook with manual fan support — quantized backend.
- ASUS ProArt Studiobook — curve.

**Requirements:**

- Linux kernel **5.15+** for any backend (6.1+ for the rich curve backend).
- `python3` (already on every modern Linux).
- `systemd` (for the daemon and sleep hook).
- Optional: `plotext` Python package for live graphs in the TUI; `install.sh`
  installs it via pip automatically. Without it, graphs fall back to a text
  summary — the rest of the TUI works.

**Not supported:**

- Non-ASUS laptops.
- Init systems other than systemd.
- Kernels older than 5.15 with no fan-control sysfs at all.

## Installation

```bash
git clone https://github.com/ajirezk/asus-fan-control.git
cd asus-fan-control
chmod +x install.sh uninstall.sh
sudo ./install.sh
```

> ⚠️ **`chmod +x` is required**, otherwise `Permission denied` and **the TUI
> will not be installed**.
>
> ⚠️ **`sudo` is required** — the installer writes to `/opt/`,
> `/usr/local/bin/`, `/etc/systemd/system/`, `/usr/lib/systemd/system-sleep/`,
> and enables a systemd service. Reading and writing to fan hwmon nodes also
> requires root.

What the installer does:

1. Verifies that `asus_custom_fan_curve` is present (hard requirement).
2. Verifies `python3` and `systemd` are available.
3. Installs Python sources to `/opt/asus-fan-control/`.
4. Installs the `asusfan` launcher to `/usr/local/bin/asusfan`.
5. Installs the systemd service `asus-fan-control.service` and sleep hook.
6. Enables and starts the daemon.

After install the `asusfan` command is available immediately (no reboot):

```bash
asusfan                              # show current state (mode, %, RPM, temps)
asusfan 50                           # set both fans to 50%
asusfan 100                          # max
asusfan 0                            # off (firmware may clamp)
asusfan auto                         # restore factory automatic mode

# advanced:
asusfan tui                          # open the curses TUI window
asusfan status                       # full JSON state (for scripts)
asusfan apply --cpu 80 --gpu 50      # asymmetric per-fan control
asusfan --help                       # full help
```

## Commands

| command | what it does |
|---|---|
| `asusfan` | pretty status: mode, %, RPM, temperatures (no sudo) |
| `asusfan <0-100>` | set both fans to N% (sudo prompts automatically) |
| `asusfan auto` | restore factory automatic mode |
| `asusfan status` | full JSON state — pipe to `jq` for scripts |
| `asusfan tui` | open the curses TUI window (slider, presets, options) |
| `asusfan apply --cpu N --gpu N` | asymmetric per-fan control |
| `asusfan apply --cpu N --gpu N --unsafe-no-floor` | same, disables EC safety floor |
| `asusfan --help` | full help |

Useful for hotkeys, conky widgets, sensor-driven scripts:

```bash
# bind to a hotkey
sudo asusfan 100

# loop while gaming
while pgrep -x steam >/dev/null; do sudo asusfan 80; sleep 30; done

# show in conky
asusfan status | jq -r '.fans.cpu.rpm' 
```

## Controls (TUI)

| key     | action |
|---------|--------|
| `Tab`   | switch to next control |
| `← / →` | change by 1 |
| `↑ / ↓` | change by 5 |
| `Space` | toggle a checkbox in `Additional Options` |
| `A`     | re-apply the current manual profile |
| `R`     | restore ASUS factory automatic mode |
| `S`     | sync target sliders from current state |
| `E`     | show / hide `Additional Options` |
| `Q`     | quit |

## Daemon

The systemd service `asus-fan-control.service` runs as root, watches the saved
profile, and keeps it applied. It survives logout and reboot.

```bash
sudo systemctl status asus-fan-control       # service health
sudo journalctl -u asus-fan-control -f       # live log
sudo systemctl restart asus-fan-control      # nudge it
```

## Persistence across suspend / hibernate

The sleep hook at `/usr/lib/systemd/system-sleep/asus-fan-control` waits for
the hwmon node to be ready after resume, then restarts the service so your
manual profile is re-applied. Many ASUS firmwares reset the curve on wake —
this hook covers that.

## Safety

- Values **below 20%** can cause overheating; the TUI shows a warning. Firmware
  may also clamp the actual minimum.
- The `EC zone floor` mechanism (in `fan_hwmon.py`) raises your effective fan
  speed automatically when CPU/GPU enters hotter zones, so even if you set a
  ridiculously low manual value the laptop won't melt under load. You can
  disable this floor in `Additional Options`, **at your own risk**.
- `0%` is accepted because the kernel allows it. Don't run sustained workloads
  at 0%.

## Uninstall

```bash
sudo ./uninstall.sh
```

This stops + disables the daemon, removes all installed files, and restores
factory automatic mode (`pwm_enable = 3`) on the way out, so your fans
immediately return to ASUS firmware control.

## Layout (flat, no subdirectories)

```
.
├── README.md
├── LICENSE
├── .gitignore
├── install.sh                  # 6-step coloured installer
├── uninstall.sh                # restores factory auto on the way out
├── asusfan.in                  # launcher template (@APP_DIR@ is templated)
├── asus-fan-control.service.in # systemd service template
├── asus-fan-control-sleep.sh   # /usr/lib/systemd/system-sleep/ hook
├── app_state.py                # config persistence, notifications
├── fan_daemon.py               # systemd service entry — applies + supervises
├── fan_hwmon.py                # hwmon discovery + multi-backend dispatch
└── tui.py                      # curses TUI (main UI)
```

`fan_hwmon.py` is what makes the same TUI work on every backend — it detects
which kernel interface is available, then either writes PWM curves directly
(curve backend) or maps percentages to firmware profiles (quantized).

`plotext` (used by the TUI for live graphs) is installed via pip by `install.sh`.
If it can't be installed, the TUI degrades to a text summary instead of a graph.

## Troubleshooting

**TUI exits immediately with `'asus_custom_fan_curve' hwmon node was not
found`** — your kernel doesn't have the writable curve. Update kernel
(`uname -r` should be 6.1+) and load the module:

```bash
sudo modprobe asus-armoury 2>/dev/null || sudo modprobe asus_wmi
```

**Daemon won't start** — check the journal:

```bash
sudo journalctl -u asus-fan-control -b 0 --no-pager
```

Common reasons: another tool (asusctl, nbfc) is fighting over the same hwmon —
disable it. Or hwmon disappears on suspend; the sleep hook should handle that
but it can race on slow machines (let me know on GitHub if it does).

**Conflicts with `asusctl`** — `asusctl` and this tool both want to control the
same fan curve. Pick one. To use this:
```bash
sudo systemctl stop asusd asusd-user 2>/dev/null
sudo systemctl disable asusd asusd-user 2>/dev/null
```

## License

MIT, © 2026 ajirezk. See `LICENSE`.
