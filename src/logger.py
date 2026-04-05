import logging
import os
import shutil
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


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
                shutil.copy2(source, dest)
                # Truncate the source so new entries start fresh
                with open(source, "w", encoding="utf-8"):
                    pass
            except Exception:
                pass  # Never crash the app over a log rotation failure


def get_logger(name: str, logs_dir: str = "logs") -> logging.Logger:
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

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
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger
