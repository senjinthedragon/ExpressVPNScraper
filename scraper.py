# Copyright (c) 2026 senjinthedragon
# Licensed under the MIT License - see LICENSE file for details.
#
# scraper.py - Main entry point for the ExpressVPN .ovpn config scraper.
#
# Launches a real (headed) Chromium browser, walks through the ExpressVPN
# email-OTP login flow with user input, then navigates to the config
# download page and saves every .ovpn file it finds.
#
# Usage:
#   .venv/bin/python scraper.py

import asyncio
import sys

from playwright.async_api import async_playwright

from session import collect_ovpn_links, download_ovpn_files, find_ovpn_download_page, login

# User-agent string that mirrors a real desktop Chrome on Linux.
# Keeping this consistent helps avoid bot-detection heuristics.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def main():
    async with async_playwright() as pw:
        # Launch a visible browser window - headless mode is more easily
        # fingerprinted as automation, so we use a real window instead.
        browser = await pw.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )

        # Create a browser context with realistic locale and timezone settings
        # so the request profile matches a genuine user session.
        context = await browser.new_context(
            viewport=None,  # Let the OS window size dictate the viewport
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
                print("No .ovpn links found on the current page.")
                print(f"Current URL: {page.url}")
                sys.exit(1)

            # Step 4 - download each file, skipping any already on disk
            await download_ovpn_files(page, links)

        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
