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
PLAYLIST_NAME = "prmovies.energy"
OUTPUT_FILE = "prmovies_playlist.m3u"
PAGES_TO_SCAN = 3
MAX_WORKERS = 4
REQUEST_TIMEOUT = 20

# Target category path
CATEGORIES = [
    {"path": "bollywood-movies-on-prmovies", "group_name": "Bollywood Movies"}
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

thread_local = threading.local()

def get_scraper():
    if not hasattr(thread_local, "scraper"):
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retries)
        scraper.mount('http://', adapter)
        scraper.mount('https://', adapter)
        thread_local.scraper = scraper
    return thread_local.scraper

def unpack_packer(packed_js):
    """Deobfuscates Dean Edwards p,a,c,k,e,d JavaScript blocks."""
    match = re.search(r"}\s*\('(.*)',\s*(\d+),\s*(\d+),\s*'(.*?)'\.split\('\|'\)", packed_js, re.DOTALL)
    if not match:
        return ""
    payload, radix_str, _, symtab_str = match.groups()
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

def extract_jwplayer_stream(html_content):
    """Extracts direct m3u8 or mp4 stream URLs from JW Player configurations."""
    # 1. Look for packed JavaScript blocks
    if 'eval(function(p,a,c,k,e,d)' in html_content:
        for block in re.findall(r"eval\(function\(p,a,c,k,e,d\).*?\.split\('\|'\)\)\)", html_content, re.DOTALL):
            unpacked = unpack_packer(block)
            match = re.search(r'["\'](https?://[^"\']+\.(?:m3u8|mp4)[^"\']*)["\']', unpacked)
            if match:
                return match.group(1)

    # 2. Look for jwplayer setup block
    jw_match = re.search(r'jwplayer\([^)]*\)\.setup\(\s*\{.*?\}\s*\);', html_content, re.DOTALL)
    search_scope = jw_match.group(0) if jw_match else html_content

    # Match 'file' or 'source' parameters
    stream_match = re.search(r'(?:file|source)\s*:\s*["\'](https?://[^"\']+\.(?:m3u8|mp4)[^"\']*)["\']', search_scope, re.I)
    if stream_match:
        return stream_match.group(1)

    # 3. Direct regex match on any playable stream URL in the DOM
    fallback = re.search(r'["\'](https?://[^"\']+\.(?:m3u8|mp4)[^"\']*)["\']', html_content)
    if fallback:
        return fallback.group(1)

    return None

def resolve_speedostream(speedo_url):
    """Handles the first 'Proceed to video' gate and parses JW Player on the destination page."""
    scraper = get_scraper()
    try:
        # Step 1: Open first speedostream page
        res = scraper.get(speedo_url, headers={"User-Agent": USER_AGENT, "Referer": BASE_URL}, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, 'html.parser')
        second_page_url = None
        post_data = {}

        # Check for form submission or anchor button for "Proceed to video"
        form = soup.find('form')
        proceed_btn = soup.find(lambda tag: tag.name in ['a', 'button'] and 'proceed' in tag.get_text().lower())

        if form:
            action = form.get('action') or speedo_url
            second_page_url = action if action.startswith('http') else urljoin(speedo_url, action)
            for inp in form.find_all('input'):
                if inp.get('name'):
                    post_data[inp.get('name')] = inp.get('value', '')
        elif proceed_btn and proceed_btn.name == 'a' and proceed_btn.get('href'):
            href = proceed_btn['href']
            second_page_url = href if href.startswith('http') else urljoin(speedo_url, href)

        # Step 2: Request the second page (player page)
        if second_page_url:
            if post_data:
                player_res = scraper.post(second_page_url, data=post_data, headers={"User-Agent": USER_AGENT, "Referer": speedo_url}, timeout=REQUEST_TIMEOUT)
            else:
                player_res = scraper.get(second_page_url, headers={"User-Agent": USER_AGENT, "Referer": speedo_url}, timeout=REQUEST_TIMEOUT)
            content = player_res.text
            final_referer = second_page_url
        else:
            content = res.text
            final_referer = speedo_url

        # Step 3: Extract the stream URL from JW Player
        stream_url = extract_jwplayer_stream(content)
        if stream_url:
            return f"{stream_url}|Referer={final_referer}&User-Agent={USER_AGENT}"

    except Exception:
        pass
    return None

def process_movie(movie_url, group_name):
    """Visits movie details page, finds speedostream link in download table, and resolves video."""
    scraper = get_scraper()
    try:
        res = scraper.get(movie_url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, 'html.parser')

        # Movie Title & Poster
        title_tag = soup.find('h1', class_='entry-title') or soup.find('h1')
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Movie"
        
        poster_tag = soup.find('meta', property='og:image')
        poster = poster_tag['content'] if poster_tag and poster_tag.get('content') else ""

        # Find speedostream1 link in the Download Section/Table
        speedo_link = None
        for a in soup.find_all('a', href=True):
            if 'speedostream' in a['href']:
                speedo_link = a['href']
                break

        if not speedo_link:
            return None

        playable_stream = resolve_speedostream(speedo_link)
        if not playable_stream:
            return None

        extinf = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{group_name}", {title}'
        return f"{extinf}\n{playable_stream}\n"

    except Exception:
        return None

def scan_category(path, page_num):
    """Fetches movie cards from the given category page."""
    url = f"{BASE_URL}/{path}/page/{page_num}/" if page_num > 1 else f"{BASE_URL}/{path}/"
    scraper = get_scraper()
    movie_links = []
    try:
        res = scraper.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200:
            return []

        soup = BeautifulSoup(res.text, 'html.parser')
        
        # Matches movie cards inside DooPlay/WordPress movie wrappers
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Target movie post URLs while filtering out pagination/tags
            if f"{BASE_URL}/movies/" in href or f"{BASE_URL}/movie/" in href:
                if href not in movie_links:
                    movie_links.append(href)

        # Fallback card selection
        if not movie_links:
            for div in soup.find_all('div', class_=re.compile(r'poster|item')):
                a_tag = div.find('a', href=True)
                if a_tag and a_tag['href'].startswith(BASE_URL) and a_tag['href'] not in movie_links:
                    movie_links.append(a_tag['href'])

        return list(set(movie_links))
    except Exception:
        return []

def main():
    print(f"🚀 Starting scraper for {PLAYLIST_NAME}...")
    new_entries = []

    for cat in CATEGORIES:
        group_name = cat["group_name"]
        path = cat["path"]
        print(f"Scanning category: {group_name}...")

        all_movie_urls = set()
        for page in range(1, PAGES_TO_SCAN + 1):
            urls = scan_category(path, page)
            all_movie_urls.update(urls)

        print(f"Found {len(all_movie_urls)} movie URLs. Resolving player streams...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_url = {executor.submit(process_movie, url, group_name): url for url in all_movie_urls}
            for future in concurrent.futures.as_completed(future_to_url):
                result = future.result()
                if result:
                    new_entries.append(result)
                    print("  ⚡ Successfully extracted stream")

    bd_time = datetime.now(timezone.utc) + timedelta(hours=6)
    timestamp = bd_time.strftime("%Y-%m-%d %I:%M:%S %p (BD Time)")

    print(f"Writing {len(new_entries)} streams to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U x-tvg-url="" x-tvg-name="{PLAYLIST_NAME}"\n')
        f.write(f'#PLAYLIST:{PLAYLIST_NAME}\n')
        f.write(f'# Last Updated: {timestamp}\n\n')
        for entry in new_entries:
            f.write(entry)

    print("Completed.")

if __name__ == "__main__":
    main()
