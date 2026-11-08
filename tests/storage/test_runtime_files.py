"""The guarded writers refuse a code-folder path at run time and write anywhere else."""
from __future__ import annotations

import sqlite3

import pytest

from src.storage import runtime_files as rf


@pytest.mark.parametrize("folder", rf.CODE_FOLDERS)
def test_checked_refuses_every_code_folder(folder):
    with pytest.raises(ValueError, match="code folder"):
        rf.checked(rf.REPO_ROOT / folder / "x.txt")


def test_checked_refuses_the_code_folder_itself():
    with pytest.raises(ValueError):
        rf.checked(rf.REPO_ROOT / "src")


def test_writers_refuse_a_code_folder_path_and_write_nothing():
    target = rf.REPO_ROOT / "src" / "storage" / "should_not_exist.txt"
    with pytest.raises(ValueError):
        rf.save_text(target, "x")
    with pytest.raises(ValueError):
        rf.make_dirs(rf.REPO_ROOT / "app" / "should_not_exist")
    with pytest.raises(ValueError):
        rf.copy_file(__file__, target)
    with pytest.raises(ValueError):
        rf.connect_sqlite(target)
    assert not target.exists()
    assert not (rf.REPO_ROOT / "app" / "should_not_exist").exists()


def test_writers_work_outside_the_code_folders(tmp_path):
    folder = rf.make_dirs(tmp_path / "a" / "b")
    assert folder.is_dir()
    note = tmp_path / "a" / "b" / "note.txt"
    rf.save_text(note, "hello")
    assert note.read_text(encoding="utf-8") == "hello"
    copy = rf.copy_file(note, tmp_path / "copy.txt")
    assert copy.read_text(encoding="utf-8") == "hello"
    with rf.open_for_write(tmp_path / "w.txt", "w", encoding="utf-8") as handle:
        handle.write("w")
    assert (tmp_path / "w.txt").read_text(encoding="utf-8") == "w"


def test_backup_sqlite_copies_the_database(tmp_path):
    src = sqlite3.connect(":memory:")
    src.execute("CREATE TABLE t (n INTEGER)")
    src.execute("INSERT INTO t VALUES (7)")
    src.commit()
    dest = rf.backup_sqlite(src, tmp_path / "snap.db")
    src.close()
    check = sqlite3.connect(dest)
    try:
        assert check.execute("SELECT n FROM t").fetchone() == (7,)
    finally:
        check.close()
