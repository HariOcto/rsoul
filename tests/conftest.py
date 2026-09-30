import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def no_import_settle_wait(monkeypatch):
    """Tests check files right away instead of waiting for Readarr/Chaptarr to move them."""
    from rsoul import postprocess

    monkeypatch.setattr(postprocess, "IMPORT_SETTLE_SECONDS", 0)
