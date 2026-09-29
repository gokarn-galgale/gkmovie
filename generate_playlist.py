import os
import re
import json
import urllib.parse
from collections import deque
import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

OUTPUT_FILE = "playlist.m3u"
CACHE_FILE = "poster_cache.json"
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()

# Options: 'w185', 'w342', 'w500', 'w780', 'original'
POSTER_SIZE = "w500"
BACKDROP_SIZE = "w780"
TMDB_IMG_BASE = f"https://image.tmdb.org/t/p/{POSTER_SIZE}"
TMDB_BACKDROP_BASE = f"https://image.tmdb.org/t/p/{BACKDROP_SIZE}"

# Language priority per category
CATEGORY_LANGUAGES = {
    "Hindi": "hi-IN",
    "Hindi Dubbed": "hi-IN",
    "Hindi Drama": "hi-IN",
    "Korean": "ko-KR",
    "English": "en-US",
    "English Drama": "en-US",
    "Kids": "en-US",
}

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


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(cache: dict):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[!] Failed to save cache: {e}")


def clean_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_clean_query_and_year(raw_title: str) -> tuple[str, str | None]:
    decoded = urllib.parse.unquote(raw_title)
    name, _ = os.path.splitext(decoded)

    # 4-digit release year extraction
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", name)
    year = year_match.group(1) if year_match else None

    name = re.sub(r"\[.*?\]|\(.*?\)", " ", name)

    scene_tags = (
        r"\b(1080p|720p|480p|2160p|4k|uhd|bluray|blu-ray|web-dl|webrip|hdrip|dvdrip|"
        r"x264|x265|hevc|h264|h265|aac|ac3|dts|ddp5\.1|dual audio|hindi|english|"
        r"esub|proper|repack|remux)\b"
    )
    name = re.split(scene_tags, name, flags=re.IGNORECASE)[0]

    if year:
        name = re.split(r"\b" + year + r"\b", name)[0]

    clean = re.sub(r"[._\-]", " ", name).strip()
    return clean, year


def fetch_advanced_posters(search_title: str, category: str, is_drama: bool, cache: dict) -> tuple[str, str]:
    """Returns a tuple of (poster_url, backdrop_url) with multi-language fallback and caching."""
    if not TMDB_API_KEY:
        return "", ""

    query, year = parse_clean_query_and_year(search_title)
    if not query:
        query = re.sub(r"[._\-]", " ", os.path.splitext(urllib.parse.unquote(search_title))[0]).strip()

    cache_key = f"{'tv' if is_drama else 'movie'}:{query.lower()}:{year or ''}:{category}"
    if cache_key in cache:
        cached_data = cache[cache_key]
        return cached_data.get("poster", ""), cached_data.get("backdrop", "")

    endpoint = "tv" if is_drama else "movie"
    url = f"https://api.themoviedb.org/3/search/{endpoint}"
    primary_lang = CATEGORY_LANGUAGES.get(category, "en-US")

    # Try 1: Search with specific category language preference and year
    params = {
        "api_key": TMDB_API_KEY,
        "query": query,
        "language": primary_lang,
        "include_adult": "true",
    }
    if year:
        params["first_air_date_year" if is_drama else "year"] = year

    try:
        res = requests.get(url, params=params, timeout=5)
        results = res.json().get("results", []) if res.status_code == 200 else []

        # Try 2: Fallback to English and drop year constraint if no poster returned
        if not results:
            params.pop("year", None)
            params.pop("first_air_date_year", None)
            params["language"] = "en-US"
            res2 = requests.get(url, params=params, timeout=5)
            results = res2.json().get("results", []) if res2.status_code == 200 else []

        # Try 3: Multi-search fallback
        if not results:
            multi_url = "https://api.themoviedb.org/3/search/multi"
            res3 = requests.get(multi_url, params={"api_key": TMDB_API_KEY, "query": query, "include_adult": "true"}, timeout=5)
            results = res3.json().get("results", []) if res3.status_code == 200 else []

        if results:
            best_match = results[0]
            poster_path = best_match.get("poster_path")
            backdrop_path = best_match.get("backdrop_path")

            poster_url = f"{TMDB_IMG_BASE}{poster_path}" if poster_path else ""
            backdrop_url = f"{TMDB_BACKDROP_BASE}{backdrop_path}" if backdrop_path else ""

            cache[cache_key] = {"poster": poster_url, "backdrop": backdrop_url}
            return poster_url, backdrop_url

    except Exception as err:
        print(f"    [!] Poster lookup error for '{query}': {err}", flush=True)

    cache[cache_key] = {"poster": "", "backdrop": ""}
    return "", ""


def format_title(filename: str, parent_folder: str, is_drama: bool) -> str:
    decoded = urllib.parse.unquote(filename)
    base, _ = os.path.splitext(decoded)
    clean_f = re.sub(r"[._]", " ", base).strip()

    if is_drama and parent_folder:
        clean_p = re.sub(r"[._]", " ", urllib.parse.unquote(parent_folder)).strip()
        if clean_p.lower() not in clean_f.lower():
            return f"{clean_p} - {clean_f}"
    return clean_f


def crawl_category(category: str, seeds: list, session: requests.Session, cache: dict) -> list:
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

        local_images = []
        video_links = []
        subfolder_links = []

        for link in soup.find_all("a", href=True):
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

        # Identify local folder artwork
        default_dir_poster = ""
        for img in local_images:
            img_lower = img.lower()
            if any(k in img_lower for k in ("poster", "folder", "cover", "thumb")):
                default_dir_poster = img
                break
        if not default_dir_poster and local_images:
            default_dir_poster = local_images[0]

        for vid_url, filename in video_links:
            title = format_title(filename, parent_folder, is_drama)
            poster = default_dir_poster
            backdrop = ""

            base_vid = os.path.splitext(filename)[0].lower()
            for img in local_images:
                if base_vid in img.lower():
                    poster = img
                    break

            if not poster:
                query_name = parent_folder if is_drama and parent_folder else filename
                poster, backdrop = fetch_advanced_posters(query_name, category, is_drama, cache)

            status = f"✓ Poster [{poster[:42]}...]" if poster else "✗ No Poster"
            print(f"  [{category}] {title} -> {status}", flush=True)

            items.append({
                "title": title,
                "url": vid_url,
                "category": category,
                "logo": poster,
                "backdrop": backdrop
            })

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
            # tvg-banner provides horizontal background art in supported IPTV apps
            banner_attr = f' tvg-banner="{item["backdrop"]}"' if item.get("backdrop") else ""
            f.write(f'#EXTINF:-1 group-title="{item["category"]}" tvg-name="{item["title"]}"{logo_attr}{banner_attr},{item["title"]}\n')
            f.write(f"{item['url']}\n\n")
    print(f"\nM3U written to {filepath} with {len(entries)} items.", flush=True)


def main():
    poster_cache = load_cache()
    print(f"[+] Loaded {len(poster_cache)} cached posters from {CACHE_FILE}.", flush=True)

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
        category_videos = crawl_category(category, seeds, session, poster_cache)
        all_videos.extend(category_videos)

    save_cache(poster_cache)
    all_videos.sort(key=lambda x: (x["category"], x["title"].lower()))
    write_m3u(all_videos, OUTPUT_FILE)


if __name__ == "__main__":
    main()
