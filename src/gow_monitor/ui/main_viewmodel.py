from __future__ import annotations

from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from gow_monitor.application import RunControlPort, RunResumePort
from gow_monitor.domain import (
    EvaluationPoint,
    GowProcessResourceSnapshot,
    RunSnapshot,
    RunState,
    SystemResourceSnapshot,
)
from gow_monitor.infrastructure import (
    GowCliRunResumer,
    GowFilesystemRunController,
    GowFilesystemRunReader,
)
from gow_monitor.ui.live_refresh import LiveRefreshController, LiveRefreshPayload
from gow_monitor.ui.resource_monitor import ResourceMonitorController

AUTO_REFRESH_INTERVAL_MS = 1000


class MainViewModel(QObject):
    """Central state and controller coordination for the GOW Monitor UI."""

    monitor_data_changed = Signal()
    refresh_status_changed = Signal(str, str, str)
    run_state_changed = Signal(object)
    system_resource_updated = Signal(object, object)
    gow_resource_updated = Signal(object, object)
    resource_error = Signal(str)
    gow_resource_error = Signal(str)
    pause_request_succeeded = Signal(str)
    resume_request_succeeded = Signal(int)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        gow_pid: int | None = None,
        data_mode: str = "auto",
        run_controller: RunControlPort | None = None,
        run_resumer: RunResumePort | None = None,
    ) -> None:
        super().__init__(parent)

        self.data_mode = str(data_mode).strip().lower()
        if self.data_mode not in {"auto", "summary", "full"}:
            raise ValueError("data_mode must be one of: auto, summary, full")
        self._reader: GowFilesystemRunReader | None = None
        self._snapshots: tuple[RunSnapshot, ...] = ()
        self._histories: dict[str, tuple[EvaluationPoint, ...]] = {}
        self._connected_path: Path | None = None

        self.run_controller: RunControlPort = (
            run_controller
            if run_controller is not None
            else GowFilesystemRunController()
        )

        self.run_resumer: RunResumePort = (
            run_resumer
            if run_resumer is not None
            else GowCliRunResumer()
        )

        self._resource_history: deque[SystemResourceSnapshot] = deque(maxlen=60)
        self._gow_resource_history: deque[
            GowProcessResourceSnapshot
        ] = deque(maxlen=60)

        self.live_refresh = LiveRefreshController(
            self,
            interval_ms=AUTO_REFRESH_INTERVAL_MS,
            data_mode=self.data_mode,
        )
        self.resource_monitor = ResourceMonitorController(
            self,
            interval_ms=AUTO_REFRESH_INTERVAL_MS,
            gow_pid=gow_pid,
        )

        self.live_refresh.refresh_started.connect(self._on_refresh_started)
        self.live_refresh.refreshed.connect(self._on_refresh_completed)
        self.live_refresh.refresh_failed.connect(self._on_refresh_failed)

        self.resource_monitor.sampled.connect(self._on_system_resource)
        self.resource_monitor.sample_failed.connect(self._on_resource_failed)
        self.resource_monitor.gow_sampled.connect(self._on_gow_resource)
        self.resource_monitor.gow_sample_failed.connect(
            self._on_gow_resource_failed
        )

    @property
    def reader(self) -> GowFilesystemRunReader | None:
        return self._reader

    @property
    def snapshots(self) -> tuple[RunSnapshot, ...]:
        return self._snapshots

    @property
    def histories(self) -> dict[str, tuple[EvaluationPoint, ...]]:
        return self._histories

    @property
    def connected_path(self) -> Path | None:
        return self._connected_path

    @property
    def resource_history(self) -> deque[SystemResourceSnapshot]:
        return self._resource_history

    @property
    def gow_resource_history(
        self,
    ) -> deque[GowProcessResourceSnapshot]:
        return self._gow_resource_history

    def get_snapshot_by_index(self, index: int) -> RunSnapshot | None:
        if 0 <= index < len(self._snapshots):
            return self._snapshots[index]
        return None

    def get_history_for_run(
        self,
        run_id: str,
    ) -> tuple[EvaluationPoint, ...]:
        return self._histories.get(run_id, ())

    def apply_loaded_data(
        self,
        reader: GowFilesystemRunReader,
        snapshots: tuple[RunSnapshot, ...],
        histories: dict[str, tuple[EvaluationPoint, ...]],
    ) -> None:
        """Accept already-loaded run data and enable live refresh."""
        self._reader = reader
        self._snapshots = snapshots
        self._histories = histories
        self._connected_path = reader.selected_path

        self.live_refresh.connect_path(reader.selected_path)
        self.monitor_data_changed.emit()
        self.refresh_status_changed.emit(
            "AUTO 1s",
            "live",
            "GOW artifacts are refreshed every second in a background worker.",
        )

    def connect_results_root_async(
        self,
        results_root: str | Path,
    ) -> None:
        """Connect without blocking the UI during initial artifact discovery."""
        selected_path = Path(results_root).expanduser().resolve()

        if not selected_path.exists():
            raise FileNotFoundError(
                f"The selected path does not exist: {selected_path}"
            )

        if not selected_path.is_dir():
            raise NotADirectoryError(
                f"The selected path is not a directory: {selected_path}"
            )

        self._reader = None
        self._snapshots = ()
        self._histories = {}
        self._connected_path = selected_path

        self.live_refresh.connect_path(selected_path)

        self.refresh_status_changed.emit(
            "SYNC",
            "busy",
            "Initial GOW artifact discovery is running in a background worker.",
        )

        self.live_refresh.refresh_now()

    def refresh_connected_results(self) -> bool:
        return self.live_refresh.refresh_now()

    def request_pause(
        self,
        snapshot: RunSnapshot,
    ) -> str:
        """Request a cooperative pause for one running GOW run.

        The filesystem write itself is intentionally delegated to the
        RunControlPort. The ViewModel owns the application-state guard and
        immediately exposes PAUSE_REQUESTED to the UI while filesystem
        refresh catches up with the newly written control artifact.
        """

        if snapshot.state is not RunState.RUNNING:
            raise ValueError(
                "Pause can only be requested for a RUNNING GOW run "
                f"(current state: {snapshot.state.value})"
            )

        request_id = self.run_controller.request_pause(
            snapshot.reference
        )

        self.run_state_changed.emit(
            RunState.PAUSE_REQUESTED
        )

        self.pause_request_succeeded.emit(
            request_id
        )

        return request_id

    def request_resume(
        self,
        snapshot: RunSnapshot,
    ) -> int:
        """Resume one PAUSED GOW run using its persisted runtime context.

        The actual subprocess launch is delegated to RunResumePort.

        RESUMING is emitted only after the adapter successfully launches
        the new GOW process and returns a valid PID. If launching fails,
        the exception propagates and no optimistic state transition is
        emitted.
        """

        if snapshot.state is not RunState.PAUSED:
            raise ValueError(
                "Resume can only be requested for a PAUSED GOW run "
                f"(current state: {snapshot.state.value})"
            )

        pid = self.run_resumer.resume(
            snapshot.reference
        )

        self.resource_monitor.attach_gow_process(
            pid
        )

        self.run_state_changed.emit(
            RunState.RESUMING
        )

        self.resume_request_succeeded.emit(
            pid
        )

        return pid

    def start_monitoring(self) -> None:
        if not self.resource_monitor.is_active:
            self.resource_monitor.start()

    def stop_monitoring(self) -> None:
        self.live_refresh.stop()
        self.resource_monitor.stop()

    def watch_gow_run(
        self,
        *,
        results_root: Path,
        run_id: str,
    ) -> None:
        self.resource_monitor.watch_gow_run(
            results_root=results_root,
            run_id=run_id,
        )

    @Slot()
    def _on_refresh_started(self) -> None:
        self.refresh_status_changed.emit(
            "SYNC",
            "busy",
            "Reading new GOW artifacts outside the UI thread.",
        )

    @Slot(object)
    def _on_refresh_completed(self, payload: object) -> None:
        if not isinstance(payload, LiveRefreshPayload):
            return

        if self._connected_path is None:
            return

        if payload.selected_path != self._connected_path:
            return

        changed = (
            payload.snapshots != self._snapshots
            or payload.histories != self._histories
            or payload.reader.results_root
            != (
                self._reader.results_root
                if self._reader is not None
                else None
            )
        )

        self._reader = payload.reader
        self._snapshots = payload.snapshots
        self._histories = payload.histories

        if changed:
            self.monitor_data_changed.emit()

        self.refresh_status_changed.emit(
            "AUTO 1s",
            "live",
            "Live refresh is active. The selected run is preserved.",
        )

    @Slot(str)
    def _on_refresh_failed(self, message: str) -> None:
        self.refresh_status_changed.emit(
            "AUTO ERR",
            "error",
            message or "Unable to refresh GOW artifacts.",
        )

    @Slot(object)
    def _on_system_resource(self, payload: object) -> None:
        if not isinstance(payload, SystemResourceSnapshot):
            return

        self._resource_history.append(payload)

        self.system_resource_updated.emit(
            payload,
            tuple(self._resource_history),
        )

    @Slot(str)
    def _on_resource_failed(self, message: str) -> None:
        self.resource_error.emit(
            message or "Host telemetry is temporarily unavailable"
        )

    @Slot(object)
    def _on_gow_resource(self, payload: object) -> None:
        if not isinstance(payload, GowProcessResourceSnapshot):
            return

        self._gow_resource_history.append(payload)

        self.gow_resource_updated.emit(
            payload,
            tuple(self._gow_resource_history),
        )

    @Slot(str)
    def _on_gow_resource_failed(self, message: str) -> None:
        self.gow_resource_error.emit(
            message or "GOW process telemetry is temporarily unavailable"
        )