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
from .preload import BackgroundDataPreloader
from core.paths import ensure_runtime_layout
from quota.token_usage_cache import DailyTokenUsageCacheReader, TOKEN_USAGE_CACHE_FILE
from .settings import handle_click, handle_setting_key, render_sidebar
from .session_tokens import TrustedDirectoryTokenUsageMonitor
from .token_charts import token_usage_chart_lines
from .trusted_directories import TOKEN_USAGE_DIRECTORIES_FILE
from core.state import (
    parse_interval,
    read_codextop_state,
    read_sampler_interval,
    saved_color_scheme,
    saved_curve_mode,
    saved_display_scope,
    saved_interval,
    saved_period,
    saved_usage_directory_scope,
    saved_usage_panel_layout,
    saved_window_scope,
    save_codextop_state,
    send_sampler_interval,
)
from core.update_check import start_daily_update_check, update_check_path
from .terminal_io import TerminalSession, parse_input
from .terminal_text import fit_ansi, paint

def _render_main_content(
    state: MonitorState,
    records: list[dict],
    accounts: list[dict],
    current: int | str | None,
    term_width: int,
    term_height: int,
    zones: list[ClickZone],
) -> tuple[list[str], int, int]:
    token_monitor = state.token_monitor
    token_usage = getattr(token_monitor, "latest", None)
    token_series = getattr(token_monitor, "rate_series", {})
    sidebar_width = 21
    main_width = max(40, term_width - sidebar_width)
    content_height = max(6, term_height - 1)
    if state.display_scope == "usage":
        if getattr(state, "token_preload_waiting", False):
            chart_lines = [paint("正在后台预加载 Token 数据...", "yellow")]
            chart_lines.extend([""] * max(0, content_height - 3))
        else:
            chart_lines = token_usage_chart_lines(
                token_series,
                token_usage,
                state.period,
                main_width - 2,
                content_height - 2,
                state.curve_mode,
                state.color_scheme,
                state.usage_panel_layout,
            )
        left_lines = panel(
            "Token 用量",
            chart_lines,
            main_width,
            content_height,
            "cyan",
        )
    elif not accounts:
        loading_text = (
            "正在后台预加载 quota 数据..."
            if getattr(state, "history_preload_waiting", False)
            else "正在加载 quota 数据..."
        )
        left_lines = [paint(loading_text, "yellow")]
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
            state.window_scope,
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
            border_color(active, current, state.window_scope),
        )
        left_lines = compose_columns([left_block, right_block], [left_width, right_width], content_height)
    elif state.display_scope == "merged":
        color = merged_border_color(accounts, state.window_scope)
        if content_height >= 17:
            top_height = 10
        elif content_height >= 14:
            top_height = 9
        else:
            top_height = max(4, content_height // 2)
        history_height = max(4, content_height - top_height)
        top_left_width = min(50, max(37, main_width // 3))
        top_right_width = max(24, main_width - top_left_width)
        top_left_width = main_width - top_right_width
        top_left_block = panel(
            "账号信息",
            merged_account_reset_lines(
                accounts,
                current,
                top_left_width,
                top_height,
                state.window_scope,
            ),
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
                state.window_scope,
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
                blocks.append(
                    panel(
                        title,
                        body,
                        width,
                        row_height,
                        border_color(account, current, state.window_scope),
                    )
                )
            while len(blocks) < columns:
                blocks.append([" " * panel_width for _ in range(row_height)])
            left_lines.extend(compose_columns(blocks, widths, row_height))
        if len(left_lines) < content_height:
            left_lines.extend([" " * main_width] * (content_height - len(left_lines)))
        left_lines = left_lines[:content_height]
    return left_lines, main_width, content_height


def start_background_preload(state: MonitorState) -> bool:
    preloader = state.preloader
    if state.background_preload_started or not isinstance(preloader, BackgroundDataPreloader):
        return False
    state.background_preload_started = True
    return preloader.start(state.display_scope, state.usage_directory_scope)


def _adopt_history_preload(state: MonitorState) -> bool:
    preloader = state.preloader
    if not isinstance(preloader, BackgroundDataPreloader) or not preloader.history_scheduled:
        return False
    try:
        result = preloader.take_history_if_ready()
    except Exception as exc:
        state.status = "quota 预加载失败，正在前台加载"
        state.error = str(exc)
        return False
    if result is None:
        return False
    state.history_cache = result.cache
    state.records = result.records
    state.records_version = result.cache.version
    state.last_records_read = result.completed_at
    state.next_read = 0.0
    state.main_cache_key = None
    return True


def _token_preload_needed(state: MonitorState, monitor: object) -> bool:
    if not isinstance(monitor, TrustedDirectoryTokenUsageMonitor) or monitor.version == 0:
        return True
    return state.usage_directory_scope == "all" and monitor.scope != "all"


def _adopt_token_preload(state: MonitorState) -> bool:
    preloader = state.preloader
    if not isinstance(preloader, BackgroundDataPreloader) or not preloader.token_scheduled:
        return False
    try:
        monitor = preloader.take_token_if_ready()
    except Exception as exc:
        state.status = "Token 预加载失败，正在前台加载"
        state.error = str(exc)
        return False
    if monitor is None:
        return False
    state.token_monitor = monitor
    state.token_version = monitor.version
    state.main_cache_key = None
    return True


def render_frame(state: MonitorState, term_width: int, term_height: int) -> tuple[list[str], list[ClickZone]]:
    state.history_preload_waiting = False
    state.token_preload_waiting = False
    preloader = state.preloader
    if state.display_scope == "usage":
        records = state.records or []
    else:
        _adopt_history_preload(state)
        if (
            state.records is None
            and isinstance(preloader, BackgroundDataPreloader)
            and preloader.history_scheduled
        ):
            state.history_preload_waiting = True
            state.status = "正在后台预加载 quota 数据"
            records = []
        else:
            records = read_records_if_due(state)
    token_monitor = state.token_monitor
    if state.display_scope != "usage" and isinstance(token_monitor, TrustedDirectoryTokenUsageMonitor):
        token_monitor.sync_directories()
    if state.display_scope == "usage":
        if _token_preload_needed(state, token_monitor):
            _adopt_token_preload(state)
            token_monitor = state.token_monitor
        if (
            _token_preload_needed(state, token_monitor)
            and isinstance(preloader, BackgroundDataPreloader)
            and preloader.token_scheduled
        ):
            state.token_preload_waiting = True
            state.status = "正在后台预加载 Token 数据"
        elif isinstance(token_monitor, TrustedDirectoryTokenUsageMonitor):
            token_monitor.set_scope(state.usage_directory_scope)
            if token_monitor.poll():
                state.token_version = token_monitor.version
                state.main_cache_key = None
            if token_monitor.error:
                state.status = "Token 读取失败"
                state.error = token_monitor.error
            elif state.status in {
                "启动中",
                "正在后台预加载 Token 数据",
                "Token 预加载失败，正在前台加载",
            }:
                state.status = "Token 数据已加载" if token_monitor.latest is not None else "等待 Token 数据"
                state.error = None
    accounts = current_accounts(state, records)
    token_total_reader = state.token_total_reader
    if isinstance(token_total_reader, DailyTokenUsageCacheReader):
        if token_total_reader.poll():
            state.token_total_version = token_total_reader.version
            state.main_cache_key = None
        enriched_accounts = []
        for account in accounts:
            index = account_index(account)
            cached = token_total_reader.totals.get(str(index)) if index is not None else None
            if cached is None:
                enriched_accounts.append(account)
                continue
            total, checked_at = cached
            enriched = dict(account)
            enriched["u"] = [total, checked_at]
            enriched_accounts.append(enriched)
        accounts = enriched_accounts
    current = current_index(state, records)
    last_timestamp = records[-1].get("t") if records else None
    cache_key = (
        state.records_version,
        state.token_version,
        state.token_total_version,
        len(records),
        last_timestamp,
        term_width,
        term_height,
        state.period,
        state.curve_mode,
        state.display_scope,
        state.window_scope,
        state.usage_directory_scope,
        state.usage_panel_layout,
        state.color_scheme,
        state.summary_offset,
        state.history_preload_waiting,
        state.token_preload_waiting,
    )
    if (
        state.main_cache_key == cache_key
        and state.main_cache_lines is not None
        and state.main_cache_zones is not None
    ):
        left_lines = state.main_cache_lines
        main_zones = list(state.main_cache_zones)
        sidebar_width = 21
        main_width = max(40, term_width - sidebar_width)
        content_height = max(6, term_height - 1)
    else:
        main_zones = []
        left_lines, main_width, content_height = _render_main_content(
            state,
            records,
            accounts,
            current,
            term_width,
            term_height,
            main_zones,
        )
        sidebar_width = 21
        state.main_cache_key = cache_key
        state.main_cache_lines = left_lines
        state.main_cache_zones = list(main_zones)

    zones = main_zones
    lines: list[str] = []
    sidebar = render_sidebar(state, sidebar_width, content_height, main_width + 1, zones)
    for left, right in zip(left_lines, sidebar):
        lines.append(fit_ansi(left, main_width) + fit_ansi(right, sidebar_width))
    while len(lines) < term_height:
        lines.append(" " * term_width)
    return [fit_ansi(line, term_width) for line in lines[:term_height]], zones


def run_once(state: MonitorState) -> int:
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
                start_background_preload(state)

                deadline = time.monotonic() + 0.2
                while time.monotonic() < deadline:
                    keep_running, clicks, keys = parse_input(session)
                    if not keep_running:
                        running = False
                        break
                    interacted = bool(keys or clicks)
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
                    if interacted:
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
        help="额度窗口：both 同时显示 5h/7d，5h 只显示 5h，7d 只显示 7d。",
    )
    parser.add_argument(
        "--color-scheme",
        choices=[value for _label, value in color_schemes.color_scheme_choices()],
        default=None,
        help="百分比配色方案 keyword。",
    )
    parser.add_argument(
        "--display-scope",
        choices=["all", "current", "merged", "usage"],
        default=None,
        help="展示范围：all 全部账号，current 启用账号，merged 合并账号，usage 当前项目 Token 用量。",
    )
    parser.add_argument(
        "--usage-directory-scope",
        choices=[value for _label, value in USAGE_DIRECTORY_SCOPE_CHOICES],
        default=None,
        help="Token 用量目录范围：current 当前信任目录，all 全部未禁用信任目录。",
    )
    parser.add_argument(
        "--usage-panel-layout",
        choices=[value for _label, value in USAGE_PANEL_LAYOUT_CHOICES],
        default=None,
        help="Token 用量面板布局：combined 合并图，split 四图拆分。",
    )
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="quota 历史日志目录。")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="quota 历史日志基础文件名，用于匹配月度日志。")
    parser.add_argument("--control-file", default=DEFAULT_CONTROL_FILE, help="后台 sampler 控制文件名。")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="CodexTOP 状态文件名。")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(), help="用于选择当前 Codex 线程的项目目录。")
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
    selected_usage_directory_scope = (
        args.usage_directory_scope
        or saved_usage_directory_scope(saved_state)
        or DEFAULT_USAGE_DIRECTORY_SCOPE
    )
    selected_usage_panel_layout = (
        args.usage_panel_layout
        or saved_usage_panel_layout(saved_state)
        or DEFAULT_USAGE_PANEL_LAYOUT
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
        usage_directory_scope=selected_usage_directory_scope,
        usage_panel_layout=selected_usage_panel_layout,
        control_path=control_path,
        update_check_path=update_check_path(runtime_paths.settings_dir),
        token_monitor=TrustedDirectoryTokenUsageMonitor(
            runtime_paths.codex_dir / "sessions",
            args.project_dir,
            runtime_paths.codex_dir / "config.toml",
            runtime_paths.settings_dir / TOKEN_USAGE_DIRECTORIES_FILE,
            selected_usage_directory_scope,
        ),
        token_total_reader=DailyTokenUsageCacheReader(
            runtime_paths.settings_dir / TOKEN_USAGE_CACHE_FILE
        ),
        preloader=BackgroundDataPreloader(
            args.log_dir.expanduser() / args.log_file,
            args.tz,
            runtime_paths.codex_dir / "sessions",
            args.project_dir,
            runtime_paths.codex_dir / "config.toml",
            runtime_paths.settings_dir / TOKEN_USAGE_DIRECTORIES_FILE,
        ),
    )
    if args.once:
        return run_once(state)
    return run_tui(state)
