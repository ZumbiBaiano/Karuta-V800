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


@app.command()
def login(
    account_id: Annotated[str, typer.Argument(help="a, b ou c")],
    log_level: Annotated[str, typer.Option(help="DEBUG/INFO/WARNING/ERROR")] = "INFO",
):
    """Abre apenas o navegador da conta indicada para login manual."""
    cfg = _bootstrap(log_level)
    if account_id not in cfg.accounts:
        typer.echo(f"conta '{account_id}' nao existe no config", err=True)
        raise typer.Exit(1)

    asyncio.run(_login_flow(cfg, account_id))  # type: ignore[arg-type]


async def _login_flow(cfg, account_id: AccountId):
    acc_cfg = cfg.accounts[account_id]
    button = cfg.pick_button_index[account_id]
    account = Account(id=account_id, cfg=acc_cfg, button_index=button)
    async with async_playwright() as pw:
        await account.start(pw, cfg)
        logger.info(
            f"conta '{account_id}' aberta. Faca login no Discord, depois feche a janela."
        )
        # Espera o usuario fechar a janela (context fica fechado).
        try:
            while account.context and len(account.context.pages) > 0:
                await asyncio.sleep(2)
        except Exception:
            pass
        await account.stop()
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
