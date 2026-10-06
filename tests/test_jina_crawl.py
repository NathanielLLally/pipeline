"""
Unit tests for the Jina site crawler (flow/jina_crawl.py).

Everything asserted here is pure: the selection SQL, the parsing of a Jina
response, and the row that gets persisted. The HTTP call and the database
write are the only impure parts and are exercised separately.

The sample payload is a real r.jina.ai response, trimmed in the middle only.
"""

import json

import pytest

from flow.jina_crawl import (
    PAGE_KIND,
    build_insert,
    build_target_query,
    clean_text,
    normalize_url,
    parse_jina_response,
    shape_row,
)

SAMPLE = json.dumps({
    "code": 200,
    "status": 20000,
    "data": {
        "title": "Happy Tails Paw Care",
        "description": "A pet services growth agency",
        "url": "http://happytailspawcare.com/",
        # Trimmed in the middle, but kept well past MIN_CONTENT_CHARS: a real
        # homepage runs to thousands of characters, and a fixture under the
        # floor would be testing the short-content gate by accident.
        "content": "We Fetch the Clients You Handle the Pets\n\n"
                   "Qualified leads, zero hustle.\n\n"
                   + "Lead Generation. CRM Integration. SEO Optimization. "
                     "Website Design. Email Outreach. Content Strategy. " * 8,
        "links": {
            "Contact": "https://happytailspawcare.com/#contact",
            "info@happytailspawcare.com": "mailto:info@happytailspawcare.com",
        },
        "metadata": {"lang": "en", "description": "A pet services growth agency"},
        "httpStatus": 200,
        "httpStatusText": "",
        "usage": {"tokens": 1064},
    },
    "meta": {"usage": {"tokens": 1064}},
})

BUSINESS = {"id": "11111111-1111-1111-1111-111111111111",
            "name": "Happy Tails Paw Care",
            "website": "happytailspawcare.com"}


class TestTargetQuery:
    def test_default_mode_takes_businesses_with_no_usable_crawl_text(self):
        """The whole point of the default run: what enrich-websites.mjs missed."""
        sql, _ = build_target_query(limit=10)

        assert "leads.website_crawl" in sql
        assert "text_excerpt" in sql
        assert "not exists" in sql.lower()

    def test_default_mode_also_retries_businesses_whose_pages_errored(self):
        sql, _ = build_target_query(limit=10)

        assert "fetch_error is not null" in sql
        assert "http_status" in sql

    def test_all_mode_drops_the_failure_predicate(self):
        sql, _ = build_target_query(limit=10, mode="all")

        assert "fetch_error is not null" not in sql

    def test_skips_what_this_crawler_already_fetched(self):
        sql, _ = build_target_query(limit=10)

        assert "leads.website_crawl_jina" in sql

    def test_refetch_stops_skipping_its_own_prior_rows(self):
        sql, _ = build_target_query(limit=10, refetch=True)

        assert "leads.website_crawl_jina" not in sql

    def test_carries_over_the_mjs_exclusions(self):
        """Same filters as scripts/enrich-websites.mjs buildWhere()."""
        sql, _ = build_target_query(limit=10)

        assert "facebook" in sql
        assert "REJECTED" in sql

    def test_limit_is_a_bound_parameter_not_interpolated(self):
        sql, params = build_target_query(limit=25)

        assert "25" not in sql
        assert 25 in params

    def test_tier_filter_is_a_bound_parameter(self):
        sql, params = build_target_query(limit=10, tiers=["Tier 1", "Tier 2"])

        assert "icp_tier" in sql
        assert ["Tier 1", "Tier 2"] in params
        assert "Tier 1" not in sql

    def test_service_category_filter_is_a_bound_parameter(self):
        sql, params = build_target_query(limit=10,
                                         service_categories=["dog_training"])

        assert "service_category" in sql
        assert ["dog_training"] in params
        assert "dog_training" not in sql

    def test_no_filter_clauses_when_none_requested(self):
        """icp_tier is still SELECTed; what must be absent is any = any($n)."""
        sql, params = build_target_query(limit=10)

        assert "= any(" not in sql
        assert params == [10]

    def test_orders_by_icp_score_so_the_budget_goes_to_the_best_rows(self):
        sql, _ = build_target_query(limit=10)

        assert "icp_score desc" in sql.lower()


class TestBusinessIds:
    """Naming businesses crawls exactly those, whatever they have already got."""

    def test_selects_only_the_named_businesses(self):
        sql, params = build_target_query(limit=10, business_ids=["b1", "b2"])
        assert "b.id = any(" in sql
        assert ["b1", "b2"] in params

    def test_ids_are_bound_parameters_not_interpolated(self):
        sql, _ = build_target_query(limit=10,
                                    business_ids=["x'; drop table t; --"])
        assert "drop table" not in sql

    def test_named_businesses_bypass_the_gap_and_already_fetched_filters(self):
        """The caller asked for these; skipping one silently would look like
        the crawl simply did nothing."""
        sql, _ = build_target_query(limit=10, business_ids=["b1"])
        assert "website_crawl_jina" not in sql
        assert "fetch_error" not in sql

    def test_named_businesses_still_need_a_crawlable_website(self):
        sql, _ = build_target_query(limit=10, business_ids=["b1"])
        assert "b.website is not null" in sql
        assert "qc_status" in sql

    def test_limit_still_bounds_the_named_set(self):
        _, params = build_target_query(limit=3, business_ids=["a", "b", "c", "d"])
        assert params[0] == 3

    def test_flow_passes_ids_through_to_selection(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from flow import jina_crawl as jc
        with patch("flow.jina_crawl.select_targets",
                   new=AsyncMock(return_value=[])) as sel:
            asyncio.run(jc.jina_crawl.fn(business_ids=["b1"]))
        assert sel.await_args.kwargs["business_ids"] == ["b1"]

    def test_cli_takes_comma_separated_ids(self):
        from flow.jina_crawl import _parse_args, _split
        args = _parse_args(["--business-id", "b1, b2"])
        assert _split(args.business_id) == ["b1", "b2"]

    def test_flow_signature_accepts_none(self):
        """Prefect rejects a None default on a non-Optional annotation."""
        import typing
        from flow.jina_crawl import jina_crawl
        hint = typing.get_type_hints(jina_crawl.fn)["business_ids"]
        assert type(None) in typing.get_args(hint)


class TestResponseParsing:
    def test_reads_content_title_and_final_url(self):
        parsed = parse_jina_response(SAMPLE)

        assert parsed["text_excerpt"].startswith("We Fetch the Clients")
        assert parsed["payload"]["title"] == "Happy Tails Paw Care"
        assert parsed["final_url"] == "http://happytailspawcare.com/"
        assert parsed["fetch_error"] is None

    def test_reports_the_sites_own_status_not_jinas(self):
        """httpStatus is the crawled site's; `code` is Jina's own."""
        parsed = parse_jina_response(SAMPLE)

        assert parsed["http_status"] == 200

    def test_keeps_the_links_summary_for_later_email_extraction(self):
        parsed = parse_jina_response(SAMPLE)

        assert parsed["payload"]["links"]["info@happytailspawcare.com"] == \
            "mailto:info@happytailspawcare.com"

    def test_content_is_never_duplicated_into_the_payload(self):
        """text_excerpt holds the page text; storing it twice doubles the table."""
        parsed = parse_jina_response(SAMPLE)

        assert "content" not in parsed["payload"]

    def test_empty_content_is_a_failure_not_a_crawled_page(self):
        raw = json.dumps({"code": 200, "data": {"url": "https://x.example",
                                                "content": "   ",
                                                "httpStatus": 200}})
        parsed = parse_jina_response(raw)

        assert parsed["text_excerpt"] is None
        assert parsed["fetch_error"] == "empty content"

    def test_a_tracker_redirect_stub_is_a_failure_not_a_crawled_page(self):
        """Observed live: an ad-click URL resolved to 37 chars with status 200.

        Jina had followed match.adsrvr.org and rendered the tracker, not the
        site. Stored as-is it reads downstream as usable page text.
        """
        raw = json.dumps({"code": 200, "data": {
            "url": "https://match.adsrvr.org/track/cmf/google",
            "title": "https://match.adsrvr.org/track/cmf/google",
            "content": "https://match.adsrvr.org/track/cmf/google",
            "httpStatus": 200}})
        parsed = parse_jina_response(raw)

        assert parsed["text_excerpt"] is None
        assert "too short" in parsed["fetch_error"]

    def test_real_page_text_well_over_the_floor_is_kept(self):
        raw = json.dumps({"code": 200, "data": {"url": "https://x.example",
                                                "content": "word " * 100,
                                                "httpStatus": 200}})
        parsed = parse_jina_response(raw)

        assert parsed["text_excerpt"] is not None

    def test_a_jina_error_envelope_becomes_a_fetch_error(self):
        raw = json.dumps({"code": 422, "status": 42206,
                          "name": "AssertionFailureError",
                          "message": "Failed to goto https://x.example"})
        parsed = parse_jina_response(raw)

        assert parsed["text_excerpt"] is None
        assert parsed["http_status"] == 422
        assert "Failed to goto" in parsed["fetch_error"]

    def test_non_json_body_becomes_a_fetch_error_rather_than_raising(self):
        parsed = parse_jina_response("<html>502 Bad Gateway</html>")

        assert parsed["text_excerpt"] is None
        assert parsed["fetch_error"]


class TestCleanText:
    def test_strips_nul_and_the_other_c0_controls(self):
        """Postgres text cannot store \\u0000; it aborts the whole transaction."""
        assert clean_text("a\x00b\x01c") == "a b c"

    def test_keeps_newlines_and_tabs(self):
        """The markdown's structure is content, not noise."""
        assert clean_text("a\nb\tc") == "a\nb\tc"

    def test_none_stays_none(self):
        assert clean_text(None) is None


class TestRowShaping:
    def test_builds_a_row_keyed_on_the_business_and_the_requested_url(self):
        row = shape_row(BUSINESS, "https://happytailspawcare.com/",
                        parse_jina_response(SAMPLE))

        assert row["business_id"] == BUSINESS["id"]
        assert row["url"] == "https://happytailspawcare.com/"
        assert row["page_kind"] == PAGE_KIND
        assert row["text_excerpt"].startswith("We Fetch")
        assert row["fetch_error"] is None

    def test_a_failed_fetch_still_produces_a_row(self):
        """A recorded failure is how a later pass knows not to retry blindly."""
        row = shape_row(BUSINESS, "https://x.example",
                        {"text_excerpt": None, "payload": {}, "final_url": None,
                         "http_status": 0, "fetch_error": "timeout"})

        assert row["text_excerpt"] is None
        assert row["fetch_error"] == "timeout"
        assert row["url"] == "https://x.example"

    def test_page_text_is_cleaned_on_the_way_in(self):
        raw = json.dumps({"code": 200, "data": {"url": "https://x.example",
                                                "content": "ok\x00text " * 60,
                                                "httpStatus": 200}})
        row = shape_row(BUSINESS, "https://x.example", parse_jina_response(raw))

        assert "\x00" not in row["text_excerpt"]


class TestLinkExtraction:
    def test_extracts_text_and_href_pairs_from_jina_links_map(self):
        from flow.jina_crawl import extract_links

        raw = json.dumps({"code": 200, "data": {"url": "https://x.example",
                                                "content": "ok " * 200,
                                                "links": {
                                                    "Contact": "https://x.example/contact",
                                                    "info@x.example": "mailto:info@x.example",
                                                },
                                                "httpStatus": 200}})
        parsed = parse_jina_response(raw)
        links = extract_links(BUSINESS, "https://x.example", parsed)

        assert len(links) == 2
        assert links[0]["link_text"] == "Contact"
        assert links[0]["link_href"] == "https://x.example/contact"
        assert links[1]["link_text"] == "info@x.example"
        assert links[1]["link_href"] == "mailto:info@x.example"
        assert all(l["business_id"] == BUSINESS["id"] for l in links)
        assert all(l["url"] == "https://x.example" for l in links)

    def test_link_rows_carry_the_crawl_url_not_the_link_target(self):
        """Links are keyed on the page they came from, not where they point."""
        from flow.jina_crawl import extract_links

        raw = json.dumps({"code": 200, "data": {"url": "https://x.example/about",
                                                "content": "ok " * 200,
                                                "links": {"Home": "https://x.example/"},
                                                "httpStatus": 200}})
        links = extract_links(BUSINESS, "https://x.example/about",
                             parse_jina_response(raw))

        assert links[0]["url"] == "https://x.example/about"
        assert links[0]["link_href"] == "https://x.example/"

    def test_empty_links_map_produces_no_rows(self):
        from flow.jina_crawl import extract_links

        raw = json.dumps({"code": 200, "data": {"url": "https://x.example",
                                                "content": "ok " * 200,
                                                "links": {},
                                                "httpStatus": 200}})
        links = extract_links(BUSINESS, "https://x.example",
                             parse_jina_response(raw))

        assert len(links) == 0

    def test_links_only_when_crawl_succeeded(self):
        from flow.jina_crawl import extract_links

        # No content, so the crawl itself failed.
        parsed = {"text_excerpt": None, "payload": {}, "final_url": None,
                  "http_status": 0, "fetch_error": "timeout"}
        links = extract_links(BUSINESS, "https://x.example", parsed)

        assert len(links) == 0


class TestUrlNormalization:
    def test_adds_a_scheme_when_gbp_stored_a_bare_domain(self):
        assert normalize_url("happytailspawcare.com") == \
            "https://happytailspawcare.com/"

    def test_leaves_an_explicit_scheme_alone(self):
        assert normalize_url("http://x.example/about") == \
            "http://x.example/about"

    def test_unparseable_input_is_none_rather_than_an_exception(self):
        assert normalize_url("") is None
        assert normalize_url(None) is None
        assert normalize_url("not a url at all") is None


class TestBuildLinkInsert:
    def test_builds_upsert_for_links_table(self):
        from flow.jina_crawl import build_link_insert, extract_links

        raw = json.dumps({"code": 200, "data": {"url": "https://x.example",
                                                "content": "ok " * 200,
                                                "links": {"Contact": "https://x.example/contact"},
                                                "httpStatus": 200}})
        links = extract_links(BUSINESS, "https://x.example", parse_jina_response(raw))
        sql, params = build_link_insert(links)

        assert "leads.website_crawl_jina_link" in sql
        assert "on conflict" in sql.lower()
        assert len(params) == 1
        assert json.loads(params[0])[0]["business_id"] == BUSINESS["id"]

    def test_links_travel_as_json_parameter(self):
        """No text splicing, no injection vectors."""
        from flow.jina_crawl import build_link_insert, extract_links

        raw = json.dumps({"code": 200, "data": {"url": "https://x.example",
                                                "content": "ok " * 200,
                                                "links": {"'; DROP TABLE leads.--": "https://x.example"},
                                                "httpStatus": 200}})
        links = extract_links(BUSINESS, "https://x.example", parse_jina_response(raw))
        sql, _ = build_link_insert(links)

        assert "DROP TABLE" not in sql

    def test_empty_links_list_produces_no_insert(self):
        from flow.jina_crawl import build_link_insert

        sql, params = build_link_insert([])

        assert sql == ""
        assert params == []


class TestInsert:
    def test_upserts_on_business_and_url_so_a_rerun_is_resumable(self):
        sql, _ = build_insert([shape_row(BUSINESS, "https://x.example",
                                         parse_jina_response(SAMPLE))])

        assert "leads.website_crawl_jina" in sql
        assert "on conflict" in sql.lower()
        assert "do update" in sql.lower()

    def test_rows_travel_as_one_bound_json_parameter(self):
        """Page text spliced into SQL text is both an injection and a NUL hazard."""
        rows = [shape_row(BUSINESS, "https://x.example",
                          parse_jina_response(SAMPLE))]
        sql, params = build_insert(rows)

        assert "Happy Tails" not in sql
        assert len(params) == 1
        assert json.loads(params[0])[0]["business_id"] == BUSINESS["id"]

    def test_never_writes_content_bytes(self):
        """The dev table deliberately mirrors website_crawl minus that column."""
        sql, _ = build_insert([shape_row(BUSINESS, "https://x.example",
                                         parse_jina_response(SAMPLE))])

        assert "content_bytes" not in sql

    def test_deduplicates_a_repeated_url_within_one_batch(self):
        """Postgres refuses to update the same row twice in one statement."""
        row = shape_row(BUSINESS, "https://x.example",
                        parse_jina_response(SAMPLE))
        _, params = build_insert([row, dict(row)])

        assert len(json.loads(params[0])) == 1
