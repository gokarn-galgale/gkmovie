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
MAX_CONCURRENT_REQUESTS = 35

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

async def fetch_html(session, url, referer=None, timeout=8):
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
        html = await fetch_html(session, url)
        
    if not html:
        return []

    soup = BeautifulSoup(html, 'html.parser')
    
    # Target post articles rendered in the blog feed
    articles = soup.select('.posts-wrapper article') or soup.select('article.post')
    
    page_movies = []
    seen = set()

    for article in articles:
        # Extract direct movie post title and link
        title_tag = article.select_one('h2.entry-title a') or article.select_one('.blog-entry-title a')
        if not title_tag or not title_tag.get('href'):
            continue
            
        full_url = urljoin(BASE_URL, title_tag['href'].strip())
        if full_url in seen or re.search(r'/(category|tag|author|page)/', full_url, re.IGNORECASE):
            continue
            
        seen.add(full_url)
        
        # Extract fallback title and poster thumbnail directly from article card
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
        # Step 1: Open movie page
        page_html = await fetch_html(session, movie["url"])
        if not page_html:
            return

        soup = BeautifulSoup(page_html, 'html.parser')

        # Step 2: Use title and poster from category scan as baseline fallback
        title_elem = soup.select_one('.mp-title') or soup.find('h1') or soup.select_one('h1.entry-title')
        title = title_elem.get_text(strip=True) if title_elem else movie.get("title", "Unknown Movie")
        title = re.sub(r'[\r\n\t]+', ' ', title).strip()

        poster = movie.get("poster", "")
        img_wrap = soup.select_one('.mp-img-wrap, .entry-content img')
        if img_wrap:
            img = img_wrap if img_wrap.name == 'img' else img_wrap.find('img')
            if img:
                src = img.get('src') or img.get('data-src') or img.get('data-lazy-src') or ""
                if src:
                    poster = f"https:{src}" if src.startswith('//') else src

        # Step 3: Fetch multicloudlinks.com URL
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

        # Step 4: Fetch multidownload link from multicloudlinks page
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

        # Step 5: Export entry immediately to M3U
        m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{movie["group"]}", {title}\n{stream_link}\n'
        
        async with lock:
            counter["count"] += 1
            file_handle.write(m3u_entry)
            file_handle.flush()
            print(f"⚡ [{counter['count']}] Added: {title[:55]}", flush=True)

async def main():
    start_time = datetime.now()
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    lock = asyncio.Lock()
    counter = {"count": 0}

    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now_str = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        # Write M3U Header
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write(f'# Playlist Generated Automatically\n')
        f.write(f'# Last Updated: {now_str}\n\n')
        f.flush()

        connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            print(f"🚀 Scanning {len(CATEGORIES) * PAGES_PER_CATEGORY} category pages...", flush=True)

            # Stage 1: Collect 15 pages per category
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

            print(f"⚡ Discovered {len(all_movies)} unique movies. Resolving streams...", flush=True)

            # Stage 2: Concurrent resolution & incremental append
            resolve_tasks = [
                resolve_movie_stream(session, sem, movie, f, lock, counter)
                for movie in all_movies
            ]
            
            await asyncio.gather(*resolve_tasks)

    duration = round((datetime.now() - start_time).total_seconds() / 60, 2)
    print(f"\n🎉 Done! Saved {counter['count']} items in {duration} minutes to {OUTPUT_FILE}", flush=True)

if __name__ == "__main__":
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
