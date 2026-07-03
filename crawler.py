"""
crawler.py – High-performance SEO metadata crawler
====================================================
Architecture
------------
* Single shared Playwright browser + context reused across ALL pages.
* True async concurrency via asyncio.Semaphore (default 10 parallel tabs).
* Resource blocking (images / fonts / CSS / media) cuts page-load time ~60 %.
* Smart metadata wait: polls until <title> is non-default, then grabs HTML.
* Retry logic: up to MAX_RETRIES attempts with exponential back-off.
* requests fast-path for static/SSR pages (skips browser entirely).
* Thread-safe progress callback so Streamlit updates in real time.
* Works on Linux (Hugging Face Docker) and Windows.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup

from core.parser import extract_metadata_from_html
from utils.helpers import is_same_domain, is_valid_url, normalize_url
from utils.logger import logger

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}

NAV_TIMEOUT   = 20_000   # ms – max time to navigate to a page
META_TIMEOUT  = 8_000    # ms – max time to wait for metadata to appear
EXTRA_WAIT    = 800      # ms – extra settle time after metadata detected
MAX_RETRIES   = 2        # retry failed pages this many times
MAX_CONCURRENT = 10      # parallel browser tabs

# Resource types to block (saves bandwidth + time)
BLOCKED_TYPES = {"image", "media", "font", "stylesheet", "other"}

# Chromium launch flags for Docker / headless Linux + Windows
BROWSER_ARGS = [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--hide-scrollbars",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--ignore-certificate-errors",
    "--disable-breakpad",
    "--disable-default-apps",
    "--disable-hang-monitor",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--proxy-server=direct://",
    "--proxy-bypass-list=*",
]

# Install Chromium once on startup (idempotent, silent)
try:
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False, timeout=120, capture_output=True,
    )
except Exception:
    pass

# ─────────────────────────────────────────────────────────────────────────────
# PROGRESS HELPER
# ─────────────────────────────────────────────────────────────────────────────

def safe_progress(callback: Optional[Callable], pct: int, text: str) -> None:
    """Call the Streamlit progress callback safely from any thread."""
    if not callback:
        return
    try:
        callback(min(max(int(pct), 0), 100), str(text))
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# STATIC / SSR FAST PATH  (no browser needed)
# ─────────────────────────────────────────────────────────────────────────────

def _requests_get(url: str, timeout: int = 15) -> tuple[str, int]:
    """Plain HTTP fetch via requests. Returns (html, status) or ('', 0)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return resp.text, resp.status_code
    except Exception as e:
        logger.debug(f"requests GET failed {url}: {e}")
    return "", 0


def _is_csr_shell(html: str) -> bool:
    """
    True when the server returns a bare React/Vue/Angular shell –
    i.e. <div id='root'> or <div id='app'> is empty and a JS bundle exists.
    These pages need Playwright; requests will always return identical metadata.
    """
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("div", id="root") or soup.find("div", id="app")
    root_empty = root is not None and len(root.get_text(strip=True)) < 30
    has_bundle = bool(
        soup.find(
            "script",
            src=lambda s: s and any(
                k in s for k in ("/static/js/main", "bundle.js", "app.js", "index.js")
            ),
        )
    )
    return root_empty and has_bundle

# ─────────────────────────────────────────────────────────────────────────────
# SITEMAP FETCHING  (pure requests – no browser needed)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_sitemap_xml(xml_text: str) -> list[str]:
    """Parse a sitemap or sitemap-index XML string and return all <loc> URLs."""
    xml_text = re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    urls: list[str] = []
    tag = root.tag.lower()

    if "sitemapindex" in tag:
        # Recurse into each child sitemap
        for child in root:
            for sub in child:
                if sub.tag.endswith("loc") and sub.text:
                    child_text, _ = _requests_get(sub.text.strip(), timeout=10)
                    if child_text:
                        urls.extend(_parse_sitemap_xml(child_text))
    elif "urlset" in tag:
        for child in root:
            for sub in child:
                if sub.tag.endswith("loc") and sub.text:
                    urls.append(sub.text.strip())

    return urls


def fetch_sitemap_urls(base_url: str, progress_callback: Optional[Callable] = None) -> list[str]:
    """
    Attempt to fetch a sitemap via robots.txt → common paths.
    Returns list of URLs or [] if no sitemap found.
    """
    safe_progress(progress_callback, 5, "Checking for sitemap.xml…")

    candidates: list[str] = []

    # robots.txt may advertise Sitemap: URL
    robots_text, _ = _requests_get(base_url.rstrip("/") + "/robots.txt", timeout=10)
    for line in robots_text.splitlines():
        if line.lower().startswith("sitemap:"):
            candidates.append(line.split(":", 1)[1].strip())

    candidates += [
        base_url.rstrip("/") + "/sitemap.xml",
        base_url.rstrip("/") + "/sitemap_index.xml",
        base_url.rstrip("/") + "/sitemap/sitemap.xml",
    ]

    for sm_url in dict.fromkeys(candidates):      # dedup, preserve order
        xml_text, status = _requests_get(sm_url, timeout=10)
        if status == 200 and xml_text.strip():
            urls = _parse_sitemap_xml(xml_text)
            if urls:
                logger.info(f"Sitemap at {sm_url}: {len(urls)} URLs")
                return urls

    return []

# ─────────────────────────────────────────────────────────────────────────────
# ASYNC CRAWLER CORE
# ─────────────────────────────────────────────────────────────────────────────

async def _setup_page_interception(page) -> None:
    """
    Abort requests for resource types we don't need.
    Keeps JS (required for React) but drops images, CSS, fonts, media.
    Saves ~60 % of network traffic and ~40 % of load time.
    """
    async def _route_handler(route, request):
        if request.resource_type in BLOCKED_TYPES:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", _route_handler)


async def _wait_for_metadata(page, default_title: str) -> None:
    """
    Wait until the page has rendered meaningful SEO metadata.
    Specifically watches for the <title> to differ from the shell default
    (catches react-helmet / vue-meta updates) OR for an <h1> to appear.
    Falls back gracefully on timeout so we always return whatever exists.
    """
    try:
        await page.wait_for_function(
            f"""() => {{
                const title = (document.title || '').trim();
                const defaultTitle = {repr(default_title)};
                const titleChanged = title.length > 0 && title !== defaultTitle;
                const hasDesc = (() => {{
                    const m = document.querySelector('meta[name="description"]');
                    return m && m.content && m.content.trim().length > 0;
                }})();
                const hasH1 = !!document.querySelector('h1');
                return titleChanged || hasDesc || hasH1;
            }}""",
            timeout=META_TIMEOUT,
        )
        await page.wait_for_timeout(EXTRA_WAIT)
    except Exception:
        # Timeout is fine — take whatever is in the DOM right now
        pass


async def _fetch_one(
    url: str,
    context,
    semaphore: asyncio.Semaphore,
    default_title: str,
    retries: int = MAX_RETRIES,
) -> tuple[str, str | int]:
    """
    Fetch a single URL inside the shared browser context.
    Retries up to `retries` times on failure with exponential back-off.
    Returns (html, status_code).
    """
    attempt = 0
    last_error: Exception | None = None

    while attempt <= retries:
        async with semaphore:
            page = await context.new_page()
            try:
                await _setup_page_interception(page)

                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT
                )
                status = response.status if response else 200

                await _wait_for_metadata(page, default_title)

                html = await page.content()
                return html, status

            except Exception as e:
                last_error = e
                attempt += 1
                wait_s = attempt * 2          # 2 s, then 4 s back-off
                logger.warning(
                    f"Attempt {attempt}/{retries+1} failed for {url}: {e}"
                    + (f" — retrying in {wait_s}s" if attempt <= retries else "")
                )
                if attempt <= retries:
                    await asyncio.sleep(wait_s)
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    return "", f"Error after {retries+1} attempts: {last_error}"


async def _crawl_async(
    urls: list[str],
    force_playwright: bool,
    default_title: str,
    max_concurrent: int,
    progress_callback: Optional[Callable],
    start_pct: int,
) -> list[dict]:
    """
    Core async crawl loop.
    Launches ONE browser + ONE context, then fans out to `max_concurrent`
    tabs in parallel using asyncio.Semaphore.
    """
    from playwright.async_api import async_playwright

    results: list[dict | None] = [None] * len(urls)
    total = len(urls)
    completed = 0
    lock = asyncio.Lock()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=BROWSER_ARGS,
            chromium_sandbox=False,
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            ignore_https_errors=True,
            java_script_enabled=True,
            bypass_csp=True,
        )

        semaphore = asyncio.Semaphore(max_concurrent)

        async def process(index: int, url: str) -> None:
            nonlocal completed

            # Fast path: static/SSR pages don't need a browser
            html, status = "", 0
            if not force_playwright:
                html, status = _requests_get(url, timeout=15)
                if html and _is_csr_shell(html):
                    html, status = "", 0   # shell – fall through to Playwright

            # Playwright path
            if not html:
                html, status = await _fetch_one(url, context, semaphore, default_title)

            # Parse metadata
            if isinstance(status, int) and status == 200 and html:
                meta = extract_metadata_from_html(html, url)
                meta["Status"] = status
            else:
                meta = _empty_meta(url, status)

            results[index] = meta

            async with lock:
                completed += 1
                pct = start_pct + int((completed / total) * (95 - start_pct))
                safe_progress(
                    progress_callback,
                    pct,
                    f"Scanning pages: {completed}/{total}",
                )

        await asyncio.gather(*[process(i, url) for i, url in enumerate(urls)])
        await context.close()
        await browser.close()

    return [r for r in results if r is not None]


def _empty_meta(url: str, status) -> dict:
    return {
        "URL": url,
        "Meta Title": "",
        "Title Characters": 0,
        "Meta Description": "",
        "Description Characters": 0,
        "H1": "",
        "Canonical": "",
        "Robots": "",
        "Indexability": "Error",
        "Status": status,
    }

# ─────────────────────────────────────────────────────────────────────────────
# LINK DISCOVERY  (for sites without a sitemap)
# ─────────────────────────────────────────────────────────────────────────────

def _discover_links_from_html(html: str, base_url: str) -> list[str]:
    """Extract all same-domain internal links from HTML."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    links: set[str] = set()
    base_clean = base_url.rstrip("/")

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        if href.startswith("#"):
            links.add(base_clean + "/" + href)
            continue
        full = normalize_url(base_url, href)
        if is_valid_url(full) and is_same_domain(base_url, full):
            links.add(full)

    return list(links)


def discover_links(base_url: str, progress_callback: Optional[Callable] = None) -> list[str]:
    """
    Try to discover internal links from the homepage.
    Uses requests first; falls back to Playwright if the page is a CSR shell
    (React apps render links via JS — static fetch gets an empty <body>).
    """
    safe_progress(progress_callback, 10, "Discovering internal links…")

    html, _ = _requests_get(base_url, timeout=15)

    if html and _is_csr_shell(html):
        logger.info("CSR shell on homepage — using Playwright for link discovery.")
        try:
            html, _ = _run_in_thread(
                _crawl_async(
                    [base_url],
                    force_playwright=True,
                    default_title="",
                    max_concurrent=1,
                    progress_callback=None,
                    start_pct=10,
                )
            )
            html = html[0].get("_raw_html", "") if html else ""
        except Exception:
            html = ""

    if not html:
        return [base_url]

    links = _discover_links_from_html(html, base_url)
    logger.info(f"Discovered {len(links)} links from homepage")
    return links or [base_url]

# ─────────────────────────────────────────────────────────────────────────────
# THREAD BRIDGE  (Streamlit runs in a thread; asyncio needs its own loop)
# ─────────────────────────────────────────────────────────────────────────────

def _run_in_thread(coro) -> list[dict]:
    """
    Run an async coroutine from a sync context (e.g. a Streamlit callback).
    Each call gets a fresh event loop to avoid 'loop already running' errors.
    """
    result: list[dict] = []
    exc: list[Exception] = []

    def _target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result.extend(loop.run_until_complete(coro))
        except Exception as e:
            exc.append(e)
        finally:
            loop.close()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()

    if exc:
        raise exc[0]
    return result

# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def crawl_site(
    base_url: str,
    max_pages: int = 500,
    max_concurrent: int = MAX_CONCURRENT,
    progress_callback: Optional[Callable] = None,
) -> list[dict]:
    """
    Public API called by app.py.

    Flow
    ----
    1. Probe homepage to detect CSR React vs static/SSR.
    2. Fetch URL list from sitemap (fast, pure HTTP).
       Fallback: discover links from homepage.
    3. Crawl all pages concurrently via a single Playwright browser.
       Static pages skip the browser entirely (requests fast-path).
    4. Return list of metadata dicts for Streamlit to display.
    """
    logger.info(f"Starting crawl: {base_url}")
    safe_progress(progress_callback, 1, "Initializing scanner…")

    # ── 1. Detect rendering type ─────────────────────────────────────────────
    force_playwright = False
    default_title = ""
    try:
        safe_progress(progress_callback, 2, "Detecting site rendering type…")
        probe_html, _ = _requests_get(base_url.rstrip("/") + "/", timeout=15)
        if _is_csr_shell(probe_html):
            force_playwright = True
            # Capture the shell's default title so we can detect when React
            # has overwritten it with a page-specific title.
            try:
                soup = BeautifulSoup(probe_html, "html.parser")
                default_title = (soup.title.string or "").strip() if soup.title else ""
            except Exception:
                default_title = ""
            logger.info(f"CSR React detected. Shell title: '{default_title}'")
            safe_progress(progress_callback, 3, "React SPA detected — browser rendering enabled…")
        else:
            logger.info("Static/SSR site — fast HTTP path enabled.")
    except Exception as e:
        logger.warning(f"Site detection failed: {e}")

    # ── 2. Get URL list ───────────────────────────────────────────────────────
    safe_progress(progress_callback, 4, "Fetching URL list…")
    urls: list[str] = []

    try:
        urls = fetch_sitemap_urls(base_url, progress_callback)
    except Exception as e:
        logger.warning(f"Sitemap fetch failed: {e}")

    if not urls:
        logger.info("No sitemap — discovering links from homepage.")
        safe_progress(progress_callback, 10, "No sitemap found — discovering links…")
        urls = discover_links(base_url, progress_callback)

    # Dedup + cap + ensure root is included
    urls = list(dict.fromkeys(urls))[:max_pages]
    base_clean = base_url.rstrip("/")
    if base_clean not in urls:
        urls.insert(0, base_clean)

    total = len(urls)
    logger.info(f"Crawling {total} URLs (force_playwright={force_playwright})")
    safe_progress(progress_callback, 15, f"Found {total} pages — starting scan…")

    # ── 3. Crawl ──────────────────────────────────────────────────────────────
    try:
        results = _run_in_thread(
            _crawl_async(
                urls=urls,
                force_playwright=force_playwright,
                default_title=default_title,
                max_concurrent=max_concurrent,
                progress_callback=progress_callback,
                start_pct=15,
            )
        )
    except Exception as e:
        logger.error(f"Crawl failed: {e}")
        results = [_empty_meta(url, f"Error: {e}") for url in urls]

    safe_progress(progress_callback, 100, "Scan complete!")
    logger.info(f"Crawl finished: {len(results)} results")
    return results
