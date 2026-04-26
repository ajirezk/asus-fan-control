#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import app_state
import fan_hwmon

REFRESH_MIN_SECONDS = 2.0
REFRESH_MAX_SECONDS = 10.0
IDLE_TICKS_BEFORE_SLOWDOWN = 5
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3

STOP_EVENT = threading.Event()
_SETTINGS_CACHE: dict[str, object] | None = None
_SETTINGS_MTIME: float | None = None
_PID_LOCK_FD: int | None = None
logger = logging.getLogger("asusfan")


def clamp(value: int, lower: int = 0, upper: int = 100) -> int:
    return max(lower, min(upper, value))


def require_root() -> None:
    if os.geteuid() != 0:
        raise PermissionError("fan daemon requires root privileges")


def set_process_name(name: str) -> None:
    try:
        libc = ctypes.CDLL(None)
        libc.prctl(15, ctypes.c_char_p(name.encode("utf-8")), 0, 0, 0)
    except Exception:
        return


def pid_path() -> Path:
    return app_state.daemon_pid_path()


def log_path() -> Path:
    return app_state.daemon_log_path()


def read_pid() -> int | None:
    try:
        return int(pid_path().read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def current_daemon_pid() -> int | None:
    pid = read_pid()
    if pid is None:
        return None
    if pid_is_running(pid):
        return pid
    try:
        pid_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return None


def acquire_pid_lock() -> None:
    global _PID_LOCK_FD
    path = pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError("another daemon instance already holds the pid lock")
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
    os.fsync(fd)
    _PID_LOCK_FD = fd


def remove_pid_file() -> None:
    global _PID_LOCK_FD
    try:
        pid = read_pid()
        if pid == os.getpid():
            pid_path().unlink()
    except (FileNotFoundError, OSError):
        pass
    if _PID_LOCK_FD is not None:
        try:
            fcntl.flock(_PID_LOCK_FD, fcntl.LOCK_UN)
            os.close(_PID_LOCK_FD)
        except OSError:
            pass
        _PID_LOCK_FD = None


def status_matches_target(status: dict[str, object], cpu_target: int, gpu_target: int) -> bool:
    current_cpu_pwm = int(status["fans"]["cpu"]["pwm"])
    current_gpu_pwm = int(status["fans"]["gpu"]["pwm"])
    target_cpu_pwm = fan_hwmon.percent_to_pwm(cpu_target)
    target_gpu_pwm = fan_hwmon.percent_to_pwm(gpu_target)
    return (
        status["mode"] == "manual"
        and current_cpu_pwm == target_cpu_pwm
        and current_gpu_pwm == target_gpu_pwm
        and bool(status["fans"]["cpu"]["curve_uniform"])
        and bool(status["fans"]["gpu"]["curve_uniform"])
    )


def _load_settings_cached() -> dict[str, object]:
    global _SETTINGS_CACHE, _SETTINGS_MTIME
    path = app_state.config_path()
    try:
        mtime = path.stat().st_mtime
    except (FileNotFoundError, OSError):
        mtime = None

    if _SETTINGS_CACHE is None or mtime != _SETTINGS_MTIME:
        _SETTINGS_CACHE = app_state.load_settings()
        _SETTINGS_MTIME = mtime
    return dict(_SETTINGS_CACHE)


def _invalidate_settings_cache() -> None:
    global _SETTINGS_CACHE, _SETTINGS_MTIME
    _SETTINGS_CACHE = None
    _SETTINGS_MTIME = None


def load_runtime_settings() -> dict[str, object]:
    settings = _load_settings_cached()
    desired_mode = str(settings.get("desired_mode", "manual"))
    if desired_mode not in {"manual", "auto"}:
        desired_mode = "manual"
    preset = str(settings.get("preset", "balanced"))
    if preset not in app_state.PRESETS and preset != "custom":
        preset = "custom"

    return {
        "desired_mode": desired_mode,
        "preset": preset,
        "cpu_target": clamp(int(settings.get("cpu_target", 55))),
        "gpu_target": clamp(int(settings.get("gpu_target", 55))),
        "split_control": bool(settings.get("split_control", False)),
        "enforce_floor": bool(settings.get("enforce_floor", True)),
        "hold_manual": bool(settings.get("hold_manual", True)),
        "auto_temp_mode": bool(settings.get("auto_temp_mode", False)),
        "battery_aware": bool(settings.get("battery_aware", False)),
        "battery_preset": str(settings.get("battery_preset", "silent")),
        "ac_preset": str(settings.get("ac_preset", "balanced")),
    }


def store_settings(updated: dict[str, object]) -> None:
    settings = app_state.load_settings()
    settings.update(updated)
    app_state.save_settings(settings)
    _invalidate_settings_cache()


def signal_stop(signum: int, frame: object) -> None:  # noqa: ARG001
    STOP_EVENT.set()


def effective_targets(status: dict[str, object], runtime: dict[str, object]) -> tuple[dict[str, object], int, int, str]:
    preset = str(runtime["preset"])
    cpu_target = int(runtime["cpu_target"])
    gpu_target = int(runtime["gpu_target"])

    if bool(runtime["battery_aware"]) and str(runtime["desired_mode"]) != "auto":
        on_ac = app_state.on_ac_power()
        if on_ac is not None:
            target_preset = str(runtime["ac_preset"]) if on_ac else str(runtime["battery_preset"])
            if target_preset in app_state.PRESETS and target_preset != preset:
                cpu_target, gpu_target = app_state.preset_targets(target_preset)
                preset = target_preset
                runtime["preset"] = preset
                runtime["cpu_target"] = cpu_target
                runtime["gpu_target"] = gpu_target
                logger.info("battery-aware switch to preset=%s on_ac=%s", preset, on_ac)

    if bool(runtime["auto_temp_mode"]) and str(runtime["desired_mode"]) != "auto":
        desired_preset = app_state.thermal_preset_for_status(status, current_preset=preset)
        if desired_preset != preset:
            cpu_target, gpu_target = app_state.preset_targets(desired_preset)
            preset = desired_preset
            runtime["preset"] = preset
            runtime["cpu_target"] = cpu_target
            runtime["gpu_target"] = gpu_target
            store_settings(
                {
                    "desired_mode": "manual",
                    "preset": preset,
                    "cpu_target": cpu_target,
                    "gpu_target": gpu_target,
                }
            )
            app_state.notify_user("ASUS Fan Control", f"{app_state.preset_label(preset)} mode enabled")

    return runtime, cpu_target, gpu_target, preset


def _setup_logging() -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def run_loop() -> int:
    require_root()
    set_process_name("asusfan")
    _setup_logging()
    signal.signal(signal.SIGTERM, signal_stop)
    signal.signal(signal.SIGINT, signal_stop)
    try:
        acquire_pid_lock()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1
    logger.info("daemon started pid=%s", os.getpid())

    last_signature: tuple[object, ...] | None = None
    idle_ticks = 0
    error_streak = 0

    try:
        while not STOP_EVENT.is_set():
            action_taken = False
            try:
                runtime = load_runtime_settings()
                status = fan_hwmon.read_status()
                runtime, cpu_target, gpu_target, preset = effective_targets(status, runtime)
                signature = (
                    runtime["desired_mode"],
                    preset,
                    cpu_target,
                    gpu_target,
                    runtime["split_control"],
                    runtime["enforce_floor"],
                    runtime["hold_manual"],
                    runtime["auto_temp_mode"],
                )

                if str(runtime["desired_mode"]) == "auto":
                    if status["mode"] != "auto":
                        fan_hwmon.restore_auto_mode()
                        action_taken = True
                        logger.info("restored factory auto mode")
                else:
                    needs_apply = (
                        not status_matches_target(status, cpu_target, gpu_target)
                        if bool(runtime["hold_manual"])
                        else (signature != last_signature or status["mode"] != "manual")
                    )
                    if needs_apply:
                        fan_hwmon.apply_flat_curve(
                            cpu_target,
                            gpu_target,
                            enforce_floor=bool(runtime["enforce_floor"]),
                        )
                        action_taken = True
                        logger.info(
                            "applied curve preset=%s cpu=%s gpu=%s", preset, cpu_target, gpu_target
                        )

                if signature != last_signature:
                    action_taken = True
                last_signature = signature
                error_streak = 0
            except Exception as exc:  # noqa: BLE001
                error_streak += 1
                logger.error("loop iteration failed (%d in a row): %s", error_streak, exc)

            if action_taken:
                idle_ticks = 0
            else:
                idle_ticks += 1

            if error_streak > 0:
                delay = min(REFRESH_MAX_SECONDS, REFRESH_MIN_SECONDS * (2 ** min(error_streak, 4)))
            elif idle_ticks >= IDLE_TICKS_BEFORE_SLOWDOWN:
                delay = REFRESH_MAX_SECONDS
            else:
                delay = REFRESH_MIN_SECONDS

            STOP_EVENT.wait(delay)
    finally:
        logger.info("daemon stopping pid=%s", os.getpid())
        remove_pid_file()
    return 0


def start_daemon() -> int:
    require_root()
    running_pid = current_daemon_pid()
    if running_pid is not None:
        print(json.dumps({"running": True, "pid": running_pid}))
        return 0

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(  # noqa: S603
            [sys.executable, str(Path(__file__).resolve()), "run"],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            cwd=str(Path(__file__).resolve().parent),
            env=os.environ.copy(),
        )
    print(json.dumps({"running": True, "pid": process.pid}))
    return 0


def stop_daemon() -> int:
    require_root()
    running_pid = current_daemon_pid()
    if running_pid is None:
        print(json.dumps({"running": False, "stopped": True}))
        return 0
    os.kill(running_pid, signal.SIGTERM)
    for _ in range(20):
        if not pid_is_running(running_pid):
            remove_pid_file()
            print(json.dumps({"running": False, "stopped": True}))
            return 0
        time.sleep(0.1)
    print(json.dumps({"running": True, "pid": running_pid, "stopped": False}))
    return 1


def daemon_status() -> int:
    running_pid = current_daemon_pid()
    print(json.dumps({"running": running_pid is not None, "pid": running_pid}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ASUS fan background daemon")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="run the daemon in the foreground")
    subparsers.add_parser("start", help="start the daemon if it is not running")
    subparsers.add_parser("stop", help="stop the daemon")
    subparsers.add_parser("status", help="print daemon status as JSON")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "run":
        return run_loop()
    if args.command == "start":
        return start_daemon()
    if args.command == "stop":
        return stop_daemon()
    if args.command == "status":
        return daemon_status()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
