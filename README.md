# ExpressVPN OVPN Scraper

Automates downloading all available `.ovpn` configuration files from your
ExpressVPN account. Useful when you need the full server list for a VPN
tray application or similar tooling, and don't want to click through 160+
individual download links by hand.

## How it works

The script opens a Chromium browser in the background - no window appears.
It walks through the same login flow you would use manually:

1. You enter your email address in the terminal
2. ExpressVPN sends a verification code to that email
3. You paste the code into the terminal
4. The script navigates to the manual config page, opens each region section
   (Americas, Europe, Asia Pacific, Middle East & Africa) in turn, and
   downloads every `.ovpn` file it finds

Files are saved to `ovpn_files/` with names derived from the server location
(e.g. `usa_-_new_york.ovpn`, `uk_-_east_london.ovpn`). The naming convention
is compatible with the [DragonFoxVPN](https://github.com/senjinthedragon/DragonFoxVPN)
backend - leave `CONF_PREFIX` empty in your config and the files will be
picked up and grouped correctly without any adjustments.

Already-downloaded files are skipped on subsequent runs.

## Requirements

- Python 3.12+
- An active ExpressVPN subscription

## Installation

```bash
git clone https://github.com/senjinthedragon/ExpressVPNScraper.git
cd ExpressVPNScraper

python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows
.venv\Scripts\activate

# fish shell
source .venv/bin/activate.fish

pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
python scraper.py
```

A browser will open in the background. Follow the prompts in the terminal
to complete the login. A progress bar shows the current file, overall
progress, and an estimated time to completion.

Expect around 30-60 minutes to download the full server list - the
ExpressVPN endpoint generates each config file on demand, so downloads
are not instant.

## Notes

- No credentials are ever stored - the script only uses the live browser
  session that you authenticate yourself
- Downloads are spaced out to match normal human browsing behaviour
- Re-running the script will skip any files already present in `ovpn_files/`

## License

MIT - see [LICENSE](LICENSE)
