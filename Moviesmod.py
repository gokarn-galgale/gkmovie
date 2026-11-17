import requests
import json
import re
import time
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
DOMAINS_URL = "https://cdn.jsdelivr.net/gh/phisher98/TVVVV@main/domains.json"
FALLBACK_DOMAIN = "https://moviesmod.meme"
TMDB_API_KEY = "1865f43a0549ca50d341dd9ab8b29f49"
TMDB_BASE_URL = "https://api.tmdb.org/3"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1"
}

cached_domain = "https://moviesmod.meme"
cached_domain_ts = 0

def format_chole_card(opt):
    raw_parts = [
        opt.get("filename", ""), 
        opt.get("rawText", ""), 
        opt.get("server", ""), 
        opt.get("quality", ""), 
        opt.get("size", ""), 
        opt.get("title", "")
    ]
    raw = " ".join([str(p) for p in raw_parts if p])
    text = raw.strip()
    
    clean_text = re.sub(r'4khdhub', '', text, flags=re.IGNORECASE)
    clean_text = re.sub(r'hdhub4u', '', clean_text, flags=re.IGNORECASE)
    
    res = ""
    q_check = str(opt.get("quality", "")).strip()
    
    if re.search(r'\b(?:2160p|4k|uhd)\b', q_check, re.IGNORECASE):
        res = "4K UHD"
    elif re.search(r'\b(?:1080p|fhd)\b', q_check, re.IGNORECASE):
        res = "1080p FHD"
    elif re.search(r'\b(?:720p|hd)\b', q_check, re.IGNORECASE):
        res = "720p HD"
    elif re.search(r'\b(?:480p|sd)\b', q_check, re.IGNORECASE):
        res = "480p"
    elif re.search(r'\b(?:2160p|4k|uhd)\b', clean_text, re.IGNORECASE):
        res = "4K UHD"
    elif re.search(r'\b(?:1080p|fhd)\b', clean_text, re.IGNORECASE):
        res = "1080p FHD"
    elif re.search(r'\b(?:720p|hd)\b', clean_text, re.IGNORECASE):
        res = "720p HD"
    elif re.search(r'\b(?:480p|sd)\b', clean_text, re.IGNORECASE):
        res = "480p"
    else:
        res = q_check.upper() if q_check else "1080p FHD"

    source = ""
    if re.search(r'\bremux\b', text, re.IGNORECASE):
        source = "REMUX"
    elif re.search(r'\b(?:bluray|bdrip|brrip)\b', text, re.IGNORECASE):
        source = "BluRay"
    elif re.search(r'\b(?:web-?dl|webrip|web)\b', text, re.IGNORECASE):
        source = "WEB-DL"
    elif re.search(r'\bhdtv\b', text, re.IGNORECASE):
        source = "HDTV"

    codecs = []
    if re.search(r'\b(?:hevc|x265|h\.?265)\b', text, re.IGNORECASE):
        codecs.append("HEVC")
    elif re.search(r'\b(?:x264|h\.?264|avc)\b', text, re.IGNORECASE):
        codecs.append("x264")
    if re.search(r'\b10-?bit\b', text, re.IGNORECASE):
        codecs.append("10-bit")

    hdr = []
    if re.search(r'\b(?:dolby\s*vision|dv)\b', text, re.IGNORECASE):
        hdr.append("Dolby Vision")
    if re.search(r'\bhdr10\+\b', text, re.IGNORECASE):
        hdr.append("HDR10+")
    elif re.search(r'\b(?:hdr10|hdr)\b', text, re.IGNORECASE):
        hdr.append("HDR")

    audio = []
    has_atmos = re.search(r'\b(?:atmos|ddpa)\b', text, re.IGNORECASE)
    has_truehd = re.search(r'\btruehd\b', text, re.IGNORECASE)
    has_dtshd = re.search(r'\bdts-?hd(?:\s*ma)?\b', text, re.IGNORECASE)
    has_dts = re.search(r'\bdts\b', text, re.IGNORECASE)
    has_ddp = re.search(r'\b(?:ddp|dd\+|eac3)\b', text, re.IGNORECASE)
    has_dd = re.search(r'\b(?:dd|ac3)\b', text, re.IGNORECASE)
    has_71 = re.search(r'7\.1', text, re.IGNORECASE)
    has_51 = re.search(r'5\.1', text, re.IGNORECASE)

    if has_atmos:
        audio.append("Dolby Atmos" + (" 7.1" if has_71 else " 5.1" if has_51 else ""))
    elif has_truehd:
        audio.append("TrueHD" + (" 7.1" if has_71 else " 5.1" if has_51 else ""))
    elif has_dtshd:
        audio.append("DTS-HD MA" + (" 7.1" if has_71 else " 5.1" if has_51 else ""))
    elif has_dts:
        audio.append("DTS" + (" 5.1" if has_51 else ""))
    elif has_ddp:
        audio.append("DDP 5.1")
    elif has_dd:
        audio.append("DD 5.1")
    elif re.search(r'\baac\b', text, re.IGNORECASE):
        audio.append("AAC")

    langs = []
    if re.search(r'\b(?:hindi|hin)\b', text, re.IGNORECASE): langs.append("🇮🇳 Hindi")
    if re.search(r'\b(?:tamil|tam)\b', text, re.IGNORECASE): langs.append("🇮🇳 Tamil")
    if re.search(r'\b(?:telugu|tel)\b', text, re.IGNORECASE): langs.append("🇮🇳 Telugu")
    if re.search(r'\b(?:english|eng)\b', text, re.IGNORECASE): langs.append("🇬🇧 English")
    if re.search(r'\b(?:korean|kor)\b', text, re.IGNORECASE): langs.append("🇰🇷 Korean")
    if re.search(r'\b(?:japanese|jap|jpn)\b', text, re.IGNORECASE): langs.append("🇯🇵 Japanese")
    if re.search(r'\bdual[- ]?audio\b', text, re.IGNORECASE): langs.append("🌐 Dual-Audio")
    if re.search(r'\bmulti[- ]?audio\b', text, re.IGNORECASE): langs.append("🌐 Multi-Audio")
    
    if not langs and opt.get("defaultLang"):
        langs.append(opt["defaultLang"])

    unique_langs = list(dict.fromkeys(langs))
    
    size_match = re.search(r'(?:💾\s*|\[|\b)([0-9.]+\s*[GM]B)(?:\]|\b)', text, re.IGNORECASE)
    raw_size = opt.get("size") or (size_match.group(1) if size_match else "")
    size = re.sub(r'([0-9.]+)\s*([GM]B)', r'\1 \2', raw_size, flags=re.IGNORECASE).upper() if raw_size else ""
    
    server = opt.get("server", "")
    if not server:
        grp_match = re.search(r'-([a-zA-Z0-9_]+)(?:\.[a-z]{3})?$', text, re.IGNORECASE)
        if grp_match and 2 < len(grp_match.group(1)) < 15:
            server = grp_match.group(1)

    name_parts = []
    if opt.get("latency"): name_parts.append(f"🟢 FAST ({opt['latency']}ms)")
    name_parts.append(opt.get("provider", "Stream"))
    if server: name_parts.append(f"🏷️ {server}")
    if res: name_parts.append(res)
    if source: name_parts.append(source)
    if codecs: name_parts.extend(codecs)
    if hdr: name_parts.extend(hdr)
    if audio: name_parts.append(audio[0])
    
    if unique_langs:
        dual_tag = next((l for l in unique_langs if "Dual" in l or "Multi" in l), None)
        if dual_tag:
            name_parts.append(dual_tag)
        else:
            name_parts.append(" + ".join(unique_langs[:2]))

    if opt.get("seeders") not in [None, ""]:
        name_parts.append(f"🌱 {opt['seeders']}")

    filename = str(opt.get("filename", "")).strip()
    if not filename or filename == opt.get("title"):
        base_title = re.sub(r'[^a-zA-Z0-9]+', '.', opt.get("title", "Video"))
        yr = f".{opt['year']}" if opt.get("year") else ""
        se = f".S{int(opt['season']):02d}E{int(opt['episode']):02d}" if opt.get("season") and opt.get("episode") else ""
        
        # Python 3.11 compatible format
        r = "." + re.sub(r'\s+', '.', res) if res else ""
        s = f".{source}" if source else ""
        c = f".{'.'.join(codecs)}" if codecs else ""
        a = "." + re.sub(r'[^a-zA-Z0-9]+', '.', audio[0]) if audio else ""
        g = "-" + re.sub(r'[\s\-_]+', '', server) if server else f"-{opt.get('provider', 'Release')}"
        
        filename = f"{base_title}{yr}{se}{r}{s}{c}{a}{g}.mkv"

    spec_tags = [t for t in [res, source] + codecs if t]
    season_ep = f" • S{int(opt['season']):02d}E{int(opt['episode']):02d}" if opt.get("season") and opt.get("episode") else ""
    
    line1 = f"🎬 {opt.get('title', 'Unknown')}{' (' + opt['year'] + ')' if opt.get('year') else ''}{season_ep}{' [' + ' • '.join(spec_tags) + ']' if spec_tags else ''}"
    line2 = f"📄 {filename}"
    av = hdr + audio
    line3 = f"💎 {' • '.join(av)}" if av else ""
    line4 = f"🌐 {' • '.join(unique_langs)}" if unique_langs else ""
    
    meta = []
    if size: meta.append(f"📦 {size}")
    if opt.get("seeders") not in [None, ""]: meta.append(f"🟢 {opt['seeders']} Seeders")
    if server: meta.append(f"🏷️ {server}")
    meta.append(f"🔗 {opt.get('provider', 'Stream')}")
    line5 = " • ".join(meta)
    
    body = " ".join([l for l in [line1, line2, line3, line4, line5] if l])
    
    quality_slug = "1080p"
    if "4K" in res or "2160" in res: quality_slug = "4k"
    elif "1080" in res: quality_slug = "1080p"
    elif "720" in res: quality_slug = "720p"
    elif "480" in res: quality_slug = "480p"

    return {
        "name": body,
        "description": body,
        "title": body,
        "quality": quality_slug,
        "size": size or ""
    }

def get_main_url():
    global cached_domain, cached_domain_ts
    now = time.time() * 1000
    if now - cached_domain_ts < 4 * 60 * 60 * 1000 and cached_domain:
        return cached_domain
    
    try:
        res = requests.get(DOMAINS_URL, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=3)
        if res.ok:
            data = res.json()
            if data and data.get("moviesmod"):
                cached_domain = data["moviesmod"]
                cached_domain_ts = now
                return cached_domain
    except Exception:
        pass
    
    return cached_domain or FALLBACK_DOMAIN

def get_base_url(url):
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return ""

def fix_url(url, domain):
    if not url: return ""
    if url.startswith("http"): return url
    if url.startswith("//"): return f"https:{url}"
    if url.startswith("/"): return domain + url
    return f"{domain}/{url}"

def fetch_tmdb_details(tmdb_id, media_type):
    try:
        url = f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=external_ids"
        res = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"})
        data = res.json()
        
        return {
            "title": data.get("title") or data.get("original_title") if media_type == "movie" else data.get("name") or data.get("original_name"),
            "year": (data.get("release_date") or data.get("first_air_date") or "")[:4],
            "imdbId": data.get("external_ids", {}).get("imdb_id")
        }
    except Exception:
        return None

def get_index_quality(string):
    if not string: return "Unknown"
    match = re.search(r'(\d{3,4})[pP]', string)
    if match: return match.group(1) + "p"
    if "4K" in string.upper() or "UHD" in string.upper(): return "2160p"
    return "Unknown"

def bypass_thenaukriadda(step1_action, sid):
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        
        # Step 1
        p1 = {"_lp_http": sid}
        r1 = session.post(step1_action, data=p1)
        soup1 = BeautifulSoup(r1.text, 'html.parser')
        form1 = soup1.find("form", id="lp-s1-form")
        action1 = form1.get("action") if form1.get("action") else step1_action
        
        # Step 2
        p2 = {inp.get("name"): inp.get("value", "") for inp in form1.find_all("input") if inp.get("name")}
        r2 = session.post(action1, data=p2)
        h2 = r2.text
        
        cookie_name = cookie_val = go_url = ""
        cookie_match = re.search(r'sc\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']+)["\']', h2)
        if cookie_match:
            cookie_name, cookie_val = cookie_match.groups()
            
        go_match = re.search(r'["\'](https?:\\/\\/[^"\']+\?lp_go=[^"\']+)["\']', h2)
        if go_match:
            go_url = go_match.group(1).replace("\\/", "/")
        elif cookie_name:
            go_url = f"https://en.thenaukriadda.in/?lp_go={cookie_name}"
            
        if not go_url or not cookie_name: return None
        
        # Step 3
        headers3 = HEADERS.copy()
        headers3["Cookie"] = f"{cookie_name}={cookie_val}"
        headers3["Referer"] = action1
        r3 = session.get(go_url, headers=headers3)
        
        redirect_match = re.search(r'replace\(\s*["\']([^"\']+)["\']\s*\)', r3.text)
        if redirect_match:
            return redirect_match.group(1).replace("\\/", "/")
        return None
    except Exception:
        return None

def bypass_hrefli(url):
    try:
        host = get_base_url(url)
        session = requests.Session()
        session.headers.update(HEADERS)
        
        # Step 1
        res1 = session.get(url)
        soup1 = BeautifulSoup(res1.text, 'html.parser')
        form_url1 = soup1.find("form", id="landing").get("action")
        form_data1 = {inp.get("name"): inp.get("value", "") for inp in soup1.find("form", id="landing").find_all("input")}
        
        # Step 2
        res2 = session.post(form_url1, data=form_data1)
        soup2 = BeautifulSoup(res2.text, 'html.parser')
        form_url2 = soup2.find("form", id="landing").get("action")
        form_data2 = {inp.get("name"): inp.get("value", "") for inp in soup2.find("form", id="landing").find_all("input")}
        
        # Step 3
        res3 = session.post(form_url2, data=form_data2)
        soup3 = BeautifulSoup(res3.text, 'html.parser')
        script = soup3.find(lambda tag: tag.name == "script" and "?go=" in tag.text)
        script_text = script.text if script else ""
        
        sk_token_match = re.search(r'\?go=([^"]+)', script_text)
        if not sk_token_match: return None
        
        sk_token = sk_token_match.group(1)
        wp_http2 = form_data2.get("_wp_http2", "")
        
        # Step 4
        headers4 = HEADERS.copy()
        headers4["Cookie"] = f"{sk_token}={wp_http2}"
        res4 = session.get(f"{host}?go={sk_token}", headers=headers4)
        
        soup4 = BeautifulSoup(res4.text, 'html.parser')
        meta_refresh = soup4.find("meta", attrs={"http-equiv": "refresh"})
        meta_content = meta_refresh.get("content", "") if meta_refresh else ""
        
        drive_url_match = re.search(r'url=(.+)', meta_content)
        if not drive_url_match: return None
        drive_url = drive_url_match.group(1)
        
        # Step 5
        res5 = session.get(drive_url)
        path_match = re.search(r'replace\("([^"]+)"\)', res5.text)
        if not path_match or path_match.group(1) == "/404": return None
        
        return fix_url(path_match.group(1), get_base_url(drive_url))
    except Exception:
        return None

def extract_video_seed(finallink):
    try:
        url_obj = urlparse(finallink)
        host = "video-leech.xyz" if "video-leech" in finallink else (url_obj.netloc or "video-seed.xyz")
        token = finallink.split("?url=")[1] if "?url=" in finallink else finallink
        if not token: return None
        
        headers = HEADERS.copy()
        headers.update({
            "x-token": host,
            "Referer": finallink,
            "Content-Type": "application/x-www-form-urlencoded"
        })
        
        res = requests.post(f"https://{host}/api", headers=headers, data={"keys": token})
        data = res.json()
        if data and data.get("url"):
            return data["url"].replace("\\/", "/")
        return None
    except Exception:
        return None

def instant_link(url):
    try:
        if "cdn.video-gen.xyz" in url:
            res = requests.get(url, headers=HEADERS, allow_redirects=True)
            if "url=" in res.url:
                redirected = res.url.split("url=")[1]
                if redirected and "?url=" not in redirected:
                    return redirected
                return extract_video_seed(redirected)
                
        if "?url=" in url:
            return extract_video_seed(url)
            
        return url
    except Exception:
        return None

def resume_bot(url):
    try:
        res = requests.get(url, headers=HEADERS)
        html = res.text
        
        set_cookie = res.headers.get("set-cookie", "")
        ssid_match = re.search(r'PHPSESSID=([^;]+)', set_cookie)
        ssid = ssid_match.group(1) if ssid_match else ""
        
        token_match = re.search(r"formData\.append\('token',\s*'([a-f0-9]+)'\)", html)
        path_match = re.search(r"fetch\('/download\?id=([a-zA-Z0-9/+]+)'", html)
        
        if not token_match or not path_match: return None
        
        base_url = url[:url.find("/download")]
        download_url = f"{base_url}/download?id={path_match.group(1)}"
        
        headers = HEADERS.copy()
        headers.update({
            "Accept": "*/*",
            "Origin": base_url,
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/x-www-form-urlencoded"
        })
        if ssid:
            headers["Cookie"] = f"PHPSESSID={ssid}"
            
        post_res = requests.post(download_url, headers=headers, data={"token": token_match.group(1)})
        data = post_res.json()
        
        return data.get("url") if data and data.get("url") else None
    except Exception:
        return None

if __name__ == "__main__":
    print("Starting scraper and generating M3U playlist...")
    
    # 1. Fetch details for a movie (Example: The Matrix - TMDB ID 603)
    meta = fetch_tmdb_details("603", "movie")
    
    m3u_content = "#EXTM3U\n"
    
    if meta:
        # 2. Create the stream configuration
        stream_data = {
            "title": meta["title"],
            "year": meta["year"],
            "quality": "1080p",
            "server": "MoviesMod",
            "provider": "AutoScraper",
            "defaultLang": "🇮🇳 Hindi"
        }
        
        # 3. Format it using your custom Chole Card function
        card = format_chole_card(stream_data)
        
        # 4. Add it to the M3U text
        # (You will replace fake_stream_url with the output of your actual scraper loop later)
        fake_stream_url = "https://example.com/stream/matrix.mp4"
        
        m3u_content += f'#EXTINF:-1 group-title="Movies",{card["name"]}\n'
        m3u_content += f'{fake_stream_url}\n'

    # 5. Save the file so GitHub Actions can commit it
    with open("playlist.m3u", "w", encoding="utf-8") as f:
        f.write(m3u_content)
        
    print("playlist.m3u has been generated successfully!")
