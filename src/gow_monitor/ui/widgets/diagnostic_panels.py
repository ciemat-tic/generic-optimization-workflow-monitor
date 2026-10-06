from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gow_monitor.domain import (
    EvaluationPoint,
    GowProcessResourceSnapshot,
    RunSnapshot,
    SystemResourceSnapshot,
)
from gow_monitor.ui.dashboard_metrics import (
    objective_series,
    objective_variability_percent,
    recent_improvement_percent,
    recent_improvement_series,
    rolling_valid_rate_series,
)
from gow_monitor.ui.formatting import format_binary_bytes, format_fixed
from gow_monitor.ui.widgets.semicircle_gauge import SemicircleGauge
from gow_monitor.ui.widgets.sparkline import SparklineWidget


class _MetricTile(QFrame):
    def __init__(
        self,
        title: str,
        *,
        visual: str = "sparkline",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if visual not in {"sparkline", "gauge"}:
            raise ValueError("visual must be 'sparkline' or 'gauge'")

        self.setObjectName("metricTile")
        self.setProperty("metricTone", "neutral")
        self.setProperty("metricVisual", visual)
        self.setMinimumHeight(70)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.MinimumExpanding,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(1)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("metricTitle")

        value_row = QHBoxLayout()
        value_row.setContentsMargins(0, 0, 0, 0)
        value_row.setSpacing(4)

        self.value_label = QLabel("N/A")
        self.value_label.setObjectName("metricValue")

        self.sparkline: SparklineWidget | None = None
        self.gauge: SemicircleGauge | None = None
        if visual == "gauge":
            self.gauge = SemicircleGauge()
            self.gauge.setMaximumWidth(72)
            visual_widget: QWidget = self.gauge
        else:
            self.sparkline = SparklineWidget()
            self.sparkline.setMaximumWidth(72)
            visual_widget = self.sparkline

        value_row.addWidget(self.value_label, 1)
        value_row.addWidget(visual_widget)

        self.detail_label = QLabel("Not available")
        self.detail_label.setObjectName("metricDetail")
        self.detail_label.setWordWrap(True)
        self.detail_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignTop
        )
        self.detail_label.setMinimumHeight(20)
        self.detail_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.MinimumExpanding,
        )

        layout.addWidget(self.title_label)
        layout.addLayout(value_row)
        layout.addWidget(self.detail_label)

    def set_metric(
        self,
        value: str,
        *,
        detail: str,
        tone: str = "neutral",
        series: Iterable[float] = (),
        gauge_value: float | None = None,
    ) -> None:
        self.value_label.setText(value)
        self.detail_label.setText(detail)
        if self.sparkline is not None:
            self.sparkline.set_values(series, tone=tone)
        if self.gauge is not None:
            self.gauge.set_value(gauge_value, tone=tone)
        self.setProperty("metricTone", tone)
        self.style().unpolish(self)
        self.style().polish(self)


class _MetricPanel(QFrame):
    """Metric-card panel with a width-aware responsive grid."""

    def __init__(
        self,
        title: str,
        metric_specs: tuple[tuple[str, str, str], ...],
        parent: QWidget | None = None,
        *,
        preferred_columns: int = 2,
        compact_breakpoint_px: int = 560,
        single_breakpoint_px: int = 340,
    ) -> None:
        super().__init__(parent)

        if preferred_columns < 1:
            raise ValueError("preferred_columns must be positive")
        if single_breakpoint_px < 1:
            raise ValueError("single_breakpoint_px must be positive")
        if compact_breakpoint_px <= single_breakpoint_px:
            raise ValueError(
                "compact_breakpoint_px must exceed single_breakpoint_px"
            )

        self.setObjectName("metricPanel")
        self.setMinimumHeight(130)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        self._preferred_columns = preferred_columns
        self._compact_breakpoint_px = compact_breakpoint_px
        self._single_breakpoint_px = single_breakpoint_px
        self._current_columns = 0
        self._tile_order: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(5)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)

        title_label = QLabel(title)
        title_label.setObjectName("panelTitle")

        self.availability_label = QLabel("ARTIFACT DATA")
        self.availability_label.setObjectName("availabilityBadge")
        self.availability_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        header.addWidget(title_label)
        header.addStretch(1)
        header.addWidget(self.availability_label)

        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(5)
        self.grid.setVerticalSpacing(5)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.tiles: dict[str, _MetricTile] = {}

        for key, metric_title, visual in metric_specs:
            tile = _MetricTile(metric_title, visual=visual)
            self.tiles[key] = tile
            self._tile_order.append(key)

        self._reflow_tiles(self._preferred_columns)

        layout.addLayout(header)
        layout.addLayout(self.grid)

    @property
    def column_count(self) -> int:
        return self._current_columns

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)

        columns = self._columns_for_width(event.size().width())
        self._reflow_tiles(columns)

    def _columns_for_width(self, width: int) -> int:
        if width < self._single_breakpoint_px:
            return 1

        if (
            self._preferred_columns > 2
            and width < self._compact_breakpoint_px
        ):
            return 2

        return self._preferred_columns

    def _reflow_tiles(self, columns: int) -> None:
        columns = max(1, min(columns, self._preferred_columns))

        if columns == self._current_columns:
            return

        while self.grid.count():
            self.grid.takeAt(0)

        maximum_rows = len(self._tile_order)

        for row in range(maximum_rows):
            self.grid.setRowMinimumHeight(row, 0)
            self.grid.setRowStretch(row, 0)

        for column in range(self._preferred_columns):
            self.grid.setColumnStretch(column, 0)

        for index, key in enumerate(self._tile_order):
            row = index // columns
            column = index % columns
            self.grid.addWidget(self.tiles[key], row, column)

        for column in range(columns):
            self.grid.setColumnStretch(column, 1)

        row_count = (
            len(self._tile_order) + columns - 1
        ) // columns

        tile_height = 70
        vertical_spacing = 5
        header_and_margins = 40

        for row in range(row_count):
            self.grid.setRowMinimumHeight(row, tile_height)

        required_height = (
            header_and_margins
            + row_count * tile_height
            + max(0, row_count - 1) * vertical_spacing
        )

        self.setMinimumHeight(required_height)
        self._current_columns = columns
        self.updateGeometry()

    def clear(self) -> None:
        for tile in self.tiles.values():
            tile.set_metric(
                "N/A",
                detail="Waiting for GOW artifacts",
            )


class SearchBehaviorPanel(_MetricPanel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Search behavior",
            (
                ("variability", "Objective variability", "sparkline"),
                ("improvement", "Recent improvement", "sparkline"),
                ("valid_rate", "Valid rate", "gauge"),
                ("dimensions", "Effective dimensions", "sparkline"),
            ),
            parent,
            preferred_columns=3,
            compact_breakpoint_px=460,
            single_breakpoint_px=300,
        )

        # Preserve the compatibility tile object but remove the empty slot
        # while effective dimensionality is unavailable.
        self.tiles["dimensions"].setVisible(False)
        self._tile_order.remove("dimensions")
        self._current_columns = 0
        self._reflow_tiles(self._preferred_columns)

        variability_explanation = (
            "CV = 100 x population standard deviation / absolute mean. "
            "The current window contains valid objectives found within the "
            "latest 100 evaluations."
        )
        self.tiles["variability"].setToolTip(variability_explanation)
        self.tiles["variability"].title_label.setToolTip(
            variability_explanation
        )

    def render(
        self,
        snapshot: RunSnapshot,
        history: tuple[EvaluationPoint, ...],
    ) -> None:
        variability = objective_variability_percent(history)
        self.tiles["variability"].set_metric(
            format_fixed(variability, suffix="%") if variability is not None else "N/A",
            detail=(
                "CV over recent generation checkpoints"
                if snapshot.artifact_mode == "generation-summary"
                else "CV over the latest 100 evaluations"
            ),
            tone=(
                "warning"
                if variability is not None and variability > 100
                else "neutral"
            ),
            series=objective_series(history),
        )

        improvement = recent_improvement_percent(
            history,
            snapshot.reference.direction,
        )
        self.tiles["improvement"].set_metric(
            format_fixed(improvement, suffix="%") if improvement is not None else "N/A",
            detail=(
                "Best-so-far change, recent generation checkpoints"
                if snapshot.artifact_mode == "generation-summary"
                else "Best-so-far change, latest 100 evaluations"
            ),
            tone=(
                "good"
                if improvement is not None and improvement > 0
                else "neutral"
            ),
            series=recent_improvement_series(
                history,
                snapshot.reference.direction,
            ),
        )

        if snapshot.quality_metrics_complete:
            valid_rate = snapshot.success_rate * 100.0
            self.tiles["valid_rate"].set_metric(
                format_fixed(valid_rate, suffix="%"),
                detail=f"{snapshot.successful_evaluations} valid objective values",
                tone="good" if snapshot.successful_evaluations else "warning",
                series=rolling_valid_rate_series(history),
                gauge_value=valid_rate,
            )
        else:
            self.tiles["valid_rate"].set_metric(
                "N/A",
                detail="Exact validity rate requires --data-mode full",
                tone="neutral",
            )

        self.tiles["dimensions"].set_metric(
            "N/A",
            detail="Parameter dimensionality unavailable",
        )


class ResourcesPanel(_MetricPanel):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        preferred_columns: int = 3,
        compact_breakpoint_px: int = 560,
        single_breakpoint_px: int = 360,
    ) -> None:
        super().__init__(
            "Resources utilization",
            (
                ("cpu", "Host CPU utilization", "gauge"),
                ("memory", "Host RAM utilization", "gauge"),
                ("cores", "Host active CPU cores", "sparkline"),
                ("gow_memory", "GOW RAM (RSS)", "sparkline"),
                ("sources", "Artifact sources", "sparkline"),
            ),
            parent,
            preferred_columns=preferred_columns,
            compact_breakpoint_px=compact_breakpoint_px,
            single_breakpoint_px=single_breakpoint_px,
        )
        self.availability_label.setText("HOST + GOW + ARTIFACT")

    def render(self, snapshot: RunSnapshot) -> None:
        self.tiles["sources"].set_metric(
            str(snapshot.result_sources),
            detail=(
                f"Artifact mode: {snapshot.artifact_mode}; "
                "generation summaries/shards feeding the monitor"
            ),
            tone="good" if snapshot.result_sources else "warning",
        )

    def render_system(
        self,
        snapshot: SystemResourceSnapshot,
        history: tuple[SystemResourceSnapshot, ...] = (),
    ) -> None:
        physical = (
            str(snapshot.physical_cores)
            if snapshot.physical_cores is not None
            else "N/A"
        )
        cpu_series = tuple(item.cpu_percent for item in history)
        memory_series = tuple(item.memory_percent for item in history)
        core_series = tuple(
            float(item.active_logical_cores)
            for item in history
        )

        self.tiles["cpu"].set_metric(
            format_fixed(snapshot.cpu_percent, suffix="%"),
            detail=(
                f"{snapshot.active_logical_cores}/{snapshot.logical_cores} "
                "logical cores active at >= "
                f"{format_fixed(snapshot.active_core_threshold_percent, suffix='%')}"
            ),
            tone=self._utilization_tone(snapshot.cpu_percent),
            series=cpu_series,
            gauge_value=snapshot.cpu_percent,
        )
        sampled_at = datetime.fromtimestamp(
            snapshot.captured_at
        ).strftime("%H:%M:%S")
        self.tiles["memory"].set_metric(
            format_fixed(snapshot.memory_percent, suffix="%"),
            detail=(
                f"{self._format_bytes(snapshot.memory_used_bytes)} / "
                f"{self._format_bytes(snapshot.memory_total_bytes)} "
                f"| sample {sampled_at}"
            ),
            tone=self._utilization_tone(snapshot.memory_percent),
            series=memory_series,
            gauge_value=snapshot.memory_percent,
        )
        self.tiles["cores"].set_metric(
            f"{snapshot.active_logical_cores} / {snapshot.logical_cores}",
            detail=(
                f"{physical} physical cores; "
                "hover for per-core percentages"
            ),
            tone=(
                "warning"
                if snapshot.active_logical_cores == snapshot.logical_cores
                else "neutral"
            ),
            series=core_series,
        )
        per_core_details = "\n".join(
            (
                f"Logical core {index}: "
                f"{format_fixed(value, suffix='%')}"
            )
            for index, value in enumerate(snapshot.per_core_percent)
        )
        self.tiles["cores"].setToolTip(
            per_core_details or "Per-core percentages are unavailable"
        )

    def render_gow_process(
        self,
        snapshot: GowProcessResourceSnapshot,
        history: tuple[GowProcessResourceSnapshot, ...] = (),
    ) -> None:
        if not snapshot.available:
            self.tiles["gow_memory"].set_metric(
                "N/A",
                detail=snapshot.status,
            )
            return

        assert snapshot.memory_rss_bytes is not None
        memory_series = tuple(
            float(item.memory_rss_bytes)
            for item in history
            if item.available and item.memory_rss_bytes is not None
        )
        root_name = snapshot.root_name or "unknown process"

        sampled_at = datetime.fromtimestamp(
            snapshot.captured_at
        ).strftime("%H:%M:%S")
        self.tiles["gow_memory"].set_metric(
            self._format_bytes(snapshot.memory_rss_bytes),
            detail=(
                f"Aggregated RSS for {root_name} "
                f"| PID {snapshot.root_pid} "
                f"| sample {sampled_at}"
            ),
            tone="neutral",
            series=memory_series,
        )

    def render_gow_process_error(self, message: str) -> None:
        self.tiles["gow_memory"].set_metric(
            "N/A",
            detail=(
                message
                or "GOW memory telemetry is temporarily unavailable"
            ),
            tone="warning",
        )

    def render_system_error(self, message: str) -> None:
        for key in ("cpu", "memory", "cores"):
            self.tiles[key].set_metric(
                "N/A",
                detail=message or "Host telemetry is temporarily unavailable",
                tone="warning",
            )

    @staticmethod
    def _format_bytes(value: int) -> str:
        return format_binary_bytes(value)

    @staticmethod
    def _utilization_tone(value: float) -> str:
        if value >= 90.0:
            return "bad"
        if value >= 75.0:
            return "warning"
        return "good"
