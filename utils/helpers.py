from urllib.parse import urljoin, urlparse, urlunparse

def normalize_url(base_url, href):
    """Join relative or absolute href with base_url and strip fragments."""
    full_url = urljoin(base_url, href)
    parsed = urlparse(full_url)
    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc.lower(),
        parsed.path,
        parsed.params,
        parsed.query,
        ''
    ))
    return normalized

def is_same_domain(url1, url2):
    """Check if url1 and url2 share the same domain (ignoring www. prefix)."""
    try:
        domain1 = urlparse(url1).netloc.lower().replace('www.', '')
        domain2 = urlparse(url2).netloc.lower().replace('www.', '')
        return domain1 == domain2 and domain1 != ''
    except Exception:
        return False

def is_valid_url(url):
    """Validate if URL has valid http/https scheme and netloc."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            return False
        return True
    except Exception:
        return False
