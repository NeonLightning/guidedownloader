#!/usr/bin/env python3

import os, re, time, requests, io, hashlib
from urllib.parse import urljoin, urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from readability import Document
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup
from ebooklib import epub
from PIL import Image
import zipfile

session = requests.Session()

RESOLUTIONS = {
    "480p":  (480,  640),
    "720p":  (720,  960),
    "1080p": (1080, 1440),
    "2k":    (1264, 1680),
}

BLOCKED_DOMAINS = ["zdbb.net", "doubleclick.net", "googlesyndication.com"]

def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')

def normalize_url(url):
    p = urlparse(url)
    return p.scheme + "://" + p.netloc + p.path.rstrip("/").lower()


def compress_epub(path):
    tmp = path + ".tmp"
    with zipfile.ZipFile(path, 'r') as zin:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
    os.replace(tmp, path)
    print(f"Compressed: {path}")

def download_image(url, folder, max_size=(720, 960), quality=75):
    if url.startswith("data:"): return None
    if any(domain in url for domain in BLOCKED_DOMAINS): return None
    try:
        name = os.path.basename(urlparse(url).path)
        if '.' not in name:
            name += '.jpg'
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            # Send browser headers to avoid CDN serving WebP/AVIF
            headers = {"Accept": "image/jpeg,image/png,image/*", "User-Agent": "Mozilla/5.0"}
            r = session.get(url, timeout=20, headers=headers)
            if name.lower().endswith('.svg'):
                with open(path, "wb") as f: f.write(r.content)
            else:
                try:
                    img = Image.open(io.BytesIO(r.content)).convert("RGB")
                    target_w, target_h = max_size
                    orig_w, orig_h = img.size
                    new_h = int(orig_h * target_w / orig_w)
                    if new_h > target_h:
                        new_w = int(orig_w * target_h / orig_h)
                        img = img.resize((new_w, target_h), Image.LANCZOS)
                    else:
                        img = img.resize((target_w, new_h), Image.LANCZOS)
                    # Always save as jpg regardless of original extension
                    name = os.path.splitext(name)[0] + '.jpg'
                    path = os.path.join(folder, name)
                    img.save(path, format='JPEG', optimize=True, quality=quality)
                except Exception:
                    with open(path, "wb") as f: f.write(r.content)
        return name
    except Exception as e:
        print(f"  Failed to download image {url}: {e}")
        return None

def download_images_parallel(image_urls, img_folder, max_size=(720, 960), quality=75):
    downloaded = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {executor.submit(download_image, url, img_folder, max_size, quality): url for url in image_urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                name = future.result()
                if name:
                    downloaded.append((url, name))
            except:
                pass
    return downloaded

def get_current_toc_container(driver):
    try:
        return WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".wiki-toc span.sidebar-section[class*='toc-']"))
        )
    except TimeoutException:
        return None

def click_back_button(driver):
    try:
        btn = driver.find_element(By.CSS_SELECTOR, '[data-cy="left-chevron"]')
        driver.execute_script("arguments[0].click();", btn)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".wiki-toc span.sidebar-section[class*='toc-']"))
        )
        return True
    except:
        return False

def collect_pages_recursive(driver, container, current_depth, visited=None):
    if visited is None: visited = set()
    pages, idx = [], 0
    while True:
        container = get_current_toc_container(driver)
        if not container: break
        items = container.find_elements(By.CSS_SELECTOR, "a.navigation-item, button.navigation-item")
        if idx >= len(items): break
        item = items[idx]
        if item.tag_name == "a":
            url, title = item.get_attribute("href"), item.text.strip()
            if url and url not in visited:
                visited.add(url)
                pages.append((title, url, current_depth))
                print(f"  Found: level {current_depth} - {title}")
            idx += 1
        elif item.tag_name == "button":
            driver.execute_script("arguments[0].click();", item)
            WebDriverWait(driver, 20).until(EC.staleness_of(container))
            new_container = get_current_toc_container(driver)
            if new_container:
                pages.extend(collect_pages_recursive(driver, new_container, current_depth + 1, visited))
            click_back_button(driver)
            idx += 1
    return pages

def load_page_noblock(driver, url):
    driver.get(url)
    WebDriverWait(driver, 60).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".wiki-toc span.sidebar-section[class*='toc-']"))
    )
    time.sleep(0.3)

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

def scrape_file_page(driver, url, img_folder, max_size=(720, 960), quality=75):
    """Handle IGN wiki File: pages — find the full-size image and return a simple img chapter."""
    try:
        load_page_noblock(driver, url)
        time.sleep(0.3)
        raw_soup = BeautifulSoup(driver.page_source, "lxml")

        img_url = None

        # IGN file pages serve images via oyster.ignimgs.com/mediawiki/ — find that img
        # and strip query params to get the full-size version
        for img in raw_soup.find_all("img"):
            src = get_real_image_url(img)
            if src and "oyster.ignimgs.com/mediawiki/" in src:
                # Strip query params to get full resolution
                img_url = src.split("?")[0]
                break

        if not img_url:
            # Fallback: any link pointing directly to a mediawiki image
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

        name = download_image(img_url, img_folder, max_size, quality)
        if not name:
            return None

        html = f'<img src="images/{name}" alt="{name}"/>'
        return html
    except Exception as e:
        print(f"  Failed to scrape file page {url}: {e}")
        return None

def process_page(driver, url, img_folder, link_map, max_size=(720, 960), quality=75, no_images=False):
    load_page_noblock(driver, url)
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(0.3)

    raw_soup = BeautifulSoup(driver.page_source, "lxml")

    # Detect stub pages before anything else
    raw_text = raw_soup.get_text()
    if "This page has been created by selecting" in raw_text or "Start editing this page" in raw_text:
        print(f"  Stub page (no content): {url}")
        return None, set(), set(), None

    # Extract wiki content directly — IGN puts it in .wiki-html sections
    # inside a .content div. Readability is the wrong tool here and strips it.
    content_sections = raw_soup.select("section.wiki-html, section.wiki-section.wiki-html")
    if not content_sections:
        # broader fallback
        content_sections = raw_soup.find_all(class_=re.compile(r'\bwiki-html\b'))

    if not content_sections:
        print(f"  No wiki-html sections found, skipping: {url}")
        return None, set(), set(), None
    else:
        # Wrap all sections in a single div
        wrapper_html = "<div>" + "".join(str(s) for s in content_sections) + "</div>"
        content_soup = BeautifulSoup(wrapper_html, "lxml")

    # Strip junk from extracted content
    for tag in content_soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    for selector in ["[class*='ad']", "[class*='promo']", "[class*='sponsor']",
                     "[class*='video']", "[class*='paging']", "nav", "footer"]:
        for el in content_soup.select(selector):
            el.decompose()

    # Handle images
    img_map = {}
    if not no_images:
        for i, img in enumerate(content_soup.find_all("img")):
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
        for img in content_soup.find_all("img"):
            img.decompose()

    if not no_images:
        downloaded = download_images_parallel(list(img_map.values()), img_folder, max_size, quality)
        url_to_name = {u: n for u, n in downloaded}
        for p in content_soup.find_all("p"):
            pid = p.get("id", "")
            if pid in img_map:
                orig_url = img_map[pid]
                if orig_url in url_to_name:
                    new_img = content_soup.new_tag("img", src="images/" + url_to_name[orig_url])
                    p.replace_with(new_img)
                else:
                    p.decompose()

    # Fix internal links and collect any extras not yet in link_map
    extra_pages = set()
    file_links = set()
    body = content_soup.find("body") or content_soup
    for a in body.find_all("a"):
        href = a.get("href")
        if not href:
            continue
        full_href = urljoin(url, href)
        normalized = normalize_url(full_href)
        last_seg = normalized.rstrip("/").split("/")[-1]
        if normalized in link_map:
            a["href"] = link_map[normalized] + ".xhtml"
        elif "ign.com/wikis" in normalized:
            if "/wikis/ratings/" in normalized:
                continue
            if last_seg.startswith("file:"):
                file_links.add(normalized)
            else:
                extra_pages.add(normalized)

    content_str = body.decode_contents() if hasattr(body, 'decode_contents') else str(body)
    if len(content_str.strip()) < 50:
        print(f"  Warning: very little content extracted from {url}")

    # Compute fingerprint here to avoid re-parsing in caller
    text_fp = hashlib.md5(body.get_text().strip().encode()).hexdigest()
    return content_str, extra_pages, file_links, text_fp

def inline_file_links(chapters, file_url_to_img, link_map):
    """Replace <a href="File:..."> links with inline <img> tags in all chapters."""
    for chapter, _, _ in chapters:
        soup = BeautifulSoup(chapter.content, "lxml")
        body = soup.find("body")
        if not body:
            continue
        changed = False
        for a in body.find_all("a"):
            href = a.get("href", "")
            # Match both normalized file: URLs and already-converted .xhtml refs
            norm = normalize_url(urljoin("https://www.ign.com", href)) if not href.endswith(".xhtml") else None
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
        normalized = normalize_url(urljoin("https://www.ign.com", href))
        if normalized in link_map:
            a["href"] = link_map[normalized] + ".xhtml"
            changed = True
    if changed:
        chapter.content = str(soup).encode()

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--epub", required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--img-quality", default=75, type=int,
                        help="JPEG quality 1-95 (default: 75)")
    parser.add_argument("--img-size", default="720p", choices=RESOLUTIONS.keys(),
                        help="Image resolution (default: 720p). Options: 480p, 720p, 1080p, 2k")
    parser.add_argument("--no-images", action="store_true",
                        help="Skip downloading images (smaller file, useful for debugging)")
    args = parser.parse_args()
    url = args.epub
    max_size = RESOLUTIONS[args.img_size]

    options = uc.ChromeOptions()
    options.page_load_strategy='none'
    if args.headless: options.add_argument("--headless=new")
    driver = uc.Chrome(options=options, headless=False, version_main=145)

    try:
        load_page_noblock(driver, url)
        container = get_current_toc_container(driver)
        if not container: raise Exception("No TOC found")
        toc_pages = collect_pages_recursive(driver, container, 0)
        print(f"Total pages: {len(toc_pages)}")
        if args.debug:
            for t,u,d in toc_pages: print(f"{d} {t} -> {u}")
            driver.quit(); return
        if not toc_pages: driver.quit(); return

        if not args.no_images:
            os.makedirs("images", exist_ok=True)

        book = epub.EpubBook()
        page_title = driver.title or "ign_wiki"
        page_title = re.sub(r'\s*[-|]\s*IGN.*$', '', page_title).strip()
        safe_title = slugify(page_title)

        book.set_title(page_title)
        book.set_language("en")

        style = epub.EpubItem(
            uid="style",
            file_name="style/default.css",
            media_type="text/css",
            content=b"""
                img {
                    width: 100%;
                    max-width: 100%;
                    height: auto;
                    display: block;
                    margin: 1em auto;
                }
            """
        )
        book.add_item(style)
        spine=["nav"]; epub_toc=[]; link_map={}; chapters=[]
        all_extra_pages = set()
        all_file_links = set()
        content_fingerprints = {}

        # Build link_map with normalized URLs as keys
        for title, url, d in toc_pages:
            link_map[normalize_url(url)] = slugify(title)

        def norm_seg(u):
            return unquote(u.rstrip("/").split("/")[-1]).lower()

        # Reverse index: chapter_slug -> filename for O(1) duplicate lookup
        seg_index = {norm_seg(u): fn for u, fn in link_map.items()}

        def resolve_extra(ep_url, link_map):
            if ep_url in link_map:
                return True
            ep_seg = norm_seg(ep_url)
            if ep_seg in seg_index:
                link_map[ep_url] = seg_index[ep_seg]
                return True
            return False

        # First pass: scrape all TOC pages, collect extra linked pages
        for title, url, d in toc_pages:
            print(f"Scraping: {title}")
            content, extra_pages, file_links, fp = process_page(driver, url, "images", link_map, max_size, args.img_quality, args.no_images)
            if content is None:
                norm = normalize_url(url)
                link_map.pop(norm, None)
                continue
            for ep in extra_pages:
                if not resolve_extra(ep, link_map):
                    all_extra_pages.add(ep)
            all_file_links.update(file_links)
            chapter = epub.EpubHtml(title=title, file_name=link_map[normalize_url(url)]+".xhtml",
                                    content=f"<h1>{title}</h1>{content}")
            chapter.add_link(href="../style/default.css", rel="stylesheet", type="text/css")
            book.add_item(chapter); spine.append(chapter)
            chapters.append((chapter, d, title))
            if fp: content_fingerprints[fp] = link_map[normalize_url(url)]

        # Add genuinely new extra pages to link_map (used_filenames prevents slug collisions)
        used_filenames = set(link_map.values())
        for extra_url in all_extra_pages:
            if extra_url not in link_map:
                candidate = slugify(extra_url.split("/")[-1])
                final = candidate
                n = 2
                while final in used_filenames:
                    final = f"{candidate}-{n}"; n += 1
                link_map[extra_url] = final
                seg_index[norm_seg(extra_url)] = final
                used_filenames.add(final)

        # Scrape extra pages now that they're in link_map
        for extra_url in all_extra_pages:
            last_seg = extra_url.rstrip("/").split("/")[-1]
            title = last_seg.replace("-", " ").replace("_", " ").title()
            is_file_page = last_seg.lower().startswith("file:")
            print(f"Scraping extra: {title}")
            try:
                if is_file_page and not args.no_images:
                    page_content = scrape_file_page(driver, extra_url, "images", max_size, args.img_quality)
                else:
                    page_content, _, _fl, fp = process_page(driver, extra_url, "images", link_map, max_size, args.img_quality, args.no_images)
                if page_content is None:
                    link_map.pop(extra_url, None)
                    continue
                if fp and fp in content_fingerprints:
                    existing_filename = content_fingerprints[fp]
                    print(f"  Duplicate content detected, aliasing to: {existing_filename}.xhtml")
                    link_map[extra_url] = existing_filename
                    continue
                if fp: content_fingerprints[fp] = link_map[extra_url]
                chapter = epub.EpubHtml(title=title, file_name=link_map[extra_url]+".xhtml",
                                        content=f"<h1>{title}</h1>{page_content}")
                chapter.add_link(href="../style/default.css", rel="stylesheet", type="text/css")
                book.add_item(chapter); spine.append(chapter)
                chapters.append((chapter, 0, title))
            except Exception as e:
                print(f"  Failed to scrape extra page {extra_url}: {e}")

        # Resolve File: pages to images and inline them into chapters
        file_url_to_img = {}
        if all_file_links and not args.no_images:
            print(f"Resolving {len(all_file_links)} file pages to images...")
            for file_url in all_file_links:
                name = scrape_file_page(driver, file_url, "images", max_size, args.img_quality)
                # scrape_file_page returns html string; extract just the filename
                if name:
                    m = re.search(r'src="images/([^"]+)"', name)
                    if m:
                        file_url_to_img[file_url] = m.group(1)
            inline_file_links(chapters, file_url_to_img, link_map)

        # Post-pass: fix links in already-scraped TOC chapters that point to extra pages
        print("Fixing links in scraped chapters...")
        for chapter, _, _ in chapters:
            fix_chapter_links(chapter, link_map)

        flat_toc = []
        for ch, d, t in chapters:
            prefix = "— " * d
            flat_toc.append(epub.Link(ch.file_name, prefix + t, ch.file_name))
        book.toc = tuple(flat_toc)

        if not args.no_images:
            for f in os.listdir("images"):
                path=os.path.join("images",f)
                ext=f.split(".")[-1].lower()
                mt="image/jpeg" if ext in ["jpg","jpeg"] else f"image/{ext}"
                with open(path,"rb") as img:
                    book.add_item(epub.EpubItem(uid=f,file_name="images/"+f,media_type=mt,content=img.read()))

        book.add_item(epub.EpubNav())
        book.add_item(epub.EpubNcx())
        book.spine=spine
        epub.write_epub(f"{safe_title}.epub", book)
        compress_epub(f"{safe_title}.epub")
        print("EPUB created successfully!")

    finally: driver.quit()

if __name__=="__main__":
    main()