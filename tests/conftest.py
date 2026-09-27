"""Shared fixtures.

No test in this suite may open a serial port - everything must run without hardware.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aero_q32  # noqa: E402  (path set up above)


@pytest.fixture()
def temp_data_dir(monkeypatch):
    """Point the data directory at a throwaway location.

    Important: log file handles must be released BEFORE the temporary directory is
    removed. On Windows an open handle makes the directory undeletable, which is
    exactly the resource leak this project once had.
    """
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("AEROQ32_DATA_DIR", d)
        try:
            yield d
        finally:
            aero_q32.close_logging()