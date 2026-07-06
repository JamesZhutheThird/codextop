"""CodexTOP command-line entrypoint and terminal application loop."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import color_schemes
from core.constants import *
from .history import current_accounts, current_index, read_records_if_due
from .models import ClickZone, MonitorState
from .panels import *
from core.paths import ensure_runtime_layout
from .settings import handle_click, handle_setting_key, render_sidebar
from core.state import (
    parse_interval,
    read_codextop_state,
    read_sampler_interval,
    saved_color_scheme,
    saved_curve_mode,
    saved_display_scope,
    saved_interval,
    saved_period,
    saved_window_scope,
    save_codextop_state,
    send_sampler_interval,
)
from core.update_check import start_daily_update_check, update_check_path
from .terminal_io import TerminalSession, parse_input
from .terminal_text import fit_ansi, paint

def render_frame(state: MonitorState, term_width: int, term_height: int) -> tuple[list[str], list[ClickZone]]:
    zones: list[ClickZone] = []
    records = read_records_if_due(state)
    accounts = current_accounts(state, records)
    current = current_index(state, records)

    sidebar_width = 21
    main_width = max(40, term_width - sidebar_width)
    lines: list[str] = []

    content_height = max(6, term_height - 1)
    if not accounts:
        left_lines = [paint("正在加载 quota 数据...", "yellow")]
        left_lines.extend([""] * (content_height - 1))
        left_lines = [fit_ansi(line, main_width) for line in left_lines[:content_height]]
    elif state.display_scope == "current":
        active = selected_account(accounts, current)
        left_width = min(50, max(20, main_width // 3))
        right_width = max(12, main_width - left_width)
        left_width = main_width - right_width
        left_block = stacked_summary_panels(
            accounts,
            current,
            left_width,
            content_height,
            state.summary_offset,
            state.curve_mode,
            zones,
            1,
        )
        history_title = f"【{provider_name(active)}】额度历史"
        history_body = account_history_lines(
            active,
            records,
            right_width,
            content_height,
            state.period,
            state.curve_mode,
            state.window_scope,
        )
        right_block = panel(
            history_title,
            history_body,
            right_width,
            content_height,
            border_color(active, current),
        )
        left_lines = compose_columns([left_block, right_block], [left_width, right_width], content_height)
    elif state.display_scope == "merged":
        color = merged_border_color(accounts)
        if content_height >= 17:
            top_height = 9
        elif content_height >= 14:
            top_height = 8
        else:
            top_height = max(4, content_height // 2)
        history_height = max(4, content_height - top_height)
        top_left_width = min(50, max(37, main_width // 3))
        top_right_width = max(24, main_width - top_left_width)
        top_left_width = main_width - top_right_width
        top_left_block = panel(
            "账号信息",
            merged_account_reset_lines(accounts, current, top_left_width, top_height),
            top_left_width,
            top_height,
            color,
        )
        top_right_block = panel(
            "总额度",
            merged_quota_summary_lines(
                accounts,
                current,
                top_right_width,
                top_height,
                state.curve_mode,
            ),
            top_right_width,
            top_height,
            color,
        )
        top_lines = compose_columns(
            [top_left_block, top_right_block],
            [top_left_width, top_right_width],
            top_height,
        )
        history_block = panel(
            "合并额度历史",
            merged_history_lines(
                records,
                main_width,
                history_height,
                state.period,
                state.curve_mode,
                state.window_scope,
            ),
            main_width,
            history_height,
            color,
        )
        left_lines = top_lines + history_block
        if len(left_lines) < content_height:
            left_lines.extend([" " * main_width] * (content_height - len(left_lines)))
        left_lines = left_lines[:content_height]
    else:
        columns = min(3, len(accounts))
        row_count = max(1, (len(accounts) + columns - 1) // columns)
        base_row_height = max(8, content_height // row_count)
        left_lines = []
        for offset in range(0, len(accounts), columns):
            chunk = accounts[offset:offset + columns]
            row_height = base_row_height
            if offset + columns >= len(accounts):
                row_height = max(8, content_height - len(left_lines))
            panel_width = max(24, main_width // columns)
            widths = [panel_width] * columns
            widths[-1] += max(0, main_width - sum(widths))
            blocks = []
            for account, width in zip(chunk, widths):
                index = account_index(account)
                title = provider_name(account)
                if current is not None and index == current:
                    title = f"【{title}】"
                body = account_lines(
                    account,
                    records,
                    width,
                    row_height,
                    state.period,
                    current,
                    state.curve_mode,
                    state.window_scope,
                )
                blocks.append(panel(title, body, width, row_height, border_color(account, current)))
            while len(blocks) < columns:
                blocks.append([" " * panel_width for _ in range(row_height)])
            left_lines.extend(compose_columns(blocks, widths, row_height))
        if len(left_lines) < content_height:
            left_lines.extend([" " * main_width] * (content_height - len(left_lines)))
        left_lines = left_lines[:content_height]

    sidebar = render_sidebar(state, sidebar_width, content_height, main_width + 1, zones)
    for left, right in zip(left_lines, sidebar):
        lines.append(fit_ansi(left, main_width) + fit_ansi(right, sidebar_width))
    while len(lines) < term_height:
        lines.append(" " * term_width)
    return [fit_ansi(line, term_width) for line in lines[:term_height]], zones


def run_once(state: MonitorState) -> int:
    read_records_if_due(state, force=True)
    width, height = shutil.get_terminal_size((160, 48))
    lines, _zones = render_frame(state, width, height)
    print("\n".join(lines))
    return 0 if not state.error else 1


def run_update_after_tui() -> int:
    updater_path = Path(__file__).resolve().parents[1] / "core" / "updater.py"
    print("正在更新 CodexTOP...\n")
    proc = subprocess.run([sys.executable, str(updater_path)], check=False)
    if proc.returncode == 0:
        print("\n更新完成。请重新打开 CODEXTOP。")
    else:
        print("\n更新失败。请查看上面的命令行输出后重试。")
    return proc.returncode


def run_tui(state: MonitorState) -> int:
    start_daily_update_check(state)
    state.next_read = 0.0
    if state.interval != state.restore_interval or state.display_scope == "merged":
        try:
            send_sampler_interval(state, state.interval, sample_now=True, all_auth=True)
        except Exception as exc:
            state.status = "命令失败"
            state.error = str(exc)
    cleanup_errors: list[str] = []
    try:
        with TerminalSession() as session:
            running = True
            zones: list[ClickZone] = []
            while running:
                width, height = shutil.get_terminal_size((120, 36))
                lines, zones = render_frame(state, width, height)
                sys.stdout.write("\x1b[H" + "\n".join(lines))
                sys.stdout.flush()

                deadline = time.monotonic() + 0.2
                while time.monotonic() < deadline:
                    keep_running, clicks, keys = parse_input(session)
                    if not keep_running:
                        running = False
                        break
                    for key in keys:
                        running = handle_setting_key(state, key)
                        if not running:
                            break
                    if not running:
                        break
                    for x, y in clicks:
                        running = handle_click(state, zones, x, y)
                        if not running:
                            break
                    if not running:
                        break
                    time.sleep(0.03)
    finally:
        try:
            save_codextop_state(state)
        except Exception as exc:
            cleanup_errors.append(f"保存 CodexTOP 设置失败: {exc}")
        try:
            send_sampler_interval(state, state.restore_interval, sample_now=False)
        except Exception as exc:
            cleanup_errors.append(f"恢复 sampler 间隔失败: {exc}")
    if cleanup_errors:
        print("\n".join(cleanup_errors), file=sys.stderr)
        return 1
    if state.update_requested:
        return run_update_after_tui()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="全屏 Codex quota 监控。")
    parser.add_argument("-p", "--period", choices=PERIOD_CHOICES, default=None, help="初始历史长度。")
    parser.add_argument("-i", "--interval", type=parse_interval, default=None, help="初始读取/后台更新间隔，如 30s 或 2m。")
    parser.add_argument(
        "--curve-mode",
        choices=[value for _label, value in CURVE_MODE_CHOICES],
        default=None,
        help="历史曲线模式：connected 连续，box 线条，bar 柱状，braille 精细，points 间断。",
    )
    parser.add_argument(
        "--window-scope",
        choices=[value for _label, value in WINDOW_SCOPE_CHOICES],
        default=None,
        help="历史窗口：both 同时显示 5h/7d，5h 只显示 5h，7d 只显示 7d。",
    )
    parser.add_argument(
        "--color-scheme",
        choices=[value for _label, value in color_schemes.color_scheme_choices()],
        default=None,
        help="百分比配色方案 keyword。",
    )
    parser.add_argument(
        "--display-scope",
        choices=["all", "current", "merged"],
        default=None,
        help="展示范围：all 全部账号，current 启用账号，merged 合并账号。",
    )
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="quota 历史日志目录。")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="quota 历史日志基础文件名，用于匹配月度日志。")
    parser.add_argument("--control-file", default=DEFAULT_CONTROL_FILE, help="后台 sampler 控制文件名。")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="CodexTOP 状态文件名。")
    parser.add_argument("--tz", default="Asia/Shanghai", help="本地时区。")
    parser.add_argument("--once", action="store_true", help="渲染一帧后退出，用于调试。")
    args = parser.parse_args()

    runtime_paths = ensure_runtime_layout()
    control_path = args.log_dir.expanduser() / args.control_file
    state_path = args.log_dir.expanduser() / args.state_file
    saved_state = read_codextop_state(state_path)
    restore_interval = read_sampler_interval(control_path, DEFAULT_SAMPLER_INTERVAL_SECONDS)
    selected_color_scheme = color_schemes.set_active_color_scheme(
        args.color_scheme or saved_color_scheme(saved_state) or DEFAULT_COLOR_SCHEME
    )
    state = MonitorState(
        period=args.period or saved_period(saved_state) or DEFAULT_PERIOD,
        interval=args.interval if args.interval is not None else (saved_interval(saved_state) or restore_interval),
        tz=args.tz,
        log_path=args.log_dir.expanduser() / args.log_file,
        restore_interval=restore_interval,
        state_path=state_path,
        curve_mode=args.curve_mode or saved_curve_mode(saved_state) or DEFAULT_CURVE_MODE,
        display_scope=args.display_scope or saved_display_scope(saved_state) or DEFAULT_DISPLAY_SCOPE,
        window_scope=args.window_scope or saved_window_scope(saved_state) or DEFAULT_WINDOW_SCOPE,
        color_scheme=selected_color_scheme,
        control_path=control_path,
        update_check_path=update_check_path(runtime_paths.settings_dir),
    )
    if args.once:
        return run_once(state)
    return run_tui(state)
