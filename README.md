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

### 3. Login manual em cada conta (uma vez)

Cada navegador abre numa janela; logue na conta correspondente e feche. A sessão fica salva em `profiles/account_X`.

```bash
karuta login a   # abre Chrome → logar na conta A
karuta login b   # abre Brave → logar na conta B
karuta login c   # abre Opera GX → logar na conta C
```

### 4. Rodar

```bash
karuta start              # roda em foreground com logs no terminal
karuta start --dry-run    # detecta drops mas nao envia kd nem clica
karuta status             # mostra cooldowns atuais
```

## Plataforma

- **Linux nativo**: funciona out-of-the-box.
- **WSL2 com WSLg** (Windows 11): também funciona — Playwright abre janelas via WSLg.
- **WSL2 sem WSLg**: precisa de `Xvfb` ou rodar do lado Windows. Para Opera GX (que só existe no Windows), aponte `executable_path` em `config.yaml` para o `.exe` via `/mnt/c/...`.

## Estrutura

```
src/karuta/
├── cli.py              # comandos start / login / status
├── config.py           # carrega config.yaml + .env
├── browser.py          # factory de BrowserContext (Chrome/Brave/Opera GX)
├── discord_page.py     # interagir com Discord: enviar msg, clicar botão
├── drop_detector.py    # MutationObserver injetado p/ detectar drops
├── account.py          # encapsula 1 conta/navegador
├── orchestrator.py     # loop principal: rotação + cooldown
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

### Notas pra WSL2

- O service precisa do **WSLg** (Windows 11) pra abrir as janelas dos navegadores. Se usar `Xvfb`, ajuste `DISPLAY` no `karuta.service`.
- WSL2 desliga quando nenhum processo está rodando dentro dele. Se você fechar TODAS as janelas WSL e o computador suspender, o service também para. Mantenha pelo menos uma sessão WSL aberta (ou agende `wsl.exe -d Ubuntu --exec true` no startup).
- Os 3 navegadores aparecem no seu desktop como janelas normais via WSLg — minimize-as se quiser, mas **não feche**, senão o bot perde a sessão.
