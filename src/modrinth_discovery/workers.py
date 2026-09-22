from __future__ import annotations

import traceback
import time
from collections.abc import Callable
from typing import Any
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from .diagnostics import get_logger

logger = get_logger("worker")


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(object)
    progress = Signal(int)
    item = Signal(object)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function: Callable[..., Any], *args: Any, with_progress: bool = False,
                 with_items: bool = False, name: str | None = None, **kwargs: Any) -> None:
        super().__init__(); self.function=function; self.args=args; self.kwargs=kwargs
        self.signals=WorkerSignals(); self.with_progress=with_progress
        self.with_items=with_items; self.name = name or getattr(function, "__qualname__", repr(function))

    @Slot()
    def run(self) -> None:
        started = time.monotonic()
        logger.info("worker.start name=%s", self.name)
        try:
            if self.with_progress: self.kwargs["progress"] = self.signals.progress.emit
            if self.with_items: self.kwargs["emit_item"] = self.signals.item.emit
            result = self.function(*self.args, **self.kwargs)
            logger.info("worker.result name=%s elapsed=%.3fs", self.name, time.monotonic() - started)
            self.signals.result.emit(result)
        except Exception as error:
            logger.exception("worker.error name=%s elapsed=%.3fs", self.name, time.monotonic() - started)
            traceback.print_exc(); self.signals.error.emit(error)
        finally:
            logger.info("worker.finished name=%s elapsed=%.3fs", self.name, time.monotonic() - started)
            self.signals.finished.emit()


class WorkerController(QObject):
    """Keeps Python QRunnable/signal objects alive until finished is delivered."""

    def __init__(self, pool: QThreadPool | None = None) -> None:
        super().__init__()
        self.pool = pool or QThreadPool.globalInstance()
        self._active: set[Worker] = set()

    def start(self, worker: Worker, on_finished: Callable[[], None] | None = None) -> None:
        self._active.add(worker)
        logger.info("controller.retain name=%s active=%d", worker.name, len(self._active))
        worker.signals.finished.connect(
            lambda current=worker, callback=on_finished: self._finished(current, callback)
        )
        self.pool.start(worker)

    def _finished(self, worker: Worker, callback: Callable[[], None] | None) -> None:
        try:
            if callback is not None:
                callback()
        finally:
            self._active.discard(worker)
            logger.info("controller.release name=%s active=%d", worker.name, len(self._active))

    @property
    def active_count(self) -> int:
        return len(self._active)
