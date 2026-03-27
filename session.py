# session.py - ExpressVPN OVPN Scraper: Browser Session and Download Logic
# Copyright (c) 2026 Senjin the Dragon.
# https://github.com/senjinthedragon/ExpressVPNScraper
# Licensed under the MIT License.
# See /LICENSE for full license information.
#
# Contains all Playwright-driven browser logic and the pure helper functions
# that support it:
#   - login() walks the email-OTP flow, handling both a single code input
#     and the individual-digit-box layout that some browsers get.
#   - find_ovpn_download_page() navigates to /setup and confirms the page
#     has .ovpn links or accordion sections containing them.
#   - collect_ovpn_links() handles the exclusive accordion on the setup page:
#     it clicks each section toggle in turn, harvests links after each, then
#     deduplicates across all sections. Falls back to a direct scan if no
#     accordion is present.
#   - download_ovpn_files() triggers each download via an injected <a> element
#     (matching real browser behaviour) with an authenticated-request fallback.
#
# Pure helpers at the bottom of the file (base_origin, normalize_url,
# filename_from_url, deduplicate) have no browser dependency and are
# covered by unit tests.

import asyncio
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

# The public marketing site - used as the entry point for navigation
EXPRESSVPN_URL = "https://www.expressvpn.com"

# The authenticated portal that the login flow redirects to.
# Download links and account pages live here after sign-in.
PORTAL_URL = "https://portal.expressvpn.com"

# Local directory where downloaded .ovpn files are written
DOWNLOAD_DIR = Path("ovpn_files")

# Candidate paths to try on each known base URL when searching for the
# config download page. Tried in order - first match with .ovpn links wins.
# The portal paths are tried first since that is where the session lands
# after login; the main-site paths are a fallback.
DOWNLOAD_PAGE_CANDIDATES: list[tuple[str, str]] = [
    # /setup is the manual config page - the portal appends the subscription_id
    # query parameter automatically for authenticated sessions.
    (PORTAL_URL, "/setup"),
    (PORTAL_URL, "/setup/manual"),
    (PORTAL_URL, "/vpn-configs"),
    (PORTAL_URL, "/downloads"),
    (PORTAL_URL, "/dashboard"),
    (EXPRESSVPN_URL, "/vpn-software/vpn-configs"),
    (EXPRESSVPN_URL, "/support/vpn-setup/manual-config-expressvpn-with-openvpn/"),
]

# Seconds to wait between individual file downloads - keeps request
# timing consistent with a human clicking through a download list.
DOWNLOAD_DELAY_SECONDS = 0.8


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


async def login(page: Page) -> None:
    """Walk through the ExpressVPN email-OTP login flow.

    The user is prompted for their email address and, once ExpressVPN
    sends the verification code, for that code as well. The browser
    fills in both fields so the session cookies are set exactly as they
    would be for a real user.
    """
    print("Navigating to ExpressVPN...")
    await page.goto(EXPRESSVPN_URL, wait_until="domcontentloaded")

    # The 'My Account' link is in the top navigation bar
    print("Looking for 'My Account' link...")
    account_link = page.get_by_role("link", name=re.compile(r"my account", re.IGNORECASE))
    await account_link.first.click()
    await page.wait_for_load_state("domcontentloaded")

    # Prompt the user for their email and fill it into the login form
    email = input("Enter your ExpressVPN account email: ").strip()
    email_input = page.get_by_role("textbox", name=re.compile(r"email", re.IGNORECASE))
    await email_input.fill(email)

    # Submit the email to trigger the OTP email
    submit = page.get_by_role(
        "button",
        name=re.compile(r"send|continue|next|sign in", re.IGNORECASE),
    )
    await submit.first.click()
    print("Email submitted. Check your inbox for the verification code.")

    # Wait until the code entry field appears
    await page.wait_for_selector("input", timeout=30_000)

    code = input("Enter the verification code from your email: ").strip()

    # ExpressVPN may render the code field as a single text input or as
    # individual single-digit boxes - handle both cases.
    inputs = page.locator("input[type='text'], input[type='number'], input[type='tel']")
    count = await inputs.count()

    if count == 1:
        # Single input - paste the full code
        await inputs.nth(0).fill(code)
    elif count >= len(code):
        # One box per digit - fill each character separately
        for i, digit in enumerate(code):
            await inputs.nth(i).fill(digit)
    else:
        print(f"Unexpected input count ({count}). Attempting to fill first input.")
        await inputs.nth(0).fill(code)

    # Submit the code to complete authentication
    confirm = page.get_by_role(
        "button",
        name=re.compile(r"verify|confirm|sign in|continue|submit", re.IGNORECASE),
    )
    await confirm.first.click()

    # Wait for the portal redirect to complete. We use "load" rather than
    # "networkidle" because the portal keeps background requests running
    # indefinitely (analytics, popups, etc.) and networkidle never fires.
    print("Waiting for login to complete...")
    await page.wait_for_load_state("load", timeout=30_000)
    print(f"Logged in. Current URL: {page.url}")


# ---------------------------------------------------------------------------
# Download page navigation
# ---------------------------------------------------------------------------


async def _find_subscription_setup_url(page: Page) -> str | None:
    """Look for the subscription-specific setup URL on the current portal page.

    The portal setup page requires a subscription_id query parameter to show
    the OpenVPN manual-config section with per-region .ovpn download links.
    Without it, the page shows a generic activation-code view instead.

    The portal dashboard always includes navigation links that carry the
    correct subscription_id, so we find one here rather than constructing
    the URL ourselves.
    """
    links = await page.locator("a[href*='subscription_id']").all()
    for link in links:
        href = await link.get_attribute("href")
        if href and "setup" in href:
            return normalize_url(href, PORTAL_URL)
    return None


async def find_ovpn_download_page(page: Page) -> bool:
    """Navigate to the manual-config section of the portal setup page.

    The setup page URL must include the account's subscription_id parameter
    or it shows a generic device-setup page instead of the OpenVPN config
    section. We discover the correct URL from the portal navigation links
    rather than constructing it manually.

    Steps:
      1. Find a setup link on the current portal page that includes the
         subscription_id and navigate to it.
      2. Click the Manual Config tab to reach the #manual section.
      3. Return True if the URL contains #manual (confirming the right
         section loaded).

    Returns False if navigation fails so the caller can ask the user to
    navigate manually.
    """
    # Find the subscription-specific setup link from the portal dashboard.
    # This avoids hard-coding a URL structure that includes a private ID.
    setup_url = await _find_subscription_setup_url(page)
    if setup_url:
        print("Found subscription setup URL, navigating...")
    else:
        # Fallback - try /setup without subscription_id, which may show a
        # reduced view but might still work for some accounts.
        setup_url = PORTAL_URL + "/setup"
        print(f"No subscription link found, trying {setup_url} ...")

    try:
        response = await page.goto(setup_url, wait_until="domcontentloaded", timeout=15_000)
        if not (response and response.ok):
            return False
    except PlaywrightTimeoutError:
        return False

    # The setup page has OS/platform tabs. Click the one for manual
    # OpenVPN configuration to land on the #manual section.
    manual_tab = page.get_by_role("link", name=re.compile(r"manual", re.IGNORECASE))
    if not await manual_tab.count():
        manual_tab = page.get_by_role("tab", name=re.compile(r"manual", re.IGNORECASE))
    if await manual_tab.count():
        print("Clicking Manual Config tab...")
        await manual_tab.first.click()
        await asyncio.sleep(0.5)
    else:
        print("Manual tab not found - page may already be on the right section.")

    if "manual" in page.url.lower():
        print("Manual config section ready.")
        return True

    if await page.locator("a[href$='.ovpn']").count() > 0:
        print("Config page ready.")
        return True

    print(f"Could not confirm manual config section. Current URL: {page.url}")
    return False


# ---------------------------------------------------------------------------
# Link collection
# ---------------------------------------------------------------------------


REGION_NAMES = ["Americas", "Europe", "Asia Pacific", "Middle East & Africa"]


async def _click_regions_and_collect(page: Page) -> list[str]:
    """Find and click each continent section header using JavaScript.

    When Playwright locators cannot find the region toggle elements (because
    they may be plain divs or other non-semantic elements), we fall back to
    a JavaScript DOM walk that finds elements whose trimmed text exactly
    matches one of the four continent names, clicks each one in turn, and
    collects .ovpn links after each click.

    Returns a flat list of raw hrefs collected across all regions.
    """
    raw_hrefs: list[str] = []

    # Diagnostic - print the first 800 chars of visible page text and
    # the number of iframes so we can see what has actually rendered.
    page_text = await page.evaluate("() => document.body.innerText.trim().substring(0, 800)")
    frame_count = len(page.frames)
    print(f"  [diag] frames={frame_count}, page text snippet:")
    print(f"  {page_text[:400]}")
    print("  ---")

    for name in REGION_NAMES:
        # Walk the full DOM and click the first element whose trimmed
        # innerText equals the target region name exactly.
        clicked = await page.evaluate(
            """name => {
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_ELEMENT
                );
                while (walker.nextNode()) {
                    const el = walker.currentNode;
                    const text = (el.innerText || el.textContent || "").trim();
                    if (text === name) {
                        el.click();
                        return el.tagName + " | " + (el.className || "(no class)");
                    }
                }
                return null;
            }""",
            name,
        )

        if clicked:
            print(f"  Clicked '{name}' ({clicked}) - collecting links...")
            await asyncio.sleep(0.5)
            raw_hrefs.extend(await _scrape_links_from_current_view(page))
        else:
            print(f"  Could not find section header for '{name}'")

    return raw_hrefs


async def _scrape_links_from_current_view(page: Page) -> list[str]:
    """Collect all .ovpn hrefs visible in the current DOM state.

    Checks both standard anchor hrefs and data-href / data-url attributes.
    Returns raw hrefs (may be relative) - the caller normalises them.
    """
    hrefs: list[str] = []

    anchors = await page.locator("a[href$='.ovpn']").all()
    for anchor in anchors:
        href = await anchor.get_attribute("href")
        if href:
            hrefs.append(href)

    data_els = await page.locator("[data-href$='.ovpn'], [data-url$='.ovpn']").all()
    for el in data_els:
        href = await el.get_attribute("data-href") or await el.get_attribute("data-url")
        if href:
            hrefs.append(href)

    return hrefs


async def collect_ovpn_links(page: Page) -> list[str]:
    """Return a deduplicated list of absolute .ovpn download URLs.

    The manual-config section uses an exclusive accordion where only one
    region panel (Americas, Europe, Asia Pacific, Middle East & Africa) can
    be open at a time. Links only appear in the DOM after their panel is
    opened, so we iterate through all four region names, click each header
    via a JavaScript DOM walk (which works regardless of element type), and
    collect links after each click.

    Falls back to a direct single-pass scan if no region headers are clicked
    (e.g. if the page layout changes and region names are different).

    Relative hrefs are resolved against the current page's origin so they
    work correctly whether we are on the portal or the main site.
    """
    raw_hrefs: list[str] = []
    current_origin = base_origin(page.url)

    # Use JavaScript to find and click each region header regardless of
    # what element type ExpressVPN uses for the accordion toggles.
    print("Clicking through region sections...")
    js_hrefs = await _click_regions_and_collect(page)

    if js_hrefs:
        raw_hrefs.extend(js_hrefs)
    else:
        # JS approach found nothing either - fall back to a direct scan
        print("No region sections clicked - scanning page directly.")
        raw_hrefs.extend(await _scrape_links_from_current_view(page))

    absolute = [normalize_url(h, current_origin) for h in raw_hrefs]
    return deduplicate(absolute)


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------


async def download_ovpn_files(page: Page, links: list[str]) -> None:
    """Download each .ovpn URL and write it to DOWNLOAD_DIR.

    First attempts a JS-triggered download (which preserves the
    browser's download dialogue semantics). If that times out, falls
    back to a plain authenticated GET request reusing the session
    cookies already held by the browser context.
    """
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    print(f"\nDownloading {len(links)} .ovpn file(s) to '{DOWNLOAD_DIR}/'...")

    for i, url in enumerate(links, 1):
        filename = filename_from_url(url)
        dest = DOWNLOAD_DIR / filename

        if dest.exists():
            print(f"  [{i}/{len(links)}] Already exists, skipping: {filename}")
            continue

        try:
            # Trigger a download by injecting a temporary <a> element and
            # clicking it - this keeps the download flow identical to what
            # a user would do manually in the browser.
            async with page.expect_download(timeout=30_000) as dl_info:
                await page.evaluate(
                    """url => {
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = '';
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                    }""",
                    url,
                )
            download = await dl_info.value
            await download.save_as(dest)
            print(f"  [{i}/{len(links)}] Downloaded: {filename}")

        except PlaywrightTimeoutError:
            # JS-triggered download did not fire - fall back to fetching
            # the file directly using the page's authenticated session.
            try:
                response = await page.request.get(url)
                if response.ok:
                    dest.write_bytes(await response.body())
                    print(f"  [{i}/{len(links)}] Downloaded (fallback): {filename}")
                else:
                    print(f"  [{i}/{len(links)}] Failed ({response.status}): {url}")
            except Exception as exc:
                print(f"  [{i}/{len(links)}] Error: {exc} - {url}")

        # Brief pause between requests to match human download cadence
        await asyncio.sleep(DOWNLOAD_DELAY_SECONDS)

    print(f"\nDone. Files saved to '{DOWNLOAD_DIR.resolve()}'")


# ---------------------------------------------------------------------------
# Pure helper functions (no browser dependency - tested in tests/)
# ---------------------------------------------------------------------------


def base_origin(url: str) -> str:
    """Return the scheme and host of a URL, with no trailing slash.

    Used to resolve relative hrefs against whatever domain the browser
    is currently on - which may be portal.expressvpn.com after login.

    >>> base_origin("https://portal.expressvpn.com/dashboard")
    'https://portal.expressvpn.com'
    >>> base_origin("https://www.expressvpn.com/setup/manual")
    'https://www.expressvpn.com'
    """
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_url(href: str, base_url: str) -> str:
    """Return an absolute URL, prepending base_url if href is relative.

    >>> normalize_url("/configs/uk.ovpn", "https://www.expressvpn.com")
    'https://www.expressvpn.com/configs/uk.ovpn'
    >>> normalize_url("https://cdn.example.com/us.ovpn", "https://www.expressvpn.com")
    'https://cdn.example.com/us.ovpn'
    """
    if href.startswith("http://") or href.startswith("https://"):
        return href
    # Ensure we don't double up the slash between base and path
    return base_url.rstrip("/") + "/" + href.lstrip("/")


def filename_from_url(url: str) -> str:
    """Extract a clean filename from a URL, stripping any query string.

    >>> filename_from_url("https://example.com/configs/uk-london.ovpn?v=2")
    'uk-london.ovpn'
    >>> filename_from_url("https://example.com/us-new-york.ovpn")
    'us-new-york.ovpn'
    """
    return url.split("/")[-1].split("?")[0]


def deduplicate(items: list[str]) -> list[str]:
    """Return a list with duplicates removed, preserving original order.

    >>> deduplicate(["a", "b", "a", "c"])
    ['a', 'b', 'c']
    """
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
