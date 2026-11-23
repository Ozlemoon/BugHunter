"""
Screenshots of critical web findings, taken with a headless Chromium browser
(Edge, Chrome or Chromium already installed on the machine; nothing is downloaded).

For each CRITICAL/HIGH web finding the failing request is replayed in the
browser — a GET page is opened, a form is filled in and submitted — and the
page is captured as PNG. Destructive methods (DELETE, PUT, PATCH) are never
replayed. Without a browser, screenshots are skipped and the run is unaffected.

The browser is driven over the DevTools protocol and is always closed with its
whole process tree, so no background browser keeps using memory.
"""

from __future__ import annotations

import base64
import json
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
REPLAYABLE = ("GET", "POST")
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
  const show = () => {
    const bar = document.createElement('div');
    bar.textContent = 'XSS: sayfaya enjekte edilen kod tarayıcıda çalıştı (bughunter_xss_probe)';
    bar.setAttribute('style', 'position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:12px 16px;'
      + 'background:#b91c1c;color:#fff;font:600 15px/1.4 system-ui,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.3)');
    document.documentElement.appendChild(bar);
    document.body && (document.body.style.marginTop = '52px');
  };
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', show) : show();
};
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


def worth_capturing(finding: Finding, test: Optional[TestCase]) -> bool:
    """Only serious web findings whose request can be replayed without side effects."""
    if test is None or finding.evidence.severity.value not in SEVERITIES:
        return False
    if finding.remediation is not None and not finding.remediation.noteworthy:
        return False
    if test.method.upper() not in REPLAYABLE:
        return False
    if test.method.upper() == "POST" and not (test.is_form_data or test.payload is None):
        return False  # JSON bodies are API-style requests, not something a page shows
    return True


def caption_for(test: TestCase, status_code: Optional[int]) -> str:
    path = urlsplit(test.url).path or "/"
    status = f" · HTTP {status_code}" if status_code else ""
    action = "form gönderildi" if test.method.upper() == "POST" else "sayfa açıldı"
    return f"{test.method.upper()} {path}{status} · tarayıcıda yeniden {action}"


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
    def capture(self, test: TestCase) -> bytes:
        """Replays the test's request in the browser and returns a PNG of the page."""
        method = test.method.upper()
        if method == "GET":
            self._call("Page.navigate", url=_with_query(test.url, test.query_params))
        elif method == "POST":
            self._call("Page.navigate", url="about:blank")
            fields = test.payload if isinstance(test.payload, dict) else {}
            # Build and submit the same form the test sent; values are passed as JSON data, not code.
            script = ("(() => { const f = document.createElement('form'); f.method = 'POST';"
                      f" f.action = {json.dumps(_with_query(test.url, test.query_params))};"
                      f" for (const [k, v] of Object.entries({json.dumps({str(k): '' if v is None else str(v) for k, v in fields.items()})}))"
                      " { const i = document.createElement('input'); i.type = 'hidden'; i.name = k; i.value = v; f.appendChild(i); }"
                      " document.body.appendChild(f); f.submit(); return true; })()")
            self._call("Runtime.evaluate", expression=script, returnByValue=True)
        else:
            raise ScreenshotError(f"{method} istekleri tarayıcıda tekrarlanmaz.")
        time.sleep(0.3)
        self._wait_for_load()
        data = self._call("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"]
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
        if worth_capturing(finding, test) and test.id not in {t.id for t in chosen}:
            chosen.append(test)
        if len(chosen) >= limit:
            break
    if not chosen:
        return {}
    shots: Dict[str, dict] = {}
    with browser_factory(executable, headers) as browser:
        for test in chosen:
            try:
                status = getattr(results.get(test.id), "status_code", None)
                shots[test.id] = {"png": browser.capture(test), "caption": caption_for(test, status)}
            except Exception as error:  # one page failing must not lose the others
                logger.warning("Screenshot of %s failed: %s", test.id, error)
    return shots
