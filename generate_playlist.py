import os
import re
import urllib.parse
import requests
from bs4 import BeautifulSoup

BASE_URL = "http://103.225.94.27/Infobase/"
OUTPUT_FILE = "playlist.m3u"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v")

# Map category labels to regex patterns matching folder names
CATEGORIES = {
    "Hindi Dubbed": re.compile(r"hindi\s*dub", re.IGNORECASE),
    "South Dubbed": re.compile(r"south\s*dub", re.IGNORECASE),
    "Hindi": re.compile(r"\bhindi\b", re.IGNORECASE),
    "English": re.compile(r"\benglish\b", re.IGNORECASE),
}


def classify_category(name: str):
    """Matches a folder name to one of the 4 defined categories.

    Order matters: 'Hindi Dubbed' matches before generic 'Hindi'.
    """
    for category_name, pattern in CATEGORIES.items():
        if pattern.search(name):
            return category_name
    return None


def clean_title(filename: str) -> str:
    """Decodes URL encoding and strips extensions and noise characters."""
    decoded = urllib.parse.unquote(filename)
    base_name, _ = os.path.splitext(decoded)
    return re.sub(r"[._]", " ", base_name).strip()


def crawl_videos(url: str, session: requests.Session, current_category: str, visited: set) -> list:
    """Recursively crawls inside an identified category folder to find video files."""
    entries = []
    if url in visited:
        return entries
    visited.add(url)

    try:
        response = session.get(url, timeout=15)
        response.raise_for_status()
    except Exception as err:
        print(f"Failed to access {url}: {err}")
        return entries

    soup = BeautifulSoup(response.text, "html.parser")

    for link in soup.find_all("a", href=True):
        href = link.get("href")
        if href in ("../", "./", "/") or href.startswith("?") or href.startswith("#"):
            continue

        target_url = urllib.parse.urljoin(url, href)
        if not target_url.startswith(BASE_URL):
            continue

        if href.endswith("/"):
            # Recurse down subfolders within the category
            entries.extend(crawl_videos(target_url, session, current_category, visited))
        elif any(href.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
            filename = os.path.basename(urllib.parse.urlsplit(target_url).path)
            title = clean_title(filename)
            entries.append({
                "title": title,
                "url": target_url,
                "category": current_category
            })
            print(f"[{current_category}] Found: {title}")

    return entries


def scan_root_for_categories(session: requests.Session) -> list:
    """Scans the root URL for top-level folders that match the target categories."""
    all_videos = []
    visited = set()

    try:
        response = session.get(BASE_URL, timeout=15)
        response.raise_for_status()
    except Exception as err:
        print(f"Failed to connect to root {BASE_URL}: {err}")
        return all_videos

    soup = BeautifulSoup(response.text, "html.parser")

    for link in soup.find_all("a", href=True):
        href = link.get("href")
        if href in ("../", "./", "/") or href.startswith("?") or href.startswith("#"):
            continue

        if href.endswith("/"):
            folder_name = urllib.parse.unquote(href.strip("/"))
            matched_category = classify_category(folder_name)

            if matched_category:
                folder_url = urllib.parse.urljoin(BASE_URL, href)
                print(f"--> Scanning Category [{matched_category}]: {folder_url}")
                videos = crawl_videos(folder_url, session, matched_category, visited)
                all_videos.extend(videos)

    return all_videos


def write_m3u(entries: list, filepath: str):
    """Outputs items into an Extended M3U playlist with group-title attributes."""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n\n")
        for item in entries:
            title = item["title"]
            category = item["category"]
            url = item["url"]
            # group-title enables category folder grouping in VLC, Kodi, and IPTV apps
            f.write(f'#EXTINF:-1 group-title="{category}" tvg-name="{title}",{title}\n')
            f.write(f"{url}\n\n")

    print(f"\nSaved {len(entries)} items to {filepath}")


def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; M3UCrawler/2.0)"})

    print(f"Connecting to base path: {BASE_URL}")
    videos = scan_root_for_categories(session)

    # Sort primarily by category, then alphabetically by title
    videos.sort(key=lambda x: (x["category"], x["title"].lower()))

    write_m3u(videos, OUTPUT_FILE)


if __name__ == "__main__":
    main()
