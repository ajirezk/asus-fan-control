#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path

APP_SLUG = "asus-fan-control"
PRESETS: dict[str, dict[str, object]] = {
    "silent": {"label": "Silent", "cpu": 30, "gpu": 30},
    "balanced": {"label": "Balanced", "cpu": 55, "gpu": 55},
    "turbo": {"label": "Turbo", "cpu": 100, "gpu": 100},
}
THERMAL_PRESET_UP: tuple[tuple[float, str], ...] = (
    (80.0, "turbo"),
    (65.0, "balanced"),
    (-273.0, "silent"),
)
THERMAL_PRESET_DOWN: dict[str, float] = {
    "turbo": 75.0,
    "balanced": 60.0,
}
DEFAULT_SETTINGS: dict[str, object] = {
    "desired_mode": "manual",
    "preset": "balanced",
    "cpu_target": 55,
    "gpu_target": 55,
    "split_control": False,
    "enforce_floor": True,
    "hold_manual": True,
    "auto_temp_mode": False,
    "autostart": False,
    "battery_aware": False,
    "battery_preset": "silent",
    "ac_preset": "balanced",
}

POWER_SUPPLY_ROOT = Path("/sys/class/power_supply")


def on_ac_power() -> bool | None:
    if not POWER_SUPPLY_ROOT.exists():
        return None
    for supply in POWER_SUPPLY_ROOT.iterdir():
        try:
            supply_type = (supply / "type").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            continue
        if supply_type != "Mains":
            continue
        try:
            online = (supply / "online").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            continue
        return online == "1"
    return None


def _detect_active_user() -> str | None:
    loginctl = shutil.which("loginctl")
    if loginctl is None:
        return None
    try:
        result = subprocess.run(
            [loginctl, "list-sessions", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        username = parts[2]
        if username in ("root", ""):
            continue
        try:
            pwd.getpwnam(username)
        except KeyError:
            continue
        return username
    return None


def _pkexec_user() -> str | None:
    raw = os.environ.get("PKEXEC_UID")
    if not raw:
        return None
    try:
        return pwd.getpwuid(int(raw)).pw_name
    except (ValueError, KeyError):
        return None


def runtime_user() -> tuple[str, Path, int, int]:
    candidates = [
        os.environ.get("SUDO_USER"),
        _pkexec_user(),
        os.environ.get("USER") if os.environ.get("USER") != "root" else None,
    ]
    for candidate in candidates:
        if candidate and candidate != "root":
            try:
                user_info = pwd.getpwnam(candidate)
                return candidate, Path(user_info.pw_dir), user_info.pw_uid, user_info.pw_gid
            except KeyError:
                continue

    detected = _detect_active_user()
    if detected is not None:
        user_info = pwd.getpwnam(detected)
        return detected, Path(user_info.pw_dir), user_info.pw_uid, user_info.pw_gid

    fallback = pwd.getpwuid(os.getuid())
    return fallback.pw_name, Path(fallback.pw_dir), fallback.pw_uid, fallback.pw_gid


def config_dir() -> Path:
    _, home_dir, _, _ = runtime_user()
    return home_dir / ".config" / APP_SLUG


def config_path() -> Path:
    return config_dir() / "config.json"


def daemon_pid_path() -> Path:
    return config_dir() / "daemon.pid"


def daemon_log_path() -> Path:
    return config_dir() / "daemon.log"


def desktop_entry_path() -> Path:
    _, home_dir, _, _ = runtime_user()
    return home_dir / ".local" / "share" / "applications" / "asus-fan-control.desktop"


def autostart_path() -> Path:
    _, home_dir, _, _ = runtime_user()
    return home_dir / ".config" / "autostart" / "asus-fan-control.desktop"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _fix_ownership(path: Path) -> None:
    if os.geteuid() != 0:
        return
    _, _, uid, gid = runtime_user()
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
    except OSError:
        pass


def load_settings() -> dict[str, object]:
    settings = dict(DEFAULT_SETTINGS)
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    if isinstance(data, dict):
        settings.update(data)
    settings["autostart"] = autostart_enabled()
    return settings


def save_settings(settings: dict[str, object]) -> None:
    merged = dict(DEFAULT_SETTINGS)
    merged.update(settings)
    path = config_path()
    _ensure_parent(path)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _fix_ownership(path.parent)
    _fix_ownership(path)


def autostart_enabled() -> bool:
    return autostart_path().exists()


def set_autostart(enabled: bool) -> bool:
    source = desktop_entry_path()
    target = autostart_path()
    _ensure_parent(target)
    _fix_ownership(target.parent)

    if not enabled:
        if target.exists() or target.is_symlink():
            target.unlink()
        return False

    if not source.exists():
        raise FileNotFoundError(f"desktop entry not found: {source}")

    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        target.symlink_to(source)
    except OSError:
        shutil.copy2(source, target)
    if target.exists() and not target.is_symlink():
        _fix_ownership(target)
    return True


def preset_targets(name: str) -> tuple[int, int]:
    preset = PRESETS[name]
    return int(preset["cpu"]), int(preset["gpu"])


def preset_label(name: str) -> str:
    if name == "custom":
        return "Custom"
    return str(PRESETS[name]["label"])


def preset_name_for_targets(cpu_target: int, gpu_target: int, split_control: bool) -> str:
    if split_control:
        return "custom"
    for name in PRESETS:
        cpu_preset, gpu_preset = preset_targets(name)
        if cpu_target == cpu_preset and gpu_target == gpu_preset:
            return name
    return "custom"


def thermal_preset_for_status(status: dict[str, object], current_preset: str | None = None) -> str:
    cpu_temp = float(status["temperatures"]["cpu"]["celsius"])
    gpu_celsius = status["temperatures"].get("gpu", {}).get("celsius")
    gpu_temp = float(gpu_celsius) if gpu_celsius is not None else cpu_temp
    max_temp = max(cpu_temp, gpu_temp)

    if current_preset in THERMAL_PRESET_DOWN and max_temp >= THERMAL_PRESET_DOWN[current_preset]:
        upgraded = _preset_from_up_thresholds(max_temp)
        order = ["silent", "balanced", "turbo"]
        if order.index(upgraded) > order.index(current_preset):
            return upgraded
        return current_preset

    return _preset_from_up_thresholds(max_temp)


def _preset_from_up_thresholds(max_temp: float) -> str:
    for threshold, preset_name in THERMAL_PRESET_UP:
        if max_temp >= threshold:
            return preset_name
    return "silent"


def notify_user(title: str, body: str) -> None:
    notify_send = shutil.which("notify-send")
    if not notify_send:
        return

    if os.geteuid() == 0:
        username, _, uid, _ = runtime_user()
        display = os.environ.get("DISPLAY", ":0")
        runtime_dir = f"/run/user/{uid}"
        dbus_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
        command = [
            "runuser",
            "-u",
            username,
            "--",
            "env",
            f"DISPLAY={display}",
            f"XDG_RUNTIME_DIR={runtime_dir}",
            f"DBUS_SESSION_BUS_ADDRESS={dbus_address}",
            notify_send,
            title,
            body,
        ]
    else:
        command = [notify_send, title, body]

    subprocess.run(command, check=False)
