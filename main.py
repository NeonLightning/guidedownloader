import os, sys, pathlib
import tempfile
import re
import time
import zipfile
from optparse import OptionParser
import base64
import urllib.parse
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
import undetected_chromedriver as uc

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

def remove_gamefaqs_chrome():
	"""Remove GameFAQs headers, footers, ads and nav via JavaScript."""
	selectors_js = ', '.join(GAMEFAQS_REMOVE_SELECTORS)
	driver.execute_script(f"""
		var selectors = {repr(GAMEFAQS_REMOVE_SELECTORS)};
		selectors.forEach(function(sel) {{
			document.querySelectorAll(sel).forEach(function(el) {{
				el.parentNode && el.parentNode.removeChild(el);
			}});
		}});
	""")

def print_page():
	global driver, parser_options

	result = driver.execute_cdp_cmd('Page.printToPDF', {
		'paperWidth': res_w / 96,
		'paperHeight': res_h / 96,
		'marginTop': 0,
		'marginBottom': 0,
		'marginLeft': 0,
		'marginRight': 0,
		'printBackground': True,
		'preferCSSPageSize': False,
	})
	pdf_bytes = base64.b64decode(result['data'])

	output_dir = pathlib.Path(parser_options.output_dir)
	output_dir.mkdir(parents=True, exist_ok=True)
	pdf_path = output_dir / "manual.pdf"
	with open(pdf_path, 'wb') as f:
		f.write(pdf_bytes)
	print(f"Saved PDF to {pdf_path}")

def save_epub(html_content, guide_id):
	output_dir = pathlib.Path(parser_options.output_dir)
	output_dir.mkdir(parents=True, exist_ok=True)
	epub_path = output_dir / "manual.epub"

	opf = '''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
	<dc:title>{title}</dc:title>
	<dc:language>en</dc:language>
	<dc:identifier id="uid">{guide_id}</dc:identifier>
  </metadata>
  <manifest>
	<item id="content" href="content.html" media-type="application/xhtml+xml"/>
	<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx">
	<itemref idref="content"/>
  </spine>
</package>'''.format(title=guide_id, guide_id=guide_id)

	ncx = '''<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="{guide_id}"/></head>
  <docTitle><text>{title}</text></docTitle>
  <navMap>
	<navPoint id="np1" playOrder="1">
	  <navLabel><text>Start</text></navLabel>
	  <content src="content.html"/>
	</navPoint>
  </navMap>
</ncx>'''.format(title=guide_id, guide_id=guide_id)

	xhtml = '''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
  <style>pre, code {{ white-space: pre-wrap; font-family: monospace; }}</style>
</head>
<body>{content}</body>
</html>'''.format(content=html_content)

	with zipfile.ZipFile(epub_path, 'w', zipfile.ZIP_DEFLATED) as zf:
		zf.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip')
		zf.writestr('META-INF/container.xml', '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
	<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>''')
		zf.writestr('OEBPS/content.opf', opf)
		zf.writestr('OEBPS/toc.ncx', ncx)
		zf.writestr('OEBPS/content.html', xhtml)

	print(f"Saved EPUB to {epub_path}")

def save_text(html_content, guide_id):
	output_dir = pathlib.Path(parser_options.output_dir)
	output_dir.mkdir(parents=True, exist_ok=True)
	text_path = output_dir / f"{guide_id}.txt"
	plain = BeautifulSoup(html_content, 'html.parser').get_text('\n')
	plain = re.sub(r'\n{3,}', '\n\n', plain).strip()
	with open(text_path, 'w', encoding='utf-8') as f:
		f.write(plain)
	print(f"Saved text to {text_path}")

def setup_parser():
	parser = OptionParser(usage="%prog [OPTIONS] URL", version="%prog 1.0")
	parser.set_description("Converts an online GameFAQs guide into a PDF, EPUB, or text file.")
	parser.add_option("-s", "--size", dest="text_size", default="medium",
					  help="Font size: small, medium or large (plain text mode only) [default: %default]")
	parser.add_option("-o", "--outputdir", dest="output_dir",
					  help="Output file to DIR", metavar="DIR")
	parser.add_option("-r", "--resolution", dest="resolution", default="960x544",
					  help="Browser window size as WIDTHxHEIGHT, also sets PDF page size [default: %default]")
	parser.add_option("--epub", action="store_true", dest="epub", default=False,
					  help="Output as EPUB instead of PDF")
	parser.add_option("--text", action="store_true", dest="text_only", default=False,
					  help="Output as plain text file")
	parser.add_option("--html", action="store_true", dest="html_mode", default=False,
					  help="Keep full page formatting and images, just strip GameFAQs site chrome. "
						   "Honors --epub or defaults to PDF.")
	parser.set_defaults(output_dir="output/", formatted=False)
	return parser

driver = None
parser_options = None
res_w, res_h = 960, 544

if __name__ == "__main__":
	parser = setup_parser()
	(parser_options, args) = parser.parse_args()

	res_w, res_h = map(int, parser_options.resolution.lower().split('x'))

	if len(args) == 0:
		parser.print_help()
		exit()

	url = args[0]

	parsed_url = urllib.parse.urlparse(url)
	guide_id = os.path.basename(parsed_url.path)

	if "single=1" not in url:
		parsed_query = urllib.parse.parse_qs(parsed_url.query)
		if "single" not in parsed_query:
			parsed_query['single'] = ['1']
		else:
			if '1' not in parsed_query['single']:
				parsed_query['single'] = ['1']
		new_query = urllib.parse.urlencode(parsed_query, doseq=1)
		url = urllib.parse.urlunparse([new_query if i == 4 else x for i, x in enumerate(parsed_url)])

	uc_options = uc.ChromeOptions()
	uc_options.add_argument('--no-sandbox')
	uc_options.add_argument('--disable-dev-shm-usage')
	uc_options.add_argument(f'--window-size={res_w},{res_h}')
	uc_options.add_argument('--kiosk-printing')

	driver = uc.Chrome(options=uc_options, headless=False, version_main=145)

	driver.set_script_timeout(60)
	driver.set_page_load_timeout(60)

	driver.get(url)

	try:
		WebDriverWait(driver, 20).until(
			lambda d: d.find_elements(By.CSS_SELECTOR,
				'div#faqwrap, div.ffaqbody, div#faq_body, div.faqtext, div#faq')
		)
	except Exception:
		pass

	if parser_options.html_mode:
		remove_gamefaqs_chrome()

		WebDriverWait(driver, 30).until(
			lambda d: d.execute_script("return document.readyState") == "complete"
		)

		if parser_options.epub:
			cleaned_html = driver.execute_script("return document.documentElement.outerHTML")
			driver.quit()
			soup = BeautifulSoup(cleaned_html, 'html.parser')
			faq_body = (
				soup.find('div', id='faqwrap') or
				soup.find('div', class_='ffaqbody') or
				soup.find('div', id='faq_body') or
				soup.find('div', class_='faqtext') or
				soup.find('div', id='faq') or
				soup.find('body')
			)
			save_epub(str(faq_body), guide_id)
		else:
			input("Press Enter when the page is ready to print...")
			print_page()
			driver.quit()

	else:
		soup = BeautifulSoup(driver.page_source, features="html5lib")

		faq_body = (
			soup.find('div', id='faqwrap') or
			soup.find('div', class_='ffaqbody') or
			soup.find('div', id='faq_body') or
			soup.find('div', class_='faqtext') or
			soup.find('div', id='faq')
		)

		if faq_body is None:
			print("ERROR: Could not find FAQ content on the page. GameFAQs may have changed their HTML structure.")
			print("Dumping all div IDs and classes found on the page to help diagnose:")
			for tag in soup.find_all('div', id=True):
				print(f"  id={tag['id']}")
			for tag in soup.find_all('div', class_=True):
				print(f"  class={tag['class']}")
			driver.quit()
			sys.exit(1)

		for img_tag in faq_body.find_all('img'):
			source = str(img_tag.attrs['src'])
			if source.endswith(".png") or source.endswith(".jpg"):
				img_tag.attrs['src'] = 'https://' + parsed_url.hostname + source
			else:
				img_tag.attrs['src'] = 'https://' + parsed_url.hostname + '/a/faqs/' + guide_id[3:] + '/' + guide_id \
								+ '-' + os.path.basename(source[:-1]) + '.jpg'
			img_tag.attrs['width'] = "auto"
			img_tag.attrs['height'] = "260%"

		html_content = str(faq_body)
		html_content = re.sub(r'(<br\s*/?>\s*){2,}', '<br>', html_content, flags=re.IGNORECASE)

		if parser_options.epub:
			driver.quit()
			save_epub(html_content, guide_id)
		elif parser_options.text_only:
			driver.quit()
			save_text(html_content, guide_id)
		else:
			font_sizes = {"small": "8px", "medium": "10px", "large": "13px"}
			font_size = font_sizes.get(parser_options.text_size, "10px")
			font_style = f'<style>body, div, pre, p, td, th {{ font-size: {font_size} !important; line-height: 1.3 !important; }}</style>'
			html_content = '<head>' + font_style + '</head>' + html_content

			with tempfile.NamedTemporaryFile('w', encoding="utf-8", suffix=".html", delete=False) as temp_html_file:
				temp_html_file.write(html_content)
				temp_html_file.flush()

			local_url = "file://" + temp_html_file.name
			driver.get(local_url)

			WebDriverWait(driver, 30).until(
				lambda d: d.execute_script("return document.readyState") == "complete"
			)
			input("Press Enter when the page is ready to print...")

			print_page()

			os.remove(temp_html_file.name)
			driver.quit()