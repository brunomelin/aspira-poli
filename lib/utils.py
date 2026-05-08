from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("fb_scraper")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s — %(message)s", datefmt="%H:%M:%S")
    )
    logger.addHandler(handler)
    return logger


async def random_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def scroll_until_end(page, container_selector: str | None = None, max_retries: int = 3):
    """Scroll incrementally inside a container (or the page) until no new content loads."""
    retries_without_change = 0
    previous_height = 0

    while retries_without_change < max_retries:
        if container_selector:
            current_height = await page.evaluate(
                f'document.querySelector("{container_selector}")?.scrollHeight ?? 0'
            )
            await page.evaluate(
                f'document.querySelector("{container_selector}").scrollTop = '
                f'document.querySelector("{container_selector}").scrollHeight'
            )
        else:
            current_height = await page.evaluate("document.body.scrollHeight")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        await asyncio.sleep(1.5)

        if current_height == previous_height:
            retries_without_change += 1
        else:
            retries_without_change = 0

        previous_height = current_height


def parse_facebook_redirect(url: str) -> str:
    """Extract the real destination URL from Facebook's l.facebook.com redirect wrapper."""
    if not url:
        return url

    parsed = urlparse(url)
    if parsed.hostname in ("l.facebook.com", "lm.facebook.com") and parsed.path == "/l.php":
        params = parse_qs(parsed.query)
        if "u" in params:
            return unquote(params["u"][0])

    return url


def save_partial(data: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_partial(path: str | Path) -> dict | None:
    path = Path(path)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None
