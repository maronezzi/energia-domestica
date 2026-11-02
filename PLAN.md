# PLAN — PWA instalável sobre o dashboard existente (rev. 2 pós-crítica)

**Meta:** transformar o dashboard web em PWA instalável servida pelo FastAPI na cubie2 — manifest + service worker (cache do shell) + layout mobile-first com barra de abas inferior — sem backend novo além de rotas estáticas e sem mudar comportamento de produção.

**Restrições:**
- Só APIs locais da placa; o app nunca fala com a nuvem BYD direto (1 sessão/conta).
- NUNCA rodar `src/dashboard.py` completo no PC (logaria no BYD e derrubaria a sessão da placa) — verificação visual via stub uvicorn em `/tmp` com JSON sintético.
- Convenções do QWEN.md: UI em pt-BR, commits em inglês (`feat:`), sem deps novas no backend. PIL = dependência só de dev no PC (ícones PNG commitados; a placa nunca importa PIL).

**Decisões da crítica (subagente):**
- Precache do SW **não-atômico** (`Promise.allSettled` com `cache.add()` individual): um CDN fora do ar não pode falhar o install (cache.addAll é atômico).
- Fetch handler: **não-GET nunca interceptado** (POSTs start/stop-charge, config, eventos); `/api/*` sempre rede; tiles OSM fora do SW; navegação **network-first** (deploy atualiza o shell sem bump manual de versão).
- Markup das abas **intacto** — CSS-only transforma `.tabs` em barra inferior; `showTab` (index.html:994 usa `.tab[onclick*=...]`) só ganha scrollTo.
- Ícones: só 2 PNGs (512 "any" + apple-touch 180) — maskable cortado.
- `FileResponse` precisa de import novo (HTTPException já existe, dashboard.py:23).
- rsync copia `src/icons/` (nada nos excludes pega) e `.gitignore` não engole os PNGs — verificado pelo crítico.
- sw.js mantido (inerte sobre HTTP LAN hoje; vivo no dia em que HTTPS for ligado). `tailscale serve` fica documentado no README como caminho para ativá-lo — NÃO executado agora.

## Passos

1. **Camada estática PWA (rotas + arquivos + testes)** — no dashboard.py: `GET /manifest.json`, `GET /sw.js`, `GET /icons/{name}` (whitelist {icon-512.png, apple-touch-icon.png}, 404 fora dela) com `FileResponse` (import novo). Arquivos: `src/manifest.json` (standalone, theme `#0a0e14`, start_url `/`, ícones 512+180), `src/sw.js` (v1: precache não-atômico do shell + CDN Chart.js/Leaflet; CDNs cache-first; `/api/*` e não-GET passthrough; OSM fora; navegação network-first; activate limpa caches antigos), `src/icons/` gerados com PIL (gradiente teal→azul, raio branco; apple-touch 180 quadrado full-bleed — iOS arredonda sozinho). *Merge dos passos 1+2 da rev.1: rotas sem arquivos falham no teste (FileResponse lança com path inexistente) — um deliverable, um check.*
   - Check: `pytest tests/test_pwa.py` verde (rotas chamadas direto, padrão do repo; manifest JSON válido apontando p/ arquivos existentes; sw.js intercepta só GET; PNGs com magic bytes). ✅ **7 passed; ícones gerados e inspecionados** (2026-11-02)

2. **Head do index.html + registro do SW** — link manifest, `theme-color`, `viewport-fit=cover`, metas iOS (`apple-mobile-web-app-capable`, `black-translucent`, título, apple-touch-icon); registro do SW guardado por `'serviceWorker' in navigator && window.isSecureContext` no load.
   - Check: stub `/tmp` + browser (control-browser) em `http://localhost:8052` (8051 ocupado por stub da sessão anterior): SW **ativo** no scope `/`, manifest + metas iOS na DOM, cache `energia-shell-v1` com os 7 assets do shell (CDNs inclusos), página controlada pelo SW, **0 erros** em 2,5s de polls. (2026-11-02)

3. **CSS/JS mobile-first** — ≤600px: `.tabs` vira barra fixa inferior (flex, safe-area-inset-bottom, fundo com blur), `main` ganha padding-bottom, header compacto (esconde `.last-update`); desktop intocado. `showTab()` rola ao topo.
   - Check: screenshots no browser em 390px (barra inferior visível e funcional) e 1280px (idêntico ao antes). ✅ **390px: barra fixed com "Cargas" (rótulo curto), cards EV empilhados (bug de overflow do .ev-cards-row corrigido), toque real na aba Carro funciona (via coordenada — actionability do Playwright trava em fixed, hit-test provado), scroll ao topo; 1280px: layout original idêntico, abas estáticas no topo** (2026-11-02)

4. **Suite + Lighthouse** — `pytest tests/` 100%; `npx lighthouse --only-audits=installable-manifest,service-worker` contra o stub localhost (Chrome+npx já verificados no PC; fallback: checklist manual dos critérios de install via browser).
   - Check: suite verde + evidência do Lighthouse (ou checklist manual documentada). ✅ **195 passed. Lighthouse 12 REMOVEU os audits de PWA (`--list-audits` sem nenhum match; o JSON saiu com 0 audits) — fallback do plano executado e mais forte: morte do stub + reload = página abre pelo cache do SW com Chart.js e Leaflet carregados do cache do CDN, badge "Offline" no header** (2026-11-02)

5. **Docs** — README ganha seção "📱 App (PWA)": iOS (Compartilhar → Adicionar à Tela de Início, standalone total já em HTTP), Android (menu → Adicionar à tela inicial; standalone completo + SW exigem HTTPS → `tailscale serve` na placa, um comando, caminho futuro), CHANGELOG com data.
   - Check: leitura das duas docs — passos claros e caveat HTTPS explícito.

6. **Commit + push** — um commit `feat: installable PWA (manifest, service worker, mobile bottom nav)`.
   - Check: `git log -1` + push sem erro.

7. **Deploy + verificação na placa** — `./deploy_cubie.sh` (guarda de carga embutida); na placa: `curl` de `/manifest.json`, `/sw.js` e ícones = 200; browser apontando para a placa carrega o dashboard e a barra inferior funciona.
   - Check: evidências curl (status + content-type) + screenshot ao vivo da placa.
