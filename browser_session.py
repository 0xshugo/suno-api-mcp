"""Browser session manager for hCaptcha token acquisition.

Uses rebrowser-playwright (Playwright drop-in with bot-detection patches)
to maintain a persistent Chromium session on suno.com/create, enabling
invisible hCaptcha token retrieval for the v2-web generation endpoint.
"""

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger("aoi-suno-mcp.browser")

# Cookie value (same as SUNO_REFRESH_TOKEN in server.py)
SUNO_REFRESH_TOKEN = os.environ.get("SUNO_REFRESH_TOKEN", "")


class BrowserSession:
    """Persistent Chromium session for hCaptcha token acquisition."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._last_nav_time: float = 0.0
        self._nav_count: int = 0

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
        await self._context.add_cookies(
            [
                {**cookie_base, "domain": ".suno.com"},
                {**cookie_base, "domain": ".clerk.suno.com"},
            ]
        )

        self._page = await self._context.new_page()

        # Auto-recover on crash
        self._page.on("crash", self._on_page_crash)

        # Track navigations to detect SPA redirects
        self._nav_count = 0
        self._page.on("framenavigated", self._on_frame_navigated)

        # Navigate to create page (loads hCaptcha JS)
        # Use domcontentloaded — networkidle hangs on SPAs with persistent connections
        logger.info("Navigating to suno.com/create...")
        await self._page.goto(
            "https://suno.com/create",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        # Wait for SPA client-side routing to settle (monitors frame navigations)
        await self._wait_for_page_stable()

        # SPA may redirect /create → /home (Clerk auth flow).
        # If not on /create, navigate there explicitly.
        current_url = self._page.url
        if "/create" not in current_url:
            logger.info(
                "Redirected to %s instead of /create. Re-navigating...", current_url
            )
            await self._page.goto(
                "https://suno.com/create",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await self._wait_for_page_stable()

        # Wait for hCaptcha JS to load (after page is stable)
        await self._wait_for_hcaptcha()

        self._initialized = True
        self._last_nav_time = time.monotonic()
        final_url = self._page.url
        logger.info("Browser session initialized. Final URL: %s", final_url)

    async def _wait_for_page_stable(self, settle_time: float = 3.0, max_wait: float = 30.0) -> None:
        """Wait for SPA client-side navigations to settle.

        Monitors navigation count and waits until no new navigations occur
        for ``settle_time`` seconds.
        """
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            nav_before = self._nav_count
            await asyncio.sleep(settle_time)
            if self._nav_count == nav_before:
                logger.info(
                    "Page stable after %d navigations. URL: %s",
                    self._nav_count,
                    self._page.url,
                )
                return
            logger.info(
                "Page still navigating (count=%d), waiting...", self._nav_count
            )
        logger.warning("Page did not stabilize within %.0fs", max_wait)

    async def _wait_for_hcaptcha(self, timeout: float = 30.0) -> None:
        """Wait until hcaptcha.execute is available on the page."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                has_hcaptcha = await self._page.evaluate(
                    "typeof window.hcaptcha !== 'undefined' && typeof window.hcaptcha.execute === 'function'"
                )
                if has_hcaptcha:
                    logger.info("hCaptcha JS loaded and ready.")
                    return
            except Exception as e:
                logger.debug("hCaptcha check failed (page may be navigating): %s", e)
            await asyncio.sleep(1.0)
        logger.warning("hCaptcha JS not detected after %.0fs — will attempt anyway.", timeout)

    def _on_frame_navigated(self, frame: Any) -> None:
        """Track main-frame navigations for stability detection."""
        if frame == self._page.main_frame:
            self._nav_count += 1
            logger.info("Frame navigated (#%d): %s", self._nav_count, frame.url)

    def _on_page_crash(self, _page: Any) -> None:
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
                await self._wait_for_page_stable()
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

            # Verify we're on /create — SPA may have navigated away
            current_url = self._page.url
            if "/create" not in current_url:
                logger.info("Page drifted to %s, re-navigating to /create", current_url)
                await self._page.goto(
                    "https://suno.com/create",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                await self._wait_for_page_stable()

            # Re-verify hCaptcha is still available
            await self._wait_for_hcaptcha(timeout=15.0)

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
                    return str(token)

                logger.warning("hCaptcha returned invalid token: %s", repr(token)[:100])
                return None

            except Exception as e:
                logger.error("hCaptcha token acquisition failed: %s", e)
                return None

    async def generate_via_browser(
        self, payload: dict[str, Any], bearer_token: str
    ) -> dict[str, Any] | None:
        """Fallback: execute generation request via browser's fetch().

        This bypasses the need for a separate hCaptcha token since the
        browser session already has the captcha context.

        Returns the parsed JSON response or None on failure.
        """
        async with self._lock:
            await self._ensure_session()

            # Ensure page is stable before executing
            try:
                await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception as e:
                logger.warning("wait_for_load_state failed before browser fetch: %s", e)

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
                    return dict(result)

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
