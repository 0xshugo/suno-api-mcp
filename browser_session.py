"""Browser session manager for hCaptcha token acquisition.

Uses rebrowser-playwright (Playwright drop-in with bot-detection patches)
to maintain a persistent Chromium session on suno.com/create, enabling
invisible hCaptcha token retrieval for the v2-web generation endpoint.
"""

import asyncio
import logging
import os
import time

logger = logging.getLogger("aoi-suno-mcp.browser")

# Cookie value (same as SUNO_REFRESH_TOKEN in server.py)
SUNO_REFRESH_TOKEN = os.environ.get("SUNO_REFRESH_TOKEN", "")


class BrowserSession:
    """Persistent Chromium session for hCaptcha token acquisition."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._last_nav_time: float = 0.0

    async def initialize(self) -> None:
        """Launch Chromium, inject cookies, navigate to suno.com/create."""
        if self._initialized:
            return

        from rebrowser_playwright.async_api import async_playwright

        logger.info("Launching Chromium browser session...")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--single-process",
            ],
        )

        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )

        # Inject __client cookie to both domains
        client_jwt = SUNO_REFRESH_TOKEN
        if not client_jwt:
            raise RuntimeError("SUNO_REFRESH_TOKEN is not set; cannot inject cookie.")

        cookie_base = {
            "name": "__client",
            "value": client_jwt,
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        }
        await self._context.add_cookies([
            {**cookie_base, "domain": ".suno.com"},
            {**cookie_base, "domain": ".clerk.suno.com"},
        ])

        self._page = await self._context.new_page()

        # Auto-recover on crash
        self._page.on("crash", self._on_page_crash)

        # Navigate to create page (loads hCaptcha JS)
        logger.info("Navigating to suno.com/create...")
        await self._page.goto(
            "https://suno.com/create",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        # Wait for hCaptcha JS to load
        await self._wait_for_hcaptcha()

        self._initialized = True
        self._last_nav_time = time.monotonic()
        logger.info("Browser session initialized successfully.")

    async def _wait_for_hcaptcha(self, timeout: float = 30.0) -> None:
        """Wait until hcaptcha.execute is available on the page."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            has_hcaptcha = await self._page.evaluate(
                "typeof window.hcaptcha !== 'undefined' && typeof window.hcaptcha.execute === 'function'"
            )
            if has_hcaptcha:
                logger.info("hCaptcha JS loaded and ready.")
                return
            await asyncio.sleep(1.0)
        logger.warning("hCaptcha JS not detected after %.0fs — will attempt anyway.", timeout)

    def _on_page_crash(self, _page) -> None:
        logger.error("Page crashed! Will re-initialize on next request.")
        self._initialized = False

    async def _ensure_session(self) -> None:
        """Check session health, re-initialize if needed."""
        if not self._initialized:
            await self.close()
            await self.initialize()
            return

        # Refresh page every 30 minutes to keep session/cookies alive
        if time.monotonic() - self._last_nav_time > 1800:
            logger.info("Session refresh: reloading create page...")
            try:
                await self._page.reload(wait_until="domcontentloaded", timeout=60000)
                await self._wait_for_hcaptcha()
                self._last_nav_time = time.monotonic()
            except Exception as e:
                logger.warning("Session refresh failed, re-initializing: %s", e)
                self._initialized = False
                await self.close()
                await self.initialize()

    async def get_hcaptcha_token(self) -> str | None:
        """Execute invisible hCaptcha and return the token string.

        Returns None if hCaptcha is not available or fails.
        """
        async with self._lock:
            await self._ensure_session()

            try:
                token = await self._page.evaluate("""
                    async () => {
                        if (typeof window.hcaptcha === 'undefined') return null;
                        try {
                            const resp = await hcaptcha.execute({async: true});
                            return resp?.response || resp || null;
                        } catch (e) {
                            console.error('hcaptcha.execute failed:', e);
                            return null;
                        }
                    }
                """)

                if token and isinstance(token, str) and len(token) > 20:
                    logger.info("hCaptcha token acquired (len=%d)", len(token))
                    return token

                logger.warning("hCaptcha returned invalid token: %s", repr(token)[:100])
                return None

            except Exception as e:
                logger.error("hCaptcha token acquisition failed: %s", e)
                return None

    async def generate_via_browser(
        self, payload: dict, bearer_token: str
    ) -> dict | None:
        """Fallback: execute generation request via browser's fetch().

        This bypasses the need for a separate hCaptcha token since the
        browser session already has the captcha context.

        Returns the parsed JSON response or None on failure.
        """
        async with self._lock:
            await self._ensure_session()

            try:
                result = await self._page.evaluate(
                    """
                    async ([payload, bearerToken]) => {
                        try {
                            const resp = await fetch(
                                'https://studio-api.prod.suno.com/api/generate/v2-web/',
                                {
                                    method: 'POST',
                                    headers: {
                                        'Content-Type': 'application/json',
                                        'Authorization': 'Bearer ' + bearerToken,
                                    },
                                    body: JSON.stringify(payload),
                                    credentials: 'include',
                                }
                            );
                            if (!resp.ok) {
                                return {error: true, status: resp.status, text: await resp.text()};
                            }
                            return await resp.json();
                        } catch (e) {
                            return {error: true, message: e.toString()};
                        }
                    }
                    """,
                    [payload, bearer_token],
                )

                if result and not result.get("error"):
                    logger.info("Browser-based generation succeeded.")
                    return result

                logger.error("Browser-based generation failed: %s", result)
                return None

            except Exception as e:
                logger.error("Browser fetch failed: %s", e)
                return None

    async def close(self) -> None:
        """Release browser resources."""
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._initialized = False
        logger.info("Browser session closed.")
