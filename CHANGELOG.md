# Changelog

## [Unreleased] - 2026-09-12

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
