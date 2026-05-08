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
    # locale/timezone US-East: Meta usa esses sinais (junto com Accept-Language
    # derivado do locale) pra decidir o country do redirect. Com pt-BR/Sao_Paulo,
    # Meta força ?country=BR mesmo quando a URL pede ALL — o que zera ads de
    # marcas não-brasileiras (Hers, etc). Setando en-US/New_York, o redirect
    # respeita ?country=ALL e servimos ads globais.
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
    )
    return context


async def apply_stealth(page: Page) -> None:
    await _stealth.apply_stealth_async(page)
