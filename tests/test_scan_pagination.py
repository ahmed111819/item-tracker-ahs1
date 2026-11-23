"""Exercise the real search loop with repeated listings and failed pages."""

import ast
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest

from scan_pagination import RepeatedPages, next_page_url, prioritized_searches, retry_failed_page


def run(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("Unexpected external async operation.")


async def gather(*coroutines, return_exceptions=False):
    results = []
    for coroutine in coroutines:
        try:
            results.append(await coroutine)
        except Exception as error:
            if not return_exceptions:
                raise
            results.append(error)
    return results


async def sleep(_):
    pass


def search_loop(bot, scan):
    tree = ast.parse(Path(f"scanner_amazon_sa_bot{bot}.py").read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "scan_one_search")
    state = {"scan_state": {}}
    events = []
    namespace = dict(
        asyncio=SimpleNamespace(gather=gather, sleep=sleep), datetime=datetime,
        RepeatedPages=RepeatedPages, next_page_url=next_page_url, scan_one_page=scan,
        tracking_data=state, record_event=lambda *args, **kwargs: events.append(kwargs),
        save_tracking=lambda: None, print=lambda *args: None, BOT_ID=f"bot{bot}",
        MAX_PAGES_PER_SEARCH=30, PAGE_WORKERS_PER_SEARCH=3,
    )
    exec(compile(ast.Module(body=[node], type_ignores=[]), "search_loop", "exec"), namespace)
    return namespace["scan_one_search"], state, events


def page(number, asins, end=False):
    return dict(success=True, asins=set(asins), listing_asins=set(asins),
                next_href=f"/s?i=toys&page={number + 1}", end_confirmed=end)


class PaginationTests(unittest.TestCase):
    def test_long_searches_start_first_and_keep_their_report_indexes(self):
        urls = ["short", "long", "unknown", "fallback"]
        durations = {"short": 30, "long": 800}
        states = {"fallback": {"total_pages": 40}}
        self.assertEqual(prioritized_searches(urls, durations, states),
                         [(1, "long"), (3, "fallback"), (0, "short"), (2, "unknown")])

    def test_deferred_retry_only_requests_one_attempt_and_preserves_incomplete_tail(self):
        url = "https://www.amazon.sa/s?i=toys"
        actual_url = "https://www.amazon.sa/s?i=toys&page=15"
        state = dict(end_reason="failed", next_page=15, deferred_next_url=actual_url)
        calls = []

        async def scan(context, search_url, number, index, **kwargs):
            calls.append((search_url, number, index, kwargs))
            return page(number, {"A"})

        self.assertEqual(run(retry_failed_page(scan, None, url, 4, state)),
                         ("RECOVERED_PAGE_TAIL_UNSCANNED", 1, 0, 0))
        self.assertEqual(calls, [(url, 15, 4, {"page_url_override": actual_url, "attempt_limit": 1})])
        self.assertEqual(state["next_page"], 16)
        self.assertFalse(state["completed"])

    def test_next_link_requires_the_expected_page_and_amazon_host(self):
        self.assertEqual(next_page_url("https://www.amazon.sa/b?node=1", "/s?page=2", 2),
                         "https://www.amazon.sa/s?page=2")
        for href in ("https://other.test/s?page=2", "/s?page=1", "/s?page=2&page=3", "javascript:alert(1)"):
            self.assertIsNone(next_page_url("https://www.amazon.sa/", href, 2))

    def test_repeated_pages_do_not_count_failed_or_changing_results(self):
        tracker = RepeatedPages()
        self.assertFalse(tracker.observe({"A"}))
        self.assertFalse(tracker.observe({"A"}))
        self.assertFalse(tracker.observe({"B"}))
        self.assertFalse(tracker.observe(set()))
        self.assertFalse(tracker.observe({"A"}))
        self.assertFalse(tracker.observe({"B"}))
        self.assertTrue(tracker.observe({"A"}))

    def test_all_scanners_stop_repeated_category_pages_as_incomplete(self):
        for bot in (1, 2, 3):
            async def scan(context, url, number, index, page_url_override=None):
                return page(number, {"A", "B"})
            search, state, events = search_loop(bot, scan)
            url = "https://www.amazon.sa/b?node=1"
            run(search(None, url, 1))
            self.assertEqual(state["scan_state"][url]["end_reason"], "REPEATED_RESULT_PAGES")
            self.assertFalse(state["scan_state"][url]["completed"])
            self.assertEqual(events[-1]["status"], "REPEATED_RESULT_PAGES")

    def test_failed_synthetic_page_recovers_through_previous_next_link(self):
        for bot in (1, 2, 3):
            calls = []
            async def scan(context, url, number, index, page_url_override=None):
                calls.append((number, page_url_override))
                if number == 2 and page_url_override is None:
                    return dict(success=False)
                return page(number, {str(number)}, end=number == 4)
            search, state, events = search_loop(bot, scan)
            url = "https://www.amazon.sa/s?i=toys"
            run(search(None, url, 1))
            self.assertIn((2, "https://www.amazon.sa/s?i=toys&page=2"), calls)
            self.assertTrue(state["scan_state"][url]["completed"])
            self.assertEqual(state["scan_state"][url]["total_pages"], 4)

    def test_repeated_synthetic_page_can_recover_to_new_results(self):
        for bot in (1, 2, 3):
            calls = []
            async def scan(context, url, number, index, page_url_override=None):
                calls.append((number, page_url_override))
                asins = {str(number)} if page_url_override or number == 1 else {"1"}
                return page(number, asins, end=number == 4)
            search, state, events = search_loop(bot, scan)
            url = "https://www.amazon.sa/b?node=1"
            run(search(None, url, 1))
            self.assertTrue(any(number == 2 and override for number, override in calls))
            self.assertTrue(state["scan_state"][url]["completed"])


    def test_disabled_next_span_confirms_the_end_in_all_scanners(self):
        class Locator:
            def __init__(self, selector):
                self.selector = selector
                self.first = self

            async def count(self):
                return 1 if self.selector == ".s-pagination-next" else 0

            async def wait_for(self, **kwargs):
                pass

            async def inner_text(self, **kwargs):
                return ""

            async def get_attribute(self, name):
                return {"class": "s-pagination-next s-pagination-disabled", "aria-disabled": "true"}.get(name)

        class Page:
            url = "https://www.amazon.sa/s?page=5"
            closed = False

            async def goto(self, *args, **kwargs):
                return SimpleNamespace(status=200)

            async def title(self):
                return ""

            def locator(self, selector):
                return Locator(selector)

            async def close(self):
                self.closed = True

        for bot in (1, 2, 3):
            browser_page = Page()

            async def new_page():
                return browser_page

            tree = ast.parse(Path(f"scanner_amazon_sa_bot{bot}.py").read_text(encoding="utf-8"))
            node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "_scan_one_page")
            namespace = dict(MAX_ATTEMPTS_PER_PAGE=1, print=lambda *args: None,
                             build_page_url=lambda *args: browser_page.url)
            exec(compile(ast.Module(body=[node], type_ignores=[]), "page_scan", "exec"), namespace)
            result = run(namespace["_scan_one_page"](SimpleNamespace(new_page=new_page), browser_page.url, 5, 1, {}))
            self.assertTrue(result["success"])
            self.assertTrue(result["end_confirmed"])
            self.assertTrue(browser_page.closed)
