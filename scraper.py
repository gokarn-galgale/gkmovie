import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
import re
from urllib.parse import unquote, urljoin
from bs4 import BeautifulSoup
import sys

# --- Configuration ---
BASE_URL = "https://go4.india4movies.net"
OUTPUT_FILE = "all_movies.m3u"
PAGES_PER_CATEGORY = 15

# Lower concurrency slightly to prevent silent HTTP 429 / 503 / Cloudflare drops
MAX_CONCURRENT_REQUESTS = 12

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

async def fetch_html(session, url, referer=None, timeout=15, retries=3):
    """Fetch HTML with exponential backoff on dropouts or rate-limiting."""
    req_headers = HEADERS.copy()
    if referer:
        req_headers["Referer"] = referer

    for attempt in range(1, retries + 1):
        try:
            async with session.get(url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    return await resp.text(errors='ignore')
                elif resp.status in (429, 503, 502):
                    await asyncio.sleep(2 * attempt)
                else:
                    return None
        except Exception:
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
    return None

async def scan_category_page(session, sem, category_path, page_num, group_name):
    clean_cat = category_path.strip('/')
    url = f"{BASE_URL}/{clean_cat}/" if page_num == 1 else f"{BASE_URL}/{clean_cat}/page/{page_num}/"
    
    async with sem:
        html = await fetch_html(session, url)
        # Gentle pacing between category page fetches
        await asyncio.sleep(0.15)
        
    if not html:
        print(f"⚠️ [Failed Page] Could not load: {url}", flush=True)
        return []

    soup = BeautifulSoup(html, 'html.parser')
    articles = soup.select('article[id^="post-"]') or soup.select('article.post')
    
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
        
        # Read poster & title directly from listing card
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

async def resolve_movie_stream(session, sem, movie, file_handle, lock, counter):
    async with sem:
        # Step 1: Open movie detail page
        page_html = await fetch_html(session, movie["url"])
        if not page_html:
            return

        soup = BeautifulSoup(page_html, 'html.parser')

        # Fallback Title & Poster
        title_elem = soup.select_one('.mp-title, h1.entry-title, h1')
        title = title_elem.get_text(strip=True) if title_elem else movie.get("title", "Unknown Movie")
        title = re.sub(r'[\r\n\t]+', ' ', title).strip()

        poster = movie.get("poster", "")
        img_elem = soup.select_one('.mp-img-wrap img, .entry-content img')
        if img_elem:
            src = img_elem.get('src') or img_elem.get('data-src') or ""
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
            return

        # Step 3: Open multicloudlinks page to grab multidownload URL
        cloud_html = await fetch_html(session, multicloud_url, referer=movie["url"])
        if not cloud_html:
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
            return

        # Step 4: Write immediately
        m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{movie["group"]}", {title}\n{stream_link}\n'
        
        async with lock:
            counter["count"] += 1
            file_handle.write(m3u_entry)
            file_handle.flush()
            print(f"⚡ [{counter['count']}] Added: {title[:50]}", flush=True)

async def main():
    start_time = datetime.now()
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    lock = asyncio.Lock()
    counter = {"count": 0}

    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now_str = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write(f'# Playlist Generated Automatically\n')
        f.write(f'# Last Updated: {now_str}\n\n')
        f.flush()

        connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            print(f"🚀 Scanning {len(CATEGORIES) * PAGES_PER_CATEGORY} category pages...", flush=True)

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

            print(f"⚡ Discovered {len(all_movies)} unique movies. Resolving stream links...", flush=True)

            resolve_tasks = [
                resolve_movie_stream(session, sem, movie, f, lock, counter)
                for movie in all_movies
            ]
            
            await asyncio.gather(*resolve_tasks)

    duration = round((datetime.now() - start_time).total_seconds() / 60, 2)
    print(f"\n🎉 Done! Saved {counter['count']} items in {duration} minutes to {OUTPUT_FILE}", flush=True)

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
