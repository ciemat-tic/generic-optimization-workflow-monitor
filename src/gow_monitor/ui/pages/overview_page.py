from __future__ import annotations

import time
from decimal import Decimal

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gow_monitor.domain import (
    EvaluationPoint,
    GowProcessResourceSnapshot,
    ObjectiveDirection,
    RunSnapshot,
    RunState,
    SystemResourceSnapshot,
)
from gow_monitor.ui.dashboard_metrics import (
    best_series,
    cumulative_evaluations_series,
    recent_improvement_percent,
    recent_improvement_series,
    rolling_failure_rate_series,
    rolling_valid_rate_series,
)
from gow_monitor.ui.formatting import format_fixed
from gow_monitor.ui.widgets import (
    KpiCard,
    ObjectiveProgressChart,
    PopulationDiversityChart,
    ResourcesPanel,
    RunHealthPanel,
    SearchBehaviorPanel,
)
from gow_monitor.ui.widgets.chart_dialog import ChartDialog


class OverviewPage(QWidget):
    """Dense live dashboard for the currently selected GOW run."""

    _OBJECTIVE_DECIMALS_MIN = 2
    _OBJECTIVE_DECIMALS_MAX = 12

    _TERMINAL_STATES = frozenset(
        {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.STOPPED,
        }
    )

    _PAUSED_RUNTIME_STATES = frozenset(
        {
            RunState.PAUSED,
            RunState.RESUMING,
        }
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self._snapshot: RunSnapshot | None = None
        self._history: tuple[EvaluationPoint, ...] = ()
        self._runtime_completion_times: dict[str, float] = {}
        self._runtime_paused_seconds: dict[str, float] = {}
        self._runtime_pause_started_at: dict[str, float] = {}
        self._evaluation_detail_base = ""
        self._objective_decimals = self._OBJECTIVE_DECIMALS_MIN
        self._progress_dialog: ChartDialog | None = None
        self._diversity_dialog: ChartDialog | None = None
        self._expanded_progress_chart: ObjectiveProgressChart | None = None
        self._expanded_diversity_chart: PopulationDiversityChart | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(5)

        heading_row = QHBoxLayout()
        heading_row.setContentsMargins(0, 0, 0, 0)
        heading_row.setSpacing(12)

        self.title_label = QLabel("Overview")
        self.title_label.setObjectName("pageTitle")
        description = QLabel(
            "Live metrics calculated only from observable GOW artifacts."
        )
        description.setObjectName("description")
        description.setWordWrap(True)
        description.setMaximumHeight(24)

        heading_row.addWidget(self.title_label)
        heading_row.addWidget(description, 1)

        cards_layout = QHBoxLayout()
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(6)

        self.cards = {
            "best": KpiCard("Best objective"),
            "evaluations": KpiCard("Evaluations"),
            "elapsed": KpiCard("Run time"),
            "improvement": KpiCard("Improvement rate"),
            "success": KpiCard("Success rate", visual="gauge"),
            "failures": KpiCard("Failure rate", visual="gauge"),
        }

        self.cards["elapsed"].setParent(self)
        self.cards["elapsed"].hide()

        self.cards["best"].show_value_controls()
        self.cards["best"].decrease_button.clicked.connect(
            self._decrease_objective_decimals
        )
        self.cards["best"].increase_button.clicked.connect(
            self._increase_objective_decimals
        )

        for key, card in self.cards.items():
            if key == "elapsed":
                card.setParent(self)
                continue
            cards_layout.addWidget(card, 1)

        self.timer_row = QHBoxLayout()
        self.timer_row.setContentsMargins(0, 0, 0, 0)
        self.timer_row.setSpacing(10)

        self.timer_panel = QFrame()
        self.timer_panel.setObjectName("runTimerPanel")
        self.timer_panel.setProperty("timerState", "waiting")
        self.timer_panel.setFixedSize(300, 50)

        timer_layout = QHBoxLayout(self.timer_panel)
        timer_layout.setContentsMargins(14, 5, 12, 5)
        timer_layout.setSpacing(10)

        self.timer_caption_label = QLabel("RUN TIMER")
        self.timer_caption_label.setObjectName("runTimerCaption")

        self.timer_value_label = QLabel("--:--:--")
        self.timer_value_label.setObjectName("runTimerValue")
        self.timer_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timer_value_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.timer_status_label = QLabel("WAITING")
        self.timer_status_label.setObjectName("runTimerStatus")
        self.timer_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        timer_layout.addWidget(self.timer_caption_label)
        timer_layout.addWidget(self.timer_value_label, 1)
        timer_layout.addWidget(self.timer_status_label)

        self.timer_row.addStretch(1)
        self.timer_row.addWidget(self.timer_panel)
        self.timer_row.addStretch(1)

        self.dashboard_layout = QGridLayout()
        self.dashboard_layout.setContentsMargins(0, 0, 0, 0)
        self.dashboard_layout.setHorizontalSpacing(6)
        self.dashboard_layout.setVerticalSpacing(6)

        self.progress_panel = QFrame()
        self.progress_panel.setObjectName("dashboardPanel")
        progress_layout = QVBoxLayout(self.progress_panel)
        progress_layout.setContentsMargins(7, 5, 7, 5)
        progress_layout.setSpacing(3)

        progress_header = QHBoxLayout()
        progress_header.setContentsMargins(0, 0, 0, 0)
        progress_header.setSpacing(6)

        progress_title = QLabel("Optimization progress")
        progress_title.setObjectName("panelTitle")

        self.window_selector = QComboBox()
        self.window_selector.setObjectName("windowSelector")
        self.window_selector.addItem("All evaluations", None)
        self.window_selector.addItem("Last 500", 500)
        self.window_selector.addItem("Last 100", 100)
        self.window_selector.currentIndexChanged.connect(
            self._window_selection_changed
        )

        self.expand_progress_button = QPushButton("Expand")
        self.expand_progress_button.setObjectName("chartActionButton")
        self.expand_progress_button.setToolTip(
            "Open Optimization progress in a large live window"
        )
        self.expand_progress_button.clicked.connect(
            self._open_progress_chart
        )

        progress_header.addWidget(progress_title)
        progress_header.addStretch(1)
        progress_header.addWidget(self.window_selector)
        progress_header.addWidget(self.expand_progress_button)

        self.progress_chart = ObjectiveProgressChart()

        self.chart_meta_bar = QFrame()
        self.chart_meta_bar.setObjectName("chartMetaBar")
        chart_meta_layout = QHBoxLayout(self.chart_meta_bar)
        chart_meta_layout.setContentsMargins(7, 3, 7, 3)

        self.chart_footer = QLabel("No GOW run connected")
        self.chart_footer.setObjectName("chartFooter")
        self.chart_footer.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.chart_footer.setWordWrap(False)
        chart_meta_layout.addWidget(self.chart_footer)

        progress_layout.addLayout(progress_header)
        progress_layout.addWidget(self.progress_chart, 1)
        progress_layout.addWidget(self.chart_meta_bar)

        self.run_health_panel = RunHealthPanel()

        self.diversity_panel = QFrame()
        self.diversity_panel.setObjectName("dashboardPanel")
        diversity_layout = QVBoxLayout(self.diversity_panel)
        diversity_layout.setContentsMargins(7, 5, 7, 5)
        diversity_layout.setSpacing(3)

        diversity_header = QHBoxLayout()
        diversity_header.setContentsMargins(0, 0, 0, 0)
        diversity_header.setSpacing(6)

        self.diversity_title = QLabel("Population diversity")
        self.diversity_title.setObjectName("panelTitle")

        self.diversity_meta_label = QLabel("Awaiting population data")
        self.diversity_meta_label.setObjectName("panelMeta")

        self.expand_diversity_button = QPushButton("Expand")
        self.expand_diversity_button.setObjectName("chartActionButton")
        self.expand_diversity_button.setToolTip(
            "Open Population diversity in a large live window"
        )
        self.expand_diversity_button.clicked.connect(
            self._open_diversity_chart
        )

        diversity_header.addWidget(self.diversity_title)
        diversity_header.addStretch(1)
        diversity_header.addWidget(self.diversity_meta_label)
        diversity_header.addWidget(self.expand_diversity_button)

        self.diversity_chart = PopulationDiversityChart()

        diversity_layout.addLayout(diversity_header)
        diversity_layout.addWidget(self.diversity_chart, 1)

        self.dashboard_layout.addWidget(
            self.progress_panel, 0, 0, 2, 1
        )
        self.dashboard_layout.addWidget(
            self.run_health_panel, 0, 1
        )
        self.dashboard_layout.addWidget(
            self.diversity_panel, 1, 1
        )

        self.dashboard_layout.setColumnStretch(0, 7)
        self.dashboard_layout.setColumnStretch(1, 3)
        self.dashboard_layout.setRowStretch(0, 1)
        self.dashboard_layout.setRowStretch(1, 1)

        self.lower_layout = QGridLayout()
        self.lower_layout.setContentsMargins(0, 0, 0, 0)
        self.lower_layout.setHorizontalSpacing(6)
        self.lower_layout.setVerticalSpacing(6)

        self.search_behavior_panel = SearchBehaviorPanel()
        self.resources_panel = ResourcesPanel(
            preferred_columns=5,
            compact_breakpoint_px=700,
            single_breakpoint_px=360,
        )
        self._lower_panels_stacked: bool | None = None
        self._reflow_lower_panels(stacked=False)

        layout.addLayout(heading_row)
        layout.addLayout(self.timer_row)
        layout.addLayout(cards_layout)
        layout.addLayout(self.dashboard_layout, 1)
        layout.addLayout(self.lower_layout)

        self.runtime_timer = QTimer(self)
        self.runtime_timer.setInterval(1000)
        self.runtime_timer.timeout.connect(
            self._refresh_runtime_cards
        )

        self.clear()
        self.runtime_timer.start()

    def install_run_controls(
        self,
        pause_button: QPushButton,
        continue_button: QPushButton,
    ) -> None:
        """Place the run lifecycle controls around the central timer.

        The buttons remain the same instances owned by HeaderWidget.
        Only their visual parent and layout position change, preserving the
        already validated signal and state-machine behavior.
        """

        if self.timer_row.indexOf(pause_button) >= 0:
            return

        if self.timer_row.indexOf(continue_button) >= 0:
            return

        pause_button.setParent(self)
        continue_button.setParent(self)

        pause_button.setText("⏸  Pause")
        continue_button.setText("▶  Continue")

        pause_button.setFixedSize(
            self.timer_panel.size()
        )
        continue_button.setFixedSize(
            self.timer_panel.size()
        )

        pause_button.setAccessibleName(
            "Pause optimization"
        )
        continue_button.setAccessibleName(
            "Continue optimization"
        )

        pause_button.setToolTip(
            "Pause after GOW reaches the next safe generation boundary."
        )
        continue_button.setToolTip(
            "Continue this run from its persisted checkpoint."
        )

        # Current items:
        #
        #   stretch | timer | stretch
        #
        # Result:
        #
        #   stretch | Pause | timer | Continue | stretch
        #
        self.timer_row.insertWidget(
            1,
            pause_button,
        )

        self.timer_row.insertWidget(
            self.timer_row.count() - 1,
            continue_button,
        )

    @property
    def lower_panels_stacked(self) -> bool:
        return bool(self._lower_panels_stacked)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)

        self._reflow_lower_panels(
            stacked=event.size().width() < 1100
        )

    def _reflow_lower_panels(
        self,
        *,
        stacked: bool,
    ) -> None:
        if self._lower_panels_stacked == stacked:
            return

        self.lower_layout.removeWidget(
            self.search_behavior_panel
        )
        self.lower_layout.removeWidget(
            self.resources_panel
        )

        for row in range(2):
            self.lower_layout.setRowStretch(row, 0)

        for column in range(2):
            self.lower_layout.setColumnStretch(column, 0)

        if stacked:
            self.lower_layout.addWidget(
                self.search_behavior_panel,
                0,
                0,
            )
            self.lower_layout.addWidget(
                self.resources_panel,
                1,
                0,
            )
            self.lower_layout.setColumnStretch(0, 1)
        else:
            self.lower_layout.addWidget(
                self.search_behavior_panel,
                0,
                0,
            )
            self.lower_layout.addWidget(
                self.resources_panel,
                0,
                1,
            )
            self.lower_layout.setColumnStretch(0, 4)
            self.lower_layout.setColumnStretch(1, 6)

        self._lower_panels_stacked = stacked
        self.updateGeometry()

    @staticmethod
    def _format_objective(value: float | None) -> str:
        if value is None:
            return "-"
        decimal_value = Decimal(str(value))
        if decimal_value == 0:
            return "0"

        decimal_text = format(decimal_value, "f")
        if "." in decimal_text:
            decimal_text = decimal_text.rstrip("0").rstrip(".")
        return decimal_text

    def _format_best_objective(
        self,
        value: float | None,
    ) -> str:
        if value is None:
            return "-"
        return f"{value:.{self._objective_decimals}f}"

    def _set_objective_decimals(self, decimals: int) -> None:
        bounded = max(
            self._OBJECTIVE_DECIMALS_MIN,
            min(self._OBJECTIVE_DECIMALS_MAX, int(decimals)),
        )
        if bounded == self._objective_decimals:
            self._update_objective_precision_controls()
            return

        self._objective_decimals = bounded
        self._update_objective_precision_controls()
        self._render_best_objective()

    def _decrease_objective_decimals(
        self,
        checked: bool = False,
    ) -> None:
        del checked
        self._set_objective_decimals(
            self._objective_decimals - 1
        )

    def _increase_objective_decimals(
        self,
        checked: bool = False,
    ) -> None:
        del checked
        self._set_objective_decimals(
            self._objective_decimals + 1
        )

    def _update_objective_precision_controls(self) -> None:
        card = self.cards["best"]
        card.decrease_button.setEnabled(
            self._objective_decimals
            > self._OBJECTIVE_DECIMALS_MIN
        )
        card.increase_button.setEnabled(
            self._objective_decimals
            < self._OBJECTIVE_DECIMALS_MAX
        )
        card.value_label.setToolTip(
            f"Displayed with {self._objective_decimals} decimal places"
        )

    def _render_best_objective(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            self._update_objective_precision_controls()
            return

        reference = snapshot.reference
        self.cards["best"].set_value(
            self._format_best_objective(snapshot.best_objective),
            detail=reference.direction.value,
            tone=(
                "good"
                if snapshot.best_objective is not None
                else "neutral"
            ),
            series=best_series(self._history),
        )
        self._update_objective_precision_controls()

    def render_snapshot(
        self,
        snapshot: RunSnapshot,
        history: tuple[EvaluationPoint, ...] = (),
    ) -> None:
        self._snapshot = snapshot
        self._history = history
        reference = snapshot.reference
        self._capture_runtime_completion(snapshot)

        improvement = recent_improvement_percent(
            history,
            reference.direction,
        )
        improvement_text = (
            format_fixed(improvement, suffix="%")
            if improvement is not None
            else "N/A"
        )
        success_rate = snapshot.success_rate * 100.0
        failure_rate = snapshot.failure_rate * 100.0

        self._render_best_objective()
        if snapshot.planned_evaluations is not None:
            planned = snapshot.planned_evaluations
            evaluation_value = (
                f"{snapshot.evaluation_count:,} / {planned:,}"
            )
            completion = (
                snapshot.evaluation_count / planned * 100.0
                if planned
                else 0.0
            )
            evaluation_detail = (
                f"{format_fixed(completion, suffix='%')} completed"
                f" | {snapshot.result_sources} source(s)"
                f" | {snapshot.artifact_mode}"
            )
        else:
            evaluation_value = f"{snapshot.evaluation_count:,}"
            evaluation_detail = (
                f"{snapshot.result_sources} distinct source(s)"
            )

        self._evaluation_detail_base = evaluation_detail
        self.cards["evaluations"].set_value(
            evaluation_value,
            detail=evaluation_detail,
            series=cumulative_evaluations_series(history),
        )
        self.cards["improvement"].set_value(
            improvement_text,
            detail=(
                "Over recent generation checkpoints"
                if snapshot.artifact_mode == "generation-summary"
                else "Over last 100 evaluations"
            ),
            tone=(
                "good"
                if improvement is not None and improvement > 0.0
                else "neutral"
            ),
            series=recent_improvement_series(history, reference.direction),
        )
        if snapshot.quality_metrics_complete:
            self.cards["success"].set_value(
                format_fixed(success_rate, suffix="%"),
                detail=f"{snapshot.successful_evaluations} valid objectives",
                tone="good" if snapshot.successful_evaluations else "warning",
                series=rolling_valid_rate_series(history),
                gauge_value=success_rate,
            )
            self.cards["failures"].set_value(
                format_fixed(failure_rate, suffix="%"),
                detail=f"{snapshot.failed_evaluations} failed evaluations",
                tone="bad" if snapshot.failed_evaluations else "good",
                series=rolling_failure_rate_series(history),
                gauge_value=failure_rate,
            )
        else:
            self.cards["success"].set_value(
                "N/A",
                detail="Not scanned in scalable generation-summary mode",
                tone="neutral",
            )
            self.cards["failures"].set_value(
                "N/A",
                detail="Not scanned in scalable generation-summary mode",
                tone="neutral",
            )

        self._refresh_runtime_cards()

        self.progress_chart.set_history(history, reference.direction)
        self.diversity_chart.set_precomputed_series(
            snapshot.population_diversity
        )
        self.diversity_chart.set_history(history)
        self._update_diversity_meta(snapshot)
        self._sync_expanded_charts()
        self.run_health_panel.render(snapshot, history)
        self.search_behavior_panel.render(snapshot, history)
        self.resources_panel.render(snapshot)

        best_candidate = snapshot.best_candidate_id or "-"
        self.chart_footer.setText(
            f"Best candidate: {best_candidate}   |   "
            f"Problem: {reference.problem_id or 'unknown'}   |   "
            f"Evaluations: {snapshot.evaluation_count:,}"
        )
        self.chart_footer.setToolTip(str(reference.run_root))

    def _refresh_runtime_cards(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return

        elapsed = self._elapsed_seconds(snapshot)
        if elapsed is None:
            self.cards["elapsed"].set_value(
                "N/A",
                detail="Timing data unavailable",
            )
            self._set_timer_display(
                "N/A",
                status="UNAVAILABLE",
                state="unavailable",
            )
            self._render_evaluation_rate(
                throughput=None,
                terminal=False,
            )
            return

        terminal = self._runtime_is_final(snapshot)
        runtime_paused = (
            snapshot.state in self._PAUSED_RUNTIME_STATES
        )

        if terminal:
            elapsed_detail = "Final duration"
            timer_status = "FINAL"
            timer_state = "final"
        elif runtime_paused:
            elapsed_detail = "Paused"
            timer_status = snapshot.state.value.upper()
            timer_state = "paused"
        else:
            elapsed_detail = "Running"
            timer_status = "LIVE"
            timer_state = "running"

        throughput = (
            snapshot.evaluation_count * 60.0 / elapsed
            if elapsed >= 1.0
            else None
        )

        duration_text = self._format_duration(elapsed)
        self.cards["elapsed"].set_value(
            duration_text,
            detail=elapsed_detail,
            tone="good" if terminal else "neutral",
        )
        self._set_timer_display(
            duration_text,
            status=timer_status,
            state=timer_state,
        )
        self._render_evaluation_rate(
            throughput=throughput,
            terminal=terminal,
        )

    def _render_evaluation_rate(
        self,
        *,
        throughput: float | None,
        terminal: bool,
    ) -> None:
        if throughput is None:
            rate_text = "N/A eval/min"
        else:
            rate_text = format_fixed(
                throughput,
                suffix=" eval/min",
            )

        context = "final avg" if terminal else "live avg"
        secondary_line = f"{rate_text} | {context}"

        if self._evaluation_detail_base:
            detail = (
                f"{secondary_line}\n"
                f"{self._evaluation_detail_base}"
            )
        else:
            detail = secondary_line

        self.cards["evaluations"].detail_label.setText(detail)

    @staticmethod
    def _evaluation_target_reached(snapshot: RunSnapshot) -> bool:
        planned = snapshot.planned_evaluations
        return (
            planned is not None
            and planned > 0
            and snapshot.evaluation_count >= planned
        )

    def _capture_runtime_completion(
        self,
        snapshot: RunSnapshot,
    ) -> None:
        if not self._evaluation_target_reached(snapshot):
            return

        run_id = snapshot.reference.run_id
        if run_id in self._runtime_completion_times:
            return

        finished_at = snapshot.run_finished_at
        stop_time = (
            finished_at
            if finished_at is not None
            else time.time()
        )
        self._runtime_completion_times[run_id] = stop_time

    def _runtime_is_final(
        self,
        snapshot: RunSnapshot,
    ) -> bool:
        if self._evaluation_target_reached(snapshot):
            return True
        return snapshot.state in self._TERMINAL_STATES

    def _sync_runtime_pause_state(
        self,
        snapshot: RunSnapshot,
    ) -> None:
        """Track live paused intervals for one observed GOW run.

        PAUSE_REQUESTED deliberately remains active runtime because GOW may
        still be completing the current safe generation boundary.

        PAUSED and RESUMING do not consume active runtime.
        """

        run_id = snapshot.reference.run_id
        now = time.time()

        if snapshot.state in self._PAUSED_RUNTIME_STATES:
            if run_id not in self._runtime_pause_started_at:
                self._runtime_pause_started_at[run_id] = now
            return

        pause_started_at = self._runtime_pause_started_at.pop(
            run_id,
            None,
        )

        if pause_started_at is None:
            return

        if (
            snapshot.state in self._TERMINAL_STATES
            and snapshot.run_finished_at is not None
        ):
            pause_finished_at = snapshot.run_finished_at
        else:
            pause_finished_at = now

        paused_seconds = max(
            0.0,
            pause_finished_at - pause_started_at,
        )

        self._runtime_paused_seconds[run_id] = (
            self._runtime_paused_seconds.get(run_id, 0.0)
            + paused_seconds
        )

    def _elapsed_seconds(
        self,
        snapshot: RunSnapshot,
    ) -> float | None:
        started_at = snapshot.run_started_at
        if started_at is None:
            return None

        self._sync_runtime_pause_state(snapshot)
        self._capture_runtime_completion(snapshot)
        run_id = snapshot.reference.run_id

        if self._evaluation_target_reached(snapshot):
            end_time = self._runtime_completion_times[run_id]
        elif snapshot.state in self._TERMINAL_STATES:
            finished_at = snapshot.run_finished_at
            if finished_at is None:
                return None
            end_time = finished_at
        else:
            end_time = time.time()

        paused_seconds = self._runtime_paused_seconds.get(
            run_id,
            0.0,
        )

        pause_started_at = self._runtime_pause_started_at.get(
            run_id
        )

        if pause_started_at is not None:
            paused_seconds += max(
                0.0,
                end_time - pause_started_at,
            )

        return max(
            0.0,
            end_time - started_at - paused_seconds,
        )

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = max(0, int(seconds))
        days, remainder = divmod(total_seconds, 86_400)
        hours, remainder = divmod(remainder, 3_600)
        minutes, seconds_part = divmod(remainder, 60)

        clock = f"{hours:02d}:{minutes:02d}:{seconds_part:02d}"
        return f"{days}d {clock}" if days else clock

    def _set_timer_display(
        self,
        value: str,
        *,
        status: str,
        state: str,
    ) -> None:
        self.timer_value_label.setText(value)
        self.timer_status_label.setText(status)
        self.timer_panel.setProperty("timerState", state)
        self.timer_panel.style().unpolish(self.timer_panel)
        self.timer_panel.style().polish(self.timer_panel)
        self.timer_status_label.style().unpolish(
            self.timer_status_label
        )
        self.timer_status_label.style().polish(
            self.timer_status_label
        )

    def _selected_window_size(self) -> int | None:
        value = self.window_selector.currentData()
        return int(value) if value is not None else None

    def _update_diversity_meta(
        self,
        snapshot: RunSnapshot,
    ) -> None:
        samples = snapshot.population_diversity
        if not samples:
            self.diversity_meta_label.setText(
                "Awaiting population data"
            )
            return

        latest = samples[-1]
        self.diversity_meta_label.setText(
            f"Gen {latest.generation_id} | "
            f"Pop {latest.population_size} | "
            f"Dims {latest.active_dimensions}"
        )

    def _open_progress_chart(
        self,
        checked: bool = False,
    ) -> None:
        del checked
        if self._expanded_progress_chart is None:
            self._expanded_progress_chart = ObjectiveProgressChart()
            self._expanded_progress_chart.set_interactive_navigation(
                True
            )
            self._progress_dialog = ChartDialog(
                "Optimization progress",
                self._expanded_progress_chart,
                footer_text=(
                    "X axis — Evaluation: sequential objective evaluations. "
                    "Y axis — Objective value: best-so-far, cumulative median "
                    "and cumulative mean. A logarithmic Y scale is selected "
                    "automatically for positive high-dynamic-range data."
                ),
                parent=self,
            )
        self._sync_expanded_charts()
        assert self._progress_dialog is not None
        self._progress_dialog.present()

    def _open_diversity_chart(
        self,
        checked: bool = False,
    ) -> None:
        del checked
        if self._expanded_diversity_chart is None:
            self._expanded_diversity_chart = (
                PopulationDiversityChart()
            )
            self._expanded_diversity_chart.set_interactive_navigation(
                True
            )
            self._diversity_dialog = ChartDialog(
                "Population diversity",
                self._expanded_diversity_chart,
                footer_text=(
                    "X axis — Generation: optimization population generation. "
                    "Y axis — Diversity metric: Spread is the sum of marginal "
                    "sample standard deviations in normalized parameter space; "
                    "Ellipse is the area of the 95% confidence ellipse in the "
                    "two dominant PCA directions."
                ),
                parent=self,
            )
        self._sync_expanded_charts()
        assert self._diversity_dialog is not None
        self._diversity_dialog.present()

    def _sync_expanded_charts(self) -> None:
        window_size = self._selected_window_size()

        if self._expanded_progress_chart is not None:
            direction = (
                self._snapshot.reference.direction
                if self._snapshot is not None
                else ObjectiveDirection.UNKNOWN
            )
            self._expanded_progress_chart.set_history(
                self._history,
                direction,
            )
            self._expanded_progress_chart.set_window_size(
                window_size
            )

        if self._expanded_diversity_chart is not None:
            precomputed = (
                self._snapshot.population_diversity
                if self._snapshot is not None
                else ()
            )
            self._expanded_diversity_chart.set_precomputed_series(
                precomputed
            )
            self._expanded_diversity_chart.set_history(
                self._history
            )
            self._expanded_diversity_chart.set_window_size(
                window_size
            )

    def render_resource_snapshot(
        self,
        snapshot: SystemResourceSnapshot,
        history: tuple[SystemResourceSnapshot, ...] = (),
    ) -> None:
        self.resources_panel.render_system(snapshot, history)

    def render_resource_error(self, message: str) -> None:
        self.resources_panel.render_system_error(message)

    def render_gow_resource_snapshot(
        self,
        snapshot: GowProcessResourceSnapshot,
        history: tuple[GowProcessResourceSnapshot, ...] = (),
    ) -> None:
        self.resources_panel.render_gow_process(snapshot, history)

    def render_gow_resource_error(self, message: str) -> None:
        self.resources_panel.render_gow_process_error(message)

    def clear(self, message: str = "No GOW run connected") -> None:
        self._snapshot = None
        self._history = ()
        self._evaluation_detail_base = ""
        for card in self.cards.values():
            card.set_value("-", detail="Waiting for GOW artifacts")
        self._set_timer_display(
            "--:--:--",
            status="WAITING",
            state="waiting",
        )
        self._update_objective_precision_controls()
        self.progress_chart.set_history((), ObjectiveDirection.UNKNOWN)
        self.diversity_chart.set_precomputed_series(())
        self.diversity_chart.set_history(())
        self.diversity_meta_label.setText("Awaiting population data")
        self._sync_expanded_charts()
        self.run_health_panel.clear()
        self.search_behavior_panel.clear()
        self.resources_panel.clear()
        self.chart_footer.setText(message)

    def _window_selection_changed(self, index: int) -> None:
        window_size = self.window_selector.itemData(index)
        if window_size is not None:
            window_size = int(window_size)
        self.progress_chart.set_window_size(window_size)
        self.diversity_chart.set_window_size(window_size)
        self._sync_expanded_charts()
