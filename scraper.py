#!/usr/bin/env python3
"""
Facebook Ad Library Scraper — versão otimizada com processamento paralelo.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from lib.ad_library import scrape_domain
from lib.browser import apply_stealth, create_browser_context
from lib.url_checker import check_and_enrich_ads, summarize_urls
from lib.utils import load_partial, save_partial, setup_logging

logger = setup_logging()

PARTIAL_FILENAME = "parcial.json"
# Reduzido de 5 → 3: cada worker tem mais bandwidth, scroll da Meta renderiza
# mais rápido, e _scroll_all_cards desiste menos cedo. Em paralelismo=5 com
# rede compartilhada, páginas grandes (catchy.life 560 ads) perdiam ~95% dos
# ads porque scroll-stop disparava antes da Meta carregar próxima leva.
DEFAULT_WORKERS = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scraper da Biblioteca de Anúncios do Facebook",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python scraper.py -d informacaoagora.com
  python scraper.py -f dominios.txt --resume
  python scraper.py -f dominios.txt -w 8
        """,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-d", "--domain", nargs="+", help="Um ou mais domínios para buscar")
    group.add_argument("-f", "--file", type=str, help="Arquivo .txt com um domínio por linha")
    parser.add_argument("-o", "--output", type=str, default="output", help="Diretório de saída (padrão: output/)")
    parser.add_argument("-w", "--workers", type=int, default=DEFAULT_WORKERS, help=f"Workers paralelos (padrão: {DEFAULT_WORKERS})")
    parser.add_argument("--headed", action="store_true", default=False, help="Rodar com interface gráfica (padrão: headless)")
    parser.add_argument("--resume", action="store_true", default=False, help="Retomar de onde parou")
    return parser.parse_args()


def load_domains(args: argparse.Namespace) -> list:
    if args.domain:
        return [d.strip().lower() for d in args.domain if d.strip()]
    file_path = Path(args.file)
    if not file_path.exists():
        logger.error("Arquivo não encontrado: %s", file_path)
        sys.exit(1)
    with open(file_path, "r", encoding="utf-8") as f:
        domains = [line.strip().lower() for line in f if line.strip() and not line.startswith("#")]
    if not domains:
        logger.error("Nenhum domínio encontrado no arquivo %s", file_path)
        sys.exit(1)
    return domains


async def _worker(
    worker_id: int,
    queue: asyncio.Queue,
    browser,
    processed: dict,
    domains: list,
    partial_path: Path,
    lock: asyncio.Lock,
    headless: bool,
):
    ctx_kwargs: dict = {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1920, "height": 1080},
        "locale": "pt-BR",
        "timezone_id": "America/Sao_Paulo",
    }
    # Carrega FB session (gerada por scripts/login.py) pra acessar ads age-restricted
    state_path = Path("storage_state.json")
    if state_path.exists():
        ctx_kwargs["storage_state"] = str(state_path)
        logger.info("[W%d] storage_state.json carregado — login persistente ativo", worker_id)
    else:
        logger.info("[W%d] sem storage_state.json — ads 18+ ficam gated", worker_id)
    context = await browser.new_context(**ctx_kwargs)
    page = await context.new_page()
    await apply_stealth(page)

    while True:
        try:
            idx, domain = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        logger.info("[W%d] (%d/%d) Iniciando: %s", worker_id, idx + 1, len(domains), domain)

        try:
            result = await scrape_domain(page, domain)
            result["anuncios"] = await check_and_enrich_ads(result.get("anuncios", []))
            url_summary = summarize_urls(result.get("anuncios", []))
            result.update(url_summary)

            async with lock:
                processed[domain] = result
                _save_partial_progress(partial_path, processed, domains)

            logger.info(
                "[W%d] (%d/%d) OK: %s — %d anúncios, %d anunciantes, %d on / %d off",
                worker_id, idx + 1, len(domains), domain,
                result.get("total_anuncios", 0),
                len(result.get("anunciantes", [])),
                url_summary.get("total_urls_online", 0),
                url_summary.get("total_urls_offline", 0),
            )
        except Exception as e:
            logger.error("[W%d] (%d/%d) ERRO: %s — %s", worker_id, idx + 1, len(domains), domain, e)
            async with lock:
                processed[domain] = {
                    "dominio": domain,
                    "erro": str(e),
                    "total_anuncios": 0,
                    "anunciantes": [],
                    "anuncios": [],
                    "urls_unicas": [],
                    "total_urls_online": 0,
                    "total_urls_offline": 0,
                }
                _save_partial_progress(partial_path, processed, domains)

        queue.task_done()

    await page.close()
    await context.close()


async def main() -> None:
    args = parse_args()
    domains = load_domains(args)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    partial_path = output_dir / PARTIAL_FILENAME
    headless = not args.headed

    processed = {}
    if args.resume:
        existing = load_partial(partial_path)
        if existing and "resultados" in existing:
            for r in existing["resultados"]:
                processed[r["dominio"]] = r
            logger.info("Resume: %d domínios já processados", len(processed))

    remaining = [(i, d) for i, d in enumerate(domains) if d not in processed]
    logger.info(
        "Total: %d | Processados: %d | Restantes: %d | Workers: %d | Headless: %s",
        len(domains), len(processed), len(remaining), args.workers, headless,
    )

    if not remaining:
        logger.info("Todos os domínios já foram processados!")
        _save_final(output_dir, partial_path, processed, domains)
        return

    queue: asyncio.Queue = asyncio.Queue()
    for item in remaining:
        queue.put_nowait(item)

    lock = asyncio.Lock()
    start = asyncio.get_event_loop().time()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-translate",
                "--metrics-recording-only",
                "--no-first-run",
            ],
        )
        try:
            num_workers = min(args.workers, len(remaining))
            workers = [
                asyncio.create_task(
                    _worker(i, queue, browser, processed, domains, partial_path, lock, headless)
                )
                for i in range(num_workers)
            ]
            await asyncio.gather(*workers)
        finally:
            await browser.close()

    elapsed = asyncio.get_event_loop().time() - start
    logger.info("Tempo total: %.1f segundos (%.1f min)", elapsed, elapsed / 60)
    _save_final(output_dir, partial_path, processed, domains)


def _save_partial_progress(partial_path: Path, processed: dict, domains: list) -> None:
    data = {
        "data_execucao": datetime.now().isoformat(timespec="seconds"),
        "total_dominios": len(domains),
        "dominios_processados": len(processed),
        "resultados": [processed[d] for d in domains if d in processed],
    }
    save_partial(data, partial_path)


def _save_final(output_dir: Path, partial_path: Path, processed: dict, domains: list) -> None:
    final_data = {
        "data_execucao": datetime.now().isoformat(timespec="seconds"),
        "total_dominios": len(domains),
        "dominios_processados": len(processed),
        "resultados": [processed[d] for d in domains if d in processed],
    }
    final_path = output_dir / "resultado_final.json"
    with open(final_path, "w", encoding="utf-8") as f:
        json.dump(final_data, f, ensure_ascii=False, indent=2)
    logger.info("Resultado salvo em: %s", final_path)
    logger.info("Domínios processados: %d/%d", len(processed), len(domains))


if __name__ == "__main__":
    asyncio.run(main())
