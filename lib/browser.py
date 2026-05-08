from __future__ import annotations

from pathlib import Path

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
    ctx_kwargs: dict = {
        "user_agent": USER_AGENT,
        "viewport": {"width": 1920, "height": 1080},
        "locale": "en-US",
        "timezone_id": "America/New_York",
    }

    # Storage state pra Facebook auth (gerado por scripts/login.py).
    # Permite scrapear ads age-restricted que exigem login. Use conta FB
    # SECUNDÁRIA — Meta detecta automation logada e pode suspender.
    state_path = Path("storage_state.json")
    if state_path.exists():
        ctx_kwargs["storage_state"] = str(state_path)
        logger.info("storage_state.json encontrado — usando login persistente")
    else:
        logger.info("sem storage_state.json — rodando deslogado (ads 18+ ficam gated)")

    context = await browser.new_context(**ctx_kwargs)
    return context


async def apply_stealth(page: Page) -> None:
    await _stealth.apply_stealth_async(page)
