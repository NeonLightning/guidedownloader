#!/usr/bin/env python3
"""
IGN wiki → EPUB scraper
Usage: python ign.py --epub "https://www.ign.com/wikis/some-game/" [options]

TOC navigation uses Selenium (IGN's sidebar is fully JS-rendered).
Page content is fetched via requests where possible, Selenium as fallback.
"""

import os, re, time, requests, io, hashlib
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
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
session.mount("https://", adapter)
session.mount("http://",  adapter)

THUMB_SIZES = {
    "160p": (160, 213),
    "320p": (320, 427),
    "480p": (480, 640),
}

RESOLUTIONS = {
    "kindle": (758,  1024),
    "480p":   (480,   640),
    "720p":   (720,   960),
    "1080p":  (1080, 1440),
    "2k":     (1264, 1680),
}

BLOCKED_DOMAINS = ["zdbb.net", "doubleclick.net", "googlesyndication.com"]

IGN_DOMAIN = "ign.com"


# ── Utilities ─────────────────────────────────────────────────────────────────

def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')

def normalize_url(url):
    p = urlparse(url)
    return p.scheme + "://" + p.netloc + p.path.rstrip("/").lower()

def minify_xml(data: bytes) -> bytes:
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
                        pass
                zout.writestr(item, data)
    os.replace(tmp, path)
    print(f"Compressed: {path}")


# ── Image handling ─────────────────────────────────────────────────────────────

def download_image(url, folder, max_size=(720, 960), quality=75,
                   grayscale=False, contrast=1.0, thumb_size=(320, 427)):
    if url.startswith("data:"):
        return None
    if any(domain in url for domain in BLOCKED_DOMAINS):
        return None
    try:
        name = os.path.basename(urlparse(url).path)
        if '.' not in name:
            name += '.jpg'
        if not name.lower().endswith(('.jpg', '.jpeg', '.svg')):
            name = os.path.splitext(name)[0] + '.jpg'
        path        = os.path.join(folder, name)
        full_folder = os.path.join(folder, "full")
        full_path   = os.path.join(full_folder, name)
        os.makedirs(full_folder, exist_ok=True)
        if not os.path.exists(path):
            headers = {"Accept": "image/jpeg,image/png,image/*", "User-Agent": "Mozilla/5.0"}
            r = session.get(url, timeout=20, headers=headers)
            if len(r.content) < 10_000:
                return None
            if name.lower().endswith('.svg'):
                with open(path, "wb") as f:     f.write(r.content)
                with open(full_path, "wb") as f: f.write(r.content)
            else:
                try:
                    img = Image.open(io.BytesIO(r.content))
                    img = img.convert("L" if grayscale else "RGB")
                    if contrast != 1.0:
                        img = ImageEnhance.Contrast(img).enhance(contrast)

                    save_kwargs = dict(format='JPEG', optimize=True, progressive=True, quality=quality)
                    if not grayscale:
                        save_kwargs['subsampling'] = 2

                    orig_w, orig_h = img.size

                    # Full-size version
                    fw, fh = max_size
                    fnh = int(orig_h * fw / orig_w)
                    if fnh > fh:
                        full_img = img.resize((int(orig_w * fh / orig_h), fh), Image.LANCZOS)
                    else:
                        full_img = img.resize((fw, fnh), Image.LANCZOS)
                    full_img.save(full_path, **save_kwargs)

                    # Thumbnail version
                    tw, th = thumb_size
                    tnh = int(orig_h * tw / orig_w)
                    if tnh > th:
                        thumb_img = img.resize((int(orig_w * th / orig_h), th), Image.LANCZOS)
                    else:
                        thumb_img = img.resize((tw, tnh), Image.LANCZOS)
                    thumb_img.save(path, **save_kwargs)

                except Exception as img_err:
                    print(f"  PIL failed for {url}: {img_err}")
                    if grayscale:
                        return None
                    with open(path, "wb") as f:     f.write(r.content)
                    with open(full_path, "wb") as f: f.write(r.content)
        return name
    except Exception as e:
        print(f"  Failed to download image {url}: {e}")
        return None

def download_images_parallel(image_urls, img_folder, max_size=(720, 960),
                              quality=75, grayscale=False, contrast=1.0,
                              thumb_size=(320, 427)):
    downloaded = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        fut = {executor.submit(download_image, url, img_folder, max_size,
                               quality, grayscale, contrast, thumb_size): url
               for url in image_urls}
        for future in as_completed(fut):
            try:
                name = future.result()
                if name:
                    downloaded.append((fut[future], name))
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


# ── IGN-specific Selenium TOC navigation ─────────────────────────────────────

def get_current_toc_container(driver):
    try:
        return WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, ".wiki-toc span.sidebar-section[class*='toc-']")
            )
        )
    except TimeoutException:
        return None

def click_back_button(driver):
    try:
        btn = driver.find_element(By.CSS_SELECTOR, '[data-cy="left-chevron"]')
        driver.execute_script("arguments[0].click();", btn)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, ".wiki-toc span.sidebar-section[class*='toc-']")
            )
        )
        return True
    except Exception:
        return False

def collect_pages_recursive(driver, container, current_depth, visited=None):
    if visited is None:
        visited = set()
    pages, idx = [], 0
    while True:
        container = get_current_toc_container(driver)
        if not container:
            break
        items = container.find_elements(
            By.CSS_SELECTOR, "a.navigation-item, button.navigation-item"
        )
        if idx >= len(items):
            break
        item = items[idx]
        if item.tag_name == "a":
            url, title = item.get_attribute("href"), item.text.strip()
            if url and url not in visited:
                visited.add(url)
                pages.append((title, url, current_depth, True))
                print(f"  Found: level {current_depth} - {title}")
            idx += 1
        elif item.tag_name == "button":
            driver.execute_script("arguments[0].click();", item)
            WebDriverWait(driver, 20).until(EC.staleness_of(container))
            new_container = get_current_toc_container(driver)
            if new_container:
                pages.extend(collect_pages_recursive(
                    driver, new_container, current_depth + 1, visited
                ))
            click_back_button(driver)
            idx += 1
    return pages

def load_page_noblock(driver, url):
    driver.get(url)
    # Wait for sidebar TOC to confirm the page has loaded
    WebDriverWait(driver, 60).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, ".wiki-toc span.sidebar-section[class*='toc-']")
        )
    )
    # Wait for wiki content to actually render — IGN renders sidebar and body
    # in separate React passes. We wait for a section with real text content,
    # not just any section element (which may appear empty initially).
    from selenium.common.exceptions import StaleElementReferenceException

    def wiki_content_ready(d):
        try:
            els = d.find_elements(
                By.CSS_SELECTOR, "section.wiki-html, section.wiki-section"
            )
            return len(els) > 0 and any(len(el.text.strip()) > 50 for el in els)
        except StaleElementReferenceException:
            return False  # React re-rendered; WebDriverWait will retry

    try:
        WebDriverWait(driver, 20).until(wiki_content_ready)
    except TimeoutException:
        time.sleep(1.5)
    time.sleep(0.3)


# ── Page scraping ──────────────────────────────────────────────────────────────

def scrape_file_page(driver, url, img_folder, max_size=(720, 960), quality=75,
                     grayscale=False, contrast=1.0, thumb_size=(320, 427)):
    """Handle IGN wiki File: pages — download full-size image and return img HTML."""
    try:
        load_page_noblock(driver, url)
        time.sleep(0.3)
        raw_soup = BeautifulSoup(driver.page_source, "lxml")

        img_url = None
        for img in raw_soup.find_all("img"):
            src = get_real_image_url(img)
            if src and "oyster.ignimgs.com/mediawiki/" in src:
                img_url = src.split("?")[0]
                break

        if not img_url:
            for a in raw_soup.find_all("a"):
                href = a.get("href", "")
                if "oyster.ignimgs.com/mediawiki/" in href or "apis.ign.com" in href:
                    img_url = href.split("?")[0]
                    break

        if not img_url:
            print(f"  No image found on file page: {url}")
            return None

        img_url = urljoin(url, img_url)
        if any(domain in img_url for domain in BLOCKED_DOMAINS):
            return None

        name = download_image(img_url, img_folder, max_size, quality,
                              grayscale, contrast, thumb_size)
        if not name:
            return None

        return f'<img src="{img_folder}/{name}" alt="{name}"/>'
    except Exception as e:
        print(f"  Failed to scrape file page {url}: {e}")
        return None

def process_page(driver, url, img_folder, link_map, max_size=(720, 960),
                 quality=75, no_images=False, grayscale=False, contrast=1.0,
                 thumb_size=(320, 427)):
    """Scrape an IGN wiki page. Always uses Selenium (IGN is JS-rendered)."""
    load_page_noblock(driver, url)
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(0.3)

    raw_soup = BeautifulSoup(driver.page_source, "lxml")
    raw_text = raw_soup.get_text()

    # Stub detection
    if ("This page has been created by selecting" in raw_text
            or "Start editing this page" in raw_text):
        print(f"  Stub page (no content): {url}")
        return None, set(), set(), None

    # IGN puts content in .wiki-html sections
    content_sections = raw_soup.select(
        "section.wiki-html, section.wiki-section.wiki-html"
    )
    if not content_sections:
        content_sections = raw_soup.find_all(class_=re.compile(r'\bwiki-html\b'))
    if not content_sections:
        print(f"  No wiki-html sections found, skipping: {url}")
        return None, set(), set(), None

    wrapper_html = "<div>" + "".join(str(s) for s in content_sections) + "</div>"
    content_soup = BeautifulSoup(wrapper_html, "lxml")
    content_el   = content_soup.find("body") or content_soup

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

    # Unwrap lightbox links
    ASSET_EXTS = ('.webp', '.png', '.jpg', '.jpeg', '.gif', '.svg')
    for a in list(content_el.find_all("a", href=True)):
        href = a.get("href", "")
        if any(href.lower().endswith(ext) for ext in ASSET_EXTS):
            a.unwrap()

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
        downloaded = download_images_parallel(
            list(img_map.values()), img_folder, max_size, quality,
            grayscale, contrast, thumb_size
        )
        url_to_name = {u: n for u, n in downloaded}
        for p in content_el.find_all("p"):
            pid = p.get("id", "")
            if pid in img_map:
                orig_url = img_map[pid]
                if orig_url in url_to_name:
                    img_src = img_folder + "/" + url_to_name[orig_url]
                    new_img = content_soup.new_tag("img", src=img_src)
                    new_a   = content_soup.new_tag(
                        "a", **{"href": "__IMG_VIEWER__" + url_to_name[orig_url],
                                "class": "img-link"}
                    )
                    new_a.append(new_img)
                    p.replace_with(new_a)
                else:
                    p.decompose()

    # Fix internal links and collect extras / File: links
    extra_pages = set()
    file_links  = set()
    for a in content_el.find_all("a"):
        href = a.get("href")
        if not href:
            continue
        if any(href.lower().endswith(ext) for ext in ('.webp','.png','.jpg','.jpeg','.gif','.svg','.css','.js')):
            continue
        full_href  = urljoin(url, href)
        normalized = normalize_url(full_href)
        last_seg   = normalized.rstrip("/").split("/")[-1]
        if normalized in link_map:
            a["href"] = link_map[normalized] + ".xhtml"
        elif "ign.com/wikis" in normalized:
            if "/wikis/ratings/" in normalized:
                continue
            if last_seg.startswith("file:"):
                file_links.add(normalized)
            else:
                extra_pages.add(normalized)

    content_str = content_el.decode_contents() if hasattr(content_el, 'decode_contents') else str(content_el)
    if len(content_str.strip()) < 50:
        print(f"  Warning: very little content extracted from {url}")

    text_fp = hashlib.md5(content_el.get_text().strip().encode()).hexdigest()
    return content_str, extra_pages, file_links, text_fp

def inline_file_links(chapters, file_url_to_img, link_map):
    """Replace <a href="File:..."> links with inline <img> tags."""
    for chapter, _, _, _ in chapters:
        soup = BeautifulSoup(chapter.content, "lxml")
        body = soup.find("body")
        if not body:
            continue
        changed = False
        for a in body.find_all("a"):
            href = a.get("href", "")
            norm = (normalize_url(urljoin("https://www.ign.com", href))
                    if not href.endswith(".xhtml") else None)
            img_name = None
            if norm and norm in file_url_to_img:
                img_name = file_url_to_img[norm]
            if img_name:
                new_img = soup.new_tag("img", src="images/" + img_name)
                a.replace_with(new_img)
                changed = True
        if changed:
            chapter.content = str(soup).encode()

def fix_chapter_links(chapter, link_map):
    """Re-run link fixing after link_map is complete."""
    soup = BeautifulSoup(chapter.content, "lxml")
    body = soup.find("body")
    if not body:
        return
    changed = False
    for a in body.find_all("a"):
        href = a.get("href")
        if not href or href.endswith(".xhtml") or href.startswith("__IMG_VIEWER__"):
            continue
        if IGN_DOMAIN not in href:
            continue
        normalized = normalize_url(urljoin("https://www.ign.com", href))
        if normalized in link_map:
            a["href"] = link_map[normalized] + ".xhtml"
            changed = True
    if changed:
        chapter.content = str(soup).encode()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Scrape an IGN wiki guide and save it as an EPUB."
    )
    parser.add_argument("--epub",       required=True, help="URL of the IGN wiki index page")
    parser.add_argument("--headless",   action="store_true")
    parser.add_argument("--debug",      action="store_true", help="Print TOC and exit")
    parser.add_argument("--img-quality", default=75, type=int,
                        help="JPEG quality 1-95 (default: 75)")
    parser.add_argument("--img-size",   default="720p", choices=RESOLUTIONS.keys(),
                        help="Full-size image resolution (default: 720p)")
    parser.add_argument("--thumb-size", default="320p", choices=THUMB_SIZES.keys(),
                        help="Thumbnail size for inline images (default: 320p)")
    parser.add_argument("--workers",    default=8, type=int,
                        help="Parallel scrape threads (default: 8)")
    parser.add_argument("--contrast",   default=1.0, type=float,
                        help="Image contrast multiplier (default: 1.0, try 1.3-1.8 for e-ink)")
    parser.add_argument("--grayscale",  action="store_true",
                        help="Convert images to grayscale (ideal for Kindle e-ink)")
    parser.add_argument("--no-images",  action="store_true",
                        help="Skip downloading images")
    args = parser.parse_args()

    url        = args.epub
    max_size   = RESOLUTIONS[args.img_size]
    thumb_size = THUMB_SIZES[args.thumb_size]
    img_folder = "images-gs" if args.grayscale else "images"

    options = uc.ChromeOptions()
    options.page_load_strategy = 'none'
    if args.headless:
        options.add_argument("--headless=new")
    driver = uc.Chrome(options=options, headless=False, version_main=145)

    try:
        print(f"Loading: {url}")
        load_page_noblock(driver, url)
        container = get_current_toc_container(driver)
        if not container:
            raise Exception("No TOC found — check that the URL is a valid IGN wiki page")

        print("Collecting TOC...")
        toc_pages = collect_pages_recursive(driver, container, 0)
        print(f"Total pages: {len(toc_pages)}")

        if args.debug:
            for t, u, d, in_toc in toc_pages:
                marker = "" if in_toc else " [spine only]"
                print(f"  {'  ' * d}{t}  →  {u}{marker}")
            driver.quit()
            return

        if not toc_pages:
            driver.quit()
            return

        if not args.no_images:
            os.makedirs(img_folder, exist_ok=True)

        book = epub.EpubBook()
        page_title = re.sub(r'\s*[-|]\s*IGN.*$', '', driver.title or "ign_wiki").strip()
        safe_title = slugify(page_title)
        book.set_title(page_title)
        book.set_language("en")

        style = epub.EpubItem(
            uid="style", file_name="style/default.css", media_type="text/css",
            content=b"""
                body { font-family: Georgia, serif; line-height: 1.6; }
                img { width: 100%; max-width: 100%; height: auto;
                      display: block; margin: 1em auto; }
                h1, h2, h3 { font-family: sans-serif; }
                table { border-collapse: collapse; width: 100%; }
                td, th { border: 1px solid #ccc; padding: 4px 8px; }
            """
        )
        book.add_item(style)

        spine               = ["nav"]
        link_map            = {}
        chapters            = []
        all_extra_pages     = set()
        all_file_links      = set()
        content_fingerprints = {}

        def norm_seg(u):
            return unquote(u.rstrip("/").split("/")[-1]).lower()

        used_slugs = set()

        def unique_slug(candidate, url):
            if candidate not in used_slugs:
                used_slugs.add(candidate)
                return candidate
            parts = urlparse(url).path.strip("/").split("/")
            for i in range(len(parts) - 1, 0, -1):
                prefixed = slugify(parts[i - 1]) + "-" + candidate
                if prefixed not in used_slugs:
                    used_slugs.add(prefixed)
                    return prefixed
            n = 2
            while f"{candidate}-{n}" in used_slugs:
                n += 1
            result = f"{candidate}-{n}"
            used_slugs.add(result)
            return result

        for title, url_i, d, in_toc in toc_pages:
            base = slugify(title) or slugify(url_i.rstrip("/").split("/")[-1])
            link_map[normalize_url(url_i)] = unique_slug(base, url_i)

        seg_index = {norm_seg(u): fn for u, fn in link_map.items()}

        def resolve_extra(ep_url):
            if ep_url in link_map:
                return True
            ep_norm = normalize_url(ep_url)
            if ep_norm in link_map:
                link_map[ep_url] = link_map[ep_norm]
                return True
            ep_seg = norm_seg(ep_url)
            if ep_seg in seg_index:
                link_map[ep_url] = seg_index[ep_seg]
                return True
            ep_path = urlparse(ep_url).path.lower().rstrip("/")
            for k in link_map:
                if urlparse(k).path.lower().rstrip("/") == ep_path:
                    link_map[ep_url] = link_map[k]
                    return True
            return False

        # Capture locals for closures
        img_quality = args.img_quality
        no_images   = args.no_images
        grayscale   = args.grayscale
        contrast    = args.contrast

        # Parallel scrape
        # IGN requires Selenium for every page — must scrape sequentially
        # since all threads share one browser and driver.get() is not thread-safe
        print(f"Scraping {len(toc_pages)} pages sequentially...")
        for title, url_i, d, in_toc in toc_pages:
            print(f"  Scraping: {title}")
            result = process_page(driver, url_i, img_folder, link_map, max_size,
                                  img_quality, no_images, grayscale, contrast, thumb_size)
            page_content, extra_pages, file_links, fp = result
            if page_content is None:
                link_map.pop(normalize_url(url_i), None)
                continue
            for ep in extra_pages:
                if not resolve_extra(ep):
                    all_extra_pages.add(ep)
            all_file_links.update(file_links)
            chapter = epub.EpubHtml(
                title=title,
                file_name=link_map[normalize_url(url_i)] + ".xhtml",
                content=f"<h1>{title}</h1>{page_content}",
            )
            chapter.add_link(href="../style/default.css", rel="stylesheet", type="text/css")
            book.add_item(chapter)
            spine.append(chapter)
            chapters.append((chapter, d, title, in_toc))
            if fp:
                content_fingerprints[fp] = link_map[normalize_url(url_i)]

        # Extra pages
        for extra_url in all_extra_pages:
            if extra_url not in link_map:
                candidate = slugify(extra_url.rstrip("/").split("/")[-1])
                link_map[extra_url] = unique_slug(candidate, extra_url)
                seg_index[norm_seg(extra_url)] = link_map[extra_url]

        # Extra pages — also sequential for same reason
        print(f"Scraping {len(all_extra_pages)} extra pages sequentially...")
        for extra_url in all_extra_pages:
            last_seg = extra_url.rstrip("/").split("/")[-1]
            title    = last_seg.replace("-", " ").replace("_", " ").title()
            is_file  = last_seg.lower().startswith("file:")
            print(f"  Scraping extra: {title}")
            try:
                if is_file and not no_images:
                    pg_content = scrape_file_page(driver, extra_url, img_folder,
                                                  max_size, img_quality, grayscale,
                                                  contrast, thumb_size)
                    fp = None
                else:
                    pg_content, _, _, fp = process_page(
                        driver, extra_url, img_folder, link_map,
                        max_size, img_quality, no_images, grayscale, contrast, thumb_size
                    )
            except Exception as e:
                print(f"  Failed: {extra_url}: {e}")
                pg_content, fp = None, None

            if pg_content is None:
                link_map.pop(extra_url, None)
                continue
            if fp and fp in content_fingerprints:
                existing = content_fingerprints[fp]
                print(f"  Duplicate, aliasing to: {existing}.xhtml")
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

        # Resolve File: links to inline images
        file_url_to_img = {}
        if all_file_links and not no_images:
            print(f"Resolving {len(all_file_links)} File: pages...")
            for file_url in all_file_links:
                html = scrape_file_page(driver, file_url, img_folder, max_size,
                                        img_quality, grayscale, contrast, thumb_size)
                if html:
                    m = re.search(r'src="[^/]+/([^"]+)"', html)
                    if m:
                        file_url_to_img[file_url] = m.group(1)
            inline_file_links(chapters, file_url_to_img, link_map)

        # Fix cross-chapter links
        print("Fixing cross-chapter links...")
        for chapter, _, _, _ in chapters:
            fix_chapter_links(chapter, link_map)

        # Build TOC (in_toc=True entries only)
        flat_toc = []
        for ch, d, t, in_toc in chapters:
            if not in_toc:
                continue
            flat_toc.append(epub.Link(ch.file_name, "— " * d + t, ch.file_name))
        book.toc = tuple(flat_toc)

        # Embed images (thumbnail + full-size)
        if not no_images:
            full_folder = os.path.join(img_folder, "full")
            for f in os.listdir(img_folder):
                path = os.path.join(img_folder, f)
                if not os.path.isfile(path):
                    continue
                ext = f.split(".")[-1].lower()
                mt  = "image/jpeg" if ext in ["jpg", "jpeg"] else f"image/{ext}"
                with open(path, "rb") as img_file:
                    book.add_item(epub.EpubItem(
                        uid=f, file_name=img_folder + "/" + f,
                        media_type=mt, content=img_file.read()
                    ))
                full_path = os.path.join(full_folder, f)
                if os.path.exists(full_path):
                    with open(full_path, "rb") as img_file:
                        book.add_item(epub.EpubItem(
                            uid="full-" + f,
                            file_name=img_folder + "/full/" + f,
                            media_type=mt, content=img_file.read()
                        ))

        # Create per-chapter image viewer pages
        if not no_images:
            viewer_uid_seen = set()
            for chapter, _, _, _ in chapters:
                soup    = BeautifulSoup(chapter.content, "lxml")
                changed = False
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if not href.startswith("__IMG_VIEWER__"):
                        continue
                    img_name = href[len("__IMG_VIEWER__"):]
                    uid = hashlib.md5(
                        f"{chapter.file_name}:{img_name}".encode()
                    ).hexdigest()[:10]
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