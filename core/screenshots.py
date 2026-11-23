"""
Screenshots of critical web findings, taken with a headless Chromium browser
(Edge, Chrome or Chromium already installed on the machine; nothing is downloaded).

The request is never sent again. The browser renders the response the test
received (with the page's own address as base, so its styles and images load)
and only the part of the page that shows the problem is captured: around the
XSS marker when an injected script ran, otherwise around the visible content.
This also works for sites the browser itself refuses to open, such as
deliberately vulnerable practice sites that Edge SmartScreen blocks.

The browser is driven over the DevTools protocol and is always closed with its
whole process tree, so no background browser keeps using memory.
"""

from __future__ import annotations

import base64
import json
import re
from html import escape as html_escape
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterator, List, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from core.engine import Finding
    from core.models import TestCase

logger = logging.getLogger(__name__)

SEVERITIES = ("CRITICAL", "HIGH")
MAX_PER_RUN = 10
VIEWPORT = (1280, 800)
PAGE_TIMEOUT = 15.0

_CANDIDATES = {
    "win32": [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ],
    "darwin": [
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
}
_ON_PATH = ("msedge", "microsoft-edge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


# Runs before every page's own scripts. The XSS test injects `bughunter_xss_probe()`;
# if the page executes it, a red banner makes the proof visible in the screenshot.
XSS_MARKER = """
window.bughunter_xss_probe = function () {
  // Placed right where the page executed the injected script, so the crop shows the injection point.
  const where = document.currentScript;
  const bar = document.createElement('div');
  bar.setAttribute('data-bughunter-xss', '1');
  bar.textContent = 'XSS: sayfaya enjekte edilen kod tarayıcıda çalıştı (bughunter_xss_probe)';
  bar.setAttribute('style', 'display:block;margin:8px 0;padding:10px 14px;border-radius:6px;background:#b91c1c;'
    + 'color:#fff;font:600 15px/1.4 system-ui,sans-serif;text-align:left;max-width:760px');
  if (where && where.parentNode) where.parentNode.insertBefore(bar, where.nextSibling);
  else (document.body || document.documentElement).prepend(bar);
};
"""

# Returns the page area worth showing, in page coordinates: around the XSS banner when the
# injection ran, otherwise the box around the visible text and controls (capped in size).
FOCUS_SCRIPT = """
(() => {
  const PAD = 16, MIN_W = 480, MIN_H = 90, MAX_H = 640;
  const page = document.documentElement;
  const pageW = Math.max(page.scrollWidth, innerWidth), pageH = Math.max(page.scrollHeight, innerHeight);
  const boxes = [];
  const add = r => { if (r.width > 0 && r.height > 0) boxes.push({l: r.left + scrollX, t: r.top + scrollY, r: r.right + scrollX, b: r.bottom + scrollY}); };
  const bar = document.querySelector('[data-bughunter-xss]');
  let maxH = MAX_H;
  if (bar) {
    const r = bar.getBoundingClientRect();
    add({left: r.left, right: r.right, width: r.width, height: r.height + 220, top: r.top - 160, bottom: r.bottom + 60});
    const host = bar.parentElement && bar.parentElement.getBoundingClientRect();
    if (host && host.width < 1100) add({left: host.left, right: host.right, width: host.width, height: 1, top: r.top, bottom: r.top + 1});
    maxH = 420;
  } else {
    const walker = document.createTreeWalker(document.body || page, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!node.textContent.trim() || !node.parentElement || node.parentElement.closest('script,style,noscript')) continue;
      const style = getComputedStyle(node.parentElement);
      if (style.visibility === 'hidden' || style.display === 'none') continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const r of range.getClientRects()) add(r);
    }
    document.querySelectorAll('img,input,button,textarea,select,video,canvas,svg,iframe').forEach(e => add(e.getBoundingClientRect()));
  }
  if (!boxes.length) return null;
  let l = Math.min(...boxes.map(b => b.l)) - PAD, t = Math.min(...boxes.map(b => b.t)) - PAD;
  let r = Math.max(...boxes.map(b => b.r)) + PAD, b = Math.max(...boxes.map(b => b.b)) + PAD;
  l = Math.max(0, l); t = Math.max(0, t); r = Math.min(pageW, Math.max(r, l + MIN_W)); b = Math.min(pageH, Math.max(b, t + MIN_H));
  if (b - t > maxH) b = t + maxH;
  return {x: Math.floor(l), y: Math.floor(t), width: Math.ceil(r - l), height: Math.ceil(b - t)};
})()
"""


class ScreenshotError(RuntimeError):
    pass


def find_browser(configured: str = "") -> Optional[str]:
    """The configured browser, else the first installed Edge/Chrome/Chromium."""
    if configured:
        return configured if Path(configured).is_file() else None
    for candidate in _CANDIDATES.get(sys.platform, []):
        path = Path(os.path.expandvars(candidate))
        if path.is_file():
            return str(path)
    for name in _ON_PATH:
        found = shutil.which(name)
        if found:
            return found
    return None


def worth_capturing(finding: Finding, test: Optional[TestCase], result=None) -> bool:
    """Serious web findings whose response is a page the browser can show."""
    if test is None or finding.evidence.severity.value not in SEVERITIES:
        return False
    if finding.remediation is not None and not finding.remediation.noteworthy:
        return False
    if result is not None:
        body = getattr(result, "_full_body", None)
        kind = _content_type(getattr(result, "response_headers", {}) or {})
        if not body or not (kind.startswith("text/") or "html" in kind or not kind):
            return False
    return True


def _content_type(headers: Dict[str, str]) -> str:
    for name, value in headers.items():
        if name.lower() == "content-type":
            return value.split(";")[0].strip().lower()
    return ""


def caption_for(test: TestCase, status_code: Optional[int]) -> str:
    path = urlsplit(test.url).path or "/"
    status = f" · HTTP {status_code}" if status_code else ""
    return f"{test.method.upper()} {path}{status} · testin aldığı yanıt"


def _with_query(url: str, params: Dict) -> str:
    if not params:
        return url
    parts = urlsplit(url)
    query = "&".join(filter(None, [parts.query, urlencode(params, doseq=True)]))
    return urlunsplit(parts._replace(query=query))


class Browser:
    """One headless browser for a batch of captures. Use as a context manager."""

    def __init__(self, executable: str, headers: Optional[Dict[str, str]] = None):
        self.executable = executable
        self.headers = headers or {}
        self._process: Optional[subprocess.Popen] = None
        self._profile: Optional[str] = None
        self._ws = None
        self._stack = ExitStack()
        self._next_id = 0

    # -- lifecycle ------------------------------------------------------------------
    def __enter__(self) -> Browser:
        from websockets.sync.client import connect

        self._profile = tempfile.mkdtemp(prefix="bughunter-browser-")
        args = [self.executable, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-background-networking", "--disable-sync", "--mute-audio",
                "--hide-scrollbars", f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}",
                "--remote-debugging-port=0", f"--user-data-dir={self._profile}", "about:blank"]
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self._process = subprocess.Popen(args, **kwargs)
        try:
            port = self._wait_for_port()
            self._ws = self._stack.enter_context(connect(self._page_socket(port), max_size=50_000_000, open_timeout=10))
            self._call("Page.enable")
            self._call("Page.addScriptToEvaluateOnNewDocument", source=XSS_MARKER)
            self._call("Network.enable")
            if self.headers:
                self._call("Network.setExtraHTTPHeaders", headers=self.headers)
            self._call("Emulation.setDeviceMetricsOverride", width=VIEWPORT[0], height=VIEWPORT[1],
                       deviceScaleFactor=1, mobile=False)
            # Light theme, like most visitors: plain-text error pages would otherwise render dark.
            self._call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _wait_for_port(self) -> int:
        marker = Path(self._profile) / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        # On Windows msedge.exe may hand over to a new process and exit at once, so the
        # launcher exiting is not a failure: the profile's port file is the readiness signal.
        while time.monotonic() < deadline:
            try:
                return int(marker.read_text().splitlines()[0])
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        raise ScreenshotError("Tarayıcı başlatılamadı ya da zamanında hazır olmadı.")

    @staticmethod
    def _page_socket(port: int) -> str:
        import urllib.request

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as response:
                    pages = [t for t in json.load(response) if t.get("type") == "page"]
                if pages:
                    return pages[0]["webSocketDebuggerUrl"]
            except OSError:
                pass
            time.sleep(0.1)
        raise ScreenshotError("Tarayıcı sekmesine bağlanılamadı.")

    def close(self) -> None:
        """Closes the browser and every child process, then removes the temporary profile."""
        if self._ws is not None:
            try:
                self._call("Browser.close", timeout=3)
            except Exception:
                pass
            try:
                self._stack.close()
            except Exception:
                pass
            self._ws = None
        if self._process is not None:
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if self._process.poll() is None:
                self._kill_tree(self._process.pid)
            self._process = None
        if self._profile and sys.platform == "win32":
            self._kill_profile_processes(self._profile, self.executable)  # incl. the process msedge.exe handed over to
        if self._profile:
            for _ in range(10):  # Windows keeps files locked for a moment after exit
                shutil.rmtree(self._profile, ignore_errors=True)
                if not Path(self._profile).exists():
                    break
                time.sleep(0.3)
            self._profile = None

    @staticmethod
    def _kill_tree(pid: int) -> None:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            else:
                os.killpg(pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass

    @staticmethod
    def _kill_profile_processes(profile: str, executable: str) -> None:
        """Safety net: ends any process still running with this run's temporary profile."""
        name = Path(profile).name  # bughunter-browser-xxxx: unique to this browser
        image = Path(executable).name.replace("'", "")
        # Filter by the browser's image name: this PowerShell's own command line contains the
        # profile name too, and must not match (it would stop itself first).
        script = (f"Get-CimInstance Win32_Process -Filter \"Name='{image}'\" | "
                  f"Where-Object {{ $_.CommandLine -like '*{name}*' }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass

    # -- DevTools protocol --------------------------------------------------------------
    def _call(self, method: str, timeout: float = PAGE_TIMEOUT, **params) -> dict:
        self._next_id += 1
        my_id = self._next_id
        self._ws.send(json.dumps({"id": my_id, "method": method, "params": params}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ScreenshotError(f"{method} zaman aşımına uğradı.")
            message = json.loads(self._ws.recv(timeout=remaining))
            if message.get("id") == my_id:
                if "error" in message:
                    raise ScreenshotError(f"{method}: {message['error'].get('message')}")
                return message.get("result", {})

    def _wait_for_load(self, timeout: float = PAGE_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                page = self._call("Runtime.evaluate", returnByValue=True,
                                  expression="[document.readyState, location.href]")["result"].get("value") or []
            except ScreenshotError:  # the page is being replaced right now
                page = []
            if len(page) == 2 and page[0] == "complete" and page[1] != "about:blank":
                time.sleep(0.4)  # let late images and fonts paint
                return
            time.sleep(0.2)

    # -- capturing --------------------------------------------------------------------
    def capture(self, test: TestCase, result) -> bytes:
        """Shows the response the test received and returns a PNG of the part that matters."""
        body = getattr(result, "_full_body", None)
        if not body:
            raise ScreenshotError("Testin aldığı yanıt yok; görüntü alınmadı.")
        kind = _content_type(result.response_headers or {}) or "text/html"
        page_url = _with_query(test.url, test.query_params)
        if "html" in kind:
            # Resolve the page's relative styles, images and links against its real address.
            base = f'<base href="{html_escape(page_url, quote=True)}">'
            match = re.search(r"<head[^>]*>", body, re.IGNORECASE)
            body = body[:match.end()] + base + body[match.end():] if match else base + body
        else:
            kind = "text/plain"
        data_url = f"data:{kind};charset=utf-8;base64," + base64.b64encode(body.encode("utf-8")).decode()
        navigation = self._call("Page.navigate", url=data_url)
        if navigation.get("errorText"):
            raise ScreenshotError(f"Sayfa gösterilemedi: {navigation['errorText']}")
        time.sleep(0.2)
        self._wait_for_load()
        href = self._call("Runtime.evaluate", expression="location.href", returnByValue=True)["result"].get("value", "")
        if str(href).startswith("chrome-error:"):
            raise ScreenshotError("Tarayıcı sayfayı gösteremedi.")
        clip = self._call("Runtime.evaluate", expression=FOCUS_SCRIPT, returnByValue=True)["result"].get("value")
        if not clip:
            raise ScreenshotError("Sayfada gösterilecek içerik yok.")
        data = self._call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                          clip={**clip, "scale": 1})["data"]
        return base64.b64decode(data)


    def print_pdf(self, html: str, footer: str = "BugHunter test raporu") -> bytes:
        """Prints an HTML document to an A4 PDF with page numbers in the footer."""
        page = Path(self._profile) / "report.html"  # inside the temporary profile: removed on close
        page.write_text(html, encoding="utf-8")
        navigation = self._call("Page.navigate", url=page.as_uri())
        if navigation.get("errorText"):
            raise ScreenshotError(f"Rapor açılamadı: {navigation['errorText']}")
        time.sleep(0.2)
        self._wait_for_load()
        # Paper cannot be clicked: print collapsed sections (evidence script, technical details) open.
        self._call("Runtime.evaluate", expression="document.querySelectorAll('details').forEach(d => d.open = true)")
        footer_html = ('<div style="width:100%;padding:0 14mm;font:8px system-ui,sans-serif;color:#64748b;'
                       'display:flex;justify-content:space-between"><span>' + html_escape(footer) + '</span>'
                       '<span>sayfa <span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
        data = self._call("Page.printToPDF", timeout=90, printBackground=True, preferCSSPageSize=True,
                          displayHeaderFooter=True, headerTemplate="<span></span>", footerTemplate=footer_html,
                          marginTop=0.5, marginBottom=0.6, marginLeft=0.5, marginRight=0.5)["data"]
        return base64.b64decode(data)


@contextmanager
def open_browser(executable: str, headers: Optional[Dict[str, str]] = None) -> Iterator[Browser]:
    with Browser(executable, headers) as browser:
        yield browser


def capture_findings(findings: List[Finding], tests: Dict[str, TestCase], results: Dict[str, object],
                     executable: str, headers: Optional[Dict[str, str]] = None,
                     limit: int = MAX_PER_RUN, browser_factory=open_browser) -> Dict[str, dict]:
    """Returns {test_id: {"png": bytes, "caption": str}} for the findings worth a screenshot."""
    chosen = []
    for finding in findings:  # already ordered most severe first
        test = tests.get(finding.evidence.test_id or "")
        result = results.get(test.id) if test else None
        if worth_capturing(finding, test, result) and test.id not in {t.id for t in chosen}:
            chosen.append(test)
        if len(chosen) >= limit:
            break
    if not chosen:
        return {}
    shots: Dict[str, dict] = {}
    with browser_factory(executable, headers) as browser:
        for test in chosen:
            try:
                result = results.get(test.id)
                shots[test.id] = {"png": browser.capture(test, result),
                                  "caption": caption_for(test, getattr(result, "status_code", None))}
            except Exception as error:  # one page failing must not lose the others
                logger.warning("Screenshot of %s failed: %s", test.id, error)
    return shots
