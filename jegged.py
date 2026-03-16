#!/usr/bin/env python3
"""
jegged.com → EPUB scraper
Usage: python jegged.py --epub "https://www.jegged.com/Games/Some-Game/" [options]

NOTE: If selectors stop matching, open a guide page in DevTools and verify:
  TOC_SELECTOR     – the sidebar element containing all chapter <a> links
  CONTENT_SELECTOR – the main article/content div for each page
"""

import os, re, time, requests, io, hashlib
from collections import deque
from urllib.parse import urljoin, urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup
from ebooklib import epub
from PIL import Image, ImageEnhance
import zipfile

session = requests.Session()
# Increase connection pool size to match parallel worker count
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
session.mount("https://", adapter)
session.mount("http://",  adapter)

THUMB_SIZES = {
    "160p": (160, 213),
    "320p": (320, 427),
    "480p": (480, 640),
}

RESOLUTIONS = {
    "kindle": (758,  1024),   # Kindle basic / Paperwhite portrait width
    "480p":   (480,   640),
    "720p":   (720,   960),
    "1080p":  (1080, 1440),
    "2k":     (1264, 1680),
}

BLOCKED_DOMAINS = ["zdbb.net", "doubleclick.net", "googlesyndication.com"]

# ── Selectors to verify against live jegged.com pages ────────────────────────
# The element wrapping the guide body text on each page.
CONTENT_SELECTORS = [
    ".guide-content",
    ".entry-content",
    "article .post-content",
    "article",
    "#primary article",
    ".walkthrough",
    ".page-content",
    "main.main-game",
    "#main-content",
    "main",
]

# Jegged domain root – used to filter internal links
JEGGED_DOMAIN = "jegged.com"

# URL path segments to skip during crawling (case-insensitive)
SKIP_PATHS = ["videos"]
# ─────────────────────────────────────────────────────────────────────────────


def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')

def normalize_url(url):
    p = urlparse(url)
    return p.scheme + "://" + p.netloc + p.path.rstrip("/").lower()

def minify_xml(data: bytes) -> bytes:
    """Strip redundant whitespace from XML/HTML."""
    text = data.decode("utf-8", errors="ignore")
    text = re.sub(r">\s+<", "><", text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines).encode("utf-8")



def compress_epub(path):
    tmp = path + ".tmp"
    MINIFY_EXTS = {".xhtml", ".html", ".htm", ".ncx", ".opf", ".xml"}
    with zipfile.ZipFile(path, 'r') as zin:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                ext  = os.path.splitext(item.filename)[1].lower()
                if ext in MINIFY_EXTS:
                    try:
                        data = minify_xml(data)
                    except Exception:
                        pass  # leave as-is if minification fails
                zout.writestr(item, data)
    os.replace(tmp, path)
    print(f"Compressed: {path}")

def download_image(url, folder, max_size=(720, 960), quality=75, grayscale=False, contrast=1.0,
                   thumb_size=(320, 427)):
    """
    Downloads an image and saves two versions:
      folder/name.jpg          — thumbnail (max_size, for inline display)
      folder/full/name.jpg     — full-size (original quality, for viewer)
    Returns name (thumbnail filename) or None.
    """
    if url.startswith("data:"):
        return None
    if any(domain in url for domain in BLOCKED_DOMAINS):
        return None
    try:
        name = os.path.basename(urlparse(url).path)
        if '.' not in name:
            name += '.jpg'
        # Always normalise to .jpg
        if not name.lower().endswith(('.jpg', '.jpeg', '.svg')):
            name = os.path.splitext(name)[0] + '.jpg'
        path = os.path.join(folder, name)
        full_folder = os.path.join(folder, "full")
        full_path   = os.path.join(full_folder, name)
        os.makedirs(full_folder, exist_ok=True)
        if not os.path.exists(path):
            headers = {"Accept": "image/jpeg,image/png,image/*", "User-Agent": "Mozilla/5.0"}
            r = session.get(url, timeout=20, headers=headers)
            if len(r.content) < 10_000:
                return None  # skip small images (icons, spacers, etc.)
            if name.lower().endswith('.svg'):
                with open(path, "wb") as f:
                    f.write(r.content)
                with open(full_path, "wb") as f:
                    f.write(r.content)
            else:
                try:
                    img = Image.open(io.BytesIO(r.content))
                    img = img.convert("L" if grayscale else "RGB")
                    if contrast != 1.0:
                        img = ImageEnhance.Contrast(img).enhance(contrast)

                    save_kwargs = dict(
                        format='JPEG', optimize=True, progressive=True, quality=quality,
                    )
                    if not grayscale:
                        save_kwargs['subsampling'] = 2

                    orig_w, orig_h = img.size

                    # Save full-size version (capped at max_size, no thumb crop)
                    fw, fh = max_size
                    fnh = int(orig_h * fw / orig_w)
                    if fnh > fh:
                        fnw = int(orig_w * fh / orig_h)
                        full_img = img.resize((fnw, fh), Image.LANCZOS)
                    else:
                        full_img = img.resize((fw, fnh), Image.LANCZOS)
                    full_img.save(full_path, **save_kwargs)

                    # Save thumbnail version
                    tw, th = thumb_size
                    tnh = int(orig_h * tw / orig_w)
                    if tnh > th:
                        tnw = int(orig_w * th / orig_h)
                        thumb_img = img.resize((tnw, th), Image.LANCZOS)
                    else:
                        thumb_img = img.resize((tw, tnh), Image.LANCZOS)
                    thumb_img.save(path, **save_kwargs)

                except Exception as img_err:
                    print(f"  PIL failed for {url}: {img_err} — writing raw bytes")
                    if grayscale:
                        return None
                    with open(path, "wb") as f:
                        f.write(r.content)
                    with open(full_path, "wb") as f:
                        f.write(r.content)
        return name
    except Exception as e:
        print(f"  Failed to download image {url}: {e}")
        return None

def download_images_parallel(image_urls, img_folder, max_size=(720, 960), quality=75, grayscale=False, contrast=1.0, thumb_size=(320, 427)):
    downloaded = []
    with ThreadPoolExecutor(max_workers=10) as executor:  # image downloads
        future_to_url = {
            executor.submit(download_image, url, img_folder, max_size, quality, grayscale, contrast, thumb_size): url
            for url in image_urls
        }
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                name = future.result()
                if name:
                    downloaded.append((url, name))
            except Exception:
                pass
    return downloaded

def get_real_image_url(img):
    for attr in ["data-src", "data-lazy-src", "data-original"]:
        val = img.get(attr)
        if val and not val.startswith("data:"):
            return val
    src = img.get("src")
    if src and not src.startswith("data:"):
        return src
    srcset = img.get("srcset")
    if srcset:
        parts = [p.strip() for p in srcset.split(",")]
        if parts:
            return parts[-1].split()[0]
    return None

# ── Jegged-specific helpers ───────────────────────────────────────────────────

def fetch_soup(url):
    """Fetch a page directly with requests (no JS). Used for TOC discovery."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    r = session.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    # Force UTF-8 — requests sometimes guesses Latin-1 from the HTTP headers
    # which mangles curly quotes and other Unicode characters
    r.encoding = "utf-8"
    return BeautifulSoup(r.text, "lxml")

def load_page(driver, url):
    """Navigate Selenium and wait until the new page is actually rendered."""
    target = url.split("?")[0].rstrip("/").lower()
    driver.get(url)
    try:
        WebDriverWait(driver, 30).until(
            lambda d: (
                d.execute_script("return document.readyState") == "complete"
                and target in d.current_url.split("?")[0].rstrip("/").lower()
            )
        )
    except TimeoutException:
        pass
    time.sleep(0.4)

def is_skipped(path):
    """Return True if this path should be excluded."""
    path_lower = path.lower()
    return any(
        f"/{s.lower()}/" in path_lower or path_lower.endswith(f"/{s.lower()}")
        for s in SKIP_PATHS
    )


def best_link_title(a, fallback_url):
    """
    Get the best human-readable title for a link.
    Avoids generic image alt text like 'Map' by checking if the text
    came from an <img> alt attribute, and falling back to the URL slug.
    """
    # If the only content is an image, use the URL slug not the alt text
    texts = [t.strip() for t in a.strings if t.strip()]
    imgs  = a.find_all("img")
    if imgs and all(t in [img.get("alt","") for img in imgs] for t in texts):
        # All text came from img alt attributes - use URL slug instead
        texts = []

    title = " ".join(texts).strip()
    GENERIC_LABELS = {"read more", "map", "more", "details", "view", ""}
    if title and title.lower() not in GENERIC_LABELS:
        return title

    # Derive from URL slug
    seg = urlparse(fallback_url).path.rstrip("/").split("/")[-1]
    return re.sub(r"^[0-9]+-", "", seg).replace("-", " ").replace(".html", "").title()


def get_nav_sections(soup, base_url, game_path):
    """
    Walk the nav <ul>/<li> tree properly so that href="#" dropdown parents
    (like "Equipment and Items" and "Walkthrough") are preserved as grouping
    entries, with their children nested underneath them at depth+1.

    Returns list of (title, url_or_None, depth).
    url=None means it's a grouping label with no scrapeable content page.
    """
    seen = {normalize_url(base_url)}
    results = []

    def process_item(a, depth):
        href  = a.get("href", "").strip()
        title = a.get_text(strip=True)
        if not href:
            return None
        if href == "#":
            # Grouping label — no real URL
            return (title, None, depth)
        full_url = urljoin(base_url, href)
        parsed   = urlparse(full_url)
        if JEGGED_DOMAIN not in parsed.netloc:
            return None
        if not parsed.path.startswith(game_path + "/"):
            return None
        if is_skipped(parsed.path):
            return None
        norm = normalize_url(full_url)
        if norm in seen:
            return None
        seen.add(norm)
        return (title, norm, depth)

    def walk_ul(ul, depth):
        for li in ul.find_all("li", recursive=False):
            a = li.find("a", recursive=False)
            if not a:
                # Some <li> only have text — skip
                continue
            entry = process_item(a, depth)
            if entry:
                results.append(entry)
            # Recurse into any nested <ul> (dropdown children)
            sub_ul = li.find("ul", recursive=False)
            if sub_ul:
                walk_ul(sub_ul, depth + 1)

    # Pick the <ul> that contains the most game-path links — that's the main nav
    best_ul    = None
    best_count = 0
    for ul in soup.find_all("ul"):
        count = sum(
            1 for a in ul.find_all("a", href=True)
            if game_path.lower() in a["href"].lower()
        )
        if count > best_count:
            best_count = count
            best_ul    = ul

    if best_ul:
        walk_ul(best_ul, 0)

    return results


def get_subpages_in_order(soup, section_url, game_path, seen_norms):
    """
    Extract sub-pages from a section index page in DOM order.
    Returns list of (title, url, in_toc) tuples.

    Tab handling:
    - DUPLICATE tabs (Standard/Alphabetical, Full/Short): same links appear in
      multiple panes. Skip inactive panes to avoid duplicates. in_toc=True.
    - PARTITIONED tabs (Hunts 1-20 / 21-45): each pane has different links.
      Include all panes. in_toc=False for ALL entries (they are spine-only,
      reachable via the section index page but not individually listed in TOC).
    - No tabs: normal links. in_toc=True.
    """
    section_path = urlparse(section_url).path.rstrip("/").lower() + "/"
    content_el   = find_content(soup)
    if not content_el:
        return []

    # Tab strategy: include links from ALL tab panes for discovery.
    # seen_norms deduplicates links that appear in multiple panes.
    # All entries found this way are marked in_toc=False — they are
    # reachable via the section index but don't need individual TOC entries.
    all_panes      = content_el.find_all("div", class_="tab-pane")
    inactive_panes = set()   # never skip any panes now
    partitioned_tabs = bool(all_panes)  # any tabs = mark children spine-only

    results = []
    for a in content_el.find_all("a", href=True):
        if any(id(p) in inactive_panes for p in a.parents):
            continue
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        if any(href.lower().endswith(ext) for ext in ('.webp','.png','.jpg','.jpeg','.gif','.svg','.css','.js')):
            continue
        full_url = urljoin(section_url, href)
        parsed   = urlparse(full_url)
        if JEGGED_DOMAIN not in parsed.netloc:
            continue
        if not parsed.path.startswith(game_path + "/"):
            continue
        if is_skipped(parsed.path):
            continue
        if not parsed.path.lower().startswith(section_path):
            continue
        norm = normalize_url(full_url)
        if norm in seen_norms:
            continue
        if norm == normalize_url(section_url):
            continue
        seen_norms.add(norm)
        title  = best_link_title(a, full_url)
        in_toc = not partitioned_tabs
        results.append((title, norm, in_toc))

    return results


def expand_recursive(ordered_pages, seen_norms, game_path, workers,
                     url, soup, depth, in_toc=True, max_depth=4):
    """
    Recursively expand a page's direct children in DOM order.
    in_toc propagates: if a parent is spine-only, all its children are too.
    """
    subpages = get_subpages_in_order(soup, url, game_path, seen_norms)
    if not subpages:
        return

    # Fetch all children in parallel
    def fetch_child(args):
        t, u, it = args
        try:
            return (t, u, it, fetch_soup(u))
        except Exception as e:
            print(f"    fetch failed for {t}: {e}")
            return (t, u, it, None)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_child, s): s for s in subpages}
        child_soups = {}
        for future in as_completed(futures):
            t, u, it, s = future.result()
            child_soups[u] = (t, it, s)

    for sub_title, sub_url, sub_in_toc in subpages:
        t, it, child_soup = child_soups.get(sub_url, (sub_title, sub_in_toc, None))
        # Parent in_toc=False propagates to all children
        child_in_toc = in_toc and sub_in_toc
        ordered_pages.append((sub_title, sub_url, depth, child_in_toc))
        marker = "+" if child_in_toc else "-"
        print(f"{'  ' * depth}{marker} {sub_title}")
        if child_soup and depth < max_depth:
            expand_recursive(ordered_pages, seen_norms, game_path, workers,
                             sub_url, child_soup, depth + 1,
                             in_toc=child_in_toc)


def collect_toc_links(driver, base_url, workers=8):
    """
    Ordered two-pass collection:
    Pass 1 - Nav tree: walk <ul>/<li> nav tree in DOM order.
    Pass 2 - Expand: for each section, recursively extract direct children.
    Returns flat list of (title, url_or_None, depth, in_toc).
    """
    parsed_base = urlparse(base_url)
    game_path   = parsed_base.path.rstrip("/")

    print("  Fetching base page...")
    try:
        seed_soup = fetch_soup(base_url)
    except Exception as e:
        print(f"  requests failed ({e}), using driver")
        seed_soup = BeautifulSoup(driver.page_source, "lxml")

    nav_sections = get_nav_sections(seed_soup, base_url, game_path)
    print(f"  Nav entries: {len(nav_sections)}")
    if not nav_sections:
        all_a = seed_soup.find_all("a", href=True)
        print(f"  WARNING: 0 nav entries (total <a> tags: {len(all_a)})")
        return []

    ordered_pages = []
    seen_norms    = {normalize_url(base_url)}
    seen_norms.update(n for _, n, _ in nav_sections if n)

    # Fetch all section index pages in parallel
    real_sections = [(t, u, d) for t, u, d in nav_sections if u is not None]
    print(f"  Fetching {len(real_sections)} section pages in parallel...")

    def fetch_section(args):
        title, url, depth = args
        try:
            return (title, url, depth, fetch_soup(url))
        except Exception as e:
            print(f"    fetch failed for {title}: {e}")
            return (title, url, depth, None)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_section, s): s for s in real_sections}
        soup_map = {}
        for future in as_completed(futures):
            title, url, depth, soup = future.result()
            soup_map[url] = soup

    # Reassemble in original nav order
    for section_title, section_url, nav_depth in nav_sections:
        if section_url is None:
            ordered_pages.append((section_title, None, nav_depth, True))
            print(f"  Group: {section_title}")
            continue

        section_soup = soup_map.get(section_url)
        if section_soup is None:
            ordered_pages.append((section_title, section_url, nav_depth, True))
            continue

        ordered_pages.append((section_title, section_url, nav_depth, True))
        print(f"  Section: {section_title}")
        expand_recursive(ordered_pages, seen_norms, game_path, workers,
                         section_url, section_soup, nav_depth + 1)

    print(f"  Total entries: {len(ordered_pages)}")
    return ordered_pages


def find_content(soup):
    """Return the best content element from a parsed jegged page."""
    for selector in CONTENT_SELECTORS:
        el = soup.select_one(selector)
        if el and len(el.get_text(strip=True)) > 100:
            return el
    # Absolute fallback
    return soup.find("body")

def process_page(driver, url, img_folder, link_map, max_size=(720, 960),
                 quality=75, no_images=False, grayscale=False, contrast=1.0, thumb_size=(320, 427)):
    # Try fast requests fetch first; fall back to Selenium if it fails
    try:
        raw_soup = fetch_soup(url)
    except Exception:
        load_page(driver, url)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.3)
        raw_soup = BeautifulSoup(driver.page_source, "lxml")
    raw_text = raw_soup.get_text()

    # Jegged stub / placeholder detection
    stub_phrases = [
        "coming soon",
        "this page is under construction",
        "no content yet",
        "page not found",
    ]
    if any(p in raw_text.lower() for p in stub_phrases):
        print(f"  Stub/empty page, skipping: {url}")
        return None, set(), None

    content_el = find_content(raw_soup)
    if not content_el or len(content_el.get_text(strip=True)) < 50:
        print(f"  No usable content found, skipping: {url}")
        return None, set(), None

    # Clone to a fresh soup so we can modify without affecting raw_soup
    content_soup = BeautifulSoup(str(content_el), "lxml")
    content_el = content_soup.find("body") or content_soup

    # Remove inactive Bootstrap tab panes and nav buttons.
    # Collect ALL candidates into plain lists BEFORE decomposing anything —
    # decompose() modifies the tree in place which corrupts subsequent
    # find_all results and causes NoneType errors on the next iteration.
    all_panes = content_el.find_all("div", class_="tab-pane")
    has_active = any("active" in p.get("class", []) for p in all_panes if p)
    # Only strip inactive panes when JS has set an active class (live browser).
    # If none are active we fetched via requests (no JS) — keep all panes.
    inactive_panes = []
    if has_active:
        inactive_panes = [
            p for p in all_panes
            if p and "active" not in p.get("class", [])
        ]
    nav_tab_lists = list(content_el.find_all("ul", class_="nav-tabs"))
    for p in inactive_panes:
        p.decompose()
    for t in nav_tab_lists:
        t.decompose()

    # Unwrap lightbox links — jegged wraps images in <a href="full-size.webp">
    # which breaks EPUB readers (clicking image navigates to a broken page).
    # Keep the <img> but remove the <a> wrapper.
    ASSET_EXTS = ('.webp', '.png', '.jpg', '.jpeg', '.gif', '.svg')
    for a in list(content_el.find_all("a", href=True)):
        href = a.get("href", "")
        if any(href.lower().endswith(ext) for ext in ASSET_EXTS):
            a.unwrap()

    # Strip junk
    for tag in content_el(["script", "style", "noscript", "iframe", "svg", "video", "source"]):
        tag.decompose()
    for selector in [
        ".ad", ".ads", ".advert", ".advertisement", ".ad-container",
        ".ad-wrapper", ".ad-block", "[id^='ad-']", "[id^='ads-']",
        ".promo", ".sponsor", ".sponsored",
        "[class*='video']", "[class*='paging']",
        ".social-share", ".share-buttons", ".comments", ".comment-section",
        "nav", "footer", ".sidebar", "#sidebar", ".widget",
    ]:
        for el in content_el.select(selector):
            el.decompose()

    # Handle images
    img_map = {}
    if not no_images:
        for i, img in enumerate(content_el.find_all("img")):
            src = get_real_image_url(img)
            if not src:
                img.decompose()
                continue
            placeholder_id = f"IMG_PLACEHOLDER_{i}"
            img_map[placeholder_id] = urljoin(url, src)
            placeholder = content_soup.new_tag("p")
            placeholder["id"] = placeholder_id
            placeholder.string = placeholder_id
            img.replace_with(placeholder)
    else:
        for img in content_el.find_all("img"):
            img.decompose()

    if not no_images:
        downloaded = download_images_parallel(list(img_map.values()), img_folder, max_size, quality, grayscale, contrast, thumb_size)
        url_to_name = {u: n for u, n in downloaded}
        for p in content_el.find_all("p"):
            pid = p.get("id", "")
            if pid in img_map:
                orig_url = img_map[pid]
                if orig_url in url_to_name:
                    img_src = img_folder + "/" + url_to_name[orig_url]
                    new_img = content_soup.new_tag("img", src=img_src)
                    # img_viewer_map is populated after scraping, so store
                    # a placeholder href using the image filename — fix_image_links
                    # will resolve it to the viewer xhtml after the map is built
                    new_a = content_soup.new_tag("a", **{"href": "__IMG_VIEWER__" + url_to_name[orig_url], "class": "img-link"})
                    new_a.append(new_img)
                    p.replace_with(new_a)
                else:
                    p.decompose()

    # Fix internal links and collect extra pages
    # Derive the game path from the current URL so we only follow same-guide links
    _parsed_url = urlparse(url)
    _path_parts = _parsed_url.path.strip("/").split("/")
    # Game path is /Games/<game-name> — first two path segments
    _game_path  = "/" + "/".join(_parsed_url.path.strip("/").split("/")[:2])

    extra_pages = set()
    for a in content_el.find_all("a"):
        href = a.get("href")
        if not href:
            continue
        # Skip lightbox / direct image/asset links
        if any(href.lower().endswith(ext) for ext in ('.webp','.png','.jpg','.jpeg','.gif','.svg','.css','.js')):
            continue
        full_href = urljoin(url, href)
        if JEGGED_DOMAIN not in full_href:
            continue
        # Only follow links within this game's path
        _fhref_path = urlparse(full_href).path
        if not _fhref_path.lower().startswith(_game_path.lower() + "/"):
            continue
        normalized = normalize_url(full_href)
        if normalized in link_map:
            a["href"] = link_map[normalized] + ".xhtml"
        else:
            extra_pages.add(normalized)

    body = content_el if content_el.name != "body" else content_el
    content_str = body.decode_contents() if hasattr(body, "decode_contents") else str(body)
    if len(content_str.strip()) < 50:
        print(f"  Warning: very little content extracted from {url}")

    text_fp = hashlib.md5(body.get_text().strip().encode()).hexdigest()
    return content_str, extra_pages, text_fp

def fix_chapter_links(chapter, link_map):
    """Re-run link fixing on an already-scraped chapter now that link_map is complete."""
    soup = BeautifulSoup(chapter.content, "lxml")
    body = soup.find("body")
    if not body:
        return
    changed = False
    for a in body.find_all("a"):
        href = a.get("href")
        if not href or href.endswith(".xhtml"):
            continue
        if JEGGED_DOMAIN not in href:
            continue
        normalized = normalize_url(urljoin(f"https://www.{JEGGED_DOMAIN}", href))
        if normalized in link_map:
            a["href"] = link_map[normalized] + ".xhtml"
            changed = True
    if changed:
        chapter.content = str(soup).encode()

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Scrape a jegged.com game guide and save it as an EPUB."
    )
    parser.add_argument("--epub", required=True,
                        help="URL of the jegged.com guide index page")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--debug", action="store_true",
                        help="Print TOC and exit without scraping")
    parser.add_argument("--img-quality", default=75, type=int,
                        help="JPEG quality 1-95 (default: 75)")
    parser.add_argument("--img-size", default="720p", choices=RESOLUTIONS.keys(),
                        help="Image resolution (default: 720p)")
    parser.add_argument("--workers", default=8, type=int,
                        help="Max parallel fetch/scrape threads (default: 8)")
    parser.add_argument("--thumb-size", default="320p",
                        choices=["160p","320p","480p"],
                        help="Thumbnail size for inline images (default: 320p)")
    parser.add_argument("--contrast", default=1.0, type=float,
                        help="Contrast multiplier for images (default: 1.0, try 1.3-1.8 for e-ink)")
    parser.add_argument("--grayscale", action="store_true",
                        help="Convert images to grayscale (ideal for e-ink Kindles, ~60%% smaller)")
    parser.add_argument("--no-images", action="store_true",
                        help="Skip downloading images")
    args = parser.parse_args()

    start_url = args.epub
    max_size = RESOLUTIONS[args.img_size]

    options = uc.ChromeOptions()
    options.page_load_strategy = "none"
    if args.headless:
        options.add_argument("--headless=new")
    driver = uc.Chrome(options=options, headless=False, version_main=145)

    try:
        print(f"Loading: {start_url}")
        load_page(driver, start_url)

        print("Collecting TOC...")
        toc_pages = collect_toc_links(driver, start_url, args.workers)
        print(f"Total pages found: {len(toc_pages)}")

        if args.debug:
            for t, u, d, in_toc in toc_pages:
                label = "(group)" if u is None else u
                toc_marker = "" if in_toc else " [spine only]"
                print(f"  {'  ' * d}{t}  →  {label}{toc_marker}")
            print(f"\nTotal: {len(toc_pages)} entries ({sum(1 for _,u,_ in toc_pages if u)} scrapeable)")
            driver.quit()
            return

        if not toc_pages:
            print("No pages found – check TOC_SELECTORS and try again.")
            driver.quit()
            return

        img_folder = "images-gs" if args.grayscale else "images"
        if not args.no_images:
            os.makedirs(img_folder, exist_ok=True)

        book = epub.EpubBook()
        raw_title = driver.title or "jegged_guide"
        # Strip common jegged/site suffixes from the browser title
        page_title = re.sub(r'\s*[-|–]\s*(Jegged.*|Guide.*|Walkthrough.*)$',
                            '', raw_title, flags=re.IGNORECASE).strip()
        if not page_title:
            page_title = raw_title
        safe_title = slugify(page_title)

        book.set_title(page_title)
        book.set_language("en")

        style = epub.EpubItem(
            uid="style",
            file_name="style/default.css",
            media_type="text/css",
            content=b"""
                body { font-family: Georgia, serif; line-height: 1.6; }
                img {
                    width: 100%;
                    max-width: 100%;
                    height: auto;
                    display: block;
                    margin: 1em auto;
                }
                h1, h2, h3 { font-family: sans-serif; }
                table { border-collapse: collapse; width: 100%; }
                td, th { border: 1px solid #ccc; padding: 4px 8px; }
            """
        )
        book.add_item(style)

        spine = ["nav"]
        epub_toc = []
        link_map = {}
        chapters = []
        all_extra_pages = set()
        content_fingerprints = {}

        # Build link_map: normalized URL → chapter slug
        def norm_seg(u):
            return unquote(u.rstrip("/").split("/")[-1]).lower()

        used_slugs = set()

        def unique_slug(candidate, url):
            """Return candidate slug, falling back to parent+slug if already taken."""
            if candidate not in used_slugs:
                used_slugs.add(candidate)
                return candidate
            # Prefix with the parent path segment to disambiguate
            # e.g. maps/garamsythe-waterway vs walkthrough/garamsythe-waterway
            parts = urlparse(url).path.strip("/").split("/")
            # Walk back through path parts to find a unique prefix
            for i in range(len(parts) - 1, 0, -1):
                prefixed = slugify(parts[i - 1]) + "-" + candidate
                if prefixed not in used_slugs:
                    used_slugs.add(prefixed)
                    return prefixed
            # Last resort: append a counter
            n = 2
            while f"{candidate}-{n}" in used_slugs:
                n += 1
            result = f"{candidate}-{n}"
            used_slugs.add(result)
            return result

        for title, url, d, in_toc in toc_pages:
            if url is None:
                slug = unique_slug(slugify(title) + "-index", "")
                link_map[f"__group__{slugify(title)}"] = slug
            else:
                base = slugify(title) or slugify(url.rstrip("/").split("/")[-1])
                link_map[normalize_url(url)] = unique_slug(base, url)

        seg_index = {norm_seg(u): fn for u, fn in link_map.items() if not u.startswith("__group__")}

        def resolve_extra(ep_url):
            if ep_url in link_map:
                return True
            # Try normalizing and checking again
            ep_norm = normalize_url(ep_url)
            if ep_norm in link_map:
                link_map[ep_url] = link_map[ep_norm]
                return True
            ep_seg = norm_seg(ep_url)
            if ep_seg in seg_index:
                link_map[ep_url] = seg_index[ep_seg]
                return True
            # Check if any link_map key ends with the same path segment
            ep_path = urlparse(ep_url).path.lower().rstrip("/")
            for k in link_map:
                if k.startswith("__group__"):
                    continue
                if urlparse(k).path.lower().rstrip("/") == ep_path:
                    link_map[ep_url] = link_map[k]
                    return True
            return False

        # First pass: scrape TOC pages in parallel, assemble in order
        real_toc = [(t, u, d, it) for t, u, d, it in toc_pages if u is not None]
        print(f"Scraping {len(real_toc)} pages in parallel...")

        # Capture args as locals so the closure doesn't capture the arg namespace
        img_quality = args.img_quality
        no_images   = args.no_images
        grayscale   = args.grayscale
        contrast    = args.contrast
        thumb_size  = THUMB_SIZES[args.thumb_size]

        def scrape_one(item):
            t, u, d, it = item
            result = process_page(driver, u, img_folder, link_map, max_size,
                                  img_quality, no_images, grayscale, contrast, thumb_size)
            return (t, u, d, it, result)

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(scrape_one, item): item for item in real_toc}
            results_map = {}
            for future in as_completed(futures):
                t, u, d, it, result = future.result()
                results_map[u] = (t, d, it, result)
                page_content, _, _ = result
                status = "ok" if page_content is not None else "skipped"
                print(f"  [{status}] {t}")

        # Insert stub chapters for group labels and real chapters in toc order
        for title, url, d, in_toc in toc_pages:
            if url is None:
                file_name = slugify(title) + "-index.xhtml"
                chapter = epub.EpubHtml(
                    title=title,
                    file_name=file_name,
                    content=f"<h1>{title}</h1>",
                )
                chapter.add_link(href="../style/default.css", rel="stylesheet", type="text/css")
                book.add_item(chapter)
                spine.append(chapter)
                chapters.append((chapter, d, title, True))
                continue

            if url not in results_map:
                continue
            title, d, in_toc, result = results_map[url]
            page_content, extra_pages, fp = result
            if page_content is None:
                link_map.pop(normalize_url(url), None)
                continue
            for ep in extra_pages:
                if not resolve_extra(ep):
                    all_extra_pages.add(ep)
            chapter = epub.EpubHtml(
                title=title,
                file_name=link_map[normalize_url(url)] + ".xhtml",
                content=f"<h1>{title}</h1>{page_content}",
            )
            chapter.add_link(href="../style/default.css", rel="stylesheet", type="text/css")
            book.add_item(chapter)
            spine.append(chapter)
            chapters.append((chapter, d, title, in_toc))
            if fp:
                content_fingerprints[fp] = link_map[normalize_url(url)]

        # Add genuinely new extra pages to link_map (unique_slug handles dedup)
        for extra_url in all_extra_pages:
            if extra_url not in link_map:
                candidate = slugify(extra_url.rstrip("/").split("/")[-1])
                link_map[extra_url] = unique_slug(candidate, extra_url)
                seg_index[norm_seg(extra_url)] = link_map[extra_url]

        # Scrape extra pages in parallel
        extra_list = list(all_extra_pages)
        print(f"Scraping {len(extra_list)} extra pages in parallel...")

        def scrape_extra_one(extra_url):
            last_seg = extra_url.rstrip("/").split("/")[-1]
            title = last_seg.replace("-", " ").replace("_", " ").title()
            try:
                pg_content, _, fp = process_page(driver, extra_url, img_folder, link_map,
                                                  max_size, img_quality, no_images, grayscale, contrast, thumb_size)
                return (extra_url, title, pg_content, fp)
            except Exception as e:
                print(f"  Failed to scrape extra page {extra_url}: {e}")
                return (extra_url, title, None, None)

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(scrape_extra_one, u): u for u in extra_list}
            for future in as_completed(futures):
                extra_url, title, pg_content, fp = future.result()
                status = "ok" if pg_content is not None else "skipped"
                print(f"  [{status}] extra: {title}")
                if pg_content is None:
                    link_map.pop(extra_url, None)
                    continue
                if fp and fp in content_fingerprints:
                    existing = content_fingerprints[fp]
                    print(f"  Duplicate content, aliasing to: {existing}.xhtml")
                    link_map[extra_url] = existing
                    continue
                if fp:
                    content_fingerprints[fp] = link_map[extra_url]
                chapter = epub.EpubHtml(
                    title=title,
                    file_name=link_map[extra_url] + ".xhtml",
                    content=f"<h1>{title}</h1>{pg_content}",
                )
                chapter.add_link(href="../style/default.css", rel="stylesheet", type="text/css")
                book.add_item(chapter)
                spine.append(chapter)
                chapters.append((chapter, 0, title, False))

        # Post-pass: fix links now that link_map is complete
        print("Fixing cross-chapter links...")
        for chapter, _, _, _ in chapters:
            fix_chapter_links(chapter, link_map)

        # Build flat TOC — only include entries marked in_toc=True
        # Spine-only entries (partitioned tab pages, extra linked pages)
        # are readable via links but don't appear in the reader's TOC
        flat_toc = []
        for ch, d, t, in_toc in chapters:
            if not in_toc:
                continue
            prefix = "— " * d
            flat_toc.append(epub.Link(ch.file_name, prefix + t, ch.file_name))
        book.toc = tuple(flat_toc)

        # Embed thumbnail and full-size images, create viewer xhtml pages.
        if not args.no_images:
            full_folder = os.path.join(img_folder, "full")
            for f in os.listdir(img_folder):
                path = os.path.join(img_folder, f)
                if not os.path.isfile(path):
                    continue  # skip the full/ subdirectory itself
                ext = f.split(".")[-1].lower()
                mt = "image/jpeg" if ext in ["jpg", "jpeg"] else f"image/{ext}"
                # Embed thumbnail
                with open(path, "rb") as img_file:
                    book.add_item(epub.EpubItem(
                        uid=f, file_name=img_folder + "/" + f,
                        media_type=mt, content=img_file.read()
                    ))
                # Embed full-size version
                full_path = os.path.join(full_folder, f)
                if os.path.exists(full_path):
                    with open(full_path, "rb") as img_file:
                        book.add_item(epub.EpubItem(
                            uid="full-" + f,
                            file_name=img_folder + "/full/" + f,
                            media_type=mt, content=img_file.read()
                        ))
        # Resolve __IMG_VIEWER__ placeholders: create one viewer page per
        # (image, chapter) pair so the back link goes to the exact source chapter.
        if not args.no_images:
            viewer_uid_seen = set()
            for chapter, _, _, _ in chapters:
                soup = BeautifulSoup(chapter.content, "lxml")
                changed = False
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if not href.startswith("__IMG_VIEWER__"):
                        continue
                    img_name = href[len("__IMG_VIEWER__"):]
                    # Use a short hash-based name to avoid long/invalid filenames
                    import hashlib
                    uid = hashlib.md5(f"{chapter.file_name}:{img_name}".encode()).hexdigest()[:10]
                    viewer_name = f"v{uid}.xhtml"
                    if viewer_name not in viewer_uid_seen:
                        viewer_uid_seen.add(viewer_name)
                        viewer = epub.EpubHtml(
                            title=img_name,
                            file_name=viewer_name,
                            content=f'''<style>
body{{margin:0;padding:0;background:#000;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center}}
img{{max-width:100%;max-height:calc(100vh - 3em);object-fit:contain}}
#back{{position:fixed;top:0.5em;left:0.5em;background:rgba(0,0,0,0.6);color:#fff;border:1px solid #fff;border-radius:4px;padding:0.3em 0.8em;font-size:1.1em;text-decoration:none;z-index:999}}
#back:hover{{background:rgba(255,255,255,0.2)}}
</style>
<a id="back" href="{chapter.file_name}">&#8592; Back</a>
<img src="{img_folder}/full/{img_name}" alt="{img_name}"/>'''
                        )
                        book.add_item(viewer)
                        spine.append(viewer)
                    a["href"] = viewer_name
                    changed = True
                if changed:
                    chapter.content = str(soup).encode()

        book.add_item(epub.EpubNav())
        book.add_item(epub.EpubNcx())
        book.spine = spine
        epub.write_epub(f"{safe_title}.epub", book)
        compress_epub(f"{safe_title}.epub")
        print(f"\nDone! → {safe_title}.epub")

    finally:
        driver.quit()

if __name__ == "__main__":
    main()