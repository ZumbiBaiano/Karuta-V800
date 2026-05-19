# karuta

Bot de pick automatizado para o [Karuta](https://karuta.io/) no Discord, rodando 3 contas em paralelo, cada uma num navegador Chromium-based diferente (Chrome, Brave, Opera GX), usando [Playwright](https://playwright.dev/python/).

> ⚠️ **Aviso**: automatizar contas de usuário no Discord viola o ToS do Discord e do Karuta. Risco de ban da conta Discord e ban permanente no Karuta. Use por sua conta e risco.

## Como funciona

1. Conta **A** envia `kd` no canal — o Karuta dropa 3 cartas com botões.
2. As 3 contas pickam imediatamente: **A→botão 1**, **B→botão 2**, **C→botão 3**.
3. Após o cooldown de pick (~11 min), conta **B** envia o próximo `kd`. E assim por diante (rotação A→B→C→A…).

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
# editar config.yaml com guild_id, channel_id e caminhos dos navegadores
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
