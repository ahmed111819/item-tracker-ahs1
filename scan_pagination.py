"""Validate Amazon Next links and detect repeated listing pages."""

from urllib.parse import parse_qs, urljoin, urlsplit


def prioritized_searches(urls, durations, states):
    """Start longer searches first while preserving their configured report IDs."""
    def estimated_seconds(job):
        _, url = job
        seconds = durations.get(url)
        if isinstance(seconds, (int, float)) and seconds > 0:
            return seconds
        pages = states.get(url, {}).get("total_pages")
        return pages * 5 if isinstance(pages, int) and pages > 0 else 0

    return sorted(enumerate(urls), key=estimated_seconds, reverse=True)


async def retry_failed_page(scan_page, context, url, search_index, state):
    """Give a failed page one final attempt after the search queue has drained."""
    number = state.get("next_page")
    if state.get("end_reason") != "failed" or not isinstance(number, int) or number < 1:
        return None
    actual_url = next_page_url(url, state.get("deferred_next_url"), number)
    print(f"Search {search_index}: making one deferred attempt on page {number}.")
    try:
        result = await scan_page(context, url, number, search_index,
                                 page_url_override=actual_url, attempt_limit=1)
    except Exception as error:
        result = {"failure_reason": type(error).__name__}
    if not result.get("success"):
        state["failure_reason"] = result.get("failure_reason", "unconfirmed page")
        print(f"Search {search_index}: deferred page {number} failed ({state['failure_reason']}).")
        return "failed", 0, 0, 0

    completed = bool(result.get("end_confirmed"))
    status = "AMAZON_NEXT_DISABLED" if completed else "RECOVERED_PAGE_TAIL_UNSCANNED"
    state.update(completed=completed, end_reason=status, next_page=None if completed else number + 1)
    if completed:
        state["total_pages"] = number
    state.pop("failure_reason", None)
    state.pop("deferred_next_url", None)
    state.pop("last_error_at", None)
    print(f"Search {search_index}: recovered deferred page {number} ({status}).")
    return status, 1, result.get("products_saved", 0), result.get("alerts_sent", 0)


def next_page_url(search_url, href, page_number):
    if not href:
        return None
    candidate = urljoin(search_url, href)
    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or parsed.hostname not in {"www.amazon.sa", "amazon.sa"}:
        return None
    if parsed.username or parsed.password:
        return None
    if parse_qs(parsed.query).get("page") != [str(page_number)]:
        return None
    return candidate


class RepeatedPages:
    def __init__(self):
        self.fingerprints = set()
        self.repeated_count = 0

    def repeated(self, asins):
        return bool(asins) and frozenset(asins) in self.fingerprints

    def observe(self, asins):
        fingerprint = frozenset(asins)
        if not fingerprint:
            self.repeated_count = 0
            return False
        if fingerprint in self.fingerprints:
            self.repeated_count += 1
        else:
            self.fingerprints.add(fingerprint)
            self.repeated_count = 0
        return self.repeated_count >= 3
