# karuta

Bot de pick automatizado para o [Karuta](https://karuta.io/) no Discord, rodando 3 contas em paralelo, cada uma num navegador Chromium-based diferente (Chrome, Brave, Opera GX), usando [Playwright](https://playwright.dev/python/).

> ⚠️ **Aviso**: automatizar contas de usuário no Discord viola o ToS do Discord e do Karuta. Risco de ban da conta Discord e ban permanente no Karuta. Use por sua conta e risco.

## Como funciona

1. Conta **A** envia `kd` no canal — o Karuta dropa 3 cartas com botões.
2. As 3 contas pickam imediatamente: **A→botão 1**, **B→botão 2**, **C→botão 3**.
3. Após o cooldown de pick (~11 min), conta **B** envia o próximo `kd`. E assim por diante (rotação A→B→C→A…).

### Ícone de evento

Durante eventos o Karuta adiciona um **4º ícone** no drop, além das 3 cartas. O bot verifica todo drop que ele mesmo gerou e, se o ícone veio, **só uma conta clica nele** — as outras ignoram.

A detecção é genérica: qualquer reaction do drop que não seja `one`/`two`/`three` conta como ícone de evento. Não precisa saber qual emoji o evento usa.

```yaml
event_icon:
  enabled: true
  grabber: a       # única conta que clica no ícone
  mode: extra      # extra = pega a carta dela E o ícone
                   # instead = pega SÓ o ícone, abre mão da carta
```

Em `mode: instead` a checagem do ícone roda **antes** dos picks (o grabber precisa saber se abre mão da carta); em `extra` roda depois, para não atrasar a disputa pela carta.

### Dois canais

O bot separa o que é disputa do que é consulta, em dois canais:

| Canal | Config | O que roda lá |
|---|---|---|
| `#karuta-drops` | `channel_id` | `kd`, `krm` e os cliques de pick |
| `#karuta-info` | `info_channel_id` | `k!ci`, `k!wi`, `k!lu`, `kb`, `k!i`, `k!bi` e o `karuta scan` |

```yaml
channel_id: "1292965515815551006"       # drops
info_channel_id: "1533484123644100818"  # info — vazio ("") joga tudo no de drop
```

Cada conta abre **duas abas** no mesmo navegador, uma por canal. É por isso que são abas separadas em vez de uma só navegando entre canais: trocar de canal descartaria o `MutationObserver` que detecta os drops, e o bot ficaria cego durante toda a avaliação (~100s por ciclo).

Como as duas abas dividem um contexto, só uma fica visível por vez — quem vai usar a aba pede o foco antes (`bring_to_front`), já que o Chromium desprioriza aba em segundo plano. O foco volta para o canal de drop no `krm`, antes de cada `kd`.

## Banco de dados

PostgreSQL guarda as cartas obtidas **a partir do próximo `kd`** — não há carga retroativa da coleção antiga.

### Quando roda

A avaliação acontece **no intervalo entre um drop e o próximo**, não durante a disputa pelo pick:

```
kd → drop → 3 picks → ícone de evento → AVALIAÇÃO → espera cooldown → kd …
```

Depois dos picks, o bot lê a confirmação do Karuta (`took the X card <code>`) para saber qual carta cada conta levou e roda **três comandos** por carta:

| Comando | Atalho | O que extrai |
|---|---|---|
| `k!workerinfo <code>` | `k!wi` | valor base, esforço → decide **job** |
| `k!cardinfo <code>` | `k!ci` | ★, `#print`, `◈edição`, série, condição |
| `k!lookup <personagem>` | `k!lu` | wishlists, circulação, claim rate → decide **desejada** |

Só depois disso a carta é gravada. Assim a tabela mestre nunca tem carta sem avaliação. Custa 3 cartas × 3 comandos × 11s ≈ 99s, contra ~11 min de cooldown ocioso.

> ⚠️ `k!lookup` busca por **nome de personagem**, não por código — por isso depende do `k!ci` ter rodado antes. E **não** use `k!i`: esse abre o inventário, não a carta.

### Tabelas

Toda carta entra em `cartas` (mestre). As especializadas são subconjuntos, e uma carta pode estar em mais de um:

| Tabela | O que guarda |
|---|---|
| `cartas` | mestre — tudo que possuímos, com avaliação |
| `cartas_pegas` | histórico de cada pick, inclusive os que falharam |
| `cartas_job` | boas para trabalhar (esforço ou valor base altos) |
| `cartas_desejadas` | personagens que outros jogadores querem (wishlists) |
| `cartas_burn` | candidatas a queimar — fracas nos dois critérios |
| `avaliacoes` | texto bruto das respostas do Karuta, para reprocessar |
| `eventos_icone` | ícones de evento pegos |
| `contas` | cadastro das contas (a, b, c) |

Tudo separado por `conta_id`. Views prontas: `vw_resumo_por_conta` e `vw_melhores_job`.

### Comandos

```bash
karuta banco init                    # cria esquema e cadastra as contas
karuta banco resumo                  # cartas/avaliadas/pendentes por conta
karuta banco job --conta a           # melhores workers
karuta banco desejadas               # mais pedidas por outros jogadores
karuta banco burn --limite 50        # candidatas a queimar
```

### Painel web

```bash
karuta painel                        # http://127.0.0.1:8787
karuta painel --porta 9000 --abrir   # outra porta, abrindo o navegador
karuta painel --host 0.0.0.0         # acessível de outra máquina na rede
```

#### Pelo VS Code

O projeto já vem com `.vscode/` configurado (arquivos locais — `.vscode/` está no `.gitignore`):

1. **Ctrl+Shift+P → "Tasks: Run Task" → "Karuta: painel"** — sobe o servidor num terminal dedicado. (Tem também "Karuta: painel (escolher porta)" e "Karuta: resumo do banco".)
2. **Ctrl+clique no link `http://127.0.0.1:8787`** que aparece no terminal — abre o painel **dentro do editor**, numa aba ao lado do código.

O passo 2 funciona por causa de `workbench.externalUriOpeners` em `.vscode/settings.json`, que manda qualquer URL de `127.0.0.1`/`localhost` pro Simple Browser embutido. Sem isso, o Ctrl+clique iria pro navegador do Windows.

Alternativas: **Ctrl+Shift+P → "Simple Browser: Show"** e colar a URL; ou a aba **PORTS** (a porta 8787 aparece sozinha rotulada `karuta painel`, com o ícone de globo pra abrir no navegador). Pra mexer no código do painel com breakpoint, **F5 → "Karuta: painel (debug)"**.

Painel local, só leitura, pode rodar junto com `karuta start`. Cobre as três tabelas de carta: `cartas`, `cartas_job` e `cartas_desejadas`.

Barra lateral: conta (a/b/c/todas), seção e visão. Topo: número-herói + stat tiles (avaliadas com medidor, pendentes, job, desejadas, melhor esforço, mais desejada).

**Visão geral** — gráficos calculados sobre as tabelas inteiras, não sobre a página carregada:

- *cartas por conta* — barra empilhada com rótulo direto
- *ritmo de pick* — cartas pegas em drop por hora nas últimas 24h (cai pra dias se não houve drop); só `origem='drop'`, porque o scan importa a coleção toda de uma vez e o pico achataria o resto
- *distribuição de estrelas* — 0 a 5★, empilhado por conta
- *esforço das cartas job* — histograma das faixas de k!wi
- *séries mais desejadas* — top 10 por soma de desejos

**Seções de tabela** (`cartas`, `job`, `desejadas`), cada uma com três visões:

| visão | pra quê |
|---|---|
| **tabela densa** | varrer tudo: todas as colunas, ordenação por clique, barra proporcional na métrica principal |
| **galeria** | olhar carta a carta: personagem, série, estrelas, print e medidores de esforço/desejos |
| **colunas por conta** | comparar as contas lado a lado, cada uma com seu top da métrica |

Filtros rápidos em chips (todas / avaliadas / pendentes / job / desejada / 3★+). Em `cartas` eles rodam **no SQL**, não no navegador: a tabela tem milhares de linhas e o painel carrega só umas centenas, então filtrar no cliente dava zero resultado — as mais recentes são scans ainda sem `k!wi`. A contagem no chip é o total real da tabela.

Ainda: busca por texto, limite de linhas, auto-refresh (10s por padrão) e tema claro/escuro/auto. Cada conta tem uma cor fixa (paleta validada pra daltonismo), sempre acompanhada da letra — cor nunca é o único canal. Se o banco piscar, a página avisa e tenta de novo no refresh seguinte.

A página vive em `src/karuta/web/` (`painel.html` + `painel.css` + `painel.js`), lida do disco a cada request: dá pra mexer no layout e só dar F5, sem reiniciar o servidor nem o bot. Os gráficos são SVG montado na mão — nenhuma biblioteca externa, o painel funciona offline.

Limiares de classificação ficam em `avaliacao:` no `config.yaml`. O banco é **opcional**: se cair, o bot continua pegando cartas e só loga aviso — perder registro não pode custar um pick.

## Setup

### 1. Instalar dependências

```bash
pip install -e .[dev]
playwright install chromium
```

### 2. Configurar

```bash
cp config.example.yaml config.yaml
cp .env.example .env
# editar config.yaml com guild_id, channel_id, info_channel_id e caminhos dos navegadores
```

Campos que valem atenção:

| campo | padrão | |
|---|---|---|
| `guild_id` / `channel_id` | — | servidor e canal do drop |
| `info_channel_id` | — | canal dos comandos de info. Vazio (`""`) manda tudo no canal de drop |
| `headless` | `true` | sem janela. Necessário para rodar como service |
| `carregar_imagens` | `false` | o bot lê o DOM, nunca pixels — desligar economiza memória. Ligue só para depurar com print legível |
| `banco.dsn` | — | Postgres. A senha pode ficar fora daqui, na env `KARUTA_DB_DSN` do `.env` |
| `accounts.*.discord_user_id` | `""` | ID numérico, usado pelo `transferir` (veja abaixo) |
| `avaliacao.esforco_min_job` etc. | — | limiares de classificação. Mudaram? rode `karuta banco reclassificar` |

### 3. Login em cada conta (uma vez)

A sessão fica salva em `profiles/account_X` e sobrevive a restarts — só precisa refazer se o Discord expirar o token.

Há três formas. **No WSL, prefira `--creds` ou `--qr`**: o modo com janela depende do WSLg conseguir pintar o Chromium, o que nesta máquina não acontece (a janela abre vazia).

```bash
# Email/senha, sem janela. Pergunta no terminal, senha oculta, nada é gravado
# em disco. Trata 2FA (pede o código) e aborta explicando se cair CAPTCHA.
karuta login a --creds

# QR code, sem janela. Salva o PNG em logs/qr_<conta>.png e refaz o print em
# loop, porque o código do Discord expira a cada ~2min.
karuta login a --qr          # escaneie em: Discord mobile > Config > Escanear QR

# Com janela (Linux nativo com desktop). Logue e feche a janela.
karuta login a
```

Repita para `b` e `c`.

#### ID de usuário de cada conta

O comando `transferir` precisa do ID numérico do Discord de cada conta, em `config.yaml`:

```yaml
accounts:
  a:
    discord_user_id: "374720793197936650"
```

Para obter: Discord → Configurações → Avançado → **Modo de desenvolvedor**, depois botão direito no próprio perfil → *Copiar ID do usuário*. Precisa ser o ID: o Karuta recusa nome de usuário, e contas diferentes podem compartilhar o mesmo nome de exibição.

### 4. Rodar

```bash
karuta start              # roda em foreground com logs no terminal
karuta start --dry-run    # detecta drops mas nao envia kd nem clica
karuta start --pick-only  # nao envia kd; pega qualquer drop que aparecer
karuta status             # mostra cooldowns atuais
```

Para rodar de forma permanente, veja [Rodar como service](#rodar-como-service-systemd) — é o modo recomendado, porque o bot sobrevive ao fechamento do terminal.

## Transferir cartas entre contas

Consolida as cartas numa conta só, via `k!multitrade`. Útil quando uma conta
concentra o progresso (work permit, workers) e as outras só coletam.

**Pare o bot antes**: os perfis do navegador ficam travados enquanto ele roda.

```bash
systemctl --user stop karuta

# As cartas classificadas como job/desejada, segundo o banco
karuta transferir --de b --para a

# A coleção REAL do Karuta, ordenada por desejo — use quando a conta tiver
# cartas que o bot nunca catalogou (o banco só conhece o que ele mesmo pegou)
karuta transferir --de b --para a --da-colecao --min-desejos 50

# Tudo, sem limiar
karuta transferir --de b --para a --da-colecao --min-desejos 0

karuta transferir --de b --para a --dry-run   # só lista, não envia

systemctl --user start karuta
```

| opção | padrão | |
|---|---|---|
| `--lote` | 50 | cartas por sessão. **50 é o máximo**: 75 e 100 o Karuta recusa |
| `--min-desejos` | 50 | com `--da-colecao`, limiar de ♡ |
| `--max-paginas` | 12 | páginas de `k!c` a ler; `0` = todas |
| `--max-cartas` | 0 | teto total; `0` = sem limite |

**É idempotente.** A tabela `transferencias` registra cada carta movida, então
interromper e religar é seguro: nada é reenviado nem duplicado. Rode de novo a
qualquer momento para levar o que acumulou desde a última vez.

O banco só é atualizado depois do Karuta confirmar (`"The trade has been
accepted"`). Uma sessão que expira não deixa o banco afirmando uma
transferência que não aconteceu.

## Plataforma

- **Linux nativo**: funciona out-of-the-box.
- **WSL2 com WSLg** (Windows 11): também funciona — Playwright abre janelas via WSLg.
- **WSL2 sem WSLg**: precisa de `Xvfb` ou rodar do lado Windows. Para Opera GX (que só existe no Windows), aponte `executable_path` em `config.yaml` para o `.exe` via `/mnt/c/...`.

## Estrutura

```
src/karuta/
├── cli.py              # comandos start / login / status / banco / transferir / painel
├── avaliador.py        # extrai e classifica as cartas (sem Playwright nem banco)
├── db.py               # esquema e persistencia em Postgres
├── scanner.py          # varredura da colecao (k!c + k!wi), paginacao
├── config.py           # carrega config.yaml + .env
├── browser.py          # factory de BrowserContext (Chrome/Brave/Opera GX)
├── discord_page.py     # interagir com Discord: enviar msg, clicar botão
├── drop_detector.py    # MutationObserver injetado p/ detectar drops
├── account.py          # encapsula 1 conta/navegador
├── orchestrator.py     # loop principal: rotação + cooldown
├── painel.py           # painel web local: servidor + consultas
├── web/                # a pagina do painel, lida do disco a cada request
│   ├── painel.html     #   marcacao
│   ├── painel.css      #   estilos (tokens de cor claro/escuro)
│   └── painel.js       #   logica + graficos em SVG
└── logging_setup.py    # loguru config
```

## Testes

```bash
pytest
```

## Rodar como service (systemd)

Para o bot rodar em background sem precisar manter o terminal aberto, instala como **systemd user service**:

```bash
./scripts/install-service.sh
systemctl --user start karuta
```

**Comandos:**
```bash
systemctl --user start    karuta   # iniciar
systemctl --user stop     karuta   # parar
systemctl --user restart  karuta   # reiniciar
systemctl --user status   karuta   # status
journalctl --user -u karuta -f     # logs ao vivo
journalctl --user -u karuta -n 200 # ultimas 200 linhas
```

**Para desinstalar:**
```bash
./scripts/uninstall-service.sh
```

O service já vem com duas proteções configuradas:

- **`Restart=on-failure`** — se o start falhar (Discord lento, uma conta sem
  subir), ele tenta de novo sozinho em 30s, até 5 vezes em 10min.
- **`RuntimeMaxSec=86400`** — recicla o processo a cada 24h. O Chromium vaza
  memória de forma lenta mas constante; medido em ~50-85 MB/h, o que leva o
  conjunto de ~2,5 GB para ~4 GB num dia. Reiniciar resolve, e é mais simples
  (e menos arriscado) do que recarregar abas durante a operação.

Habilitado no boot: como o service está em `default.target.wants`, ele sobe
sozinho quando o WSL inicia — sem precisar abrir terminal.

### Notas pra WSL2

- O service precisa do **WSLg** (Windows 11) pra abrir as janelas dos navegadores. Se usar `Xvfb`, ajuste `DISPLAY` no `karuta.service`.
- WSL2 desliga quando nenhum processo está rodando dentro dele. Se você fechar TODAS as janelas WSL e o computador suspender, o service também para. Mantenha pelo menos uma sessão WSL aberta (ou agende `wsl.exe -d Ubuntu --exec true` no startup).
- Os 3 navegadores aparecem no seu desktop como janelas normais via WSLg — minimize-as se quiser, mas **não feche**, senão o bot perde a sessão.
