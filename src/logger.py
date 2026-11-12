import logging
import os
import threading
from logging.handlers import TimedRotatingFileHandler

from src.storage.runtime_files import copy_file, make_dirs, open_for_write

_FILE_HANDLER_NAME = "stripe_automation.file"
_STREAM_HANDLER_NAME = "stripe_automation.stream"
_configure_lock = threading.Lock()


class _WinSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """TimedRotatingFileHandler that survives Windows file-lock errors on rotation.

    On Windows, os.rename fails if another process still has the log file open
    (e.g. a previous Streamlit run that hasn't fully exited).  We fall back to
    copy-then-delete so the active file is never locked out.
    """

    def rotate(self, source: str, dest: str) -> None:
        try:
            super().rotate(source, dest)
        except PermissionError:
            try:
                copy_file(source, dest)
                # Truncate the source so new entries start fresh
                with open_for_write(source, "w", encoding="utf-8"):
                    pass
            except Exception:
                pass  # Never crash the app over a log rotation failure


def _configure_root(logs_dir: str) -> None:
    """Attach the one file handler and one stream handler to the root logger.

    A single handler owns the log file, so midnight rotation has one writer in the
    process instead of one per module. The root logger's own level stays at its
    default: third-party libraries keep logging at WARNING, while module loggers
    set DEBUG themselves and propagate here.
    """
    root = logging.getLogger()
    with _configure_lock:
        if any(h.get_name() == _FILE_HANDLER_NAME for h in root.handlers):
            return

        make_dirs(logs_dir)
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        fh = _WinSafeTimedRotatingFileHandler(
            os.path.join(logs_dir, "stripe_automation.log"),
            when="midnight",
            backupCount=30,
            encoding="utf-8",
            delay=True,
        )
        fh.set_name(_FILE_HANDLER_NAME)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)

        ch = logging.StreamHandler()
        ch.set_name(_STREAM_HANDLER_NAME)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)

        root.addHandler(fh)
        root.addHandler(ch)


def get_logger(name: str, logs_dir: str = "data/logs") -> logging.Logger:
    """Return the module logger ``name``; the first call also configures the shared handlers.

    ``logs_dir`` only matters on that first call.
    """
    _configure_root(logs_dir)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    return logger
