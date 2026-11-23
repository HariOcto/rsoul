import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def no_import_settle_wait(monkeypatch):
    """Tests check files right away instead of waiting for Readarr/Chaptarr to move them."""
    from rsoul import postprocess

    monkeypatch.setattr(postprocess, "IMPORT_SETTLE_SECONDS", 0)


@pytest.fixture(autouse=True)
def short_test_audio_counts_as_a_book(monkeypatch):
    """The audio fixtures play for seconds and stand for whole books; the minimum running time
    for an audiobook is tested on its own (test_v11_wrong_author.py) with an explicit setting."""
    from rsoul import import_transaction

    monkeypatch.setattr(import_transaction, "DEFAULT_MIN_AUDIOBOOK_MINUTES", 0)
