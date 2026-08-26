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
from core.i18n import detect_language, set_language, t
from .history import account_freshness_cache_key, current_accounts, current_index, read_records_if_due
from .models import ClickZone, MonitorState
from .panels import *
from .preload import BackgroundDataPreloader
from core.paths import ensure_runtime_layout
from quota.token_usage_cache import DailyTokenUsageCacheReader, TOKEN_USAGE_CACHE_FILE
from .settings import (
    handle_click,
    handle_setting_key,
    handle_update_dialog_key,
    handle_version_dialog_key,
    render_sidebar,
    render_update_overlay,
    render_version_overlay,
)
from .session_tokens import TrustedDirectoryTokenUsageMonitor
from .token_charts import token_usage_chart_lines
from .trusted_directories import TOKEN_USAGE_DIRECTORIES_FILE
from core.state import (
    parse_interval,
    read_codextop_state,
    read_sampler_interval,
    saved_color_scheme,
    saved_curve_mode,
    saved_dashboard_density,
    saved_daily_summary_date,
    saved_display_scope,
    saved_hide_account_info,
    saved_interval,
    saved_language,
    saved_merged_quota_mode,
    saved_period,
    saved_usage_axis_scale,
    saved_usage_directory_scope,
    saved_usage_panel_layout,
    saved_window_scope,
    save_codextop_state,
    send_sampler_interval,
)
from core.update_check import start_daily_update_check, update_check_path
from .terminal_io import TerminalSession, parse_input
from .terminal_text import fit_ansi, paint
from .statistics_summary import (
    close_expired_statistics,
    handle_statistics_key,
    open_daily_statistics_if_due,
    render_statistics_overlay,
)


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
    sidebar_width = SIDEBAR_WIDTH
    main_width = max(40, term_width - sidebar_width)
    content_height = max(6, term_height - 1)
    if state.display_scope == "usage":
        if getattr(state, "token_preload_waiting", False):
            chart_lines = [paint(t("app.token_preloading"), "yellow")]
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
                getattr(state, "usage_axis_scale", DEFAULT_USAGE_AXIS_SCALE),
            )
        left_lines = panel(
            t("app.token_usage"),
            chart_lines,
            main_width,
            content_height,
            current_account_border_color(accounts, current, state.window_scope),
        )
    elif not accounts:
        loading_text = (
            t("app.quota_preloading")
            if getattr(state, "history_preload_waiting", False)
            else t("app.quota_loading")
        )
        left_lines = [paint(loading_text, "yellow")]
        left_lines.extend([""] * (content_height - 1))
        left_lines = [fit_ansi(line, main_width) for line in left_lines[:content_height]]
    elif state.display_scope == "focus":
        active = selected_account(accounts, current)
        left_width = min(50, max(20, main_width // 3))
        right_width = max(12, main_width - left_width)
        left_width = main_width - right_width
        _focus_heights, page_starts, page_index = focus_page_layout(
            accounts,
            left_width,
            content_height,
            state.curve_mode,
            state.window_scope,
            state.summary_offset,
        )
        state.summary_offset = page_starts[page_index]
        left_block = stacked_summary_panels(
            accounts,
            current,
            left_width,
            content_height,
            state.summary_offset,
            state.curve_mode,
            state.window_scope,
            getattr(state, "hide_account_info", DEFAULT_HIDE_ACCOUNT_INFO),
        )
        history_title = t("app.provider_quota_history", provider=provider_name(active))
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
            current_account_border_color(accounts, current, state.window_scope),
        )
        left_lines = compose_columns([left_block, right_block], [left_width, right_width], content_height)
    elif state.display_scope == "merged":
        color = current_account_border_color(accounts, current, state.window_scope)
        if content_height >= 17:
            base_top_height = 10
        elif content_height >= 14:
            base_top_height = 9
        else:
            base_top_height = max(4, content_height // 2)
        top_left_width = min(50, max(37, main_width // 3))
        top_right_width = max(24, main_width - top_left_width)
        top_left_width = main_width - top_right_width
        required_top_height = len(
            merged_account_reset_body(
                accounts,
                current,
                max(10, top_left_width - 2),
                state.window_scope,
            )
        ) + 2
        top_height = min(
            max(base_top_height, required_top_height),
            max(4, content_height - 4),
        )
        history_height = max(4, content_height - top_height)
        top_left_block = panel(
            t("app.account_info"),
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
            t("app.total_quota"),
            merged_quota_summary_lines(
                accounts,
                current,
                top_right_width,
                top_height,
                state.curve_mode,
                state.window_scope,
                getattr(state, "merged_quota_mode", DEFAULT_MERGED_QUOTA_MODE),
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
            t("app.merged_quota_history"),
            merged_history_lines(
                records,
                main_width,
                history_height,
                state.period,
                state.curve_mode,
                state.window_scope,
                accounts=accounts,
                merged_quota_mode=getattr(
                    state,
                    "merged_quota_mode",
                    DEFAULT_MERGED_QUOTA_MODE,
                ),
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
        heights, widths, last_page_start = dashboard_grid_layout(
            main_width,
            content_height,
            len(accounts),
            getattr(state, "dashboard_density", DEFAULT_DASHBOARD_DENSITY),
        )
        page_size = max(1, len(heights) * len(widths))
        state.summary_offset = min(
            max(0, state.summary_offset) // page_size * page_size,
            last_page_start,
        )
        row_count = len(heights)
        body_lines = []
        for row, row_height in enumerate(heights):
            blocks = []
            for column, width in enumerate(widths):
                account_position = state.summary_offset + column * row_count + row
                if account_position >= len(accounts):
                    blocks.append([" " * width for _ in range(row_height)])
                    continue
                account = accounts[account_position]
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
                    getattr(state, "hide_account_info", DEFAULT_HIDE_ACCOUNT_INFO),
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
            body_lines.extend(compose_columns(blocks, widths, row_height))
        left_lines = body_lines
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
        state.status = t("status.quota_preload_failed")
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
    requested_scope = "all" if state.statistics_open else state.usage_directory_scope
    return requested_scope == "all" and monitor.scope != "all"


def _adopt_token_preload(state: MonitorState) -> bool:
    preloader = state.preloader
    if not isinstance(preloader, BackgroundDataPreloader) or not preloader.token_scheduled:
        return False
    try:
        monitor = preloader.take_token_if_ready()
    except Exception as exc:
        state.status = t("status.token_preload_failed")
        state.error = str(exc)
        return False
    if monitor is None:
        return False
    state.token_monitor = monitor
    state.token_version = monitor.version
    state.main_cache_key = None
    return True


def _prepare_statistics_tokens(state: MonitorState) -> bool:
    preloader = state.preloader
    if isinstance(preloader, BackgroundDataPreloader):
        if not state.background_preload_started:
            return False
        if preloader.token_scheduled:
            _adopt_token_preload(state)
            if preloader.token_pending:
                return False
    monitor = state.token_monitor
    if not isinstance(monitor, TrustedDirectoryTokenUsageMonitor):
        return False
    if monitor.scope != "all":
        monitor.set_scope("all")
    if monitor.poll(force_discovery=monitor.version == 0):
        state.token_version = monitor.version
        state.main_cache_key = None
        state.statistics_cache_key = None
    return monitor.error is None


def render_frame(state: MonitorState, term_width: int, term_height: int) -> tuple[list[str], list[ClickZone]]:
    close_expired_statistics(state)
    state.history_preload_waiting = False
    state.token_preload_waiting = False
    preloader = state.preloader
    _adopt_history_preload(state)
    if state.display_scope == "usage":
        records = state.records or []
        if state.history_cache is not None:
            token_status = state.status
            token_error = state.error
            records = read_records_if_due(state)
            state.status = token_status
            state.error = token_error
    else:
        if (
            state.records is None
            and isinstance(preloader, BackgroundDataPreloader)
            and preloader.history_scheduled
        ):
            state.history_preload_waiting = True
            state.status = t("status.quota_preloading")
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
            and (preloader.token_scheduled or (state.statistics_open and not state.background_preload_started))
        ):
            state.token_preload_waiting = True
            state.status = t("status.token_preloading")
        elif isinstance(token_monitor, TrustedDirectoryTokenUsageMonitor):
            token_monitor.set_scope("all" if state.statistics_open else state.usage_directory_scope)
            if token_monitor.poll():
                state.token_version = token_monitor.version
                state.main_cache_key = None
            if token_monitor.error:
                state.status = t("status.token_read_failed")
                state.error = token_monitor.error
            elif state.status in {
                t("status.starting"),
                t("status.token_preloading"),
                t("status.token_preload_failed"),
            }:
                state.status = (
                    t("status.token_loaded")
                    if token_monitor.latest is not None
                    else t("status.token_waiting")
                )
                state.error = None
    statistics_tokens_ready = _prepare_statistics_tokens(state) if state.statistics_open else False
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
        getattr(state, "dashboard_density", DEFAULT_DASHBOARD_DENSITY),
        getattr(state, "hide_account_info", DEFAULT_HIDE_ACCOUNT_INFO),
        state.window_scope,
        getattr(state, "merged_quota_mode", DEFAULT_MERGED_QUOTA_MODE),
        state.usage_directory_scope,
        state.usage_panel_layout,
        getattr(state, "usage_axis_scale", DEFAULT_USAGE_AXIS_SCALE),
        state.color_scheme,
        state.summary_offset,
        account_freshness_cache_key(accounts),
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
        sidebar_width = SIDEBAR_WIDTH
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
        sidebar_width = SIDEBAR_WIDTH
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
    fitted = [fit_ansi(line, term_width) for line in lines[:term_height]]
    if state.version_dialog_open:
        return render_version_overlay(fitted, state, term_width, term_height)
    if state.update_dialog_open:
        return render_update_overlay(fitted, state, term_width, term_height)
    if state.statistics_open:
        return render_statistics_overlay(
            fitted,
            state,
            records,
            accounts,
            term_width,
            term_height,
            local_tokens_ready=statistics_tokens_ready,
        )
    return fitted, zones


def run_once(state: MonitorState) -> int:
    width, height = shutil.get_terminal_size((160, 48))
    lines, _zones = render_frame(state, width, height)
    print("\n".join(lines))
    return 0 if not state.error else 1


def run_update_after_tui() -> int:
    updater_path = Path(__file__).resolve().parents[1] / "core" / "updater.py"
    print(t("update.starting") + "\n")
    proc = subprocess.run([sys.executable, str(updater_path)], check=False)
    if proc.returncode == 0:
        print("\n" + t("update.complete_restart"))
    else:
        print("\n" + t("update.failed_retry"))
    return proc.returncode


def run_tui(state: MonitorState) -> int:
    if open_daily_statistics_if_due(state):
        try:
            save_codextop_state(state)
        except Exception as exc:
            state.status = t("cleanup.save_settings_failed", error=exc)
            state.error = str(exc)
    start_daily_update_check(state)
    state.next_read = 0.0
    if state.interval != state.restore_interval or state.display_scope == "merged":
        try:
            send_sampler_interval(state, state.interval, sample_now=True, all_auth=True)
        except Exception as exc:
            state.status = t("common.command_failed")
            state.error = str(exc)
    cleanup_errors: list[str] = []
    try:
        with TerminalSession() as session:
            start_background_preload(state)
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
                    interacted = bool(keys or clicks)
                    for key in keys:
                        if state.version_dialog_open:
                            running = handle_version_dialog_key(state, key)
                        elif state.update_dialog_open:
                            running = handle_update_dialog_key(state, key)
                        elif state.statistics_open:
                            handle_statistics_key(state, key)
                        else:
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
        preloader = state.preloader
        if isinstance(preloader, BackgroundDataPreloader):
            preloader.persist(state.history_cache, state.token_monitor)
        try:
            save_codextop_state(state)
        except Exception as exc:
            cleanup_errors.append(t("cleanup.save_settings_failed", error=exc))
        try:
            send_sampler_interval(state, state.restore_interval, sample_now=False)
        except Exception as exc:
            cleanup_errors.append(t("cleanup.restore_interval_failed", error=exc))
    if cleanup_errors:
        print("\n".join(cleanup_errors), file=sys.stderr)
        return 1
    if state.update_requested:
        return run_update_after_tui()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=t("app.arg.description"))
    parser.add_argument("-p", "--period", choices=PERIOD_CHOICES, default=None, help=t("app.arg.period"))
    parser.add_argument("-i", "--interval", type=parse_interval, default=None, help=t("app.arg.interval"))
    parser.add_argument(
        "--curve-mode",
        choices=[value for _label, value in CURVE_MODE_CHOICES],
        default=None,
        help=t("app.arg.curve_mode"),
    )
    parser.add_argument(
        "--window-scope",
        choices=[value for _label, value in WINDOW_SCOPE_CHOICES],
        default=None,
        help=t("app.arg.window_scope"),
    )
    parser.add_argument(
        "--merged-quota-mode",
        choices=[value for _label, value in MERGED_QUOTA_MODE_CHOICES],
        default=None,
        help=t("app.arg.merged_quota_mode"),
    )
    parser.add_argument(
        "--color-scheme",
        choices=[value for _label, value in color_schemes.color_scheme_choices()],
        default=None,
        help=t("app.arg.color_scheme"),
    )
    parser.add_argument(
        "--display-scope",
        choices=[value for _label, value in DISPLAY_SCOPE_CHOICES],
        default=None,
        help=t("app.arg.display_scope"),
    )
    parser.add_argument(
        "--usage-directory-scope",
        choices=[value for _label, value in USAGE_DIRECTORY_SCOPE_CHOICES],
        default=None,
        help=t("app.arg.usage_directory_scope"),
    )
    parser.add_argument(
        "--usage-panel-layout",
        choices=[value for _label, value in USAGE_PANEL_LAYOUT_CHOICES],
        default=None,
        help=t("app.arg.usage_panel_layout"),
    )
    parser.add_argument(
        "--usage-axis-scale",
        choices=[value for _label, value in USAGE_AXIS_SCALE_CHOICES],
        default=None,
        help=t("app.arg.usage_axis_scale"),
    )
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help=t("app.arg.log_dir"))
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE, help=t("app.arg.log_file"))
    parser.add_argument("--control-file", default=DEFAULT_CONTROL_FILE, help=t("app.arg.control_file"))
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help=t("app.arg.state_file"))
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(), help=t("app.arg.project_dir"))
    parser.add_argument("--tz", default="Asia/Shanghai", help=t("app.arg.timezone"))
    parser.add_argument("--once", action="store_true", help=t("app.arg.once"))
    args = parser.parse_args()

    runtime_paths = ensure_runtime_layout()
    control_path = args.log_dir.expanduser() / args.control_file
    state_path = args.log_dir.expanduser() / args.state_file
    saved_state = read_codextop_state(state_path)
    selected_language = saved_language(saved_state) or detect_language()
    set_language(selected_language)
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
    selected_usage_axis_scale = (
        args.usage_axis_scale
        or saved_usage_axis_scale(saved_state)
        or DEFAULT_USAGE_AXIS_SCALE
    )
    selected_hide_account_info = saved_hide_account_info(saved_state)
    state = MonitorState(
        period=args.period or saved_period(saved_state) or DEFAULT_PERIOD,
        interval=args.interval if args.interval is not None else (saved_interval(saved_state) or restore_interval),
        tz=args.tz,
        log_path=args.log_dir.expanduser() / args.log_file,
        restore_interval=restore_interval,
        state_path=state_path,
        curve_mode=args.curve_mode or saved_curve_mode(saved_state) or DEFAULT_CURVE_MODE,
        display_scope=args.display_scope or saved_display_scope(saved_state) or DEFAULT_DISPLAY_SCOPE,
        dashboard_density=(
            saved_dashboard_density(saved_state) or DEFAULT_DASHBOARD_DENSITY
        ),
        hide_account_info=(
            selected_hide_account_info
            if selected_hide_account_info is not None
            else DEFAULT_HIDE_ACCOUNT_INFO
        ),
        window_scope=args.window_scope or saved_window_scope(saved_state) or DEFAULT_WINDOW_SCOPE,
        color_scheme=selected_color_scheme,
        language=selected_language,
        status=t("status.starting"),
        merged_quota_mode=(
            args.merged_quota_mode
            or saved_merged_quota_mode(saved_state)
            or DEFAULT_MERGED_QUOTA_MODE
        ),
        usage_directory_scope=selected_usage_directory_scope,
        usage_panel_layout=selected_usage_panel_layout,
        usage_axis_scale=selected_usage_axis_scale,
        daily_summary_date=saved_daily_summary_date(saved_state),
        statistics_week_start=SUMMARY_WEEK_START,
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
            cache_dir=runtime_paths.runtime_dir / "cache",
        ),
    )
    if args.once:
        return run_once(state)
    return run_tui(state)
