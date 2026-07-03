import asyncio
import subprocess
import sys
import re
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from utils.helpers import normalize_url, is_same_domain, is_valid_url
from utils.logger import logger
from core.parser import extract_metadata_from_html

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

# Install Playwright Chromium on startup (silent, non-blocking)
try:
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False, timeout=60, capture_output=True
    )
except Exception:
    pass


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def safe_progress(callback, pct, text):
    if callback:
        try:
            callback(min(max(int(pct), 0), 100), str(text))
        except Exception:
            pass


def _powershell_get(url, timeout=20):
    """
    Fetch a URL via PowerShell, writing response to a temp file to avoid
    shell escaping issues with HTML content (ampersands, special chars).
    Returns (html, status_code) or ("", 0) on failure.
    """
    import os, tempfile
    tmp_file = os.path.join(tempfile.gettempdir(), f"metadatascanner_fetch_{abs(hash(url))}.txt")
    try:
        ps_script = (
            f"$ProgressPreference='SilentlyContinue'; "
            f"$r = Invoke-WebRequest -Uri '{url}' -UseBasicParsing "
            f"-TimeoutSec {timeout} -UserAgent '{HEADERS['User-Agent']}'; "
            f"$r.Content | Out-File -FilePath '{tmp_file}' -Encoding UTF8; "
            f"Write-Output $r.StatusCode"
        )
        result = subprocess.run(
            ["powershell", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=timeout + 8
        )
        if result.returncode == 0 and os.path.exists(tmp_file):
            with open(tmp_file, "r", encoding="utf-8", errors="replace") as f:
                body = f.read()
            try:
                status = int(result.stdout.strip().splitlines()[-1])
            except Exception:
                status = 200
            return body, status
    except Exception as e:
        logger.debug(f"PowerShell GET failed for {url}: {e}")
    finally:
        try:
            import os as _os
            if _os.path.exists(tmp_file):
                _os.remove(tmp_file)
        except Exception:
            pass
    return "", 0


def _requests_get(url, timeout=20):
    """Plain requests.get. Returns (html, status_code) or ("", 0)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return resp.text, resp.status_code
    except Exception as e:
        logger.debug(f"requests GET failed for {url}: {e}")
    return "", 0


def _is_react_shell(html):
    """
    Return True if the HTML is a bare React CSR shell —
    i.e. <div id="root"> is empty and a JS bundle is present.
    These shells have the same content for every URL.
    """
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    root_div = soup.find("div", id="root") or soup.find("div", id="app")
    root_empty = root_div and len(root_div.get_text(strip=True)) < 20
    has_bundle = bool(
        soup.find("script", src=lambda s: s and (
            "/static/js/main" in s or "bundle.js" in s or "app.js" in s
        ))
    )
    return bool(root_empty and has_bundle)


def _robust_get(url, timeout=20):
    """
    Try every available HTTP method in order:
      1. requests (fastest)
      2. PowerShell Invoke-WebRequest (bypasses Python process firewall rules)
    Returns (html, status_code).
    """
    html, status = _requests_get(url, timeout)
    if html:
        return html, status

    logger.info(f"requests failed for {url} — trying PowerShell...")
    html, status = _powershell_get(url, timeout)
    return html, status


# ─────────────────────────────────────────────
# PLAYWRIGHT (CSR React fallback)
# ─────────────────────────────────────────────

_PLAYWRIGHT_ARGS = [
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--ignore-certificate-errors",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--hide-scrollbars",
    "--mute-audio",
    "--disable-breakpad",
    "--disable-default-apps",
    "--disable-hang-monitor",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--proxy-server=direct://",   # skip system proxy
    "--proxy-bypass-list=*",
]


async def _playwright_fetch_async(url):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=_PLAYWRIGHT_ARGS,
            chromium_sandbox=False
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            ignore_https_errors=True,
            java_script_enabled=True,
            bypass_csp=True,
        )
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="load", timeout=45000)

            # Wait for React/helmet to inject page-specific metadata
            try:
                await page.wait_for_function(
                    """() => {
                        const t = document.title && document.title.trim().length > 0;
                        const d = document.querySelector('meta[name="description"]');
                        const h = document.querySelector('h1');
                        const body = document.body && document.body.innerText.trim().length > 200;
                        return t || (d && d.content && d.content.trim().length > 0) || !!h || body;
                    }""",
                    timeout=10000
                )
                await page.wait_for_timeout(1500)   # extra tick for helmet tag swap
            except Exception:
                await page.wait_for_timeout(3000)   # graceful fallback

            status = response.status if response else 200
            html = await page.content()
            return html, status
        except Exception as e:
            logger.error(f"Playwright navigation failed for {url}: {e}")
            raise
        finally:
            try:
                await page.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


def _playwright_fetch(url):
    """Sync wrapper around the async Playwright fetch."""
    return asyncio.run(_playwright_fetch_async(url))


# ─────────────────────────────────────────────
# SITEMAP
# ─────────────────────────────────────────────

def fetch_sitemap_urls(base_url, progress_callback=None):
    """
    Fetch sitemap.xml via _robust_get (requests → PowerShell).
    Checks robots.txt first, then common locations.
    Handles sitemap index files.
    """
    safe_progress(progress_callback, 5, "Checking for sitemap.xml...")

    candidates = []

    # Check robots.txt for Sitemap: directive
    robots_html, _ = _robust_get(base_url.rstrip("/") + "/robots.txt", timeout=10)
    for line in robots_html.splitlines():
        if line.lower().startswith("sitemap:"):
            candidates.append(line.split(":", 1)[1].strip())

    candidates += [
        base_url.rstrip("/") + "/sitemap.xml",
        base_url.rstrip("/") + "/sitemap_index.xml",
        base_url.rstrip("/") + "/sitemap/sitemap.xml",
    ]

    def _parse_sitemap(content):
        content = re.sub(r"<\?xml[^?]*\?>", "", content, count=1).strip()
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return []
        urls = []
        tag = root.tag.lower()
        if "sitemapindex" in tag:
            for child in root:
                for sub in child:
                    if sub.tag.endswith("loc") and sub.text:
                        child_html, _ = _robust_get(sub.text.strip(), timeout=10)
                        urls.extend(_parse_sitemap(child_html))
        elif "urlset" in tag:
            for child in root:
                for sub in child:
                    if sub.tag.endswith("loc") and sub.text:
                        urls.append(sub.text.strip())
        return urls

    for sm_url in candidates:
        content, status = _robust_get(sm_url, timeout=10)
        if status == 200 and content:
            urls = _parse_sitemap(content)
            if urls:
                logger.info(f"Sitemap at {sm_url}: {len(urls)} URLs")
                return urls

    return []


# ─────────────────────────────────────────────
# PAGE FETCHER (with CSR React detection)
# ─────────────────────────────────────────────

def fetch_page_html(url, force_playwright=False):
    """
    Fetch a page's rendered HTML.

    - Normal sites (SSR/static): _robust_get is sufficient.
    - CSR React sites: Playwright required so JS executes and
      react-helmet injects page-specific meta tags.

    force_playwright=True skips the HTTP fetchers and goes
    straight to Playwright (set automatically for detected CSR sites).
    """
    if not force_playwright:
        html, status = _robust_get(url)
        if html and not _is_react_shell(html):
            return html, status
        if html:
            logger.info(f"React shell detected at {url} — switching to Playwright...")

    # Playwright path
    try:
        logger.info(f"Playwright rendering: {url}")
        return _playwright_fetch(url)
    except Exception as e:
        logger.error(f"Playwright failed for {url}: {e}")
        return "", f"Error: {e}"


# ─────────────────────────────────────────────
# LINK DISCOVERY
# ─────────────────────────────────────────────

def discover_links(base_url, progress_callback=None):
    safe_progress(progress_callback, 10, "Loading homepage to discover internal links...")
    try:
        html, _ = _robust_get(base_url)
        if not html:
            return [base_url]

        safe_progress(progress_callback, 13, "Parsing internal links...")
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        links = set()
        base_clean = base_url.rstrip("/")

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("javascript:") or href.startswith("mailto:") or href.startswith("tel:"):
                continue
            if href.startswith("#"):
                links.add(base_clean + "/" + href)
                continue
            full_url = normalize_url(base_url, href)
            if is_valid_url(full_url) and is_same_domain(base_url, full_url):
                links.add(full_url)

        logger.info(f"Discovered {len(links)} links from homepage")
        return list(links)
    except Exception as e:
        logger.error(f"Link discovery failed: {e}")
        return [base_url]


# ─────────────────────────────────────────────
# CRAWL
# ─────────────────────────────────────────────

def crawl_pages(urls, progress_callback=None, force_playwright=False):
    results = []
    total = len(urls)
    for i, url in enumerate(urls):
        pct = int(15 + ((i + 1) / total) * 80)
        safe_progress(progress_callback, pct, f"Scanning pages: {i+1}/{total} ({pct}%)")
        try:
            html, status = fetch_page_html(url, force_playwright=force_playwright)
            if isinstance(status, int) and status == 200 and html:
                meta = extract_metadata_from_html(html, url)
                meta["Status"] = status
            else:
                meta = {
                    "URL": url, "Meta Title": "", "Title Characters": 0,
                    "Meta Description": "", "Description Characters": 0,
                    "H1": "", "Canonical": "", "Robots": "",
                    "Indexability": "Error", "Status": status
                }
        except Exception as e:
            meta = {
                "URL": url, "Meta Title": "", "Title Characters": 0,
                "Meta Description": "", "Description Characters": 0,
                "H1": "", "Canonical": "", "Robots": "",
                "Indexability": "Error", "Status": f"Error: {e}"
            }
        results.append(meta)
    return results


# ─────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────

def crawl_site(base_url, max_pages=1000, max_concurrent=5, progress_callback=None):
    logger.info(f"Starting crawl for {base_url}")
    safe_progress(progress_callback, 1, "Initializing scanner...")

    # Detect CSR React: probe homepage with a plain HTTP request
    force_playwright = False
    try:
        safe_progress(progress_callback, 2, "Detecting site rendering type...")
        probe_html, _ = _robust_get(base_url.rstrip("/") + "/", timeout=15)
        if _is_react_shell(probe_html):
            force_playwright = True
            logger.info("CSR React site detected — Playwright will be used for all pages.")
            safe_progress(progress_callback, 3, "React SPA detected — using browser rendering for accurate metadata...")
        else:
            logger.info("Static/SSR site detected — using fast HTTP fetching.")
    except Exception as e:
        logger.warning(f"Site detection failed: {e}")

    # 1. Sitemap path
    try:
        sitemap_urls = fetch_sitemap_urls(base_url, progress_callback)
        if sitemap_urls:
            urls = list(dict.fromkeys(sitemap_urls))[:max_pages]
            logger.info(f"Crawling {len(urls)} URLs from sitemap.")
            safe_progress(progress_callback, 15, f"Found {len(urls)} URLs in sitemap. Starting crawl...")
            results = crawl_pages(urls, progress_callback, force_playwright=force_playwright)
            safe_progress(progress_callback, 100, "Scan Complete!")
            return results
    except Exception as e:
        logger.warning(f"Sitemap path failed: {e}")

    # 2. Link discovery fallback
    logger.info("No sitemap — discovering links from homepage.")
    discovered = discover_links(base_url, progress_callback)
    urls = list(dict.fromkeys(discovered))[:max_pages]
    base_clean = base_url.rstrip("/")
    if base_clean not in urls:
        urls.insert(0, base_clean)

    logger.info(f"Crawling {len(urls)} discovered URLs.")
    safe_progress(progress_callback, 15, f"Discovered {len(urls)} pages. Starting crawl...")
    results = crawl_pages(urls, progress_callback, force_playwright=force_playwright)
    safe_progress(progress_callback, 100, "Scan Complete!")
    return results
