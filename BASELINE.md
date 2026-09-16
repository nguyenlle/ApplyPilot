# Upstream baseline

Inspected upstream commit `4a8d521f67f5139811c0a910ef37410f8e6d836a`.
Windows, Python 3.12.14, isolated workspace venv using bundled base packages.

Before source changes:

- `pip install -e ".[dev]"`: passed.
- `pytest tests/ -v`: failed, exit 4; upstream contains no tests directory.
- `ruff check src/`: failed with 149 findings (61 automatically fixable).
- `applypilot --version`: passed, 0.3.0.
- `applypilot doctor`: missing profile, resume, search config, JobSpy and LLM configuration. Claude executable exists in the user-local bin directory but is not on PATH. Chrome and Node detected. Doctor incorrectly exits successfully with required dependencies missing.
- CI only supported manual dispatch at baseline.

Raw baseline command output is retained in ignored `.private/baseline/`.
Chromium and documented JobSpy dependencies are installed separately after this baseline.
No external application submissions were made during baseline collection.
