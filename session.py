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
#     has the OpenVPN accordion sections.
#   - collect_ovpn_links() handles the exclusive accordion on the setup page:
#     clicks each region header in turn and harvests (url, label) pairs after
#     each click. Each link is a custom_installer URL that serves the .ovpn.
#   - download_ovpn_files() fetches each custom_installer URL and saves the
#     resulting .ovpn file using the server location name as the filename.
#
# Pure helpers at the bottom of the file (base_origin, normalize_url,
# filename_from_url, label_to_filename) have no browser dependency and
# are covered by unit tests.

import asyncio
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Frame, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)

# The public marketing site - used as the entry point for navigation
EXPRESSVPN_URL = "https://www.expressvpn.com"

# The authenticated portal that the login flow redirects to.
# Download links and account pages live here after sign-in.
PORTAL_URL = "https://portal.expressvpn.com"

# Local directory where downloaded .ovpn files are written
DOWNLOAD_DIR = Path("ovpn_files")

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
    await page.goto(EXPRESSVPN_URL, wait_until="domcontentloaded")

    # The 'My Account' link is in the top navigation bar
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

    # Wait until the code entry field appears
    await page.wait_for_selector("input", timeout=30_000)

    code = input("Check your inbox and enter the verification code: ").strip()

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
    await page.wait_for_load_state("load", timeout=30_000)
    print("Logged in.\n")


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


async def find_ovpn_download_page(page: Page) -> None:
    """Navigate to the manual-config section of the portal setup page.

    The setup page URL must include the account's subscription_id parameter
    or it shows a generic device-setup page instead of the OpenVPN config
    section. We discover the correct URL from the portal navigation links
    rather than constructing it manually.

    The #manual fragment is appended directly to the URL rather than
    clicking a tab - this is more reliable in headless mode where tab
    clicks can land on the wrong section.

    Raises RuntimeError if navigation fails.
    """
    # Find the subscription-specific setup link from the portal dashboard.
    # This avoids hard-coding a URL structure that includes a private ID.
    setup_url = await _find_subscription_setup_url(page)
    if not setup_url:
        # Fallback - try /setup without subscription_id, which may show a
        # reduced view but might still work for some accounts.
        setup_url = PORTAL_URL + "/setup"
        print(f"Warning: no subscription link found, trying {setup_url} ...")

    # Strip any existing fragment and force the #manual section directly.
    # This is equivalent to clicking the Manual Config tab but works
    # consistently in headless mode.
    manual_url = setup_url.split("#")[0] + "#manual"

    try:
        response = await page.goto(manual_url, wait_until="domcontentloaded", timeout=15_000)
        if not (response and response.ok):
            raise RuntimeError(f"Failed to load the download page ({manual_url})")
    except PlaywrightTimeoutError:
        raise RuntimeError(f"Timed out loading the download page ({manual_url})")


# ---------------------------------------------------------------------------
# Link collection
# ---------------------------------------------------------------------------


REGION_NAMES = ["Americas", "Europe", "Asia Pacific", "Middle East & Africa"]


async def _find_content_frame(page: Page) -> Frame:
    """Return the frame that contains the OpenVPN accordion content.

    The setup page embeds its main content in an iframe, and that iframe
    may take a moment to load after navigation. We poll every 0.5 s for
    up to 10 s waiting for any frame's innerText to contain "Americas"
    (the first region name on the accordion page). Falls back to the main
    frame if nothing is found within the timeout.
    """
    for _ in range(20):  # 20 attempts x 0.5 s = 10 s max wait
        for frame in page.frames:
            try:
                text = await frame.evaluate("() => document.body ? document.body.innerText : ''")
                if "Americas" in text:
                    return frame
            except Exception:
                continue
        await asyncio.sleep(0.5)

    return page.main_frame


async def _click_regions_and_collect(page: Page) -> list[tuple[str, str]]:
    """Find the content frame, then click each continent section header.

    After each click, collects all custom_installer anchor elements from
    the open accordion panel along with their visible text (the server
    location name). Each location name is used as the .ovpn filename.

    Returns a list of (url, label) pairs across all four regions.
    """
    pairs: list[tuple[str, str]] = []

    frame = await _find_content_frame(page)

    for name in REGION_NAMES:
        # Click the BUTTON element whose trimmed text exactly matches the
        # region name. Targeting button specifically avoids clicking an
        # outer wrapper DIV that has the same text but no click handler.
        clicked = await frame.evaluate(
            """name => {
                for (const btn of document.querySelectorAll('button')) {
                    const text = (btn.innerText || btn.textContent || '').trim();
                    if (text === name) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""",
            name,
        )

        if clicked:
            await asyncio.sleep(0.5)

            # Each server location in the open panel is an <a> pointing to
            # https://www.expressvpn.com/custom_installer?cluster_id=N&code=...
            # We collect both the href and the visible location name so we
            # can use the name as the .ovpn filename.
            found: list[dict] = await frame.evaluate(
                """() => Array.from(
                    document.querySelectorAll('[data-state="open"] a[href*="custom_installer"]')
                ).map(a => ({
                    href: a.href,
                    text: (a.innerText || a.textContent || '').trim()
                }))"""
            )

            print(f"  {name:<24} {len(found)} locations")
            for item in found:
                if item["href"]:
                    pairs.append((item["href"], item["text"]))
        else:
            print(f"  Could not find section header for '{name}'")

    return pairs


async def collect_ovpn_links(page: Page) -> list[tuple[str, str]]:
    """Return a deduplicated list of (url, label) pairs for every server location.

    The manual-config section uses an exclusive accordion where only one
    region panel (Americas, Europe, Asia Pacific, Middle East & Africa) can
    be open at a time. Each location entry links to a custom_installer URL
    that serves the .ovpn file for that server. Links only appear in the DOM
    after their panel is opened, so we click each region header in turn and
    collect after each click.

    The label is the visible location name (e.g. "USA - NEW YORK") and is
    used to derive the .ovpn filename via label_to_filename().
    """
    current_origin = base_origin(page.url)

    print("Collecting server list...")
    pairs = await _click_regions_and_collect(page)

    # Resolve any relative hrefs and deduplicate by URL
    normalized = [(normalize_url(url, current_origin), label) for url, label in pairs]
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for url, label in normalized:
        if url not in seen:
            seen.add(url)
            unique.append((url, label))

    return unique


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------


async def download_ovpn_files(
    page: Page, links: list[tuple[str, str]], *, force: bool = False
) -> None:
    """Fetch each custom_installer URL and save the .ovpn content to disk.

    Each URL is fetched using the page's authenticated session (which holds
    the portal cookies). The server returns the raw .ovpn file content.
    The destination filename is derived from the server location label
    (e.g. "USA - NEW YORK" -> "usa_-_new_york.ovpn").

    If force is True, existing files are overwritten instead of skipped.

    A rich progress bar shows the current filename, overall progress, and
    an estimated time to completion that updates as each file is fetched.
    """
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    total = len(links)
    skipped = 0
    failed = 0

    print(f"\nDownloading {total} files to {DOWNLOAD_DIR}/\n")

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.fields[filename]}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
    )

    interrupted = False
    current_dest: Path | None = None

    with progress:
        task = progress.add_task("", total=total, filename="")

        try:
            for url, label in links:
                filename = label_to_filename(label) if label else filename_from_url(url)
                dest = DOWNLOAD_DIR / filename
                progress.update(task, filename=filename)

                if dest.exists() and not force:
                    skipped += 1
                    progress.advance(task)
                    # No delay - skipped files don't hit the network
                    continue

                # Track which file is in-flight so we can remove it if the
                # download is interrupted before the write completes.
                current_dest = dest
                t0 = time.monotonic()
                try:
                    response = await page.request.get(url)
                    if response.ok:
                        dest.write_bytes(await response.body())
                        current_dest = None  # write completed successfully
                    else:
                        current_dest = None
                        failed += 1
                        progress.console.print(
                            f"  [yellow]Failed ({response.status}):[/yellow] {filename}"
                        )
                except Exception as exc:
                    current_dest = None
                    failed += 1
                    progress.console.print(f"  [red]Error:[/red] {filename} - {exc}")

                progress.advance(task)

                # Pad any remaining time up to DOWNLOAD_DELAY_SECONDS so the
                # delay is consistent regardless of how long the request took.
                elapsed = time.monotonic() - t0
                remaining = DOWNLOAD_DELAY_SECONDS - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)

        except KeyboardInterrupt:
            # Remove any partially written file before closing the progress bar
            # so the terminal is left in a tidy state.
            if current_dest is not None and current_dest.exists():
                current_dest.unlink()
            interrupted = True

    if interrupted:
        saved = total - skipped - failed - 1  # last file was not completed
        print(f"\nInterrupted. {saved} files saved to {DOWNLOAD_DIR.resolve()}")
        raise KeyboardInterrupt

    saved = total - skipped - failed
    parts = [f"{saved} downloaded"]
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{failed} failed")
    print(f"\nDone. {', '.join(parts)}. Files saved to {DOWNLOAD_DIR.resolve()}")


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


def label_to_filename(label: str) -> str:
    """Convert a server location label to a safe .ovpn filename.

    The format is designed to round-trip cleanly through the DragonFoxVPN
    PHP backend's prettyName() function, which replaces underscores with
    spaces and splits on " - " to extract the country name for continent
    grouping. The country-city separator " - " is preserved as "_-_" so
    that prettyName("usa_-_new_york") yields "Usa - New York", which
    splits correctly to country key "usa".

    >>> label_to_filename("USA - NEW YORK")
    'usa_-_new_york.ovpn'
    >>> label_to_filename("UK - EAST LONDON")
    'uk_-_east_london.ovpn'
    >>> label_to_filename("SWEDEN")
    'sweden.ovpn'
    """
    name = label.lower()
    # Preserve the country-city separator as _-_ before replacing other chars
    name = name.replace(" - ", "_-_")
    # Replace anything else that is not alphanumeric, underscore, or hyphen
    name = re.sub(r"[^a-z0-9_-]+", "_", name)
    name = name.strip("_")
    return name + ".ovpn"
