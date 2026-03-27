# scraper.py - ExpressVPN OVPN Scraper: Browser Entry Point and Orchestration
# Copyright (c) 2026 Senjin the Dragon.
# https://github.com/senjinthedragon/ExpressVPNScraper
# Licensed under the MIT License.
# See /LICENSE for full license information.
#
# Launches a headless Chromium browser and orchestrates the four-step scrape:
#   - login() prompts for email and OTP code, fills them into the live browser
#     so the resulting session is indistinguishable from a real user login.
#   - find_ovpn_download_page() navigates to the manual config section
#     automatically, raising RuntimeError if navigation fails.
#   - collect_ovpn_links() harvests every .ovpn URL from the page.
#   - download_ovpn_files() saves each file to ovpn_files/ on disk.
#
# Usage:
#   python scraper.py                         # download everything
#   python scraper.py netherlands             # substring filter on location name
#   python scraper.py --country nl            # country-code lookup
#   python scraper.py --file usa_-_new_york   # single file by DragonFoxVPN filename
#   python scraper.py --country nl --force    # re-download even if files exist

import argparse
import asyncio
import os
import re
import signal
import sys

from playwright.async_api import async_playwright

from session import (
    collect_ovpn_links,
    download_ovpn_files,
    find_ovpn_download_page,
    label_to_filename,
    login,
)

VERSION = "1.0.0"

# Maps ISO 3166-1 alpha-2 country codes to the country name as it appears
# in ExpressVPN location labels. A few non-standard codes are included for
# convenience (e.g. "uk" alongside "gb", "us" alongside the "usa" label).
COUNTRY_CODES: dict[str, str] = {
    "ae": "uae",
    "ar": "argentina",
    "at": "austria",
    "au": "australia",
    "be": "belgium",
    "bg": "bulgaria",
    "br": "brazil",
    "ca": "canada",
    "ch": "switzerland",
    "cl": "chile",
    "co": "colombia",
    "cy": "cyprus",
    "cz": "czech republic",
    "de": "germany",
    "dk": "denmark",
    "ee": "estonia",
    "es": "spain",
    "fi": "finland",
    "fr": "france",
    "gb": "uk",
    "gr": "greece",
    "hk": "hong kong",
    "hr": "croatia",
    "hu": "hungary",
    "id": "indonesia",
    "ie": "ireland",
    "il": "israel",
    "in": "india",
    "is": "iceland",
    "it": "italy",
    "jp": "japan",
    "ke": "kenya",
    "kr": "south korea",
    "lt": "lithuania",
    "lu": "luxembourg",
    "lv": "latvia",
    "mx": "mexico",
    "my": "malaysia",
    "ng": "nigeria",
    "nl": "netherlands",
    "no": "norway",
    "nz": "new zealand",
    "pa": "panama",
    "pe": "peru",
    "ph": "philippines",
    "pl": "poland",
    "pt": "portugal",
    "ro": "romania",
    "se": "sweden",
    "sg": "singapore",
    "sk": "slovakia",
    "th": "thailand",
    "tr": "turkey",
    "tw": "taiwan",
    "ua": "ukraine",
    "uk": "uk",
    "us": "usa",
    "za": "south africa",
}

# User-agent string that mirrors a real desktop Chrome on Linux.
# Keeping this consistent helps avoid bot-detection heuristics.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_BANNER_WIDTH = 70


def _parse_args() -> argparse.Namespace:
    code_entries = [f"{code}={name}" for code, name in sorted(COUNTRY_CODES.items())]
    # Wrap into rows of 4 entries each for readable --help output
    rows = ["  ".join(code_entries[i : i + 4]) for i in range(0, len(code_entries), 4)]
    parser = argparse.ArgumentParser(
        description="Download ExpressVPN .ovpn config files.",
        epilog="Supported country codes:\n  " + "\n  ".join(rows),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "filter",
        nargs="?",
        default=None,
        metavar="FILTER",
        help=(
            "case-insensitive substring matched against location names "
            "(e.g. 'netherlands', 'usa', 'new york'). "
            "Downloads all locations if omitted."
        ),
    )
    parser.add_argument(
        "--country",
        metavar="CODE",
        default=None,
        help=(
            "ISO 3166-1 alpha-2 country code (e.g. 'nl', 'us', 'de'). "
            "Matches only locations in that country."
        ),
    )
    parser.add_argument(
        "--file",
        metavar="FILENAME",
        default=None,
        help=(
            "exact filename in DragonFoxVPN format to download "
            "(e.g. 'usa_-_new_york' or 'usa_-_new_york.ovpn'). "
            "The .ovpn extension is optional."
        ),
    )
    parser.add_argument(
        "--email",
        metavar="ADDRESS",
        default=None,
        help="account email address, skipping the interactive prompt.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="overwrite files that already exist on disk instead of skipping them.",
    )
    args = parser.parse_args()
    selectors = [x for x in (args.filter, args.country, args.file) if x]
    if len(selectors) > 1:
        parser.error("FILTER, --country, and --file cannot be combined")
    return args


def _print_banner() -> None:
    """Print the startup banner, with colour on TTY terminals."""
    can_color = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    purple = "\033[38;5;93m" if can_color else ""
    gold = "\033[38;5;220m" if can_color else ""
    reset = "\033[0m" if can_color else ""

    lines = [
        f" EXPRESSVPN OVPN SCRAPER - v{VERSION}",
        " Developed by Senjin the Dragon  https://github.com/senjinthedragon",
        " Please support my work: https://github.com/sponsors/senjinthedragon",
        " Bitcoin: bc1qjsaqw6rjcmhv6ywv2a97wfd4zxnae3ncrn8mf9",
    ]
    bar = "\u2550" * _BANNER_WIDTH
    print(f"\n{purple}\u2554{bar}\u2557")
    for line in lines:
        print(f"\u2551{gold}{line.ljust(_BANNER_WIDTH)}{purple}\u2551")
    print(f"\u255a{bar}\u255d{reset}\n")


async def main(
    location_filter: str | None,
    country_code: str | None,
    file_target: str | None,
    force: bool,
    email: str | None,
) -> None:
    # asyncio.run() installs its own SIGINT handler that defers KeyboardInterrupt
    # until the event loop can process it. During blocking input() calls the
    # loop is stalled, so the first Ctrl+C gets swallowed. Restoring Python's
    # default handler makes Ctrl+C raise KeyboardInterrupt immediately.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    _print_banner()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # Create a browser context with realistic locale and timezone settings
        # so the request profile matches a genuine user session.
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await context.new_page()

        try:
            # Step 1 - log in via the email OTP flow (user provides both inputs)
            await login(page, email=email)

            # Step 2 - navigate to the .ovpn download page
            await find_ovpn_download_page(page)

            # Step 3 - collect every .ovpn link on the current page
            links = await collect_ovpn_links(page)

            if not links:
                print("No download links found. The page layout may have changed.")
                sys.exit(1)

            # Apply the optional location filter before downloading
            if file_target:
                # Normalise: ensure the target has a .ovpn extension
                target = file_target if file_target.endswith(".ovpn") else file_target + ".ovpn"
                links = [(url, label) for url, label in links if label_to_filename(label) == target]
                if not links:
                    print(f"No location found matching filename '{target}'.")
                    sys.exit(1)
                print(f"File '{target}': matched.\n")
            elif country_code:
                # --country: anchored match so 'uk' doesn't match 'ukraine' etc.
                code = country_code.lower()
                if code not in COUNTRY_CODES:
                    known = ", ".join(sorted(COUNTRY_CODES))
                    print(f"Unknown country code '{code}'. Known codes: {known}")
                    sys.exit(1)
                name = COUNTRY_CODES[code]
                pattern = re.compile(rf"^{re.escape(name)}( - |$)", re.IGNORECASE)
                links = [(url, label) for url, label in links if pattern.match(label)]
                if not links:
                    print(f"No locations found for country code '{code}' ({name}).")
                    sys.exit(1)
                print(f"Country '{code}' ({name}): {len(links)} location(s) matched.\n")
            elif location_filter:
                needle = location_filter.lower()
                links = [(url, label) for url, label in links if needle in label.lower()]
                if not links:
                    print(f"No locations matched '{location_filter}'.")
                    sys.exit(1)
                print(f"Filter '{location_filter}': {len(links)} location(s) matched.\n")

            # Step 4 - download each file, skipping any already on disk
            await download_ovpn_files(page, links, force=force)

        except KeyboardInterrupt:
            pass  # download_ovpn_files prints a summary if interrupted there
        except RuntimeError as exc:
            print(f"\nError: {exc}")
            sys.exit(1)
        finally:
            try:
                await browser.close()
            except Exception:
                pass  # driver may already be gone on interrupt


if __name__ == "__main__":
    args = _parse_args()
    try:
        asyncio.run(main(args.filter, args.country, args.file, args.force, args.email))
    except KeyboardInterrupt:
        pass
