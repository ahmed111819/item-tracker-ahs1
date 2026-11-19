"""Validate Amazon Next links and detect repeated listing pages."""

from urllib.parse import parse_qs, urljoin, urlsplit


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
