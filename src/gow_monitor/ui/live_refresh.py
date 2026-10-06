from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

from gow_monitor.domain import EvaluationPoint, RunSnapshot
from gow_monitor.infrastructure import GowFilesystemRunReader


@dataclass(frozen=True, slots=True)
class LiveRefreshPayload:
    """Result of one filesystem refresh performed outside the UI thread."""

    connection_id: int
    selected_path: Path
    reader: GowFilesystemRunReader
    snapshots: tuple[RunSnapshot, ...]
    histories: dict[str, tuple[EvaluationPoint, ...]]


class _RefreshTaskSignals(QObject):
    completed = Signal(object)
    failed = Signal(int, str, str)


class _RefreshTask(QRunnable):
    def __init__(
        self,
        connection_id: int,
        selected_path: Path,
        *,
        data_mode: str = "auto",
    ) -> None:
        super().__init__()
        self.connection_id = connection_id
        self.selected_path = selected_path
        self.data_mode = data_mode
        self.signals = _RefreshTaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            reader = GowFilesystemRunReader(
                self.selected_path,
                data_mode=self.data_mode,
            )
            snapshots: list[RunSnapshot] = []
            histories: dict[str, tuple[EvaluationPoint, ...]] = {}

            for reference in reader.discover_runs():
                snapshot, history = reader.snapshot_and_history_of(reference)
                snapshots.append(snapshot)
                histories[reference.run_id] = history

            payload = LiveRefreshPayload(
                connection_id=self.connection_id,
                selected_path=self.selected_path,
                reader=reader,
                snapshots=tuple(snapshots),
                histories=histories,
            )
        except Exception as exc:
            self.signals.failed.emit(
                self.connection_id,
                str(self.selected_path),
                str(exc),
            )
            return

        self.signals.completed.emit(payload)


class LiveRefreshController(QObject):
    """Periodically refresh GOW artifacts without blocking the Qt UI thread."""

    refresh_started = Signal()
    refreshed = Signal(object)
    refresh_failed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        interval_ms: int = 1000,
        data_mode: str = "auto",
    ) -> None:
        super().__init__(parent)
        if interval_ms < 100:
            raise ValueError("interval_ms must be at least 100 milliseconds")

        self._selected_path: Path | None = None
        self._data_mode = str(data_mode).strip().lower()
        if self._data_mode not in {"auto", "summary", "full"}:
            raise ValueError("data_mode must be one of: auto, summary, full")
        self._connection_id = 0
        self._refresh_in_flight = False
        self._active_task: _RefreshTask | None = None

        self._thread_pool = QThreadPool(self)
        self._thread_pool.setMaxThreadCount(1)

        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.refresh_now)

    @property
    def interval_ms(self) -> int:
        return self._timer.interval()

    @property
    def is_active(self) -> bool:
        return self._timer.isActive()

    @property
    def selected_path(self) -> Path | None:
        return self._selected_path

    def connect_path(self, selected_path: str | Path) -> None:
        self._connection_id += 1
        self._selected_path = Path(selected_path).expanduser().resolve()
        self._timer.start()

    def stop(self) -> None:
        self._connection_id += 1
        self._selected_path = None
        self._timer.stop()

    @Slot()
    def refresh_now(self) -> bool:
        if self._selected_path is None or self._refresh_in_flight:
            return False

        task = _RefreshTask(
            self._connection_id,
            self._selected_path,
            data_mode=self._data_mode,
        )
        task.signals.completed.connect(self._refresh_completed)
        task.signals.failed.connect(self._refresh_failed)

        self._refresh_in_flight = True
        self._active_task = task
        self.refresh_started.emit()
        self._thread_pool.start(task)
        return True

    @Slot(object)
    def _refresh_completed(self, payload: object) -> None:
        self._refresh_in_flight = False
        self._active_task = None

        if not isinstance(payload, LiveRefreshPayload):
            return
        if payload.connection_id != self._connection_id:
            return
        if payload.selected_path != self._selected_path:
            return

        self.refreshed.emit(payload)

    @Slot(int, str, str)
    def _refresh_failed(
        self,
        connection_id: int,
        selected_path: str,
        message: str,
    ) -> None:
        self._refresh_in_flight = False
        self._active_task = None

        if connection_id != self._connection_id:
            return
        if self._selected_path is None:
            return
        if Path(selected_path) != self._selected_path:
            return

        self.refresh_failed.emit(message)