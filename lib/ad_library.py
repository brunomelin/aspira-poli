from __future__ import annotations

import asyncio
import json
import re
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import Page, Response, TimeoutError as PlaywrightTimeout

from lib.utils import parse_facebook_redirect, setup_logging

logger = setup_logging()

AD_LIBRARY_URL = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country=ALL"
    "&is_targeted_country=false&media_type=all"
    "&q={domain}&search_type=keyword_unordered"
    "&sort_data[direction]=desc&sort_data[mode]=total_impressions"
)


def build_url(domain: str) -> str:
    return AD_LIBRARY_URL.format(domain=quote_plus(domain))


_FULL_URL_PREFIX = "https://www.facebook.com/ads/library/"


def is_full_ad_library_url(s: str) -> bool:
    """Detecta se o input já é uma URL completa da Ad Library (ex.: busca por
    page_id, advertiser_id, etc.). Usado pra permitir input de URLs custom
    sem mexer no fluxo de input por domínio."""
    return s.startswith(_FULL_URL_PREFIX)


class VideoMapCollector:
    """Intercepts network responses to build a map of ad_id → HD/SD video URLs."""

    def __init__(self):
        self.video_map: dict = {}

    async def on_response(self, response: Response) -> None:
        try:
            ct = response.headers.get("content-type", "")
            if "html" not in ct and "json" not in ct and "javascript" not in ct:
                return
            url = response.url
            if not any(k in url for k in ["ads/library", "api/graphql", "graphql"]):
                if "html" not in ct:
                    return
            body = await response.text()
            if "video_hd_url" not in body and "video_sd_url" not in body:
                return
            self._extract_from_text(body)
        except Exception:
            pass

    def extract_from_html(self, html: str) -> None:
        self._extract_from_text(html)

    def _extract_from_text(self, text: str) -> None:
        hd_re = re.compile(r'"video_hd_url"\s*:\s*"(https?:[^"]+)"')
        sd_re = re.compile(r'"video_sd_url"\s*:\s*"(https?:[^"]+)"')
        id_re = re.compile(r'"ad_archive_id"\s*:\s*"(\d+)"')

        all_ids = [(m.start(), m.group(1)) for m in id_re.finditer(text)]
        if not all_ids:
            return

        def find_nearest_id(pos: int) -> Optional[str]:
            best_id = None
            best_dist = float("inf")
            for id_pos, id_val in all_ids:
                if id_pos < pos:
                    dist = pos - id_pos
                    if dist < best_dist and dist < 6000:
                        best_dist = dist
                        best_id = id_val
            return best_id

        for m in hd_re.finditer(text):
            url = m.group(1).replace("\\/", "/")
            if ".mp4" not in url:
                continue
            archive_id = find_nearest_id(m.start())
            if archive_id:
                self.video_map.setdefault(archive_id, {"hd": "", "sd": ""})
                if not self.video_map[archive_id]["hd"]:
                    self.video_map[archive_id]["hd"] = url

        for m in sd_re.finditer(text):
            url = m.group(1).replace("\\/", "/")
            if ".mp4" not in url:
                continue
            archive_id = find_nearest_id(m.start())
            if archive_id:
                self.video_map.setdefault(archive_id, {"hd": "", "sd": ""})
                if not self.video_map[archive_id]["sd"]:
                    self.video_map[archive_id]["sd"] = url


async def scrape_domain(page: Page, domain: str) -> dict:
    # Aceita 2 formatos de input:
    # 1) Domínio puro (ex: "yougolong.com") → constrói URL keyword_unordered
    #    via build_url. Fluxo legado, usado pelo creative-machine.
    # 2) URL completa da Ad Library (ex: ".../?view_all_page_id=...&search_type=page")
    #    → usa direto. Útil pra busca por page_id, advertiser_id, etc.
    # Em ambos os casos, o resto do pipeline (extração de cards, video map, etc.)
    # é idêntico — Meta renderiza os mesmos cards independente do search_type.
    if is_full_ad_library_url(domain):
        url = domain
    else:
        url = build_url(domain)
    logger.info("[%s] Navegando para Ad Library...", domain)

    collector = VideoMapCollector()
    page.on("response", collector.on_response)

    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    total_ads = await _extract_ad_count(page, domain)
    advertisers = await _extract_advertisers(page, domain)

    logger.info("[%s] Carregando cards...", domain)
    await _scroll_all_cards(page, domain)

    html = await page.evaluate("document.documentElement.innerHTML")
    collector.extract_from_html(html)

    page.remove_listener("response", collector.on_response)

    ads_data = await _extract_ads_from_cards(page, domain, collector.video_map)
    logger.info(
        "[%s] Video HD map: %d ads com HD de %d total no mapa",
        domain,
        sum(1 for v in collector.video_map.values() if v["hd"]),
        len(collector.video_map),
    )

    return {
        "dominio": domain,
        "total_anuncios": total_ads,
        "anunciantes": advertisers,
        "anuncios": ads_data,
    }


async def _extract_ad_count(page: Page, domain: str) -> int:
    try:
        count_text = await page.evaluate("""
            () => {
                const body = document.body.innerText;
                const patterns = [
                    /~(\\d[\\d.,]*)\\s*resultado/i,
                    /(?:About|Cerca de|Mais de|Over)\\s+([\\d.,]+)/i,
                    /([\\d.,]+)\\s*(?:results?|resultados?)/i,
                ];
                for (const pat of patterns) {
                    const m = body.match(pat);
                    if (m) return m[1];
                }
                return '';
            }
        """)
        if count_text:
            num_str = count_text.replace(".", "").replace(",", "")
            count = int(num_str)
            logger.info("[%s] Total de anúncios: %d", domain, count)
            return count
        logger.warning("[%s] Não foi possível extrair contagem", domain)
        return 0
    except Exception as e:
        logger.warning("[%s] Erro ao extrair contagem: %s", domain, e)
        return 0


async def _extract_advertisers(page: Page, domain: str) -> list:
    try:
        # Anchored sem IGNORECASE pra não bater em "Limpar filtros" / "Clear filters".
        # A UI nova da Meta tem 2 botões: "Filtros" e "Limpar filtros" — antes do
        # anchor, get_by_role pegava ambos e dava strict mode violation.
        filters_btn = page.get_by_role("button", name=re.compile(r"^(Filtros|Filters)$"))
        await filters_btn.wait_for(state="visible", timeout=8000)
        await filters_btn.click()
        await page.wait_for_timeout(800)

        dropdown = page.get_by_text(re.compile(r"Todos os anunciantes|All advertisers", re.IGNORECASE))
        await dropdown.first.wait_for(state="visible", timeout=8000)
        await dropdown.first.click()
        await page.wait_for_timeout(800)

        listbox = page.locator('[role="listbox"]')
        await listbox.first.wait_for(state="visible", timeout=8000)
        await _scroll_listbox(page, listbox.first)

        options = listbox.first.locator('[role="option"]')
        count = await options.count()

        advertisers = []
        for i in range(count):
            text = (await options.nth(i).inner_text()).strip()
            if text and text not in ("Todos os anunciantes", "All advertisers"):
                advertisers.append(text)

        logger.info("[%s] Anunciantes: %s", domain, advertisers)

        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)

        return advertisers

    except PlaywrightTimeout:
        logger.warning("[%s] Timeout ao extrair anunciantes", domain)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        return []
    except Exception as e:
        logger.warning("[%s] Erro ao extrair anunciantes: %s", domain, e)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return []


async def _scroll_listbox(page: Page, listbox, max_retries: int = 3) -> None:
    retries = 0
    prev_count = 0
    while retries < max_retries:
        options = listbox.locator('[role="option"]')
        current_count = await options.count()
        if current_count == prev_count:
            retries += 1
        else:
            retries = 0
        prev_count = current_count
        try:
            await page.evaluate("(el) => el.scrollTop = el.scrollHeight", await listbox.element_handle())
        except Exception:
            break
        await page.wait_for_timeout(300)


async def _scroll_all_cards(page: Page, domain: str, max_retries: int = 12) -> None:
    """Scroll até `scrollHeight` parar de crescer por max_retries × 1.5s.
    Antes: max_retries=4, sleep=0.6 → tolerava só 2.4s sem novos cards.
    Agora: 12 × 1.5 = 18s, suficiente pra Meta carregar próxima leva mesmo
    com rede lenta ou bandwidth dividida em paralelismo. Páginas grandes
    (catchy.life com 560 ads) precisam disso, senão aspira desiste cedo."""
    retries = 0
    prev_height = 0
    scroll_count = 0

    while retries < max_retries:
        current_height = await page.evaluate("document.body.scrollHeight")
        if current_height == prev_height:
            retries += 1
        else:
            retries = 0
        prev_height = current_height
        scroll_count += 1

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)

        if scroll_count % 10 == 0:
            # Conta cards pelo texto "Identificação da biblioteca: <id>" — funciona
            # em ambas UIs (keyword search e view_all_page_id). Antes contava
            # `l.facebook.com/l.php` que só existe no card do listing keyword,
            # vazio em UI page-mode.
            l_count = await page.evaluate(
                r"""(() => {
                    const re = /(?:da biblioteca|Library ID):\s*\d+/;
                    let n = 0;
                    for (const el of document.querySelectorAll('div, span')) {
                        if (re.test(el.textContent || '')) n++;
                    }
                    return n;
                })()"""
            )
            logger.info("[%s] Scroll %d — %d cards", domain, scroll_count, l_count)

    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.3)


async def _extract_ads_from_cards(page: Page, domain: str, video_map: Optional[dict] = None) -> list:
    video_map_json = json.dumps(video_map or {})
    ads = await page.evaluate(r"""
        (videoMapStr) => {
            const videoMap = JSON.parse(videoMapStr);
            const results = [];
            const processed = new Set();

            // Encontra cards pelo texto "Identificação da biblioteca: <id>" — funciona
            // em ambas UIs (keyword search e view_all_page_id).
            // Antes usava `a[href*="l.facebook.com/l.php"]` que existe só na UI keyword.
            const idRegex = /(?:da biblioteca|Library ID):\s*(\d+)/;
            const cardSet = new Set();
            for (const el of document.querySelectorAll('div, span')) {
                if (!idRegex.test(el.textContent || '')) continue;
                // Sobe até container que tenha "Patrocinado"/"Sponsored" e tamanho razoável.
                let card = el;
                for (let i = 0; i < 20; i++) {
                    if (!card.parentElement) break;
                    card = card.parentElement;
                    const t = card.innerText || '';
                    if ((t.includes('Patrocinado') || t.includes('Sponsored')) && t.length > 100) break;
                }
                cardSet.add(card);
            }

            for (const card of cardSet) {
                // Link de destino externo (presente em UI keyword, opcional em UI page).
                const linkEl = card.querySelector('a[href*="l.facebook.com/l.php"]');
                const href = linkEl ? linkEl.href : '';

                const cardText = card.innerText || '';

                let adId = '';
                const idMatch = cardText.match(/(?:da biblioteca|Library ID):\s*(\d+)/);
                if (idMatch) adId = idMatch[1];

                let advertiser = '';
                const sponsoredMatch = cardText.match(/(?:Abrir menu suspenso|Open dropdown)\s*(?:Ver [\s\S]*?do anúncio|See ad details)\s*([^\n]+?)\s*Patrocinado/);
                if (sponsoredMatch) {
                    advertiser = sponsoredMatch[1].trim();
                } else {
                    const advLinks = card.querySelectorAll('a[href*="facebook.com/"]');
                    for (const al of advLinks) {
                        const aHref = al.href;
                        if (
                            !aHref.includes('/ads/library') &&
                            !aHref.includes('l.facebook.com') &&
                            aHref.match(/facebook\.com\/\d+/) &&
                            al.textContent.trim()
                        ) {
                            advertiser = al.textContent.trim();
                            break;
                        }
                    }
                }

                let adText = '';
                const sponsoredIdx = cardText.indexOf('Patrocinado');
                const sponsoredIdxEn = cardText.indexOf('Sponsored');
                const startIdx = Math.max(sponsoredIdx, sponsoredIdxEn);
                if (startIdx >= 0) {
                    let raw = cardText.substring(startIdx);
                    raw = raw.replace(/^(?:Patrocinado|Sponsored)/, '').trim();
                    const ctaPattern = /^(Saiba mais|Shop now|Learn more|Sign up|Comprar agora|Curtir|Like|Cadastre-se|Inscreva-se|Assista|Watch more|Ver detalhes|INFORMACAOAGORA|SERIEDRAMA|YOUGOLONG|Abrir menu|Identificação)/im;
                    const lines = raw.split('\n');
                    const cleaned = [];
                    for (const line of lines) {
                        const trimmed = line.trim();
                        if (!trimmed) continue;
                        if (ctaPattern.test(trimmed)) break;
                        if (/^[a-z0-9-]+\.[a-z]{2,}$/i.test(trimmed)) break;
                        cleaned.push(trimmed);
                    }
                    adText = cleaned.join(' ').substring(0, 500);
                }

                // --- Phase 3: resolve video URL (prefer HD from JSON, fallback to DOM) ---
                let videoUrl = '';
                let videoHdUrl = '';
                let thumbnailUrl = '';

                const mapped = adId ? videoMap[adId] : null;
                if (mapped) {
                    videoHdUrl = mapped.hd || '';
                    videoUrl = mapped.sd || '';
                }

                // Fallback: read from <video> element if JSON didn't have it
                if (!videoHdUrl && !videoUrl) {
                    const video = card.querySelector('video');
                    if (video) {
                        videoUrl = video.src || '';
                        thumbnailUrl = video.getAttribute('poster') || '';
                        if (!videoUrl) {
                            const source = video.querySelector('source');
                            if (source) videoUrl = source.src || '';
                        }
                    }
                }

                // Thumbnail: use video poster or largest image in card
                if (!thumbnailUrl) {
                    const video = card.querySelector('video');
                    if (video) thumbnailUrl = video.getAttribute('poster') || '';
                }
                if (!thumbnailUrl) {
                    const imgs = card.querySelectorAll('img');
                    for (const img of imgs) {
                        const s = img.src || '';
                        if (!s || s.includes('emoji') || s.includes('data:')) continue;
                        const w = img.naturalWidth || img.width || 0;
                        if (w > 80 || s.includes('scontent')) {
                            thumbnailUrl = s;
                            break;
                        }
                    }
                }

                const key = adId || href || (card.outerHTML || '').slice(0, 200);
                if (processed.has(key)) continue;
                processed.add(key);

                results.push({
                    id_anuncio: adId,
                    anunciante: advertiser,
                    texto_anuncio: adText,
                    url_destino_facebook: href,
                    video_url: videoUrl,
                    video_hd_url: videoHdUrl,
                    thumbnail_url: thumbnailUrl,
                });
            }
            return results;
        }
    """, video_map_json)

    enriched = []
    for ad in ads:
        ad["url_destino_real"] = parse_facebook_redirect(ad.get("url_destino_facebook", ""))
        enriched.append(ad)

    logger.info("[%s] Extraídos %d anúncios", domain, len(enriched))
    return enriched
