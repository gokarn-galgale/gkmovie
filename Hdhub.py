import argparse
import json
import re
import sys
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests

DEFAULT_BASE_URL = "https://hdhub4u.ms"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class HDHub4uScraper:
    def __init__(self, base_url: str = DEFAULT_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def search(self, query: str) -> list[dict]:
        """Search the website for titles matching query."""
        search_url = f"{self.base_url}/?s={requests.utils.quote(query)}"
        resp = self.session.get(search_url, timeout=15)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []

        # HDHub4u posts are usually thumb-listing articles
        for article in soup.select("article, .post, .thumb-listing figure"):
            a_tag = article.find("a")
            if not a_tag or not a_tag.get("href"):
                continue

            img_tag = article.find("img")
            title = (
                a_tag.get("title")
                or (img_tag.get("alt") if img_tag else None)
                or a_tag.get_text(strip=True)
            )
            link = a_tag["href"]
            poster = (
                img_tag.get("data-src")
                or img_tag.get("src")
                if img_tag
                else None
            )

            if title and link:
                results.append({
                    "title": title,
                    "url": link,
                    "poster": poster
                })

        return results

    def get_details_and_links(self, post_url: str) -> dict:
        """Extract title, metadata, and quality links from a post."""
        resp = self.session.get(post_url, timeout=15)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        title_el = soup.select_one("h1.entry-title, .page-title, h1")
        title = title_el.get_text(strip=True) if title_el else "Unknown Title"

        download_links = []
        for a in soup.select("a[href]"):
            href = a["href"]
            text = a.get_text(strip=True)

            # Match common host keywords used by HDHub4u
            if any(k in href.lower() for k in ["hubcloud", "driveleech", "gadget", "links"]):
                # Try inferring resolution/quality from context
                parent_text = a.find_parent(["p", "h5", "h4", "div"])
                context_info = parent_text.get_text(strip=True) if parent_text else text

                download_links.append({
                    "label": text or "Download/Stream",
                    "context": context_info,
                    "redirect_url": href,
                })

        return {
            "title": title,
            "url": post_url,
            "links": download_links,
        }

    def resolve_hubcloud_stream(self, hubcloud_url: str) -> dict | None:
        """Attempt following HubCloud landing/intermediate page to obtain download/stream link."""
        try:
            resp = self.session.get(
                hubcloud_url,
                headers={"Referer": self.base_url},
                timeout=15,
                allow_redirects=True,
            )
            soup = BeautifulSoup(resp.text, "html.parser")

            # Look for typical HubCloud direct stream anchor buttons
            btn = soup.select_one("a.btn-success, a#download, a[href*='download'], a[href*='stream']")
            if btn and btn.get("href"):
                direct_url = urljoin(hubcloud_url, btn["href"])
                return {
                    "provider": "HubCloud",
                    "stream_url": direct_url,
                    "headers": {
                        "Referer": hubcloud_url,
                        "User-Agent": DEFAULT_HEADERS["User-Agent"],
                    },
                }

            # Check for inline redirection scripts or variables
            match = re.search(r'var\s+url\s*=\s*[\'"]([^\'"]+)[\'"]', resp.text)
            if match:
                return {
                    "provider": "HubCloud",
                    "stream_url": match.group(1),
                    "headers": {"Referer": hubcloud_url},
                }

        except Exception as err:
            return {"error": str(err)}

        return None


def main():
    parser = argparse.ArgumentParser(description="HDHub4u search and extraction utility")
    parser.add_argument("--search", type=str, help="Search query (e.g. 'Inception')")
    parser.add_argument("--post", type=str, help="Full HDHub4u post URL to inspect")
    parser.add_argument("--resolve", type=str, help="HubCloud redirect URL to resolve")
    args = parser.parse_args()

    scraper = HDHub4uScraper()

    if args.search:
        results = scraper.search(args.search)
        print(json.dumps(results, indent=2))
    elif args.post:
        details = scraper.get_details_and_links(args.post)
        print(json.dumps(details, indent=2))
    elif args.resolve:
        stream = scraper.resolve_hubcloud_stream(args.resolve)
        print(json.dumps(stream, indent=2))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
