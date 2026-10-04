import concurrent.futures
from datetime import datetime, timezone, timedelta
import re
import threading
import time
from urllib.parse import unquote, urljoin, urlparse, parse_qs
import base64
from bs4 import BeautifulSoup
import cloudscraper

# --- Configuration ---
BASE_URL = "https://go5.india4movies.net"
OUTPUT_FILE = "all_movies.m3u"
PAGES_PER_CATEGORY = 15
MAX_WORKERS = 16  # Scaled for fast throughput within CI limits
TOTAL_TIMEOUT_MINUTES = 26  # Stop safely before GitHub Actions 30m kill

CATEGORIES = [
    {"group_name": "Hollywood Hindi Movies", "category_path": "/category/hollywood-hindi-movies/"},
    {"group_name": "Marathi Movies", "category_path": "/category/marathi-movies/"},
    {"group_name": "Bollywood Movies", "category_path": "/category/bollywood-movies-download/"},
    {"group_name": "South Dubbed Movies", "category_path": "/category/south-indian-hindi-dubbed-movies/"}
]

thread_local = threading.local()
stop_flag = threading.Event()

def get_scraper():
    if not hasattr(thread_local, "scraper"):
        thread_local.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
    return thread_local.scraper

def safe_request(url, referer=None, timeout=(4, 7)):
    scraper = get_scraper()
    headers = {"Referer": referer} if referer else {}
    try:
        res = scraper.get(url, headers=headers, timeout=timeout)
        if res.status_code == 200:
            return res
    except Exception:
        return None
    return None

def extract_nested_url(raw_url):
    if not raw_url:
        return None
    raw_url = raw_url.strip().rstrip('\\";),')
    
    parsed = urlparse(raw_url)
    qs = parse_qs(parsed.query)
    for key in ['url', 'link', 'target', 'r', 'token']:
        if key in qs and qs[key]:
            val = qs[key][0]
            if val.startswith("http"):
                return unquote(val)
            try:
                decoded = base64.b64decode(val).decode('utf-8', errors='ignore')
                if decoded.startswith("http"):
                    return decoded
            except Exception:
                pass
    return raw_url

def find_cloud_links(html_text, soup):
    found_links = []
    patterns = [
        r'https?://[^\s"\'<>`]*multicloudlinks\.[^\s"\'<>`]+',
        r'https?://[^\s"\'<>`]*multidownload\.[^\s"\'<>`]+',
        r'https?://[^\s"\'<>`]*hubcloud\.[^\s"\'<>`]+'
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, html_text, re.IGNORECASE):
            link = m.group(0).rstrip('\\";),')
            if link not in found_links:
                found_links.append(link)

    for tag in soup.find_all(['a', 'iframe'], href=True) + soup.find_all('iframe', src=True):
        target = tag.get('href') or tag.get('src')
        if target:
            clean = target.strip()
            if any(k in clean.lower() for k in ['multicloudlinks.', 'multidownload.', 'hubcloud.', 'download']):
                if clean.startswith('//'):
                    clean = f"https:{clean}"
                if clean.startswith('http') and clean not in found_links:
                    found_links.append(clean)
                    
    return found_links

def resolve_stream(cloud_url, movie_url):
    if re.search(r'\.(mkv|mp4|m3u8)(\?|$)', cloud_url, re.IGNORECASE):
        return cloud_url

    cloud_res = safe_request(cloud_url, referer=movie_url, timeout=(4, 7))
    if not cloud_res:
        return None

    dl_match = re.search(r'https?://[^\s"\'<>`]+multidownload\.[^\s"\'<>`]+', cloud_res.text, re.IGNORECASE)
    if dl_match:
        return extract_nested_url(dl_match.group(0))

    cloud_soup = BeautifulSoup(cloud_res.text, 'html.parser')
    for a in cloud_soup.find_all(['a', 'link'], href=True):
        href = a['href'].strip()
        if 'multidownload.' in href.lower() or any(ext in href.lower() for ext in ['.mp4', '.mkv', '.m3u8']):
            return extract_nested_url(href)

    return None

def process_movie(movie_url, group_name):
    if stop_flag.is_set():
        return None

    res = safe_request(movie_url, timeout=(4, 7))
    if not res:
        return None

    soup = BeautifulSoup(res.text, 'html.parser')

    title_elem = soup.select_one('.mp-title') or soup.find('h1')
    title = title_elem.get_text(strip=True) if title_elem else "Unknown Movie"
    title = re.sub(r'[\r\n\t]+', ' ', title).strip()

    poster = ""
    img_wrap = soup.select_one('.mp-img-wrap')
    if img_wrap:
        img = img_wrap if img_wrap.name == 'img' else img_wrap.find('img')
        if img:
            poster = img.get('src') or img.get('data-src') or img.get('data-lazy-src') or ""
            if poster.startswith('//'):
                poster = f"https:{poster}"

    candidate_links = find_cloud_links(res.text, soup)
    if not candidate_links:
        return None

    stream_link = None
    for cand in candidate_links:
        stream_link = resolve_stream(cand, movie_url)
        if stream_link:
            break

    if not stream_link:
        return None

    m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{group_name}", {title}\n{stream_link}\n'
    return m3u_entry, title

def scan_category_page(category_path, page_num):
    if stop_flag.is_set():
        return []
    url = urljoin(BASE_URL, category_path) if page_num == 1 else urljoin(BASE_URL, f"{category_path.rstrip('/')}/page/{page_num}/")
    movie_links = []
    seen = set()

    res = safe_request(url, timeout=(4, 7))
    if not res:
        return []

    soup = BeautifulSoup(res.text, 'html.parser')
    post_items = soup.select('.thumb-content, .item-list, article.post, .mp-post, .thumb, div[class*="movie-item"]')

    if post_items:
        for item in post_items:
            a = item.find('a', href=True)
            if a:
                full = urljoin(BASE_URL, a['href'].strip())
                if full not in seen and not re.search(r'/(category|tag|author|page)/', full, re.IGNORECASE):
                    seen.add(full)
                    movie_links.append(full)
    else:
        main_c = soup.select_one('#content, #main, .site-main, body')
        if main_c:
            for a in main_c.find_all('a', href=True):
                full = urljoin(BASE_URL, a['href'].strip())
                if full.startswith(BASE_URL) and not re.search(r'/(category|tag|author|page|dmca|about)/', full, re.IGNORECASE):
                    if full.rstrip('/') != BASE_URL.rstrip('/') and full not in seen:
                        seen.add(full)
                        movie_links.append(full)

    return movie_links

def main():
    start_time = time.time()
    cutoff_seconds = TOTAL_TIMEOUT_MINUTES * 60

    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now_str = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    # Step 1: Initialize M3U file with header immediately
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write(f'# Generated by Automated Scraper\n')
        f.write(f'# Last Updated: {now_str}\n\n')

    print(f"🚀 Starting Scraper (Max Time: {TOTAL_TIMEOUT_MINUTES} mins, Workers: {MAX_WORKERS})...", flush=True)

    discovered_movies = []
    seen_urls = set()

    # Step 2: Discover category links with fast I/O
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as page_executor:
        future_map = {
            page_executor.submit(scan_category_page, cat["category_path"], p): (cat["group_name"], p)
            for cat in CATEGORIES
            for p in range(1, PAGES_PER_CATEGORY + 1)
        }

        for fut in sorted(future_map.keys(), key=lambda f: (future_map[f][0], future_map[f][1])):
            group_name, page_num = future_map[fut]
            try:
                urls = fut.result()
                for u in urls:
                    if u not in seen_urls:
                        seen_urls.add(u)
                        discovered_movies.append((u, group_name))
            except Exception:
                pass

    print(f"⚡ Total discovered candidates: {len(discovered_movies)}. Streaming results to {OUTPUT_FILE}...", flush=True)

    # Step 3: Stream resolved items to disk in real-time
    file_lock = threading.Lock()
    saved_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as movie_executor:
        futures = {
            movie_executor.submit(process_movie, url, group): url
            for url, group in discovered_movies
        }

        for fut in concurrent.futures.as_completed(futures):
            # Graceful exit safeguard before GitHub Actions timeout triggers
            if time.time() - start_time > cutoff_seconds:
                print(f"\n⏳ Approaching timeout threshold ({TOTAL_TIMEOUT_MINUTES}m). Wrapping up...", flush=True)
                stop_flag.set()
                break

            try:
                res = fut.result()
                if res:
                    entry, title = res
                    with file_lock:
                        saved_count += 1
                        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                            f.write(entry)
                        print(f"   ⚡ Added ({saved_count}): {title[:50]}", flush=True)
            except Exception:
                pass

    elapsed = round((time.time() - start_time) / 60, 2)
    print(f"\n🎉 Finished! Saved {saved_count} movies in {elapsed} mins directly to {OUTPUT_FILE}", flush=True)

if __name__ == "__main__":
    main()
