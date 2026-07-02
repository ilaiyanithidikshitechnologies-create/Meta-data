from bs4 import BeautifulSoup

def extract_metadata_from_html(html_content, url):
    """Extract SEO metadata (Title, Description, H1, Canonical, Robots, Indexability) from HTML."""
    if not html_content:
        return {
            "URL": url,
            "Meta Title": "",
            "Title Characters": 0,
            "Meta Description": "",
            "Description Characters": 0,
            "H1": "",
            "Canonical": "",
            "Robots": "",
            "Indexability": "Error"
        }

    try:
        soup = BeautifulSoup(html_content, "lxml")
    except Exception:
        soup = BeautifulSoup(html_content, "html.parser")

    # 1. Meta Title
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    else:
        og_title = soup.find("meta", property="og:title")
        if og_title:
            title = og_title.get("content", "").strip()

    # 2. Meta Description
    desc = ""
    desc_tag = soup.find("meta", attrs={"name": lambda x: x and x.lower() == "description"})
    if desc_tag:
        desc = desc_tag.get("content", "").strip()
    else:
        og_desc = soup.find("meta", property="og:description")
        if og_desc:
            desc = og_desc.get("content", "").strip()

    # 3. H1
    h1_tags = [h1.get_text(strip=True) for h1 in soup.find_all("h1") if h1.get_text(strip=True)]
    h1_text = " | ".join(h1_tags) if h1_tags else ""

    # 4. Canonical
    canonical = ""
    can_tag = soup.find("link", rel=lambda x: x and ("canonical" in [r.lower() for r in x] if isinstance(x, list) else x.lower() == "canonical"))
    if not can_tag:
        can_tag = soup.find("link", rel="canonical")
    if can_tag:
        canonical = can_tag.get("href", "").strip()

    # 5. Robots
    robots = ""
    robots_tag = soup.find("meta", attrs={"name": lambda x: x and x.lower() == "robots"})
    if robots_tag:
        robots = robots_tag.get("content", "").strip()

    # 6. Indexability
    indexability = "Indexable"
    if "noindex" in robots.lower():
        indexability = "Non-Indexable"

    return {
        "URL": url,
        "Meta Title": title,
        "Title Characters": len(title),
        "Meta Description": desc,
        "Description Characters": len(desc),
        "H1": h1_text,
        "Canonical": canonical,
        "Robots": robots,
        "Indexability": indexability
    }
