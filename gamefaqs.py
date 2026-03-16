#!/usr/bin/env python3
"""
GameFAQs guide → EPUB / PDF / text scraper
Usage: python gamefaqs.py [OPTIONS] URL

Adds single=1 to the URL automatically to get the full guide on one page.
PDF and text modes are unchanged from the original.
EPUB mode uses ebooklib with chapter splitting, image processing, and
all the same options as the jegged/ign scrapers.
"""

import os, sys, re, io, hashlib, pathlib, tempfile, zipfile, base64, time
import urllib.parse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from optparse import OptionParser
from bs4 import BeautifulSoup
from ebooklib import epub
from PIL import Image, ImageEnhance
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
import undetected_chromedriver as uc

# ── Session ───────────────────────────────────────────────────────────────────
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
session.mount("https://", adapter)
session.mount("http://",  adapter)

# ── Constants ─────────────────────────────────────────────────────────────────
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

BLOCKED_DOMAINS = ["doubleclick.net", "googlesyndication.com"]

GAMEFAQS_REMOVE_SELECTORS = [
    '#header', '#footer', '#nav', '#site-nav', '#breadcrumb',
    '#ad-box', '.ad-wrap', '.ad-unit', '.ad-container',
    '#right-col', '.right-rail', '#sidebar',
    '.header-wrap', '.footer-wrap',
    '#paginate', '.paginate',
    '#account-header', '.account-header',
    '#search-bar', '.search-bar',
    '.site-header', '.site-footer',
    '#site-header', '#site-footer',
]

# ── Utilities ─────────────────────────────────────────────────────────────────

def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')

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
        name = os.path.basename(urllib.parse.urlparse(url).path)
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
            if len(r.content) < 5_000:
                return None
            if name.lower().endswith('.svg'):
                with open(path, "wb") as f:      f.write(r.content)
                with open(full_path, "wb") as f:  f.write(r.content)
            else:
                try:
                    img = Image.open(io.BytesIO(r.content))
                    img = img.convert("L" if grayscale else "RGB")
                    if contrast != 1.0:
                        img = ImageEnhance.Contrast(img).enhance(contrast)
                    save_kwargs = dict(format='JPEG', optimize=True,
                                       progressive=True, quality=quality)
                    if not grayscale:
                        save_kwargs['subsampling'] = 2
                    orig_w, orig_h = img.size
                    # Full-size
                    fw, fh = max_size
                    fnh = int(orig_h * fw / orig_w)
                    fi  = img.resize((int(orig_w * fh / orig_h), fh) if fnh > fh
                                     else (fw, fnh), Image.LANCZOS)
                    fi.save(full_path, **save_kwargs)
                    # Thumbnail
                    tw, th = thumb_size
                    tnh = int(orig_h * tw / orig_w)
                    ti  = img.resize((int(orig_w * th / orig_h), th) if tnh > th
                                     else (tw, tnh), Image.LANCZOS)
                    ti.save(path, **save_kwargs)
                except Exception as e:
                    print(f"  PIL failed for {url}: {e}")
                    if grayscale:
                        return None
                    with open(path, "wb") as f:      f.write(r.content)
                    with open(full_path, "wb") as f:  f.write(r.content)
        return name
    except Exception as e:
        print(f"  Failed: {url}: {e}")
        return None

def download_images_parallel(urls, folder, max_size, quality,
                              grayscale, contrast, thumb_size):
    downloaded = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        fut = {ex.submit(download_image, u, folder, max_size, quality,
                         grayscale, contrast, thumb_size): u for u in urls}
        for f in as_completed(fut):
            try:
                name = f.result()
                if name:
                    downloaded.append((fut[f], name))
            except Exception:
                pass
    return downloaded


# ── Chapter splitting ──────────────────────────────────────────────────────────

def split_into_chapters(faq_body, guide_title):
    """
    Split FAQ content into (title, html_content) chapter pairs.

    Handles two guide types:
    1. Text/ASCII guides: content in <pre> tags with ===SECTION=== style headers
    2. HTML guides: content with <h1>-<h3> heading tags

    Returns list of (title, html_string) tuples.
    """
    # Detect guide type
    pre_tags = faq_body.find_all('pre')
    has_headings = bool(faq_body.find(['h1', 'h2', 'h3']))

    if pre_tags and not has_headings:
        return _split_text_guide(pre_tags, guide_title)
    else:
        return _split_html_guide(faq_body, guide_title)


def _split_text_guide(pre_tags, guide_title):
    """Split ASCII text guide by section header patterns."""
    # Combine all pre blocks
    full_text = "\n".join(tag.get_text() for tag in pre_tags)

    # Common GameFAQs section header patterns (order matters — most specific first)
    HEADER_PATTERNS = [
        r'^={5,}[^=\n]{2,}={5,}\s*$',      # =====SECTION=====
        r'^-{5,}[^-\n]{2,}-{5,}\s*$',       # -----SECTION-----
        r'^\*{5,}[^*\n]{2,}\*{5,}\s*$',     # *****SECTION*****
        r'^#{5,}[^#\n]{2,}#{5,}\s*$',       # #####SECTION#####
        r'^={3,}\s+[^=\n]{2,}\s+={3,}\s*$', # === SECTION ===
        r'^\[{1,2}[^\]\n]{3,}\]{1,2}\s*$',  # [SECTION] or [[SECTION]]
    ]
    combined_pattern = '|'.join(f'(?:{p})' for p in HEADER_PATTERNS)

    lines  = full_text.split('\n')
    chapters = []
    current_title   = guide_title
    current_lines   = []

    for line in lines:
        if re.match(combined_pattern, line, re.MULTILINE):
            # Save previous section
            if current_lines and any(l.strip() for l in current_lines):
                chapters.append((current_title, _lines_to_html(current_lines)))
            current_title = line.strip().strip('=-*#[]').strip()
            current_lines = []
        else:
            current_lines.append(line)

    # Save last section
    if current_lines and any(l.strip() for l in current_lines):
        chapters.append((current_title, _lines_to_html(current_lines)))

    # If no sections found, return as single chapter
    if not chapters:
        chapters = [(guide_title, _lines_to_html(lines))]

    return chapters


def _lines_to_html(lines):
    text = "\n".join(lines)
    # Escape HTML special chars and wrap in pre for monospace layout
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<pre>{text}</pre>"


def _split_html_guide(faq_body, guide_title):
    """Split HTML guide by h1/h2/h3 heading tags."""
    chapters     = []
    current_title = guide_title
    current_parts = []

    for el in faq_body.children:
        if not hasattr(el, 'name') or not el.name:
            continue
        if el.name in ('h1', 'h2', 'h3'):
            if current_parts:
                chapters.append((current_title, "".join(str(p) for p in current_parts)))
            current_title = el.get_text(strip=True)
            current_parts = []
        else:
            current_parts.append(el)

    if current_parts:
        chapters.append((current_title, "".join(str(p) for p in current_parts)))

    if not chapters:
        chapters = [(guide_title, str(faq_body))]

    return chapters


# ── EPUB builder ───────────────────────────────────────────────────────────────

def build_epub(faq_body, guide_title, guide_id, parsed_url, opts,
               max_size, thumb_size):
    """Build an EPUB using ebooklib with chapters, images, and viewer pages."""

    img_folder = "images-gs" if opts.grayscale else "images"
    if not opts.no_images:
        os.makedirs(img_folder, exist_ok=True)

    book = epub.EpubBook()
    book.set_title(guide_title)
    book.set_language("en")
    book.set_identifier(guide_id)

    style = epub.EpubItem(
        uid="style", file_name="style/default.css", media_type="text/css",
        content=b"""
            body  { font-family: Georgia, serif; line-height: 1.6; margin: 1em; }
            pre, code { white-space: pre-wrap; font-family: monospace;
                        font-size: 0.82em; line-height: 1.4; }
            img   { width: 100%; max-width: 100%; height: auto;
                    display: block; margin: 1em auto; }
            h1, h2, h3 { font-family: sans-serif; }
            table { border-collapse: collapse; width: 100%; }
            td, th { border: 1px solid #ccc; padding: 4px 8px; }
        """
    )
    book.add_item(style)

    # Download all images up-front
    img_map = {}  # original_url -> local_filename
    if not opts.no_images:
        img_urls = []
        for img in faq_body.find_all('img'):
            src = img.get('src', '')
            if not src or src.startswith('data:'):
                continue
            # Resolve relative URLs
            if src.startswith('/'):
                src = f"https://{parsed_url.hostname}{src}"
            elif not src.startswith('http'):
                src = f"https://{parsed_url.hostname}/a/faqs/" \
                      f"{guide_id[3:]}/{guide_id}-" \
                      f"{os.path.basename(src.rstrip('/'))}.jpg"
            img_urls.append(src)

        print(f"Downloading {len(img_urls)} images...")
        downloaded = download_images_parallel(
            img_urls, img_folder, max_size, opts.img_quality,
            opts.grayscale, opts.contrast, thumb_size
        )
        img_map = {url: name for url, name in downloaded}
        print(f"  Downloaded {len(img_map)} images")

        # Rewrite img src in the body to use local paths
        for img in faq_body.find_all('img'):
            src = img.get('src', '')
            if src in img_map:
                img['src'] = img_folder + "/" + img_map[src]
                img.pop('width',  None)
                img.pop('height', None)

    # Unwrap lightbox links wrapping images
    for a in list(faq_body.find_all('a', href=True)):
        href = a.get('href', '')
        if any(href.lower().endswith(ext)
               for ext in ('.webp','.png','.jpg','.jpeg','.gif','.svg')):
            a.unwrap()

    # Split into chapters
    print("Splitting into chapters...")
    chapters_data = split_into_chapters(faq_body, guide_title)
    print(f"  {len(chapters_data)} chapters")

    spine    = ["nav"]
    chapters = []
    used_slugs = set()

    def unique_slug(s):
        base = slugify(s) or "chapter"
        if base not in used_slugs:
            used_slugs.add(base)
            return base
        n = 2
        while f"{base}-{n}" in used_slugs:
            n += 1
        used_slugs.add(f"{base}-{n}")
        return f"{base}-{n}"

    for i, (title, content_html) in enumerate(chapters_data):
        fn      = unique_slug(title) + ".xhtml"
        chapter = epub.EpubHtml(
            title=title, file_name=fn,
            content=f"<h1>{title}</h1>{content_html}"
        )
        chapter.add_link(href="../style/default.css",
                         rel="stylesheet", type="text/css")
        book.add_item(chapter)
        spine.append(chapter)
        chapters.append(chapter)

    # Embed images
    if not opts.no_images:
        full_folder = os.path.join(img_folder, "full")
        for f in os.listdir(img_folder):
            fpath = os.path.join(img_folder, f)
            if not os.path.isfile(fpath):
                continue
            ext = f.split(".")[-1].lower()
            mt  = "image/jpeg" if ext in ["jpg","jpeg"] else f"image/{ext}"
            with open(fpath, "rb") as fh:
                book.add_item(epub.EpubItem(
                    uid=f, file_name=img_folder+"/"+f,
                    media_type=mt, content=fh.read()
                ))
            fp2 = os.path.join(full_folder, f)
            if os.path.exists(fp2):
                with open(fp2, "rb") as fh:
                    book.add_item(epub.EpubItem(
                        uid="full-"+f,
                        file_name=img_folder+"/full/"+f,
                        media_type=mt, content=fh.read()
                    ))

    # Image viewer pages
    if not opts.no_images:
        viewer_seen = set()
        for chapter in chapters:
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
                vname = f"v{uid}.xhtml"
                if vname not in viewer_seen:
                    viewer_seen.add(vname)
                    viewer = epub.EpubHtml(
                        title=img_name, file_name=vname,
                        content=f'''<style>
body{{margin:0;padding:0;background:#000;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center}}
img{{max-width:100%;max-height:calc(100vh - 3em);object-fit:contain}}
#back{{position:fixed;top:0.5em;left:0.5em;background:rgba(0,0,0,0.6);color:#fff;border:1px solid #fff;border-radius:4px;padding:0.3em 0.8em;font-size:1.1em;text-decoration:none;z-index:999}}
</style>
<a id="back" href="{chapter.file_name}">&#8592; Back</a>
<img src="{img_folder}/full/{img_name}" alt="{img_name}"/>'''
                    )
                    book.add_item(viewer)
                    spine.append(viewer)
                a["href"] = vname
                changed = True
            if changed:
                chapter.content = str(soup).encode()

    # TOC
    book.toc = tuple(epub.Link(ch.file_name, ch.title, ch.file_name)
                     for ch in chapters)
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.spine = spine

    out_dir = pathlib.Path(opts.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(out_dir / f"{slugify(guide_title)}.epub")
    epub.write_epub(out_path, book)
    compress_epub(out_path)
    print(f"\nDone! → {out_path}")


# ── Legacy helpers (PDF / text modes unchanged) ───────────────────────────────

def remove_gamefaqs_chrome(driver):
    driver.execute_script(f"""
        var selectors = {repr(GAMEFAQS_REMOVE_SELECTORS)};
        selectors.forEach(function(sel) {{
            document.querySelectorAll(sel).forEach(function(el) {{
                el.parentNode && el.parentNode.removeChild(el);
            }});
        }});
    """)

def print_page(driver, opts, res_w, res_h):
    result = driver.execute_cdp_cmd('Page.printToPDF', {
        'paperWidth': res_w / 96, 'paperHeight': res_h / 96,
        'marginTop': 0, 'marginBottom': 0,
        'marginLeft': 0, 'marginRight': 0,
        'printBackground': True, 'preferCSSPageSize': False,
    })
    pdf_bytes = base64.b64decode(result['data'])
    out_dir   = pathlib.Path(opts.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / "manual.pdf"
    with open(pdf_path, 'wb') as f:
        f.write(pdf_bytes)
    print(f"Saved PDF to {pdf_path}")

def save_text(html_content, guide_id, opts):
    out_dir = pathlib.Path(opts.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    text_path = out_dir / f"{guide_id}.txt"
    plain = BeautifulSoup(html_content, 'html.parser').get_text('\n')
    plain = re.sub(r'\n{3,}', '\n\n', plain).strip()
    with open(text_path, 'w', encoding='utf-8') as f:
        f.write(plain)
    print(f"Saved text to {text_path}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def setup_parser():
    parser = OptionParser(usage="%prog [OPTIONS] URL", version="%prog 2.0")
    parser.set_description(
        "Converts an online GameFAQs guide into an EPUB, PDF, or text file."
    )
    # Original options
    parser.add_option("-s", "--size", dest="text_size", default="medium",
                      help="Font size: small, medium, large (PDF mode) [default: %default]")
    parser.add_option("-o", "--outputdir", dest="output_dir", default="output/",
                      help="Output directory [default: %default]", metavar="DIR")
    parser.add_option("-r", "--resolution", dest="resolution", default="960x544",
                      help="Browser / PDF page size WIDTHxHEIGHT [default: %default]")
    parser.add_option("--epub", action="store_true", dest="epub", default=False,
                      help="Output as EPUB (default output mode)")
    parser.add_option("--text", action="store_true", dest="text_only", default=False,
                      help="Output as plain text file")
    parser.add_option("--html", action="store_true", dest="html_mode", default=False,
                      help="PDF/EPUB with full page formatting (strip site chrome only)")
    # Image options
    parser.add_option("--img-size", dest="img_size", default="720p",
                      choices=list(RESOLUTIONS.keys()),
                      help="Full-size image resolution [default: %default]")
    parser.add_option("--thumb-size", dest="thumb_size", default="320p",
                      choices=list(THUMB_SIZES.keys()),
                      help="Inline thumbnail size [default: %default]")
    parser.add_option("--img-quality", dest="img_quality", default=75, type="int",
                      help="JPEG quality 1-95 [default: %default]")
    parser.add_option("--grayscale", action="store_true", dest="grayscale", default=False,
                      help="Convert images to grayscale (ideal for e-ink Kindle)")
    parser.add_option("--contrast", dest="contrast", default=1.0, type="float",
                      help="Image contrast multiplier (try 1.3-1.8 for e-ink) [default: %default]")
    parser.add_option("--no-images", action="store_true", dest="no_images", default=False,
                      help="Skip downloading images")
    parser.add_option("--headless", action="store_true", dest="headless", default=False,
                      help="Run browser in headless mode")
    return parser


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = setup_parser()
    (opts, args) = parser.parse_args()

    if not args:
        parser.print_help()
        sys.exit(0)

    url        = args[0]
    parsed_url = urllib.parse.urlparse(url)
    guide_id   = os.path.basename(parsed_url.path)
    res_w, res_h = map(int, opts.resolution.lower().split('x'))

    # Force single-page mode
    if "single=1" not in url:
        q = urllib.parse.parse_qs(parsed_url.query)
        q['single'] = ['1']
        url = urllib.parse.urlunparse(
            parsed_url._replace(query=urllib.parse.urlencode(q, doseq=True))
        )

    # Launch browser
    uc_options = uc.ChromeOptions()
    uc_options.add_argument('--no-sandbox')
    uc_options.add_argument('--disable-dev-shm-usage')
    uc_options.add_argument(f'--window-size={res_w},{res_h}')
    uc_options.add_argument('--kiosk-printing')
    if opts.headless:
        uc_options.add_argument('--headless=new')

    driver = uc.Chrome(options=uc_options, headless=False, version_main=145)
    driver.set_script_timeout(60)
    driver.set_page_load_timeout(60)

    try:
        driver.get(url)
        try:
            WebDriverWait(driver, 20).until(
                lambda d: d.find_elements(By.CSS_SELECTOR,
                    'div#faqwrap, div.ffaqbody, div#faq_body, div.faqtext, div#faq')
            )
        except Exception:
            pass

        # ── PDF mode (html_mode without --epub) ──
        if opts.html_mode and not opts.epub:
            remove_gamefaqs_chrome(driver)
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            input("Press Enter when the page is ready to print...")
            print_page(driver, opts, res_w, res_h)
            driver.quit()
            sys.exit(0)

        # ── Grab page content ──
        if opts.html_mode:
            remove_gamefaqs_chrome(driver)
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            page_source = driver.execute_script("return document.documentElement.outerHTML")
        else:
            page_source = driver.page_source

        driver.quit()
        driver = None

        soup     = BeautifulSoup(page_source, 'lxml')
        soup.encoding = 'utf-8'

        faq_body = (
            soup.find('div', id='faqwrap') or
            soup.find('div', class_='ffaqbody') or
            soup.find('div', id='faq_body') or
            soup.find('div', class_='faqtext') or
            soup.find('div', id='faq')
        )

        if faq_body is None:
            print("ERROR: Could not find FAQ content. GameFAQs may have changed their HTML.")
            print("div IDs on page:")
            for tag in soup.find_all('div', id=True):
                print(f"  id={tag['id']}")
            sys.exit(1)

        # Get guide title from page
        guide_title = (
            (soup.find('h1') or soup.find('title') or soup.find('h2'))
        )
        guide_title = guide_title.get_text(strip=True) if guide_title else guide_id
        # Strip site name suffix
        guide_title = re.sub(r'\s*[-|–]\s*GameFAQs.*$', '', guide_title,
                             flags=re.IGNORECASE).strip()
        if not guide_title:
            guide_title = guide_id

        # ── EPUB mode ──
        if opts.epub:
            max_size   = RESOLUTIONS[opts.img_size]
            thumb_size = THUMB_SIZES[opts.thumb_size]
            build_epub(faq_body, guide_title, guide_id, parsed_url,
                       opts, max_size, thumb_size)

        # ── Text mode ──
        elif opts.text_only:
            save_text(str(faq_body), guide_id, opts)

        # ── PDF mode (plain text rendered in browser) ──
        else:
            # Fix image URLs
            for img_tag in faq_body.find_all('img'):
                src = str(img_tag.attrs.get('src', ''))
                if src.endswith(".png") or src.endswith(".jpg"):
                    img_tag.attrs['src'] = f"https://{parsed_url.hostname}{src}"
                else:
                    img_tag.attrs['src'] = (
                        f"https://{parsed_url.hostname}/a/faqs/"
                        f"{guide_id[3:]}/{guide_id}-"
                        f"{os.path.basename(src.rstrip('/'))}.jpg"
                    )
                img_tag.attrs['width']  = "auto"
                img_tag.attrs['height'] = "260%"

            html_content = str(faq_body)
            html_content = re.sub(r'(<br\s*/?>\s*){2,}', '<br>',
                                  html_content, flags=re.IGNORECASE)

            font_sizes = {"small": "8px", "medium": "10px", "large": "13px"}
            font_size  = font_sizes.get(opts.text_size, "10px")
            font_style = (f'<style>body, div, pre, p, td, th '
                          f'{{ font-size: {font_size} !important; '
                          f'line-height: 1.3 !important; }}</style>')
            html_content = '<head>' + font_style + '</head>' + html_content

            with tempfile.NamedTemporaryFile('w', encoding='utf-8',
                                             suffix='.html', delete=False) as tf:
                tf.write(html_content)
                temp_path = tf.name

            driver = uc.Chrome(options=uc_options, headless=False, version_main=145)
            driver.get("file://" + temp_path)
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            input("Press Enter when the page is ready to print...")
            print_page(driver, opts, res_w, res_h)
            os.remove(temp_path)
            driver.quit()

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
