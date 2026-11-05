"""Background work for the GUI (thread pool + Qt signals)."""

from __future__ import annotations

import threading
import traceback
from typing import Any
from collections.abc import Callable

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from ..download import Cancelled


class _Signals(QObject):
    progress = pyqtSignal(str, float)
    done = pyqtSignal(object)
    failed = pyqtSignal(str, str)


class Task(QRunnable):
    def __init__(self, name: str, fn: Callable[..., Any], key: str | None = None, quiet: bool = False):
        super().__init__()
        self.name = name
        self.key = key or name
        self.fn = fn
        self.quiet = quiet
        self.cancel = threading.Event()
        self.signals = _Signals()
        self.message = name
        self.fraction = -1.0
        self.setAutoDelete(False)

    def report(self, message: str, fraction: float = -1.0) -> None:
        self.signals.progress.emit(message, float(fraction))

    def run(self) -> None:
        try:
            result = self.fn(self.report, self.cancel)
        except Cancelled:
            self.signals.failed.emit("Cancelled", "")
        except Exception as e:  # noqa: BLE001 - surface every failure to the UI
            self.signals.failed.emit(str(e) or e.__class__.__name__, traceback.format_exc())
        else:
            self.signals.done.emit(result)


class TaskManager(QObject):
    changed = pyqtSignal()
    error = pyqtSignal(str, str, str)      # task name, message, traceback

    def __init__(self):
        super().__init__()
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max(4, self.pool.maxThreadCount()))
        self.active: dict[str, Task] = {}

    def running(self, key: str) -> bool:
        return key in self.active

    def start(self, name: str, fn: Callable[..., Any], on_done: Callable[[Any], None] | None = None,
              on_error: Callable[[str], None] | None = None, key: str | None = None,
              quiet: bool = False) -> Task | None:
        """fn(progress, cancel_event) runs in a worker thread; callbacks run on the GUI thread."""
        key = key or name
        if key in self.active:
            return None
        task = Task(name, fn, key, quiet)

        def progress(msg: str, frac: float) -> None:
            task.message, task.fraction = msg, frac
            self.changed.emit()

        def done(result: Any) -> None:
            self.active.pop(key, None)
            self.changed.emit()
            if on_done:
                on_done(result)

        def failed(msg: str, tb: str) -> None:
            self.active.pop(key, None)
            self.changed.emit()
            if on_error:
                on_error(msg)
            if msg != "Cancelled" and not quiet:
                self.error.emit(name, msg, tb)

        task.signals.progress.connect(progress)
        task.signals.done.connect(done)
        task.signals.failed.connect(failed)
        self.active[key] = task
        self.changed.emit()
        self.pool.start(task)
        return task

    def visible(self) -> Task | None:
        """The task to show in the status bar (most recent non-quiet one)."""
        for task in reversed(list(self.active.values())):
            if not task.quiet:
                return task
        return None
