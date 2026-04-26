#!/usr/bin/env python3
from __future__ import annotations

import curses
import os
import re
import sys
import time
from collections import deque
from pathlib import Path

try:
    import plotext as plt  # type: ignore
except ImportError:
    plt = None  # type: ignore

import app_state
import fan_hwmon

HISTORY_MAX_SAMPLES = 600
HISTORY_MIN_SAMPLES = 3
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _ansi_256_to_curses(color_index: int | None, theme: dict[str, int]) -> int:
    if color_index is None:
        return theme["accent"]
    if color_index in (2, 10, 40, 46, 82, 118):
        return theme["bar_fill"]
    if color_index in (1, 9, 124, 160, 196):
        return theme["danger"]
    if color_index in (3, 11, 178, 214, 220, 226):
        return theme["warning"]
    if color_index in (4, 12, 27, 33, 39, 45, 75):
        return theme["border"]
    if color_index in (5, 13, 135, 165, 201):
        return theme["accent"]
    if color_index in (6, 14, 51):
        return theme["accent"]
    return theme["muted"]


def _parse_ansi_line(line: str) -> list[tuple[str, int | None]]:
    result: list[tuple[str, int | None]] = []
    pos = 0
    current: int | None = None
    for match in _ANSI_SGR_RE.finditer(line):
        if match.start() > pos:
            result.append((line[pos:match.start()], current))
        codes_raw = match.group(1)
        codes = [int(c) for c in codes_raw.split(";") if c]
        if not codes or codes == [0]:
            current = None
        elif len(codes) >= 3 and codes[0] == 38 and codes[1] == 5:
            current = codes[2]
        pos = match.end()
    if pos < len(line):
        result.append((line[pos:], current))
    return result

REFRESH_SECONDS = 1.0
HOLD_REAPPLY_SECONDS = 1.0
STEP_SMALL = 1
STEP_LARGE = 5
MIN_WIDTH = 58
MIN_HEIGHT = 16
COMPACT_WIDTH = 84
COMPACT_HEIGHT = 30
FLASH_MESSAGE_SECONDS = 5.0


def preset_hotkey_hint() -> str:
    return "F8 = Silent   F9 = Balanced   F10 = Turbo"


def clamp(value: int, lower: int = 0, upper: int = 100) -> int:
    return max(lower, min(upper, value))


def fan_bar(percent: int, width: int = 30) -> str:
    filled = max(0, min(width, round((percent / 100) * width)))
    return f"{'█' * filled}{'░' * (width - filled)}  {percent:3d}%"


def fan_bar_colored(percent: int, width: int = 30) -> tuple[str, str, str]:
    filled = max(0, min(width, round((percent / 100) * width)))
    return "█" * filled, "░" * (width - filled), f"{percent:3d}%"


def safe_addstr(stdscr: curses.window, row: int, col: int, text: str, width: int, attr: int = curses.A_NORMAL) -> None:
    max_y, max_x = stdscr.getmaxyx()
    if row < 0 or row >= max_y or col < 0 or col >= max_x:
        return
    max_len = max(0, min(width, max_x) - col - 1)
    if max_len <= 0:
        return
    try:
        stdscr.addstr(row, col, text[:max_len], attr)
    except curses.error:
        return


def init_theme() -> dict[str, int]:
    theme = {
        "title": curses.A_BOLD,
        "logo": curses.A_BOLD,
        "accent": curses.A_BOLD,
        "border": curses.A_NORMAL,
        "focus": curses.A_REVERSE | curses.A_BOLD,
        "good": curses.A_BOLD,
        "warning": curses.A_BOLD,
        "danger": curses.A_BOLD,
        "muted": curses.A_DIM,
        "panel_title": curses.A_BOLD,
        "bar_fill": curses.A_BOLD,
        "bar_empty": curses.A_DIM,
        "preset_active": curses.A_REVERSE | curses.A_BOLD,
        "preset_inactive": curses.A_DIM,
        "check_on": curses.A_BOLD,
        "check_off": curses.A_DIM,
        "label": curses.A_NORMAL,
    }
    if not curses.has_colors():
        return theme

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_BLUE, -1)
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(4, curses.COLOR_GREEN, -1)
    curses.init_pair(5, curses.COLOR_YELLOW, -1)
    curses.init_pair(6, curses.COLOR_RED, -1)
    curses.init_pair(7, curses.COLOR_WHITE, -1)
    curses.init_pair(8, curses.COLOR_RED, -1)
    curses.init_pair(9, curses.COLOR_MAGENTA, -1)
    curses.init_pair(10, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    theme.update(
        {
            "title": curses.color_pair(1) | curses.A_BOLD,
            "logo": curses.color_pair(8) | curses.A_BOLD,
            "accent": curses.color_pair(1) | curses.A_BOLD,
            "border": curses.color_pair(2),
            "focus": curses.color_pair(3) | curses.A_BOLD,
            "good": curses.color_pair(4) | curses.A_BOLD,
            "warning": curses.color_pair(5) | curses.A_BOLD,
            "danger": curses.color_pair(6) | curses.A_BOLD,
            "muted": curses.color_pair(7) | curses.A_DIM,
            "panel_title": curses.color_pair(1) | curses.A_BOLD,
            "bar_fill": curses.color_pair(4) | curses.A_BOLD,
            "bar_empty": curses.color_pair(2) | curses.A_DIM,
            "preset_active": curses.color_pair(10) | curses.A_BOLD,
            "preset_inactive": curses.color_pair(7) | curses.A_DIM,
            "check_on": curses.color_pair(4) | curses.A_BOLD,
            "check_off": curses.color_pair(7) | curses.A_DIM,
            "label": curses.color_pair(7),
        }
    )
    return theme


def draw_panel(
    stdscr: curses.window,
    top: int,
    left: int,
    height: int,
    width: int,
    title: str,
    screen_width: int,
    theme: dict[str, int],
) -> None:
    max_y, max_x = stdscr.getmaxyx()
    if top < 0 or left < 0 or top >= max_y or left >= max_x:
        return
    height = min(height, max_y - top)
    width = min(width, max_x - left)
    if height < 3 or width < 8:
        return
    attr = theme["border"]
    try:
        stdscr.addch(top, left, curses.ACS_ULCORNER, attr)
        stdscr.hline(top, left + 1, curses.ACS_HLINE | attr, width - 2)
        stdscr.addch(top, left + width - 1, curses.ACS_URCORNER, attr)
        for row in range(top + 1, top + height - 1):
            stdscr.addch(row, left, curses.ACS_VLINE, attr)
            stdscr.addch(row, left + width - 1, curses.ACS_VLINE, attr)
        stdscr.addch(top + height - 1, left, curses.ACS_LLCORNER, attr)
        stdscr.hline(top + height - 1, left + 1, curses.ACS_HLINE | attr, width - 2)
        stdscr.addch(top + height - 1, left + width - 1, curses.ACS_LRCORNER, attr)
    except curses.error:
        return
    safe_addstr(stdscr, top, left + 2, f" {title} ", screen_width, theme["panel_title"])


def full_layout_min_height(advanced_open: bool, split_control: bool) -> int:
    additional_height = 18 if advanced_open and split_control else 15 if advanced_open else 5
    footer_height = 21
    row_after_overview = 1 + 13 + 1
    row_after_main = row_after_overview + 9 + 1
    row_after_hotkeys = row_after_main + 4 + 1
    row_after_additional = row_after_hotkeys + additional_height + 1
    return row_after_additional + footer_height


def temp_attr(theme: dict[str, int], value: float | None) -> int:
    if value is None:
        return theme["muted"]
    if value >= 85:
        return theme["danger"]
    if value >= 75:
        return theme["warning"]
    return theme["good"]


def mode_attr(theme: dict[str, int], mode: str) -> int:
    if mode == "auto":
        return theme["good"]
    if mode == "manual":
        return theme["accent"]
    return theme["warning"]


def render_logo() -> list[str]:
    return [
        "       /\\       ",
        "      /  \\      ",
        "     /\\   \\     ",
        "    /      \\    ",
        "   /   ,,   \\   ",
        "  /   |  |  -\\  ",
        " /_-''    ''-_\\ ",
    ]


def key_matches(key: int, *chars: str, extras: tuple[int, ...] = ()) -> bool:
    codes = {ord(char) for char in chars}
    codes.update(extras)
    return key in codes


def percent_from_hotkey(key: int) -> int | None:
    if ord("0") <= key <= ord("9"):
        return (key - ord("0")) * 10
    return None


def mode_label(status: dict) -> str:
    mapping = {"auto": "AUTO", "manual": "MANUAL", "mixed": "MIXED"}
    return mapping.get(status["mode"], str(status["mode"]).upper())


def preset_label(name: str) -> str:
    return app_state.preset_label(name).upper()


def preset_name_from_targets(cpu_target: int, gpu_target: int, split_control: bool) -> str:
    return app_state.preset_name_for_targets(cpu_target, gpu_target, split_control)


def idle_message(
    status: dict,
    preset_name: str,
    split_control: bool,
    enforce_floor: bool,
    hold_manual: bool,
    auto_temp_mode: bool,
) -> str:
    return (
        f"Current mode {preset_label(preset_name)} | ASUS {mode_label(status)}"
        f" | AutoTemp {'ON' if auto_temp_mode else 'OFF'}"
        f" | Split {'ON' if split_control else 'OFF'}"
        f" | Floor {'ON' if enforce_floor else 'OFF'}"
        f" | Hold {'ON' if hold_manual else 'OFF'}"
    )


def master_target(cpu_target: int, gpu_target: int) -> int:
    return round((cpu_target + gpu_target) / 2)


def status_matches_target(status: dict, cpu_target: int, gpu_target: int) -> bool:
    current_cpu = int(status["fans"]["cpu"]["percent"])
    current_gpu = int(status["fans"]["gpu"]["percent"])
    return (
        status["mode"] == "manual"
        and current_cpu == cpu_target
        and current_gpu == gpu_target
        and bool(status["fans"]["cpu"]["curve_uniform"])
        and bool(status["fans"]["gpu"]["curve_uniform"])
    )


def format_temp_points(points: list[int]) -> str:
    return "/".join(str(point) for point in points)


def detect_ec_clamp(status: dict, cpu_target: int, gpu_target: int) -> tuple[int, int, str] | None:
    if status["mode"] != "manual":
        return None

    actual_cpu = int(status["fans"]["cpu"]["percent"])
    actual_gpu = int(status["fans"]["gpu"]["percent"])
    if actual_cpu <= cpu_target and actual_gpu <= gpu_target:
        return None

    new_cpu = max(cpu_target, actual_cpu)
    new_gpu = max(gpu_target, actual_gpu)
    cpu_temp = status["temperatures"]["cpu"]["celsius"]
    gpu_temp = status["temperatures"].get("gpu", {}).get("celsius")
    cpu_zone = status["ec"]["cpu_zone"]
    gpu_zone = status["ec"]["gpu_zone"]
    cpu_points = format_temp_points(status["ec"]["cpu_temp_points"])
    gpu_points = format_temp_points(status["ec"]["gpu_temp_points"])
    message = (
        f"EC clamp active: CPU {cpu_temp:.1f}C zone {cpu_zone}/8 ({cpu_points})"
        f" | GPU {gpu_temp:.1f}C zone {gpu_zone}/8 ({gpu_points})"
        f" | floor -> CPU {new_cpu}% GPU {new_gpu}%."
    )
    return new_cpu, new_gpu, message


def protection_notice(status: dict, cpu_target: int, gpu_target: int) -> str | None:
    cpu_zone = status["ec"]["cpu_zone"]
    gpu_zone = status["ec"]["gpu_zone"] or 0
    cpu_rpm = int(status["fans"]["cpu"]["rpm"])
    gpu_rpm = int(status["fans"]["gpu"]["rpm"])
    if status["mode"] != "manual":
        return None
    if max(cpu_zone, gpu_zone) < 6:
        return None
    if max(cpu_target, gpu_target) > 10:
        return None
    if cpu_rpm < 2500 and gpu_rpm < 2500:
        return None
    return (
        f"EC protection active: requested CPU {cpu_target}% GPU {gpu_target}%,"
        f" but high temperature zones keep real RPM above the writable curve."
    )


def recommended_minimums(status: dict, split_control: bool) -> tuple[int, int]:
    cpu_floor = int(status["ec"]["cpu_floor_percent"])
    gpu_floor = int(status["ec"]["gpu_floor_percent"])
    if split_control:
        return cpu_floor, gpu_floor
    master_floor = int(status["ec"]["master_floor_percent"])
    return master_floor, master_floor


def unsafe_floor_notice(status: dict, cpu_target: int, gpu_target: int, split_control: bool) -> str | None:
    safe_cpu, safe_gpu = recommended_minimums(status, split_control)
    if cpu_target >= safe_cpu and gpu_target >= safe_gpu:
        return None
    return (
        "Unsafe mode: EC safety floor is OFF. "
        f"Recommended minimum is CPU {safe_cpu}% | GPU {safe_gpu}%, "
        f"but current request is CPU {cpu_target}% | GPU {gpu_target}%. "
        "Hardware may still clamp RPM, and too-low values can overheat or damage the system."
    )


def apply_software_floor(
    requested_cpu: int,
    requested_gpu: int,
    status: dict,
    split_control: bool,
    enforce_floor: bool,
) -> tuple[int, int, str | None]:
    if not enforce_floor:
        return requested_cpu, requested_gpu, None

    cpu_floor, gpu_floor = recommended_minimums(status, split_control)
    clamped_cpu = max(requested_cpu, cpu_floor)
    clamped_gpu = max(requested_gpu, gpu_floor)

    if (clamped_cpu, clamped_gpu) == (requested_cpu, requested_gpu):
        return clamped_cpu, clamped_gpu, None

    message = (
        f"Safety floor applied: CPU zone {status['ec']['cpu_zone']}/8 -> min {recommended_minimums(status, True)[0]}%"
        f" | GPU zone {status['ec']['gpu_zone']}/8 -> min {recommended_minimums(status, True)[1]}%"
        f" | requested CPU {requested_cpu}% GPU {requested_gpu}%"
        f" | using CPU {clamped_cpu}% GPU {clamped_gpu}%."
    )
    return clamped_cpu, clamped_gpu, message


def focus_order(advanced_open: bool, split_control: bool) -> list[str]:
    order = ["master"]
    if not advanced_open:
        return order
    order.extend(["split_toggle", "floor_toggle", "hold_toggle", "autostart_toggle", "auto_temp_toggle"])
    if split_control:
        order.extend(["cpu", "gpu"])
    return order


def draw_overview(
    stdscr: curses.window,
    width: int,
    status: dict,
    preset_name: str,
    auto_temp_mode: bool,
    autostart_enabled: bool,
    split_control: bool,
    enforce_floor: bool,
    hold_manual: bool,
    manual_lock_active: bool,
    theme: dict[str, int],
) -> int:
    cpu_temp = status["temperatures"]["cpu"]["celsius"]
    gpu_temp = status["temperatures"].get("gpu", {}).get("celsius")
    cpu_rpm = status["fans"]["cpu"]["rpm"]
    gpu_rpm = status["fans"]["gpu"]["rpm"]
    logo = render_logo()
    right_col = min(40, max(34, width // 2))
    top = 1
    panel_height = 13
    draw_panel(stdscr, top, 1, panel_height, width - 2, "Live Overview", width, theme)
    row = top + 1

    for offset, line in enumerate(logo):
        safe_addstr(stdscr, row + offset, 3, line, width, theme["logo"])

    safe_addstr(stdscr, row + 1, 24, "ASUS Fan Control", width, theme["title"])
    safe_addstr(stdscr, row + 2, 24, "fixed manual curve", width, theme["muted"])
    safe_addstr(stdscr, row + 3, 24, "live temps + RPM", width, theme["muted"])
    safe_addstr(stdscr, row + 4, 24, "terminal control", width, theme["muted"])

    safe_addstr(stdscr, row, right_col, f"Mode          : {mode_label(status)}", width, mode_attr(theme, status["mode"]))
    safe_addstr(stdscr, row + 1, right_col, f"CPU temp      : {cpu_temp:4.1f} C", width, temp_attr(theme, cpu_temp))
    safe_addstr(
        stdscr,
        row + 2,
        right_col,
        f"GPU temp      : {gpu_temp:4.1f} C" if gpu_temp is not None else "GPU temp      : n/a",
        width,
        temp_attr(theme, gpu_temp),
    )
    safe_addstr(stdscr, row + 3, right_col, f"CPU fan       : {cpu_rpm:5d} RPM", width, theme["accent"])
    safe_addstr(stdscr, row + 4, right_col, f"GPU fan       : {gpu_rpm:5d} RPM", width, theme["accent"])
    safe_addstr(stdscr, row + 5, right_col, f"Preset        : {preset_label(preset_name)}", width, theme["accent"])
    safe_addstr(
        stdscr,
        row + 6,
        right_col,
        f"Thermal auto  : {'ON' if auto_temp_mode else 'OFF'} | Autostart {'ON' if autostart_enabled else 'OFF'}",
        width,
        theme["good"] if auto_temp_mode else theme["muted"],
    )
    safe_addstr(
        stdscr,
        row + 7,
        right_col,
        f"Split {'ON' if split_control else 'OFF'} | Floor {'ON' if enforce_floor else 'OFF'} | Hold {'ON' if hold_manual else 'OFF'}",
        width,
        theme["danger"] if not enforce_floor else theme["muted"],
    )
    safe_addstr(
        stdscr,
        row + 8,
        right_col,
        f"Lock engaged  : {'YES' if manual_lock_active else 'NO'}",
        width,
        theme["good"] if manual_lock_active else theme["muted"],
    )
    safe_addstr(
        stdscr,
        row + 9,
        right_col,
        f"EC CPU zone   : {status['ec']['cpu_zone']}/8 | min {status['ec']['cpu_floor_percent']}%",
        width,
        theme["warning"] if status["ec"]["cpu_zone"] >= 6 else theme["muted"],
    )
    safe_addstr(
        stdscr,
        row + 10,
        right_col,
        f"EC GPU zone   : {status['ec']['gpu_zone']}/8 | min {status['ec']['gpu_floor_percent']}%" if status["ec"]["gpu_zone"] is not None else "EC GPU zone   : n/a",
        width,
        theme["warning"] if status["ec"]["gpu_zone"] is not None and status["ec"]["gpu_zone"] >= 6 else theme["muted"],
    )

    return top + panel_height + 1


def draw_main_controls(
    stdscr: curses.window,
    start_row: int,
    width: int,
    focus: str,
    preset_name: str,
    cpu_target: int,
    gpu_target: int,
    dirty: bool,
    split_control: bool,
    theme: dict[str, int],
) -> int:
    master = master_target(cpu_target, gpu_target)
    panel_height = 9
    draw_panel(stdscr, start_row, 1, panel_height, width - 2, "Main Control", width, theme)
    safe_addstr(stdscr, start_row + 1, 3, "Main Control", width, theme["panel_title"])
    safe_addstr(
        stdscr,
        start_row + 2,
        3,
        f"Current mode   {preset_label(preset_name)}",
        width,
        theme["good"] if preset_name != "custom" else theme["warning"],
    )

    master_line = f"Master target   {fan_bar(master)}"
    safe_addstr(
        stdscr,
        start_row + 4,
        3,
        master_line,
        width,
        theme["focus"] if focus == "master" else theme["accent"],
    )

    if split_control:
        split_note = f"Split values staged: CPU {cpu_target}% | GPU {gpu_target}%"
    else:
        split_note = f"Both fans staged together: CPU {cpu_target}% | GPU {gpu_target}%"
    safe_addstr(stdscr, start_row + 5, 3, split_note, width)
    safe_addstr(
        stdscr,
        start_row + 6,
        3,
        f"Pending changes : {'YES' if dirty else 'NO'}",
        width,
        theme["warning"] if dirty else theme["good"],
    )
    return start_row + panel_height + 1


def draw_additional_options(
    stdscr: curses.window,
    start_row: int,
    width: int,
    focus: str,
    advanced_open: bool,
    split_control: bool,
    enforce_floor: bool,
    hold_manual: bool,
    autostart_enabled: bool,
    auto_temp_mode: bool,
    cpu_target: int,
    gpu_target: int,
    theme: dict[str, int],
) -> int:
    header = "Additional Options"
    state = "expanded" if advanced_open else "collapsed"
    panel_height = 18 if advanced_open and split_control else 15 if advanced_open else 5
    draw_panel(stdscr, start_row, 1, panel_height, width - 2, f"{header} [{state}]", width, theme)
    safe_addstr(stdscr, start_row + 1, 3, f"{header} [{state}]  (press E)", width, theme["panel_title"])
    if not advanced_open:
        safe_addstr(
            stdscr,
            start_row + 2,
            3,
            "Press E to open extra controls for separate CPU/GPU fan targets and manual lock.",
            width,
            theme["muted"],
        )
        return start_row + panel_height + 1

    split_line = f"[{'x' if split_control else ' '}] Separate CPU/GPU control"
    floor_line = f"[{'x' if enforce_floor else ' '}] Enforce EC safety floor"
    hold_line = f"[{'x' if hold_manual else ' '}] Keep manual profile locked"
    autostart_line = f"[{'x' if autostart_enabled else ' '}] Start app on login"
    auto_temp_line = f"[{'x' if auto_temp_mode else ' '}] Auto switch presets by temperature"
    safe_addstr(
        stdscr,
        start_row + 2,
        3,
        split_line,
        width,
        theme["focus"] if focus == "split_toggle" else curses.A_NORMAL,
    )
    safe_addstr(
        stdscr,
        start_row + 3,
        3,
        floor_line,
        width,
        theme["focus"] if focus == "floor_toggle" else curses.A_NORMAL,
    )
    safe_addstr(
        stdscr,
        start_row + 4,
        3,
        hold_line,
        width,
        theme["focus"] if focus == "hold_toggle" else curses.A_NORMAL,
    )
    safe_addstr(
        stdscr,
        start_row + 5,
        3,
        autostart_line,
        width,
        theme["focus"] if focus == "autostart_toggle" else curses.A_NORMAL,
    )
    safe_addstr(
        stdscr,
        start_row + 6,
        3,
        auto_temp_line,
        width,
        theme["focus"] if focus == "auto_temp_toggle" else curses.A_NORMAL,
    )
    safe_addstr(stdscr, start_row + 7, 5, "Space toggles a checkbox.", width, theme["muted"])
    if enforce_floor:
        safe_addstr(
            stdscr,
            start_row + 8,
            5,
            "Safe mode: the app will not stage values below the EC-derived minimum.",
            width,
            theme["good"],
        )
    elif focus == "floor_toggle":
        safe_addstr(
            stdscr,
            start_row + 8,
            5,
            "Unsafe mode is enabled. Use with care.",
            width,
            theme["danger"],
        )

    if not split_control:
        safe_addstr(
            stdscr,
            start_row + 10,
            5,
            "Split control is off. Main slider keeps CPU and GPU identical.",
            width,
            theme["muted"],
        )
        safe_addstr(
            stdscr,
            start_row + 11,
            5,
            "Thermal auto uses max CPU/GPU temp: >=80C Turbo, >=65C Balanced, else Silent.",
            width,
            theme["muted"],
        )
        return start_row + panel_height + 1

    safe_addstr(
        stdscr,
        start_row + 10,
        5,
        "With split control on, CPU/GPU sliders can differ. Main slider still syncs both.",
        width,
        theme["muted"],
    )
    safe_addstr(
        stdscr,
        start_row + 11,
        5,
        "Thermal auto uses max CPU/GPU temp: >=80C Turbo, >=65C Balanced, else Silent.",
        width,
        theme["muted"],
    )
    cpu_line = f"CPU target      {fan_bar(cpu_target)}"
    gpu_line = f"GPU target      {fan_bar(gpu_target)}"
    safe_addstr(
        stdscr,
        start_row + 13,
        5,
        cpu_line,
        width,
        theme["focus"] if focus == "cpu" else theme["accent"],
    )
    safe_addstr(
        stdscr,
        start_row + 14,
        5,
        gpu_line,
        width,
        theme["focus"] if focus == "gpu" else theme["accent"],
    )
    return start_row + panel_height + 1


def draw_hotkeys(stdscr: curses.window, start_row: int, width: int, theme: dict[str, int]) -> int:
    panel_height = 4
    draw_panel(stdscr, start_row, 1, panel_height, width - 2, "Hotkeys", width, theme)
    safe_addstr(stdscr, start_row + 1, 3, "Mode presets", width, theme["panel_title"])
    safe_addstr(stdscr, start_row + 2, 5, "F8 = Silent | F9 = Balanced | F10 = Turbo", width, theme["accent"])
    return start_row + panel_height + 1


def draw_footer(stdscr: curses.window, start_row: int, width: int, message: str, theme: dict[str, int]) -> None:
    controls = [
        "Tab next control",
        "Left/Right change by 1",
        "Up/Down change by 5",
        "0..9 set 0..90 instantly",
        "End set 100 instantly",
        "Home set 0 instantly",
        "Space toggle checkbox",
        "A/Ф apply manual profile now",
        "R/К restore ASUS auto profile",
        "S/Ы sync staged values from current state",
        "E/У show or hide additional options",
        "Q/Й quit",
    ]
    panel_height = len(controls) + 10
    draw_panel(stdscr, start_row, 1, panel_height, width - 2, "Controls + Status", width, theme)
    safe_addstr(stdscr, start_row + 1, 3, "Controls", width, theme["panel_title"])
    for index, line in enumerate(controls, start=1):
        safe_addstr(stdscr, start_row + index + 1, 5, line, width)

    safe_addstr(stdscr, start_row + len(controls) + 4, 3, "Status", width, theme["panel_title"])
    safe_addstr(stdscr, start_row + len(controls) + 5, 5, message, width)
    safe_addstr(
        stdscr,
        start_row + len(controls) + 7,
        3,
        "Note: EC can still enforce a hardware floor. Safety floor OFF gives more control, not a promise of zero RPM.",
        width,
        theme["warning"],
    )


def _put(stdscr: curses.window, row: int, col: int, ch: str, attr: int) -> None:
    try:
        stdscr.addstr(row, col, ch, attr)
    except curses.error:
        pass


def _plot_series(
    stdscr: curses.window,
    top: int,
    left: int,
    height: int,
    width: int,
    samples: list[tuple[float, float | None]],
    y_min: float,
    y_max: float,
    attr_primary: int,
    attr_secondary: int,
) -> None:
    if len(samples) < 2 or height < 3 or width < 8:
        return
    plot_w = width
    plot_h = height
    span = max(1e-6, y_max - y_min)
    count = len(samples)

    def row_for(value: float) -> int:
        row = plot_h - 1 - int(round((value - y_min) / span * (plot_h - 1)))
        return max(0, min(plot_h - 1, row))

    cpu_rows: list[int] = []
    gpu_rows: list[int | None] = []
    for col in range(plot_w):
        frac = col / max(1, plot_w - 1)
        idx = min(count - 1, int(round(frac * (count - 1))))
        a, b = samples[idx]
        cpu_rows.append(row_for(a))
        gpu_rows.append(row_for(b) if b is not None else None)

    def draw_line(rows: list[int | None], attr: int, char: str) -> None:
        prev_row: int | None = None
        for col, row in enumerate(rows):
            if row is None:
                prev_row = None
                continue
            if prev_row is None:
                _put(stdscr, top + row, left + col, char, attr)
            else:
                lo, hi = (prev_row, row) if prev_row <= row else (row, prev_row)
                for r in range(lo, hi + 1):
                    _put(stdscr, top + r, left + col, char, attr)
            prev_row = row

    draw_line([r for r in cpu_rows], attr_primary, "●")
    if any(g is not None for g in gpu_rows):
        draw_line(gpu_rows, attr_secondary, "○")


def _build_plotext_graph(
    samples: list[tuple[float, float, float | None]],
    plot_width: int,
    plot_height: int,
) -> list[str]:
    if plt is None:
        return ["plotext module not available — graph disabled."]
    times = [s[0] for s in samples]
    cpu = [s[1] for s in samples]
    gpu_raw = [s[2] for s in samples]
    has_gpu = any(g is not None for g in gpu_raw)
    t0 = times[0]
    x_seconds = [t - t0 for t in times]
    span_s = max(1.0, x_seconds[-1] - x_seconds[0])
    if span_s >= 60:
        x = [s / 60.0 for s in x_seconds]
        xlabel = "minutes ago (0 = now)"
        x = [max(x) - v for v in x]
    else:
        x = [max(x_seconds) - v for v in x_seconds]
        xlabel = "seconds ago (0 = now)"

    plt.clear_figure()
    plt.plotsize(plot_width, plot_height)
    plt.theme("clear")
    plt.plot(x, cpu, label="CPU", marker="braille", color="green")
    if has_gpu:
        gpu = [g if g is not None else cpu[i] for i, g in enumerate(gpu_raw)]
        plt.plot(x, gpu, label="GPU", marker="braille", color="yellow")
    plt.xlabel(xlabel)
    plt.ylabel("°C")
    plt.grid(horizontal=True, vertical=False)
    plt.xaxes(True, False)
    plt.yaxes(True, False)

    raw = plt.build()
    lines = [line.rstrip() for line in raw.split("\n")]
    return lines


def draw_history_view(
    stdscr: curses.window,
    width: int,
    height: int,
    history: deque,
    theme: dict[str, int],
) -> None:
    draw_panel(stdscr, 0, 0, height, width, " Temperature · live (press H to return) ", width, theme)

    if plt is None:
        safe_addstr(
            stdscr, 2, 3,
            "plotext not installed — falling back to summary only.",
            width, theme["warning"],
        )

    if len(history) < HISTORY_MIN_SAMPLES:
        safe_addstr(
            stdscr, 2, 3,
            f"Collecting data... {len(history)}/{HISTORY_MIN_SAMPLES} samples",
            width, theme["muted"],
        )
        return

    samples = list(history)
    cpu_values = [s[1] for s in samples]
    gpu_values = [s[2] for s in samples if s[2] is not None]

    plot_top = 2
    plot_left = 2
    plot_width = max(30, width - 4)
    plot_height = max(10, height - 10)

    if plt is not None:
        lines = _build_plotext_graph(samples, plot_width, plot_height)
        for offset, line in enumerate(lines):
            if plot_top + offset >= height - 5:
                break
            segments = _parse_ansi_line(line)
            col_cursor = plot_left
            for text, color_index in segments:
                if not text:
                    continue
                attr = _ansi_256_to_curses(color_index, theme)
                safe_addstr(stdscr, plot_top + offset, col_cursor, text, width, attr)
                col_cursor += len(text)

    cpu_cur = cpu_values[-1]
    cpu_max = max(cpu_values)
    cpu_min = min(cpu_values)
    cpu_avg = sum(cpu_values) / len(cpu_values)
    stats_row = height - 5
    safe_addstr(
        stdscr, stats_row, 3,
        f"CPU   current {cpu_cur:5.1f}°C   max {cpu_max:5.1f}°C   min {cpu_min:5.1f}°C   avg {cpu_avg:5.1f}°C",
        width, theme["bar_fill"],
    )
    if gpu_values:
        gpu_cur = gpu_values[-1]
        gpu_max = max(gpu_values)
        gpu_min = min(gpu_values)
        gpu_avg = sum(gpu_values) / len(gpu_values)
        safe_addstr(
            stdscr, stats_row + 1, 3,
            f"GPU   current {gpu_cur:5.1f}°C   max {gpu_max:5.1f}°C   min {gpu_min:5.1f}°C   avg {gpu_avg:5.1f}°C",
            width, theme["warning"],
        )

    footer_row = height - 2
    safe_addstr(
        stdscr, footer_row, 3,
        f"samples: {len(samples)}  ·  H back  ·  Q quit",
        width, theme["muted"],
    )


def _draw_fan_row(
    stdscr: curses.window,
    row: int,
    col: int,
    label: str,
    percent: int,
    rpm: int,
    temp: float | None,
    bar_width: int,
    width: int,
    theme: dict[str, int],
    focused: bool,
) -> None:
    fill, empty, pct = fan_bar_colored(percent, bar_width)
    label_attr = theme["focus"] if focused else theme["accent"]
    safe_addstr(stdscr, row, col, f"{label:<4}", width, label_attr)
    safe_addstr(stdscr, row, col + 4, fill, width, theme["bar_fill"])
    safe_addstr(stdscr, row, col + 4 + len(fill), empty, width, theme["bar_empty"])
    tail_col = col + 4 + bar_width + 2
    safe_addstr(stdscr, row, tail_col, pct, width, theme["accent"])
    safe_addstr(stdscr, row, tail_col + 5, f" {rpm:>5} rpm", width, theme["label"])
    if temp is not None:
        safe_addstr(stdscr, row, tail_col + 17, f" {temp:4.1f}°C", width, temp_attr(theme, temp))


def _draw_preset_row(
    stdscr: curses.window,
    row: int,
    col: int,
    active_preset: str,
    width: int,
    theme: dict[str, int],
) -> None:
    items = [("Silent", "silent", "F8"), ("Balanced", "balanced", "F9"), ("Turbo", "turbo", "F10"), ("Auto", "auto", "R")]
    cell_width = 14
    for index, (label, name, hotkey) in enumerate(items):
        is_active = (name == active_preset) or (name == "auto" and active_preset == "auto")
        marker = "●" if is_active else "○"
        text = f" {marker} {label:<8}"
        x = col + index * cell_width
        attr = theme["preset_active"] if is_active else theme["preset_inactive"]
        safe_addstr(stdscr, row, x, text, width, attr)
        hotkey_x = x + (cell_width - len(hotkey)) // 2
        safe_addstr(stdscr, row + 1, hotkey_x, hotkey, width, theme["muted"])


def _draw_checkbox(
    stdscr: curses.window,
    row: int,
    col: int,
    checked: bool,
    label: str,
    width: int,
    theme: dict[str, int],
    focused: bool,
) -> None:
    mark = "☑" if checked else "☐"
    mark_attr = theme["check_on"] if checked else theme["check_off"]
    if focused:
        mark_attr = theme["focus"]
    safe_addstr(stdscr, row, col, mark, width, mark_attr)
    label_attr = theme["focus"] if focused else theme["label"]
    safe_addstr(stdscr, row, col + 2, label, width, label_attr)


def draw_compact_view(
    stdscr: curses.window,
    width: int,
    status: dict,
    focus: str,
    preset_name: str,
    cpu_target: int,
    gpu_target: int,
    advanced_open: bool,
    split_control: bool,
    enforce_floor: bool,
    hold_manual: bool,
    autostart_enabled: bool,
    auto_temp_mode: bool,
    manual_lock_active: bool,
    message: str,
    theme: dict[str, int],
) -> None:
    cpu_temp = status["temperatures"]["cpu"]["celsius"]
    gpu_temp = status["temperatures"].get("gpu", {}).get("celsius")
    cpu_rpm = status["fans"]["cpu"]["rpm"]
    gpu_rpm = status["fans"]["gpu"]["rpm"]
    logo = render_logo()
    show_logo = width >= 82
    logo_col = 2
    logo_width = 16
    content_left = logo_col + logo_width + 2 if show_logo else 3

    total_height = max(22, min(32, curses.LINES - 2))
    draw_panel(stdscr, 0, 0, total_height, width, f" ASUS Fan Control · {preset_label(preset_name)} ", width, theme)

    if show_logo:
        for offset, line in enumerate(logo):
            safe_addstr(stdscr, 3 + offset, logo_col, line, width, theme["logo"])

    safe_addstr(stdscr, 1, content_left, "ASUS Fan Control", width, theme["title"])
    header = (
        f"{mode_label(status)}  ·  AutoTemp {'ON' if auto_temp_mode else 'off'}"
        f"  ·  Autostart {'ON' if autostart_enabled else 'off'}"
        f"  ·  targets CPU {cpu_target}% GPU {gpu_target}%"
    )
    safe_addstr(stdscr, 2, content_left, header, width, theme["muted"])

    fans_top = 4
    draw_panel(stdscr, fans_top, content_left - 1, 4, width - content_left - 1, " Fans ", width, theme)
    bar_w = max(14, min(24, width - content_left - 34))
    _draw_fan_row(
        stdscr, fans_top + 1, content_left + 1, "CPU",
        int(status["fans"]["cpu"]["percent"]), cpu_rpm, cpu_temp,
        bar_w, width, theme, focused=(focus == "cpu"),
    )
    _draw_fan_row(
        stdscr, fans_top + 2, content_left + 1, "GPU",
        int(status["fans"]["gpu"]["percent"]), gpu_rpm, gpu_temp,
        bar_w, width, theme, focused=(focus == "gpu"),
    )

    preset_top = fans_top + 5
    draw_panel(stdscr, preset_top, content_left - 1, 5, width - content_left - 1, " Preset ", width, theme)
    active = "auto" if status["mode"] == "auto" else preset_name
    _draw_preset_row(stdscr, preset_top + 1, content_left + 1, active, width, theme)

    opts_top = preset_top + 6
    opts_height = 8 if advanced_open else 3
    title = " Options " + ("▾" if advanced_open else "▸")
    draw_panel(stdscr, opts_top, content_left - 1, opts_height, width - content_left - 1, title, width, theme)

    if advanced_open:
        checkboxes = [
            (split_control, "Separate CPU/GPU", "split_toggle"),
            (enforce_floor, "Enforce EC safety floor", "floor_toggle"),
            (hold_manual, "Keep manual profile locked", "hold_toggle"),
            (autostart_enabled, "Start app on login", "autostart_toggle"),
            (auto_temp_mode, "Auto-switch presets by temperature", "auto_temp_toggle"),
        ]
        for offset, (checked, label, key) in enumerate(checkboxes):
            _draw_checkbox(
                stdscr, opts_top + 1 + offset, content_left + 2,
                checked, label, width, theme, focused=(focus == key),
            )
    else:
        safe_addstr(
            stdscr, opts_top + 1, content_left + 2,
            "press E to expand advanced options",
            width, theme["muted"],
        )

    footer_top = opts_top + opts_height + 1
    safe_addstr(
        stdscr, footer_top, 3,
        "tab move  ·  ←→ adjust  ·  F8/F9/F10 preset  ·  R auto  ·  E options  ·  Q quit",
        width, theme["muted"],
    )
    is_idle = message.startswith("Current mode ")
    if message and not is_idle:
        safe_addstr(stdscr, footer_top + 1, 3, message, width, theme["warning"])


def apply_step(focus: str, step: int, cpu_target: int, gpu_target: int) -> tuple[int, int]:
    if focus == "master":
        value = clamp(master_target(cpu_target, gpu_target) + step)
        return value, value
    if focus == "cpu":
        return clamp(cpu_target + step), gpu_target
    if focus == "gpu":
        return cpu_target, clamp(gpu_target + step)
    return cpu_target, gpu_target


def apply_targets(cpu_target: int, gpu_target: int, enforce_floor: bool) -> tuple[dict, float]:
    status = fan_hwmon.apply_flat_curve(cpu_target, gpu_target, enforce_floor=enforce_floor)
    return status, time.monotonic()


def apply_live_targets(
    cpu_target: int,
    gpu_target: int,
    focus: str,
    status: dict,
    split_control: bool,
    enforce_floor: bool,
) -> tuple[dict, float, str, int, int]:
    cpu_target, gpu_target, floor_message = apply_software_floor(
        cpu_target,
        gpu_target,
        status,
        split_control,
        enforce_floor,
    )
    status, applied_at = apply_targets(cpu_target, gpu_target, enforce_floor)
    clamp_result = detect_ec_clamp(status, cpu_target, gpu_target)
    if clamp_result is not None:
        _, _, message = clamp_result
        return status, applied_at, f"{message} Requested CPU {cpu_target}% GPU {gpu_target}%.", cpu_target, gpu_target
    if floor_message is not None:
        return status, applied_at, floor_message, cpu_target, gpu_target
    unsafe_notice = unsafe_floor_notice(status, cpu_target, gpu_target, split_control)
    if unsafe_notice is not None:
        return status, applied_at, unsafe_notice, cpu_target, gpu_target
    if focus == "master":
        message = f"Main target applied immediately: both fans -> {cpu_target}%."
    elif min(cpu_target, gpu_target) < 20:
        message = f"Advanced target applied: CPU {cpu_target}% | GPU {gpu_target}%. Low values are risky."
    else:
        message = f"Advanced target applied: CPU {cpu_target}% | GPU {gpu_target}%."
    return status, applied_at, message, cpu_target, gpu_target


def cycle_focus(current: str, advanced_open: bool, split_control: bool) -> str:
    order = focus_order(advanced_open, split_control)
    index = order.index(current) if current in order else 0
    return order[(index + 1) % len(order)]


def sync_from_status(status: dict, split_control: bool, enforce_floor: bool) -> tuple[int, int]:
    cpu_value = int(status["fans"]["cpu"]["percent"])
    gpu_value = int(status["fans"]["gpu"]["percent"])
    if enforce_floor:
        safe_cpu, safe_gpu = recommended_minimums(status, True)
        cpu_value = max(cpu_value, safe_cpu)
        gpu_value = max(gpu_value, safe_gpu)
    if split_control:
        return cpu_value, gpu_value
    master_value = max(cpu_value, gpu_value) if enforce_floor else master_target(cpu_value, gpu_value)
    return master_value, master_value


def save_runtime_settings(
    desired_mode: str,
    preset_name: str,
    cpu_target: int,
    gpu_target: int,
    split_control: bool,
    enforce_floor: bool,
    hold_manual: bool,
    auto_temp_mode: bool,
    autostart_enabled: bool,
) -> None:
    app_state.save_settings(
        {
            "desired_mode": desired_mode,
            "preset": preset_name,
            "cpu_target": cpu_target,
            "gpu_target": gpu_target,
            "split_control": split_control,
            "enforce_floor": enforce_floor,
            "hold_manual": hold_manual,
            "auto_temp_mode": auto_temp_mode,
            "autostart": autostart_enabled,
        }
    )


def apply_named_preset(
    preset_name: str,
    focus: str,
    status: dict,
    split_control: bool,
    enforce_floor: bool,
) -> tuple[dict, float, str, int, int, str]:
    cpu_target, gpu_target = app_state.preset_targets(preset_name)
    status, applied_at, message, cpu_target, gpu_target = apply_live_targets(
        cpu_target,
        gpu_target,
        focus,
        status,
        split_control,
        enforce_floor,
    )
    return status, applied_at, message, cpu_target, gpu_target, preset_name


def run_app(stdscr: curses.window) -> None:
    import signal

    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(150)
    theme = init_theme()

    def _die(_signum, _frame):
        raise SystemExit(0)

    for _sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGPIPE):
        try:
            signal.signal(_sig, _die)
        except (OSError, ValueError):
            pass

    status = fan_hwmon.read_status()
    settings = app_state.load_settings()
    preset_name = str(settings.get("preset", "balanced"))
    if preset_name not in app_state.PRESETS and preset_name != "custom":
        preset_name = "balanced"
    split_control = bool(settings.get("split_control", False))
    enforce_floor = bool(settings.get("enforce_floor", True))
    hold_manual = bool(settings.get("hold_manual", True))
    autostart_enabled = bool(settings.get("autostart", False))
    auto_temp_mode = bool(settings.get("auto_temp_mode", False))
    desired_mode = str(settings.get("desired_mode", "manual"))
    if desired_mode not in {"manual", "auto"}:
        desired_mode = "manual"
    advanced_open = True
    history: deque = deque(maxlen=HISTORY_MAX_SAMPLES)
    history_view = False
    cpu_target = clamp(int(settings.get("cpu_target", 55)))
    gpu_target = clamp(int(settings.get("gpu_target", 55)))
    if not split_control and preset_name in app_state.PRESETS:
        cpu_target, gpu_target = app_state.preset_targets(preset_name)
    manual_lock_active = status["mode"] == "manual"
    focus = "master"
    dirty = False
    message = idle_message(status, preset_name, split_control, enforce_floor, hold_manual, auto_temp_mode)
    message_expires_at = 0.0
    last_refresh = 0.0
    last_hold_apply = 0.0

    def persist_current_state() -> None:
        current_preset = preset_name_from_targets(cpu_target, gpu_target, split_control)
        save_runtime_settings(
            desired_mode,
            current_preset,
            cpu_target,
            gpu_target,
            split_control,
            enforce_floor,
            hold_manual,
            auto_temp_mode,
            autostart_enabled,
        )

    def flash(text: str) -> None:
        nonlocal message, message_expires_at
        message = text
        message_expires_at = time.monotonic() + FLASH_MESSAGE_SECONDS

    def refresh_message(now: float) -> None:
        nonlocal message, message_expires_at
        if message_expires_at and now >= message_expires_at:
            message_expires_at = 0.0
        if not message_expires_at:
            message = idle_message(status, preset_name, split_control, enforce_floor, hold_manual, auto_temp_mode)

    while True:
        now = time.monotonic()

        if now - last_refresh >= REFRESH_SECONDS:
            try:
                status = fan_hwmon.read_status()
                last_refresh = now
                cpu_t = float(status["temperatures"]["cpu"]["celsius"])
                gpu_raw = status["temperatures"].get("gpu", {}).get("celsius")
                gpu_t = float(gpu_raw) if gpu_raw is not None else None
                history.append((now, cpu_t, gpu_t))
                autostart_enabled = app_state.autostart_enabled()
                if auto_temp_mode and not dirty:
                    desired_preset = app_state.thermal_preset_for_status(status)
                    if desired_preset != preset_name:
                        status, last_hold_apply, _, cpu_target, gpu_target, preset_name = apply_named_preset(
                            desired_preset,
                            focus,
                            status,
                            split_control,
                            enforce_floor,
                        )
                        manual_lock_active = True
                        desired_mode = "manual"
                        flash(
                            f"Thermal auto switched to {preset_label(preset_name)}"
                            f" at CPU {status['temperatures']['cpu']['celsius']:.1f}C"
                            f" / GPU {status['temperatures'].get('gpu', {}).get('celsius', status['temperatures']['cpu']['celsius']):.1f}C."
                        )
                        app_state.notify_user("ASUS Fan Control", f"{app_state.preset_label(preset_name)} mode enabled")
                        save_runtime_settings(
                            desired_mode,
                            preset_name,
                            cpu_target,
                            gpu_target,
                            split_control,
                            enforce_floor,
                            hold_manual,
                            auto_temp_mode,
                            autostart_enabled,
                        )
                if not dirty:
                    cpu_target, gpu_target = sync_from_status(status, split_control, enforce_floor)
                    preset_name = preset_name_from_targets(cpu_target, gpu_target, split_control)
            except Exception as exc:  # noqa: BLE001
                flash(f"Refresh failed: {exc}")

        clamp_result = detect_ec_clamp(status, cpu_target, gpu_target)
        clamp_active = clamp_result is not None
        if clamp_result is not None:
            new_cpu, new_gpu, clamp_message = clamp_result
            if (new_cpu, new_gpu) != (cpu_target, gpu_target):
                cpu_target, gpu_target = new_cpu, new_gpu
                dirty = False
                manual_lock_active = True
                flash(clamp_message)
                last_hold_apply = now

        notice = protection_notice(status, cpu_target, gpu_target)
        if notice is not None and not dirty and not enforce_floor:
            flash(notice)

        should_hold = hold_manual and manual_lock_active and not dirty and not clamp_active
        if should_hold and now - last_hold_apply >= HOLD_REAPPLY_SECONDS:
            try:
                corrected = not status_matches_target(status, cpu_target, gpu_target)
                fan_hwmon.apply_flat_curve(cpu_target, gpu_target, enforce_floor=enforce_floor)
                status = fan_hwmon.read_status()
                last_refresh = now
                last_hold_apply = now
                if corrected:
                    flash("Manual hold reapplied the flat curve to keep firmware from drifting.")
            except Exception as exc:  # noqa: BLE001
                flash(f"Hold failed: {exc}")

        refresh_message(now)

        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < MIN_HEIGHT or width < MIN_WIDTH:
            safe_addstr(stdscr, 1, 2, f"Window is too small. Resize it to at least {MIN_WIDTH}x{MIN_HEIGHT}.", width, curses.A_BOLD)
            safe_addstr(stdscr, 3, 2, "Compact mode will activate automatically once the window is large enough.", width)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                persist_current_state()
                return
            continue

        if history_view:
            draw_history_view(stdscr, width, height, history, theme)
            stdscr.refresh()
            key = stdscr.getch()
            if key == -1:
                continue
            if key_matches(key, "q", "Q", "й", "Й", extras=(27,)):
                persist_current_state()
                return
            if key_matches(key, "h", "H", "р", "Р"):
                history_view = False
            continue

        compact_mode = height < full_layout_min_height(advanced_open, split_control) or width < COMPACT_WIDTH
        if compact_mode:
            draw_compact_view(
                stdscr,
                width,
                status,
                focus,
                preset_name,
                cpu_target,
                gpu_target,
                advanced_open,
                split_control,
                enforce_floor,
                hold_manual,
                autostart_enabled,
                auto_temp_mode,
                manual_lock_active and hold_manual,
                message,
                theme,
            )
        else:
            row = draw_overview(
                stdscr,
                width,
                status,
                preset_name,
                auto_temp_mode,
                autostart_enabled,
                split_control,
                enforce_floor,
                hold_manual,
                manual_lock_active and hold_manual,
                theme,
            )
            row = draw_main_controls(stdscr, row, width, focus, preset_name, cpu_target, gpu_target, dirty, split_control, theme)
            row = draw_hotkeys(stdscr, row, width, theme)
            row = draw_additional_options(
                stdscr,
                row,
                width,
                focus,
                advanced_open,
                split_control,
                enforce_floor,
                hold_manual,
                autostart_enabled,
                auto_temp_mode,
                cpu_target,
                gpu_target,
                theme,
            )
            draw_footer(stdscr, row, width, message, theme)
        stdscr.refresh()

        key = stdscr.getch()
        if key == -1:
            continue

        if key_matches(key, "q", "Q", "й", "Й", extras=(27,)):
            persist_current_state()
            return
        if key_matches(key, "e", "E", "у", "У", extras=(curses.KEY_F2,)):
            advanced_open = not advanced_open
            focus = focus if focus in focus_order(advanced_open, split_control) else "master"
            flash(f"Additional options {'opened' if advanced_open else 'collapsed'}.")
            continue
        if key_matches(key, "h", "H", "р", "Р"):
            try:
                import subprocess
                graph_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fan_graph.py")
                env = os.environ.copy()
                sudo_user = env.get("SUDO_USER")
                if sudo_user and os.geteuid() == 0:
                    import pwd
                    pw = pwd.getpwnam(sudo_user)
                    uid = pw.pw_uid
                    env.setdefault("DISPLAY", ":0")
                    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
                    env.setdefault("HOME", pw.pw_dir)
                    xauth = os.path.join(pw.pw_dir, ".Xauthority")
                    if os.path.exists(xauth):
                        env.setdefault("XAUTHORITY", xauth)
                    subprocess.Popen(
                        ["runuser", "-u", sudo_user, "--", "python3", graph_script],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        env=env,
                    )
                else:
                    subprocess.Popen(
                        ["python3", graph_script],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        env=env,
                    )
                flash("Graph window opened.")
            except Exception as exc:  # noqa: BLE001
                flash(f"Graph failed: {exc}")
            continue
        if key_matches(key, extras=(curses.KEY_F8,)):
            try:
                status, last_hold_apply, _, cpu_target, gpu_target, preset_name = apply_named_preset(
                    "silent",
                    focus,
                    status,
                    split_control,
                    enforce_floor,
                )
                dirty = False
                manual_lock_active = True
                desired_mode = "manual"
                hold_manual = True
                flash(f"{preset_label(preset_name)} preset enabled.")
                app_state.notify_user("ASUS Fan Control", "Silent mode enabled")
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
            except Exception as exc:  # noqa: BLE001
                flash(f"Preset apply failed: {exc}")
            continue
        if key_matches(key, extras=(curses.KEY_F9,)):
            try:
                status, last_hold_apply, _, cpu_target, gpu_target, preset_name = apply_named_preset(
                    "balanced",
                    focus,
                    status,
                    split_control,
                    enforce_floor,
                )
                dirty = False
                manual_lock_active = True
                desired_mode = "manual"
                hold_manual = True
                flash(f"{preset_label(preset_name)} preset enabled.")
                app_state.notify_user("ASUS Fan Control", "Balanced mode enabled")
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
            except Exception as exc:  # noqa: BLE001
                flash(f"Preset apply failed: {exc}")
            continue
        if key_matches(key, extras=(curses.KEY_F10,)):
            try:
                status, last_hold_apply, _, cpu_target, gpu_target, preset_name = apply_named_preset(
                    "turbo",
                    focus,
                    status,
                    split_control,
                    enforce_floor,
                )
                dirty = False
                manual_lock_active = True
                desired_mode = "manual"
                hold_manual = True
                flash(f"{preset_label(preset_name)} preset enabled.")
                app_state.notify_user("ASUS Fan Control", "Turbo mode enabled")
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
            except Exception as exc:  # noqa: BLE001
                flash(f"Preset apply failed: {exc}")
            continue
        if key == 9:
            focus = cycle_focus(focus, advanced_open, split_control)
            continue
        if key in (ord(" "), 10, 13):
            if focus == "split_toggle":
                split_control = not split_control
                if not split_control:
                    merged = master_target(cpu_target, gpu_target)
                    cpu_target = merged
                    gpu_target = merged
                    if focus not in focus_order(advanced_open, split_control):
                        focus = "master"
                dirty = (
                    cpu_target != int(status["fans"]["cpu"]["percent"])
                    or gpu_target != int(status["fans"]["gpu"]["percent"])
                )
                preset_name = preset_name_from_targets(cpu_target, gpu_target, split_control)
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
                flash("Split control toggled.")
                continue
            if focus == "floor_toggle":
                enforce_floor = not enforce_floor
                if enforce_floor:
                    try:
                        status, last_hold_apply, message, cpu_target, gpu_target = apply_live_targets(
                            cpu_target,
                            gpu_target,
                            focus,
                            status,
                            split_control,
                            enforce_floor,
                        )
                        flash(message)
                        dirty = False
                        manual_lock_active = True
                        desired_mode = "manual"
                        preset_name = preset_name_from_targets(cpu_target, gpu_target, split_control)
                    except Exception as exc:  # noqa: BLE001
                        flash(f"Safety floor enable failed: {exc}")
                else:
                    cpu_target, gpu_target = sync_from_status(status, split_control, enforce_floor)
                    preset_name = preset_name_from_targets(cpu_target, gpu_target, split_control)
                    flash(
                        "EC safety floor disabled. Full manual control is unlocked. "
                        "Warning: too-low values can overheat or damage the system."
                    )
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
                continue
            if focus == "hold_toggle":
                hold_manual = not hold_manual
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
                flash("Manual hold toggled.")
                continue
            if focus == "autostart_toggle":
                try:
                    autostart_enabled = app_state.set_autostart(not autostart_enabled)
                    save_runtime_settings(
                        desired_mode,
                        preset_name,
                        cpu_target,
                        gpu_target,
                        split_control,
                        enforce_floor,
                        hold_manual,
                        auto_temp_mode,
                        autostart_enabled,
                    )
                    flash(f"App autostart {'enabled' if autostart_enabled else 'disabled'}.")
                except Exception as exc:  # noqa: BLE001
                    flash(f"Autostart toggle failed: {exc}")
                continue
            if focus == "auto_temp_toggle":
                auto_temp_mode = not auto_temp_mode
                if auto_temp_mode:
                    try:
                        desired_preset = app_state.thermal_preset_for_status(status)
                        status, last_hold_apply, _, cpu_target, gpu_target, preset_name = apply_named_preset(
                            desired_preset,
                            focus,
                            status,
                            split_control,
                            enforce_floor,
                        )
                        dirty = False
                        manual_lock_active = True
                        desired_mode = "manual"
                        flash(f"Thermal auto enabled. Active preset: {preset_label(preset_name)}.")
                    except Exception as exc:  # noqa: BLE001
                        auto_temp_mode = False
                        flash(f"Thermal auto enable failed: {exc}")
                else:
                    flash("Thermal auto disabled.")
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
                continue
        hot_percent = percent_from_hotkey(key)
        if hot_percent is not None:
            if focus == "gpu" and split_control:
                gpu_target = hot_percent
            elif focus == "cpu" and split_control:
                cpu_target = hot_percent
            else:
                cpu_target = hot_percent
                gpu_target = hot_percent
            try:
                status, last_hold_apply, message, cpu_target, gpu_target = apply_live_targets(
                    cpu_target,
                    gpu_target,
                    focus,
                    status,
                    split_control,
                    enforce_floor,
                )
                flash(message)
                dirty = False
                manual_lock_active = True
                desired_mode = "manual"
                preset_name = preset_name_from_targets(cpu_target, gpu_target, split_control)
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
            except Exception as exc:  # noqa: BLE001
                dirty = True
                flash(f"Live apply failed: {exc}")
            continue
        if key_matches(key, "s", "S", "ы", "Ы"):
            cpu_target, gpu_target = sync_from_status(status, split_control, enforce_floor)
            dirty = False
            manual_lock_active = status["mode"] == "manual"
            preset_name = preset_name_from_targets(cpu_target, gpu_target, split_control)
            save_runtime_settings(
                desired_mode,
                preset_name,
                cpu_target,
                gpu_target,
                split_control,
                enforce_floor,
                hold_manual,
                auto_temp_mode,
                autostart_enabled,
            )
            flash("Staged values synced from current fan state.")
            continue
        if key_matches(key, "a", "A", "ф", "Ф", extras=(curses.KEY_F6,)):
            try:
                status, last_hold_apply, message, cpu_target, gpu_target = apply_live_targets(
                    cpu_target,
                    gpu_target,
                    focus,
                    status,
                    split_control,
                    enforce_floor,
                )
                flash(message)
                dirty = False
                manual_lock_active = True
                desired_mode = "manual"
                preset_name = preset_name_from_targets(cpu_target, gpu_target, split_control)
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
            except Exception as exc:  # noqa: BLE001
                flash(f"Apply failed: {exc}")
            continue
        if key_matches(key, "r", "R", "к", "К", extras=(curses.KEY_F7,)):
            try:
                fan_hwmon.restore_auto_mode()
                status = fan_hwmon.read_status()
                cpu_target, gpu_target = sync_from_status(status, split_control, enforce_floor)
                dirty = False
                manual_lock_active = False
                desired_mode = "auto"
                auto_temp_mode = False
                preset_name = preset_name_from_targets(cpu_target, gpu_target, split_control)
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
                flash("ASUS automatic profile restored.")
            except Exception as exc:  # noqa: BLE001
                flash(f"Restore failed: {exc}")
            continue

        step = 0
        if key == curses.KEY_LEFT:
            step = -STEP_SMALL
        elif key == curses.KEY_RIGHT:
            step = STEP_SMALL
        elif key == curses.KEY_DOWN:
            step = -STEP_LARGE
        elif key == curses.KEY_UP:
            step = STEP_LARGE

        if key == curses.KEY_HOME:
            if focus == "gpu" and split_control:
                cpu_target, gpu_target = cpu_target, 0
            elif focus == "cpu" and split_control:
                cpu_target, gpu_target = 0, gpu_target
            else:
                cpu_target, gpu_target = 0, 0
            try:
                status, last_hold_apply, message, cpu_target, gpu_target = apply_live_targets(
                    cpu_target,
                    gpu_target,
                    focus,
                    status,
                    split_control,
                    enforce_floor,
                )
                flash(message)
                dirty = False
                manual_lock_active = True
                desired_mode = "manual"
                preset_name = preset_name_from_targets(cpu_target, gpu_target, split_control)
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
            except Exception as exc:  # noqa: BLE001
                dirty = True
                flash(f"Live apply failed: {exc}")
            continue

        if key == curses.KEY_END:
            if focus == "gpu" and split_control:
                cpu_target, gpu_target = cpu_target, 100
            elif focus == "cpu" and split_control:
                cpu_target, gpu_target = 100, gpu_target
            else:
                cpu_target, gpu_target = 100, 100
            try:
                status, last_hold_apply, message, cpu_target, gpu_target = apply_live_targets(
                    cpu_target,
                    gpu_target,
                    focus,
                    status,
                    split_control,
                    enforce_floor,
                )
                flash(message)
                dirty = False
                manual_lock_active = True
                desired_mode = "manual"
                preset_name = preset_name_from_targets(cpu_target, gpu_target, split_control)
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
            except Exception as exc:  # noqa: BLE001
                dirty = True
                flash(f"Live apply failed: {exc}")
            continue

        if step and focus in {"master", "cpu", "gpu"}:
            cpu_target, gpu_target = apply_step(focus, step, cpu_target, gpu_target)
            try:
                status, last_hold_apply, message, cpu_target, gpu_target = apply_live_targets(
                    cpu_target,
                    gpu_target,
                    focus,
                    status,
                    split_control,
                    enforce_floor,
                )
                flash(message)
                dirty = False
                manual_lock_active = True
                desired_mode = "manual"
                preset_name = preset_name_from_targets(cpu_target, gpu_target, split_control)
                save_runtime_settings(
                    desired_mode,
                    preset_name,
                    cpu_target,
                    gpu_target,
                    split_control,
                    enforce_floor,
                    hold_manual,
                    auto_temp_mode,
                    autostart_enabled,
                )
            except Exception as exc:  # noqa: BLE001
                dirty = True
                flash(f"Live apply failed: {exc}")


def main() -> int:
    if os.geteuid() != 0:
        print("Launch this TUI as root, or use the 'asusfan' command (installed by ./install.sh).")
        return 1

    try:
        curses.wrapper(run_app)
        return 0
    except fan_hwmon.FanControlError as exc:
        print(str(exc))
        return 1
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
