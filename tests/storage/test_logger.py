"""One shared file handler for the whole process, not one per module logger."""
import logging

import pytest

from src import logger as logger_mod


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    saved = list(root.handlers)
    for h in saved:
        if h.get_name() in (logger_mod._FILE_HANDLER_NAME, logger_mod._STREAM_HANDLER_NAME):
            root.removeHandler(h)
    yield root
    for h in list(root.handlers):
        if h not in saved:
            root.removeHandler(h)
            h.close()
    for h in saved:
        if h not in root.handlers:
            root.addHandler(h)


def test_many_module_loggers_share_one_file_handler(clean_root, tmp_path):
    names = [f"test_logger_mod_{i}" for i in range(5)]
    loggers = [logger_mod.get_logger(n, str(tmp_path)) for n in names]

    file_handlers = [h for h in clean_root.handlers if h.get_name() == logger_mod._FILE_HANDLER_NAME]
    assert len(file_handlers) == 1
    assert all(lg.handlers == [] for lg in loggers)
    assert logger_mod.get_logger(names[0], str(tmp_path)) is loggers[0]


def test_records_reach_the_log_file_once(clean_root, tmp_path):
    lg = logger_mod.get_logger("test_logger_once", str(tmp_path))
    lg.info("marker-line")
    for h in clean_root.handlers:
        h.flush()

    text = (tmp_path / "stripe_automation.log").read_text(encoding="utf-8")
    assert text.count("marker-line") == 1
