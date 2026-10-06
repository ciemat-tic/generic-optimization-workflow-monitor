from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtWidgets import QApplication

from gow_monitor import __version__
from gow_monitor.ui.main_window import MainWindow
from gow_monitor.ui.splash_screen import SplashScreen


def _positive_pid(value: str) -> int:
    try:
        pid = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("GOW PID must be an integer") from exc

    if pid < 1:
        raise argparse.ArgumentTypeError("GOW PID must be positive")

    return pid


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gow-monitor",
        description="Independent desktop monitor for GOW runs.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        help=(
            "Connect immediately to a GOW output directory. The directory "
            "may be empty when the monitor starts."
        ),
    )
    parser.add_argument(
        "--data-mode",
        choices=("auto", "summary", "full"),
        default="auto",
        help=(
            "Artifact detail policy. auto uses generation summaries for "
            "million-scale campaigns, summary always avoids shard scans, "
            "and full computes exact statistics from generation shards."
        ),
    )
    parser.add_argument(
        "--gow-pid",
        type=_positive_pid,
        help=(
            "Attach resource telemetry to the root operating-system process "
            "that is executing GOW."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    application = QApplication.instance()
    owns_application = application is None

    if application is None:
        application = QApplication(sys.argv[:1])

    application.setApplicationName("GOW Monitor")
    application.setOrganizationName("CST Modelling Tools")
    application.setStyle("Fusion")

    splash: SplashScreen | None = None
    if owns_application:
        splash = SplashScreen()
        splash.show()
        application.processEvents()

    window = MainWindow(
        gow_pid=arguments.gow_pid,
        data_mode=arguments.data_mode,
    )

    if arguments.results_root is not None:
        results_root = arguments.results_root.expanduser()
        try:
            results_root.mkdir(parents=True, exist_ok=True)
            window.connect_results_root_async(results_root)
        except OSError as exc:
            if splash is not None:
                splash.close()
            window.close()
            print(
                f"Unable to connect GOW results directory: {exc}",
                file=sys.stderr,
            )
            return 2

    if not owns_application:
        window.show()
        return 0

    assert splash is not None
    splash.finish(window)
    return int(application.exec())
