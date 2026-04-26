#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

HWMON_ROOT = Path("/sys/class/hwmon")
AUTO_MODES = {2, 3}
AUTO_MODE = 3
MANUAL_MODE = 1
PWM_MAX = 255
POINT_COUNT = 8
EC_ZONE_FLOORS_DEFAULT = (0, 15, 25, 40, 50, 65, 75, 90, 100)
EC_ZONE_FLOORS_CONSERVATIVE = (0, 20, 30, 45, 55, 70, 80, 95, 100)
EC_ZONE_FLOORS_AGGRESSIVE = (0, 10, 20, 30, 45, 60, 70, 85, 100)

DMI_PRODUCT_NAME = Path("/sys/class/dmi/id/product_name")
DMI_BOARD_NAME = Path("/sys/class/dmi/id/board_name")

_FLOOR_TABLE_BY_PREFIX: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("GA402", EC_ZONE_FLOORS_AGGRESSIVE),
    ("GA403", EC_ZONE_FLOORS_AGGRESSIVE),
    ("GA502", EC_ZONE_FLOORS_DEFAULT),
    ("GA503", EC_ZONE_FLOORS_DEFAULT),
    ("GU603", EC_ZONE_FLOORS_CONSERVATIVE),
    ("GU604", EC_ZONE_FLOORS_CONSERVATIVE),
    ("G513", EC_ZONE_FLOORS_DEFAULT),
    ("G733", EC_ZONE_FLOORS_CONSERVATIVE),
    ("FA506", EC_ZONE_FLOORS_DEFAULT),
    ("FA507", EC_ZONE_FLOORS_DEFAULT),
    ("FX507", EC_ZONE_FLOORS_DEFAULT),
    ("FX506", EC_ZONE_FLOORS_DEFAULT),
)


def _read_dmi_name() -> str:
    for path in (DMI_PRODUCT_NAME, DMI_BOARD_NAME):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            continue
        if value:
            return value
    return ""


def _resolve_zone_floors() -> tuple[int, ...]:
    dmi = _read_dmi_name().upper()
    for prefix, table in _FLOOR_TABLE_BY_PREFIX:
        if dmi.startswith(prefix) or prefix in dmi:
            return table
    return EC_ZONE_FLOORS_DEFAULT


EC_ZONE_FLOORS = _resolve_zone_floors()

CPU_SENSOR_CANDIDATES: tuple[tuple[str, str | None], ...] = (
    ("k10temp", "Tctl"),
    ("zenpower", "Tdie"),
    ("coretemp", "Package id 0"),
    ("coretemp", None),
    ("k10temp", None),
)
GPU_SENSOR_CANDIDATES: tuple[tuple[str, str | None], ...] = (
    ("amdgpu", "edge"),
    ("amdgpu", None),
    ("nvidia", None),
)

_LAYOUT_CACHE: "HwmonLayout | None" = None


class FanControlError(RuntimeError):
    pass


def invalidate_layout_cache() -> None:
    global _LAYOUT_CACHE
    _LAYOUT_CACHE = None


# ── multi-backend support ─────────────────────────────────────────────────────
# When the kernel exposes 'asus_custom_fan_curve' we get continuous PWM control
# (the original code path). On laptops that only expose 'platform_profile' or
# the legacy 'throttle_thermal_policy', we synthesize a compatible HwmonLayout
# so the TUI keeps working — quantized to 3 levels.

PROFILE_PATH = Path("/sys/firmware/acpi/platform_profile")
PROFILE_CHOICES_PATH = Path("/sys/firmware/acpi/platform_profile_choices")
THROTTLE_PATH = Path("/sys/devices/platform/asus-nb-wmi/throttle_thermal_policy")
QUANTIZED_STATE_FILE = Path("/etc/asus-fan/state.conf")

# Profile name → cosmetic percent (for the slider display in the TUI)
PROFILE_TO_PERCENT: dict[str, int] = {
    "low-power":   15,
    "quiet":       20,
    "balanced":    50,
    "cool":        50,
    "performance": 90,
}

# Synthetic temperature thresholds for the 8 EC zone points on quantized backend.
# These are not real EC values — TUI just renders zone bars; we fake them.
SYNTHETIC_TEMP_POINTS: tuple[int, ...] = (35, 45, 55, 65, 70, 80, 90, 95)


@dataclass(frozen=True)
class HwmonLayout:
    rpm_dir: Path | None
    curve_dir: Path | None
    cpu_temp_input: Path
    cpu_temp_label: str
    gpu_temp_input: Path | None
    gpu_temp_label: str | None
    backend: str = "curve"           # "curve" | "platform_profile" | "throttle"
    profile_path: Path | None = None
    profile_choices: tuple[str, ...] = ()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _write_text(path: Path, value: str) -> None:
    path.write_text(f"{value}\n", encoding="utf-8")


def _read_int(path: Path) -> int:
    return int(_read_text(path))


def _milli_c_to_c(value: int) -> float:
    return round(value / 1000.0, 1)


def _pwm_to_percent(pwm_value: int) -> int:
    return round((pwm_value / PWM_MAX) * 100)


def percent_to_pwm(percent: int) -> int:
    clamped = max(0, min(100, int(percent)))
    return round((clamped / 100) * PWM_MAX)


def _find_hwmon_by_name(target: str) -> Path:
    for candidate in sorted(HWMON_ROOT.glob("hwmon*")):
        try:
            resolved = candidate.resolve()
            name = _read_text(resolved / "name")
        except (FileNotFoundError, OSError):
            continue
        if name == target:
            return resolved
    raise FanControlError(f"hwmon node '{target}' was not found")


def _try_find_hwmon(target: str) -> Path | None:
    try:
        return _find_hwmon_by_name(target)
    except FanControlError:
        return None


def _detect_quantized_backend() -> tuple[str | None, Path | None, tuple[str, ...]]:
    """Return (backend_name, write_path, profile_choices) for quantized fallbacks."""
    if PROFILE_PATH.exists() and PROFILE_CHOICES_PATH.exists():
        try:
            choices = tuple(PROFILE_CHOICES_PATH.read_text().split())
        except OSError:
            choices = ("quiet", "balanced", "performance")
        return ("platform_profile", PROFILE_PATH, choices)
    if THROTTLE_PATH.exists():
        return ("throttle", THROTTLE_PATH, ("quiet", "balanced", "performance"))
    return (None, None, ())


def _percent_to_profile(pct: int, choices: tuple[str, ...]) -> str:
    if pct < 30:
        for c in ("quiet", "low-power"):
            if c in choices:
                return c
    elif pct >= 70:
        if "performance" in choices:
            return "performance"
    if "balanced" in choices:
        return "balanced"
    return choices[0] if choices else "balanced"


def _profile_to_percent(profile: str) -> int:
    return PROFILE_TO_PERCENT.get(profile, 50)


def _read_profile(layout: HwmonLayout) -> str:
    if layout.backend == "platform_profile" and layout.profile_path is not None:
        try:
            return layout.profile_path.read_text().strip()
        except OSError:
            return "balanced"
    if layout.backend == "throttle" and layout.profile_path is not None:
        try:
            n = int(layout.profile_path.read_text().strip())
        except (OSError, ValueError):
            return "balanced"
        return {0: "balanced", 1: "performance", 2: "quiet"}.get(n, "balanced")
    return "balanced"


def _write_profile(layout: HwmonLayout, profile: str) -> None:
    if layout.profile_path is None:
        raise FanControlError("no quantized profile path configured")
    if layout.backend == "platform_profile":
        _write_text(layout.profile_path, profile)
    elif layout.backend == "throttle":
        n = {"quiet": 2, "low-power": 2, "balanced": 0, "performance": 1}.get(profile, 0)
        _write_text(layout.profile_path, str(n))


def _save_quantized_state(profile: str, requested_percent: int | None = None) -> None:
    try:
        QUANTIZED_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"PROFILE={profile}"]
        if requested_percent is not None:
            lines.append(f"PERCENT={int(requested_percent)}")
        QUANTIZED_STATE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def _read_quantized_requested_percent() -> int | None:
    try:
        for line in QUANTIZED_STATE_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("PERCENT="):
                try:
                    return int(line.split("=", 1)[1].strip())
                except ValueError:
                    return None
    except (FileNotFoundError, OSError):
        return None
    return None


def _read_first_fan_rpm(idx: int) -> int:
    """Best-effort fan RPM read for quantized backend (no curve hwmon)."""
    asus_dir = _try_find_hwmon("asus")
    if asus_dir is None:
        return 0
    try:
        return _read_int(asus_dir / f"fan{idx}_input")
    except (FileNotFoundError, OSError, ValueError):
        return 0


def _find_temp_sensor(hwmon_name: str, preferred_label: str | None = None) -> tuple[Path, str]:
    hwmon_dir = _find_hwmon_by_name(hwmon_name)
    temp_inputs = sorted(hwmon_dir.glob("temp*_input"))
    if not temp_inputs:
        raise FanControlError(f"temperature sensor was not found in '{hwmon_name}'")

    if preferred_label:
        for temp_input in temp_inputs:
            label_path = temp_input.with_name(temp_input.name.replace("_input", "_label"))
            try:
                label = _read_text(label_path)
            except FileNotFoundError:
                continue
            if label == preferred_label:
                return temp_input, label

    chosen = temp_inputs[0]
    label_path = chosen.with_name(chosen.name.replace("_input", "_label"))
    label = _read_text(label_path) if label_path.exists() else chosen.stem.replace("_input", "")
    return chosen, label


def _find_first_sensor(
    candidates: tuple[tuple[str, str | None], ...],
) -> tuple[Path, str] | None:
    for hwmon_name, label in candidates:
        try:
            return _find_temp_sensor(hwmon_name, preferred_label=label)
        except FanControlError:
            continue
    return None


def discover_layout() -> HwmonLayout:
    global _LAYOUT_CACHE
    if _LAYOUT_CACHE is not None:
        # validate cache: curve backend needs both dirs; quantized only needs profile path
        valid = True
        if _LAYOUT_CACHE.backend == "curve":
            valid = (_LAYOUT_CACHE.curve_dir is not None
                     and _LAYOUT_CACHE.curve_dir.exists()
                     and _LAYOUT_CACHE.rpm_dir is not None
                     and _LAYOUT_CACHE.rpm_dir.exists())
        else:
            valid = (_LAYOUT_CACHE.profile_path is not None
                     and _LAYOUT_CACHE.profile_path.exists())
        if valid:
            return _LAYOUT_CACHE
        _LAYOUT_CACHE = None

    cpu_sensor = _find_first_sensor(CPU_SENSOR_CANDIDATES)
    if cpu_sensor is None:
        raise FanControlError(
            "no supported CPU temperature sensor found "
            f"(tried {[name for name, _ in CPU_SENSOR_CANDIDATES]})"
        )
    cpu_temp_input, cpu_temp_label = cpu_sensor

    gpu_sensor = _find_first_sensor(GPU_SENSOR_CANDIDATES)
    if gpu_sensor is None:
        gpu_temp_input = None
        gpu_temp_label = None
    else:
        gpu_temp_input, gpu_temp_label = gpu_sensor

    # Try the rich curve backend first.
    curve_dir = _try_find_hwmon("asus_custom_fan_curve")
    if curve_dir is not None:
        rpm_dir = _try_find_hwmon("asus")
        if rpm_dir is None:
            # curve exists but generic asus rpm hwmon missing — degrade gracefully
            rpm_dir = curve_dir
        _LAYOUT_CACHE = HwmonLayout(
            rpm_dir=rpm_dir,
            curve_dir=curve_dir,
            cpu_temp_input=cpu_temp_input,
            cpu_temp_label=cpu_temp_label,
            gpu_temp_input=gpu_temp_input,
            gpu_temp_label=gpu_temp_label,
            backend="curve",
        )
        return _LAYOUT_CACHE

    # Fall back to a quantized backend (platform_profile / throttle).
    backend, profile_path, profile_choices = _detect_quantized_backend()
    if backend is None:
        raise FanControlError(
            "no supported ASUS fan-control interface found "
            "(tried asus_custom_fan_curve, platform_profile, throttle_thermal_policy)"
        )

    rpm_dir = _try_find_hwmon("asus")  # may be None
    _LAYOUT_CACHE = HwmonLayout(
        rpm_dir=rpm_dir,
        curve_dir=None,
        cpu_temp_input=cpu_temp_input,
        cpu_temp_label=cpu_temp_label,
        gpu_temp_input=gpu_temp_input,
        gpu_temp_label=gpu_temp_label,
        backend=backend,
        profile_path=profile_path,
        profile_choices=profile_choices,
    )
    return _LAYOUT_CACHE


def _read_fan_percent(curve_dir: Path, fan_index: int) -> tuple[int, bool, int]:
    values = []
    for point in range(1, POINT_COUNT + 1):
        values.append(_read_int(curve_dir / f"pwm{fan_index}_auto_point{point}_pwm"))

    uniform = len(set(values)) == 1
    average_pwm = round(sum(values) / len(values))
    return _pwm_to_percent(average_pwm), uniform, average_pwm


def _read_temp_points(curve_dir: Path, fan_index: int) -> list[int]:
    return [_read_int(curve_dir / f"pwm{fan_index}_auto_point{point}_temp") for point in range(1, POINT_COUNT + 1)]


def _current_zone(temp_celsius: float, temp_points: list[int]) -> int:
    zone = 0
    for point in temp_points:
        if temp_celsius >= point:
            zone += 1
    return zone


def _zone_floor_percent(zone: int) -> int:
    return EC_ZONE_FLOORS[max(0, min(zone, len(EC_ZONE_FLOORS) - 1))]


def _compute_floor(layout: HwmonLayout, fan_index: int, temp_input: Path | None) -> int:
    if temp_input is None:
        return 0
    temp_c = _milli_c_to_c(_read_int(temp_input))
    temp_points = _read_temp_points(layout.curve_dir, fan_index)
    zone = _current_zone(temp_c, temp_points)
    return _zone_floor_percent(zone)


def read_status(layout: HwmonLayout | None = None) -> dict[str, object]:
    try:
        return _read_status_once(layout)
    except (FileNotFoundError, OSError):
        invalidate_layout_cache()
        return _read_status_once(None)


def _read_status_once(layout: HwmonLayout | None) -> dict[str, object]:
    if layout is None:
        layout = discover_layout()
    if layout.backend != "curve":
        return _read_status_quantized(layout)
    assert layout.curve_dir is not None
    assert layout.rpm_dir is not None
    cpu_percent, cpu_uniform, cpu_pwm = _read_fan_percent(layout.curve_dir, 1)
    gpu_percent, gpu_uniform, gpu_pwm = _read_fan_percent(layout.curve_dir, 2)
    cpu_temp_c = _milli_c_to_c(_read_int(layout.cpu_temp_input))
    gpu_temp_c = _milli_c_to_c(_read_int(layout.gpu_temp_input)) if layout.gpu_temp_input is not None else None
    cpu_temp_points = _read_temp_points(layout.curve_dir, 1)
    gpu_temp_points = _read_temp_points(layout.curve_dir, 2)

    cpu_zone = _current_zone(cpu_temp_c, cpu_temp_points)
    gpu_zone = _current_zone(gpu_temp_c, gpu_temp_points) if gpu_temp_c is not None else None
    cpu_floor_percent = _zone_floor_percent(cpu_zone)
    gpu_floor_percent = _zone_floor_percent(gpu_zone) if gpu_zone is not None else 0

    fan_modes = {
        "cpu": _read_int(layout.curve_dir / "pwm1_enable"),
        "gpu": _read_int(layout.curve_dir / "pwm2_enable"),
    }

    if fan_modes["cpu"] in AUTO_MODES and fan_modes["gpu"] in AUTO_MODES:
        mode = "auto"
    elif fan_modes["cpu"] == MANUAL_MODE and fan_modes["gpu"] == MANUAL_MODE:
        mode = "manual"
    else:
        mode = "mixed"

    cpu_label = _read_text(layout.rpm_dir / "fan1_label")
    gpu_label = _read_text(layout.rpm_dir / "fan2_label")

    status = {
        "mode": mode,
        "fans": {
            "cpu": {
                "label": cpu_label,
                "rpm": _read_int(layout.rpm_dir / "fan1_input"),
                "percent": cpu_percent,
                "pwm": cpu_pwm,
                "curve_uniform": cpu_uniform,
                "enable_mode": fan_modes["cpu"],
            },
            "gpu": {
                "label": gpu_label,
                "rpm": _read_int(layout.rpm_dir / "fan2_input"),
                "percent": gpu_percent,
                "pwm": gpu_pwm,
                "curve_uniform": gpu_uniform,
                "enable_mode": fan_modes["gpu"],
            },
        },
        "temperatures": {
            "cpu": {
                "label": layout.cpu_temp_label,
                "celsius": cpu_temp_c,
            }
        }
        ,
        "ec": {
            "cpu_temp_points": cpu_temp_points,
            "gpu_temp_points": gpu_temp_points,
            "cpu_zone": cpu_zone,
            "gpu_zone": gpu_zone,
            "cpu_floor_percent": cpu_floor_percent,
            "gpu_floor_percent": gpu_floor_percent,
            "master_floor_percent": max(cpu_floor_percent, gpu_floor_percent),
        },
        "hwmon": {
            "asus_rpm_dir": str(layout.rpm_dir),
            "asus_curve_dir": str(layout.curve_dir),
        },
    }

    if layout.gpu_temp_input is not None and layout.gpu_temp_label is not None:
        status["temperatures"]["gpu"] = {
            "label": layout.gpu_temp_label,
            "celsius": gpu_temp_c,
        }

    return status


def _require_root() -> None:
    if os.geteuid() != 0:
        raise FanControlError("writing fan control values requires root privileges")


def apply_flat_curve(cpu_percent: int, gpu_percent: int, enforce_floor: bool = True) -> dict[str, object]:
    _require_root()
    layout = discover_layout()
    cpu_percent = int(cpu_percent)
    gpu_percent = int(gpu_percent)

    if layout.backend != "curve":
        # Quantized backend: take the max and map to a profile.
        target_pct = max(cpu_percent, gpu_percent)
        profile = _percent_to_profile(target_pct, layout.profile_choices)
        try:
            _write_profile(layout, profile)
        except OSError as exc:
            raise FanControlError(f"failed to write profile '{profile}': {exc}") from exc
        # Save both the profile (what was actually applied) and the requested
        # percent (what the user/daemon asked for). read_status reports the
        # requested % so the daemon's status_matches_target() doesn't loop.
        _save_quantized_state(profile, requested_percent=target_pct)
        return read_status(layout)

    assert layout.curve_dir is not None

    if enforce_floor:
        cpu_percent = max(cpu_percent, _compute_floor(layout, 1, layout.cpu_temp_input))
        gpu_percent = max(gpu_percent, _compute_floor(layout, 2, layout.gpu_temp_input))

    try:
        for fan_index, percent in ((1, cpu_percent), (2, gpu_percent)):
            pwm_value = percent_to_pwm(percent)
            for point in range(1, POINT_COUNT + 1):
                _write_text(
                    layout.curve_dir / f"pwm{fan_index}_auto_point{point}_pwm",
                    str(pwm_value),
                )
            _write_text(layout.curve_dir / f"pwm{fan_index}_enable", str(MANUAL_MODE))
    except OSError as exc:
        for fan_index in (1, 2):
            try:
                _write_text(layout.curve_dir / f"pwm{fan_index}_enable", str(AUTO_MODE))
            except OSError:
                pass
        raise FanControlError(f"failed to apply fan curve, rolled back to auto: {exc}") from exc

    return read_status(layout)


def restore_auto_mode() -> dict[str, object]:
    _require_root()
    layout = discover_layout()
    if layout.backend != "curve":
        # Quantized backend: "auto" means firmware default = balanced.
        try:
            _write_profile(layout, "balanced")
        except OSError as exc:
            raise FanControlError(f"failed to restore balanced profile: {exc}") from exc
        # Remove saved-state so daemon stops re-applying anything.
        try:
            QUANTIZED_STATE_FILE.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return read_status()

    assert layout.curve_dir is not None
    _write_text(layout.curve_dir / "pwm1_enable", str(AUTO_MODE))
    _write_text(layout.curve_dir / "pwm2_enable", str(AUTO_MODE))
    return read_status()


def _read_status_quantized(layout: HwmonLayout) -> dict[str, object]:
    """Synthesize a status dict identical in shape to the curve backend."""
    profile = _read_profile(layout)
    # Prefer the user's requested %, fall back to the profile's cosmetic value.
    # This keeps the daemon's status_matches_target() check stable instead of
    # looping forever when the user asks for 80% (rounds to 'performance' = 90%).
    pct = _read_quantized_requested_percent()
    if pct is None:
        pct = _profile_to_percent(profile)
    pwm = percent_to_pwm(pct)

    cpu_temp_c = _milli_c_to_c(_read_int(layout.cpu_temp_input))
    gpu_temp_c = (_milli_c_to_c(_read_int(layout.gpu_temp_input))
                  if layout.gpu_temp_input is not None else None)

    rpm_cpu = _read_first_fan_rpm(1)
    rpm_gpu = _read_first_fan_rpm(2)

    cpu_temp_points = list(SYNTHETIC_TEMP_POINTS)
    gpu_temp_points = list(SYNTHETIC_TEMP_POINTS)
    cpu_zone = _current_zone(cpu_temp_c, cpu_temp_points)
    gpu_zone = _current_zone(gpu_temp_c, gpu_temp_points) if gpu_temp_c is not None else None
    cpu_floor_percent = _zone_floor_percent(cpu_zone)
    gpu_floor_percent = _zone_floor_percent(gpu_zone) if gpu_zone is not None else 0

    has_saved_state = QUANTIZED_STATE_FILE.exists()
    enable_mode = MANUAL_MODE if has_saved_state else AUTO_MODE
    mode = "manual" if has_saved_state else "auto"

    status: dict[str, object] = {
        "mode": mode,
        "fans": {
            "cpu": {
                "label": "CPU",
                "rpm": rpm_cpu,
                "percent": pct,
                "pwm": pwm,
                "curve_uniform": True,
                "enable_mode": enable_mode,
            },
            "gpu": {
                "label": "GPU",
                "rpm": rpm_gpu,
                "percent": pct,
                "pwm": pwm,
                "curve_uniform": True,
                "enable_mode": enable_mode,
            },
        },
        "temperatures": {
            "cpu": {
                "label": layout.cpu_temp_label,
                "celsius": cpu_temp_c,
            }
        },
        "ec": {
            "cpu_temp_points": cpu_temp_points,
            "gpu_temp_points": gpu_temp_points,
            "cpu_zone": cpu_zone,
            "gpu_zone": gpu_zone,
            "cpu_floor_percent": cpu_floor_percent,
            "gpu_floor_percent": gpu_floor_percent,
            "master_floor_percent": max(cpu_floor_percent, gpu_floor_percent),
        },
        "hwmon": {
            "asus_rpm_dir": str(layout.rpm_dir) if layout.rpm_dir else "",
            "asus_curve_dir": str(layout.profile_path) if layout.profile_path else "",
        },
        "backend": layout.backend,
        "profile": profile,
    }

    if layout.gpu_temp_input is not None and layout.gpu_temp_label is not None:
        status["temperatures"]["gpu"] = {
            "label": layout.gpu_temp_label,
            "celsius": gpu_temp_c,
        }

    return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ASUS fan control helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="print current temperatures and fan state as JSON")

    apply_parser = subparsers.add_parser("apply", help="set a flat manual fan curve")
    apply_parser.add_argument("--cpu", type=int, required=True, help="CPU fan target in percent")
    apply_parser.add_argument("--gpu", type=int, required=True, help="GPU fan target in percent")
    apply_parser.add_argument(
        "--unsafe-no-floor",
        action="store_true",
        help="disable the EC-derived software safety floor and write the requested percents directly",
    )

    subparsers.add_parser("auto", help="restore the factory automatic profile")
    return parser


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _persist_cli_state(*, mode: str, cpu: int = 55, gpu: int = 55, enforce_floor: bool = True) -> None:
    """When called from the CLI, mirror the change into the user-level settings
    so the systemd daemon (which periodically re-applies settings) doesn't
    immediately overwrite what the user just set from the terminal.
    """
    try:
        import app_state  # local import — only the CLI path needs it
    except ImportError:
        return
    try:
        split_control = (cpu != gpu)
        preset = app_state.preset_name_for_targets(cpu, gpu, split_control)
        update: dict = {
            "desired_mode": mode,
            "preset": preset,
            "cpu_target": int(cpu),
            "gpu_target": int(gpu),
            "split_control": split_control,
            "enforce_floor": bool(enforce_floor),
        }
        app_state.save_settings({**app_state.load_settings(), **update})
    except Exception:
        # Persistence is best-effort. If the daemon overwrites, at least the
        # hwmon write itself already happened.
        pass


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "status":
            _print_json(read_status())
            return 0
        if args.command == "apply":
            enforce_floor = not args.unsafe_no_floor
            result = apply_flat_curve(args.cpu, args.gpu, enforce_floor=enforce_floor)
            _persist_cli_state(
                mode="manual",
                cpu=int(args.cpu),
                gpu=int(args.gpu),
                enforce_floor=enforce_floor,
            )
            _print_json(result)
            return 0
        if args.command == "auto":
            result = restore_auto_mode()
            _persist_cli_state(mode="auto")
            _print_json(result)
            return 0
    except FanControlError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
        print(f"fan control operation failed: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
