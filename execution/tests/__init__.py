import os

# The jev backstop (EXECUTION_JEV_GUARD) defaults ON in production. Legacy
# gateway/runner tests build buy rows without jev_age_min, so they run with the
# guard off; test_jev_guard.py turns it on explicitly and checks the default.
os.environ.setdefault("EXECUTION_JEV_GUARD", "0")

# Isolate tests from the live PAPER_OPEN_PAUSED flag file in the repo root;
# test_paper_open_pause.py points this at its own temp flag.
os.environ.setdefault("EXECUTION_PAPER_OPEN_PAUSE_FLAG", "/nonexistent/PAPER_OPEN_PAUSED")
