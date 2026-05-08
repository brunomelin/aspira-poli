# aspira-ultimate

Scraper da Meta Ad Library + dashboard local + push pra creative-machine.

Roda na sua máquina (Playwright + IP residencial real). Não usa proxies, não
precisa de login. O resultado pode ser visualizado num dashboard HTML local
(100% client-side, sem servidor) e/ou enviado pra plataforma central
(creative-machine) via token único.

## Setup (uma vez)

1. **Python 3.11+** instalado.

2. **Clone e instala deps:**
   ```bash
   git clone <url-do-repo> aspira-ultimate
   cd aspira-ultimate
   pip install -r requirements.txt
   playwright install chromium
   ```

3. **Configura o `.env`:**
   ```bash
   cp .env.example .env
   # edita .env com seus valores
   ```
   Campos:
   - `CM_VERCEL_URL` — URL da creative-machine
   - `CM_TOKEN` — token único que o admin te enviou (formato `cmtok_...`)

4. **Cria sua lista de domínios:**
   ```bash
   cp dominios.txt.example dominios.txt
   # edita dominios.txt — um domínio por linha
   ```

   **Formatos aceitos por linha** (pode misturar):
   - **Domínio puro** (ex: `yougolong.com`) — aspira monta URL de busca por
     palavra-chave (`?q=yougolong.com&search_type=keyword_unordered`)
   - **URL completa da Ad Library** começando com
     `https://www.facebook.com/ads/library/...` — aspira usa a URL como está.
     Útil pra busca por page_id (`?view_all_page_id=...&search_type=page`),
     advertiser_id, ou qualquer outra variante de filtro.

## Uso diário

```bash
# 1. Roda o scrape
python scraper.py -f dominios.txt
# (ou pra um único domínio: python scraper.py -d exemplo.com)

# 2a. Visualiza no dashboard local (opcional)
open dashboard.html
# Clica em "Carregar JSON" e seleciona output/resultado_final.json

# 2b. Envia pra creative-machine
python push_to_app.py
```

Output esperado do `push_to_app.py`:
```
📦 Transform: 145 competitors, 1717/1989 ads (descartados sem video)
🚀 POST .../api/competitors/import (direct mode)

=== RESULTADO ===
  Token: seu-nome
  Competitors processados: 145
  Dispatched OK: 145
  Dispatched FAIL: 0
  Total ads enviados ao worker: 1717
```

Depois disso a plataforma baixa os mp4 + transcreve com Whisper. ~10-15 min e
os ads aparecem em `creative-machine-three.vercel.app/competitors`.

## O que o scraper extrai

Pra cada domínio na lista, busca na Meta Ad Library com:
```
active_status=active, country=ALL, search_type=keyword_unordered,
sort_data=total_impressions_desc
```

Pra cada anúncio: `library_id`, `anunciante`, `texto_anuncio`,
`url_destino_facebook`, `video_url` (SD), `video_hd_url` (capturado do
network interception). Anúncios sem video são descartados pela
creative-machine — só video entra no pipeline de transcrição.

## Dashboard local

`dashboard.html` é standalone — abre direto no browser, sem servidor. Lê o
JSON do `output/resultado_final.json` que você gerou. Tem 5 abas: Overview,
Mídia, Conteúdos, Criativos, Busca Global. **Não compartilha nenhum dado
com a creative-machine** — é totalmente offline.

## Privacy

- Seu `dominios.txt` fica local (gitignored).
- O `output/` dos scrapes fica local (gitignored).
- O `.env` com seu `CM_TOKEN` fica local (gitignored).
- Nenhum dado compartilhado a não ser quando você explicitamente roda
  `python push_to_app.py` (que envia o resultado pra creative-machine via
  seu token).

## Revogação de acesso

Se precisar trocar/revogar seu `CM_TOKEN`, peça pro admin:
- Lista tokens: `GET /api/admin/api-tokens`
- Revoga: `DELETE /api/admin/api-tokens/<id>`
