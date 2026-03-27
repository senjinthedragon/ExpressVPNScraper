# tests/test_helpers.py - ExpressVPN OVPN Scraper: Helper Function Unit Tests
# Copyright (c) 2026 Senjin the Dragon.
# https://github.com/senjinthedragon/ExpressVPNScraper
# Licensed under the MIT License.
# See /LICENSE for full license information.
#
# Unit tests for the pure helper functions exported by session.py.
# None of these tests require a browser or network access - they run
# entirely in-process and finish in milliseconds.
#   - TestBaseOrigin     covers scheme+host extraction from full URLs.
#   - TestNormalizeUrl   covers absolute passthrough and relative resolution.
#   - TestFilenameFromUrl covers path extraction and query-string stripping.
#   - TestDeduplicate    covers ordering guarantees and full-URL deduplication.


from session import base_origin, deduplicate, filename_from_url, normalize_url

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
# deduplicate
# ---------------------------------------------------------------------------


class TestDeduplicate:
    def test_empty_list_returns_empty(self):
        assert deduplicate([]) == []

    def test_list_with_no_duplicates_is_unchanged(self):
        items = ["a", "b", "c"]
        assert deduplicate(items) == items

    def test_duplicates_are_removed(self):
        assert deduplicate(["a", "b", "a", "c"]) == ["a", "b", "c"]

    def test_original_order_is_preserved(self):
        # The first occurrence of each item should be kept, not the last
        assert deduplicate(["c", "a", "b", "a", "c"]) == ["c", "a", "b"]

    def test_all_duplicates_collapses_to_one(self):
        assert deduplicate(["x", "x", "x"]) == ["x"]

    def test_works_with_full_urls(self):
        urls = [
            "https://www.expressvpn.com/configs/uk.ovpn",
            "https://www.expressvpn.com/configs/us.ovpn",
            "https://www.expressvpn.com/configs/uk.ovpn",  # duplicate
        ]
        result = deduplicate(urls)
        assert len(result) == 2
        assert result[0].endswith("uk.ovpn")
        assert result[1].endswith("us.ovpn")
