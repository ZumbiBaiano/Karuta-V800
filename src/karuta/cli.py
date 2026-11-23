from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from playwright.async_api import async_playwright
from rich.console import Console
from rich.table import Table

from .account import Account
from .browser import launch_for_account
from .config import AccountId, load_config
from .logging_setup import setup_logging
from .orchestrator import STATE_FILE, Orchestrator
from .scanner import CardScan, PrefilterRule, render_markdown, scan_account

app = typer.Typer(add_completion=False, help="Karuta bot CLI")
console = Console()


def _bootstrap(level: str = "INFO"):
    setup_logging(level=level)
    return load_config()


@app.command()
def start(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Detecta drops mas nao envia kd nem clica.")] = False,
    pick_only: Annotated[bool, typer.Option("--pick-only", help="Nao envia kd; pega qualquer drop que aparecer (teste).")] = False,
    log_level: Annotated[str, typer.Option(help="DEBUG/INFO/WARNING/ERROR")] = "INFO",
):
    """Roda o bot com as 3 contas."""
    cfg = _bootstrap(log_level)
    orch = Orchestrator(cfg, dry_run=dry_run, pick_only=pick_only)
    try:
        asyncio.run(orch.run())
    except KeyboardInterrupt:
        logger.info("interrompido pelo usuario")
    except RuntimeError as e:
        # Falha de setup (conta sem acesso ao canal, sessao expirada...).
        # Mensagem limpa em vez de traceback: a causa ja foi logada acima.
        logger.error(str(e))
        raise typer.Exit(1) from None


@app.command()
def login(
    account_id: Annotated[str, typer.Argument(help="a, b ou c")],
    qr: Annotated[
        bool,
        typer.Option(
            "--qr",
            help="Login por QR code, sem abrir janela. Salva o QR num PNG que"
            " voce escaneia com o app do Discord no celular. Use quando a"
            " janela do navegador nao aparece (WSLg em copy mode, sem X, etc).",
        ),
    ] = False,
    creds: Annotated[
        bool,
        typer.Option(
            "--creds",
            help="Login por email/senha, sem abrir janela. Pergunta as"
            " credenciais aqui no terminal (senha oculta) e digita na pagina."
            " Nada eh gravado em disco.",
        ),
    ] = False,
    log_level: Annotated[str, typer.Option(help="DEBUG/INFO/WARNING/ERROR")] = "INFO",
):
    """Abre apenas o navegador da conta indicada para login manual."""
    cfg = _bootstrap(log_level)
    if account_id not in cfg.accounts:
        typer.echo(f"conta '{account_id}' nao existe no config", err=True)
        raise typer.Exit(1)
    if qr and creds:
        typer.echo("use --qr OU --creds, nao os dois", err=True)
        raise typer.Exit(1)

    if creds:
        # Perguntado aqui (fora do asyncio) pra o prompt nao brigar com o loop.
        email = typer.prompt("Email")
        password = typer.prompt("Senha", hide_input=True)
        asyncio.run(_login_creds_flow(cfg, account_id, email, password))  # type: ignore[arg-type]
        return

    flow = _login_qr_flow if qr else _login_flow
    asyncio.run(flow(cfg, account_id))  # type: ignore[arg-type]


async def _login_creds_flow(cfg, account_id: AccountId, email: str, password: str):
    """
    Login headless digitando email/senha na pagina do Discord.

    Existe porque nesta maquina o WSLg nao apresenta a janela do Chromium no
    desktop (ela abre vazia), entao o login manual com janela nao eh viavel.
    Headless renderiza normal — so nao da pra ver.

    Trata os dois desvios comuns: 2FA (pede o codigo no terminal) e hCaptcha
    (impossivel resolver headless — aborta e explica).
    """
    acc_cfg = cfg.accounts[account_id]
    shot = Path(f"./logs/login_{account_id}.png")
    shot.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await launch_for_account(pw, acc_cfg, headless=True)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://discord.com/login", wait_until="domcontentloaded")

        logger.info(f"conta '{account_id}': enviando credenciais...")
        await page.fill('input[name="email"]', email)
        await page.fill('input[name="password"]', password)
        await page.click('button[type="submit"]')

        ok = await _await_login_result(page, account_id, shot)
        if ok:
            await asyncio.sleep(3)  # deixa o Discord gravar os tokens no perfil
            logger.info("sessao salva em " + str(acc_cfg.profile_dir))
        try:
            await context.close()
        except Exception:
            pass


async def _await_login_result(page, account_id: str, shot: Path, timeout_seconds: float = 90.0) -> bool:
    """
    Espera o desfecho do submit. Retorna True se logou.

    Sai por um destes: saiu da /login (sucesso), apareceu campo de 2FA,
    apareceu hCaptcha, ou deu timeout.
    """
    deadline = time.time() + timeout_seconds
    twofa = 'input[autocomplete="one-time-code"], input[name="code"]'

    while time.time() < deadline:
        await asyncio.sleep(2)

        if "/login" not in page.url:
            logger.info("login aceito!")
            return True

        if await page.locator('iframe[src*="hcaptcha"]').count() > 0:
            await page.screenshot(path=str(shot))
            logger.error(
                "Discord pediu CAPTCHA — nao da pra resolver sem janela."
                f" Print em {shot.resolve()}."
            )
            return False

        if await page.locator(twofa).count() > 0:
            code = typer.prompt("Codigo 2FA")
            await page.fill(twofa, code.strip())
            await page.keyboard.press("Enter")
            await asyncio.sleep(5)
            if "/login" not in page.url:
                logger.info("login aceito (2FA)!")
                return True

        errors = page.locator('[class*="errorMessage"], [role="alert"]')
        if await errors.count() > 0:
            msg = (await errors.first.inner_text()).strip()
            if msg:
                await page.screenshot(path=str(shot))
                logger.error(f"Discord recusou: {msg}")
                return False

    await page.screenshot(path=str(shot))
    logger.error(f"timeout no login — print em {shot.resolve()}")
    return False


async def _login_qr_flow(cfg, account_id: AccountId, timeout_seconds: float = 600.0):
    """
    Login sem janela: roda headless, salva print da tela de login (que contem
    o QR code) e fica atualizando o PNG conforme o Discord regenera o codigo.

    O QR do Discord expira a cada ~2min; por isso o print eh refeito em loop
    ate a sessao ser aceita. O fim eh detectado pela saida da URL /login.
    """
    acc_cfg = cfg.accounts[account_id]
    shot = Path(f"./logs/qr_{account_id}.png")
    shot.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await launch_for_account(pw, acc_cfg, headless=True)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://discord.com/login", wait_until="domcontentloaded")
        await asyncio.sleep(3)  # deixa o QR renderizar

        logger.info(f"QR salvo em {shot.resolve()} — escaneie com o app do Discord")
        logger.info("(Discord mobile > Configuracoes > Escanear codigo QR)")

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                await page.screenshot(path=str(shot))
            except Exception as e:  # pagina navegou no meio do print
                logger.debug(f"screenshot falhou: {e}")
            if "/login" not in page.url:
                logger.info("login aceito!")
                break
            await asyncio.sleep(5)
        else:
            logger.warning("timeout esperando o login — nada foi salvo")

        await asyncio.sleep(3)  # deixa o Discord gravar os tokens no perfil
        try:
            await context.close()
        except Exception:
            pass
        logger.info("sessao salva em " + str(acc_cfg.profile_dir))


async def _login_flow(cfg, account_id: AccountId):
    """
    Abre o navegador na tela de login do Discord e espera o usuario fechar
    a janela.

    NAO usa Account.start() de proposito: aquele fluxo chama goto_channel(),
    que espera a caixa de mensagem do canal aparecer — coisa que so existe
    quando ja ha sessao. Num perfil novo o Discord mostra a tela de login,
    sem textbox, e o wait estourava em timeout.
    """
    acc_cfg = cfg.accounts[account_id]
    async with async_playwright() as pw:
        context = await launch_for_account(pw, acc_cfg)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://discord.com/login", wait_until="domcontentloaded")
        logger.info(
            f"conta '{account_id}' aberta. Faca login no Discord, depois feche a janela."
        )
        # Espera o usuario fechar a janela. Quando o ultimo page fecha, o
        # contexto persistente cai junto e .pages levanta/retorna vazio.
        try:
            while context.pages:
                await asyncio.sleep(2)
        except Exception:
            pass
        try:
            await context.close()
        except Exception:
            pass
        logger.info("sessao salva em " + str(acc_cfg.profile_dir))


@app.command()
def status():
    """Mostra cooldown atual de cada conta (lendo state.json)."""
    cfg = load_config()
    if not STATE_FILE.exists():
        typer.echo("state.json nao existe ainda — rode 'karuta start' pelo menos uma vez.")
        raise typer.Exit(0)
    state = json.loads(STATE_FILE.read_text())
    last = state.get("last_pick_at", {})
    now = time.time()
    table = Table(title="karuta status")
    table.add_column("conta")
    table.add_column("ultimo pick")
    table.add_column("cooldown restante")
    for aid in cfg.rotation_order:
        last_at = last.get(aid, 0)
        if last_at:
            ago = f"{now - last_at:.0f}s atras"
            remaining = max(0.0, cfg.pick_cooldown_seconds - (now - last_at))
            rem = f"{remaining:.0f}s"
        else:
            ago = "—"
            rem = "0s"
        table.add_row(aid, ago, rem)
    table.add_row("proximo dropper", "", cfg.rotation_order[state.get("next_dropper_idx", 0)])
    console.print(table)


@app.command("banco")
def banco_cmd(
    acao: Annotated[str, typer.Argument(help="init | resumo | reclassificar | job | desejadas | burn | inventario | bits")],
    conta: Annotated[str, typer.Option(help="Filtra por conta (a, b ou c).")] = "",
    limite: Annotated[int, typer.Option(help="Maximo de linhas.")] = 30,
):
    """Cria o esquema e consulta as tabelas de cartas."""
    from . import db

    cfg = load_config()
    if not cfg.banco.ativo:
        typer.echo("banco desativado no config.yaml (banco.ativo: false)", err=True)
        raise typer.Exit(1)

    try:
        conn = db.conectar(cfg.banco.dsn)
    except Exception as e:
        typer.echo(f"nao consegui conectar: {e}", err=True)
        raise typer.Exit(1) from None

    with conn:
        if acao == "init":
            db.criar_esquema(conn)
            db.garantir_contas(conn, list(cfg.accounts))
            typer.echo("esquema criado/atualizado e contas cadastradas")
            return

        if acao == "resumo":
            linhas = db.resumo(conn)
            tabela = Table(title="resumo por conta")
            for col in ("conta", "cartas", "avaliadas", "pendentes", "job",
                        "desejadas", "burn", "melhor esforco", "melhor base"):
                tabela.add_column(col)
            for r in linhas:
                tabela.add_row(
                    r["conta_id"], str(r["total_cartas"]), str(r["avaliadas"]),
                    str(r["pendentes"]), str(r["boas_job"]), str(r["desejadas"]),
                    str(r["burn_pendente"]), str(r["melhor_esforco"] or "—"),
                    str(r["melhor_valor_base"] or "—"),
                )
            console.print(tabela)
            return

        if acao == "reclassificar":
            # Os limiares mudam conforme o jogo revela o que eh bom; sem isto
            # as cartas antigas ficariam presas na classificacao velha.
            from .avaliador import Avaliacao, RegrasClassificacao, classificar

            cfg_av = cfg.avaliacao
            regras = RegrasClassificacao(
                esforco_min_job=cfg_av.esforco_min_job,
                valor_base_min_job=cfg_av.valor_base_min_job,
                desejos_min=cfg_av.desejos_min,
                ouro_max_burn=cfg_av.ouro_max_burn,
                valor_base_max_burn=cfg_av.valor_base_max_burn,
                esforco_max_burn=cfg_av.esforco_max_burn,
            )
            db.limpar_classificacoes(conn)
            contagem = {"job": 0, "desejada": 0, "burn": 0, "so mestre": 0}
            for r in db.cartas_avaliadas(conn):
                av = Avaliacao(
                    codigo=r["codigo"], conta_id=r["conta_id"],
                    valor_base=r["valor_base"], esforco_total=r["esforco_total"],
                    desejos=r["desejos"], ouro_burn=r["ouro_burn"],
                )
                grupos = classificar(av, regras)
                if grupos:
                    db.classificar(
                        conn, r["codigo"], r["conta_id"], grupos,
                        valor_base=r["valor_base"] or 0,
                        esforco_total=r["esforco_total"] or 0,
                        desejos=r["desejos"] or 0,
                        personagem=r["personagem"],
                        serie=r["serie"],
                    )
                    for g in grupos:
                        contagem[g] += 1
                else:
                    contagem["so mestre"] += 1
            for k, v in contagem.items():
                typer.echo(f"  {k}: {v}")
            return

        if acao in ("inventario", "bits"):
            tipo = "bit" if acao == "bits" else "item"
            linhas = [
                r for r in db.listar_inventario(conn, conta_id=conta or None)
                if r["tipo"] == tipo
            ]
            tabela = Table(title=f"{acao} ({len(linhas)} linhas)")
            for col in ("conta", "recurso", "quantidade", "atualizado"):
                tabela.add_column(col)
            for r in linhas:
                tabela.add_row(
                    r["conta_id"], r["nome"] or r["chave"], f"{r['quantidade']:,}",
                    r["atualizado_em"].strftime("%d/%m %H:%M"),
                )
            console.print(tabela)
            return

        mapa = {
            "job": ("cartas_job", ["esforco_total", "valor_base"]),
            "desejadas": ("cartas_desejadas", ["desejos"]),
            "burn": ("cartas_burn", ["valor_base"]),
        }
        if acao not in mapa:
            typer.echo(f"acao invalida: {acao}", err=True)
            raise typer.Exit(1)

        nome, metricas = mapa[acao]
        linhas = db.listar(conn, nome, limite=limite, conta_id=conta or None)
        tabela = Table(title=f"{nome} ({len(linhas)} linhas)")
        for col in ["conta", "codigo", "personagem", "serie", *metricas, "★", "print"]:
            tabela.add_column(col)
        for r in linhas:
            tabela.add_row(
                r["conta_id"], r["codigo"], (r["personagem"] or "—")[:24],
                (r["serie"] or "—")[:28],
                *[str(r[m]) for m in metricas],
                "★" * (r["estrelas"] or 0) or "—",
                f"#{r['numero_print']}" if r["numero_print"] else "—",
            )
        console.print(tabela)


@app.command()
def scan(
    base_min: Annotated[int, typer.Option(help="Match se Base value >= este valor.")] = 40,
    effort_min: Annotated[
        int,
        typer.Option(
            help="Match se Effort total >= este valor (OR com base_min)."
            " Effort eh o que importa pro worker: 90+ eh bom.",
        ),
    ] = 90,
    accounts: Annotated[
        str,
        typer.Option(help="IDs separados por virgula (ex: a,b,c). Default: todas do config."),
    ] = "",
    output: Annotated[
        str, typer.Option(help="Arquivo markdown de saida na raiz do projeto.")
    ] = "cards_base_above.md",
    command_gap: Annotated[
        float,
        typer.Option(
            help="Intervalo minimo entre comandos ao Karuta (segundos)."
            " Cooldown do k!wi eh ~10s; 11s evita os retries de cooldown.",
        ),
    ] = 11.0,
    max_cards: Annotated[
        int, typer.Option(help="Limite de cartas por conta. 0 = sem limite."),
    ] = 0,
    max_pages: Annotated[
        int,
        typer.Option(
            help="Limite de paginas de k!c por conta. 0 = sem limite."
            " Use ex: --max-pages 2 pra testar transicao de pagina.",
        ),
    ] = 0,
    prefilter: Annotated[
        bool,
        typer.Option(
            "--prefilter",
            help="Ativa pre-filtro heuristico (stars/print/edition). Default: OFF, escaneia tudo.",
        ),
    ] = False,
    min_stars: Annotated[
        int, typer.Option(help="Prefilter: mantem cartas com ★ >= este valor."),
    ] = 3,
    min_edition: Annotated[
        int, typer.Option(help="Prefilter: mantem cartas com edicao ◈ >= este valor."),
    ] = 4,
    max_print: Annotated[
        int, typer.Option(help="Prefilter: mantem cartas com print# <= este valor."),
    ] = 5000,
    log_level: Annotated[str, typer.Option(help="DEBUG/INFO/WARNING/ERROR")] = "INFO",
):
    """
    Escaneia a colecao das contas e gera um markdown com as cartas cujo
    Base value >= base_min. Roda em sequencia (uma conta por vez) para nao
    competir com o bot principal de drop.

    AVISO: envia muitos comandos (k!c + k!wi). Risco de detecao. Use em
    canal/conta separado se possivel.
    """
    cfg = _bootstrap(log_level)
    ids = _resolve_accounts(cfg, accounts)
    prefilter_rule = (
        PrefilterRule(min_stars=min_stars, min_edition=min_edition, max_print=max_print)
        if prefilter
        else None
    )
    asyncio.run(
        _scan_flow(
            cfg,
            ids,
            base_min=base_min,
            effort_min=effort_min,
            output=output,
            command_gap=command_gap,
            max_cards=max_cards or None,
            max_pages=max_pages or None,
            prefilter=prefilter_rule,
        )
    )


def _resolve_accounts(cfg, accounts_csv: str) -> list[AccountId]:
    if not accounts_csv.strip():
        return list(cfg.rotation_order)
    ids = [a.strip() for a in accounts_csv.split(",") if a.strip()]
    for aid in ids:
        if aid not in cfg.accounts:
            typer.echo(f"conta '{aid}' nao existe no config", err=True)
            raise typer.Exit(1)
    return ids  # type: ignore[return-value]


async def _scan_flow(
    cfg,
    account_ids: list[AccountId],
    *,
    base_min: int,
    effort_min: int,
    output: str,
    command_gap: float,
    max_cards: int | None,
    max_pages: int | None,
    prefilter: PrefilterRule | None,
):
    """
    Executa o scan sequencial: conta A inteira (pagina por pagina, fazendo
    k!wi de cada carta) → conta B → conta C.

    Escreve o markdown incrementalmente: a cada match novo o arquivo eh
    re-gerado, entao se o scan for interrompido a metade, o que ja foi
    encontrado permanece em disco.
    """
    out_path = Path(output)
    results_by_account: dict[str, list[CardScan]] = {aid: [] for aid in account_ids}

    def write_md() -> None:
        md = render_markdown(results_by_account, base_min=base_min, effort_min=effort_min)
        out_path.write_text(md, encoding="utf-8")

    def on_match(aid: str, scan: CardScan) -> None:
        results_by_account.setdefault(aid, []).append(scan)
        write_md()

    write_md()  # estado inicial vazio

    async with async_playwright() as pw:
        for aid in account_ids:
            acc_cfg = cfg.accounts[aid]
            button = cfg.pick_button_index[aid]
            account = Account(id=aid, cfg=acc_cfg, button_index=button)
            await account.start(pw, cfg)
            try:
                # scan_account ja popula via on_match conforme acha matches.
                # O retorno aqui eh redundante mas mantemos pra log final.
                await scan_account(
                    account,
                    base_min=base_min,
                    effort_min=effort_min,
                    command_gap_seconds=command_gap,
                    max_cards=max_cards,
                    max_pages=max_pages,
                    prefilter=prefilter,
                    on_match=on_match,
                )
            finally:
                await account.stop()
            write_md()
            logger.info(f"scan: conta {aid} finalizada, markdown atualizado")

    write_md()
    logger.info(f"scan: salvo em {out_path.resolve()}")


if __name__ == "__main__":
    app()
