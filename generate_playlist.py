import os
import re
import urllib.parse
from collections import deque
import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

OUTPUT_FILE = "playlist.m3u"
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w500"

CATEGORY_SEEDS = {
    "English": [
        "http://103.225.94.27/Infobase/hdd-1/English/",
        "http://103.225.94.27/Infobase/hdd-2/English2.0/",
        "http://103.225.94.27/Infobase/hdd-3/english/",
        "http://103.225.94.27/Infobase/hdd-5/English%20.5/",
        "http://103.225.94.27/Infobase/hdd-5/English%20.5/2025-26/",
        "http://103.225.94.27/Infobase/hdd-5/English%20.5/Charlie%20Chaplin%20/",
    ],
    "Hindi Dubbed": [
        "http://103.225.94.27/Infobase/hdd-1/HINDI%20DUBBED/",
        "http://103.225.94.27/Infobase/hdd-2/Hindi%20Dub2.0/",
        "http://103.225.94.27/Infobase/hdd-3/Hindi%20Dub/",
        "http://103.225.94.27/Infobase/hdd-5/hindi%20dub.5/",
    ],
    "Korean": [
        "http://103.225.94.27/Infobase/hdd-1/Korean/",
        "http://103.225.94.27/Infobase/hdd-2/Korean/",
    ],
    "Hindi": [
        "http://103.225.94.27/Infobase/hdd-1/Hindi/",
        "http://103.225.94.27/Infobase/hdd-2/hindi2.0/",
        "http://103.225.94.27/Infobase/hdd-3/hindi/",
        "http://103.225.94.27/Infobase/hdd-5/hindi.5/",
        "http://103.225.94.27/Infobase/hdd-5/hindi.5/2025-26/",
        "http://103.225.94.27/Infobase/hdd-5/hindi.5/aug%202026%20/",
    ],
    "Kids": [
        "http://103.225.94.27/Infobase/hdd-1/Animation/English/",
        "http://103.225.94.27/Infobase/hdd-2/Anime/",
    ],
    "Hindi Drama": [
        "http://103.225.94.27/Infobase/hdd-1/Hindi%20Drama/",
        "http://103.225.94.27/Infobase/hdd-2/Hindi%20Drama%202.0/",
        "http://103.225.94.27/Infobase/hdd-3/Hindi%20Drama/",
        "http://103.225.94.27/Infobase/hdd-5/hindi%20drama%20.5/",
    ],
    "English Drama": [
        "http://103.225.94.27/Infobase/hdd-1/english%20drama/",
        "http://103.225.94.27/Infobase/hdd-2/english%20drama/",
        "http://103.225.94.27/Infobase/hdd-3/English%20Drama/",
        "http://103.225.94.27/Infobase/hdd-5/English%20Drama%20.5/",
    ],
}

POSTER_CACHE = {}


def clean_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sanitize_search_query(raw_title: str) -> str:
    """Aggressively purges scene tags, audio, codecs, and years for clean TMDb matching."""
    text = urllib.parse.unquote(raw_title)
    text, _ = os.path.splitext(text)

    # Remove text in parentheses or square brackets
    text = re.sub(r"\[.*?\]|\(.*?\)", " ", text)

    # Cut off at common scene tags, codecs, sources, or resolutions
    tags = (
        r"\b(1080p|720p|480p|2160p|4k|uhd|bluray|blu-ray|web-dl|webrip|hdrip|dvdrip|"
        r"x264|x265|hevc|h264|h265|aac|ac3|dts|ddp5\.1|dual audio|hindi|english|"
        r"esub|mkv|mp4|proper|repack|remux)\b"
    )
    text = re.split(tags, text, flags=re.IGNORECASE)[0]

    # Remove 4-digit release years (e.g. 1999, 2024)
    text = re.sub(r"\b(19\d\d|20\d\d)\b.*", "", text)

    # Replace dots, underscores, dashes with spaces
    text = re.sub(r"[._\-]", " ", text).strip()
    return text


def fetch_tmdb_poster(search_title: str, is_drama: bool) -> str:
    if not TMDB_API_KEY:
        return ""

    query = sanitize_search_query(search_title)
    if not query:
        return ""

    cache_key = f"{'tv' if is_drama else 'movie'}:{query.lower()}"
    if cache_key in POSTER_CACHE:
        return POSTER_CACHE[cache_key]

    endpoint = "tv" if is_drama else "movie"
    url = f"https://api.themoviedb.org/3/search/{endpoint}"
    params = {"api_key": TMDB_API_KEY, "query": query}

    try:
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            results = res.json().get("results", [])
            if results:
                poster_path = results[0].get("poster_path")
                if poster_path:
                    full_url = f"{TMDB_IMG_BASE}{poster_path}"
                    POSTER_CACHE[cache_key] = full_url
                    return full_url
    except Exception:
        pass

    POSTER_CACHE[cache_key] = ""
    return ""


def format_title(filename: str, parent_folder: str, is_drama: bool) -> str:
    decoded = urllib.parse.unquote(filename)
    base, _ = os.path.splitext(decoded)
    clean_f = re.sub(r"[._]", " ", base).strip()

    if is_drama and parent_folder:
        clean_p = re.sub(r"[._]", " ", urllib.parse.unquote(parent_folder)).strip()
        if clean_p.lower() not in clean_f.lower():
            return f"{clean_p} - {clean_f}"
    return clean_f


def crawl_category(category: str, seeds: list, session: requests.Session) -> list:
    items = []
    visited_paths = set()
    is_drama = "drama" in category.lower()

    queue = deque()
    for seed in seeds:
        norm_seed = clean_url(seed)
        if not norm_seed.endswith("/"):
            norm_seed += "/"
        queue.append((norm_seed, "", 0))

    while queue:
        curr_url, parent_folder, depth = queue.popleft()
        if depth > 5:
            continue

        parsed = urllib.parse.urlsplit(curr_url)
        norm_path = os.path.normpath(urllib.parse.unquote(parsed.path))
        if norm_path in visited_paths:
            continue
        visited_paths.add(norm_path)

        try:
            response = session.get(curr_url, timeout=(3.0, 6.0))
            if response.status_code != 200:
                continue
            html_text = response.text
        except requests.RequestException:
            continue

        soup = BeautifulSoup(html_text, "html.parser")
        links = soup.find_all("a", href=True)

        # 1. First pass: look for any local poster/image files in this directory
        local_images = []
        video_links = []
        subfolder_links = []

        for link in links:
            raw_href = link.get("href").strip()
            if "?" in raw_href or "#" in raw_href or raw_href in ("../", "./", "/", "") or "parent directory" in link.text.lower():
                continue

            target_url = clean_url(urllib.parse.urljoin(curr_url, raw_href))
            target_norm = os.path.normpath(urllib.parse.unquote(urllib.parse.urlsplit(target_url).path))

            if any(target_norm.lower().endswith(ext) for ext in IMAGE_EXTS):
                local_images.append(target_url)
            elif any(target_norm.lower().endswith(ext) for ext in VIDEO_EXTS):
                video_links.append((target_url, os.path.basename(target_norm)))
            else:
                subfolder_links.append((target_url, os.path.basename(target_norm)))

        # Pick default directory poster if available (e.g. poster.jpg, folder.jpg)
        default_dir_poster = ""
        for img in local_images:
            img_lower = img.lower()
            if any(k in img_lower for k in ("poster", "folder", "cover", "thumb")):
                default_dir_poster = img
                break
        if not default_dir_poster and local_images:
            default_dir_poster = local_images[0]

        # 2. Second pass: process video files
        for vid_url, filename in video_links:
            title = format_title(filename, parent_folder, is_drama)
            poster = default_dir_poster

            # Check if there is an image specifically sharing the movie filename
            base_vid = os.path.splitext(filename)[0].lower()
            for img in local_images:
                if base_vid in img.lower():
                    poster = img
                    break

            # If no local poster exists, fetch from TMDb
            if not poster:
                query_name = parent_folder if is_drama and parent_folder else filename
                poster = fetch_tmdb_poster(query_name, is_drama)

            status = "✓ Poster" if poster else "✗ No Poster"
            print(f"[{category}] {title} -> {status}", flush=True)

            items.append({
                "title": title,
                "url": vid_url,
                "category": category,
                "logo": poster
            })

        # 3. Queue subfolders
        for sub_url, folder_name in subfolder_links:
            if not any(sub_url.startswith(clean_url(s).rstrip("/") + "/") or sub_url == clean_url(s) for s in seeds):
                continue
            if not sub_url.endswith("/"):
                sub_url += "/"
            queue.append((sub_url, folder_name, depth + 1))

    return items


def write_m3u(entries: list, filepath: str):
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n\n")
        for item in entries:
            logo_attr = f' tvg-logo="{item["logo"]}"' if item["logo"] else ""
            f.write(f'#EXTINF:-1 group-title="{item["category"]}" tvg-name="{item["title"]}"{logo_attr},{item["title"]}\n')
            f.write(f"{item['url']}\n\n")
    print(f"\nM3U written to {filepath} with {len(entries)} items.", flush=True)


def main():
    if not TMDB_API_KEY:
        print("[!] NOTICE: TMDB_API_KEY secret is EMPTY or NOT FOUND in environment.")
        print("    TMDb lookups will be skipped. Only local server images will be used.\n")
    else:
        print("[+] TMDB_API_KEY detected. Online poster fetching enabled.\n")

    session = requests.Session()
    adapter = HTTPAdapter(max_retries=0, pool_connections=15, pool_maxsize=15)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)",
        "Connection": "close",
    })

    all_videos = []
    for category, seeds in CATEGORY_SEEDS.items():
        print(f"\n--- Scanning Category: {category} ---", flush=True)
        category_videos = crawl_category(category, seeds, session)
        all_videos.extend(category_videos)

    all_videos.sort(key=lambda x: (x["category"], x["title"].lower()))
    write_m3u(all_videos, OUTPUT_FILE)


if __name__ == "__main__":
    main()
