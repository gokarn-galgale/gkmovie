import json
import urllib.parse
import urllib.request
from typing import Any, Dict, List

SOURCE_URL = "https://raw.githubusercontent.com/msplayerott/yeash/refs/heads/main/wak_tu/db4.json"
OUTPUT_FILE = "playlist.m3u"


def clean_title(title: str) -> str:
    """Strips leading dashes, spaces, and formatting quirks."""
    return title.lstrip("- ").strip()


def build_m3u_entry(item: Dict[str, Any]) -> str:
    # Skip items explicitly marked offline or missing a stream URL
    if item.get("status") == "off" or not item.get("streamUrl"):
        return ""

    title = clean_title(item.get("title", "Untitled"))
    group = item.get("Category", "General")
    logo = item.get("posterUrl", "")
    stream_url = item.get("streamUrl", "").strip()

    # Base EXTINF line
    lines = [f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{title}']

    # Extract HTTP headers
    headers = item.get("headers", {})
    referer = headers.get("referer")
    user_agent = headers.get("user_agent")

    # Syntax 1: VLC HTTP options
    if user_agent:
        lines.append(f"#EXTVLCOPT:http-user-agent={user_agent}")
    if referer:
        lines.append(f"#EXTVLCOPT:http-referrer={referer}")

    # Syntax 2: Pipe delimiter for Kodi / TiviMate / FFmpeg
    header_params = []
    if referer:
        header_params.append(f"Referer={urllib.parse.quote(referer)}")
    if user_agent:
        header_params.append(f"User-Agent={urllib.parse.quote(user_agent)}")

    if header_params:
        stream_url += "|" + "&".join(header_params)

    lines.append(stream_url)
    return "\n".join(lines)


def fetch_json_data(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; M3UGenerator/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    print(f"Fetching data from: {SOURCE_URL}")
    data = fetch_json_data(SOURCE_URL)

    # Normalize single objects or lists
    items: List[Dict[str, Any]] = data if isinstance(data, list) else [data]

    m3u_output = ["#EXTM3U\n"]
    for item in items:
        entry = build_m3u_entry(item)
        if entry:
            m3u_output.append(entry)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n\n".join(m3u_output) + "\n")

    print(f"Successfully wrote {len(m3u_output) - 1} entries to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
