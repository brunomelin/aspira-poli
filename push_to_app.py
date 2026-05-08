#!/usr/bin/env python3
"""
Pega o output/resultado_final.json gerado pelo `python scraper.py`,
transforma pro schema do creative-machine e envia pro nosso endpoint.

DOIS MODOS de envio (auto-selecionados pela presença das envs):

  Modo direto (recomendado, multi-user):
    .env:
      CM_VERCEL_URL=https://creative-machine-three.vercel.app
      CM_TOKEN=cmtok_...      # gerado pelo admin via POST /api/admin/api-tokens

  Modo gist (legado, single-user):
    .env:
      CM_VERCEL_URL=https://...
      CM_BEARER=<CRON_SECRET>   # mesmo do .env.local da creative-machine
      GITHUB_PAT=ghp_...
      GIST_ID=96c9c8889c2...

Se CM_TOKEN estiver setado, usa modo direto. Senão, tenta modo gist.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import httpx
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)

OUTPUT_FILE = Path(__file__).parent / "output" / "resultado_final.json"
GIST_FILENAME = "creative-machine-import.json"

CM_VERCEL_URL = os.environ.get("CM_VERCEL_URL", "").rstrip("/")
CM_TOKEN = os.environ.get("CM_TOKEN")  # modo direto
CM_BEARER = os.environ.get("CM_BEARER")  # modo gist
GITHUB_PAT = os.environ.get("GITHUB_PAT")
GIST_ID = os.environ.get("GIST_ID")


def build_ads_library_url(domain: str) -> str:
    """Mesma URL que scraper.py usa pra buscar — paridade com country=ALL +
    keyword_unordered + sort_data."""
    return (
        "https://www.facebook.com/ads/library/"
        "?active_status=active&ad_type=all&country=ALL"
        "&is_targeted_country=false&media_type=all"
        f"&q={quote_plus(domain)}&search_type=keyword_unordered"
        "&sort_data[direction]=desc&sort_data[mode]=total_impressions"
    )


def transform_ad(asp: dict) -> dict | None:
    """Aspira ad → schema creative-machine. Retorna None se ad não tem
    library_id ou video_url usável."""
    ad_id = str(asp.get("id_anuncio") or "").strip()
    if not ad_id:
        return None

    hd_url = (asp.get("video_hd_url") or "").strip()
    sd_url = (asp.get("video_url") or "").strip()

    has_hd = hd_url.startswith("http") and ".mp4" in hd_url
    has_sd = sd_url.startswith("http") and ".mp4" in sd_url

    if has_hd:
        video_url, video_quality = hd_url, "hd"
    elif has_sd:
        video_url, video_quality = sd_url, "sd"
    else:
        return None

    return {
        "library_id": ad_id,
        "advertiser_name": (asp.get("anunciante") or "").strip() or None,
        "advertiser_page": None,
        "start_date": None,
        "is_active": True,
        "video_url": video_url,
        "video_quality": video_quality,
    }


def transform_competitor(asp: dict) -> dict:
    domain = (asp.get("dominio") or "").strip()
    raw_ads = asp.get("anuncios") or []
    transformed = [t for t in (transform_ad(a) for a in raw_ads) if t is not None]
    return {
        "name": domain,
        "ads_library_url": build_ads_library_url(domain),
        "scraped_ads": transformed,
        "_meta": {
            "total_ads_aspira": len(raw_ads),
            "ads_with_video": len(transformed),
            "advertisers": asp.get("anunciantes") or [],
        },
    }


def _chunk_competitors_by_size(competitors: list, max_size_mb: float = 3.5) -> list:
    """Divide a lista de competitors em chunks que caibam no limite de 4.5MB
    do Vercel serverless function body. Calculamos size estimado por competitor
    (varia muito: 10 ads ~5KB, 380 ads ~700KB) e quebra antes de estourar.
    Margem de 1MB pra header + JSON wrapper."""
    chunks = []
    current = []
    current_size = 200  # base pra wrapper {generated_at, competitors: [...]}
    threshold = int(max_size_mb * 1024 * 1024)
    for c in competitors:
        c_size = len(json.dumps(c, ensure_ascii=False).encode("utf-8"))
        if current and (current_size + c_size) > threshold:
            chunks.append(current)
            current = []
            current_size = 200
        current.append(c)
        current_size += c_size + 2  # +2 por causa do `, ` entre elementos
    if current:
        chunks.append(current)
    return chunks


def push_direct(payload: dict) -> dict:
    """POST direto pro endpoint multi-user. Divide em chunks se payload >3.5MB
    (Vercel limita body em 4.5MB). Cada chunk é idempotente e acumula resultados."""
    competitors = payload["competitors"]
    chunks = _chunk_competitors_by_size(competitors)

    if len(chunks) == 1:
        print(f"🚀 POST {CM_VERCEL_URL}/api/competitors/import (direct mode)")
    else:
        total_size = sum(len(json.dumps(c, ensure_ascii=False).encode("utf-8")) for c in competitors)
        print(f"🚀 Payload {total_size // 1024}KB > 3.5MB → split em {len(chunks)} chunks")

    aggregated = {
        "generated_at": payload.get("generated_at"),
        "competitors_total": 0,
        "dispatched_ok": 0,
        "dispatched_failed": 0,
        "ads_total": 0,
        "results": [],
        "user_label": None,
    }

    for i, chunk in enumerate(chunks):
        chunk_payload = {
            "generated_at": payload.get("generated_at"),
            "competitors": chunk,
        }
        if len(chunks) > 1:
            print(f"  ↗ chunk {i + 1}/{len(chunks)} — {len(chunk)} competitors...")
        with httpx.Client(timeout=300) as client:
            r = client.post(
                f"{CM_VERCEL_URL}/api/competitors/import",
                headers={"Authorization": f"Bearer {CM_TOKEN}"},
                json=chunk_payload,
            )
            if r.status_code >= 300:
                print(f"❌ Vercel call falhou no chunk {i + 1}: {r.status_code} {r.text[:500]}", file=sys.stderr)
                sys.exit(1)
            data = r.json()
            aggregated["competitors_total"] += data.get("competitors_total", 0)
            aggregated["dispatched_ok"] += data.get("dispatched_ok", 0)
            aggregated["dispatched_failed"] += data.get("dispatched_failed", 0)
            aggregated["ads_total"] += data.get("ads_total", 0)
            aggregated["results"].extend(data.get("results", []))
            aggregated["user_label"] = data.get("user_label") or aggregated["user_label"]

    return aggregated


def push_via_gist(payload: dict) -> dict:
    """Modo legado: PATCH no gist + trigger /api/competitors/import-from-gist."""
    print(f"⬆️  Push pra Gist {GIST_ID}...")
    with httpx.Client(timeout=30) as client:
        r = client.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={
                "Authorization": f"Bearer {GITHUB_PAT}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "files": {
                    GIST_FILENAME: {"content": json.dumps(payload, ensure_ascii=False, indent=2)},
                },
            },
        )
        if r.status_code >= 300:
            print(f"❌ Gist PATCH falhou: {r.status_code} {r.text[:200]}", file=sys.stderr)
            sys.exit(1)
        print(f"✅ Gist atualizado: https://gist.github.com/{r.json().get('owner', {}).get('login', '')}/{GIST_ID}")

    print(f"🚀 Trigger {CM_VERCEL_URL}/api/competitors/import-from-gist (gist mode)")
    with httpx.Client(timeout=300) as client:
        r = client.post(
            f"{CM_VERCEL_URL}/api/competitors/import-from-gist",
            headers={"Authorization": f"Bearer {CM_BEARER}"},
            json={"gist_id": GIST_ID},
        )
        if r.status_code >= 300:
            print(f"❌ Vercel call falhou: {r.status_code} {r.text[:500]}", file=sys.stderr)
            sys.exit(1)
        return r.json()


def main() -> None:
    if not CM_VERCEL_URL:
        print("❌ CM_VERCEL_URL obrigatório no .env", file=sys.stderr)
        sys.exit(1)

    use_direct = bool(CM_TOKEN)
    if not use_direct:
        if not all([GITHUB_PAT, GIST_ID, CM_BEARER]):
            print(
                "❌ Configure CM_TOKEN (modo direto) OU GITHUB_PAT+GIST_ID+CM_BEARER (modo gist legado).\n"
                f"   .env: {ENV_PATH}",
                file=sys.stderr,
            )
            sys.exit(1)

    if not OUTPUT_FILE.exists():
        print(f"❌ {OUTPUT_FILE} não existe — rode primeiro: python scraper.py", file=sys.stderr)
        sys.exit(1)

    with OUTPUT_FILE.open(encoding="utf-8") as f:
        aspira_data = json.load(f)

    competitors = [transform_competitor(c) for c in aspira_data.get("resultados", [])]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "competitors": competitors,
    }

    total_competitors = len(competitors)
    total_ads = sum(len(c["scraped_ads"]) for c in competitors)
    total_ads_raw = sum(c["_meta"]["total_ads_aspira"] for c in competitors)
    print(f"📦 Transform: {total_competitors} competitors, {total_ads}/{total_ads_raw} ads (descartados sem video)")

    result = push_direct(payload) if use_direct else push_via_gist(payload)

    print(f"\n=== RESULTADO ===")
    if result.get("user_label"):
        print(f"  Token: {result['user_label']}")
    print(f"  Competitors processados: {result.get('competitors_total', 0)}")
    print(f"  Dispatched OK: {result.get('dispatched_ok', 0)}")
    print(f"  Dispatched FAIL: {result.get('dispatched_failed', 0)}")
    print(f"  Total ads enviados ao worker: {result.get('ads_total', 0)}")

    failed = [r for r in result.get("results", []) if not r.get("ok")]
    if failed:
        print(f"\n❌ Falhas:")
        for f in failed[:20]:
            print(f"  {f['name']}: {f.get('error', '?')}")


if __name__ == "__main__":
    main()
