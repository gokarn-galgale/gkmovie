import os
import re
import urllib.parse
from collections import deque
import requests
from bs4 import BeautifulSoup

BASE_URL = "http://103.225.94.27/Infobase/"
OUTPUT_FILE = "playlist.m3u"
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v")

# Exclusion filter: discard folders starting with or containing drama* or dub*
EXCLUDE_PATTERN = re.compile(r"(dub|drama)", re.IGNORECASE)

# Inclusion rules: subfolders starting with the target name
CATEGORY_RULES = [
    ("Hindi", re.compile(r"^hindi", re.IGNORECASE)),
    ("English", re.compile(r"^english", re.IGNORECASE)),
    ("Kids", re.compile(r"^kids", re.IGNORECASE)),
]


def classify_folder(folder_name: str) -> str | None:
    """Checks if a folder starts with Hindi, English, or Kids, while excluding drama* and dub*."""
    clean_name = urllib.parse.unquote(folder_name).strip().strip("/")
    
    # 1. Skip if it contains 'drama' or 'dub'
    if EXCLUDE_PATTERN.search(clean_name):
        return None

    # 2. Check if it starts with one of the allowed prefixes
    for category_name, pattern in CATEGORY_RULES:
        if pattern.search(clean_name):
            return category_name

    return None


def clean_title(filename: str) -> str:
    """Decodes URL encoding and cleans extensions and separators."""
    decoded = urllib.parse.unquote(filename)
    base_name, _ = os.path.splitext(decoded)
    return re.sub(r"[._]", " ", base_name).strip()


def normalize_url(url: str) -> str:
    """Strips query parameters (?C=N;O=D) and anchors to avoid looping."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def run_crawler():
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=1)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*"
    })

    print(f"Connecting to base URL: {BASE_URL}")

    # Connectivity probe with short timeout
    try:
        res = session.get(BASE_URL, timeout=(5, 8))
        print(f"Connected successfully (HTTP {res.status_code})")
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Failed to connect: {e}")
        write_m3u([], OUTPUT_FILE)
        return

    # Queue contains: (url, current_category)
    queue = deque([(BASE_URL, None)])
    visited = set()
    videos = []

    dirs_scanned = 0
    max_dirs = 3000  # Safety circuit breaker

    while queue and dirs_scanned < max_dirs:
        current_url, current_category = queue.popleft()
        norm_url = normalize_url(current_url)

        if norm_url in visited:
            continue
        visited.add(norm_url)
        dirs_scanned += 1

        try:
            response = session.get(norm_url, timeout=(5, 8))
            if response.status_code != 200:
                continue
        except requests.RequestException:
            continue

        soup = BeautifulSoup(response.text, "html.parser")

        for link in soup.find_all("a", href=True):
            href = link.get("href").strip()

            # Skip Apache sort links, parent directories, and anchors
            if href in ("../", "./", "/") or href.startswith("?") or href.startswith("#"):
                continue

            target_url = urllib.parse.urljoin(norm_url, href)
            target_norm = normalize_url(target_url)

            if not target_norm.startswith(BASE_URL):
                continue

            parsed_path = urllib.parse.urlsplit(target_norm).path
            is_video = any(parsed_path.lower().endswith(ext) for ext in VIDEO_EXTS)

            if is_video:
                # Add video only if it is inside an active, accepted category
                if current_category:
                    title = clean_title(os.path.basename(parsed_path))
                    videos.append({
                        "title": title,
                        "url": target_norm,
                        "category": current_category
                    })
                    print(f"[{current_category}] {title}")
            else:
                # It's a directory
                folder_name = href.strip("/")
                
                # If not inside a category yet, check if this folder initiates one
                if not current_category:
                    assigned_category = classify_folder(folder_name)
                    # If this folder is excluded (e.g. drama/dub), do NOT enter it
                    if EXCLUDE_PATTERN.search(urllib.parse.unquote(folder_name)):
                        continue
                else:
                    # Already inside a category, but ensure subfolder itself isn't a excluded drama/dub
                    if EXCLUDE_PATTERN.search(urllib.parse.unquote(folder_name)):
                        continue
                    assigned_category = current_category

                # Queue the directory to crawl
                if target_norm not in visited:
                    queue.append((target_norm, assigned_category))

    print(f"\nDiscovered {len(videos)} matching items across {dirs_scanned} directories.")
    videos.sort(key=lambda x: (x["category"], x["title"].lower()))
    write_m3u(videos, OUTPUT_FILE)


def write_m3u(entries: list, filepath: str):
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n\n")
        for item in entries:
            f.write(f'#EXTINF:-1 group-title="{item["category"]}" tvg-name="{item["title"]}",{item["title"]}\n')
            f.write(f"{item['url']}\n\n")
    print(f"File successfully written to {filepath}")


if __name__ == "__main__":
    run_crawler()
