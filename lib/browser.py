from __future__ import annotations

from playwright.async_api import BrowserContext, Page, Playwright
from playwright_stealth import Stealth

_stealth = Stealth()

from lib.utils import setup_logging

logger = setup_logging()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def create_browser_context(
    playwright: Playwright,
    headless: bool = True,
) -> BrowserContext:
    browser = await playwright.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
    )
    return context


async def apply_stealth(page: Page) -> None:
    await _stealth.apply_stealth_async(page)
