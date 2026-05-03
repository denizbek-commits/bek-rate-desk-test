"""
Bek Rate Desk — I/O Utilities
==============================
Atomic file write helpers.  All JSON persistence in the platform should go
through _atomic_json_write so that concurrent browser-tab writes and process
crashes can never leave a half-written (corrupt) file behind.

Pattern:
  1. Write to a temp file in the same directory as the target.
  2. os.replace() — atomic on POSIX, best-effort on Windows.
  3. If the write itself fails the temp file is cleaned up; the original is untouched.
"""

import json, os, tempfile
from typing import Any


def _atomic_json_write(path: str, data: Any, indent: int = 2,
                       ensure_ascii: bool = False, default=None) -> None:
    """
    Atomically write *data* as JSON to *path*.

    Uses write-to-temp + os.replace so the target file is never in a
    partially-written state, even under concurrent writes or process death.
    """
    dir_ = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent,
                      ensure_ascii=ensure_ascii, default=default)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
