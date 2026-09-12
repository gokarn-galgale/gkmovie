import concurrent.futures
from datetime import datetime, timezone, timedelta
import os
import re
import threading
from urllib.parse import unquote, urlparse
from bs4 import BeautifulSoup
import cloudscraper

# --- Configuration ---
BASE_URL = "https://fibwatch.art"
OUTPUT_FILE = "latest_movies.m3u"
FIRST_RUN_PAGES = 150       # Deep scan per category if file does not exist
INCREMENTAL_PAGES = 10      # Fast scan per category on regular cron runs
IMAGE_PROXY = "https://srhady-live-stream.hf.space/image?url="
MAX_WORKERS = 15

CATEGORIES = [
    {"cat_id": "4", "group_name": "Hindi Movies"},
    {"cat_id": "5", "group_name": "Hindi-Dubbed Movies"},
    {"cat_id": "13", "group_name": "Marathi Movies"},
    {"cat_id": "7", "group_name": "Cartoon Movies"}
]

thread_local = threading.local()

def get_scraper():
    if not hasattr(thread_local, "scraper"):
        thread_local.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
    return thread_local.scraper

def get_resolution(text):
    match = re.search(r'(\d{3,4})p', text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if '4k' in text.lower():
        return 2160
    return 0

def get_domain(url):
    parsed_uri = urlparse(url)
    return f"{parsed_uri.scheme}://{parsed_uri.netloc}"

def extract_file_key(url_or_line):
    """Extracts base filename (e.g. movie.2024.1080p.mkv) for duplicate tracking."""
    clean_url = url_or_line.split('|')[0].strip()
    match = re.search(r'/([^/?#]+\.(?:mkv|mp4))', clean_url, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return None

def process_movie(base_name, watch_link, group_name):
    scraper = get_scraper()
    try:
        res = scraper.get(watch_link, timeout=15)
        if res.status_code != 200:
            return None

        watch_soup = BeautifulSoup(res.text, 'html.parser')
        actual_link = None
        
        for a in watch_soup.find_all('a', href=True):
            href = a['href']
            
            if 'urlshortlink.top' in href and 'url=' in href:
                match = re.search(r'url=(.*)', href)
                if match:
                    decoded = unquote(match.group(1))
                    if any(ext in decoded.lower() for ext in ('.mkv', '.mp4')):
                        actual_link = decoded
                        break
            
            elif any(ext in href.lower() for ext in ('.mkv', '.mp4')) and 'urlshortlink.top' not in href:
                actual_link = href if href.startswith('http') else f"{BASE_URL}{href}"
                break
        
        if not actual_link:
            return None
            
        poster_tag = watch_soup.find('meta', property='og:image')
        poster = poster_tag['content'] if poster_tag else ""
        if poster:
            poster = f"{IMAGE_PROXY}{poster}"
        
        raw_name = actual_link.split('/')[-1].split('?')[0]
        file_name = re.sub(r'\[Fibwatch\.Com\]|\.mkv|\.mp4', '', raw_name, flags=re.IGNORECASE).replace('.', ' ').strip()
        final_video_link = f"{actual_link}|Referer={BASE_URL}/"
        
        m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{group_name}", {file_name}\n{final_video_link}\n'
        return m3u_entry, get_domain(actual_link), raw_name.lower()
        
    except Exception:
        return None

def scan_single_page(cat_id, page_num):
    url = f"{BASE_URL}/videos/category/{cat_id}?page_id={page_num}"
    scraper = get_scraper()
    found_movies = []
    try:
        response = scraper.get(url, timeout=15)
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        links = soup.find_all('a', href=True)
        watch_links = [l['href'] for l in links if '/watch/' in l['href'] and l['href'].endswith('.html')]
        
        for link in set(watch_links):
            full_link = link if link.startswith('http') else f"{BASE_URL}{link}"
            filename = full_link.split('/')[-1]
            base_name = re.sub(r'[-_]?\d{3,4}p.*\.html$', '', filename, flags=re.IGNORECASE)
            found_movies.append((base_name, full_link))
            
        return found_movies
    except Exception:
        return []

def main():
    print("🚀 Starting Unified 4-Category Scraper for GitHub Actions...")

    existing_file_keys = set()
    old_entries = []
    old_domain = None

    # Step 1: Detect First Run vs Subsequent Incremental Run
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
                if ('.mkv' in line or '.mp4' in line) and not old_domain:
                    clean_link = line.split('|')[0].strip()
                    old_domain = get_domain(clean_link)

        print(f"📁 Loaded existing unified playlist: {len(existing_file_keys)} items.")
        print(f"⚡ Mode: INCREMENTAL ({pages_to_scan} pages per category). Previous CDN: {old_domain}")
    else:
        print(f"📁 Initial run: No previous {OUTPUT_FILE} found.")
        print(f"⚡ Mode: DEEP INITIAL SCAN ({pages_to_scan} pages per category).")

    all_new_entries = []
    active_domain = None

    # Step 2: Iterate through all 4 categories
    for cat in CATEGORIES:
        cat_id = cat["cat_id"]
        group_name = cat["group_name"]
        print(f"\nScanning Category: {group_name} (ID: {cat_id})...")

        new_movies_links = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_page = {executor.submit(scan_single_page, cat_id, p): p for p in range(1, pages_to_scan + 1)}
            for future in concurrent.futures.as_completed(future_to_page):
                for base_name, full_link in future.result():
                    current_res = get_resolution(full_link)
                    if base_name in new_movies_links:
                        existing_link = new_movies_links[base_name]
                        if current_res > get_resolution(existing_link):
                            new_movies_links[base_name] = full_link
                    else:
                        new_movies_links[base_name] = full_link

        print(f"   Found {len(new_movies_links)} candidates. Extracting direct links...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(process_movie, b_name, w_link, group_name): b_name 
                for b_name, w_link in new_movies_links.items()
            }
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    entry, domain, file_key = result
                    # Deduplicate globally across old entries and the current run
                    if file_key not in existing_file_keys:
                        existing_file_keys.add(file_key)
                        all_new_entries.append(entry)
                        print(f"   ⚡ Added [{group_name}]: {file_key[:45]}")
                        if not active_domain:
                            active_domain = domain

    # Step 3: Handle CDN domain updates across previous links
    if old_domain and active_domain and old_domain != active_domain:
        print(f"\n🔄 CDN change detected: {old_domain} -> {active_domain}. Updating historical links...")
        old_entries_text = "".join(old_entries)
        old_entries_text = old_entries_text.replace(old_domain, active_domain)
        old_entries = [old_entries_text]

    # Step 4: Write all categories into single latest_movies.m3u
    bd_time = datetime.now(timezone.utc) + timedelta(hours=6)
    now = bd_time.strftime("%Y-%m-%d %I:%M:%S %p (BD Time)")

    print(f"\n💾 Writing to {OUTPUT_FILE} (+{len(all_new_entries)} total new items added)...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write('# Playlist Generated Automatically (Hindi, Dubbed, Marathi, Cartoon)\n')
        f.write(f'# Last Updated: {now}\n\n')
        
        # Newest items prepended
        for entry in all_new_entries:
            f.write(entry)
            
        f.write("".join(old_entries))

    print(f"🎉 Successfully completed! Updated {OUTPUT_FILE} with 0 duplicates.")

if __name__ == "__main__":
    main()

