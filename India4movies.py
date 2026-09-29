import concurrent.futures
from datetime import datetime, timezone, timedelta
import os
import re
import threading
from urllib.parse import unquote, urlparse, urljoin
from bs4 import BeautifulSoup
import cloudscraper
from requests.adapters import HTTPAdapter

# --- Configuration ---
BASE_URL = "https://go4.india4movies.net"
OUTPUT_FILE = "all_movies.m3u"
PAGES_PER_CATEGORY = 15
MAX_WORKERS = 32

CATEGORIES = [
    {"group_name": "Hollywood Hindi Movies", "category_path": "/category/hollywood-hindi-movies/"},
    {"group_name": "Marathi Movies", "category_path": "/category/marathi-movies/"},
    {"group_name": "Bollywood Movies", "category_path": "/category/bollywood-movies-download/"},
    {"group_name": "South Dubbed Movies", "category_path": "/category/south-indian-hindi-dubbed-movies/"}
]

# Fast parser selection
try:
    import lxml
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"

# Pre-compiled Regular Expressions
RE_FILE_KEY = re.compile(r'/([^/?#]+\.(?:mkv|mp4|m3u8))', re.IGNORECASE)
RE_MULTICLOUD = re.compile(r'https?://[^\s"\'<>`]*multicloudlinks\.com[^\s"\'<>`]*', re.IGNORECASE)
RE_MULTIDOWNLOAD = re.compile(r'https?://[^\s"\'<>`]+multidownload\.[^\s"\'<>`]+', re.IGNORECASE)
RE_POST_LINK = re.compile(r'href=["\'](https?://[^"\'>]+|/[^"\'>]+)["\']', re.IGNORECASE)
RE_MP_TITLE = re.compile(r'class=["\'][^"\']*\bmp-title\b[^"\']*["\'][^>]*>([^<]+)<', re.IGNORECASE)
RE_H1_TITLE = re.compile(r'<h1[^>]*>([^<]+)</h1>', re.IGNORECASE)
RE_PAGE_TITLE = re.compile(r'<title>([^<]+)</title>', re.IGNORECASE)
RE_TITLE_CLEANUP = re.compile(r'[-–—|:]\s*(?:India4Movies|Watch Online|Download)', re.IGNORECASE)
RE_WHITESPACE = re.compile(r'[\r\n\t]+')
RE_MP_IMG = re.compile(r'class=["\'][^"\']*\bmp-img-wrap\b[^"\']*.*?<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE | re.DOTALL)
RE_IMG_FALLBACK = re.compile(r'src=["\'](https?://image\.india4movies\.net/[^"\']+)["\']', re.IGNORECASE)
RE_EXCLUDE_URLS = re.compile(r'/(category|tag|author|page)/', re.IGNORECASE)

thread_local = threading.local()

def get_scraper():
    """Provides a thread-local scraper with an optimized connection pool."""
    if not hasattr(thread_local, "scraper"):
        session = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=1)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        thread_local.scraper = session
    return thread_local.scraper

def get_domain(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"

def extract_file_key(url_or_line):
    clean = url_or_line.split('|')[0].strip()
    match = RE_FILE_KEY.search(clean)
    if match:
        return match.group(1).lower()
    return clean.rstrip('/').split('/')[-1].split('?')[0].lower()

def extract_movie_title_fast(html_text, soup=None):
    # Regex fast path
    m = RE_MP_TITLE.search(html_text) or RE_H1_TITLE.search(html_text)
    if m:
        return RE_WHITESPACE.sub(' ', m.group(1)).strip()

    m = RE_PAGE_TITLE.search(html_text)
    if m:
        cleaned = RE_TITLE_CLEANUP.split(m.group(1))[0]
        return RE_WHITESPACE.sub(' ', cleaned).strip()

    # DOM Fallback
    if soup:
        mp_elem = soup.find(class_=re.compile(r'\bmp-title\b', re.IGNORECASE))
        if mp_elem and mp_elem.get_text(strip=True):
            return RE_WHITESPACE.sub(' ', mp_elem.get_text(strip=True)).strip()

    return "Unknown Movie"

def normalize_image_url(url, base_url):
    if not url:
        return ""
    clean = url.strip()
    if clean.startswith("//"):
        return f"https:{clean}"
    if clean.startswith("http://"):
        return clean.replace("http://", "https://", 1)
    if clean.startswith("https://"):
        return clean
    return urljoin(base_url, clean)

def extract_poster_fast(html_text, page_url, soup=None):
    # Regex fast path
    m = RE_MP_IMG.search(html_text) or RE_IMG_FALLBACK.search(html_text)
    if m:
        return normalize_image_url(m.group(1), page_url)

    # DOM Fallback
    if soup:
        wrap = soup.find(class_=re.compile(r'\bmp-img-wrap\b', re.IGNORECASE))
        if wrap:
            img = wrap if wrap.name == 'img' else wrap.find('img')
            if img:
                for attr in ['src', 'data-src', 'data-lazy-src', 'data-orig-file', 'data-original']:
                    val = img.get(attr)
                    if val and not str(val).startswith('data:image'):
                        return normalize_image_url(str(val), page_url)

    return ""

def fast_extract_multidownload_link(html_text, page_url):
    m = RE_MULTIDOWNLOAD.search(html_text)
    if m:
        link = m.group(0).rstrip('\\";),')
        if 'url=' in link:
            param = re.search(r'url=([^&]+)', link)
            if param:
                decoded = unquote(param.group(1))
                if 'multidownload.' in decoded.lower():
                    return decoded
        return link

    # Light fallback check
    soup = BeautifulSoup(html_text, HTML_PARSER)
    for a in soup.find_all(['a', 'link'], href=True):
        if 'multidownload.' in a['href'].lower():
            return urljoin(page_url, a['href'].strip())

    return None

def process_movie(post_url, group_name):
    scraper = get_scraper()
    try:
        res = scraper.get(post_url, timeout=(3.5, 6.0))
        if res.status_code != 200:
            return None

        html = res.text

        # 1. Resolve multicloud URL
        multicloud_matches = RE_MULTICLOUD.findall(html)
        if multicloud_matches:
            multicloud_url = multicloud_matches[0].rstrip('\\";),')
        else:
            soup = BeautifulSoup(html, HTML_PARSER)
            found = False
            for el in soup.find_all(['a', 'iframe'], src=True) + soup.find_all('a', href=True):
                target = el.get('href') or el.get('src')
                if target and 'multicloudlinks.com' in target.lower():
                    multicloud_url = target.strip()
                    found = True
                    break
            if not found:
                return None

        # 2. Extract metadata
        movie_name = extract_movie_title_fast(html)
        poster = extract_poster_fast(html, post_url)

        # 3. Resolve direct download link
        cloud_res = scraper.get(multicloud_url, headers={"Referer": post_url}, timeout=(3.5, 6.0))
        if cloud_res.status_code != 200:
            return None

        stream_link = fast_extract_multidownload_link(cloud_res.text, multicloud_url)
        if not stream_link:
            return None

        clean_file_key = extract_file_key(stream_link)
        m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{group_name}", {movie_name}\n{stream_link}\n'
        return m3u_entry, get_domain(stream_link), clean_file_key, movie_name

    except Exception:
        return None

def scan_single_page(category_path, page_num):
    url = urljoin(BASE_URL, category_path) if page_num == 1 else urljoin(BASE_URL, f"{category_path.rstrip('/')}/page/{page_num}/")
    scraper = get_scraper()
    found_urls = set()

    try:
        response = scraper.get(url, timeout=(3.5, 6.0))
        if response.status_code != 200:
            return []

        # Regex scan on hrefs avoids building large DOMs for listing pages
        raw_hrefs = RE_POST_LINK.findall(response.text)
        for href in raw_hrefs:
            full_url = urljoin(BASE_URL, href)
            if (
                full_url.startswith(BASE_URL)
                and not RE_EXCLUDE_URLS.search(full_url)
                and full_url.rstrip('/') != BASE_URL.rstrip('/')
            ):
                found_urls.add(full_url)

        return list(found_urls)
    except Exception:
        return []

def main():
    print(f"🚀 Starting Scraper (Workers: {MAX_WORKERS}, Pages/Cat: {PAGES_PER_CATEGORY})...", flush=True)

    existing_file_keys = set()
    old_entries = []
    old_domain = None

    if os.path.exists(OUTPUT_FILE) and os.path.getsize(OUTPUT_FILE) > 0:
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.startswith('#'):
                    key = extract_file_key(line)
                    if key:
                        existing_file_keys.add(key)
                    if 'multidownload.' in line and not old_domain:
                        old_domain = get_domain(line.split('|')[0].strip())
                else:
                    if not line.startswith(('#EXTM3U', '# Playlist', '# Last')):
                        old_entries.append(line)
        print(f"📁 Loaded {len(existing_file_keys)} existing tracks from {OUTPUT_FILE}.", flush=True)

    all_new_entries = []
    active_domain = None
    lock = threading.Lock()
    discovered_urls = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        movie_futures = []

        # Launch scans across all 4 categories (15 pages each = 60 total page tasks)
        page_tasks = {
            executor.submit(scan_single_page, cat["category_path"], p): cat["group_name"]
            for cat in CATEGORIES
            for p in range(1, PAGES_PER_CATEGORY + 1)
        }

        for page_fut in concurrent.futures.as_completed(page_tasks):
            group_name = page_tasks[page_fut]
            try:
                urls = page_fut.result()
                for u in urls:
                    if u not in discovered_urls:
                        discovered_urls.add(u)
                        movie_futures.append(executor.submit(process_movie, u, group_name))
            except Exception:
                pass

        print(f"⚡ Discovered {len(discovered_urls)} unique movie candidates. Resolving streams...", flush=True)

        for future in concurrent.futures.as_completed(movie_futures):
            try:
                result = future.result()
                if result:
                    entry, domain, file_key, movie_name = result
                    with lock:
                        if file_key not in existing_file_keys:
                            existing_file_keys.add(file_key)
                            all_new_entries.append(entry)
                            print(f"   ⚡ Added: {movie_name[:45]}...", flush=True)
                            if not active_domain:
                                active_domain = domain
            except Exception:
                pass

    # Update domains if changed
    if old_domain and active_domain and old_domain != active_domain:
        print(f"🔄 Updating domain: {old_domain} -> {active_domain}", flush=True)
        old_entries = [line.replace(old_domain, active_domain) for line in old_entries]

    # Write final M3U playlist
    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    print(f"\n💾 Writing {len(all_new_entries)} new entries to {OUTPUT_FILE}...", flush=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write('# Playlist Generated Automatically (Hollywood, Marathi, Bollywood, South Dubbed)\n')
        f.write(f'# Last Updated: {now}\n\n')
        for entry in all_new_entries:
            f.write(entry)
        for line in old_entries:
            f.write(line)

    print(f"🎉 Complete! Updated {OUTPUT_FILE} successfully.", flush=True)

if __name__ == "__main__":
    main()
