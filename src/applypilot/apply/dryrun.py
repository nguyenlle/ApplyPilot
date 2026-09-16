"""Network-isolated browser filling of a captured application document.

The initial unauthenticated GET retrieves HTML. A fresh browser then renders
that captured document with all network, frames, forms and popups blocked.
This intentionally cannot validate authenticated, JS-bundled or multi-page ATS
flows; those are reported as limitations, never as successful applications.
"""

import hashlib
import html
import json
import re
import socket
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from applypilot import config

# Exact matching only: 'employer name' must never match the applicant's name.
FIELD_MAP = {
    "full name": "full_name", "name": "full_name", "fullname": "full_name",
    "first name": "first_name", "firstname": "first_name",
    "last name": "last_name", "lastname": "last_name",
    "email": "email", "email address": "email", "phone": "phone",
    "phone number": "phone", "telephone": "phone", "address": "address",
    "street address": "address", "city": "city", "state": "province_state",
    "province": "province_state", "country": "country", "postal code": "postal_code",
    "zip code": "postal_code", "zip": "postal_code", "linkedin": "linkedin_url",
    "linkedin url": "linkedin_url", "github": "github_url", "github url": "github_url",
    "portfolio": "portfolio_url", "website": "website_url",
}


def _field_value(candidates: list[str], profile: dict) -> tuple[str | None, str | None]:
    personal = profile.get("personal", {})
    for label in candidates:
        normalized = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
        if not normalized:
            continue
        key = FIELD_MAP.get(normalized)
        if key and personal.get(key) not in (None, ""):
            return key, str(personal[key])
        # An explicit human label beats a contradictory name/id attribute.
        return None, None
    return None, None


def _capture_html(url: str) -> tuple[str, str]:
    """Retrieve only HTML with bounded GET redirects and no stored credentials."""
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        for _ in range(6):
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                raise ValueError("Dry-run requires an HTTP(S) URL without embedded credentials")
            with client.stream("GET", url, headers={"User-Agent": "ApplyPilot-DryRun/1.0"}) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect has no destination")
                    url = str(response.url.join(location))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "html" not in content_type:
                    raise ValueError("Dry-run source is not HTML")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 5_000_000:
                        raise ValueError("Dry-run HTML exceeds 5 MB")
                    chunks.append(chunk)
                return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace"), str(response.url)
    raise ValueError("Too many redirects")


def run_dry_run(job: dict, headless: bool = True, *, profile: dict | None = None,
                output_dir: Path | None = None, html_content: str | None = None) -> dict:
    """Fill a captured single-page form and return debugging evidence, never submit.

    html_content is a fixture/captured-document injection for deterministic tests.
    Network isolation is installed before any untrusted HTML executes.
    """
    profile = config.load_profile() if profile is None else profile
    url = job.get("application_url") or job["url"]
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Dry-run requires an HTTP(S) URL without embedded credentials")
    if html_content is None:
        html_content, url = _capture_html(url)
    key = hashlib.sha256(job["url"].encode()).hexdigest()[:20]
    output = output_dir or config.LOG_DIR / "dry-runs" / key / uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=True)

    # Prefix our CSP before all source markup. Retain inline JS so event-driven
    # form behavior runs, but prevent every network-capable destination.
    soup = BeautifulSoup(html_content, "html.parser")
    for element in soup.select("base, meta[http-equiv]"):
        element.decompose()
    policy = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
              "img-src data:; connect-src 'none'; frame-src 'none'; child-src 'none'; "
              "object-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none'")
    captured = '<meta http-equiv="Content-Security-Policy" content="' + html.escape(policy, quote=True) + '">'
    # Cancel form navigation itself as well as its network request. Otherwise a
    # blocked autosubmit can replace the local document with an error page.
    captured += """<script>
    for (const method of ['submit', 'requestSubmit']) {
      Object.defineProperty(HTMLFormElement.prototype, method, {
        value: function() {}, writable: false, configurable: false
      });
    }
    window.addEventListener('submit', event => {
      event.preventDefault(); event.stopImmediatePropagation();
    }, true);
    Object.defineProperty(window, 'open', {value: () => null, writable: false, configurable: false});
    for (const api of ['RTCPeerConnection', 'webkitRTCPeerConnection']) {
      Object.defineProperty(window, api, {value: undefined, writable: false, configurable: false});
    }
    </script>"""
    captured += str(soup)
    blocked = []
    filled = []
    missing = []
    observed_fields = 0
    with socket.socket() as deny_proxy, sync_playwright() as playwright:
        # Reserve a non-listening local port; no proxy service can accept data.
        deny_proxy.bind(("127.0.0.1", 0))
        browser = playwright.chromium.launch(
            headless=headless,
            # Fail closed even for browser networking outside Playwright routes.
            proxy={"server": f"http://127.0.0.1:{deny_proxy.getsockname()[1]}"},
            args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
        )
        context = browser.new_context(service_workers="block", accept_downloads=False)
        try:
            def block_request(route):
                request = route.request
                blocked.append({"method": request.method, "url": request.url.split("?")[0]})
                route.abort("blockedbyclient")
            context.route("**/*", block_request)
            # WebSocket connections are not covered by HTTP route interception.
            if not hasattr(context, "route_web_socket"):
                raise RuntimeError("Dry-run needs Playwright >=1.48 for WebSocket interception")
            context.route_web_socket("**/*", lambda ws: ws.close())
            page = context.new_page()
            page.set_default_timeout(3000)
            context.on("page", lambda popup: popup.close() if popup != page else None)
            page.on("dialog", lambda dialog: dialog.dismiss())
            page.on("download", lambda download: download.cancel())
            page.set_content(captured, wait_until="domcontentloaded", timeout=15000)
            fields = page.locator("input, textarea, select")
            for index in range(fields.count()):
                field = fields.nth(index)
                if not field.is_visible():
                    continue
                data = field.evaluate("""el => ({tag:el.tagName.toLowerCase(), type:el.type,
                    required:el.required || el.getAttribute('aria-required') === 'true',
                    disabled:el.disabled || el.readOnly,
                    labels:[...(el.labels || [])].map(l => l.innerText).concat(
                        [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.name, el.id].filter(Boolean)),
                    value:el.value, checked:el.checked})""")
                if data["disabled"] or data["type"] in {"hidden", "submit", "button", "reset", "image"}:
                    continue
                observed_fields += 1
                label = next(iter(data["labels"]), f"field-{index}")
                key_name, value = _field_value(data["labels"], profile)
                if value is not None and data["type"] in {"text", "email", "tel", "url", "number", "textarea"}:
                    field.fill(value)
                    filled.append({"field": label, "profile_key": f"personal.{key_name}"})
                elif value is not None and data["tag"] == "select":
                    options = field.locator("option").evaluate_all("els => els.map(e => ({label:e.label,value:e.value}))")
                    matches = [o for o in options if value.casefold() in {o["label"].casefold(), o["value"].casefold()}]
                    if len(matches) == 1:
                        field.select_option(matches[0]["value"])
                        filled.append({"field": label, "profile_key": f"personal.{key_name}"})
                    elif data["required"]:
                        missing.append({"field": label, "category": "missing_profile_data"})
                elif data["required"]:
                    # Even prefilled values require known provenance. Never guess
                    # legal checkbox/radio answers, passwords or upload consent.
                    missing.append({"field": label, "category": "upload_required" if data["type"] == "file"
                                    else "missing_profile_data"})
            page.screenshot(path=str(output / "form.png"), full_page=True)
            snapshot = page.locator("body").inner_text()
            (output / "page.txt").write_text(snapshot, encoding="utf-8")
        finally:
            context.close()
            browser.close()
    result = {"status": "dry_run", "verification_state": "not_submitted", "url": url,
              "job_url": job["url"], "filled_fields": filled, "missing_fields": missing,
              "observed_fields": observed_fields, "blocked_requests": blocked,
              "screenshot": str(output / "form.png"), "evidence_path": str(output / "evidence.json"),
              "limitations": ["Captured single-page HTML only; external scripts and all browser network blocked.",
                              "No account login, file upload, final submission or multi-page navigation tested."]}
    (output / "evidence.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
