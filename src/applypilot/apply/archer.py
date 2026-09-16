"""Reviewed Archer/Greenhouse form contract and network-isolated live-page preview.

This adapter is preview-only until uploads, dynamic location selection, CAPTCHA
and an attempt-bound submission transport have been qualified. It never submits.
"""

import hashlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.artifacts import verify_job_artifacts

APPLICATION_URL = "https://job-boards.greenhouse.io/archer56/jobs/7652667003"
COMPANY = "Archer"
TITLE = "Mechanical Design Engineer - Thermal and Ducting"

# Employer-owned field IDs and labels, inspected on the public form 2026-09-16.
# A changed/added field fails the contract instead of guessing its meaning.
FIELDS = {
    "first_name": ("First Name", "personal.first_name", True),
    "last_name": ("Last Name", "personal.last_name", True),
    "email": ("Email", "personal.email", True),
    "country": ("Country", "personal.country", True),
    "phone": ("Phone", "personal.phone", True),
    "iti-0__search-input": ("", None, False),
    "candidate-location": ("Location (City)", "personal.city", True),
    "resume": ("Attach", None, False),
    "cover_letter": ("Attach", None, False),
    "question_29923450003": ("LinkedIn Profile", "personal.linkedin_url", False),
    "question_29923451003": ("Website", "personal.portfolio_url", False),
    "question_29923452003": ("What are your base ($USD) expectations?", "employer_answers.archer.base_salary_usd", True),
    "question_29923453003": ("Are you legally authorized to work in the United States?", "work_authorization.legally_authorized_to_work", True),
    "question_29923454003": (
        'Will you now, or at any point during your employment, require Archer to sponsor or petition for a nonimmigrant work visa status on your behalf? (Please note: If you hold a temporary or nonimmigrant status or work authorization, including but not limited to F-1 OPT, F-1 STEM OPT, H-1B, H-4, J-1, L-1, or EAD, please respond with "Yes" so that Archer can determine whether future sponsorship actions may be needed.)',
        "work_authorization.require_sponsorship", True),
    "question_29923455003": (
        "Consistent with company policy, employees are expected to report to work onsite at our offices. Are you open to relocating to our corporate headquarters in San Jose, CA?",
        "employer_answers.archer.relocate_san_jose", True),
    "question_29923456003": ("Were you referred to Archer by a current employee?", "employer_answers.archer.referred", True),
    "question_29923457003": ("If you were referred, please provide the name of the employee.", "employer_answers.archer.referrer_name", True),
    "question_31582971003": (
        "By selecting YES, I consent to receive recruiting SMS messages from Archer Aviation at the phone number provided on above.",
        "employer_answers.archer.sms_consent", False),
    "gender": ("Gender", "eeo_voluntary.gender", False),
    "hispanic_ethnicity": ("Are you Hispanic/Latino?", "eeo_voluntary.hispanic_ethnicity", False),
    "veteran_status": ("Veteran Status", "eeo_voluntary.veteran_status", False),
    "disability_status": ("Disability Status", "eeo_voluntary.disability_status", False),
}


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("*", "")).strip()


def validate_contract(observed: list[dict]) -> str:
    """Require exhaustive field IDs, labels and requiredness; return schema hash."""
    actual = {field["id"]: field for field in observed if field.get("id")}
    if len(actual) != len([field for field in observed if field.get("id")]) or set(actual) != set(FIELDS):
        raise ValueError("Archer form fields changed; adapter review required")
    for key, (label, _, required) in FIELDS.items():
        field = actual[key]
        if _normalize(field.get("label", "")) != label or field.get("required") is not required:
            raise ValueError(f"Archer field label/requiredness changed: {key}")
    schema = [{k: f.get(k) for k in ("id", "label", "required", "type", "role")} for f in observed]
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()


def profile_value(profile: dict, path: str | None) -> str | None:
    value = profile
    if not path:
        return None
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if not isinstance(value, (str, int, float)) or str(value).strip().casefold() in {"", "unknown", "none", "null"}:
        return None
    return str(value).strip()


def cached_location(path: Path, profile: dict) -> tuple[dict, str] | None:
    """Use only a captured public city lookup matching explicit residence fields."""
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        city = profile_value(profile, "personal.city")
        region = profile_value(profile, "personal.province_state")
        country = profile_value(profile, "personal.country")
        request = cache["request"]
        url = urlsplit(request["url"])
        query = parse_qs(url.query)
        response = cache["response"]
        if (not city or not region or country != "United States" or cache.get("status") != 200
                or request.get("method") != "GET" or url.scheme != "https" or url.username or url.password
                or url.hostname != "api-geocode-earth-proxy.greenhouse.io" or url.path != "/v1/autocomplete"
                or query.get("text") != [city] or query.get("layers") != ["locality"]
                or response.get("type") != "FeatureCollection"):
            return None
        matches = [feature["properties"] for feature in response["features"]
                   if feature.get("properties", {}).get("name") == city
                   and feature["properties"].get("region_a") == region
                   and feature["properties"].get("country_code") == "US"]
        if len(matches) != 1:
            return None
        place = matches[0]
        label = f"{place['name']}, {place['region']}, {place['country']}"
        if label not in cache.get("option_labels", []):
            return None
        return response, label
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def preview(job: dict, *, profile: dict | None = None, output_dir: Path | None = None) -> dict:
    """Load the real form read-only, disable network, then fill explicit known data.

    No job/attempt rows are written, no attachments are uploaded, no submit control
    is used, and unresolved custom controls are reported rather than approximated.
    """
    if (job.get("application_url") != APPLICATION_URL or job.get("company") != COMPANY or job.get("title") != TITLE):
        raise ValueError("Queued job does not match this exact Archer adapter")
    profile = config.load_profile() if profile is None else profile
    location = cached_location(config.APP_DIR / "archer-location-cache.json", profile)
    output = output_dir or config.LOG_DIR / "archer-previews" / uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=True)
    locked = False
    blocked, filled, missing, controls = [], [], [], []
    evidence = {"adapter": "archer_greenhouse_preview_v1", "job_url": job["url"],
                "application_url": APPLICATION_URL, "submitted": False,
                "live_submission_supported": False, "status": "form_changed"}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
        context = browser.new_context(service_workers="block", accept_downloads=False)
        try:
            def intercept(route):
                request = route.request
                parts = urlsplit(request.url)
                static = (parts.scheme == "https" and parts.hostname == "job-boards.cdn.greenhouse.io"
                          and parts.path.startswith(("/assets/", "/locales/en/", "/fonts/")))
                query = parse_qs(parts.query)
                if (locked and location and request.method == "GET" and parts.scheme == "https"
                        and parts.hostname == "api-geocode-earth-proxy.greenhouse.io"
                        and parts.path == "/v1/autocomplete" and query.get("layers") == ["locality"]
                        and query.get("text") == [profile_value(profile, "personal.city")]):
                    # Fulfilled locally: no request or candidate data reaches the geocoder.
                    route.fulfill(json=location[0], headers={"access-control-allow-origin": "https://job-boards.greenhouse.io"})
                    evidence["location_cache_replayed"] = True
                    return
                if not locked and request.method == "GET" and (request.url == APPLICATION_URL or static):
                    route.continue_()
                else:
                    blocked.append({"method": request.method, "host": parts.hostname, "path": parts.path})
                    route.abort("blockedbyclient")
            context.route("**/*", intercept)
            context.route_web_socket("**/*", lambda ws: ws.close())
            context.add_init_script("""for (const key of ['RTCPeerConnection','webkitRTCPeerConnection']) {
                Object.defineProperty(window,key,{value:undefined,writable:false,configurable:false}); }
                window.addEventListener('submit',e=>{e.preventDefault();e.stopImmediatePropagation()},true);
                for(const key of ['submit','requestSubmit']) Object.defineProperty(HTMLFormElement.prototype,key,
                    {value:()=>{},writable:false,configurable:false});""")
            page = context.new_page()
            context.on("page", lambda popup: popup.close() if popup != page else None)
            page.on("download", lambda download: download.cancel())
            page.on("dialog", lambda dialog: dialog.dismiss())
            response = page.goto(APPLICATION_URL, wait_until="networkidle", timeout=45000)
            if not response or response.status != 200 or page.url != APPLICATION_URL:
                raise ValueError("Archer application page unavailable or redirected")
            body = page.locator("body").inner_text()
            if page.locator("h1").inner_text().strip() != TITLE or COMPANY not in body:
                raise ValueError("Archer employer/title does not match")
            # From this point even GETs are denied before any candidate data enters DOM.
            locked = True
            observed = page.locator("input[id],textarea[id],select[id]").evaluate_all("""els => els.map(el=>({
                id:el.id, label:[...(el.labels||[])].map(l=>l.innerText).join(' '),
                required:el.required||el.getAttribute('aria-required')==='true',
                type:el.type,role:el.getAttribute('role')}))""")
            (output / "fields.json").write_text(json.dumps(observed, indent=2), encoding="utf-8")
            evidence["schema_sha256"] = validate_contract(observed)
            evidence["http_status"] = response.status
            for item in observed:
                key = item["id"]
                _, fact_path, required = FIELDS[key]
                value = profile_value(profile, fact_path)
                if key == "question_29923457003" and profile_value(profile, "employer_answers.archer.referred") == "No":
                    value = "Not applicable (no employee referral)"
                    fact_path = "employer_answers.archer.referred"
                if key in {"resume", "cover_letter"}:
                    continue
                if value is None:
                    if required:
                        missing.append({"field": key, "question": FIELDS[key][0], "profile_path": fact_path})
                    continue
                field = page.locator(f'[id="{key}"]')
                if item["role"] == "combobox":
                    if key == "candidate-location" and not location:
                        controls.append({"field": key, "reason": "Live geocoder selection requires separate validation"})
                        continue
                    field.click()
                    field.fill(value)
                    options = page.get_by_role("option")
                    try:
                        options.first.wait_for(state="visible", timeout=2500)
                    except PlaywrightError:
                        pass
                    labels = options.all_text_contents()
                    expected = location[1] if key == "candidate-location" and location else value
                    matches = [label for label in labels if _normalize(label).casefold() == expected.casefold()
                               or (key == "country" and value == "United States" and label.strip() == "United States +1")]
                    if len(matches) != 1:
                        controls.append({"field": key, "reason": "No unique exact option", "options": labels})
                        field.press("Escape")
                        continue
                    page.get_by_role("option", name=matches[0], exact=True).click()
                else:
                    field.fill(value)
                filled.append({"field": key, "profile_path": fact_path})
            try:
                files = verify_job_artifacts(job)
                evidence["documents"] = {kind: str(path) for kind, path in files.items()}
            except ValueError as exc:
                missing.append({"field": "resume", "reason": str(exc)})
            evidence.update(status="needs_input" if missing or controls else "preview_only", filled=filled,
                            missing_fields=missing, unresolved_controls=controls,
                            limitations=["No submission transport validated", "No external upload attempted",
                                         "CAPTCHA and live location lookup remain unvalidated"])
            page.screenshot(path=str(output / "form.png"), full_page=True)
            (output / "form.html").write_text(page.content(), encoding="utf-8")
            (output / "fields.json").write_text(json.dumps(observed, indent=2), encoding="utf-8")
        except (ValueError, OSError, PlaywrightError) as exc:
            evidence.update(error=str(exc), status="form_changed")
        finally:
            evidence["blocked_requests"] = blocked
            evidence["evidence_path"] = str(output / "evidence.json")
            (output / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            browser.close()
    return evidence
