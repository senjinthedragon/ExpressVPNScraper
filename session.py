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
#   - find_ovpn_download_page() navigates to the /setup page and expands
#     all accordion sections so every .ovpn link is present in the DOM.
#   - expand_all_accordions() clicks every collapsed [aria-expanded=false]
#     element to reveal the per-region download links.
#   - collect_ovpn_links() scrapes anchor hrefs and data-href / data-url
#     attributes, normalises relative URLs, and deduplicates the result.
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


async def expand_all_accordions(page: Page) -> None:
    """Click every collapsed accordion section on the current page.

    The /setup page groups download links by region (Americas, Europe, etc.)
    inside accordion panels. Each panel uses aria-expanded="false" when
    collapsed, so we find and click all of them in sequence. The links are
    only injected into the DOM after their panel is open, which is why we
    must expand everything before scanning for .ovpn hrefs.
    """
    # Keep clicking collapsed sections until there are none left - handles
    # the case where expanding one panel reveals nested collapsed panels.
    for _ in range(10):
        collapsed = await page.locator("[aria-expanded='false']").all()
        if not collapsed:
            break
        for el in collapsed:
            try:
                await el.click()
                # Brief pause so the panel animation can finish and the DOM
                # can settle before we look for newly revealed elements.
                await asyncio.sleep(0.3)
            except Exception:
                # Skip any element that is not clickable (e.g. hidden or
                # covered by an overlay) and move on to the next.
                pass


async def find_ovpn_download_page(page: Page) -> bool:
    """Navigate to the /setup page and expand all accordion sections.

    The portal's manual-config page (portal.expressvpn.com/setup) groups
    .ovpn download links inside per-region accordion panels. The links do
    not exist in the DOM until the panels are opened, so this function:
      1. Navigates to /setup (the portal appends the subscription_id
         automatically for authenticated sessions).
      2. Calls expand_all_accordions() to open every panel.
      3. Verifies that .ovpn links are now present.

    Falls back to the broader candidate list if /setup is unreachable,
    and returns False if nothing works so the caller can ask the user to
    navigate manually.
    """
    # Try the known setup page first - fastest path
    setup_url = PORTAL_URL + "/setup"
    print(f"Navigating to {setup_url} ...")
    try:
        response = await page.goto(setup_url, wait_until="domcontentloaded", timeout=15_000)
        if response and response.ok:
            await expand_all_accordions(page)
            links = await page.locator("a[href$='.ovpn']").all()
            if links:
                print(f"Found {len(links)} .ovpn link(s) after expanding accordions.")
                return True
    except PlaywrightTimeoutError:
        pass

    # /setup did not work - try the remaining candidate URLs
    for base, path in DOWNLOAD_PAGE_CANDIDATES:
        url = base + path
        if url == setup_url:
            continue  # already tried above
        print(f"Trying {url} ...")
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        except PlaywrightTimeoutError:
            continue

        if response and response.ok:
            await expand_all_accordions(page)
            links = await page.locator("a[href$='.ovpn']").all()
            if links:
                print(f"Found {len(links)} .ovpn link(s) at {url}")
                return True

    # None of the candidates worked - look for a config/setup nav link on
    # whatever page we are currently on and follow it.
    print("Searching current page for config or setup links...")
    config_link = page.get_by_role(
        "link",
        name=re.compile(r"openvpn|config|manual setup", re.IGNORECASE),
    )
    if await config_link.count() > 0:
        await config_link.first.click()
        await page.wait_for_load_state("domcontentloaded")
        await expand_all_accordions(page)
        return await page.locator("a[href$='.ovpn']").count() > 0

    return False


# ---------------------------------------------------------------------------
# Link collection
# ---------------------------------------------------------------------------


async def collect_ovpn_links(page: Page) -> list[str]:
    """Return a deduplicated list of absolute .ovpn download URLs.

    Checks both standard anchor hrefs and data attributes (data-href,
    data-url) that some download buttons use instead of plain anchors.
    Relative hrefs are resolved against the current page's origin so
    they work correctly whether we are on the portal or the main site.
    """
    raw_hrefs: list[str] = []

    # Standard <a href="...ovpn"> links
    anchors = await page.locator("a[href$='.ovpn']").all()
    for anchor in anchors:
        href = await anchor.get_attribute("href")
        if href:
            raw_hrefs.append(href)

    # Buttons or divs with data-href / data-url attributes
    data_els = await page.locator("[data-href$='.ovpn'], [data-url$='.ovpn']").all()
    for el in data_els:
        href = await el.get_attribute("data-href") or await el.get_attribute("data-url")
        if href:
            raw_hrefs.append(href)

    # Resolve relative hrefs against the domain we are currently on,
    # not the hardcoded main site URL.
    current_origin = base_origin(page.url)
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
