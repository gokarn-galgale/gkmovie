import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

BASE_URL = "https://prmovies.energy"
PLAYLIST_NAME = "prmovies.energy"
OUTPUT_FILE = "prmovies_playlist.m3u"
PAGES_TO_SCAN = 2  # Keep small to avoid GitHub Actions timeout

CATEGORIES = [
    {"url": f"{BASE_URL}/bollywood-movies-on-prmovies/", "group_name": "Bollywood Movies"}
]

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def get_movie_links(page, category_url, max_pages=2):
    movie_links = []
    for p in range(1, max_pages + 1):
        target_url = f"{category_url}page/{p}/" if p > 1 else category_url
        print(f"[*] Navigating catalog page: {target_url}")
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)

            anchors = page.query_selector_all("a[href]")
            for a in anchors:
                href = a.get_attribute("href")
                if href and ("/movie/" in href or "/movies/" in href):
                    full = href if href.startswith("http") else urljoin(BASE_URL, href)
                    if full not in movie_links and category_url not in full:
                        movie_links.append(full)
        except Exception as e:
            print(f"[!] Error loading catalog page {target_url}: {e}")
            break
    return list(set(movie_links))

def extract_stream_from_speedo(context, speedo_url):
    """Opens the speedostream link, clicks 'Proceed to video', and intercepts JW Player stream."""
    stream_url = None
    page = context.new_page()

    def intercept_response(response):
        nonlocal stream_url
        url = response.url
        if any(ext in url for ext in [".m3u8", ".mp4"]) and not stream_url:
            if "speedostream" in url or "cdn" in url or "video" in url:
                stream_url = url

    page.on("response", intercept_response)

    try:
        print(f"    [-] Loading host page: {speedo_url}")
        page.goto(speedo_url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(3000)

        # Look for the 'Proceed to video' button or form button
        proceed_btn = page.locator("text=/proceed to video/i").first
        if proceed_btn.is_visible(timeout=5000):
            print("    [-] Clicking 'Proceed to video'...")
            proceed_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)

        # Click the JW Player play button if stream hasn't been intercepted yet
        if not stream_url:
            play_btn = page.locator(".jw-display-icon-container, video, .vjs-big-play-button").first
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
    """Scrapes movie detail page, finds speedostream link in download section, and gets video."""
    page = context.new_page()
    try:
        print(f"[+] Processing movie: {movie_url}")
        page.goto(movie_url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(2000)

        # Get Title
        title = page.title()
        title_el = page.query_selector("h1")
        if title_el:
            title = title_el.inner_text().strip()

        # Get Poster
        poster_el = page.query_selector("meta[property='og:image']")
        poster = poster_el.get_attribute("content") if poster_el else ""

        # Find speedostream1 link in the download/links section
        speedo_link = None
        anchors = page.query_selector_all("a[href]")
        for a in anchors:
            href = a.get_attribute("href")
            if href and "speedostream" in href:
                speedo_link = href
                break

        page.close()

        if not speedo_link:
            print("    [x] No speedostream link found on page.")
            return None

        # Resolve stream through speedostream
        playable_url = extract_stream_from_speedo(context, speedo_link)
        if not playable_url:
            print("    [x] Failed to extract playable media URL.")
            return None

        final_stream = f"{playable_url}|Referer={speedo_link}&User-Agent={USER_AGENT}"
        extinf = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{group_name}", {title}'
        print(f"    [✔] Success: {title}")
        return f"{extinf}\n{final_stream}\n"

    except Exception as e:
        print(f"    [!] Error on movie {movie_url}: {e}")
        try:
            page.close()
        except:
            pass
        return None

def main():
    print("🚀 Starting Playwright Scraper on GitHub Actions...")
    results = []

    with sync_playwright() as p:
        # Launch real browser instance to pass Cloudflare checks
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        )
        context = browser.new_context(user_agent=USER_AGENT)

        for cat in CATEGORIES:
            movies = get_movie_links(context.new_page(), cat["url"], max_pages=PAGES_TO_SCAN)
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
        f.write(f'# Last Updated: {timestamp}\n\n')
        for r in results:
            f.write(r)

    print("✅ Completed.")

if __name__ == "__main__":
    main()
