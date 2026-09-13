import concurrent.futures
from datetime import datetime, timezone, timedelta
import os
import re
import threading
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import cloudscraper
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# --- Configuration ---
BASE_URL = "https://prmovies.energy"
OUTPUT_FILE = "prmovies_playlist.m3u"
FIRST_RUN_PAGES = 50
INCREMENTAL_PAGES = 3
MAX_WORKERS = 8
REQUEST_TIMEOUT = 15

# Categories matching the site structure
CATEGORIES = [
    {"slug": "genre/hindi", "group_name": "Hindi Movies"},
    {"slug": "genre/hindi-dubbed", "group_name": "Hindi-Dubbed Movies"}
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

thread_local = threading.local()

def get_scraper():
    """Initializes a thread-safe Cloudscraper instance with automated retry logic."""
    if not hasattr(thread_local, "scraper"):
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        retries = Retry(
            total=3,
            backoff_factor=0.6,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retries)
        scraper.mount('http://', adapter)
        scraper.mount('https://', adapter)
        thread_local.scraper = scraper
    return thread_local.scraper

def get_resolution_score(text):
    """Scores resolution quality to select the highest quality link available."""
    match = re.search(r'(\d{3,4})p', text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if '4k' in text.lower() or '2160' in text:
        return 2160
    return 0

def unpack_packer(packed_js):
    """Simple unpacker for Dean Edwards p,a,c,k,e,d JavaScript blocks used by hosts."""
    match = re.search(r"}\s*\('(.*)',\s*(\d+),\s*(\d+),\s*'(.*?)'\.split\('\|'\)", packed_js, re.DOTALL)
    if not match:
        return ""
    payload, radix_str, count_str, symtab_str = match.groups()
    radix = int(radix_str)
    symtab = symtab_str.split('|')

    def unbase(val_str):
        digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        res = 0
        for char in val_str:
            res = res * radix + digits.index(char)
        return res

    def replace_token(m):
        token = m.group(0)
        idx = unbase(token)
        return symtab[idx] if idx < len(symtab) and symtab[idx] else token

    return re.sub(r'\b[0-9a-zA-Z]+\b', replace_token, payload)

def resolve_stream_source(host_url):
    """Navigates through speedostream / stream pages and extracts the direct media link."""
    scraper = get_scraper()
    try:
        resp = scraper.get(host_url, timeout=REQUEST_TIMEOUT, headers={"Referer": BASE_URL})
        if resp.status_code != 200:
            return None

        # Check for intermediate "Proceed to video" button shown in the video
        soup = BeautifulSoup(resp.text, 'html.parser')
        proceed_link = soup.find('a', string=re.compile(r'proceed to video', re.I))
        if proceed_link and proceed_link.get('href'):
            next_url = proceed_link['href']
            if not next_url.startswith('http'):
                next_url = urljoin(host_url, next_url)
            resp = scraper.get(next_url, timeout=REQUEST_TIMEOUT, headers={"Referer": host_url})
            soup = BeautifulSoup(resp.text, 'html.parser')

        content = resp.text

        # 1. Look for direct video tag
        video_tag = soup.find('video')
        if video_tag:
            src = video_tag.get('src')
            if src:
                return src
            source = video_tag.find('source')
            if source and source.get('src'):
                return source['src']

        # 2. Check for packed JS containing m3u8/mp4
        if 'eval(function(p,a,c,k,e,d)' in content:
            unpacked = unpack_packer(content)
            stream_match = re.search(r'["\'](https?://[^"\']+\.(?:m3u8|mp4)[^"\']*)["\']', unpacked)
            if stream_match:
                return stream_match.group(1)

        # 3. Direct regex match on page script sources
        stream_match = re.search(r'["\'](https?://[^"\']+\.(?:m3u8|mp4)[^"\']*)["\']', content)
        if stream_match:
            return stream_match.group(1)

    except Exception:
        pass
    return None

def process_movie_detail(detail_url, group_name):
    """Scrapes the movie detail page, identifies highest quality download row, and resolves stream."""
    scraper = get_scraper()
    try:
        res = scraper.get(detail_url, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, 'html.parser')

        # Extract Title
        title_tag = soup.find('h1') or soup.find('meta', property='og:title')
        if not title_tag:
            return None
        title = title_tag.get('content', '') if title_tag.name == 'meta' else title_tag.get_text(strip=True)
        clean_title = re.sub(r'[\r\n\t]+', ' ', title).strip()

        # Extract Poster
        poster_tag = soup.find('meta', property='og:image')
        poster = poster_tag['content'] if poster_tag and poster_tag.get('content') else ""

        # Parse Download / Server Table (as shown at 00:05-00:08 in the video)
        best_link = None
        best_score = -1

        rows = soup.find_all('tr')
        for row in rows:
            text = row.get_text()
            link_tag = row.find('a', href=True)
            if link_tag and ('speedostream' in link_tag['href'] or 'download' in text.lower() or 'quality' in text.lower() or 'p' in text.lower()):
                score = get_resolution_score(text)
                if score > best_score:
                    best_score = score
                    best_link = link_tag['href']

        # Fallback: inspect any speedostream button on page
        if not best_link:
            for a in soup.find_all('a', href=True):
                if 'speedostream' in a['href']:
                    best_link = a['href']
                    break

        if not best_link:
            return None

        # Resolve final playable video link from the host
        final_stream = resolve_stream_source(best_link)
        if not final_stream:
            # If JavaScript rendering prevents headless extraction, use the host link with player headers
            final_stream = best_link

        final_url = f"{final_stream}|Referer={BASE_URL}/&User-Agent={USER_AGENT}"
        extinf = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{group_name}", {clean_title}'
        
        # Unique identifier derived from movie slug
        slug = urlparse(detail_url).path.strip('/').split('/')[-1]
        return {
            "extinf": extinf,
            "url": final_url,
            "key": slug
        }
    except Exception:
        return None

def scan_catalog_page(category_slug, page_num):
    """Scrapes individual movie cards from the category index pages."""
    url = f"{BASE_URL}/{category_slug}/page/{page_num}/" if page_num > 1 else f"{BASE_URL}/{category_slug}/"
    scraper = get_scraper()
    movie_links = []
    try:
        resp = scraper.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Select cards linking to individual posts/movies
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Match movie detail permalinks
            if re.search(r'/(movie|movies|film)/[^/]+/?$', href) or (BASE_URL in href and a.find('img')):
                full_url = href if href.startswith('http') else urljoin(BASE_URL, href)
                if full_url != BASE_URL and full_url not in movie_links:
                    movie_links.append(full_url)

        return list(set(movie_links))
    except Exception:
        return []

def main():
    print("🚀 Starting Prmovies Scraping Job for GitHub Actions...")

    existing_keys = set()
    old_entries = []

    # Check if a playlist file already exists
    if os.path.exists(OUTPUT_FILE) and os.path.getsize(OUTPUT_FILE) > 0:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
            i = 0
            while i < len(lines):
                if lines[i].startswith("#EXTINF"):
                    extinf = lines[i]
                    if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                        url_line = lines[i + 1]
                        old_entries.append((extinf, url_line))
                        # Use title or url substring as key
                        key_match = re.search(r'group-title="[^"]*",\s*(.+)$', extinf)
                        if key_match:
                            existing_keys.add(key_match.group(1).strip().lower())
                        i += 2
                        continue
                i += 1
        pages_to_scan = INCREMENTAL_PAGES
        print(f"📁 Existing playlist detected ({len(old_entries)} items). Running in INCREMENTAL mode.")
    else:
        pages_to_scan = FIRST_RUN_PAGES
        print(f"📁 No playlist found. Running DEEP SCAN ({pages_to_scan} pages per category).")

    new_entries = []

    for cat in CATEGORIES:
        group_name = cat["group_name"]
        slug = cat["slug"]
        print(f"\n[+] Scanning Category: {group_name}")

        discovered_urls = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            page_tasks = {
                executor.submit(scan_catalog_page, slug, p): p 
                for p in range(1, pages_to_scan + 1)
            }
            for task in concurrent.futures.as_completed(page_tasks):
                for m_url in task.result():
                    discovered_urls.add(m_url)

        print(f"    Found {len(discovered_urls)} movie links. Extracting video streams...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            detail_tasks = {
                executor.submit(process_movie_detail, m_url, group_name): m_url 
                for m_url in discovered_urls
            }
            for task in concurrent.futures.as_completed(detail_tasks):
                data = task.result()
                if data and data["key"] not in existing_keys:
                    existing_keys.add(data["key"])
                    new_entries.append(data)
                    print(f"    ⚡ Added: {data['key'][:50]}")

    # Write out unified M3U8 file with Bangladesh Time stamp
    bd_time = datetime.now(timezone.utc) + timedelta(hours=6)
    timestamp = bd_time.strftime("%Y-%m-%d %I:%M:%S %p (BD Time)")

    print(f"\n💾 Writing updates to {OUTPUT_FILE} (+{len(new_entries)} new entries)...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write(f'# Playlist Generated from Prmovies\n')
        f.write(f'# Last Updated: {timestamp}\n\n')

        # Prepend new entries so latest releases appear first
        for entry in new_entries:
            f.write(f"{entry['extinf']}\n{entry['url']}\n")

        for extinf, url_line in old_entries:
            f.write(f"{extinf}\n{url_line}\n")

    print("🎉 Sync completed successfully!")

if __name__ == "__main__":
    main()

