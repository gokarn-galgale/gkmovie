import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

BASE_URL = "https://prmovies.energy"
PLAYLIST_NAME = "prmovies.energy"
OUTPUT_FILE = "prmovies_playlist.m3u"
PAGES_TO_SCAN = 2

CATEGORIES = [
    {"url": f"{BASE_URL}/bollywood-movies-on-prmovies/", "group_name": "Bollywood Movies"}
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def get_movie_links(page, category_url, max_pages=2):
    movie_links = []
    # Blacklisted paths that are navigation/system links, not individual movies
    ignore_patterns = [
        "/bollywood-movies", "/genre/", "/tag/", "/category/", "/year/", 
        "/page/", "/contact", "/disclaimer", "/dmca", "#", "facebook.com", "telegram"
    ]

    for p in range(1, max_pages + 1):
        target_url = f"{category_url}page/{p}/" if p > 1 else category_url
        print(f"[*] Navigating catalog page: {target_url}")
        try:
            page.goto(target_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(4000)

            # Check if Cloudflare challenged the page
            page_title = page.title()
            print(f"    Page loaded with title: '{page_title}'")
            if "just a moment" in page_title.lower() or "attention required" in page_title.lower():
                print("    [!] Cloudflare challenge detected. Waiting for automated solve...")
                page.wait_for_timeout(6000)

            # Strategy 1: Select cards directly from movie grid containers (DooPlay / WordPress)
            card_anchors = page.query_selector_all(".items article a, .poster a, .item a, .movies a")
            
            # Strategy 2: Fallback to any relative/internal anchor on the page
            if not card_anchors:
                card_anchors = page.query_selector_all("a[href]")

            for a in card_anchors:
                href = a.get_attribute("href")
                if not href:
                    continue

                full = href if href.startswith("http") else urljoin(BASE_URL, href)
                
                # Must belong to site and not be in the blacklist
                if BASE_URL in full and not any(neg in full.lower() for neg in ignore_patterns):
                    # Ensure it is an actual content link
                    clean_slug = full.replace(BASE_URL, "").strip("/")
                    if clean_slug and "/" not in clean_slug and full not in movie_links:
                        movie_links.append(full)

        except Exception as e:
            print(f"[!] Error loading catalog page {target_url}: {e}")
            break

    return list(set(movie_links))

def extract_stream_from_speedo(context, speedo_url):
    """Opens host page, handles intermediate proceed button, and extracts direct video link."""
    stream_url = None
    page = context.new_page()

    def intercept_response(response):
        nonlocal stream_url
        url = response.url
        if any(ext in url.lower() for ext in [".m3u8", ".mp4"]) and not stream_url:
            if "speedo" in url or "cdn" in url or "video" in url or "hls" in url:
                stream_url = url

    page.on("response", intercept_response)

    try:
        print(f"    [-] Loading host page: {speedo_url}")
        page.goto(speedo_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)

        # Handle 'Proceed to video' gateway
        proceed_btn = page.locator("text=/proceed to video/i, text=/proceed/i, button:has-text('Proceed')").first
        if proceed_btn.is_visible(timeout=5000):
            print("    [-] Clicking 'Proceed to video'...")
            proceed_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)

        # Trigger player play button if stream hasn't been intercepted yet
        if not stream_url:
            play_btn = page.locator(".jw-display-icon-container, video, .vjs-big-play-button, .play-button").first
            if play_btn.is_visible(timeout=5000):
                print("    [-] Triggering play button...")
                play_btn.click(force=True)
                page.wait_for_timeout(4000)

    except Exception as e:
        print(f"    [!] Host interception warning: {e}")
    finally:
        page.close()

    return stream_url

def process_movie(context, movie_url, group_name):
    page = context.new_page()
    try:
        print(f"[+] Processing movie: {movie_url}")
        page.goto(movie_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)

        # Title Extraction
        title_el = page.query_selector("h1.entry-title, h1")
        title = title_el.inner_text().strip() if title_el else page.title()

        # Poster Extraction
        poster_el = page.query_selector("meta[property='og:image']")
        poster = poster_el.get_attribute("content") if poster_el else ""

        # Find speedostream link in download table/section
        speedo_link = None
        anchors = page.query_selector_all("a[href]")
        for a in anchors:
            href = a.get_attribute("href")
            if href and "speedostream" in href.lower():
                speedo_link = href
                break

        page.close()

        if not speedo_link:
            print("    [x] No speedostream link found.")
            return None

        # Resolve media link from host
        playable_url = extract_stream_from_speedo(context, speedo_link)
        if not playable_url:
            print("    [x] Failed to extract video stream.")
            return None

        final_stream = f"{playable_url}|Referer={speedo_link}&User-Agent={USER_AGENT}"
        extinf = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{group_name}", {title}'
        print(f"    [✔] Success: {title}")
        return f"{extinf}\n{final_stream}\n"

    except Exception as e:
        print(f"    [!] Error on {movie_url}: {e}")
        try:
            page.close()
        except:
            pass
        return None

def main():
    print("🚀 Starting Stealth Playwright Scraper on GitHub Actions...")
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080"
            ]
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-US"
        )

        # Stealth evasion: strip the automated driver flag
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        catalog_page = context.new_page()
        for cat in CATEGORIES:
            movies = get_movie_links(catalog_page, cat["url"], max_pages=PAGES_TO_SCAN)
            print(f"Found {len(movies)} candidate movie URLs for {cat['group_name']}.")

            for movie_url in movies:
                entry = process_movie(context, movie_url, cat["group_name"])
                if entry:
                    results.append(entry)

        browser.close()

    bd_time = datetime.now(timezone.utc) + timedelta(hours=6)
    timestamp = bd_time.strftime("%Y-%m-%d %I:%M:%S %p (BD Time)")

    print(f"\n💾 Writing {len(results)} movies to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U x-tvg-url="" x-tvg-name="{PLAYLIST_NAME}"\n')
        f.write(f'#PLAYLIST:{PLAYLIST_NAME}\n')
        f.write(f'# Playlist Name: {PLAYLIST_NAME}\n')
        f.write(f'# Last Updated: {timestamp}\n\n')
        for r in results:
            f.write(r)

    print("✅ Completed.")

if __name__ == "__main__":
    main()
