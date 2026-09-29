import os
import re
import urllib.parse
from collections import deque
import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

OUTPUT_FILE = "playlist.m3u"
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v")

# Get TMDb API Key from environment or hardcode it
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")  # Put your key here or in GitHub Secrets
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

# Cache to avoid duplicate API calls for identical titles or multi-episode series
POSTER_CACHE = {}


def clean_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def clean_search_query(raw_title: str) -> str:
    """Strips release tags, resolutions, and brackets so TMDb search finds the title."""
    clean = re.sub(r"\b(1080p|720p|480p|2160p|4k|bluray|web-dl|x264|x265|hevc|aac|dvdrip)\b.*", "", raw_title, flags=re.I)
    clean = re.sub(r"\[.*?\]|\(.*?\)", "", clean)
    clean = re.sub(r"[._]", " ", clean).strip()
    return clean


def get_poster_url(title: str, is_drama: bool) -> str:
    """Fetches official poster art URL from TMDb."""
    if not TMDB_API_KEY:
        return ""

    query = clean_search_query(title)
    if query in POSTER_CACHE:
        return POSTER_CACHE[query]

    media_type = "tv" if is_drama else "movie"
    url = f"https://api.themoviedb.org/3/search/{media_type}"
    params = {"api_key": TMDB_API_KEY, "query": query}

    try:
        res = requests.get(url, params=params, timeout=4)
        if res.status_code == 200:
            data = res.json()
            results = data.get("results", [])
            if results and results[0].get("poster_path"):
                poster_url = f"{TMDB_IMG_BASE}{results[0]['poster_path']}"
                POSTER_CACHE[query] = poster_url
                return poster_url
    except Exception:
        pass

    POSTER_CACHE[query] = ""
    return ""


def format_title(filename: str, parent_folder: str, is_drama: bool) -> str:
    decoded_file = urllib.parse.unquote(filename)
    base_file, _ = os.path.splitext(decoded_file)
    clean_file = re.sub(r"[._]", " ", base_file).strip()

    if is_drama and parent_folder:
        clean_show = re.sub(r"[._]", " ", urllib.parse.unquote(parent_folder)).strip()
        if clean_show.lower() not in clean_file.lower():
            return f"{clean_show} - {clean_file}"

    return clean_file


def crawl_category(category: str, seeds: list, session: requests.Session) -> list:
    items = []
    visited_paths = set()
    is_drama = "drama" in category.lower()

    queue = deque()
    for seed in seeds:
        normalized_seed = clean_url(seed)
        if not normalized_seed.endswith("/"):
            normalized_seed += "/"
        queue.append((normalized_seed, "", 0))

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

        for link in soup.find_all("a", href=True):
            raw_href = link.get("href").strip()

            if "?" in raw_href or "#" in raw_href or raw_href in ("../", "./", "/", "") or "parent directory" in link.text.lower():
                continue

            target_url = urllib.parse.urljoin(curr_url, raw_href)
            target_url = clean_url(target_url)

            if not any(target_url.startswith(clean_url(s).rstrip("/") + "/") or target_url == clean_url(s) for s in seeds):
                continue

            parsed_target = urllib.parse.urlsplit(target_url)
            target_norm = os.path.normpath(urllib.parse.unquote(parsed_target.path))

            if target_norm in visited_paths:
                continue

            file_or_folder = os.path.basename(target_norm)
            is_video = any(target_norm.lower().endswith(ext) for ext in VIDEO_EXTS)

            if is_video:
                title = format_title(file_or_folder, parent_folder, is_drama)
                # Fetch poster for the title or series folder
                lookup_name = parent_folder if is_drama and parent_folder else title
                poster_url = get_poster_url(lookup_name, is_drama)

                items.append({
                    "title": title,
                    "url": target_url,
                    "category": category,
                    "logo": poster_url
                })
                print(f"[{category}] {title} {'(Poster added)' if poster_url else ''}", flush=True)
            else:
                current_folder_name = file_or_folder
                if not target_url.endswith("/"):
                    target_url += "/"
                queue.append((target_url, current_folder_name, depth + 1))

    return items


def write_m3u(entries: list, filepath: str):
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n\n")
        for item in entries:
            # tvg-logo renders the poster in IPTV players
            logo_attr = f' tvg-logo="{item["logo"]}"' if item["logo"] else ""
            f.write(f'#EXTINF:-1 group-title="{item["category"]}" tvg-name="{item["title"]}"{logo_attr},{item["title"]}\n')
            f.write(f"{item['url']}\n\n")
    print(f"\nM3U successfully generated at {filepath} with {len(entries)} items.", flush=True)


def main():
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
