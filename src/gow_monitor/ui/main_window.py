from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gow_monitor.domain import (
    EvaluationPoint,
    GowProcessResourceSnapshot,
    RunSnapshot,
    RunState,
    SystemResourceSnapshot,
)
from gow_monitor.infrastructure import GowFilesystemRunReader
from gow_monitor.ui.main_viewmodel import MainViewModel
from gow_monitor.ui.pages import OverviewPage
from gow_monitor.ui.widgets.header_widget import Header
from gow_monitor.ui.widgets.sidebar_widget import (
    NAVIGATION_ITEMS,
    Sidebar,
)


class MainWindow(QMainWindow):
    """Top-level desktop composition and ViewModel binding."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        gow_pid: int | None = None,
        data_mode: str = "auto",
    ) -> None:
        super().__init__(parent)

        self.setWindowTitle("GOW Monitor")
        self.resize(1440, 900)
        self.setMinimumSize(1280, 760)

        self.viewmodel = MainViewModel(
            self,
            gow_pid=gow_pid,
            data_mode=data_mode,
        )

        self.page_titles: dict[str, QLabel] = {}
        self.placeholder_labels: dict[str, QLabel] = {}

        self._build_ui()
        self._install_compatibility_aliases()
        self._connect_viewmodel()
        self._apply_style()

        self._render_no_run()
        self.select_page("Overview")

    # ------------------------------------------------------------------
    # Compatibility facade
    # ------------------------------------------------------------------

    def _install_compatibility_aliases(self) -> None:
        """Keep the existing MainWindow contract during the MVVM migration."""
        self.live_refresh = self.viewmodel.live_refresh
        self.resource_monitor = self.viewmodel.resource_monitor
        self.navigation_buttons = self.sidebar.navigation_buttons

        self.run_selector = self.header.run_selector
        self.job_label = self.header.job_label
        self.open_results_button = self.header.open_results_button
        self.pause_button = self.header.pause_button
        self.continue_button = self.header.continue_button
        self.auto_refresh_label = self.header.auto_refresh_label
        self.state_label = self.header.state_label

        self.brand_label = self.sidebar.brand_label
        self.sidebar_run_id = self.sidebar.sidebar_run_id
        self.sidebar_problem = self.sidebar.sidebar_problem
        self.sidebar_evaluations = self.sidebar.sidebar_evaluations
        self.sidebar_failures = self.sidebar.sidebar_failures
        self.version_label = self.sidebar.version_label

        self._resource_history = self.viewmodel.resource_history
        self._gow_resource_history = self.viewmodel.gow_resource_history

    @property
    def _reader(self) -> GowFilesystemRunReader | None:
        return self.viewmodel.reader

    @_reader.setter
    def _reader(self, value: GowFilesystemRunReader | None) -> None:
        self.viewmodel._reader = value

    @property
    def _snapshots(self) -> tuple[RunSnapshot, ...]:
        return self.viewmodel.snapshots

    @_snapshots.setter
    def _snapshots(self, value: tuple[RunSnapshot, ...]) -> None:
        self.viewmodel._snapshots = value

    @property
    def _histories(
        self,
    ) -> dict[str, tuple[EvaluationPoint, ...]]:
        return self.viewmodel.histories

    @_histories.setter
    def _histories(
        self,
        value: dict[str, tuple[EvaluationPoint, ...]],
    ) -> None:
        self.viewmodel._histories = value

    @property
    def _connected_path(self) -> Path | None:
        return self.viewmodel.connected_path

    @_connected_path.setter
    def _connected_path(self, value: Path | None) -> None:
        self.viewmodel._connected_path = value

    @property
    def current_snapshot(self) -> RunSnapshot | None:
        return self.viewmodel.get_snapshot_by_index(
            self.run_selector.currentIndex()
        )

    @property
    def current_history(self) -> tuple[EvaluationPoint, ...]:
        snapshot = self.current_snapshot
        if snapshot is None:
            return ()
        return self.viewmodel.get_history_for_run(
            snapshot.reference.run_id
        )

    # ------------------------------------------------------------------
    # UI composition
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget(self)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.sidebar = Sidebar()
        self.sidebar.setVisible(False)
        root_layout.addWidget(self.sidebar)

        content = QWidget()
        content.setObjectName("content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 7, 12, 8)
        content_layout.setSpacing(6)

        self.header = Header()
        content_layout.addWidget(self.header)

        self.results_root_label = QLabel(
            "No GOW results directory selected"
        )
        self.results_root_label.setObjectName("resultsRoot")
        self.results_root_label.setMaximumHeight(16)
        self.results_root_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        content_layout.addWidget(self.results_root_label)

        self.stack = QStackedWidget()

        self.overview_page = OverviewPage()
        self.page_titles["Overview"] = self.overview_page.title_label

        self.overview_page.install_run_controls(
            self.header.pause_button,
            self.header.continue_button,
        )

        self.overview_scroll = QScrollArea()
        self.overview_scroll.setObjectName("overviewScroll")
        self.overview_scroll.setWidgetResizable(True)
        self.overview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.overview_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.overview_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.overview_scroll.setWidget(self.overview_page)
        self.overview_scroll.viewport().setObjectName(
            "overviewScrollViewport"
        )
        self.stack.addWidget(self.overview_scroll)

        for key, title in NAVIGATION_ITEMS:
            if key == "Overview":
                continue
            self.stack.addWidget(
                self._create_placeholder_page(title, key)
            )

        content_layout.addWidget(self.stack, 1)
        root_layout.addWidget(content, 1)
        self.setCentralWidget(root)

    def _create_placeholder_page(
        self,
        title: str,
        key: str,
    ) -> QWidget:
        page = QWidget()
        page.setObjectName("page")

        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 12, 6, 6)
        layout.setSpacing(14)

        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        self.page_titles[key] = title_label

        description = QLabel(
            "Independent desktop monitoring framework. "
            "The application reads GOW artifacts through filesystem adapters "
            "and does not import GOW internals."
        )
        description.setObjectName("description")
        description.setWordWrap(True)

        panel = QFrame()
        panel.setObjectName("emptyPanel")
        panel_layout = QVBoxLayout(panel)

        placeholder = QLabel("No GOW run connected")
        placeholder.setObjectName("placeholder")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setWordWrap(True)
        self.placeholder_labels[key] = placeholder
        panel_layout.addWidget(placeholder)

        layout.addWidget(title_label)
        layout.addWidget(description)
        layout.addWidget(panel, 1)

        return page

    def _connect_viewmodel(self) -> None:
        vm = self.viewmodel

        vm.monitor_data_changed.connect(
            self._on_monitor_data_changed
        )
        vm.refresh_status_changed.connect(
            self.header.set_refresh_status
        )
        vm.run_state_changed.connect(self.header.set_run_state)

        vm.system_resource_updated.connect(
            self.overview_page.render_resource_snapshot
        )
        vm.resource_error.connect(
            self.overview_page.render_resource_error
        )
        vm.gow_resource_updated.connect(
            self.overview_page.render_gow_resource_snapshot
        )
        vm.gow_resource_error.connect(
            self.overview_page.render_gow_resource_error
        )

        self.header.open_results_clicked.connect(
            self._choose_results_root
        )
        self.header.pause_clicked.connect(
            self._request_pause_current_run
        )
        self.header.continue_clicked.connect(
            self._request_resume_current_run
        )
        self.header.run_selected.connect(self._on_run_selected)
        self.sidebar.page_selected.connect(self.select_page)

    # ------------------------------------------------------------------
    # Navigation and data connection
    # ------------------------------------------------------------------

    def select_page(self, key: str) -> None:
        keys = [item[0] for item in NAVIGATION_ITEMS]
        if key not in keys:
            raise KeyError(f"Unknown monitor page: {key}")

        self.stack.setCurrentIndex(keys.index(key))
        self.sidebar.set_active_page(key)

    @staticmethod
    def _load_monitor_data(
        results_root: str | Path,
        *,
        data_mode: str = "auto",
    ) -> tuple[
        GowFilesystemRunReader,
        tuple[RunSnapshot, ...],
        dict[str, tuple[EvaluationPoint, ...]],
    ]:
        reader = GowFilesystemRunReader(
            results_root,
            data_mode=data_mode,
        )
        snapshots: list[RunSnapshot] = []
        histories: dict[str, tuple[EvaluationPoint, ...]] = {}

        for reference in reader.discover_runs():
            snapshot, history = reader.snapshot_and_history_of(
                reference
            )
            snapshots.append(snapshot)
            histories[reference.run_id] = history

        return reader, tuple(snapshots), histories

    def connect_results_root(
        self,
        results_root: str | Path,
    ) -> None:
        reader, snapshots, histories = self._load_monitor_data(
            results_root,
            data_mode=self.viewmodel.data_mode,
        )
        self.viewmodel.apply_loaded_data(
            reader,
            snapshots,
            histories,
        )

    def connect_results_root_async(
        self,
        results_root: str | Path,
    ) -> None:
        selected_path = Path(results_root).expanduser().resolve()

        self.header.clear_runs()
        self.results_root_label.setText(str(selected_path))
        self.results_root_label.setToolTip(str(selected_path))
        self._render_no_run(
            "Loading GOW artifacts in the background. "
            "Live resource telemetry remains available."
        )

        self.viewmodel.connect_results_root_async(selected_path)

    def refresh_connected_results(self) -> bool:
        return self.viewmodel.refresh_connected_results()

    def _request_pause_current_run(self) -> None:
        snapshot = self.current_snapshot

        if snapshot is None:
            return

        try:
            self.viewmodel.request_pause(
                snapshot
            )
        except (
            ValueError,
            RuntimeError,
            OSError,
        ) as exc:
            QMessageBox.warning(
                self,
                "Unable to pause GOW run",
                str(exc),
            )

    def _request_resume_current_run(self) -> None:
        snapshot = self.current_snapshot

        if snapshot is None:
            return

        try:
            self.viewmodel.request_resume(
                snapshot
            )
        except (
            ValueError,
            RuntimeError,
            OSError,
        ) as exc:
            QMessageBox.warning(
                self,
                "Unable to continue GOW run",
                str(exc),
            )

    def _choose_results_root(self) -> None:
        initial = (
            str(self.viewmodel.reader.selected_path)
            if self.viewmodel.reader is not None
            else str(Path.home())
        )

        selected = QFileDialog.getExistingDirectory(
            self,
            "Select GOW results directory",
            initial,
        )
        if not selected:
            return

        try:
            self.connect_results_root(selected)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Unable to read GOW results",
                str(exc),
            )

    # ------------------------------------------------------------------
    # ViewModel -> view rendering
    # ------------------------------------------------------------------

    def _on_monitor_data_changed(self) -> None:
        snapshots = self.viewmodel.snapshots
        reader = self.viewmodel.reader

        previous_run_id = self.header.current_run_id()
        selected_index = 0

        if previous_run_id is not None:
            for index, snapshot in enumerate(snapshots):
                if snapshot.reference.run_id == previous_run_id:
                    selected_index = index
                    break

        self.header.populate_runs(snapshots, selected_index)

        if reader is not None:
            self.results_root_label.setText(str(reader.results_root))
            self.results_root_label.setToolTip(
                str(reader.selected_path)
            )
        elif self.viewmodel.connected_path is not None:
            path = self.viewmodel.connected_path
            self.results_root_label.setText(str(path))
            self.results_root_label.setToolTip(str(path))

        if snapshots:
            self._render_snapshot(snapshots[selected_index])
        else:
            self._render_no_run(
                "No runs found yet. Auto-refresh is watching the selected "
                "directory for new GOW artifacts."
            )

    def _on_run_selected(self, index: int) -> None:
        snapshot = self.viewmodel.get_snapshot_by_index(index)
        if snapshot is not None:
            self._render_snapshot(snapshot)

    def _selected_run_changed(self, index: int) -> None:
        self._on_run_selected(index)

    def _render_snapshot(self, snapshot: RunSnapshot) -> None:
        reference = snapshot.reference

        self.header.set_job_title(
            f"Job: {reference.run_id}"
        )
        self.viewmodel.run_state_changed.emit(snapshot.state)

        if snapshot.planned_evaluations is None:
            evaluations_text = f"{snapshot.evaluation_count:,}"
        else:
            evaluations_text = (
                f"{snapshot.evaluation_count:,} / "
                f"{snapshot.planned_evaluations:,}"
            )

        self.sidebar.set_run_info(
            reference.run_id,
            reference.problem_id or "unknown",
            evaluations_text,
            (
                str(snapshot.failed_evaluations)
                if snapshot.quality_metrics_complete
                else "N/A"
            ),
        )

        if self.viewmodel.reader is not None:
            self.viewmodel.watch_gow_run(
                results_root=self.viewmodel.reader.results_root,
                run_id=reference.run_id,
            )

        history = self.viewmodel.get_history_for_run(
            reference.run_id
        )
        self.overview_page.render_snapshot(snapshot, history)

        for key, placeholder in self.placeholder_labels.items():
            placeholder.setText(
                f"{reference.run_id} connected.\n"
                f"The {key} view will use this run."
            )

    def _render_no_run(
        self,
        message: str = "No GOW run connected",
    ) -> None:
        self.header.set_job_title("Job: No run selected")
        self.viewmodel.run_state_changed.emit(RunState.UNKNOWN)
        self.sidebar.clear_info()
        self.overview_page.clear(message)

        for placeholder in self.placeholder_labels.values():
            placeholder.setText(message)

    # ------------------------------------------------------------------
    # Compatibility wrappers for existing tests/integrations
    # ------------------------------------------------------------------

    def _apply_monitor_data(
        self,
        reader: GowFilesystemRunReader,
        snapshots: tuple[RunSnapshot, ...],
        histories: dict[str, tuple[EvaluationPoint, ...]],
        *,
        preferred_run_id: str | None,
    ) -> None:
        del preferred_run_id
        self.viewmodel.apply_loaded_data(
            reader,
            snapshots,
            histories,
        )

    def _refresh_started(self) -> None:
        self.header.set_refresh_status(
            "SYNC",
            "busy",
            "Reading new GOW artifacts outside the UI thread.",
        )

    def _refresh_completed(self, payload: object) -> None:
        self.viewmodel._on_refresh_completed(payload)

    def _refresh_failed(self, message: str) -> None:
        self.viewmodel._on_refresh_failed(message)

    def _set_state(self, state: RunState) -> None:
        self.header.set_run_state(state)

    def _set_refresh_status(
        self,
        text: str,
        state: str,
        tooltip: str,
    ) -> None:
        self.header.set_refresh_status(text, state, tooltip)

    def _resource_sampled(self, payload: object) -> None:
        if not isinstance(payload, SystemResourceSnapshot):
            return
        self.overview_page.render_resource_snapshot(
            payload,
            tuple(self.viewmodel.resource_history),
        )

    def _resource_failed(self, message: str) -> None:
        self.overview_page.render_resource_error(
            message or "Host telemetry is temporarily unavailable"
        )

    def _gow_resource_sampled(self, payload: object) -> None:
        if not isinstance(payload, GowProcessResourceSnapshot):
            return
        self.overview_page.render_gow_resource_snapshot(
            payload,
            tuple(self.viewmodel.gow_resource_history),
        )

    def _gow_resource_failed(self, message: str) -> None:
        self.overview_page.render_gow_resource_error(
            message or "GOW process telemetry is temporarily unavailable"
        )

    # ------------------------------------------------------------------
    # Lifecycle and theme
    # ------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.viewmodel.start_monitoring()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.viewmodel.stop_monitoring()
        super().closeEvent(event)

    def _apply_style(self) -> None:
        style_path = Path(__file__).with_name("style.qss")
        self.setStyleSheet(
            style_path.read_text(encoding="utf-8")
        )
