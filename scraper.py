# scraper.py - ExpressVPN OVPN Scraper: Browser Entry Point and Orchestration
# Copyright (c) 2026 Senjin the Dragon.
# https://github.com/senjinthedragon/ExpressVPNScraper
# Licensed under the MIT License.
# See /LICENSE for full license information.
#
# Launches a headless Chromium browser and orchestrates the four-step scrape:
#   - login() prompts for email and OTP code, fills them into the live browser
#     so the resulting session is indistinguishable from a real user login.
#   - find_ovpn_download_page() navigates to the config download page
#     automatically, or pauses and asks the user to do it manually.
#   - collect_ovpn_links() harvests every .ovpn URL from the page.
#   - download_ovpn_files() saves each file to ovpn_files/ on disk.
#
# Usage:
#   .venv/bin/python scraper.py

import asyncio
import os
import sys

from playwright.async_api import async_playwright

from session import collect_ovpn_links, download_ovpn_files, find_ovpn_download_page, login

VERSION = "1.0.0"

# User-agent string that mirrors a real desktop Chrome on Linux.
# Keeping this consistent helps avoid bot-detection heuristics.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_BANNER_WIDTH = 70


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


async def main():
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
            await login(page)

            # Step 2 - navigate to the .ovpn download page
            found = await find_ovpn_download_page(page)

            if not found:
                # If automatic navigation fails, let the user take over and
                # manually browse to the right page before we continue.
                print("\nCould not automatically locate the .ovpn download page.")
                print("Please navigate to it in the browser, then press Enter here.")
                input("Press Enter when you are on the download page: ")

            # Step 3 - collect every .ovpn link on the current page
            links = await collect_ovpn_links(page)

            if not links:
                print("No download links found on the current page.")
                print(f"Current URL: {page.url}")
                sys.exit(1)

            # Step 4 - download each file, skipping any already on disk
            await download_ovpn_files(page, links)

        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
