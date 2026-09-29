import concurrent.futures
from datetime import datetime, timezone, timedelta
import re
import threading
from urllib.parse import unquote, urljoin
from bs4 import BeautifulSoup
import cloudscraper

# --- Configuration ---
BASE_URL = "https://go4.india4movies.net"
OUTPUT_FILE = "all_movies.m3u"
PAGES_PER_CATEGORY = 15
MAX_WORKERS = 20

CATEGORIES = [
    {"group_name": "Hollywood Hindi Movies", "category_path": "/category/hollywood-hindi-movies/"},
    {"group_name": "Marathi Movies", "category_path": "/category/marathi-movies/"},
    {"group_name": "Bollywood Movies", "category_path": "/category/bollywood-movies-download/"},
    {"group_name": "South Dubbed Movies", "category_path": "/category/south-indian-hindi-dubbed-movies/"}
]

# Thread-local cloudscraper instance
thread_local = threading.local()

def get_scraper():
    if not hasattr(thread_local, "scraper"):
        thread_local.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
    return thread_local.scraper

def process_movie(movie_url, group_name):
    scraper = get_scraper()
    try:
        # Step 1: Open movie page
        res = scraper.get(movie_url, timeout=(5, 10))
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, 'html.parser')

        # Step 2: Get .mp-title
        title_elem = soup.select_one('.mp-title')
        if not title_elem:
            title_elem = soup.find('h1')
        title = title_elem.get_text(strip=True) if title_elem else "Unknown Movie"
        title = re.sub(r'[\r\n\t]+', ' ', title).strip()

        # Step 3: Get .mp-img-wrap
        poster = ""
        img_wrap = soup.select_one('.mp-img-wrap')
        if img_wrap:
            img = img_wrap if img_wrap.name == 'img' else img_wrap.find('img')
            if img:
                poster = img.get('src') or img.get('data-src') or img.get('data-lazy-src') or ""
                if poster.startswith('//'):
                    poster = f"https:{poster}"

        # Step 4: Fetch multicloudlinks.com URL
        multicloud_url = None
        cloud_match = re.search(r'https?://[^\s"\'<>`]*multicloudlinks\.com[^\s"\'<>`]*', res.text, re.IGNORECASE)
        if cloud_match:
            multicloud_url = cloud_match.group(0).rstrip('\\";),')
        else:
            for tag in soup.find_all(['a', 'iframe'], src=True) + soup.find_all('a', href=True):
                target = tag.get('href') or tag.get('src')
                if target and 'multicloudlinks.com' in target.lower():
                    multicloud_url = target.strip()
                    break

        if not multicloud_url:
            return None

        # Step 5: Fetch multidownload link from multicloudlinks page
        cloud_res = scraper.get(multicloud_url, headers={"Referer": movie_url}, timeout=(5, 10))
        if cloud_res.status_code != 200:
            return None

        stream_link = None
        dl_match = re.search(r'https?://[^\s"\'<>`]+multidownload\.[^\s"\'<>`]+', cloud_res.text, re.IGNORECASE)
        if dl_match:
            stream_link = dl_match.group(0).rstrip('\\";),')
            if 'url=' in stream_link:
                param = re.search(r'url=([^&]+)', stream_link)
                if param:
                    stream_link = unquote(param.group(1))
        else:
            # Fallback DOM check inside multicloudlinks
            cloud_soup = BeautifulSoup(cloud_res.text, 'html.parser')
            for a in cloud_soup.find_all(['a', 'link'], href=True):
                if 'multidownload.' in a['href'].lower():
                    stream_link = a['href'].strip()
                    break

        if not stream_link:
            return None

        # Format clean M3U block
        m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{group_name}", {title}\n{stream_link}\n'
        return m3u_entry, title

    except Exception:
        return None

def scan_category_page(category_path, page_num):
    url = urljoin(BASE_URL, category_path) if page_num == 1 else urljoin(BASE_URL, f"{category_path.rstrip('/')}/page/{page_num}/")
    scraper = get_scraper()
    movie_links = set()

    try:
        res = scraper.get(url, timeout=(5, 10))
        if res.status_code != 200:
            return []

        soup = BeautifulSoup(res.text, 'html.parser')
        for a in soup.find_all('a', href=True):
            href = a['href']
            full = urljoin(BASE_URL, href)
            # Ensure it is a movie post link, not pagination, tag, or category
            if full.startswith(BASE_URL) and not re.search(r'/(category|tag|author|page)/', full, re.IGNORECASE):
                if full.rstrip('/') != BASE_URL.rstrip('/'):
                    movie_links.add(full)

        return list(movie_links)
    except Exception:
        return []

def main():
    print(f"🚀 Starting Scraper ({PAGES_PER_CATEGORY} pages per category)...", flush=True)

    discovered_movies = []
    seen_urls = set()

    # Phase 1: Collect movie URLs (15 pages per category)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as page_executor:
        page_tasks = {
            page_executor.submit(scan_category_page, cat["category_path"], p): cat["group_name"]
            for cat in CATEGORIES
            for p in range(1, PAGES_PER_CATEGORY + 1)
        }

        for fut in concurrent.futures.as_completed(page_tasks):
            group_name = page_tasks[fut]
            try:
                urls = fut.result()
                for u in urls:
                    if u not in seen_urls:
                        seen_urls.add(u)
                        discovered_movies.append((u, group_name))
            except Exception:
                pass

    print(f"⚡ Discovered {len(discovered_movies)} movie candidates. Resolving streams...", flush=True)

    # Phase 2: Process movies concurrently
    m3u_entries = []
    lock = threading.Lock()

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as movie_executor:
        futures = {
            movie_executor.submit(process_movie, url, group): url
            for url, group in discovered_movies
        }

        for fut in concurrent.futures.as_completed(futures):
            try:
                res = fut.result()
                if res:
                    entry, title = res
                    with lock:
                        m3u_entries.append(entry)
                        print(f"   ⚡ Added: {title[:50]}...", flush=True)
            except Exception:
                pass

    # Phase 3: Write Output M3U File
    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now_str = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    print(f"\n💾 Writing {len(m3u_entries)} entries to {OUTPUT_FILE}...", flush=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write(f'# Playlist Generated Automatically | Total: {len(m3u_entries)}\n')
        f.write(f'# Last Updated: {now_str}\n\n')
        for entry in m3u_entries:
            f.write(entry)

    print(f"🎉 Done! Successfully saved to {OUTPUT_FILE}", flush=True)

if __name__ == "__main__":
    main()
