import asyncio
import csv
from datetime import datetime, timezone, timedelta
import re
import sys
from urllib.parse import unquote, urljoin
import aiohttp
from bs4 import BeautifulSoup

# --- Configuration ---
BASE_URL = "https://go5.india4movies.net"
OUTPUT_M3U = "all_movies.m3u"
MISSING_REPORT_FILE = "missing_movies_report.tsv"
PAGES_PER_CATEGORY = 15
MAX_CONCURRENT_WORKERS = 10

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

file_lock = asyncio.Lock()


async def record_missing(tsv_writer, group, title, url, reason, extra_info=""):
    """Thread-safe append of uncollected entries to the TSV report."""
    async with file_lock:
        tsv_writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            group,
            title,
            url,
            reason,
            extra_info
        ])


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
                    await asyncio.sleep(1.5 * attempt)
                else:
                    return None
        except Exception:
            if attempt < retries:
                await asyncio.sleep(1.0 * attempt)
    return None


async def scan_category(session, cat, queue, seen_urls, tsv_writer):
    """Scans category pages sequentially and pushes movies to the worker queue."""
    clean_cat = cat["category_path"].strip('/')
    group_name = cat["group_name"]

    for page_num in range(1, PAGES_PER_CATEGORY + 1):
        url = f"{BASE_URL}/{clean_cat}/" if page_num == 1 else f"{BASE_URL}/{clean_cat}/page/{page_num}/"
        html = await fetch_html(session, url)

        if not html:
            await record_missing(tsv_writer, group_name, f"Category Page {page_num}", url, "Category Page Fetch Failed")
            break  # Stop pagination early if the page doesn't exist

        soup = BeautifulSoup(html, 'html.parser')
        articles = soup.select('article[id^="post-"]') or soup.select('article.post')

        if not articles:
            # Reached the end of pagination for this category
            break

        for article in articles:
            title_tag = article.select_one('h2.entry-title a, h2.blog-entry-title a')
            if not title_tag or not title_tag.get('href'):
                continue

            full_url = urljoin(BASE_URL, title_tag['href'].strip())
            if full_url in seen_urls or re.search(r'/(category|tag|author|page)/', full_url, re.IGNORECASE):
                continue

            seen_urls.add(full_url)
            await queue.put({"url": full_url, "group": group_name})

        await asyncio.sleep(0.1)  # Brief pause between pages


async def resolve_movie_worker(session, queue, m3u_handle, tsv_writer, counter):
    """Worker task that consumes movies from the queue and resolves streams."""
    while True:
        movie = await queue.get()
        try:
            await resolve_movie_stream(session, movie, m3u_handle, tsv_writer, counter)
        except Exception as e:
            await record_missing(tsv_writer, movie["group"], "Error", movie["url"], f"Unexpected error: {str(e)}")
        finally:
            queue.task_done()


async def resolve_movie_stream(session, movie, m3u_handle, tsv_writer, counter):
    # Step 1: Open movie detail page
    page_html = await fetch_html(session, movie["url"])
    if not page_html:
        await record_missing(tsv_writer, movie["group"], "N/A", movie["url"], "Movie Detail Page Unreachable")
        return

    soup = BeautifulSoup(page_html, 'html.parser')

    # Step 2: Extract title
    title_elem = soup.select_one('.mp-title')
    if not title_elem:
        await record_missing(tsv_writer, movie["group"], "N/A", movie["url"], "No .mp-title Found")
        return
    title = re.sub(r'[\r\n\t]+', ' ', title_elem.get_text(strip=True)).strip()

    # Step 3: Extract poster
    poster = ""
    img_wrap = soup.select_one('.mp-img-wrap')
    if img_wrap:
        img = img_wrap if img_wrap.name == 'img' else img_wrap.find('img')
        if img:
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src') or ""
            if src:
                poster = f"https:{src}" if src.startswith('//') else src

    # Step 4: Extract multicloud link
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
        other_links = [
            a.get('href') for a in soup.find_all('a', href=True)
            if any(x in a.get('href', '').lower() for x in ['cloud', 'download', 'drive', 'hub', 'fast'])
        ]
        first_alt = other_links[0] if other_links else "No alternative link found"
        await record_missing(tsv_writer, movie["group"], title, movie["url"], "No MultiCloud Link Found", first_alt)
        return

    # Step 5: Open multicloud page
    cloud_html = await fetch_html(session, multicloud_url, referer=movie["url"])
    if not cloud_html:
        await record_missing(tsv_writer, movie["group"], title, movie["url"], "MultiCloud Page Unreachable", multicloud_url)
        return

    # Step 6: Extract stream link
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
        await record_missing(tsv_writer, movie["group"], title, movie["url"], "No MultiDownload Link in MultiCloud Page", multicloud_url)
        return

    # Step 7: Write verified M3U entry
    m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{movie["group"]}", {title}\n{stream_link}\n'
    async with file_lock:
        counter["count"] += 1
        m3u_handle.write(m3u_entry)
        m3u_handle.flush()
        print(f"[{counter['count']}] Added: {title[:48]}", flush=True)


async def main():
    counter = {"count": 0}
    seen_urls = set()
    queue = asyncio.Queue(maxsize=100)

    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now_str = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    with open(OUTPUT_M3U, "w", encoding="utf-8") as m3u_f, \
         open(MISSING_REPORT_FILE, "w", encoding="utf-8", newline="") as report_f:

        m3u_f.write('#EXTM3U x-tvg-url=""\n')
        m3u_f.write('# Playlist Generated Automatically\n')
        m3u_f.write(f'# Last Updated: {now_str}\n\n')
        m3u_f.flush()

        tsv_writer = csv.writer(report_f, delimiter="\t")
        tsv_writer.writerow(["Timestamp", "Category", "Title", "Movie_URL", "Failure_Reason", "Detected_Alternate_URL"])
        report_f.flush()

        connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_WORKERS, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            # 1. Start worker pool
            workers = [
                asyncio.create_task(resolve_movie_worker(session, queue, m3u_f, tsv_writer, counter))
                for _ in range(MAX_CONCURRENT_WORKERS)
            ]

            # 2. Run category scanners
            scanner_tasks = [
                scan_category(session, cat, queue, seen_urls, tsv_writer)
                for cat in CATEGORIES
            ]
            await asyncio.gather(*scanner_tasks)

            # 3. Wait until the queue is completely drained
            await queue.join()

            # 4. Cancel idle workers
            for worker in workers:
                worker.cancel()

    print(f"\nRun complete. Success: {counter['count']}. All failures cataloged in: {MISSING_REPORT_FILE}")


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
