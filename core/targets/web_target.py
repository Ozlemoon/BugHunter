"""
Web Application & Website Target Parser.
Analyzes HTML pages, extracting forms, links, scripts, and media assets.
"""

from typing import Dict, Optional
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from core.models import FormFieldSpec, WebFormSpec, WebLinkSpec, WebPageSpec

SKIPPED_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "#")


class WebTargetParser:
    """Extracts testable components from HTML web pages."""

    def __init__(self, target_url: str, timeout: float = 10.0, headers: Optional[Dict[str, str]] = None):
        self.target_url = target_url
        self.timeout = timeout
        self.headers = headers or {}
        self.raw_html: str = ""
        self.status_code: Optional[int] = None
        self.response_headers: Dict[str, str] = {}

    def fetch(self) -> str:
        """Fetches HTML content from target URL."""
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=self.headers) as client:
            resp = client.get(self.target_url)
            resp.raise_for_status()
            self.status_code = resp.status_code
            self.response_headers = {k.lower(): v for k, v in resp.headers.items()}
            self.raw_html = resp.text
        return self.raw_html

    def crawl(self, max_pages: int = 5) -> WebPageSpec:
        """
        Breadth-first crawl of same-origin HTML pages starting at target_url.
        Returns one merged spec (links, assets and forms from every page visited),
        keeping the start page's title, URL and response headers.
        """
        root = self.parse()
        if max_pages <= 1:
            return root
        origin = urlparse(self.target_url).netloc
        visited = {urldefrag(self.target_url)[0]}
        queue = [link.href for link in root.links if urlparse(link.href).netloc == origin]
        links = {link.href: link for link in root.links}
        assets = {asset.href: asset for asset in root.assets}
        forms = {(form.action, form.method): form for form in root.forms}

        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=self.headers) as client:
            while queue and len(visited) < max_pages:
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                try:
                    resp = client.get(url)
                except httpx.HTTPError:
                    continue  # unreachable pages are reported by the link tests
                if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
                    continue
                page = WebTargetParser(url)
                page.raw_html = resp.text
                spec = page.parse()
                for link in spec.links:
                    links.setdefault(link.href, link)
                    if urlparse(link.href).netloc == origin and link.href not in visited:
                        queue.append(link.href)
                for asset in spec.assets:
                    assets.setdefault(asset.href, asset)
                for form in spec.forms:
                    forms.setdefault((form.action, form.method), form)

        return root.model_copy(update={
            "links": list(links.values()),
            "assets": list(assets.values()),
            "forms": list(forms.values()),
        })

    def parse(self) -> WebPageSpec:
        """Parses the fetched HTML page into WebPageSpec."""
        if not self.raw_html:
            self.fetch()

        soup = BeautifulSoup(self.raw_html, "html.parser")
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None

        # Extract links (deduplicated, fragments stripped)
        links = []
        seen_links = set()
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            if not href or href.lower().startswith(SKIPPED_SCHEMES):
                continue
            abs_href = urldefrag(urljoin(self.target_url, href))[0]
            if abs_href in seen_links:
                continue
            seen_links.add(abs_href)
            links.append(
                WebLinkSpec(
                    href=abs_href,
                    text=a_tag.get_text(strip=True) or href,
                    tag_name="a"
                )
            )

        # Extract assets (images, scripts, stylesheets)
        assets = []
        for img in soup.find_all("img", src=True):
            assets.append(
                WebLinkSpec(
                    href=urljoin(self.target_url, img["src"]),
                    text=img.get("alt", "image"),
                    tag_name="img"
                )
            )
        for script in soup.find_all("script", src=True):
            assets.append(
                WebLinkSpec(
                    href=urljoin(self.target_url, script["src"]),
                    text="script",
                    tag_name="script"
                )
            )
        for link_tag in soup.find_all("link", href=True):
            assets.append(
                WebLinkSpec(
                    href=urljoin(self.target_url, link_tag["href"]),
                    text=link_tag.get("rel", ["link"])[0] if isinstance(link_tag.get("rel"), list) else "link",
                    tag_name="link"
                )
            )

        # Extract forms
        forms = []
        for form in soup.find_all("form"):
            action = form.get("action", "")
            abs_action = urljoin(self.target_url, action) if action else self.target_url
            method = form.get("method", "POST").upper()

            fields = []
            for inp in form.find_all(["input", "textarea", "select"]):
                name = inp.get("name")
                if not name:
                    continue
                field_type = inp.get("type", "textarea" if inp.name == "textarea" else "text")
                required = inp.has_attr("required")
                placeholder = inp.get("placeholder")
                value = inp.get("value")

                fields.append(
                    FormFieldSpec(
                        name=name,
                        field_type=field_type,
                        required=required,
                        placeholder=placeholder,
                        value=value
                    )
                )

            forms.append(
                WebFormSpec(
                    action=abs_action,
                    method=method,
                    fields=fields
                )
            )

        return WebPageSpec(
            url=self.target_url,
            title=title,
            links=links,
            assets=list({asset.href: asset for asset in assets}.values()),
            forms=forms,
            response_headers=self.response_headers,
        )
