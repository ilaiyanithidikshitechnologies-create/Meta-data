import asyncio
import xml.etree.ElementTree as ET
from playwright.async_api import async_playwright
from utils.helpers import normalize_url, is_same_domain, is_valid_url
from utils.logger import logger
from core.parser import extract_metadata_from_html

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MetaTextBot/2.0)"}

# ---------- SITEMAP FETCH (via Playwright) ----------
async def fetch_sitemap_via_playwright(base_url):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS['User-Agent'])
            page = await context.new_page()
            sitemap_url = base_url.rstrip('/') + "/sitemap.xml"
            response = await page.goto(sitemap_url, wait_until="domcontentloaded", timeout=15000)
            if response and response.status == 200:
                content = await page.content()
                await browser.close()
                return content
            await browser.close()
            return None
    except Exception as e:
        logger.warning(f"Sitemap fetch failed: {e}")
        return None

def parse_sitemap_content(html_content):
    try:
        root = ET.fromstring(html_content)
        urls = []
        for child in root:
            if child.tag.endswith('url'):
                for sub in child:
                    if sub.tag.endswith('loc'):
                        urls.append(sub.text.strip())
        return urls
    except Exception as e:
        logger.warning(f"Sitemap parse failed: {e}")
        return []

# ---------- LINK DISCOVERY (via Playwright) ----------
async def discover_links_via_playwright(base_url):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS['User-Agent'])
            page = await context.new_page()
            # Use domcontentloaded for React/SPA compatibility instead of networkidle
            await page.goto(base_url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(2000)  # Wait for React DOM hydration
            html = await page.content()
            await browser.close()

            from bs4 import BeautifulSoup
            try:
                soup = BeautifulSoup(html, "lxml")
            except Exception:
                soup = BeautifulSoup(html, "html.parser")
            links = set()
            for a in soup.find_all("a", href=True):
                href = a['href'].strip()
                if not href or href.startswith('#') or href.startswith('javascript:'):
                    continue
                full_url = normalize_url(base_url, href)
                if is_valid_url(full_url) and is_same_domain(base_url, full_url):
                    links.add(full_url)
            return list(links)
    except Exception as e:
        logger.error(f"Link discovery failed via Playwright: {e}. Trying requests fallback...")
        try:
            import requests
            resp = requests.get(base_url, headers=HEADERS, timeout=15)
            from bs4 import BeautifulSoup
            try:
                soup = BeautifulSoup(resp.text, "lxml")
            except Exception:
                soup = BeautifulSoup(resp.text, "html.parser")
            links = set()
            for a in soup.find_all("a", href=True):
                href = a['href'].strip()
                if not href or href.startswith('#') or href.startswith('javascript:'):
                    continue
                full_url = normalize_url(base_url, href)
                if is_valid_url(full_url) and is_same_domain(base_url, full_url):
                    links.add(full_url)
            return list(links) if links else [base_url]
        except Exception:
            return [base_url]  # fallback

# ---------- PAGE FETCH (via Playwright) ----------
async def fetch_page_with_playwright(url, semaphore, context):
    async with semaphore:
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(2000)  # Wait for React DOM hydration
            status = response.status if response else 200
            html = await page.content()
            return url, html, status
        except Exception as e:
            logger.error(f"Playwright error for {url}: {e}. Trying requests fallback...")
            try:
                import requests
                resp = requests.get(url, headers=HEADERS, timeout=15)
                return url, resp.text, resp.status_code
            except Exception as ex:
                return url, "", f"Error: {str(e)}"
        finally:
            await page.close()

async def crawl_pages_async(urls, max_concurrent=10):
    results = []
    semaphore = asyncio.Semaphore(max_concurrent)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        context = await browser.new_context(user_agent=HEADERS['User-Agent'])
        tasks = [fetch_page_with_playwright(url, semaphore, context) for url in urls]
        fetched_data = await asyncio.gather(*tasks)
        await browser.close()

    for url, html, status in fetched_data:
        if status == 200 and html:
            meta = extract_metadata_from_html(html, url)
            meta["Status"] = status
            results.append(meta)
        else:
            results.append({
                "URL": url,
                "Meta Title": "",
                "Title Characters": 0,
                "Meta Description": "",
                "Description Characters": 0,
                "H1": "",
                "Canonical": "",
                "Robots": "",
                "Indexability": "Error",
                "Status": status
            })
    return results

# ---------- MAIN ORCHESTRATOR ----------
def crawl_site(base_url, max_pages=1000, max_concurrent=10):
    logger.info(f"Starting full Playwright crawl for {base_url}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 1. Try Sitemap
    try:
        sitemap_html = loop.run_until_complete(fetch_sitemap_via_playwright(base_url))
        if sitemap_html:
            urls = parse_sitemap_content(sitemap_html)
            if urls:
                logger.info(f"Found {len(urls)} URLs in sitemap.")
                urls = list(dict.fromkeys(urls))[:max_pages]
                results = loop.run_until_complete(crawl_pages_async(urls, max_concurrent))
                loop.close()
                return results
    except Exception as e:
        logger.warning(f"Sitemap flow failed: {e}")

    # 2. Fallback: Discover internal links
    logger.info("No sitemap found. Discovering links from homepage via Playwright.")
    discovered = loop.run_until_complete(discover_links_via_playwright(base_url))
    urls = list(dict.fromkeys(discovered))[:max_pages]
    if base_url.rstrip('/') not in urls:
        urls.insert(0, base_url.rstrip('/'))

    logger.info(f"Discovered {len(urls)} internal links.")
    results = loop.run_until_complete(crawl_pages_async(urls, max_concurrent))
    loop.close()
    return results