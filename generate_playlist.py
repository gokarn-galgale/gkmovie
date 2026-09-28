import os
import re
import sys
import urllib.parse
from collections import deque
import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

BASE_URL = "http://103.225.94.27/Infobase/"
OUTPUT_FILE = "playlist.m3u"
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v")

# Explicit exclusions: completely discard folders matching drama* or dub*
EXCLUDE_PATTERN = re.compile(r"(dub|drama)", re.IGNORECASE)

# Target categories: subfolders starting with Hindi, English, or Kids
CATEGORY_RULES = [
    ("Hindi", re.compile(r"^hindi", re.IGNORECASE)),
    ("English", re.compile(r"^english", re.IGNORECASE)),
    ("Kids", re.compile(r"^kids", re.IGNORECASE)),
]


def clean_url(url: str) -> str:
    """Removes query params (?C=N;O=D), fragments (#), and strips trailing slashes for clean matching."""
    parts = urllib.parse.urlsplit(url)
    clean_path = parts.path
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, clean_path, "", ""))


def classify_folder(name: str) -> str | None:
    """Matches folder name starting with Hindi, English, Kids while excluding drama/dub."""
    clean = urllib.parse.unquote(name).strip().strip("/")
    
    if EXCLUDE_PATTERN.search(clean):
        return None

    for cat_name, pattern in CATEGORY_RULES:
        if pattern.search(clean):
            return cat_name
    return None


def clean_title(filename: str) -> str:
    decoded = urllib.parse.unquote(filename)
    base_name, _ = os.path.splitext(decoded)
    return re.sub(r"[._]", " ", base_name).strip()


def run_crawler():
    session = requests.Session()
    
    # Aggressive fail-fast adapter: 0 retries to avoid hanging on slow/dead sockets
    adapter = HTTPAdapter(max_retries=0, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)",
        "Connection": "close",
    })

    print(f"Connecting to base URL: {BASE_URL}", flush=True)

    # Fast connection check (3s connect timeout, 5s read timeout)
    try:
        res = session.get(BASE_URL, timeout=(3.0, 5.0))
        print(f"Connected successfully (HTTP {res.status_code})", flush=True)
    except Exception as e:
        print(f"[!] Target server unreachable: {e}", flush=True)
        write_m3u([], OUTPUT_FILE)
        return

    # Queue structure: (url, current_category, current_depth)
    queue = deque([(BASE_URL, None, 0)])
    visited_paths = set()
    videos = []

    dirs_scanned = 0
    MAX_DIRS = 800     # Hard stop limit
    MAX_DEPTH = 5      # Never go deeper than 5 folders (prevents recursive symlinks)

    while queue and dirs_scanned < MAX_DIRS:
        curr_url, curr_cat, depth = queue.popleft()
        
        # Canonical path check to avoid symlink & trailing-slash cycles
        parsed_curr = urllib.parse.urlsplit(curr_url)
        norm_path = os.path.normpath(urllib.parse.unquote(parsed_curr.path))

        if norm_path in visited_paths or depth > MAX_DEPTH:
            continue
        visited_paths.add(norm_path)
        dirs_scanned += 1

        print(f"[{dirs_scanned}] Scanning (Depth {depth}): {norm_path}", flush=True)

        try:
            # Strict per-request timeouts (3s to connect, 5s to read HTML)
            response = session.get(curr_url, timeout=(3.0, 5.0))
            if response.status_code != 200:
                continue
            html_text = response.text
        except requests.RequestException:
            print(f"  --> Timeout on {norm_path}, skipping branch.", flush=True)
            continue

        soup = BeautifulSoup(html_text, "html.parser")

        for link in soup.find_all("a", href=True):
            raw_href = link.get("href").strip()

            # 1. KILL ALL APACHE SORT QUERY STRINGS & ANCHORS
            if "?" in raw_href or "#" in raw_href:
                continue

            # 2. KILL PARENT AND ROOT LINKS
            if raw_href in ("../", "./", "/", ""):
                continue

            # 3. Discard 'Parent Directory' text labels
            if "parent directory" in link.text.lower():
                continue

            target_url = urllib.parse.urljoin(curr_url, raw_href)
            target_url = clean_url(target_url)

            # Ensure we stay within /Infobase/
            if not target_url.startswith(BASE_URL):
                continue

            parsed_target = urllib.parse.urlsplit(target_url)
            target_norm_path = os.path.normpath(urllib.parse.unquote(parsed_target.path))

            if target_norm_path in visited_paths:
                continue

            folder_or_file_name = os.path.basename(target_norm_path)
            is_video = any(target_norm_path.lower().endswith(ext) for ext in VIDEO_EXTS)

            if is_video:
                if curr_cat:
                    title = clean_title(folder_or_file_name)
                    videos.append({
                        "title": title,
                        "url": target_url,
                        "category": curr_cat
                    })
                    print(f"  [+] ({curr_cat}) {title}", flush=True)
            else:
                # Directory branch
                folder_clean = folder_or_file_name

                # Reject any folder containing drama or dub
                if EXCLUDE_PATTERN.search(folder_clean):
                    continue

                if not curr_cat:
                    # Test if this folder triggers our Hindi / English / Kids filter
                    detected = classify_folder(folder_clean)
                    
                    # If it's a top-level drive (e.g., hdd-1, hdd-2), allow entering to look for matching folders
                    is_drive_folder = bool(re.search(r"hdd|drive|disk", folder_clean, re.IGNORECASE))
                    
                    if detected:
                        queue.append((target_url, detected, depth + 1))
                    elif is_drive_folder:
                        queue.append((target_url, None, depth + 1))
                    # All other non-matching folders (software, anime, games) are dropped immediately
                else:
                    # Already inside a matched category: recurse subfolders under same category
                    queue.append((target_url, curr_cat, depth + 1))

    print(f"\nScan completed. Found {len(videos)} videos across {dirs_scanned} directories.", flush=True)
    videos.sort(key=lambda x: (x["category"], x["title"].lower()))
    write_m3u(videos, OUTPUT_FILE)


def write_m3u(entries: list, filepath: str):
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n\n")
        for item in entries:
            f.write(f'#EXTINF:-1 group-title="{item["category"]}" tvg-name="{item["title"]}",{item["title"]}\n')
            f.write(f"{item['url']}\n\n")
    print(f"M3U saved to {filepath}", flush=True)


if __name__ == "__main__":
    run_crawler()
