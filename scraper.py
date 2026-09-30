import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
import re
from urllib.parse import unquote, urljoin
from bs4 import BeautifulSoup
import sys
import os

# --- Configuration ---
BASE_URL = "https://go4.india4movies.net"
OUTPUT_FILE = "all_movies.m3u"
LOG_FILE = "scraper_debug.log"
PAGES_PER_CATEGORY = 15
MAX_CONCURRENT_REQUESTS = 10

CATEGORIES = [
    {"group_name": "Hollywood Hindi Movies", "category_path": "/category/hollywood-hindi-movies/"},
    {"group_name": "Marathi Movies", "category_path": "/category/marathi-movies/"},
    {"group_name": "Bollywood Movies", "category_path": "/category/bollywood-movies-download/"},
    {"group_name": "South Dubbed Movies", "category_path": "/category/south-indian-hindi-dubbed-movies/"}
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CLOUD_PATTERN = re.compile(r'https?://[^\s"\'<>`]*multicloudlinks\.[^\s"\'<>`]+', re.IGNORECASE)
DOWNLOAD_PATTERN = re.compile(r'https?://[^\s"\'<>`]+multidownload\.[^\s"\'<>`]+', re.IGNORECASE)

# Global tracker for failure points
DROP_REASONS = {
    "category_page_failed": 0,
    "no_articles_found": 0,
    "movie_page_failed": 0,
    "multicloud_not_found": 0,
    "cloud_page_failed": 0,
    "multidownload_not_found": 0,
    "success": 0
}

log_lock = asyncio.Lock()

async def log_event(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {message}\n"
    async with log_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted)
            f.flush()

async def fetch_html(session, url, referer=None, timeout=12, retries=2):
    req_headers = HEADERS.copy()
    if referer:
        req_headers["Referer"] = referer

    for attempt in range(1, retries + 1):
        try:
            async with session.get(url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    return await resp.text(errors='ignore')
                elif resp.status in (403, 429, 502, 503):
                    await log_event(f"HTTP {resp.status} on {url} (Attempt {attempt})")
                    await asyncio.sleep(2 * attempt)
                else:
                    await log_event(f"HTTP {resp.status} on {url} - aborting")
                    return None
        except asyncio.TimeoutError:
            await log_event(f"TIMEOUT on {url} (Attempt {attempt})")
        except Exception as e:
            await log_event(f"ERROR on {url}: {type(e).__name__} - {e}")
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
    return None

async def scan_category_page(session, sem, category_path, page_num, group_name):
    clean_cat = category_path.strip('/')
    url = f"{BASE_URL}/{clean_cat}/" if page_num == 1 else f"{BASE_URL}/{clean_cat}/page/{page_num}/"

    async with sem:
        html = await fetch_html(session, url)
        await asyncio.sleep(0.1)

    if not html:
        DROP_REASONS["category_page_failed"] += 1
        await log_event(f"[STAGE 1 DROP] Failed to fetch category page: {url}")
        return []

    soup = BeautifulSoup(html, 'html.parser')
    articles = soup.select('article[id^="post-"]') or soup.select('article.post')

    if not articles:
        DROP_REASONS["no_articles_found"] += 1
        await log_event(f"[STAGE 1 DROP] 0 articles detected on: {url}")
        return []

    page_movies = []
    seen = set()

    for article in articles:
        title_tag = article.select_one('h2.entry-title a, h2.blog-entry-title a')
        if not title_tag or not title_tag.get('href'):
            continue

        full_url = urljoin(BASE_URL, title_tag['href'].strip())
        if full_url in seen or re.search(r'/(category|tag|author|page)/', full_url, re.IGNORECASE):
            continue

        seen.add(full_url)
        title = title_tag.get_text(strip=True)
        img_elem = article.select_one('.mpo-overlay-wrap img, .wp-post-image')
        poster = ""
        if img_elem:
            poster = img_elem.get('src') or img_elem.get('data-src') or ""
            if poster.startswith('//'):
                poster = f"https:{poster}"

        page_movies.append({
            "url": full_url,
            "group": group_name,
            "title": title,
            "poster": poster
        })

    await log_event(f"[STAGE 1 OK] Found {len(page_movies)} movies on {url}")
    return page_movies

async def resolve_movie_stream(session, sem, movie, file_handle, lock, counter):
    async with sem:
        # Step 1: Open movie detail page
        page_html = await fetch_html(session, movie["url"])
        if not page_html:
            DROP_REASONS["movie_page_failed"] += 1
            await log_event(f"[STAGE 2 DROP] Cannot open movie page: {movie['url']}")
            return

        soup = BeautifulSoup(page_html, 'html.parser')

        # Fallback Title & Poster
        title_elem = soup.select_one('.mp-title, h1.entry-title, h1')
        title = title_elem.get_text(strip=True) if title_elem else movie.get("title", "Unknown Movie")
        title = re.sub(r'[\r\n\t]+', ' ', title).strip()

        poster = movie.get("poster", "")
        img_elem = soup.select_one('.mp-img-wrap img, .entry-content img')
        if img_elem:
            src = img_elem.get('src') or img_elem.get('data-src') or img_elem.get('data-lazy-src') or ""
            if src:
                poster = f"https:{src}" if src.startswith('//') else src

        # Step 2: Extract multicloudlinks.com URL
        multicloud_url = None
        cloud_match = CLOUD_PATTERN.search(page_html)
        if cloud_match:
            multicloud_url = cloud_match.group(0).rstrip('\\";),')
        else:
            for tag in soup.find_all(['a', 'iframe'], src=True) + soup.find_all('a', href=True):
                target = tag.get('href') or tag.get('src')
                if target and 'multicloudlinks.' in target.lower():
                    multicloud_url = target.strip()
                    break

        if not multicloud_url:
            DROP_REASONS["multicloud_not_found"] += 1
            await log_event(f"[STAGE 2 DROP] No multicloud link on: {movie['url']}")
            return

        # Step 3: Open multicloudlinks page to grab multidownload URL
        cloud_html = await fetch_html(session, multicloud_url, referer=movie["url"])
        if not cloud_html:
            DROP_REASONS["cloud_page_failed"] += 1
            await log_event(f"[STAGE 2 DROP] Multicloud page unreachable: {multicloud_url}")
            return

        stream_link = None
        dl_match = DOWNLOAD_PATTERN.search(cloud_html)
        if dl_match:
            stream_link = dl_match.group(0).rstrip('\\";),')
            if 'url=' in stream_link:
                param = re.search(r'url=([^&]+)', stream_link)
                if param:
                    stream_link = unquote(param.group(1))
        else:
            cloud_soup = BeautifulSoup(cloud_html, 'html.parser')
            for a in cloud_soup.find_all(['a', 'link'], href=True):
                target = a['href'].strip()
                if 'multidownload.' in target.lower():
                    stream_link = target
                    break

        if not stream_link:
            DROP_REASONS["multidownload_not_found"] += 1
            await log_event(f"[STAGE 2 DROP] No multidownload pattern found in: {multicloud_url}")
            return

        # Step 4: Write immediately
        m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{movie["group"]}", {title}\n{stream_link}\n'

        async with lock:
            counter["count"] += 1
            DROP_REASONS["success"] += 1
            file_handle.write(m3u_entry)
            file_handle.flush()
            print(f"[{counter['count']}] Added: {title[:48]}", flush=True)

async def main():
    start_time = datetime.now()
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    lock = asyncio.Lock()
    counter = {"count": 0}

    # Reset log file
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"=== SCRAPER EXECUTION LOG: {datetime.now()} ===\n\n")

    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now_str = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write(f'# Playlist Generated Automatically\n')
        f.write(f'# Last Updated: {now_str}\n\n')
        f.flush()

        connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            print(f"Scanning {len(CATEGORIES) * PAGES_PER_CATEGORY} category pages...", flush=True)
            await log_event("Starting Stage 1: Category Scanning")

            scan_tasks = [
                scan_category_page(session, sem, cat["category_path"], p, cat["group_name"])
                for cat in CATEGORIES
                for p in range(1, PAGES_PER_CATEGORY + 1)
            ]

            page_results = await asyncio.gather(*scan_tasks)

            all_movies = []
            seen_urls = set()
            for page in page_results:
                for item in page:
                    if item["url"] not in seen_urls:
                        seen_urls.add(item["url"])
                        all_movies.append(item)

            summary_msg = f"Discovered {len(all_movies)} unique movies across scanned pages."
            print(f"{summary_msg} Resolving streams...", flush=True)
            await log_event(summary_msg)

            # Stage 2
            resolve_tasks = [
                resolve_movie_stream(session, sem, movie, f, lock, counter)
                for movie in all_movies
            ]

            await asyncio.gather(*resolve_tasks)

    # Print final summary to console and log
    duration = round((datetime.now() - start_time).total_seconds() / 60, 2)
    summary_report = f"""
================ SCRAPING SUMMARY ================
Time Elapsed              : {duration} mins
Total Successfully Added  : {DROP_REASONS['success']}
Category Pages Failed     : {DROP_REASONS['category_page_failed']}
Category Pages Empty      : {DROP_REASONS['no_articles_found']}
Movie Details Page Failed : {DROP_REASONS['movie_page_failed']}
No MultiCloud Link Found  : {DROP_REASONS['multicloud_not_found']}
MultiCloud Page Failed    : {DROP_REASONS['cloud_page_failed']}
No MultiDownload Found    : {DROP_REASONS['multidownload_not_found']}
==================================================
Detailed errors saved in: {LOG_FILE}
"""
    print(summary_report, flush=True)
    await log_event(summary_report)

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
