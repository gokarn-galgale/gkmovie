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

PAGES_TO_SCAN = 15          # Scans 15 pages per category
MAX_WORKERS = 24            # Concurrency for movie processing

CATEGORIES = [
    {"group_name": "Hollywood Hindi Movies", "category_path": "/category/hollywood-hindi-movies/"},
    {"group_name": "Marathi Movies", "category_path": "/category/marathi-movies/"},
    {"group_name": "Bollywood Movies", "category_path": "/category/bollywood-movies-download/"},
    {"group_name": "South Dubbed Movies", "category_path": "/category/south-indian-hindi-dubbed-movies/"}
]

try:
    import lxml
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"

# --- Pre-compiled Regular Expressions ---
RE_FILE_KEY = re.compile(r'/([^/?#]+\.(?:mkv|mp4|m3u8))', re.IGNORECASE)
RE_MP_TITLE = re.compile(r'\bmp-title\b', re.IGNORECASE)
RE_MP_IMG_WRAP = re.compile(r'\bmp-img-wrap\b', re.IGNORECASE)
RE_POST_CONTAINER = re.compile(r'(post|item|movie|film|entry)', re.IGNORECASE)
RE_EXCLUDE_URLS = re.compile(r'/(category|tag|author|page)/', re.IGNORECASE)
RE_CLEAN_TITLE = re.compile(r'[-–—|:]\s*(?:India4Movies|Watch Online|Download)', re.IGNORECASE)
RE_WHITESPACE = re.compile(r'[\r\n\t]+')
RE_MULTICLOUD = re.compile(r'https?://[^\s"\'<>`]*multicloudlinks\.com[^\s"\'<>`]*', re.IGNORECASE)
RE_STREAM_HREF = re.compile(r'https?://[^\s"\'<>`]+(?:\.(?:mkv|mp4|m3u8)|multidownload\.[^\s"\'<>`]+|fastdl\.[^\s"\'<>`]+)', re.IGNORECASE)
RE_URL_PARAM = re.compile(r'url=([^&]+)')
RE_SRCSET_URLS = re.compile(r'(https?://[^\s,]+|//[^\s,]+)')
RE_STYLE_BG = re.compile(r'url\([\'"]?(https?://[^\'"]*\vert{}//[^\'"]*)[\'"]?\)', re.IGNORECASE)

thread_local = threading.local()

def get_scraper():
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

def extract_movie_title(soup):
    mp_elem = soup.find(class_=RE_MP_TITLE)
    if mp_elem:
        raw_text = mp_elem.get_text(strip=True)
        if raw_text:
            return RE_WHITESPACE.sub(' ', raw_text).strip()

    h1 = soup.find('h1')
    if h1 and h1.get_text(strip=True):
        return RE_WHITESPACE.sub(' ', h1.get_text(strip=True)).strip()

    title_tag = soup.find('title')
    if title_tag:
        raw = title_tag.get_text(strip=True)
        cleaned = RE_CLEAN_TITLE.split(raw)[0]
        return cleaned.strip()

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

def extract_poster_from_mp_img_wrap(soup, page_url):
    wrap = soup.find(class_=RE_MP_IMG_WRAP)
    img = None
    if wrap:
        img = wrap if wrap.name == 'img' else wrap.find('img')

    if img:
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-orig-file', 'data-original'):
            val = img.get(attr)
            if val and not str(val).startswith('data:image'):
                return normalize_image_url(str(val), page_url)

        srcset = img.get('srcset') or img.get('data-srcset')
        if srcset:
            urls = RE_SRCSET_URLS.findall(srcset)
            if urls:
                return normalize_image_url(urls[-1], page_url)

    if wrap and wrap.get('style'):
        style_match = RE_STYLE_BG.search(wrap['style'])
        if style_match:
            return normalize_image_url(style_match.group(1), page_url)

    for fallback_img in soup.find_all('img'):
        for attr in ('src', 'data-src', 'data-lazy-src'):
            val = fallback_img.get(attr)
            if val and 'image.india4movies.net' in str(val).lower():
                return normalize_image_url(str(val), page_url)

    return ""

def extract_direct_stream_link(html_text, page_url):
    """Accurately extracts multidownload / direct streaming links."""
    # 1. Check for multidownload or direct video URLs via regex
    matches = RE_STREAM_HREF.findall(html_text)
    if matches:
        target = matches[0].rstrip('\\";),')
        if 'url=' in target:
            param = RE_URL_PARAM.search(target)
            if param:
                return unquote(param.group(1))
        return target

    # 2. Comprehensive DOM search on all standard elements & attributes
    soup = BeautifulSoup(html_text, HTML_PARSER)
    for a in soup.find_all(['a', 'link', 'source', 'iframe'], href=True):
        href = a.get('href') or a.get('src')
        if href and ('multidownload.' in href.lower() or any(ext in href.lower() for ext in ('.mkv', '.mp4', '.m3u8'))):
            return urljoin(page_url, href.strip())

    for tag in soup.find_all(attrs=True):
        for attr in ('data-url', 'data-link', 'data-src', 'data-clipboard-text', 'value'):
            val = tag.attrs.get(attr)
            if isinstance(val, str) and ('multidownload.' in val.lower() or any(ext in val.lower() for ext in ('.mkv', '.mp4', '.m3u8'))):
                return urljoin(page_url, val.strip())

    return None

def process_movie(post_url, group_name):
    scraper = get_scraper()
    try:
        res = scraper.get(post_url, timeout=(5, 10))
        if res.status_code != 200:
            return None

        # Extract multicloud link
        multicloud_matches = RE_MULTICLOUD.findall(res.text)
        multicloud_url = multicloud_matches[0].rstrip('\\";),') if multicloud_matches else None

        soup = BeautifulSoup(res.text, HTML_PARSER)
        movie_name = extract_movie_title(soup)
        poster = extract_poster_from_mp_img_wrap(soup, post_url)

        if not multicloud_url:
            for el in soup.find_all(['a', 'iframe'], src=True) + soup.find_all('a', href=True):
                target = el.get('href') or el.get('src')
                if target and 'multicloudlinks.com' in target.lower():
                    multicloud_url = target.strip()
                    break

        if not multicloud_url:
            return None

        # Request intermediate host
        cloud_res = scraper.get(multicloud_url, headers={"Referer": post_url}, timeout=(5, 10))
        if cloud_res.status_code != 200:
            return None

        stream_link = extract_direct_stream_link(cloud_res.text, multicloud_url)
        if not stream_link:
            return None

        clean_file_key = extract_file_key(stream_link)
        
        # Ensure #EXTINF and URL are combined into a guaranteed valid 2-line entry
        m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{group_name}", {movie_name}\n{stream_link}\n'
        return m3u_entry, get_domain(stream_link), clean_file_key, movie_name

    except Exception:
        return None

def scan_single_page(category_path, page_num):
    url = urljoin(BASE_URL, category_path) if page_num == 1 else urljoin(BASE_URL, f"{category_path.rstrip('/')}/page/{page_num}/")
    scraper = get_scraper()
    found_urls = set()

    try:
        response = scraper.get(url, timeout=(5, 10))
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, HTML_PARSER)
        posts = soup.find_all(['article', 'div'], class_=RE_POST_CONTAINER)
        if not posts:
            posts = [soup]

        for container in posts:
            for a in container.find_all('a', href=True):
                href = a['href']
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

def load_existing_playlist(filepath):
    """Reliably parses existing M3U file preserving complete header-URL pairs."""
    existing_keys = set()
    pairs = []
    old_domain = None

    if not (os.path.exists(filepath) and os.path.getsize(filepath) > 0):
        return existing_keys, pairs, old_domain

    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        lines = [line.strip() for line in f if line.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXTINF'):
            if i + 1 < len(lines) and not lines[i + 1].startswith('#'):
                stream_url = lines[i + 1]
                key = extract_file_key(stream_url)
                if key:
                    existing_keys.add(key)
                if not old_domain and 'http' in stream_url:
                    old_domain = get_domain(stream_url)
                pairs.append(f"{line}\n{stream_url}\n")
                i += 2
                continue
        i += 1

    return existing_keys, pairs, old_domain

def main():
    print(f"🚀 Starting Scraper (Workers: {MAX_WORKERS}, Pages/Cat: {PAGES_TO_SCAN})...", flush=True)

    existing_file_keys, old_entries, old_domain = load_existing_playlist(OUTPUT_FILE)
    print(f"📁 Loaded existing playlist: {len(old_entries)} valid movie entries.", flush=True)

    all_new_entries = []
    active_domain = None
    lock = threading.Lock()
    discovered_urls = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as movie_executor:
        movie_futures = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as page_executor:
            page_tasks = {
                page_executor.submit(scan_single_page, cat["category_path"], p): cat["group_name"]
                for cat in CATEGORIES
                for p in range(1, PAGES_TO_SCAN + 1)
            }

            for page_fut in concurrent.futures.as_completed(page_tasks):
                group_name = page_tasks[page_fut]
                try:
                    urls = page_fut.result()
                    for u in urls:
                        if u not in discovered_urls:
                            discovered_urls.add(u)
                            mf = movie_executor.submit(process_movie, u, group_name)
                            movie_futures.append(mf)
                except Exception:
                    pass

        print(f"\n⚡ Discovered {len(discovered_urls)} candidates. Processing streams...", flush=True)

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

    # Update domains if server changed
    if old_domain and active_domain and old_domain != active_domain:
        print(f"\n🔄 Domain update: {old_domain} -> {active_domain}", flush=True)
        old_entries = [entry.replace(old_domain, active_domain) for entry in old_entries]

    # Write Complete Valid M3U Playlist
    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    print(f"\n💾 Writing to {OUTPUT_FILE} (+{len(all_new_entries)} total new entries added)...", flush=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write('# Playlist Generated Automatically (Hollywood, Marathi, Bollywood, South Dubbed)\n')
        f.write(f'# Last Updated: {now}\n\n')

        for entry in all_new_entries:
            f.write(entry)

        for entry in old_entries:
            f.write(entry)

    print(f"🎉 Complete! Updated {OUTPUT_FILE} successfully.", flush=True)

if __name__ == "__main__":
    main()
