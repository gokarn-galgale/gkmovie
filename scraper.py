import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
import re
from urllib.parse import unquote, urljoin
from bs4 import BeautifulSoup

# --- Configuration ---
BASE_URL = "https://go4.india4movies.net"
OUTPUT_FILE = "all_movies.m3u"
PAGES_PER_CATEGORY = 15
MAX_CONCURRENT_REQUESTS = 40

CATEGORIES = [
    {"group_name": "Hollywood Hindi Movies", "category_path": "/category/hollywood-hindi-movies/"},
    {"group_name": "Marathi Movies", "category_path": "/category/marathi-movies/"},
    {"group_name": "Bollywood Movies", "category_path": "/category/bollywood-movies-download/"},
    {"group_name": "South Dubbed Movies", "category_path": "/category/south-indian-hindi-dubbed-movies/"}
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CLOUD_PATTERN = re.compile(r'https?://[^\s"\'<>`]*multicloudlinks\.[^\s"\'<>`]+', re.IGNORECASE)
DOWNLOAD_PATTERN = re.compile(r'https?://[^\s"\'<>`]+multidownload\.[^\s"\'<>`]+', re.IGNORECASE)

async def fetch_text(session, url, referer=None, timeout=6):
    req_headers = HEADERS.copy()
    if referer:
        req_headers["Referer"] = referer
    try:
        async with session.get(url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 200:
                return await resp.text(errors='ignore')
    except Exception:
        pass
    return None

async def scan_category_page(session, sem, category_path, page_num, group_name):
    url = urljoin(BASE_URL, category_path) if page_num == 1 else urljoin(BASE_URL, f"{category_path.rstrip('/')}/page/{page_num}/")
    
    async with sem:
        html = await fetch_text(session, url)
        
    if not html:
        return []

    soup = BeautifulSoup(html, 'html.parser')
    items = soup.select('.thumb-content, .item-list, article.post, .mp-post, .thumb, div[class*="movie-item"]')
    
    results = []
    seen = set()

    for item in items:
        a = item.find('a', href=True)
        if not a:
            continue
        full_url = urljoin(BASE_URL, a['href'].strip())
        if full_url in seen or re.search(r'/(category|tag|author|page)/', full_url, re.IGNORECASE):
            continue
        seen.add(full_url)

        title_elem = item.select_one('.mp-title, h2, h3, .title') or a
        title = title_elem.get_text(strip=True) if title_elem else "Unknown Movie"
        title = re.sub(r'[\r\n\t]+', ' ', title).strip()

        img = item.find('img')
        poster = ""
        if img:
            poster = img.get('src') or img.get('data-src') or img.get('data-lazy-src') or ""
            if poster.startswith('//'):
                poster = f"https:{poster}"

        results.append({
            "url": full_url,
            "title": title,
            "poster": poster,
            "group": group_name
        })

    return results

async def resolve_movie_stream(session, sem, movie, file_handle, lock, counter):
    async with sem:
        page_html = await fetch_text(session, movie["url"])
        if not page_html:
            return

        if movie["title"] == "Unknown Movie" or len(movie["title"]) < 2:
            soup = BeautifulSoup(page_html, 'html.parser')
            t = soup.select_one('.mp-title') or soup.find('h1')
            if t:
                movie["title"] = re.sub(r'[\r\n\t]+', ' ', t.get_text(strip=True)).strip()

        cloud_match = CLOUD_PATTERN.search(page_html)
        if not cloud_match:
            return
        cloud_url = cloud_match.group(0).rstrip('\\";),')

        cloud_html = await fetch_text(session, cloud_url, referer=movie["url"])
        if not cloud_html:
            return

        dl_match = DOWNLOAD_PATTERN.search(cloud_html)
        if not dl_match:
            return

        stream_link = dl_match.group(0).rstrip('\\";),')
        if 'url=' in stream_link:
            param = re.search(r'url=([^&]+)', stream_link)
            if param:
                stream_link = unquote(param.group(1))

        entry = f'#EXTINF:-1 tvg-logo="{movie["poster"]}" group-title="{movie["group"]}", {movie["title"]}\n{stream_link}\n'
        
        async with lock:
            counter["count"] += 1
            file_handle.write(entry)
            file_handle.flush()
            print(f"⚡ [{counter['count']}] Added: {movie['title'][:55]}", flush=True)

async def main():
    start_time = datetime.now()
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    lock = asyncio.Lock()
    counter = {"count": 0}

    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now_str = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write(f'# Playlist Generated via Automated Scraper\n')
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
            
            candidates = []
            seen_urls = set()
            for page in page_results:
                for item in page:
                    if item["url"] not in seen_urls:
                        seen_urls.add(item["url"])
                        candidates.append(item)

            print(f"⚡ Discovered {len(candidates)} candidates. Resolving streams...", flush=True)

            resolve_tasks = [
                resolve_movie_stream(session, sem, movie, f, lock, counter)
                for movie in candidates
            ]
            
            await asyncio.gather(*resolve_tasks)

    duration = (datetime.now() - start_time).total_seconds()
    print(f"\n🎉 Done! Saved {counter['count']} streams in {round(duration, 1)} seconds to {OUTPUT_FILE}", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
