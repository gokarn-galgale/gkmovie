import concurrent.futures
from datetime import datetime, timezone, timedelta
import os
import re
import threading
from urllib.parse import unquote, urlparse
from bs4 import BeautifulSoup
import cloudscraper

# --- কনফিগারেশন ---
BASE_URL = "https://fibwatch.art"
PAGES_TO_SCAN = 1000       # ইনক্রিমেন্টাল আপডেটের জন্য প্রতি ক্যাটাগরিতে কয়টি পেজ স্ক্যান করবে
IMAGE_PROXY = "https://srhady-live-stream.hf.space/image?url="
MAX_WORKERS = 15           # থ্রেড সংখ্যা

# প্রতিটি থ্রেডের জন্য আলাদা স্ক্র্যাপার সেশন
thread_local = threading.local()

def get_scraper():
    if not hasattr(thread_local, "scraper"):
        thread_local.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
    return thread_local.scraper

def get_resolution(text):
    """লিংক থেকে রেজোলিউশন বের করে সর্বোচ্চ কোয়ালিটি নিশ্চিত করার জন্য"""
    match = re.search(r'(\d{3,4})p', text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if '4k' in text.lower():
        return 2160
    return 0

def get_domain(url):
    """লিংক থেকে মূল CDN ডোমেন এক্সট্রাক্ট করে"""
    parsed_uri = urlparse(url)
    return f"{parsed_uri.scheme}://{parsed_uri.netloc}"

def process_movie(base_name, watch_link, group_name):
    """মুভির আসল ভিডিও লিংক ও পোস্টার প্রক্সি লিংক বের করে"""
    scraper = get_scraper()
    try:
        res = scraper.get(watch_link, timeout=15)
        if res.status_code != 200:
            return None

        parser_type = 'lxml' if 'lxml' in BeautifulSoup.__module__ else 'html.parser'
        watch_soup = BeautifulSoup(res.text, parser_type)
        
        actual_link = None
        for a in watch_soup.find_all('a', href=True):
            href = a['href']
            
            # urlshortlink থেকে ডিকোড করার লজিক
            if 'urlshortlink.top' in href and 'url=' in href:
                match = re.search(r'url=(.*)', href)
                if match:
                    decoded = unquote(match.group(1))
                    if any(ext in decoded.lower() for ext in ('.mkv', '.mp4')):
                        actual_link = decoded
                        break
            
            # ডিরেক্ট মিডিয়া লিংক থাকলে
            elif any(ext in href.lower() for ext in ('.mkv', '.mp4')) and 'urlshortlink.top' not in href:
                actual_link = href if href.startswith('http') else f"{BASE_URL}{href}"
                break
        
        if not actual_link:
            return None
            
        poster_tag = watch_soup.find('meta', property='og:image')
        poster = poster_tag['content'] if poster_tag else ""
        if poster:
            poster = f"{IMAGE_PROXY}{poster}"
        
        # ফাইলনেম পরিষ্কার করা
        file_name = actual_link.split('/')[-1].split('?')[0]
        file_name = re.sub(r'\[Fibwatch\.Com\]|\.mkv|\.mp4', '', file_name, flags=re.IGNORECASE).replace('.', ' ').strip()
        final_video_link = f"{actual_link}|Referer={BASE_URL}/"
        
        m3u_entry = f'#EXTINF:-1 tvg-logo="{poster}" group-title="{group_name}", {file_name}\n{final_video_link}\n'
        return m3u_entry, get_domain(actual_link), file_name
        
    except Exception:
        return None

def scan_single_page(cat_id, page_num):
    """ক্যাটাগরি পেজ স্ক্যান করে ওয়াচ লিংকগুলো সংগ্রহ করে"""
    url = f"{BASE_URL}/videos/category/{cat_id}?page_id={page_num}"
    scraper = get_scraper()
    found_movies = []
    try:
        response = scraper.get(url, timeout=15)
        if response.status_code != 200:
            return []

        parser_type = 'lxml' if 'lxml' in BeautifulSoup.__module__ else 'html.parser'
        soup = BeautifulSoup(response.text, parser_type)
        
        links = soup.find_all('a', href=True)
        watch_links = [l['href'] for l in links if '/watch/' in l['href'] and l['href'].endswith('.html')]
        
        for link in set(watch_links):
            full_link = link if link.startswith('http') else f"{BASE_URL}{link}"
            filename = full_link.split('/')[-1]
            base_name = re.sub(r'[-_]?\d{3,4}p.*\.html$', '', filename, flags=re.IGNORECASE)
            found_movies.append((base_name, full_link))
            
        return found_movies
    except Exception:
        return []

def run_category_scraper(cat_id, file_name, group_name):
    print(f"\n=======================================================")
    print(f"🚀 Processing Category: {group_name} (ID: {cat_id})")
    print(f"=======================================================")

    # ১. বিদ্যমান M3U ফাইল থেকে পুরনো ডাটা ও CDN লোড করা
    old_entries = []
    old_domain = None
    if os.path.exists(file_name):
        with open(file_name, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            old_entries = [line for line in lines if not line.startswith('#EXTM3U') and not line.startswith('# Playlist') and not line.startswith('# Last')]
            
            for line in old_entries:
                if '.mkv' in line or '.mp4' in line:
                    clean_link = line.split('|')[0].strip()
                    old_domain = get_domain(clean_link)
                    break
        print(f"📁 Existing file found. Previous CDN: {old_domain}")

    # ২. প্রথম কয়েকটি পেজ দ্রুত স্ক্যান করা
    print(f"⏳ Scanning top {PAGES_TO_SCAN} pages for latest releases...")
    new_movies_links = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_page = {executor.submit(scan_single_page, cat_id, p): p for p in range(1, PAGES_TO_SCAN + 1)}
        for future in concurrent.futures.as_completed(future_to_page):
            for base_name, full_link in future.result():
                current_res = get_resolution(full_link)
                
                # সর্বোচ্চ রেজোলিউশনের লিংক রাখা
                if base_name in new_movies_links:
                    existing_link = new_movies_links[base_name]
                    if current_res > get_resolution(existing_link):
                        new_movies_links[base_name] = full_link
                else:
                    new_movies_links[base_name] = full_link

    print(f"🔎 Found {len(new_movies_links)} candidates from latest pages.")

    # ৩. নতুন লিংক এক্সট্রাক্ট করা ও CDN শনাক্তকরণ
    new_entries = []
    new_domain = None
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_movie, b_name, w_link, group_name): b_name 
            for b_name, w_link in new_movies_links.items()
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                entry, domain, clean_title = result
                
                # টাইটেল চেক করে ডুপ্লিকেট পরিহার করা
                title_line = entry.split('\n')[0]
                if not any(title_line in old_line for old_line in old_entries):
                    new_entries.append(entry)
                    print(f"   ⚡ New Movie Added: {clean_title[:45]}")
                    if not new_domain:
                        new_domain = domain
                        print(f"   🌐 Active CDN Domain: {new_domain}")

    # ৪. যদি CDN পরিবর্তিত হয়ে থাকে, তবে আগের ফাইলগুলোর লিংক রিপ্লেস করা
    if old_domain and new_domain and old_domain != new_domain:
        print(f"🔄 CDN Change Detected! Updating links from {old_domain} to {new_domain}...")
        old_entries_text = "".join(old_entries)
        old_entries_text = old_entries_text.replace(old_domain, new_domain)
        old_entries = [old_entries_text]

    # ৫. ক্যাটাগরি ফাইল সেভ করা
    bd_time = datetime.now(timezone.utc) + timedelta(hours=6)
    now = bd_time.strftime("%Y-%m-%d %I:%M:%S %p (BD Time)")
    
    print(f"💾 Saving to {file_name} (New entries: {len(new_entries)})...")
    with open(file_name, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        f.write(f'# Playlist Generated Automatically for {group_name}\n')
        f.write(f'# Last Updated: {now}\n\n')
        
        # নতুন মুভিগুলো তালিকার উপরে থাকবে
        for entry in new_entries:
            f.write(entry)
            
        # পুরনো ডাটা যোগ করা
        f.write("".join(old_entries))

    print(f"✅ Successfully updated {file_name}!")

def main():
    # আগের কোডের মতো ক্যাটাগরি ম্যাপিং
    categories = [
        {"cat_id": "4", "file_name": "Hindi-Movies.m3u", "group_name": "Hindi Movies"},
        {"cat_id": "5", "file_name": "Hindi-DubbedMovies.m3u", "group_name": "Hindi-Dubbed Movies"},
        {"cat_id": "13", "file_name": "Marathi-Movies.m3u", "group_name": "Marathi Movies"},
        {"cat_id": "7", "file_name": "Cartoon-Movies.m3u", "group_name": "Cartoon Movies"}
    ]
    
    for cat in categories:
        run_category_scraper(
            cat_id=cat["cat_id"],
            file_name=cat["file_name"],
            group_name=cat["group_name"]
        )
    
    print("\n🎉 All category playlists have been processed and updated successfully!")

if __name__ == "__main__":
    main()
