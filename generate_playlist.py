import os
import re
import urllib.parse
from collections import deque
import requests
from bs4 import BeautifulSoup

BASE_URL = "http://103.225.94.27/Infobase/"
OUTPUT_FILE = "playlist.m3u"
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v")

# Categories to match (case-insensitive)
CATEGORY_RULES = [
    ("Hindi Dubbed", re.compile(r"hindi\s*dub", re.IGNORECASE)),
    ("South Dubbed", re.compile(r"south\s*dub", re.IGNORECASE)),
    ("Hindi", re.compile(r"\bhindi\b", re.IGNORECASE)),
    ("English", re.compile(r"\benglish\b", re.IGNORECASE)),
]


def detect_category(path_str: str) -> str | None:
    decoded = urllib.parse.unquote(path_str)
    for category_name, regex in CATEGORY_RULES:
        if regex.search(decoded):
            return category_name
    return None


def clean_title(filename: str) -> str:
    decoded = urllib.parse.unquote(filename)
    base_name, _ = os.path.splitext(decoded)
    return re.sub(r"[._]", " ", base_name).strip()


def normalize_url(url: str) -> str:
    """Strips query parameters and fragments to eliminate duplicate crawls."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def run_crawler():
    session = requests.Session()
    # Adapter with 0 retries and strict timeout to avoid hanging
    adapter = requests.adapters.HTTPAdapter(max_retries=1)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*"
    })

    print(f"Connecting to: {BASE_URL}")

    # Connectivity probe with a strict 8-second timeout
    try:
        res = session.get(BASE_URL, timeout=(5, 8))
        print(f"Server replied with HTTP status: {res.status_code}")
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Failed to connect to server: {e}")
        print("The server is unreachable or dropping cloud IP connections. Exiting to avoid hanging.")
        write_m3u([], OUTPUT_FILE)
        return

    # Use BFS (Queue) instead of deep recursion to prevent stack and loop issues
    queue = deque([(BASE_URL, None)])
    visited = set()
    videos = []
    
    max_dirs_to_crawl = 2000  # Hard circuit breaker
    dirs_checked = 0

    while queue and dirs_checked < max_dirs_to_crawl:
        current_url, current_category = queue.popleft()
        norm_url = normalize_url(current_url)

        if norm_url in visited:
            continue
        visited.add(norm_url)
        dirs_checked += 1

        print(f"[{dirs_checked}] Crawling: {norm_url}")

        try:
            # (connect timeout: 5s, read timeout: 8s)
            response = session.get(norm_url, timeout=(5, 8))
            if response.status_code != 200:
                continue
        except requests.RequestException:
            print(f"Timeout/Error loading {norm_url}, skipping...")
            continue

        soup = BeautifulSoup(response.text, "html.parser")

        for link in soup.find_all("a", href=True):
            href = link.get("href").strip()

            # Ignore relative parent paths, query sorting, anchors
            if href in ("../", "./", "/") or href.startswith("?") or href.startswith("#"):
                continue

            target_url = urllib.parse.urljoin(norm_url, href)
            target_norm = normalize_url(target_url)

            # Keep inside Infobase
            if not target_norm.startswith(BASE_URL):
                continue

            # Update category context
            matched_category = detect_category(target_norm) or current_category

            parsed_path = urllib.parse.urlsplit(target_norm).path
            is_video = any(parsed_path.lower().endswith(ext) for ext in VIDEO_EXTS)

            if is_video:
                if matched_category:
                    filename = os.path.basename(parsed_path)
                    title = clean_title(filename)
                    videos.append({
                        "title": title,
                        "url": target_norm,
                        "category": matched_category
                    })
            else:
                # Directory to traverse
                if target_norm not in visited and (href.endswith("/") or "." not in parsed_path.split("/")[-1]):
                    queue.append((target_norm, matched_category))

    print(f"\nDiscovered {len(videos)} matching items across {dirs_checked} directories.")
    videos.sort(key=lambda x: (x["category"], x["title"].lower()))
    write_m3u(videos, OUTPUT_FILE)


def write_m3u(entries: list, filepath: str):
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n\n")
        for item in entries:
            f.write(f'#EXTINF:-1 group-title="{item["category"]}" tvg-name="{item["title"]}",{item["title"]}\n')
            f.write(f"{item['url']}\n\n")
    print(f"File saved to {filepath}")


if __name__ == "__main__":
    run_crawler()
