import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
import re
from urllib.parse import unquote, urljoin
from bs4 import BeautifulSoup
import sys
import csv

# --- Configuration ---
BASE_URL = "https://go4.india4movies.net"
OUTPUT_M3U = "all_movies.m3u"
MISSING_REPORT_FILE = "missing_movies_report.tsv"
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

file_lock = asyncio.Lock()

async def record_missing(tsv_writer, group, title, url, reason, extra_info=""):
    """Appends an uncollected movie directly to the separate diagnostic file."""
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
                    await asyncio.sleep(2 * attempt)
                else:
                    return None
        except Exception:
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
    return None

async def scan_category_page(session, sem, category_path, page_num, group_name, tsv_writer):
    clean_cat = category_path.strip('/')
    url = f"{BASE_URL}/{clean_cat}/" if page_num == 1 else f"{BASE_URL}/{clean_cat}/page/{page_num}/"

    async with sem:
        html = await fetch_html(session, url)
        await asyncio.sleep(0.15)

    if not html:
        await record_missing(tsv_writer, group_name, f"Category Page {page_num}", url, "Category Page Fetch Failed")
        return []

    soup = BeautifulSoup(html, 'html.parser')
    articles = soup.select('article[id^="post-"]') or soup.select('article.post')

    if not articles:
        await record_missing(tsv_writer, group_name, f"Category Page {page_num}", url, "Zero Articles Found on Category Page")
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

    return page_movies

async def resolve_movie_stream(session, sem, movie, m3u_handle, tsv_writer, counter):
    async with sem:
        # Step 1: Open movie detail page
        page_html = await fetch_html(session, movie["url"])
        if not page_html:
            await record_missing(tsv_writer, movie["group"], movie["title"], movie["url"], "Movie Detail Page Unreachable")
            return

        soup = BeautifulSoup(page_html, 'html.parser')

        # Fallback Title & Poster
        title_elem = soup.select_one('.mp-title, h1.entry-title, h1')
        title = title_elem.get_text(strip=True) if title_elem else movie["title"]
        title = re.sub(r'[\r\n\t]+', ' ', title).strip()

        poster = movie.get("poster", "")
        img_elem = soup.select_one('.mp-img-wrap img, .entry-content img')
        if img_elem:
            src = img_elem.get('src') or img_elem.get('data-src') or img_elem.get('data-lazy-src') or ""
            if src:
                poster = f"https:{src}" if src.startswith('//') else src

        # Step 2: Extract multicloud link
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
            # Capture what download/redirect links existed instead
            other_links = [
                a.get('href') for a in soup.find_all('a', href=True)
                if any(x in a.get('href', '').lower() for x in ['cloud', 'download', 'drive', 'hub', 'fast'])
            ]
            first_alternative = other_links[0] if other_links else "No alternative link found"
            await record_missing(tsv_writer, movie["group"], title, movie["url"], "No MultiCloud Link Found", first_alternative)
            return

        # Step 3: Open multicloud page
        cloud_html = await fetch_html(session, multicloud_url, referer=movie["url"])
        if not cloud_html:
            await record_missing(tsv_writer, movie["group"], title, movie["url"], "MultiCloud Page Unreachable", multicloud_url)
            return

        # Step 4: Extract stream link
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

        # Step 5: Write valid M3U entry
        m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{movie["group"]}", {title}\n{stream_link}\n'
        async with file_lock:
            counter["count"] += 1
            m3u_handle.write(m3u_entry)
            m3u_handle.flush()
            print(f"[{counter['count']}] Added: {title[:48]}", flush=True)

async def main():
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    counter = {"count": 0}

    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now_str = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    # Open both output files
    with open(OUTPUT_M3U, "w", encoding="utf-8") as m3u_f, \
         open(MISSING_REPORT_FILE, "w", encoding="utf-8", newline="") as report_f:

        m3u_f.write('#EXTM3U x-tvg-url=""\n')
        m3u_f.write(f'# Playlist Generated Automatically\n')
        m3u_f.write(f'# Last Updated: {now_str}\n\n')
        m3u_f.flush()

        tsv_writer = csv.writer(report_f, delimiter="\t")
        tsv_writer.writerow(["Timestamp", "Category", "Title", "Movie_URL", "Failure_Reason", "Detected_Alternate_URL"])
        report_f.flush()

        connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            print(f"Scanning categories and tracking missing items in {MISSING_REPORT_FILE}...", flush=True)

            scan_tasks = [
                scan_category_page(session, sem, cat["category_path"], p, cat["group_name"], tsv_writer)
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

            print(f"Discovered {len(all_movies)} unique movies. Resolving streams...", flush=True)

            resolve_tasks = [
                resolve_movie_stream(session, sem, movie, m3u_f, tsv_writer, counter)
                for movie in all_movies
            ]

            await asyncio.gather(*resolve_tasks)

    print(f"\nRun complete. Success: {counter['count']}. All failures cataloged in: {MISSING_REPORT_FILE}")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
