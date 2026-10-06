from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gow_monitor.domain import EvaluationPoint, RunSnapshot, RunState
from gow_monitor.ui.dashboard_metrics import recent_improvement_percent
from gow_monitor.ui.formatting import format_fixed


class _HealthRow(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("healthRow")
        self.setProperty("severity", "info")
        self.setMaximumHeight(45)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(7, 3, 7, 3)
        layout.setSpacing(5)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(1)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("healthTitle")
        self.detail_label = QLabel("-")
        self.detail_label.setObjectName("healthDetail")
        self.detail_label.setWordWrap(True)

        self.severity_label = QLabel("INFO")
        self.severity_label.setObjectName("healthSeverity")
        self.severity_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.severity_label.setMinimumWidth(52)

        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.detail_label)
        layout.addLayout(text_layout, 1)
        layout.addWidget(self.severity_label)

    def update_status(self, detail: str, severity: str) -> None:
        self.detail_label.setText(detail)
        self.severity_label.setText(severity.upper())
        self.setProperty("severity", severity)
        self.style().unpolish(self)
        self.style().polish(self)


class RunHealthPanel(QFrame):
    """Compact operational diagnostics derived only from observed artifacts."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("runHealthPanel")
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 5, 7, 5)
        layout.setSpacing(3)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)

        title = QLabel("Run health")
        title.setObjectName("panelTitle")
        self.summary_label = QLabel("NO DATA")
        self.summary_label.setObjectName("healthSummary")
        self.summary_label.setProperty("healthState", "unknown")
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        header_layout.addWidget(title)
        header_layout.addStretch(1)
        header_layout.addWidget(self.summary_label)

        self.rows = {
            "stream": _HealthRow("Data stream"),
            "failures": _HealthRow("Failure rate"),
            "progress": _HealthRow("Recent progress"),
            "fireworks": _HealthRow("FireWorks / HTC"),
            "archive": _HealthRow("Artifact archive"),
            "state": _HealthRow("Observable state"),
        }

        layout.addLayout(header_layout)
        for row in self.rows.values():
            layout.addWidget(row)

        self.clear()

    def render(
        self,
        snapshot: RunSnapshot,
        history: tuple[EvaluationPoint, ...],
    ) -> None:
        valid_count = snapshot.successful_evaluations
        if not snapshot.quality_metrics_complete:
            self.rows["stream"].update_status(
                "Generation-summary mode: objective quality counts are not scanned.",
                "info",
            )
            failure_severity = "info"
            self.rows["failures"].update_status(
                "N/A in scalable summary mode; use --data-mode full for exact counts.",
                "info",
            )
        else:
            if valid_count:
                self.rows["stream"].update_status(
                    f"{valid_count} valid objectives available.",
                    "good",
                )
            else:
                self.rows["stream"].update_status(
                    "No valid objective has been observed.",
                    "critical",
                )

            failure_rate = snapshot.failure_rate * 100.0
            if snapshot.failed_evaluations == 0:
                failure_severity = "good"
            elif failure_rate >= 10.0:
                failure_severity = "critical"
            else:
                failure_severity = "warning"
            self.rows["failures"].update_status(
                (
                    f"{format_fixed(failure_rate, suffix='%')} "
                    f"({snapshot.failed_evaluations} failed)."
                ),
                failure_severity,
            )

        improvement = recent_improvement_percent(
            history,
            snapshot.reference.direction,
        )
        if improvement is None:
            self.rows["progress"].update_status(
                "Recent improvement is not available yet.",
                "info",
            )
        elif improvement > 0.0:
            self.rows["progress"].update_status(
                (
                    f"Best improved {format_fixed(improvement, suffix='%')} "
                    "over the recent monitored history."
                ),
                "good",
            )
        else:
            self.rows["progress"].update_status(
                "No incumbent improvement in the recent monitored history.",
                "warning",
            )

        if snapshot.fireworks_total:
            fw_severity = (
                "critical"
                if snapshot.launcher_inspection_errors
                else "warning"
                if snapshot.fireworks_fizzled
                else "good"
            )
            self.rows["fireworks"].update_status(
                (
                    f"{snapshot.fireworks_completed} completed, "
                    f"{snapshot.fireworks_fizzled} fizzled, "
                    f"{snapshot.fireworks_archived} archived / "
                    f"{snapshot.fireworks_total} FW; "
                    f"{snapshot.launches_total} launch(es)."
                ),
                fw_severity,
            )
        else:
            self.rows["fireworks"].update_status(
                "No generation launch manifest is available yet.",
                "info",
            )

        archived_total = (
            snapshot.archived_candidate_generations
            + snapshot.archived_launcher_generations
        )
        if archived_total or snapshot.final_launcher_archive:
            final_text = (
                "yes" if snapshot.final_launcher_archive else "no"
            )
            self.rows["archive"].update_status(
                (
                    f"candidate generations={snapshot.archived_candidate_generations}; "
                    f"launcher generations={snapshot.archived_launcher_generations}; "
                    f"final launcher archive={final_text}."
                ),
                "good",
            )
        else:
            self.rows["archive"].update_status(
                "No cold-storage generation archives observed yet.",
                "info",
            )

        state_severity = self._state_severity(snapshot.state)
        self.rows["state"].update_status(
            (
                f"{snapshot.state.value}; {snapshot.result_sources} source(s); "
                f"mode={snapshot.artifact_mode}."
            ),
            state_severity,
        )

        if snapshot.launcher_inspection_errors:
            summary, summary_state = "ATTENTION", "attention"
        elif snapshot.quality_metrics_complete and (
            valid_count == 0 or failure_severity == "critical"
        ):
            summary, summary_state = "ATTENTION", "attention"
        elif snapshot.quality_metrics_complete and snapshot.failed_evaluations:
            summary, summary_state = "CHECK", "attention"
        else:
            summary, summary_state = "HEALTHY", "healthy"
        self._set_summary(summary, summary_state)

    def clear(self) -> None:
        for row in self.rows.values():
            row.update_status("Waiting for GOW artifacts.", "info")
        self._set_summary("NO DATA", "unknown")

    @staticmethod
    def _state_severity(state: RunState) -> str:
        if state in {RunState.RUNNING, RunState.COMPLETED}:
            return "good"
        if state in {RunState.WAITING, RunState.STOPPING}:
            return "warning"
        if state in {RunState.FAILED, RunState.STOPPED}:
            return "critical"
        return "info"

    def _set_summary(self, text: str, state: str) -> None:
        self.summary_label.setText(text)
        self.summary_label.setProperty("healthState", state)
        self.summary_label.style().unpolish(self.summary_label)
        self.summary_label.style().polish(self.summary_label)
