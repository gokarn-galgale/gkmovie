import requests
import json
import re
import time
import urllib.parse
from datetime import datetime

# --- CONFIGURATION ---
TMDB_API_KEYS = [
    "1865f43a0549ca50d341dd9ab8b29f49",
    "439c478a771f35c05022f9feabcca01c",
    "8265bd1679663a7ea12ac168da84d2e8"
]
DOMAINS_URL = "https://cdn.jsdelivr.net/gh/phisher98/TVVVV@main/domains.json"
BASE_URL = "https://m1.multimovies.courses"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/"
}

domain_cache_timestamp = 0
DOMAIN_CACHE_TTL = 4 * 60 * 60  # 4 hours

# List of movies/shows to scrape for the playlist (Mix of TMDB IDs and IMDB IDs)
# Add the IDs of the content you want in your M3U here.
MEDIA_TO_SCRAPE = [
    {"id": "603", "type": "movie"}, # The Matrix
    {"id": "1399", "type": "tv", "season": 1, "episode": 1} # Game of Thrones S01E01
]

def fetch_and_update_domain():
    global BASE_URL, HEADERS, domain_cache_timestamp
    now = time.time()
    if now - domain_cache_timestamp < DOMAIN_CACHE_TTL:
        return
    try:
        res = requests.get(DOMAINS_URL, timeout=4)
        if res.ok:
            data = res.json()
            if data and data.get("MultiMovies"):
                BASE_URL = data["MultiMovies"]
                HEADERS["Referer"] = f"{BASE_URL}/"
                domain_cache_timestamp = now
    except Exception:
        pass

def get_media_meta(media_id, media_type="movie"):
    is_imdb = str(media_id).startswith("tt")
    stremio_type = "series" if media_type in ["tv", "series"] else "movie"
    
    # Try Cinemeta first if it's an IMDB ID
    if is_imdb:
        try:
            res = requests.get(f"https://v3-cinemeta.strem.io/meta/{stremio_type}/{media_id}.json", timeout=8)
            if res.ok:
                data = res.json()
                if data.get("meta") and data["meta"].get("name"):
                    return {
                        "imdbId": media_id,
                        "title": data["meta"]["name"],
                        "year": str(data["meta"].get("year", ""))
                    }
        except Exception:
            pass

    # Fallback to TMDB
    endpoint = "tv" if media_type in ["tv", "series"] else "movie"
    for key in TMDB_API_KEYS:
        try:
            url = f"https://api.tmdb.org/3/{endpoint}/{media_id}?api_key={key}&append_to_response=external_ids"
            res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if res.ok:
                data = res.json()
                imdb_id = data.get("external_ids", {}).get("imdb_id") or (media_id if is_imdb else None)
                title = data.get("title") or data.get("name") or data.get("original_title") or data.get("original_name") or ""
                release_date = data.get("release_date") or data.get("first_air_date") or ""
                year = release_date.split("-")[0] if release_date else ""
                
                if title:
                    return {"imdbId": imdb_id, "title": title, "year": year}
        except Exception:
            continue
            
    return {"imdbId": media_id if is_imdb else None, "title": "", "year": ""}

def get_streams(media_id, media_type="movie", season=1, episode=1):
    fetch_and_update_domain()
    is_tv = media_type in ["tv", "series"]
    
    meta = get_media_meta(media_id, "tv" if is_tv else "movie")
    if not meta or not meta.get("title"):
        return []

    search_url = f"{BASE_URL}/search?q={urllib.parse.quote(meta['title'].lower())}"
    try:
        html = requests.get(search_url, headers=HEADERS, timeout=8).text
    except Exception:
        return []

    target_type = "tv" if is_tv else "movie"
    matches = re.finditer(r'data-save-title="([^"]+)"', html, re.IGNORECASE)
    selected = None

    for match in matches:
        try:
            raw_data = match.group(1).replace("&quot;", '"')
            data = json.loads(raw_data)
            if data.get("type") == target_type:
                if not selected:
                    selected = data
                if str(data.get("year")) == str(meta.get("year")):
                    selected = data
                    break
        except Exception:
            continue

    if not selected or not selected.get("url"):
        return []

    watch_url = f"{BASE_URL}{selected['url']}"
    if is_tv:
        watch_url = f"{watch_url}/season/{season}/episode/{episode}"

    try:
        page_html = requests.get(watch_url, headers=HEADERS, timeout=8).text
    except Exception:
        return []

    chip_regex = r'<button[^>]+class="server-chip[^"]*"[^>]*data-url="([^"]+)"[^>]*>([\s\S]*?)<\/button>'
    chips = re.finditer(chip_regex, page_html, re.IGNORECASE)

    streams = []
    for chip in chips:
        stream_url = chip.group(1).replace("&amp;", "&").strip()
        
        # Extract embedded src if necessary
        if "src=" in stream_url.lower():
            src_match = re.search(r'src=["\']?([^"\'\s>]+)', stream_url, re.IGNORECASE)
            if src_match:
                stream_url = src_match.group(1)
                
        if not stream_url.startswith("http"):
            continue

        chip_content = chip.group(2)
        server_match = re.search(r'class="server-name">([^<]+)<\/span>', chip_content, re.IGNORECASE)
        badge_match = re.search(r'class="server-badge">([^<]+)<\/span>', chip_content, re.IGNORECASE)

        server_name = server_match.group(1).strip() if server_match else "Server"
        badge = badge_match.group(1).strip() if badge_match else "HD"
        
        # Determine group
        group = "TV Shows" if is_tv else "Movies"
        season_ep_tag = f" S{int(season):02d}E{int(episode):02d}" if is_tv else ""
        
        name = f"🎬 {meta['title']} ({meta.get('year', 'N/A')}){season_ep_tag} [{badge}] • {server_name}"

        streams.append({
            "name": name,
            "group": group,
            "url": stream_url,
            "logo": ""
        })

    return streams

def generate_playlist():
    print("Scraping streams...")
    all_streams = []
    
    for item in MEDIA_TO_SCRAPE:
        print(f"Fetching ID {item['id']}...")
        streams = get_streams(
            item["id"], 
            item.get("type", "movie"), 
            item.get("season", 1), 
            item.get("episode", 1)
        )
        all_streams.extend(streams)
        time.sleep(1) # Be polite to the server
    
    # M3U Header
    m3u_content = "#EXTM3U\n"
    
    for stream in all_streams:
        m3u_content += f'#EXTINF:-1 tvg-logo="{stream["logo"]}" group-title="{stream["group"]}",{stream["name"]}\n'
        m3u_content += f'{stream["url"]}\n'

    with open("playlist.m3u", "w", encoding="utf-8") as f:
        f.write(m3u_content)
        
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Playlist updated with {len(all_streams)} streams.")

if __name__ == "__main__":
    generate_playlist()
          
