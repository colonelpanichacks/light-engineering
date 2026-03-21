"""Nexus Runner -- Brave browser automation via Chrome DevTools Protocol.

Connects to Brave Browser's CDP endpoint to navigate, click, fill forms,
scroll, read page content, and take screenshots. Zero heavy dependencies --
just websocket-client for the CDP WebSocket.

Setup: Launch Brave with remote debugging enabled:
  /Applications/Brave\ Browser.app/Contents/MacOS/Brave\ Browser --remote-debugging-port=9222

Or let Nexus Runner launch it automatically via open_brave_debug().
"""

import base64
import json
import os
import subprocess
import time

CDP_PORT = 9222
CDP_HOST = "localhost"


def _get_ws_url(port=CDP_PORT):
    """Get WebSocket debugger URL from Brave's CDP endpoint."""
    import requests
    try:
        tabs = requests.get(f"http://{CDP_HOST}:{port}/json", timeout=3).json()
        for tab in tabs:
            if tab.get("type") == "page":
                return tab["webSocketDebuggerUrl"]
    except Exception:
        return None
    return None


def _is_brave_running():
    """Check if any Brave Browser process is running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "Brave Browser"],
            capture_output=True, text=True, timeout=5,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def open_brave_debug(port=CDP_PORT):
    """Launch Brave with remote debugging enabled.

    If Brave is already running without CDP, it must be quit and relaunched.
    """
    ws = _get_ws_url(port)
    if ws:
        return "Brave already running with CDP enabled"

    brave_path = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    if not os.path.isfile(brave_path):
        return "Brave Browser not found at /Applications/Brave Browser.app"

    # If Brave is running WITHOUT CDP, we need to quit it first
    if _is_brave_running():
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "Brave Browser" to quit'],
                capture_output=True, timeout=5,
            )
            # Wait for it to actually quit
            for _ in range(10):
                time.sleep(0.5)
                if not _is_brave_running():
                    break
            time.sleep(1)  # Extra settle time
        except Exception:
            pass

    subprocess.Popen(
        [brave_path, f"--remote-debugging-port={port}", "--remote-allow-origins=*"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for CDP to become available
    for _ in range(15):
        time.sleep(1)
        if _get_ws_url(port):
            return "Brave launched with CDP enabled on port " + str(port)
    return "Brave launched but CDP not responding. Brave may need to be fully quit (check Activity Monitor) and retried."


class BraveCDP:
    """Synchronous Brave browser controller via Chrome DevTools Protocol."""

    def __init__(self, port=CDP_PORT):
        import websocket
        ws_url = _get_ws_url(port)
        if not ws_url:
            raise RuntimeError(
                "Cannot connect to Brave. Launch it with: "
                "brave --remote-debugging-port=9222"
            )
        self.ws = websocket.create_connection(ws_url, timeout=15)
        self.msg_id = 0
        self._send("Page.enable")
        self._send("DOM.enable")
        self._send("Runtime.enable")

    def _send(self, method, **params):
        self.msg_id += 1
        self.ws.send(json.dumps({"id": self.msg_id, "method": method, "params": params}))
        while True:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == self.msg_id:
                if "error" in resp:
                    raise RuntimeError(f"CDP error: {resp['error'].get('message', resp['error'])}")
                return resp.get("result", {})

    def js(self, expression):
        """Execute JavaScript and return the result value."""
        r = self._send(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
        val = r.get("result", {})
        if val.get("type") == "undefined":
            return None
        return val.get("value", str(val))

    def navigate(self, url):
        """Navigate to a URL."""
        self._send("Page.navigate", url=url)
        time.sleep(2)  # Wait for page load
        return f"Navigated to {url}"

    def get_url(self):
        """Get the current page URL."""
        return self.js("window.location.href")

    def get_title(self):
        """Get the current page title."""
        return self.js("document.title")

    def get_text(self, max_chars=3000):
        """Get visible text content of the page."""
        text = self.js("document.body.innerText") or ""
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"
        return text

    def get_html(self, selector="body", max_chars=3000):
        """Get HTML of a specific element."""
        html = self.js(f"document.querySelector('{selector}')?.outerHTML || 'Element not found'")
        if html and len(html) > max_chars:
            html = html[:max_chars] + "\n... (truncated)"
        return html

    def click(self, selector):
        """Click an element by CSS selector."""
        result = self.js(f"""
            (() => {{
                const el = document.querySelector('{selector}');
                if (!el) return 'Element not found: {selector}';
                el.click();
                return 'Clicked: {selector}';
            }})()
        """)
        return result

    def fill(self, selector, value):
        """Fill an input field by CSS selector."""
        escaped = value.replace("'", "\\'").replace("\n", "\\n")
        result = self.js(f"""
            (() => {{
                const el = document.querySelector('{selector}');
                if (!el) return 'Element not found: {selector}';
                el.focus();
                el.value = '{escaped}';
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                return 'Filled: {selector}';
            }})()
        """)
        return result

    def submit(self, selector="form"):
        """Submit a form by CSS selector."""
        result = self.js(f"""
            (() => {{
                const el = document.querySelector('{selector}');
                if (!el) return 'Form not found: {selector}';
                if (el.tagName === 'FORM') el.submit();
                else el.closest('form')?.submit();
                return 'Submitted: {selector}';
            }})()
        """)
        return result

    def scroll(self, direction="down", amount=500):
        """Scroll the page. direction: up/down/top/bottom."""
        if direction == "top":
            self.js("window.scrollTo(0, 0)")
        elif direction == "bottom":
            self.js("window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "up":
            self.js(f"window.scrollBy(0, -{amount})")
        else:
            self.js(f"window.scrollBy(0, {amount})")
        return f"Scrolled {direction}"

    def screenshot(self, path="/tmp/nexus-screenshot.png"):
        """Take a screenshot of the current page."""
        result = self._send("Page.captureScreenshot", format="png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(result["data"]))
        return path

    def list_links(self, max_links=20):
        """List visible links on the page."""
        links = self.js(f"""
            (() => {{
                const links = Array.from(document.querySelectorAll('a[href]'));
                return links.slice(0, {max_links}).map(a => ({{
                    text: a.innerText.trim().substring(0, 80),
                    href: a.href
                }}));
            }})()
        """)
        return links

    def list_inputs(self):
        """List all input fields on the page."""
        inputs = self.js("""
            (() => {
                const els = Array.from(document.querySelectorAll('input, textarea, select'));
                return els.map(el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    name: el.name || '',
                    id: el.id || '',
                    placeholder: el.placeholder || '',
                    value: el.value?.substring(0, 50) || ''
                }));
            })()
        """)
        return inputs

    def wait_for(self, selector, timeout=10):
        """Wait for an element to appear on the page."""
        start = time.time()
        while time.time() - start < timeout:
            found = self.js(f"!!document.querySelector('{selector}')")
            if found:
                return f"Found: {selector}"
            time.sleep(0.5)
        return f"Timeout waiting for: {selector}"

    def new_tab(self, url="about:blank"):
        """Open a new tab and navigate to URL."""
        self._send("Target.createTarget", url=url)
        return f"Opened new tab: {url}"

    def close(self):
        """Close the CDP connection."""
        if self.ws:
            self.ws.close()


# Singleton connection (reused across tool calls)
_browser = None


def get_browser():
    """Get or create a browser connection."""
    global _browser
    if _browser is None:
        _browser = BraveCDP()
    return _browser


def close_browser():
    """Close the browser connection."""
    global _browser
    if _browser:
        _browser.close()
        _browser = None
