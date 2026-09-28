import os
import re
import sys
import urllib.parse
import requests
from bs4 import BeautifulSoup

BASE_URL = "http://103.225.94.27/Infobase/"
OUTPUT_FILE = "playlist.m3u"
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v")

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


def crawl(url: str, session: requests.Session, current_category: str | None, visited: set) -> list:
    if url in visited:
        return []
    visited.add(url)

    items = []

    try:
        response = session.get(url, timeout=20)
        if response.status_code != 200:
            print(f"[!] Warning: HTTP {response.status_code} on {url}")
            return items
    except requests.exceptions.RequestException as err:
        print(f"[!] Request failed for {url}: {err}")
        return items

    soup = BeautifulSoup(response.text, "html.parser")
    links = soup.find_all("a", href=True)

    for link in links:
        href = link.get("href")

        # Skip directory sorting queries, parent pointers, anchors
        if href in ("../", "./", "/") or href.startswith("?") or href.startswith("#"):
            continue

        target_url = urllib.parse.urljoin(url, href)

        # Stay within root
        if not target_url.startswith(BASE_URL):
            continue

        # Extract path to verify extension or folder name
        parsed = urllib.parse.urlsplit(target_url)
        path = parsed.path

        # Check if the folder name updates category context
        folder_category = detect_category(href) or current_category

        is_video = any(path.lower().endswith(ext) for ext in VIDEO_EXTS)

        if is_video:
            if folder_category:
                title = clean_title(os.path.basename(path))
                items.append({
                    "title": title,
                    "url": target_url,
                    "category": folder_category
                })
                print(f"[{folder_category}] Found: {title}")
        else:
            # Recurse down subfolders
            items.extend(crawl(target_url, session, folder_category, visited))

    return items


def write_m3u(entries: list, filepath: str):
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n\n")
        for item in entries:
            f.write(f'#EXTINF:-1 group-title="{item["category"]}" tvg-name="{item["title"]}",{item["title"]}\n')
            f.write(f"{item['url']}\n\n")
    print(f"\nWritten {len(entries)} items to {filepath}")


def main():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    })

    print(f"Checking access to base URL: {BASE_URL}")

    # Connectivity and diagnosis probe
    try:
        res = session.get(BASE_URL, timeout=15)
        print(f"Server replied with Status Code: {res.status_code}")
        print(f"Content Length: {len(res.text)} bytes")
        if res.status_code != 200:
            print("Response preview:\n", res.text[:500])
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Runner cannot reach {BASE_URL}: {e}")
        print("\nDIAGNOSIS:")
        print("This server does not route outside of its local ISP network.")
        print("Because GitHub Actions runs on Microsoft Azure IP addresses, the ISP firewall is dropping incoming packets.")
        print("To bypass this while running purely on GitHub, you can set a free/paid regional proxy in GitHub Repo Secrets (REGIONAL_PROXY).")
        write_m3u([], OUTPUT_FILE)
        return

    visited = set()
    videos = crawl(BASE_URL, session, None, visited)

    videos.sort(key=lambda x: (x["category"], x["title"].lower()))
    write_m3u(videos, OUTPUT_FILE)


if __name__ == "__main__":
    main()
