# Changelog

## [Unreleased] - 2026-10-07 (2)

### Fixed
- 📐 **Cabeçalho estourava a largura da página: 6º quadro (Custo Hoje) cortado e scroll horizontal** — os tracks `repeat(6,1fr)` têm piso de `min-content` e as linhas novas "casa · carro" são `nowrap`: a soma dos mínimos (~1450px) ultrapassava o container (~1287px em janela 1366) e o grid transbordava 118px além da viewport, com quadros de larguras desiguais (189–251px). Correção em 3 camadas: (1) todos os grids da página (`status-bar`, `ev-cards-row`, `phases-grid`, `gauges`, `charts-grid` + breakpoints 1100/640) usam `minmax(0,1fr)` e `.status-item` ganhou `min-width:0` — quadros iguais, sem transbordo (mesma família de bug no gráfico do canvas em janelas menores, resolvida de quebra; `canvas` com `max-width:100%`); (2) a quebra por medidor virou DUAS micro-linhas (`\n` + `white-space:pre-line`) em vez de uma com "·" — numa coluna de ~100px a linha única ia cortar com reticências justamente os R$ do carro; (3) ajuste fino medido no navegador: `si-val` 1.5→1.35rem com `nowrap` (o "kWh" do valor quebrava sozinho pra linha de baixo com valores ≥6 dígitos), ícone 44→40px, gap 1rem→.8rem, padding lateral 1.2→1.1rem. Barra final: 110px de altura (era 100 sem a quebra), overflow 0 e zero cortes em 1920/1366/1100/640 (validado com mock + screenshots).

## [Unreleased] - 2026-10-07

### Fixed
- 📊 **Cabeçalho do Dashboard agora resume a casa INTEIRA (os dois medidores), não só a fase 1** — Consumo, Corrente e Energia hoje liam apenas `devs.fase1`/`today_kwh`: com o carro carregando, o resumo ignorava ~2 kW instantâneos e todo o kWh do disjuntor (o custo já somava os dois desde 04/10, o resto do cabeçalho não). Agora cada campo traz o SOMATÓRIO casa + carro com a quebra por medidor numa linha menor embaixo (`casa 342 · carro 2180 W`): **Consumo** = fase 1 + disjuntor (W), **Corrente** = soma das correntes (mesma fase), **Energia hoje** = `today_kwh + breaker_kwh` (kWh), **Custo Hoje/Mês** ganham a quebra em R$ por circuito e tooltip com a conta (`Casa 3.21 + Carro 8.45 kWh × R$ 0.956/kWh`). **Tensão** não se soma (mesma fase): fica a fase 1 como referência, com os dois valores na linha de baixo. Gráficos ao vivo acompanham o total (títulos: "Potência total (W)" e "Energia Acumulada hoje — casa + carro"); o card "Fase 1" e o card do carro seguem por medidor, e a detecção de carga/timer do carro continuam na potência da fase 1 (comportamento inalterado). `updateUI` passou a receber o objeto `/api/today` inteiro (precisa do `breaker_kwh`).

## [Unreleased] - 2026-10-06

### Fixed
- 🐛 **Filtro de período (7d/30d/90d/1 ano/Tudo) da aba Carregamentos não filtrava a tabela de sessões** — só alimentava o fetch dos cards; a tabela puxava `/api/charge/sessions` sem período e listava o histórico inteiro (a mensagem "Nenhum carregamento no período." nunca batia com as linhas de baixo). Agora os DOIS fetches compartilham a mesma query de período: `list_charge_sessions` e o endpoint `/api/charge/sessions` aceitam `days`/`since` (mesmo corte `start_time >= ?` do summary; `since` inválido → 400; `days` limitado a 1–9999, onde 9999 = tudo) e `/api/charge/summary` aceita `since`. Opção nova **"Mês"** (entre "1 ano" e "Tudo"): desde o dia 1º do mês corrente, via `since` com data local do navegador (sem toISOString/UTC). Botões com `data-period` (acabou o mapa posicional do active) e estado único `chargePeriod`.

## [Unreleased] - 2026-10-04

### Fixed
- 💰 **Todo custo TOTAL da casa agora soma fase 1 + disjuntor do carro** — casa e carro são circuitos SEPARADOS (fase1 não inclui o carro; o gráfico diário já empilhava os dois), mas os custos exibidos multiplicavam SÓ o kWh da fase 1 pela tarifa: o custo do carro só aparecia picado em colunas/valores próprios. Onde um total é indicado, agora cobre os dois canais como a conta de luz: **Custo Hoje** = (fase1 hoje + breaker hoje) × tarifa; **Custo Mês** soma o breaker via snapshots diários (`month_breaker_kwh`, mesmo esquema de snapshots + integral ao vivo dos dias sem snapshot, cache TTL de 60s); **Custo Total do Histórico** (resumo mensal) = (fase1 + breaker) × tarifa, com `breaker_kwh` exposto na resposta; **Custo do Hora a Hora** soma o breaker do dia (integral `_sql_kwh` em `phase_c`, filtro min 1W). **Média/Dia em R$** do Histórico passou a derivar do Custo Total ÷ dias (antes: média kWh da casa × tarifa — base diferente do total ao lado). O `_kwh_snapshots_plus_missing_days` foi parametrizado (device/col/min_avg_w) para servir os dois canais. O que NÃO mudou, de propósito: tabela do Histórico continua com colunas separadas (Custo da casa / Custo ⚡ do carro), Carregamentos e Plano de carga seguem custos do carro puros, e os totais em kWh seguem por circuito (Consumo Total = casa; carro à parte) — só o R$ junta os dois. Tooltips "Casa (fase 1) + carro (disjuntor) × tarifa" nos 4 pontos. 5 testes novos (`tests/test_cost_totals.py`) com DB descartável, robustos à data de execução: soma nos 3 endpoints, dia vazio, só-snapshots sem leituras.

## [Unreleased] - 2026-10-03

### Fixed
- 🐛 **"⏰ Consumo Hora a Hora" travado no dia do load da página** — o seletor de data era preenchido UMA vez no `initCharts()` (data local do navegador no momento em que a página carregou) e o `setInterval(loadHourly, 120s)` buscava eternamente essa data fixa: com o painel aberto desde setembro, o gráfico mostrava 30/09 para sempre ("travado no mês passado"), mesmo com o poll rodando a cada 2 min. Agora a data é um estado do frontend (`hourlyViewDate`): `null` = modo "hoje ao vivo" — recalculado a cada refresh via `todayLocalStr()` (data LOCAL do navegador), então o gráfico rola sozinho para o dia novo à meia-noite com o painel aberto há dias, e o input ressincroniza junto. Navegação nova no mesmo padrão do navegador de meses do Histórico: ◀ ▶ (dia anterior/próximo), botão "Hoje" (volta ao modo ao vivo) e 🔄; ▶ e "Hoje" desabilitam no dia corrente, "Hoje" fica destacado quando ativo, o input continua selecionável direto (max = hoje) e a resposta velha de um clique anterior é descartada por contador de requisição — clique duplo rápido em ◀ não mistura dias. O `<input type="date">` nativo renderizava mm/dd/aaaa (formato US do navegador) — trocado por um mostrador sempre em dd/mm/aaaa (`fmtBR`); clicar nele abre o seletor de datas nativo (`showPicker()`), que continua dirigindo a troca via o input invisível. Na placa, a resposta do `/api/hourly` de outro dia às vezes leva 1–2s (servidor ocupado com os polls) e o card ficava um tempo mostrando o número do dia ANTERIOR como se fosse o selecionado — as estatísticas agora escurecem durante a troca de dia e voltam ao normal quando o dia certo chega.

## [Unreleased] - 2026-09-30

### Fixed
- 🐛 **"92%" com o carro cheio: SOC final estimado não chegava a 100%** — na carga de 28/09 (sessão 128, 15%→"92,09%", 11,7 kWh) o carro PAROU SOZINHO de puxar corrente (auto-stop "charge complete, idle confirmed" — o BMS só corta quando a bateria enche), mas o `soc_end` gravado foi a estimativa energética: 15 + (11,7 × 0,85 ÷ 12,9) × 100 = 92,09. O sistema não lê o SOC do carro (sem CAN/OBD); todo percentual é modelo, e o fator `car_charge_efficiency=0,85` está baixo demais para esse carro (real ≈0,94). A sessão 127 mostrou 100% por sorte (12,8 kWh saturou o teto do modelo); a 128 expôs o erro. A reconciliação do soc_start existente só disparava quando a ESTIMATIVA ≥99 — exatamente quando o modelo já acertou; com o carro cheio e estimativa em 92, nada era corrigido. Agora, no fim CONFIANTE (ação `'conclude'`: parada com energia injetada ≥90% do necessário até o alvo), o `soc_end` é PINADO em 100 — o carro só para sozinho quando encheu de verdade — e a conta é feita de trás pra frente, como o dono pediu: partida real = 100 − energia medida ÷ eficiência. De quebra o fim confiante vira dado de calibração: η real = (100 − partida) × bateria ÷ energia medida, auto-aprendida por sessão cheia (limitada a 0,70–1,00, ≥2 kWh) e persistida no config. Fim NÃO confiante (`'probe_conclude'` — sonda provou que o carro não retomou, não que encheu) continua na estimativa energética. 5 testes novos com os números reais da sessão 128 (η aprendida 0,937 → partida reconciliada ≈15%, o padrão de uso que já batia com o real).
- 🐛 **Race do `save_config` revertia a escrita do finalize (`car_current_soc` congelado desde 20/09)** — no poll loop, `cfg = load_config()` (cache de 2s), depois `finalize_charge_session()` grava `car_current_soc`/`car_charge_efficiency` no config, e o `save_config(cfg)` de fechamento escrevia de volta o dict VELHO — lost update clássico. As sessões 126, 127 e 128 tiveram a escrita descartada: o campo está travado em 15 com timestamp de 2026-09-20. Agora o poll loop recarrega o config (`cfg = load_config()`) após cada finalize antes de salvar (3 sites: desligamento externo, `'conclude'`, `'probe_conclude'`; o endpoint `/api/car/stop-charge` já carregava depois).

## [Unreleased] - 2026-09-23

### Fixed
- 🐛 **Auto-stop abriu o disjuntor com o carro em 62%** — na carga de 22/09 (sessão 126, 18:10→20:25, 25%→62,6%, 5,7 kWh) o carro PAUSOU sozinho às 20:23:30 (potência DPS 6 → 0 e contador DPS 1 congelaram juntos — pausa real, não glitch; relé fechado o tempo todo) e após os 120s de `car_charge_idle_seconds_to_stop` o auto-stop leu "carga completa, idle confirmed", abriu o relé e finalizou com `end_reason='auto'` — alvo era 100%. BMS/balanceamento, térmica do carregador ou renegociação EVSE pausam >2 min; a janela curta transformava qualquer pausa em fim de carga irreversível (relé aberto, sem retomada possível). Três frentes: (1) **janela de idle 120s → 300s** (config na placa + defaults; pausa de 2 min deixa de ser "fim"); (2) **critério de energia injetada** (`_charge_complete_confident_locked`): só conclui direto quando a energia entregue ≥ 90% do necessário até o alvo (alvo − SOC inicial, com eficiência) — "quase 100% injetado" garante o fim; pouca energia injetada NÃO conclui, pois a estimativa de SOC inicial pode estar otimista demais; (3) **sonda de retomada** (`auto_stop_action`, máquina de fases por timestamp — sem sleep no loop): pausa longe do alvo abre o relé por 120s, religa e observa pela janela de idle — carro retomou (`'resumed'`) segue a sessão de onde parou; ainda zerado → `'probe_conclude'` desliga e finaliza. Durante a sonda, `is_probing()` blinda o ramo "Breaker desligado externamente" (o relé está aberto por decisão nossa, não por ação externa) e a decisão roda em todo poll inclusive com switch=0, senão a fase 'open' não termina. `get_status()` expõe `probe_phase` e mensagem dedicada ("Pausa detectada — testando retomada (relé aberto, religa em Ns)"). 10 testes novos cobrindo o incidente (números reais da sessão 126).

## [Unreleased] - 2026-09-20

### Fixed
- 🐛 **Carga do carro partindo em duas linhas no Carregamentos** — pela 2ª vez seguida (18/09 e 20/09) uma carga única apareceu como duas sessões: o serviço finaliza a sessão no meio da carga ("Breaker desligado externamente durante sessão — finalizando") e a auto-detecção abre outra 6–12s depois (122: 8,5 kWh às 22:50:37 + 123: 1,7 kWh às 22:50:43; 124: 6,1 kWh às 18:21:56 + 125 às 18:22:08). O DPS 16 (switch) queima como False em RAJADA com o relé fechado: nas leituras brutas, switch=0 por ~20s com 2,6–2,7 kW fluindo o tempo todo. O debounce de 3 polls (fix de 02/08, para leitura ISOLADA corrompida) não basta porque o poll roda a cada 5s e a leitura do breaker a cada 10s — DUAS leituras ruins consecutivas viram 4 iterações do contador e estouram o limite. Correção em três frentes: (1) desligamento externo agora exige DOIS critérios persistentes (`breaker_off_externally_confirmed`): switch=0 **E** potência abaixo do idle em polls consecutivos — relé aberto de verdade corta o consumo junto, switch=0 com 2,7 kW é ruído (latência de desligamento real continua ~15s); (2) `read_breaker` repete o último valor conhecido do DPS 16 quando a resposta vem parcial sem ele (como já fazia com o DPS 1), em vez de defaultar para False; (3) `scripts/merge_charge_sessions.py` funde pares já gravados com as travas de segurança (gap ≤ 120s, nada 'active', contador monótono) — sessões 122+123 fundidas (10,30 kWh em 232,7 min) e 124+125 após o fim da carga. 7 testes novos cobrindo os dois incidentes.

## [Unreleased] - 2026-09-15

### Fixed
- 🐛 **Dashboard "Offline" por disco cheio na cubie2** — o `querylog.json` do AdGuardHome (mesma partição `/media/mmcblk0p1`) cresceu até encher os 467MB (219MB só de logs DNS: a retenção `interval: 3d` do AdGuard só purga o arquivo no restart). Sem espaço, o SQLite lançava `disk I/O error`, `/api/today` e `/api/charge/state` retornavam 500 e o frontend marcava "Offline" (a coleta parou às 16:10). Recuperação: querylog em arquivo desativado no AdGuard (`file_enabled: false` — mantém buffer de 1000 entradas em RAM visível na UI), logs antigos apagados, backup `apkovl.bak-20260910` movido para o PC. Banco verificado `PRAGMA quick_check: ok`, sem corrupção. Disco: 0MB → 254MB livres.

### Changed
- 📉 **Rate-limit dos warnings que inundavam o `service.log`** — três fontes de spam a cada poll agora avisam com moderação: "fase1 voltage OK but power=0" só na transição pra idle (era 1 linha a cada 5s durante a madrugada inteira), `Poll error` e `Erro fase1/breaker` só no 1º erro e depois 1x/hora com contagem de consecutivos (360 ciclos), zerando ao recuperar. Era assim que o log chegava a MBs e ajudava a encher a partição. `deploy_cubie.sh` passa a excluir `.venv` (um venv local de testes foi rsyncado pra placa numa deploy e a FAT não suporta symlinks).

### Added
- 🆕 **Vigia de disco na cubie2** (`/etc/vigia/vigia-disco.sh`, cron */15, commitado no lbu) — alerta via Telegram quando a partição persistente passa de 85%, com debounce de 6h e mensagem de normalização, no mesmo padrão dos vigias de túnel/Vibe-Trading. Nasceu do incidente de hoje: sem ele, o disco encheu silenciosamente até derrubar o energia. Rotação semanal do `service.log` via cron (cp+truncate, segunda 05:10 — `mv` quebraria o fd aberto pelo supervise-daemon).

## [Unreleased] - 2026-09-13

### Changed
- 📊 **"Energia Acumulada" agora mostra o consumo real do dia ao vivo** — o valor vinha direto do DPS 17 do medidor fase1, que os dados provaram não ser um contador confiável: em 200k leituras ele DIMINUI 7–15×/dia (até ~0,48 kWh de uma vez), reseta no meio do dia (raw 39→1 às 21h de 01/08), não reseta à meia-noite e oscila entre 0,001–0,77 kWh quando o consumo real é 2,3–5,0 kWh/dia (integração power×tempo). O próprio backend já o tratava como opcional ("unreliable and often None") e todo o resto do dashboard usa integral de potência — o gráfico era o último lugar que confiava nele. Agora o gráfico "🔋 Energia Acumulada" plota o `today_kwh` do `/api/today` (integral SQL de power×tempo, atualiza a cada poll de 3s): curva ascendente que parte do que já foi consumido no dia e vai acumulando a leitura em tempo real, numa janela visível de 10 min (trim por timestamp, não por contagem de pontos). Card "Energia" do cabeçalho e rodapé "Energia Acumulada" da Fase 1 mostram o mesmo número; "null" do servidor não é mais empurrado como 0 para o gráfico (derrubava a curva a zero). Rótulo do cabeçalho agora diz "Energia hoje".

## [Unreleased] - 2026-09-12

### Added
- 🚀 **Coleta da fase1 3× mais rápida** — o ciclo real era 16–34s (medido no DB: moda 16–17s) para um `POLL_INTERVAL` nominal de 10s. Causas: (1) `read_breaker` chamava `updatedps()` que o breaker NÃO responde (comentário do próprio código admitia) e o tinytuya, sem resposta, estoura 5s de timeout × 5 retries = até ~25s presos por ciclo; (2) fase1 e breaker lidos EM SEQUÊNCIA no mesmo loop — breaker lento/offline travava a fase1; (3) `sleep(10)` depois do trabalho — o período real acumulava o tempo de leitura. Agora: cada device tem thread de leitura própria com cadência e deadline absolutos (fase1 a cada 5s, breaker a cada 10s), timeouts do tinytuya limitados (3s × 1 retry em vez de 5s × 5), loop de controle roda a cada 5s. Frontend busca `/api/status` a cada 3s (era 6s). `DB_MAX_ROWS` 200k → 500k para manter ~29 dias de leituras brutas na nova cadência (integrais kWh e snapshots ficam MAIS precisos com amostras mais densas). Obs.: a precisão intrínseca continua sendo a do medidor (DPS atualizado internamente ~1s, chip ±1–2%) — coleta mais fresca, não mais precisa; cloud seria pior (minutos).
- 🆕 **Dica "próxima carga" no card do SOC** — com o carro parado, `/api/charge/state` expõe `next_start_soc` + `next_start_soc_is_estimate` (de `estimate_car_soc_start`), e o card "🔋 SOC Atual do Carro" mostra ao lado do campo editável: "próxima carga inicia em ~25% (estimado)". Diferencia os dois números que viviam misturados: onde a bateria ESTÁ (campo, 100% após a carga) vs. onde a próxima carga vai COMEÇAR (estimativa aprendida do soc_start reconciliado da última sessão completa). Input explícito do usuário não gera dica (o campo já o exibe).

### Fixed
- 🐛 **"🔋 SOC Atual do Carro" não atualizava após a carga** — no fim da sessão o campo era semeado com o soc_start RECONCILIADO (ex.: 31%), que é a estimativa de partida da PRÓXIMA carga (padrão de uso se repete) — não onde o carro está. Resultado: o usuário via o SOC subir até 100% durante a carga e "voltar" para o valor da carga anterior quando a sessão encerrava. Agora o campo recebe o soc_end (100% quando o carro para sozinho; soc_end parcial em fim manual) com timestamp do fim — a estimativa de partida continua por conta de `estimate_car_soc_start` (soc_start reconciliado da última sessão completa); input explícito do usuário com ts estritamente posterior continua ganhando, pois o ts semeado é IGUAL ao end_time. Migração one-shot `car_soc_field_end_v1` re-semeia o campo com o fim da última sessão real (corrige o 31% preso da carga de 11/09).

## [Unreleased] - 2026-09-08

### Fixed
- 🐛 **Consumo Diário/Detalhamento "voltaram" ao valor errado** — o fix de 07/09 (dia em curso integrado ao vivo) funcionou ontem, mas à meia-noite o rollover tentou fechar o snapshot de 07/09 e FALHOU em toda iteração: o `INSERT` do snapshot fase1 tinha 6 valores para 5 colunas (`VALUES (?, 'fase1', ?, ?, ?, ?)` — um `?` sobrando, 4 params). O snapshot PARCIAL da manhã (gravado pelo backfill pré-fix: casa 2,91 / carro 3,31 kWh) ficou no banco e virou autoritativo quando 07/09 virou dia fechado. Correção do SQL + restart: rollover regravou 07/09 completo (casa 4,75 / carro 10,63 kWh, bate com a sessão de 10,40). Como o erro era silencioso por dia (`snap_ok=False` mantém `last_snapshot_day` para retry), o log `⚠️ Erro ao fechar snapshot` agora é o sinal — estava funcionando, só ninguém tinha olhado.

### Added
- 🆕 **Ciclo de aprendizado do SOC** — a reconciliação do fim de sessão agora alimenta a próxima: (1) `finalize_charge_session` grava o SOC inicial reconciliado no campo "SOC Atual do Carro" (config + dashboard); (2) `estimate_car_soc_start` passa a usar o **SOC inicial reconciliado da última sessão completa** (carro encheu sozinho) como estimativa de partida da próxima — em vez do soc_end=100%, que satura a estimativa na primeira leitura. Sessão que não terminou cheia continua usando o soc_end dela; input explícito do usuário (com timestamp posterior) sempre ganha. Loop: plugou → estimativa aprendida → carregou → reconciliou → campo atualizado → próxima plugada parte do valor aprendido.
- 🆕 **Reconciliação do SOC inicial no fim da sessão** — quando a sessão termina porque o CARRO parou sozinho (`end_reason='auto'`, soc_end ≥ 99%), o soc_start verdadeiro é retrocalculado: `100% − (energia entregue × eficiência ÷ bateria)`. A estimativa carregada no início (soc_end da sessão anterior) satura em 100% antes do carro encher — na sessão de 07/09 a estimativa dizia 73,4% mas o carro puxou 10,40 kWh até parar, ou seja, partiu de ~31,5%. Gravado no fim da sessão automaticamente; sessão de hoje corrigida retroativamente (31% → 100%).

## [Unreleased] - 2026-09-07 (3)

### Removed
- 🗑️ **Botões "☁️ Sync" e "🗑️ Limpar" do Histórico** — Sync forçava download da nuvem Tuya, mas o sistema é 100% local (`cloud_enabled=false`); Limpar apagava leituras >30 dias manualmente, redundante com o `prune_db` automático (teto de 200k leituras) e arriscado (clique acidental). Endpoints `/api/cloud-sync` e `/api/clear-db` mantidos no backend (inofensivos, úteis via API).

### Fixed
- 🐛 **Navegação de meses do Histórico não mudava os gráficos** — ◀ ▶ atualizavam só os cards de resumo (`/api/monthly`); gráfico e tabela buscavam sempre a janela fixa dos últimos 30 dias. Agora `/api/daily-history?year&month` retorna o calendário do mês escolhido (31 dias em agosto, com as cargas de 02/08 (11,3) e 04/08 (9,5) que ficavam fora da janela), e o frontend navega com ele. Cache TTL por mês.
- 🐛 **"📅 Detalhamento Diário" com cabeçalho e linhas errados** — as colunas diziam "kWh Total"/"Custo" mas traziam só os valores da CASA (mesmo desalinho conceitual do gráfico); renomeadas para Casa kWh / Casa R$ / Carro kWh / Carro R$ / Casa W (média). E o modo mês incluía DIAS FUTUROS com zeros (setembro listava até 30/09) — agora o mês corrente lista só até hoje.
- 🐛 **Barra do dia atual congelada no Consumo Diário** — o backfill de snapshots (rodado no restart da migração de fuso) gravou snapshot PARCIAL do dia em curso, e o histórico diário/mensal preferiam snapshot sobre integração ao vivo: a barra de hoje travou em 3,3 kWh enquanto o carro entregava 10,40. Correção: dia em curso é SEMPRE integrado ao vivo no diário e no mensal (snapshots só para dias fechados), e o backfill de snapshots não grava mais o dia corrente.
- 🐛 **Gráfico "Consumo Diário" com barras erradas** — o dataset "Casa" subtraía o carro (`consumed_kwh − breaker_kwh`), mas fase1 e breaker são circuitos SEPARADOS (fase1 não mede o carro: durante uma carga de 2,7 kW ele marca ~74 W). Em dias de carga forte a barra da casa virava 0 e a barra aparecia com uma cor só. Agora: barras empilhadas Casa (verde) + Carro (azul) somando os dois medidores.
- 🐛 **Primeira e última barra do Consumo Diário cortadas pela metade** — o eixo x de categorias estava sem `offset` para as barras; fix com `x.offset: true` (também no gráfico horário).
- 🐛 **"✅ PRONTO/COMPLETO" aparecia com o carro ainda carregando** — o timer do dashboard sinalizava fim pela estimativa de SOC (`target_reached`), que satura em 100% antes do carro realmente terminar. Agora: carregando → previsão restante (nunca "completo"); carro parou de consumir → "⏹ FINALIZANDO" (com a barra de confirmação); fim real → só quando o disjuntor desligar (timer some, badge DESLIGADO, sessão final na aba Carregamentos).
- 🐛 **Fuso horário: sistema inteiro em UTC** — a placa roda Alpine com relógio UTC e nunca teve tzdata; a migração BRT de 22/08 converteu os dados antigos mas o relógio continuou em UTC, então TUDO gravado desde 23/08 (~00:23 UTC) estava +3h (sessões, leituras, limites de dia — carga das 17:44 parecia das 20:44 e "atracessava a meia-noite"). Correção completa:
  - Placa: `setup-timezone -z America/Sao_Paulo` (tzdata instalado, `/etc/localtime` → BRT)
  - Dados: migração one-shot `tz_brt_v2_migrated` — leituras/sessões ≥ 23/08 deslocadas -3h (67.712 leituras, 11 sessões), snapshots do período apagados e reconstruídos com limite de dia BRT (49 dias fase1 + 15 breaker)
  - Efeito colateral positivo: as "divergências de meia-noite" (23/08+24/08, 30/08+31/08, 05/09+06/09) eram artefato de UTC — agora cada carga cai inteira no dia real
- 🐛 Frontend: seletor de data do gráfico "Consumo por hora" usava `toISOString()` (UTC) — depois das 21h BRT abria no dia seguinte. Agora usa data local do navegador.
- 🐛 Serviço na placa sem logs (stdout → /dev/null): init.d agora grava `logs/service.log`/`service.err` — visibilidade para diagnosticar o rollover de snapshot (travado em 08-30, sob investigação).

## [Unreleased] - 2026-09-07 (2)

### Added
- 🆕 **Reconciliação Histórico × Carregamentos** — o Histórico diario mede a energia do carro integrando a potência do disjuntor, mas dias com carga não rastreada (bug de auto-detecção anterior ao fix de hoje, service fora do ar, etc.) não tinham linha na aba Carregamentos. Agora, no primeiro startup após o deploy, `backfill_charge_sessions_from_readings()` reconstrói essas sessões a partir das leituras: janelas de potência >50 W (lacunas ≤15 min ponteadas), energia pelo delta do contador DPS 1 (com sanidade contra a integral; cai na integral se o contador estiver inconsistente), duração, potência média e custo pela tarifa atual. Sessões que já existem (±5 min) não são duplicadas; sessão ativa em curso é respeitada. Status novo `reconstructed` (badge "RECUPERADA", roxo) incluído nos resumos; SOC fica vazio porque é desconhecido. One-shot via gate `charge_sessions_backfill_v1`.

## [Unreleased] - 2026-09-07

### Fixed
- 🐛 **Auto-detecção morria após o primeiro auto-stop** — `stop(reason="auto")` deixava o tracker em `done` e a auto-detecção do `poll_loop` só dispara a partir de `idle`; o próximo carregamento (disjuntor ligado manualmente) ficava **sem sessão e sem registro no DB** até restart do serviço. Agora o tracker volta sempre a `idle`.
- 🐛 Correção de SOC no dashboard durante uma sessão ativa era ignorada pelo tracker (só gravava config) — agora rebaseia o SOC efetivo na hora e atualiza `soc_start` da sessão no DB.

### Added
- 🆕 **Estimativa de SOC inicial** para carregamento iniciado manualmente (disjuntor ligado sem informar SOC): usa o `soc_end` da última sessão real; input explícito do usuário informado **depois** dela tem precedência. Vale tanto na auto-detecção quanto em "Iniciar Carregamento".
- 🆕 `GET /api/charge/state` expõe `idle_seconds_needed` — countdown de finalização no frontend deixa de ser hardcoded (120s).

### Changed
- ✏️ Previsões do cliente (Plano de Carga, ETA do SOC, timer antes de ler o tracker) agora consideram a eficiência de carga (`car_charge_efficiency`) e o custo é calculado sobre o kWh da rede, igual ao servidor.
- ✏️ Auto-detecção usa o limiar `car_charge_start_power_w` do config (era fixo 500).

## [Unreleased] - 2026-06-02

### Added
- 🆕 **Aba "Carregamentos"** — histórico de cada sessão de carga com SOC inicial/final, kWh, custo e duração
- 🆕 **Tabela `charge_sessions`** no SQLite — persiste cada sessão com UUID, start/end times, energy delivered, total cost
- 🆕 **Endpoints**:
  - `GET /api/charge/sessions` — lista sessões (ativas + finalizadas)
  - `GET /api/charge/summary` — resumo agregado (períodos: 7d, 30d, 90d, 1 ano, tudo)
- 🆕 **Auto-shutdown inteligente** — só desliga disjuntor quando SOC efetivo ≥ meta E consumo zera por 120s
- 🆕 **SOC efetivo** calculado a partir de energia real entregue (não SOC declarado)
- 🆕 Cards de info do breaker: Fault, Temperatura, Corrente por fase
- 🆕 Endpoints pra controlar modo prepayment (`/api/breaker/prepay/on|off`)
- 🆕 Toggle "Auto-desligar" no dashboard

### Changed
- ✏️ **Bugfix**: disjuntor usava DPS 11 (prepay) em vez de DPS 16 (real switch) — agora liga/desliga corretamente
- ✏️ **Bugfix**: `no_balance_alarm` agora detectado via fault_code bitfield
- ✏️ **Bugfix**: dashboard tinha layout quebrado nos breaker cards (faltava CSS)
- ✏️ **Bugfix**: `phase_a` agora formatado como inteiro (mA)
- ✏️ **Bugfix**: aba Carregamentos mostrava várias linhas pra mesma sessão de carga de hoje — agora `create_charge_session` finaliza automaticamente sessões `active` órfãs antes de criar uma nova, e `list_charge_sessions` ordena o registro ativo primeiro (defesa em profundidade). Recuperação no startup também limpa ghosts antigos.
- ✏️ Refatorado: credenciais agora em `data/devices.json` (não hardcoded)
- ✏️ Refatorado: BASE_DIR aponta pro raiz do projeto, DB em `data/`

### Migration from old tuya-dashboard
1. Copie `data/devices.json` com suas credenciais
2. O serviço antigo (`tuya-dashboard.service`) precisa ser desabilitado
3. Rode `./deploy/install.sh` pra instalar o novo `energia-domestica.service`
