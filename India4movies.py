import concurrent.futures
from datetime import datetime, timezone, timedelta
import os
import re
import threading
from urllib.parse import unquote, urlparse, urljoin
from bs4 import BeautifulSoup
import cloudscraper

# --- Configuration ---
BASE_URL = "https://go4.india4movies.net"
CATEGORY_PATH = "/category/hollywood-hindi-movies/"
GROUP_NAME = "Hollywood Hindi Movies"
OUTPUT_FILE = "hollywood_hindi_movies.m3u"

FIRST_RUN_PAGES = 150       # Scans 150 pages on initial deep run
INCREMENTAL_PAGES = 5       # Fast scan on cron runs
IMAGE_PROXY = "https://srhady-live-stream.hf.space/image?url="
MAX_WORKERS = 10

thread_local = threading.local()

def get_scraper():
    if not hasattr(thread_local, "scraper"):
        thread_local.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
    return thread_local.scraper

def get_domain(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"

def extract_file_key(url_or_line):
    clean = url_or_line.split('|')[0].strip()
    match = re.search(r'/([^/?#]+\.(?:mkv|mp4|m3u8))', clean, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return clean.rstrip('/').split('/')[-1].split('?')[0].lower()

def extract_movie_title(soup):
    """
    Extracts the clean title directly from the 'mp-title' class.
    Falls back to <h1> or <title> if missing.
    """
    # 1. Target mp-title class (div, span, h1, h2, etc.)
    mp_elem = soup.find(class_=re.compile(r'\bmp-title\b', re.IGNORECASE))
    if mp_elem:
        raw_text = mp_elem.get_text(strip=True)
        if raw_text:
            return re.sub(r'[\r\n\t]+', ' ', raw_text).strip()

    # 2. Fallback to h1 or meta title
    h1 = soup.find('h1')
    if h1 and h1.get_text(strip=True):
        return re.sub(r'[\r\n\t]+', ' ', h1.get_text(strip=True)).strip()

    title_tag = soup.find('title')
    if title_tag:
        raw = title_tag.get_text(strip=True)
        # Basic cleanup if falling back to <title>
        cleaned = re.split(r'[-–—|:]\s*(?:India4Movies|Watch Online|Download)', raw, flags=re.IGNORECASE)[0]
        return cleaned.strip()

    return "Unknown Movie"

def extract_hero_poster(soup, page_url):
    """
    Extracts poster image specifically looking for 'HERO POSTER'.
    """
    hero_img = soup.find('img', class_=re.compile(r'hero[-_]?poster', re.IGNORECASE))
    if not hero_img:
        hero_img = soup.find('img', alt=re.compile(r'hero[-_]?poster', re.IGNORECASE))
    if not hero_img:
        hero_img = soup.find('img', id=re.compile(r'hero[-_]?poster', re.IGNORECASE))
    
    # Check parent containers labeled hero-poster
    if not hero_img:
        hero_box = soup.find(lambda tag: tag.name in ['div', 'section', 'figure', 'span'] and 
                             any('hero-poster' in str(v).lower() for v in tag.attrs.values()))
        if hero_box:
            hero_img = hero_box.find('img')

    if hero_img:
        src = hero_img.get('src') or hero_img.get('data-src') or hero_img.get('data-lazy-src')
        if src:
            return urljoin(page_url, src.strip())

    # Fallback to standard post thumbnail / og:image
    post_img = soup.find('img', class_=re.compile(r'wp-post-image|attachment-post-thumbnail', re.IGNORECASE))
    if post_img:
        src = post_img.get('src') or post_img.get('data-src')
        if src:
            return urljoin(page_url, src.strip())

    og_img = soup.find('meta', property='og:image')
    if og_img and og_img.get('content'):
        return urljoin(page_url, og_img['content'].strip())

    return ""

def extract_multidownload_link(html_text, page_url):
    """
    Locates the 'Copy Stream Link' (*multidownload.*).
    """
    soup = BeautifulSoup(html_text, 'html.parser')

    for a in soup.find_all(['a', 'link'], href=True):
        href = a['href'].strip()
        if 'multidownload.' in href.lower():
            return urljoin(page_url, href)

    for tag in soup.find_all(attrs=True):
        for attr in ['data-url', 'data-link', 'data-clipboard-text', 'data-src', 'value']:
            val = tag.attrs.get(attr)
            if isinstance(val, str) and 'multidownload.' in val.lower():
                return urljoin(page_url, val.strip())

    matches = re.findall(r'https?://[^\s"\'<>`]+multidownload\.[^\s"\'<>`]+', html_text, re.IGNORECASE)
    if matches:
        return matches[0].rstrip('\\";),')

    return None

def process_movie(post_url, group_name):
    scraper = get_scraper()
    try:
        res = scraper.get(post_url, timeout=(6, 12))
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, 'html.parser')

        # 1. Movie name from "mp-title"
        movie_name = extract_movie_title(soup)

        # 2. Poster from HERO POSTER img
        poster_url = extract_hero_poster(soup, post_url)
        poster = f"{IMAGE_PROXY}{poster_url}" if poster_url else ""

        # 3. Find "Watch Online" link matching *.multicloudlinks.com
        multicloud_url = None
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            if 'multicloudlinks.com' in href.lower():
                multicloud_url = href
                break

        if not multicloud_url:
            for ifr in soup.find_all('iframe', src=True):
                if 'multicloudlinks.com' in ifr['src'].lower():
                    multicloud_url = ifr['src']
                    break

        if not multicloud_url:
            return None

        # 4. Visit multicloudlinks to get multidownload.* stream link
        cloud_headers = {"Referer": post_url}
        cloud_res = scraper.get(multicloud_url, headers=cloud_headers, timeout=(6, 12))
        if cloud_res.status_code != 200:
            return None

        stream_link = extract_multidownload_link(cloud_res.text, multicloud_url)
        if not stream_link:
            return None

        if 'url=' in stream_link:
            param = re.search(r'url=([^&]+)', stream_link)
            if param:
                decoded = unquote(param.group(1))
                if 'multidownload.' in decoded.lower():
                    stream_link = decoded

        clean_file_key = extract_file_key(stream_link)
        final_video_link = f"{stream_link}|Referer={multicloud_url}"

        m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{group_name}", {movie_name}\n{final_video_link}\n'
        return m3u_entry, get_domain(stream_link), clean_file_key, movie_name

    except Exception:
        return None

def scan_single_page(page_num):
    if page_num == 1:
        url = urljoin(BASE_URL, CATEGORY_PATH)
    else:
        url = urljoin(BASE_URL, f"{CATEGORY_PATH.rstrip('/')}/page/{page_num}/")

    scraper = get_scraper()
    found_urls = set()
    try:
        response = scraper.get(url, timeout=(6, 12))
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        posts = soup.find_all(['article', 'div'], class_=re.compile(r'(post|item|movie|film|entry)', re.IGNORECASE))
        if not posts:
            posts = [soup]

        for container in posts:
            for a in container.find_all('a', href=True):
                href = a['href']
                full_url = urljoin(BASE_URL, href)

                if (
                    full_url.startswith(BASE_URL)
                    and not re.search(r'/(category|tag|author|page)/', full_url)
                    and full_url.rstrip('/') != BASE_URL.rstrip('/')
                ):
                    found_urls.add(full_url)

        return list(found_urls)
    except Exception:
        return []

def main():
    print(f"🚀 Starting Scraper for {GROUP_NAME}...", flush=True)

    existing_file_keys = set()
    old_entries = []
    old_domain = None

    file_exists = os.path.exists(OUTPUT_FILE) and os.path.getsize(OUTPUT_FILE) > 0
    pages_to_scan = INCREMENTAL_PAGES if file_exists else FIRST_RUN_PAGES

    if file_exists:
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            old_entries = [
                line for line in lines 
                if not line.startswith('#EXTM3U') 
                and not line.startswith('# Playlist') 
                and not line.startswith('# Last')
            ]
            for line in old_entries:
                key = extract_file_key(line)
                if key:
                    existing_file_keys.add(key)
                if 'multidownload.' in line and not old_domain:
                    clean_link = line.split('|')[0].strip()
                    old_domain = get_domain(clean_link)

        print(f"📁 Loaded existing playlist: {len(existing_file_keys)} items.", flush=True)
        print(f"⚡ Mode: INCREMENTAL ({pages_to_scan} pages).", flush=True)
    else:
        print(f"📁 Initial run: No previous {OUTPUT_FILE} found.", flush=True)
        print(f"⚡ Mode: DEEP SCAN ({pages_to_scan} pages).", flush=True)

    # Step 1: Scan Category Pages (1 to 150)
    print(f"\nScanning pages 1 to {pages_to_scan}...", flush=True)
    candidate_urls = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_page = {executor.submit(scan_single_page, p): p for p in range(1, pages_to_scan + 1)}
        for future in concurrent.futures.as_completed(future_to_page):
            p_num = future_to_page[future]
            try:
                res = future.result()
                print(f" -> Page {p_num} processed ({len(res)} posts found)", flush=True)
                for post_url in res:
                    candidate_urls.add(post_url)
            except Exception as e:
                print(f" -> Page {p_num} error: {e}", flush=True)

    print(f"\nFound {len(candidate_urls)} unique posts. Extracting titles, hero posters & stream links...", flush=True)

    # Step 2: Concurrently Resolve Title (mp-title), Hero Poster, and Multidownload Link
    all_new_entries = []
    active_domain = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_movie, url, GROUP_NAME): url for url in candidate_urls}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    entry, domain, file_key, movie_name = result
                    if file_key not in existing_file_keys:
                        existing_file_keys.add(file_key)
                        all_new_entries.append(entry)
                        print(f"   ⚡ Added: {movie_name[:45]}...", flush=True)
                        if not active_domain:
                            active_domain = domain
            except Exception:
                pass

    # Step 3: Domain change migration
    if old_domain and active_domain and old_domain != active_domain:
        print(f"\n🔄 Domain update: {old_domain} -> {active_domain}", flush=True)
        old_entries_text = "".join(old_entries).replace(old_domain, active_domain)
        old_entries = [old_entries_text]

    # Step 4: Write to M3U
    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    print(f"\n💾 Writing to {OUTPUT_FILE} (+{len(all_new_entries)} new entries added)...", flush=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write(f'# Playlist Generated Automatically ({GROUP_NAME})\n')
        f.write(f'# Last Updated: {now}\n\n')

        for entry in all_new_entries:
            f.write(entry)

        f.write("".join(old_entries))

    print(f"🎉 Complete! Updated {OUTPUT_FILE} successfully.", flush=True)

if __name__ == "__main__":
    main()
