"""Read-only setup diagnostics. Never print credentials or make paid API calls."""

import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from applypilot import config

# Verified against official documentation, 2026-09-15. LLM_MODEL is ApplyPilot's
# own setting; provider/model access is deliberately not asserted by an env check.
OPENAI_SETUP_GUIDANCE = (
    "Set OPENAI_API_KEY privately in the ApplyPilot .env or process environment. "
    "Do not paste it into logs or commit it. Set LLM_MODEL to a compatible model "
    "available to your API project. This repository uses OpenAI Chat Completions. "
    "Clear GEMINI_API_KEY and LLM_URL when selecting OpenAI because they take "
    "precedence in this repository. Official key environment guidance: "
    "https://developers.openai.com/api/reference/cli"
)


def _module_available(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001 - diagnostics must survive broken third-party imports
        # Import errors can contain environment values or filesystem details.
        return False


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).is_file()
    except Exception:  # noqa: BLE001 - diagnostic boundary around the browser driver
        return False


def claude_auth_status(executable: str | None = None) -> tuple[bool, str]:
    """Check local Claude auth; return sanitized state, never CLI output."""
    executable = executable or config.resolve_claude()
    if not executable:
        return False, "Claude CLI is missing; set CLAUDE_PATH or install it."
    try:
        result = subprocess.run(
            [executable, "auth", "status", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(result.stdout)
        if result.returncode == 0 and isinstance(data, dict) and data.get("loggedIn") is True:
            return True, "Claude reports authenticated (local status only)."
        return False, "Claude is not authenticated; run claude auth login interactively."
    except subprocess.TimeoutExpired:
        return False, "Claude authentication check timed out; run claude auth status."
    except (OSError, ValueError):
        return False, "Could not verify Claude authentication; run claude auth status."


def _search_config_valid() -> bool:
    if not config.SEARCH_CONFIG_PATH.is_file():
        return False
    try:
        data = config.load_search_config()
        if not isinstance(data, dict):
            return False
        queries = data.get("queries")
        flat = data.get("searches")
        if isinstance(flat, list) and flat:
            return all(isinstance(item, dict) and isinstance(item.get("search_term"), str)
                       and item["search_term"].strip() for item in flat)
        if not isinstance(queries, list) or not queries:
            return False
        return all(isinstance(item, dict) and isinstance(item.get("query"), str)
                   and item["query"].strip() for item in queries)
    except Exception:  # noqa: BLE001 - malformed YAML or shape is reported without file contents
        return False


def run_diagnostics(required_tier: int = 3, check_auth: bool = True) -> dict:
    """Return sanitized checks and readiness for discovery, AI, or full pipeline.

    This verifies local configuration and executable availability, not provider
    billing, model access, network reachability, or successful job submissions.
    Skipping an authentication check never declares a full pipeline ready.
    """
    if required_tier not in (1, 2, 3):
        raise ValueError("required_tier must be 1, 2, or 3")
    config.load_env()
    checks: list[dict] = []

    def add(name, ok, detail, tier=1):
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       "required": tier <= required_tier})

    try:
        reasons = config.profile_safety_reasons()
        add("profile.json", not reasons,
            "Valid candidate profile; unknown answers are preserved." if not reasons else " ".join(reasons))
    except (OSError, ValueError):
        add("profile.json", False, "Missing or unreadable JSON profile; configure the candidate profile.")
    try:
        text = config.RESUME_PATH.read_text(encoding="utf-8").strip()
        resume_ok = len(text) >= 100 and "sample resume candidate" not in text.lower()
    except (OSError, UnicodeError):
        resume_ok = False
    add("resume.txt", resume_ok,
        "Nonempty text resume is available." if resume_ok else
        "Provide extracted and verified resume text (at least 100 characters); a PDF alone is insufficient.", 2)
    add("searches.yaml", _search_config_valid(),
        "Search configuration must contain usable queries/searches; example fallback is not accepted.")
    for module, label in (("jobspy", "python-jobspy"), ("playwright.sync_api", "Playwright")):
        available = _module_available(module)
        add(label, available, "Import succeeds." if available else "Dependency missing or cannot be imported.")
    chromium = _chromium_available()
    add("Playwright Chromium", chromium,
        "Bundled browser is installed." if chromium else "Run python -m playwright install chromium.")

    provider = config.configured_llm_provider()
    credential_ok = provider is not None
    key_name = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}.get(provider)
    if key_name:
        value = os.environ.get(key_name, "").strip().lower()
        credential_ok = not (value.startswith("your") or "placeholder" in value
                             or value in {"sk-...", "sk-xxx", "changeme", "test", "none"})
    add("LLM configuration", credential_ok,
        f"{provider} is configured; remote credentials/model access have not been tested."
        if credential_ok else "Missing usable LLM configuration. For OpenAI set OPENAI_API_KEY privately.", 2)

    claude = config.resolve_claude()
    add("Claude CLI", bool(claude), "Executable found." if claude else
        "Not found; install Claude or set CLAUDE_PATH to its executable.", 3)
    if check_auth and claude:
        authenticated, detail = claude_auth_status(claude)
    else:
        authenticated, detail = False, "Authentication not verified."
    add("Claude authentication", authenticated, detail, 3)
    try:
        config.get_chrome_path()
        chrome_ok = True
    except FileNotFoundError:
        chrome_ok = False
    add("Chrome/Chromium", chrome_ok,
        "Executable found." if chrome_ok else "Install Chrome or set CHROME_PATH.", 3)
    for executable in ("node", "npx"):
        found = bool(shutil.which(executable))
        add(executable, found, "Executable found." if found else "Install Node.js and expose node and npx on PATH.", 3)

    adapter_path = config.APP_DIR / "application_adapters.json"
    try:
        import json
        adapter_doc = json.loads(adapter_path.read_text(encoding="utf-8"))
        adapters_ok = isinstance(adapter_doc.get("adapters"), list) and bool(adapter_doc["adapters"])
    except (OSError, ValueError, AttributeError):
        adapters_ok = False
    add("Live submission adapters", adapters_ok,
        "Adapter descriptors exist; real site validation is still required." if adapters_ok else
        "No reviewed site adapter configured; live submissions remain blocked.", 3)

    return {
        "ok": all(item["ok"] for item in checks if item["required"]),
        "required_tier": required_tier,
        "checks": checks,
        "guidance": [OPENAI_SETUP_GUIDANCE,
                     "OpenAI powers scoring/tailoring here; browser submission still requires Claude authentication.",
                     "This check makes no paid API calls and does not prove external site compatibility."],
    }
