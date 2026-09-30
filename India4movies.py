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
BASE_URL = "https://go4.india4movies.net"
OUTPUT_FILE = "all_movies.m3u"
PAGES_PER_CATEGORY = 15
MAX_WORKERS = 8  # Reduced to avoid WAF rate-limits & timeouts

CATEGORIES = [
    {"group_name": "Hollywood Hindi Movies", "category_path": "/category/hollywood-hindi-movies/"},
    {"group_name": "Marathi Movies", "category_path": "/category/marathi-movies/"},
    {"group_name": "Bollywood Movies", "category_path": "/category/bollywood-movies-download/"},
    {"group_name": "South Dubbed Movies", "category_path": "/category/south-indian-hindi-dubbed-movies/"}
]

thread_local = threading.local()

def get_scraper():
    if not hasattr(thread_local, "scraper"):
        thread_local.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
    return thread_local.scraper

def safe_request(url, referer=None, retries=2, timeout=(8, 15)):
    scraper = get_scraper()
    headers = {"Referer": referer} if referer else {}
    for attempt in range(retries + 1):
        try:
            res = scraper.get(url, headers=headers, timeout=timeout)
            if res.status_code == 200:
                return res
            elif res.status_code in [429, 503]:
                time.sleep(1.5 * (attempt + 1))
        except Exception:
            if attempt < retries:
                time.sleep(1.0)
    return None

def extract_nested_url(raw_url):
    """Extracts raw media URLs wrapped inside redirectors or base64 params."""
    if not raw_url:
        return None
    raw_url = raw_url.strip().rstrip('\\";),')
    
    # Check query strings for embedded URLs
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
    """Detects multi-cloud lockers and intermediate download/resolution hubs."""
    found_links = []
    
    # Regex search for multicloud and direct multidownload locker hosts
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

    # DOM search through anchors & iframes
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
    """Fetches the cloud page and resolves the underlying video/stream file."""
    # If the candidate is already a direct media target
    if re.search(r'\.(mkv|mp4|m3u8)(\?|$)', cloud_url, re.IGNORECASE):
        return cloud_url

    cloud_res = safe_request(cloud_url, referer=movie_url)
    if not cloud_res:
        return None

    # Search for multidownload or direct downloadable/streamable targets
    dl_match = re.search(r'https?://[^\s"\'<>`]+multidownload\.[^\s"\'<>`]+', cloud_res.text, re.IGNORECASE)
    if dl_match:
        return extract_nested_url(dl_match.group(0))

    # Fallback: Parse anchors within the locker page
    cloud_soup = BeautifulSoup(cloud_res.text, 'html.parser')
    for a in cloud_soup.find_all(['a', 'link'], href=True):
        href = a['href'].strip()
        if 'multidownload.' in href.lower() or any(ext in href.lower() for ext in ['.mp4', '.mkv', '.m3u8']):
            return extract_nested_url(href)

    return None

def process_movie(movie_url, group_name):
    # Step 1: Open movie page
    res = safe_request(movie_url)
    if not res:
        return None

    soup = BeautifulSoup(res.text, 'html.parser')

    # Step 2: Extract movie title
    title_elem = soup.select_one('.mp-title') or soup.find('h1')
    title = title_elem.get_text(strip=True) if title_elem else "Unknown Movie"
    title = re.sub(r'[\r\n\t]+', ' ', title).strip()

    # Step 3: Extract poster image
    poster = ""
    img_wrap = soup.select_one('.mp-img-wrap')
    if img_wrap:
        img = img_wrap if img_wrap.name == 'img' else img_wrap.find('img')
        if img:
            poster = img.get('src') or img.get('data-src') or img.get('data-lazy-src') or ""
            if poster.startswith('//'):
                poster = f"https:{poster}"

    # Step 4: Locate cloud link / locker candidate
    candidate_links = find_cloud_links(res.text, soup)
    if not candidate_links:
        return None

    # Step 5: Resolve candidate to actual stream URL
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
    url = urljoin(BASE_URL, category_path) if page_num == 1 else urljoin(BASE_URL, f"{category_path.rstrip('/')}/page/{page_num}/")
    movie_links = []
    seen_on_page = set()

    res = safe_request(url)
    if not res:
        return []

    soup = BeautifulSoup(res.text, 'html.parser')

    # Target standard movie post cards (typically 24 cards = 12 x 2)
    post_items = soup.select(
        '.thumb-content, .item-list, article.post, .mp-post, .thumb, div[class*="movie-item"], div[class*="post-item"]'
    )

    if post_items:
        for item in post_items:
            link_tag = item.find('a', href=True)
            if link_tag:
                full = urljoin(BASE_URL, link_tag['href'].strip())
                if full not in seen_on_page and not re.search(r'/(category|tag|author|page)/', full, re.IGNORECASE):
                    seen_on_page.add(full)
                    movie_links.append(full)
    else:
        main_container = soup.select_one('#content, #main, .site-main, .movies-list, body')
        if main_container:
            for a in main_container.find_all('a', href=True):
                full = urljoin(BASE_URL, a['href'].strip())
                if full.startswith(BASE_URL) and not re.search(r'/(category|tag|author|page|dmca|contact|about)/', full, re.IGNORECASE):
                    if full.rstrip('/') != BASE_URL.rstrip('/') and full not in seen_on_page:
                        seen_on_page.add(full)
                        movie_links.append(full)

    return movie_links

def main():
    print(f"🚀 Scanning {len(CATEGORIES)} categories ({PAGES_PER_CATEGORY} pages each)...", flush=True)

    discovered_movies = []
    seen_urls = set()

    # Phase 1: Collect movie links across categories and pages
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as page_executor:
        future_map = {
            page_executor.submit(scan_category_page, cat["category_path"], p): (cat["group_name"], p)
            for cat in CATEGORIES
            for p in range(1, PAGES_PER_CATEGORY + 1)
        }

        # Keep original category and page sequencing
        for fut in sorted(future_map.keys(), key=lambda f: (future_map[f][0], future_map[f][1])):
            group_name, page_num = future_map[fut]
            try:
                urls = fut.result()
                count = 0
                for u in urls:
                    if u not in seen_urls:
                        seen_urls.add(u)
                        discovered_movies.append((u, group_name))
                        count += 1
                print(f"[{group_name}] Page {page_num}: Found {count} items", flush=True)
            except Exception:
                pass

    print(f"\n⚡ Total discovered items: {len(discovered_movies)}. Resolving stream URLs...", flush=True)

    # Phase 2: Process movie entries concurrently
    m3u_entries = []
    lock = threading.Lock()
    failed_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as movie_executor:
        futures = {
            movie_executor.submit(process_movie, url, group): url
            for url, group in discovered_movies
        }

        for fut in concurrent.futures.as_completed(futures):
            try:
                res = fut.result()
                with lock:
                    if res:
                        entry, title = res
                        m3u_entries.append(entry)
                        print(f"   ⚡ Added ({len(m3u_entries)}): {title[:55]}", flush=True)
                    else:
                        failed_count += 1
            except Exception:
                with lock:
                    failed_count += 1

    # Phase 3: Write clean M3U
    ist_time = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now_str = ist_time.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    print(f"\n💾 Summary: {len(m3u_entries)} parsed, {failed_count} skipped/unreleased.")
    print(f"Writing to {OUTPUT_FILE}...", flush=True)
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write(f'# Playlist Generated Automatically | Total: {len(m3u_entries)}\n')
        f.write(f'# Last Updated: {now_str}\n\n')
        for entry in m3u_entries:
            f.write(entry)

    print(f"🎉 Complete! Saved to {OUTPUT_FILE}", flush=True)

if __name__ == "__main__":
    main()
