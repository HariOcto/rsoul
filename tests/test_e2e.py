"""End-to-end runs of rsoul.py against fake slskd and Chaptarr HTTP servers.

The fake servers (tests/e2e/fake_servers_run.py) speak the slskd v0.26 and Chaptarr
v0.9.965 APIs as R:soul uses them, and R:soul talks to them through its real HTTP
clients. Each run happens in a subprocess because the script speeds up time.sleep.
"""

import os
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "e2e", "fake_servers_run.py")


def run(scenario=""):
    env = dict(os.environ, SCENARIO=scenario)
    out = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout


def test_audiobook_run_end_to_end():
    out = run()
    assert "wanted list filtered to audiobooks: True" in out
    assert "book 1 staged files: 12 of 12" in out  # search showed 5 chapters; browsing found all 12
    assert "decoy (other author) enqueued: False" in out
    assert "disc folder enqueued: False" in out
    assert "state file left behind: False" in out
    assert "leftover download folders: ['rsoul_audiobooks']" in out
    # Configured only through RSOUL__ environment variables
    assert "config.ini in data folder: False" in out
    # Only R:soul's own transfer records are removed from the shared slskd
    assert "foreign slskd transfer kept: True" in out
    assert "cleared all finished transfers: False" in out
    assert "R:soul transfer records left in slskd: 0" in out


def test_stalled_peer_then_retry_end_to_end():
    out = run("stall")
    run1 = out.split("--- run 2")[0]
    assert "import commands: 0" in run1
    assert "leftovers moved to failed_downloads: {'Mistborn - The Final Empire (2006) [Michael Kramer]': 6}" in run1
    assert "book 1 staged files: 12 of 12" in out  # the retry in run 2 completes the book
    assert "foreign slskd transfer kept: True" in out
    assert "R:soul transfer records left in slskd: 0" in out


def test_other_tool_clearing_finished_transfers_end_to_end():
    # Soularr-style "clear all finished transfers" happens repeatedly while R:soul downloads;
    # R:soul must recognise chapters already on disk and still import the whole book
    out = run("soularr_clears")
    assert "book 1 staged files: 12 of 12" in out
    assert "state file left behind: False" in out
