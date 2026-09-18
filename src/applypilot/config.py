"""ApplyPilot configuration: paths, platform detection, user data."""

import json
import os
import platform
import re
import shutil
from pathlib import Path

# User data directory — all user-specific files live here
APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))

# Core paths
DB_PATH = APP_DIR / "applypilot.db"
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
ENV_PATH = APP_DIR / ".env"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
LOG_DIR = APP_DIR / "logs"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"


def required_resume_template() -> Path | None:
    """Resolve an explicitly required LaTeX template; never silently fall back."""
    value = os.environ.get("APPLYPILOT_RESUME_TEMPLATE")
    setting = APP_DIR / "resume-rendering.json"
    if value is None and setting.exists():
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("Duplicate resume rendering configuration key")
                result[key] = item
            return result
        try:
            data = json.loads(setting.read_text(encoding="utf-8"), object_pairs_hook=unique)
        except (OSError, ValueError) as exc:
            raise ValueError("Cannot read required resume rendering configuration") from exc
        if not isinstance(data, dict) or set(data) != {"renderer", "template_path"} or data["renderer"] != "latex":
            raise ValueError("Resume rendering configuration requires renderer=latex and template_path")
        value = data["template_path"]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise ValueError("Required LaTeX template must use an absolute path")
    template = Path(value)
    if not template.is_file():
        raise ValueError("Required LaTeX resume template is missing; generic rendering is forbidden")
    return template


def resolve_claude() -> str | None:
    """Resolve Claude, including native installs not added to the current PATH.

    An explicit but invalid override fails closed rather than silently selecting
    a different executable. No subprocess or authentication is performed here.
    """
    override = os.environ.get("CLAUDE_PATH", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return str(candidate.resolve()) if candidate.is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    for name in ("claude.exe", "claude", "claude.cmd"):
        candidate = Path.home() / ".local" / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return None


def configured_llm_provider() -> str | None:
    """Return the actual provider precedence used by llm.py, without secrets."""
    if os.environ.get("LLM_URL", "").strip():
        return "local"
    if os.environ.get("GEMINI_API_KEY", "").strip():
        return "gemini"
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "openai"
    return None


def apply_model() -> str:
    """Browser engine is OpenAI; never inherit a Gemini/local-only model name."""
    explicit = os.environ.get("APPLY_MODEL", "").strip()
    if explicit:
        return explicit
    if configured_llm_provider() == "openai":
        return os.environ.get("LLM_MODEL", "").strip() or "gpt-4o-mini"
    return "gpt-4o-mini"


def profile_safety_reasons(profile: dict | None = None) -> list[str]:
    """Find malformed/sample values without inventing missing personal facts.

    Unknown legal and voluntary answers may remain null; the browser must stop
    or request a human answer if a required form asks for them. Validation is
    structural and cannot establish the truth of supplied facts.
    """
    data = load_profile() if profile is None else profile
    if not isinstance(data, dict):
        return ["Profile must be a JSON object."]
    reasons: list[str] = []
    personal = data.get("personal")
    if not isinstance(personal, dict):
        return ["Profile requires a personal object with full_name and email."]
    for field in ("full_name", "email"):
        value = personal.get(field)
        if not isinstance(value, str) or not value.strip():
            reasons.append(f"personal.{field} is required.")
    email = personal.get("email")
    if isinstance(email, str) and email.strip():
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip()):
            reasons.append("personal.email is not a valid email address.")
        elif email.rsplit("@", 1)[-1].lower() in {"example.com", "example.org", "example.net"}:
            reasons.append("personal.email still uses an example domain.")
    sections = ("work_authorization", "availability", "compensation", "experience",
                "skills_boundary", "resume_facts", "screening", "eeo_voluntary")
    for section in sections:
        if section in data and not isinstance(data[section], dict):
            reasons.append(f"{section} must be an object; individual unknown values may be null.")

    sample_values = {"firstname lastname", "your name", "your city", "your country",
                     "your state/province", "your university", "company a", "company b",
                     "project x", "project y", "123 main st", "555-123-4567", "changeme"}

    def visit(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
        elif isinstance(value, str):
            normalized = value.strip().lower()
            if (normalized in sample_values or normalized.startswith("your_")
                    or "sample resume candidate" in normalized
                    or "/yourprofile" in normalized or "/yourusername" in normalized):
                reasons.append(f"{path} contains an example placeholder; replace it or use null.")

    visit(data, "")
    authorization = data.get("work_authorization")
    if isinstance(authorization, dict):
        for key in ("legally_authorized_to_work", "require_sponsorship"):
            value = authorization.get(key)
            if value is not None and not isinstance(value, (str, bool)):
                reasons.append(f"work_authorization.{key} must be a supplied answer or null.")
    return reasons


def validate_profile_for_application(profile: dict | None = None) -> dict:
    """Return the unchanged profile or reject malformed/example candidate data."""
    data = load_profile() if profile is None else profile
    reasons = profile_safety_reasons(data)
    if reasons:
        raise ValueError("Invalid application profile: " + " ".join(reasons))
    return data


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def ensure_dirs():
    """Create all required directories."""
    for d in [APP_DIR, TAILORED_DIR, COVER_LETTER_DIR, LOG_DIR, CHROME_WORKER_DIR, APPLY_WORKER_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_profile() -> dict:
    """Load user profile from ~/.applypilot/profile.json."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `applypilot init` first."
        )
    data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Profile must be a JSON object.")  # noqa: TRY004 - invalid file content
    return data


def load_search_config() -> dict:
    """Load search configuration from ~/.applypilot/searches.yaml."""
    import yaml
    if not SEARCH_CONFIG_PATH.exists():
        # Fall back to package-shipped example
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return yaml.safe_load(example.read_text(encoding="utf-8"))
        return {}
    return yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain.lower() or host.endswith("." + domain.lower()) for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 7,
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}


def load_env():
    """Load environment variables from ~/.applypilot/.env if it exists."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + OpenAI key + Chrome + Node/npx
    """
    load_env()

    has_llm = configured_llm_provider() is not None
    if not has_llm:
        return 1

    has_openai = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_openai and has_chrome and shutil.which("node") and shutil.which("npx"):
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and configured_llm_provider() is None:
        missing.append("LLM credentials — set OPENAI_API_KEY, GEMINI_API_KEY, or LLM_URL")
    if required >= 3:
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            missing.append("OPENAI_API_KEY — configure privately for the OpenAI browser runner")
        if not shutil.which("node") or not shutil.which("npx"):
            missing.append("Node.js and npx — install Node.js and make both available on PATH")
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)
