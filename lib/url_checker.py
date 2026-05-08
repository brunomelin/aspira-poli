from __future__ import annotations

import asyncio
import re

import httpx

from lib.utils import parse_facebook_redirect, setup_logging

logger = setup_logging()

MAX_CONCURRENT = 20
TIMEOUT_SECONDS = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

MAX_BODY_BYTES = 50_000


async def check_urls(urls: list) -> dict:
    if not urls:
        return {}

    unique = list(set(urls))
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(TIMEOUT_SECONDS),
        verify=False,
    ) as client:
        tasks = [_check_single(client, semaphore, url) for url in unique]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    url_map = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        url_map[r["url"]] = r
    return url_map


async def _check_single(client: httpx.AsyncClient, semaphore: asyncio.Semaphore, url: str) -> dict:
    real_url = parse_facebook_redirect(url)
    base = {"url": real_url, "url_final": "", "status_code": 0, "online": False, "page_title": "", "page_description": ""}
    async with semaphore:
        try:
            async with client.stream("GET", real_url) as response:
                status = response.status_code
                final_url = str(response.url)
                online = 200 <= status < 400

                title = ""
                description = ""
                if online:
                    chunks = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= MAX_BODY_BYTES:
                            break
                    partial_html = b"".join(chunks).decode("utf-8", errors="ignore")
                    title, description = _extract_page_meta(partial_html)

                return {
                    "url": real_url,
                    "url_final": final_url,
                    "status_code": status,
                    "online": online,
                    "page_title": title,
                    "page_description": description,
                }
        except httpx.TimeoutException:
            return {**base, "erro": "timeout"}
        except httpx.ConnectError:
            return {**base, "erro": "connection_error"}
        except Exception as e:
            return {**base, "erro": str(e)}


def _extract_page_meta(html: str) -> tuple:
    title = ""
    description = ""

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = re.sub(r"\s+", " ", title_match.group(1)).strip()

    desc_match = re.search(
        r'<meta\s+[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']',
        html, re.IGNORECASE | re.DOTALL,
    )
    if not desc_match:
        desc_match = re.search(
            r'<meta\s+[^>]*content=["\'](.*?)["\'][^>]*name=["\']description["\']',
            html, re.IGNORECASE | re.DOTALL,
        )
    if desc_match:
        description = re.sub(r"\s+", " ", desc_match.group(1)).strip()

    return title, description


async def check_and_enrich_ads(ads: list) -> list:
    all_urls = [ad.get("url_destino_real", "") for ad in ads if ad.get("url_destino_real")]
    if not all_urls:
        return ads

    unique_urls = list(set(all_urls))
    logger.info("Verificando %d URLs únicas...", len(unique_urls))
    url_map = await check_urls(unique_urls)

    enriched = []
    for ad in ads:
        ad_copy = dict(ad)
        url = ad.get("url_destino_real", "")
        if url and url in url_map:
            info = url_map[url]
            ad_copy["url_status"] = info.get("status_code", 0)
            ad_copy["url_online"] = info.get("online", False)
            ad_copy["url_final"] = info.get("url_final", "")
            ad_copy["page_title"] = info.get("page_title", "")
            ad_copy["page_description"] = info.get("page_description", "")
        else:
            ad_copy["url_status"] = 0
            ad_copy["url_online"] = False
            ad_copy["url_final"] = ""
            ad_copy["page_title"] = ""
            ad_copy["page_description"] = ""
        enriched.append(ad_copy)
    return enriched


def summarize_urls(ads: list) -> dict:
    url_map = {}
    for ad in ads:
        url = ad.get("url_destino_real", "")
        if url and url not in url_map:
            url_map[url] = {
                "url": url,
                "url_final": ad.get("url_final", ""),
                "status": ad.get("url_status", 0),
                "online": ad.get("url_online", False),
                "page_title": ad.get("page_title", ""),
                "page_description": ad.get("page_description", ""),
            }

    urls_list = list(url_map.values())
    online_count = sum(1 for u in urls_list if u["online"])
    return {
        "urls_unicas": urls_list,
        "total_urls_online": online_count,
        "total_urls_offline": len(urls_list) - online_count,
    }
