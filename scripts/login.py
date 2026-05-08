#!/usr/bin/env python3
"""One-time login no Facebook pra salvar cookies/storage_state.

USO:
    python scripts/login.py

Browser abre. Faça login manualmente (incluindo 2FA se tiver).
Após terminar, volta no terminal e aperte ENTER pra salvar o state.

⚠️  Use uma conta FB SECUNDÁRIA descartável. Meta detecta automation
    em conta logada e pode suspender. Nunca use sua conta principal.

Próximas runs do scraper.py vão usar o storage_state.json automaticamente
(via lib/browser.py) — sem precisar logar de novo enquanto cookies valerem.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

STATE_PATH = Path("storage_state.json")


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")

        print()
        print("=" * 60)
        print("Faça login manualmente no browser (e 2FA se tiver).")
        print("Quando estiver logado e vendo o feed do FB, volte aqui.")
        print("=" * 60)
        input("Pressione ENTER pra salvar o storage_state e fechar... ")

        await context.storage_state(path=str(STATE_PATH))
        print(f"✓ Salvo em {STATE_PATH.resolve()}")
        print(f"  Tamanho: {STATE_PATH.stat().st_size} bytes")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
