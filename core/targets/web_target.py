"""
Web Application & Website Target Parser.
Analyzes HTML pages, extracting forms, links, scripts, and media assets.
"""

from urllib.parse import urljoin
from typing import Optional
from bs4 import BeautifulSoup
import httpx

from core.models import WebPageSpec, WebLinkSpec, WebFormSpec, FormFieldSpec


class WebTargetParser:
    """Extracts testable components from HTML web pages."""

    def __init__(self, target_url: str):
        self.target_url = target_url
        self.raw_html: str = ""
        self.status_code: Optional[int] = None

    def fetch(self) -> str:
        """Fetches HTML content from target URL."""
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(self.target_url)
            self.status_code = resp.status_code
            self.raw_html = resp.text
        return self.raw_html

    def parse(self) -> WebPageSpec:
        """Parses the fetched HTML page into WebPageSpec."""
        if not self.raw_html:
            self.fetch()

        soup = BeautifulSoup(self.raw_html, "html.parser")
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None

        # Extract links
        links = []
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.startswith("javascript:") or href.startswith("#"):
                continue
            abs_href = urljoin(self.target_url, href)
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
            assets=assets,
            forms=forms
        )
