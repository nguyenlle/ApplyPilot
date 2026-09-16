"""Deterministic preference gates before scoring and before application.

Unknown information is never interpreted as a negative fact. Configured apply
requirements can instead hold the job for review using a ``missing_info`` reason.
Salary comparisons use explicit annual amounts in the requested currency only.
"""

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from applypilot import config
from applypilot.locfilter import _matches, load_location_filter


def _values(cfg: dict, prefs: dict, name: str) -> list[str]:
    return list(cfg.get(name) or []) + list(prefs.get(name) or [])


def _mode(job: dict) -> str | None:
    value = str(job.get("work_mode") or job.get("workplace_type") or "").lower()
    text = value or str(job.get("location") or "").lower()
    if re.search(r"\bhybrid\b", text):
        return "hybrid"
    if re.search(r"\b(?:on[ -]?site|in[ -]?office|not remote)\b", text):
        return "onsite"
    if re.search(r"\b(?:remote|work from home)\b", text) or job.get("is_remote") is True:
        return "remote"
    return None


def _annual_salary_max(job: dict, wanted_currency: str) -> float | None:
    salary = str(job.get("salary") or "").strip()
    currency = str(job.get("salary_currency") or job.get("currency") or "").upper()
    if not currency:
        match = re.search(r"(?<![A-Za-z])(USD|CAD|EUR|GBP|AUD)(?=\d|\b)", salary, re.IGNORECASE)
        currency = match.group(1).upper() if match else "USD" if "$" in salary else ""
    if currency != wanted_currency.upper():
        return None
    interval = str(job.get("salary_interval") or job.get("interval") or "").lower()
    if re.search(r"\b(hour|hourly|week|weekly|month|monthly|day|daily)\b|/(hr|mo)\b", interval + " " + salary.lower()):
        return None
    if not (interval in {"year", "yearly", "annual", "annually"}
            or re.search(r"\b(year|yearly|annual|annually|annum)\b", salary.lower())):
        return None
    structured = job.get("salary_max", job.get("max_amount"))
    if structured is not None:
        try:
            number = float(structured)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None
    amounts = [float(number.replace(",", "")) * (1000 if suffix else 1)
               for number, suffix in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?", salary)]
    # A lower bound alone cannot establish that the maximum is below the target.
    if len(amounts) == 1 and re.search(r"\+|at least|starting|from\b", salary, re.IGNORECASE):
        return None
    return max(amounts) if amounts else None


def _timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def normalize_posting_date(value) -> str | None:
    """Preserve unambiguous ISO dates; relative labels remain only in posted_raw."""
    parsed = _timestamp(value)
    return parsed.isoformat() if parsed else None


def eligibility_reason(job: dict, search_cfg: dict | None = None, profile: dict | None = None,
                       *, phase: str = "score") -> str | None:
    """Return a prefixed reason to hold/reject a job, or None to continue.

    Config keys live at the root or under ``preferences`` for exclusions.
    ``target_roles``, ``work_modes``, ``salary_min``, ``salary_currency``,
    ``max_job_age_days`` and ``require_*_for_apply`` live under preferences.
    This gate does not establish qualifications or replace factual scoring.
    """
    if phase not in {"score", "apply"}:
        raise ValueError("eligibility phase must be score or apply")
    cfg = config.load_search_config() if search_cfg is None else search_cfg
    prefs = cfg.get("preferences", {})
    profile = profile or {}
    missing_reason = None
    title = str(job.get("title") or "")
    company = str(job.get("company") or "")
    location = str(job.get("location") or "")
    description = str(job.get("full_description") or job.get("description") or "")

    for field, text in (("titles", title), ("companies", company), ("locations", location)):
        if any(_matches(text, pattern) for pattern in _values(cfg, prefs, f"exclude_{field}")):
            return f"not_eligible: excluded {field[:-1] if field != 'companies' else 'company'}: {text}"
    for value in (job.get("url"), job.get("application_url")):
        if not value:
            continue
        try:
            domain = (urlsplit(str(value)).hostname or "").lower()
        except ValueError:
            missing_reason = missing_reason or "missing_info: invalid job URL"
            continue
        for blocked in _values(cfg, prefs, "exclude_domains"):
            blocked = blocked.lower().removeprefix("*.").strip(".")
            if domain == blocked or domain.endswith("." + blocked):
                return f"not_eligible: excluded domain: {domain}"

    targets = prefs.get("target_roles", [])
    if targets and title:
        if not any(_matches(title, target) for target in targets):
            return f"not_eligible: title does not match target roles: {title}"
        mechanical_search = any("mechanical" in role.lower() for role in targets)
        if mechanical_search and re.search(r"\b(?:UX|UI|user experience|user interface|digital product|software)\b", title, re.IGNORECASE):
            return f"not_eligible: software or interface role outside mechanical search: {title}"
    elif targets and phase == "apply":
        missing_reason = missing_reason or "missing_info: job title required"

    accept, reject = load_location_filter(cfg)
    accept = prefs.get("locations") or accept
    reject = list(reject) + _values(cfg, prefs, "exclude_locations")
    if any(_matches(location, pattern) for pattern in reject):
        return f"not_eligible: excluded location: {location}"
    if accept and location and not any(_matches(location, pattern) for pattern in accept):
        # A generic remote location provides no geographic eligibility evidence.
        if re.fullmatch(r"\s*(?:remote|anywhere|work from home)\s*", location, re.IGNORECASE):
            if phase == "apply" and prefs.get("require_location_for_apply", True):
                missing_reason = missing_reason or "missing_info: remote job geographic eligibility unknown"
        else:
            return f"not_eligible: location outside target areas: {location}"
    elif accept and not location and phase == "apply" and prefs.get("require_location_for_apply", True):
        missing_reason = missing_reason or "missing_info: job location required"

    modes = prefs.get("work_modes", [])
    mode = _mode(job)
    if modes and mode and mode not in modes:
        return f"not_eligible: work mode excluded: {mode}"
    if modes and not mode and phase == "apply" and prefs.get("require_work_mode_for_apply", False):
        missing_reason = missing_reason or "missing_info: work mode unknown"

    minimum = prefs.get("salary_min")
    if minimum is not None:
        maximum = _annual_salary_max(job, prefs.get("salary_currency", "USD"))
        if maximum is not None and maximum < float(minimum):
            return f"not_eligible: salary maximum below minimum: {maximum:g} < {float(minimum):g} {prefs.get('salary_currency', 'USD')} annually"
        if maximum is None and phase == "apply" and prefs.get("require_salary_for_apply", True):
            missing_reason = missing_reason or "missing_info: comparable annual salary required"

    requires_sponsorship = prefs.get("requires_sponsorship", profile.get("work_authorization", {}).get("require_sponsorship"))
    if requires_sponsorship is True or str(requires_sponsorship).strip().lower() in {"yes", "true"}:
        denied = (job.get("sponsorship_available") is False or str(job.get("sponsorship_available")).lower() == "no") or re.search(
            r"\b(?:no (?:visa )?sponsorship|(?:cannot|will not|does not|do not|unable to|not able to) (?:(?:offer|provide|support) (?:visa )?sponsorship|sponsor)|without (?:current or future |future |visa )?sponsorship|sponsorship (?:is )?not (?:available|offered)|not eligible for (?:visa )?sponsorship)\b",
            description, re.IGNORECASE,
        )
        if denied:
            return "not_eligible: employer explicitly denies required sponsorship"

    now = datetime.now(UTC)
    expires = _timestamp(job.get("valid_through") or job.get("validThrough"))
    if expires and expires < now:
        return f"not_eligible: posting expired: {expires.isoformat()}"
    posted = _timestamp(job.get("date_posted") or job.get("posted_at") or job.get("datePosted"))
    max_age = prefs.get("max_job_age_days")
    if posted and max_age is not None and (now - posted).total_seconds() > float(max_age) * 86400:
        return f"not_eligible: posting older than configured age limit: posted {posted.isoformat()}, limit {max_age} days"
    return missing_reason
