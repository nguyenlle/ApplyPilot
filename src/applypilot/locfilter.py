"""Consistent configured location and title filters for all discovery sources."""

import re

from applypilot import config


def load_location_filter(search_cfg: dict | None = None) -> tuple[list[str], list[str]]:
    cfg = config.load_search_config() if search_cfg is None else search_cfg
    location = cfg.get("location", {})
    return (location.get("accept_patterns", cfg.get("location_accept", [])),
            location.get("reject_patterns", cfg.get("location_reject_non_remote", [])))


def _matches(text: str, pattern: str) -> bool:
    return bool(pattern.strip() and re.search(r"(?<!\w)" + re.escape(pattern.strip()) + r"(?!\w)", text, re.IGNORECASE))


def location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    if not location:
        return True  # Unknowns remain available for scoring and review.
    if any(_matches(location, pattern) for pattern in reject):
        return False
    return not accept or any(_matches(location, pattern) for pattern in accept)


def title_ok(title: str | None, excluded: list[str]) -> bool:
    return bool(title) and not any(_matches(title, pattern) for pattern in excluded)
