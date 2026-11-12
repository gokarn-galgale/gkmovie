import asyncio
import csv
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import unquote, urljoin

import aiohttp
from bs4 import BeautifulSoup

# --- Configuration ---
BASE_URL = "https://go1.india4movies.click/"
OUTPUT_M3U = "all_movies.m3u"
MISSING_REPORT_FILE = "missing_movies_report.tsv"

PAGES_PER_CATEGORY = 15
MAX_CONCURRENT_WORKERS = 10
REQUEST_CONCURRENCY = 12          # global cap on in-flight HTTP requests
REQUEST_TIMEOUT = 12
MAX_RETRIES = 2
RETRYABLE_STATUS = frozenset({403, 429, 502, 503, 504})

CATEGORIES = [
    {"group_name": "Hollywood Hindi Movies", "category_path": "/category/hollywood-hindi-movies/"},
    {"group_name": "Marathi Movies",          "category_path": "/category/marathi-movies/"},
    {"group_name": "Bollywood Movies",        "category_path": "/category/bollywood-movies-download/"},
    {"group_name": "South Dubbed Movies",     "category_path": "/category/south-indian-hindi-dubbed-movies/"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CLOUD_PATTERN     = re.compile(r'https?://[^\s"\'<>`]*multicloudlinks\.[^\s"\'<>`]+', re.I)
DOWNLOAD_PATTERN  = re.compile(r'https?://[^\s"\'<>`]+multidownload\.[^\s"\'<>`]+', re.I)
BLOCKED_PATH_PATT = re.compile(r'/(category|tag|author|page)/', re.I)
URL_TRAILING_JUNK = '\\";),'

_STOP = object()                  # sentinel telling a worker to exit
logger = logging.getLogger("scraper")


@dataclass
class Stats:
    added: int = 0
    missing: int = 0
    pages_failed: int = 0


def clean_url(u: str) -> str:
    return u.rstrip(URL_TRAILING_JUNK)


def sanitize_m3u(s: str) -> str:
    """Strip characters that would break M3U attribute / line syntax."""
    return s.replace('"', "'").replace("\n", " ").replace("\r", " ").strip()


async def fetch_html(session, sem, url, referer=None):
    """
    Returns (status_code, text_or_None).

    - status 200 -> (200, html)
    - retryable error on last attempt -> (status, None)
    - non-retryable HTTP error -> (status, None)
    - every attempt raised -> (None, None)
    """
    req_headers = dict(HEADERS)
    if referer:
        req_headers["Referer"] = referer

    async with sem:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                async with session.get(url, headers=req_headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        return 200, await resp.text(errors="ignore")
                    if resp.status in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                        await asyncio.sleep(1.5 * attempt)
                        continue
                    return resp.status, None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.debug("Fetch error (attempt %d/%d) for %s: %s", attempt, MAX_RETRIES, url, e)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1.0 * attempt)
    return None, None


async def record_missing(tsv_writer, tsv_file, lock, stats, group, title, url, reason, extra=""):
    async with lock:
        tsv_writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            group, title, url, reason, extra,
        ])
        tsv_file.flush()
        stats.missing += 1


async def scan_category(session, sem, cat, queue, seen_urls,
                        tsv_writer, tsv_file, tsv_lock, stats):
    clean_cat  = cat["category_path"].strip("/")
    group_name = cat["group_name"]

    for page_num in range(1, PAGES_PER_CATEGORY + 1):
        url = (f"{BASE_URL}/{clean_cat}/" if page_num == 1
               else f"{BASE_URL}/{clean_cat}/page/{page_num}/")

        status, html = await fetch_html(session, sem, url)

        if status == 404:                     # clean end of pagination
            logger.info("[%s] End of pagination at page %d", group_name, page_num)
            break
        if not html:
            stats.pages_failed += 1
            await record_missing(tsv_writer, tsv_file, tsv_lock, stats,
                                 group_name, f"Category Page {page_num}", url,
                                 "Category Page Fetch Failed", f"status={status}")
            break

        soup     = BeautifulSoup(html, "html.parser")
        articles = soup.select('article[id^="post-"]') or soup.select("article.post")
        if not articles:
            logger.info("[%s] No articles on page %d – stopping", group_name, page_num)
            break

        for article in articles:
            title_tag = article.select_one("h2.entry-title a, h2.blog-entry-title a")
            if not title_tag or not title_tag.get("href"):
                continue
            full_url = urljoin(BASE_URL, title_tag["href"].strip())
            if full_url in seen_urls or BLOCKED_PATH_PATT.search(full_url):
                continue
            seen_urls.add(full_url)
            await queue.put({"url": full_url, "group": group_name})

        await asyncio.sleep(0.1)


async def resolve_movie_worker(session, sem, queue,
                               m3u_handle, m3u_lock,
                               tsv_writer, tsv_file, tsv_lock, stats):
    while True:
        movie = await queue.get()
        try:
            if movie is _STOP:
                return
            await resolve_movie_stream(session, sem, movie,
                                       m3u_handle, m3u_lock,
                                       tsv_writer, tsv_file, tsv_lock, stats)
        except Exception:
            logger.exception("Worker crashed processing %s", movie)
        finally:
            queue.task_done()


async def resolve_movie_stream(session, sem, movie,
                               m3u_handle, m3u_lock,
                               tsv_writer, tsv_file, tsv_lock, stats):
    group      = movie["group"]
    movie_url  = movie["url"]

    # Step 1 — detail page
    status, page_html = await fetch_html(session, sem, movie_url)
    if not page_html:
        await record_missing(tsv_writer, tsv_file, tsv_lock, stats, group, "N/A", movie_url,
                             "Movie Detail Page Unreachable", f"status={status}")
        return

    soup = BeautifulSoup(page_html, "html.parser")

    # Step 2 — title
    title_elem = soup.select_one(".mp-title")
    if not title_elem:
        await record_missing(tsv_writer, tsv_file, tsv_lock, stats, group, "N/A", movie_url,
                             "No .mp-title Found")
        return
    title = re.sub(r"\s+", " ", title_elem.get_text(" ", strip=True)).strip()

    # Step 3 — poster
    poster = ""
    img_wrap = soup.select_one(".mp-img-wrap")
    if img_wrap:
        img = img_wrap if img_wrap.name == "img" else img_wrap.find("img")
        if img:
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
            if src:
                poster = f"https:{src}" if src.startswith("//") else src

    # Step 4 — multicloud link (regex first, DOM fallback)
    multicloud_url: Optional[str] = None
    m = CLOUD_PATTERN.search(page_html)
    if m:
        multicloud_url = clean_url(m.group(0))
    else:
        for tag in soup.find_all(["a", "iframe"]):
            target = tag.get("href") or tag.get("src")
            if target and "multicloudlinks." in target.lower():
                multicloud_url = target.strip()
                break

    if not multicloud_url:
        alt = next(
            (a["href"] for a in soup.find_all("a", href=True)
             if any(k in a["href"].lower()
                    for k in ("cloud", "download", "drive", "hub", "fast"))),
            "No alternative link found",
        )
        await record_missing(tsv_writer, tsv_file, tsv_lock, stats, group, title, movie_url,
                             "No MultiCloud Link Found", alt)
        return

    # Step 5 — multicloud page
    status, cloud_html = await fetch_html(session, sem, multicloud_url, referer=movie_url)
    if not cloud_html:
        await record_missing(tsv_writer, tsv_file, tsv_lock, stats, group, title, movie_url,
                             "MultiCloud Page Unreachable",
                             f"{multicloud_url} (status={status})")
        return

    # Step 6 — stream link
    stream_link: Optional[str] = None
    m = DOWNLOAD_PATTERN.search(cloud_html)
    if m:
        candidate = clean_url(m.group(0))
        if "url=" in candidate:
            param = re.search(r"[?&]url=([^&]+)", candidate)
            if param:
                candidate = unquote(param.group(1))
        stream_link = candidate
    else:
        cloud_soup = BeautifulSoup(cloud_html, "html.parser")
        for a in cloud_soup.find_all(["a", "link"]):
            target = (a.get("href") or "").strip()
            if "multidownload." in target.lower():
                stream_link = target
                break

    if not stream_link:
        await record_missing(tsv_writer, tsv_file, tsv_lock, stats, group, title, movie_url,
                             "No MultiDownload Link in MultiCloud Page", multicloud_url)
        return

    # Step 7 — emit M3U entry
    entry = (
        f'#EXTINF:-1 tvg-logo="{sanitize_m3u(poster)}" '
        f'group-title="{sanitize_m3u(group)}", {sanitize_m3u(title)}\n'
        f"{stream_link}\n"
    )
    async with m3u_lock:
        m3u_handle.write(entry)
        m3u_handle.flush()
        stats.added += 1
        n = stats.added
    logger.info("[%d] Added: %s", n, title[:60])


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    stats      = Stats()
    seen_urls: set[str]        = set()
    queue:     asyncio.Queue   = asyncio.Queue(maxsize=100)

    ist     = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    now_str = ist.strftime("%Y-%m-%d %I:%M:%S %p (IST)")

    with open(OUTPUT_M3U, "w", encoding="utf-8") as m3u_f, \
         open(MISSING_REPORT_FILE, "w", encoding="utf-8", newline="") as report_f:

        m3u_f.write('#EXTM3U x-tvg-url=""\n')
        m3u_f.write("# Playlist Generated Automatically\n")
        m3u_f.write(f"# Last Updated: {now_str}\n\n")
        m3u_f.flush()

        tsv_writer = csv.writer(report_f, delimiter="\t")
        tsv_writer.writerow(["Timestamp", "Category", "Title", "Movie_URL",
                             "Failure_Reason", "Detected_Alternate_URL"])
        report_f.flush()

        m3u_lock = asyncio.Lock()
        tsv_lock = asyncio.Lock()
        sem      = asyncio.Semaphore(REQUEST_CONCURRENCY)

        connector = aiohttp.TCPConnector(limit=REQUEST_CONCURRENCY, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:

            workers = [
                asyncio.create_task(
                    resolve_movie_worker(session, sem, queue,
                                         m3u_f, m3u_lock,
                                         tsv_writer, report_f, tsv_lock, stats)
                )
                for _ in range(MAX_CONCURRENT_WORKERS)
            ]

            # Scanners must not abort the whole run on a single failure
            scanner_results = await asyncio.gather(
                *(scan_category(session, sem, cat, queue, seen_urls,
                                tsv_writer, report_f, tsv_lock, stats)
                  for cat in CATEGORIES),
                return_exceptions=True,
            )
            for r in scanner_results:
                if isinstance(r, BaseException):
                    logger.error("Scanner failed: %r", r)

            # Graceful shutdown: sentinel per worker, then wait for all work to finish
            for _ in workers:
                await queue.put(_STOP)
            await queue.join()
            await asyncio.gather(*workers, return_exceptions=True)

    logger.info("Run complete — added=%d missing=%d page_errors=%d — report: %s",
                stats.added, stats.missing, stats.pages_failed, MISSING_REPORT_FILE)


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            pass
    asyncio.run(main())
