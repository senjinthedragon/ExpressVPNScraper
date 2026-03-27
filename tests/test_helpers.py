# tests/test_helpers.py - ExpressVPN OVPN Scraper: Helper Function Unit Tests
# Copyright (c) 2026 Senjin the Dragon.
# https://github.com/senjinthedragon/ExpressVPNScraper
# Licensed under the MIT License.
# See /LICENSE for full license information.
#
# Unit tests for the pure helper functions exported by session.py.
# None of these tests require a browser or network access - they run
# entirely in-process and finish in milliseconds.
#   - TestBaseOrigin      covers scheme+host extraction from full URLs.
#   - TestNormalizeUrl    covers absolute passthrough and relative resolution.
#   - TestFilenameFromUrl covers path extraction and query-string stripping.
#   - TestLabelToFilename covers location label to .ovpn filename conversion.


from session import base_origin, filename_from_url, label_to_filename, normalize_url

BASE = "https://www.expressvpn.com"


# ---------------------------------------------------------------------------
# base_origin
# ---------------------------------------------------------------------------


class TestBaseOrigin:
    def test_returns_scheme_and_host_only(self):
        assert (
            base_origin("https://portal.expressvpn.com/dashboard")
            == "https://portal.expressvpn.com"
        )

    def test_works_on_main_site(self):
        assert (
            base_origin("https://www.expressvpn.com/setup/manual") == "https://www.expressvpn.com"
        )

    def test_strips_deep_path(self):
        assert base_origin("https://example.com/a/b/c/d.ovpn") == "https://example.com"

    def test_http_scheme_preserved(self):
        assert base_origin("http://example.com/page") == "http://example.com"

    def test_no_trailing_slash(self):
        result = base_origin("https://portal.expressvpn.com/")
        assert not result.endswith("/")


# ---------------------------------------------------------------------------
# normalize_url
# ---------------------------------------------------------------------------


class TestNormalizeUrl:
    def test_absolute_https_url_is_returned_unchanged(self):
        url = "https://cdn.expressvpn.com/configs/uk.ovpn"
        assert normalize_url(url, BASE) == url

    def test_absolute_http_url_is_returned_unchanged(self):
        url = "http://cdn.expressvpn.com/configs/uk.ovpn"
        assert normalize_url(url, BASE) == url

    def test_relative_path_is_prepended_with_base(self):
        assert normalize_url("/configs/uk.ovpn", BASE) == BASE + "/configs/uk.ovpn"

    def test_relative_path_without_leading_slash(self):
        # Handles the edge case where the href has no leading slash
        assert normalize_url("configs/uk.ovpn", BASE) == BASE + "/configs/uk.ovpn"

    def test_base_trailing_slash_does_not_produce_double_slash(self):
        base_with_slash = BASE + "/"
        assert normalize_url("/configs/uk.ovpn", base_with_slash) == BASE + "/configs/uk.ovpn"

    def test_empty_path_segment(self):
        # Should not crash on an edge-case empty relative href
        result = normalize_url("", BASE)
        assert result == BASE + "/"


# ---------------------------------------------------------------------------
# filename_from_url
# ---------------------------------------------------------------------------


class TestFilenameFromUrl:
    def test_plain_url_returns_last_path_segment(self):
        assert filename_from_url("https://example.com/us-new-york.ovpn") == "us-new-york.ovpn"

    def test_query_string_is_stripped(self):
        url = "https://example.com/uk-london.ovpn?v=2&token=abc"
        assert filename_from_url(url) == "uk-london.ovpn"

    def test_deep_path_returns_filename_only(self):
        assert filename_from_url("https://example.com/a/b/c/de-berlin.ovpn") == "de-berlin.ovpn"

    def test_no_query_string_works_normally(self):
        assert filename_from_url("https://example.com/jp-tokyo.ovpn") == "jp-tokyo.ovpn"


# ---------------------------------------------------------------------------
# label_to_filename
# ---------------------------------------------------------------------------


class TestLabelToFilename:
    def test_us_city_label(self):
        # " - " separator is preserved as "_-_" for PHP backend compatibility
        assert label_to_filename("USA - NEW YORK") == "usa_-_new_york.ovpn"

    def test_uk_label(self):
        assert label_to_filename("UK - EAST LONDON") == "uk_-_east_london.ovpn"

    def test_ampersand_becomes_underscore(self):
        # & is not alphanumeric, collapses into surrounding underscores
        assert label_to_filename("MIDDLE EAST & AFRICA") == "middle_east_africa.ovpn"

    def test_parentheses_stripped(self):
        assert label_to_filename("INDIA (VIA UK)") == "india_via_uk.ovpn"

    def test_numbered_suffix(self):
        # Multiple " - " separators all become "_-_"
        assert label_to_filename("USA - LOS ANGELES - 3") == "usa_-_los_angeles_-_3.ovpn"

    def test_single_word_label(self):
        assert label_to_filename("SWEDEN") == "sweden.ovpn"

    def test_multiword_country_no_city(self):
        # Countries with spaces but no city use underscores between words
        assert label_to_filename("SOUTH AFRICA") == "south_africa.ovpn"

    def test_already_lowercase_is_unchanged(self):
        assert label_to_filename("japan - tokyo") == "japan_-_tokyo.ovpn"
