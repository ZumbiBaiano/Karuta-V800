"""
Scanner: itera a colecao de uma conta via k!c, extrai codes,
e avalia o Base value de cada carta via k!workerinfo.

Uso tipico: filtrar workers com base value >= threshold pra montar
um time bom de worker. Veja `karuta scan --help`.

Roda separado do bot principal de drop. As mensagens sao enviadas
no mesmo canal configurado em config.yaml.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from loguru import logger
from playwright.async_api import Page

from .account import Account
from .discord_page import (
    click_next_page_reaction,
    find_karuta_message_id,
    list_message_reactions,
    send_message,
    wait_for_message_text_change,
)

# Karuta tem cooldown de ~5s entre comandos, mas em rajada de k!wi observamos
# rejeicoes ate com 6s de gap. 7s elimina a maioria dos retries.
DEFAULT_COMMAND_GAP_SECONDS = 7.0


class CommandRateLimiter:
    """Garante delay minimo entre comandos enviados ao Karuta."""

    def __init__(self, min_gap_seconds: float = DEFAULT_COMMAND_GAP_SECONDS):
        self.min_gap = min_gap_seconds
        self.last_at = 0.0

    async def wait(self) -> None:
        if self.last_at == 0.0:
            self.last_at = time.time()
            return
        elapsed = time.time() - self.last_at
        if elapsed < self.min_gap:
            await asyncio.sleep(self.min_gap - elapsed)
        self.last_at = time.time()


async def _paced_send(page: Page, text: str, limiter: CommandRateLimiter) -> None:
    await limiter.wait()
    await send_message(page, text)


# Markers que indicam que a mensagem eh uma RESPOSTA do Karuta (nao cooldown).
_KARUTA_OK_MARKERS = (
    "Worker Details",
    "Card Collection",
    "Cards owned by",
    "Base value",
)


async def _classify_new_messages(
    page: Page, pre_count: int,
) -> tuple[str, int | None]:
    """
    Olha apenas as mensagens adicionadas apos `pre_count` e classifica a
    resposta do Karuta.

    Retorna:
      ('cooldown', segundos) se achou mensagem de cooldown
      ('ok', None)           se achou mensagem de resposta valida
      ('none', None)         se nao achou nada relevante (sem resposta ainda)
    """
    try:
        messages = page.locator('li[id^="chat-messages-"]')
        new_count = await messages.count()
        for i in range(pre_count, new_count):
            text = (await messages.nth(i).inner_text()) or ""
            m = COOLDOWN_RE.search(text)
            if m:
                return ("cooldown", int(m.group(1)))
            if any(marker in text for marker in _KARUTA_OK_MARKERS):
                return ("ok", None)
    except Exception:
        pass
    return ("none", None)


async def _send_with_cooldown_retry(
    page: Page,
    text: str,
    limiter: CommandRateLimiter,
    settle_seconds: float,
    max_retries: int = 3,
) -> bool:
    """
    Envia comando e checa as mensagens NOVAS (apos o send) para detectar
    cooldown ou resposta valida. Se cooldown, espera e re-envia.
    Retorna False se falhar apos max_retries.
    """
    for attempt in range(max_retries + 1):
        messages = page.locator('li[id^="chat-messages-"]')
        pre_count = await messages.count()

        await _paced_send(page, text, limiter)
        await asyncio.sleep(settle_seconds)

        kind, cd = await _classify_new_messages(page, pre_count)
        if kind == "ok":
            return True
        if kind == "cooldown":
            wait = (cd or 0) + 1.5
            logger.warning(
                f"scanner: cooldown {cd}s detectado para '{text}', aguardando {wait:.1f}s"
            )
            await asyncio.sleep(wait)
            continue
        # 'none' = sem resposta visivel ainda; espera um pouco e tenta de novo
        logger.debug(f"scanner: sem resposta nova para '{text}', tentando novamente")
        await asyncio.sleep(2.0)
    logger.error(f"scanner: cooldown insistente em '{text}' apos {max_retries} retries")
    return False

# Codes do Karuta sao 7 chars lowercase alfanumericos com pelo menos 1 digito.
CODE_RE = re.compile(r"\b([a-z][a-z0-9]{6}|[a-z0-9]*\d[a-z0-9]*)\b")
# Mais simples: 7 chars alphanum
CARD_CODE_RE = re.compile(r"\b([a-z0-9]{7})\b")

# Apos o code, busca-se #<print> (obrigatorio) e ◈<edition> (opcional)
# dentro de uma janela de N chars. Stars sao contadas no intervalo entre
# code e #print.
PRINT_RE = re.compile(r"#(\d+)")
EDITION_RE = re.compile(r"◈\s*(\d+)")

# Palavras de 7 chars que nao sao codes (pra excluir falsos positivos do CARD_CODE_RE).
_NOT_CODES = {
    "karuta", "showing", "healthy", "injured", "current", "collect",
    "minutes", "seconds", "watcher", "tonight", "details", "warning",
    "loading", "perfect", "manager",
}


def _looks_like_code(s: str) -> bool:
    if s in _NOT_CODES:
        return False
    has_digit = any(c.isdigit() for c in s)
    has_alpha = any(c.isalpha() for c in s)
    return has_digit and has_alpha

# Rodape: "Showing cards 1–10 of 1,000 (1,332 total)"
COLLECTION_TOTAL_RE = re.compile(
    r"Showing cards\s+\d+[–\-]\d+\s+of\s+([\d,]+)",
    re.IGNORECASE,
)

# Cooldown do Karuta: "@user, you cannot use that command for another N seconds."
COOLDOWN_RE = re.compile(
    r"cannot use that command for another\s+(\d+)\s+seconds?",
    re.IGNORECASE,
)

BASE_VALUE_RE = re.compile(r"(\d+)\s+Base value", re.IGNORECASE)
EFFORT_TOTAL_RE = re.compile(r"Effort\s*[·:]\s*(\d+)", re.IGNORECASE)
CHARACTER_NAME_RE = re.compile(r"Character\s*[·:]\s*([^\n(]+?)\s*\(([a-z0-9]{7})\)", re.IGNORECASE)


@dataclass
class CollectionEntry:
    """Info extraida da linha do k!c (sem precisar de k!wi)."""

    code: str
    stars: int = 0       # 1-4 (numero de ★ na linha) — 0 se nao foi possivel ler
    print_num: int = 0   # ex: 1368 — 0 se nao foi possivel ler
    edition: int = 0     # ex: 7 — 0 se nao foi possivel ler


def parse_collection_entries(text: str, window: int = 200) -> list[CollectionEntry]:
    """
    Parser tolerante: ancora no code (7 chars alfanum), depois busca
    forward por #<print> e ◈<edition> dentro de uma janela.

    Ignora codes que nao sejam seguidos por algo parecido com info de carta
    (#print no proximo trecho). Isso filtra falsos positivos.
    """
    entries: list[CollectionEntry] = []
    seen: set[str] = set()

    for m in CARD_CODE_RE.finditer(text):
        code = m.group(1)
        if code in seen:
            continue
        if not _looks_like_code(code):
            continue

        chunk = text[m.end() : m.end() + window]
        print_m = PRINT_RE.search(chunk)
        if not print_m:
            # Nao tem #<num> proximo, provavelmente nao eh linha de carta.
            continue

        edition_m = EDITION_RE.search(chunk)
        edition = int(edition_m.group(1)) if edition_m else 0

        # Stars sao todos os ★ entre o code e o #<print>
        stars = chunk[: print_m.start()].count("★")

        entries.append(
            CollectionEntry(
                code=code,
                stars=stars,
                print_num=int(print_m.group(1)),
                edition=edition,
            )
        )
        seen.add(code)

    return entries


@dataclass
class PrefilterRule:
    """
    Heuristica pra pular cartas que com altissima probabilidade tem Base baixo.
    Uma carta passa se cumprir QUALQUER um dos criterios (OR).
    """

    min_stars: int = 3          # ★★★ ou mais = qualidade alta
    min_edition: int = 4        # ◈4+ = serie mais antiga (geralmente base maior)
    max_print: int = 5000       # print baixo = carta rara

    def keeps(self, e: CollectionEntry) -> bool:
        return (
            e.stars >= self.min_stars
            or e.edition >= self.min_edition
            or e.print_num <= self.max_print
        )


@dataclass
class CardScan:
    code: str
    base_value: int
    effort_total: int = 0
    character: str = ""
    stars: int = 0
    print_num: int = 0
    edition: int = 0
    raw: str = field(default="", repr=False)


async def _latest_karuta_text(page: Page, contains_any: list[str], lookback: int = 12) -> str:
    """Le inner_text das ultimas N msgs e retorna a 1a que casa com algum contains."""
    try:
        messages = page.locator('li[id^="chat-messages-"]')
        count = await messages.count()
        for i in range(count - 1, max(-1, count - 1 - lookback), -1):
            text = (await messages.nth(i).inner_text()) or ""
            if any(k.lower() in text.lower() for k in contains_any):
                return text
    except Exception as e:
        logger.debug(f"erro lendo mensagens: {e}")
    return ""


async def fetch_collection_entries(
    account: Account,
    limiter: CommandRateLimiter,
    max_pages: int = 200,
    settle_seconds: float = 2.5,
) -> list[CollectionEntry]:
    """
    Envia k!c paginando ate esgotar e retorna a lista de entries
    (code + stars + print + edition extraidos da propria linha).
    """
    assert account.page is not None
    entries: list[CollectionEntry] = []
    seen: set[str] = set()
    total_pages: int | None = None
    total_cards: int | None = None

    for page_num in range(1, max_pages + 1):
        cmd = "k!c" if page_num == 1 else f"k!c page:{page_num}"
        account.log.info(f"scanner: {cmd}")
        ok = await _send_with_cooldown_retry(
            account.page, cmd, limiter, settle_seconds=settle_seconds,
        )
        if not ok:
            account.log.error(f"scanner: nao consegui enviar {cmd}")
            break

        text = await _latest_karuta_text(
            account.page,
            contains_any=["Card Collection", "Cards owned by", "Showing cards"],
        )

        if not text:
            account.log.warning(f"scanner: sem resposta visivel na pagina {page_num}")
            break

        if total_cards is None:
            m = COLLECTION_TOTAL_RE.search(text)
            if m:
                total_cards = int(m.group(1).replace(",", ""))
                total_pages = (total_cards + 9) // 10
                account.log.info(
                    f"scanner: collection tem {total_cards} cards (~{total_pages} paginas)"
                )

        page_entries = [CollectionEntry.from_match(m) for m in CARD_LINE_FULL_RE.finditer(text)]
        # Fallback: se o regex completo nao casou (formato mudou), pega so codes.
        if not page_entries:
            for code in CARD_LINE_RE.findall(text):
                page_entries.append(CollectionEntry(code=code, stars=0, print_num=0, edition=0))

        new_entries = [e for e in page_entries if e.code not in seen]
        for e in new_entries:
            seen.add(e.code)
            entries.append(e)

        account.log.info(
            f"scanner: pagina {page_num} -> {len(page_entries)} entries ({len(new_entries)} novos)"
        )

        if not new_entries:
            account.log.info("scanner: sem codes novos, encerrando paginacao")
            break

        if total_pages is not None and page_num >= total_pages:
            break

    return entries


async def fetch_worker_info(
    account: Account,
    code: str,
    limiter: CommandRateLimiter,
    settle_seconds: float = 2.5,
) -> CardScan | None:
    """Envia k!wi <code> e parseia base value + effort total + nome."""
    assert account.page is not None
    ok = await _send_with_cooldown_retry(
        account.page, f"k!wi {code}", limiter, settle_seconds=settle_seconds,
    )
    if not ok:
        return None
    text = await _latest_karuta_text(
        account.page,
        contains_any=["Worker Details", "Base value", "Effort"],
    )
    if not text:
        return None

    m_base = BASE_VALUE_RE.search(text)
    if not m_base:
        return None

    base = int(m_base.group(1))
    effort = 0
    if (m_eff := EFFORT_TOTAL_RE.search(text)) is not None:
        effort = int(m_eff.group(1))

    name = ""
    if (m_char := CHARACTER_NAME_RE.search(text)) is not None:
        if m_char.group(2) == code:
            name = m_char.group(1).strip()

    return CardScan(code=code, base_value=base, effort_total=effort, character=name, raw=text)


SHOWING_RANGE_RE = re.compile(
    r"Showing cards\s+(\d+)\s*[–\-]\s*(\d+)\s+of\s+([\d,]+)",
    re.IGNORECASE,
)


async def _read_collection_message(
    account: Account, message_id: str,
) -> tuple[str, list[CollectionEntry], tuple[int, int, int] | None]:
    """
    Le o texto atual da mensagem da collection (que pode ter sido editada
    apos clicar a reaction de proxima pagina) e retorna:
      (texto_bruto, entries_parseadas, (range_start, range_end, total))
    """
    assert account.page is not None
    msg = account.page.locator(f'li[id$="-{message_id}"]')
    try:
        text = (await msg.inner_text()) or ""
    except Exception:
        text = ""

    entries = parse_collection_entries(text)
    range_info: tuple[int, int, int] | None = None
    if (m := SHOWING_RANGE_RE.search(text)) is not None:
        range_info = (
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3).replace(",", "")),
        )
    return text, entries, range_info


async def _dump_text(account: Account, label: str, text: str) -> None:
    """Salva texto bruto pra inspecao (debug)."""
    try:
        from pathlib import Path
        dump_path = Path("./logs") / f"scanner_dump_{account.id}_{label}_{int(time.time())}.txt"
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(text, encoding="utf-8")
        account.log.warning(f"scanner: texto bruto salvo em {dump_path}")
    except Exception:
        pass


async def scan_account(
    account: Account,
    base_min: int = 40,
    command_gap_seconds: float = DEFAULT_COMMAND_GAP_SECONDS,
    max_cards: int | None = None,
    max_pages: int | None = None,
    prefilter: PrefilterRule | None = None,
    on_match: "callable | None" = None,
) -> list[CardScan]:
    """
    Estrategia REACTION-BASED:
      1) Envia k!c UMA VEZ. Karuta responde com embed paginado.
      2) Localiza a mensagem por ID.
      3) Para cada pagina:
         - parseia entries do conteudo atual da mensagem
         - faz k!wi de cada nova entry
         - clica na reaction ▶ pra ir pra proxima pagina
         - aguarda o texto da mensagem mudar (Karuta edita o embed)
      4) Para quando atingir a ultima pagina (range_end == total_cards)
         ou max_cards/max_pages.
    """
    t0 = time.time()
    assert account.page is not None
    limiter = CommandRateLimiter(min_gap_seconds=command_gap_seconds)

    results: list[CardScan] = []
    seen: set[str] = set()
    cards_evaluated = 0
    cards_skipped_prefilter = 0

    HARD_PAGE_CAP = 200
    user_max_pages = max_pages if (max_pages and max_pages > 0) else HARD_PAGE_CAP

    # 1) Envia k!c inicial
    account.log.info("scanner: k!c (envio inicial)")
    ok = await _send_with_cooldown_retry(
        account.page, "k!c", limiter, settle_seconds=2.5,
    )
    if not ok:
        account.log.error("scanner: falha no k!c inicial")
        return results

    # 2) Localiza mensagem do embed (espera reactions renderizarem)
    await asyncio.sleep(1.5)
    msg_id = await find_karuta_message_id(account.page, "Card Collection")
    if not msg_id:
        account.log.error("scanner: nao achei mensagem do Card Collection")
        return results
    account.log.info(f"scanner: collection msg id = {msg_id}")

    # 3) Loop de paginas
    for page_num in range(1, user_max_pages + 1):
        text, entries, range_info = await _read_collection_message(account, msg_id)

        if range_info is not None:
            start, end, total = range_info
            account.log.info(
                f"scanner: pagina {page_num} (cards {start}-{end} de {total})"
            )
        else:
            account.log.info(f"scanner: pagina {page_num}")

        if not entries:
            account.log.warning("scanner: 0 entries parseadas")
            await _dump_text(account, f"page{page_num}_no_entries", text)
            break

        new_entries = [e for e in entries if e.code not in seen]
        for e in new_entries:
            seen.add(e.code)

        account.log.info(
            f"scanner: {len(new_entries)} cards novos nessa pagina, iniciando k!wi..."
        )

        for entry in new_entries:
            if max_cards is not None and cards_evaluated >= max_cards:
                account.log.info(f"scanner: atingiu max_cards={max_cards}")
                break

            if prefilter is not None and not prefilter.keeps(entry):
                cards_skipped_prefilter += 1
                continue

            info = await fetch_worker_info(account, entry.code, limiter=limiter)
            cards_evaluated += 1
            if info is None:
                account.log.warning(f"scanner: nao consegui ler {entry.code}")
                continue

            info.stars = entry.stars
            info.print_num = entry.print_num
            info.edition = entry.edition

            if info.base_value >= base_min:
                results.append(info)
                account.log.info(
                    f"scanner: ✓ {entry.code} base={info.base_value} "
                    f"effort={info.effort_total} ★{entry.stars} #{entry.print_num} "
                    f"◈{entry.edition} {info.character}"
                )
                if on_match is not None:
                    on_match(account.id, info)
            else:
                account.log.debug(
                    f"scanner: skip {entry.code} base={info.base_value}"
                )

        if max_cards is not None and cards_evaluated >= max_cards:
            break

        # Verifica se essa eh a ultima pagina
        if range_info is not None:
            _, end, total = range_info
            if end >= total:
                account.log.info(f"scanner: ultima pagina (cards ate {end} de {total})")
                break

        # Vai pra proxima pagina via reaction
        account.log.info("scanner: clicando reaction ▶ pra proxima pagina")
        clicked = await click_next_page_reaction(account.page, msg_id)
        if not clicked:
            labels = await list_message_reactions(account.page, msg_id)
            account.log.warning(
                f"scanner: nao consegui clicar 'next'. Reactions disponiveis: {labels}"
            )
            await _dump_text(account, f"no_next_btn_page{page_num}", text)
            break

        # Espera o conteudo mudar (Karuta edita o embed)
        marker = f"cards {range_info[0]}–{range_info[1]}" if range_info else "Showing"
        changed = await wait_for_message_text_change(
            account.page, msg_id, previous_text_marker=marker, timeout_seconds=8.0,
        )
        if not changed:
            account.log.warning("scanner: mensagem nao atualizou apos click, encerrando")
            break
        await asyncio.sleep(0.5)  # pequeno buffer pos-update

    extra = (
        f", {cards_skipped_prefilter} prefilter-skipped"
        if prefilter is not None and cards_skipped_prefilter
        else ""
    )
    account.log.info(
        f"scanner: conta {account.id} -> {len(results)} matches "
        f"(base >= {base_min}) de {cards_evaluated} avaliadas{extra} "
        f"em {time.time() - t0:.1f}s"
    )
    return results


def render_markdown(
    results_by_account: dict[str, list[CardScan]],
    base_min: int,
) -> str:
    lines = [
        f"# Cartas com Base Value >= {base_min}",
        "",
        f"_Gerado por `karuta scan` em {time.strftime('%Y-%m-%d %H:%M:%S')}_",
        "",
    ]
    total = sum(len(r) for r in results_by_account.values())
    lines.append(f"**Total: {total} cartas** acima de base {base_min} nas {len(results_by_account)} contas.")
    lines.append("")

    for aid, results in results_by_account.items():
        lines.append(f"## Conta {aid.upper()} — {len(results)} cartas")
        lines.append("")
        if not results:
            lines.append("_Nenhuma carta encontrada acima do threshold._")
            lines.append("")
            continue
        lines.append("| Code | Base | Effort | ★ | Print | ◈ Ed | Personagem |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in sorted(results, key=lambda x: (-x.base_value, -x.effort_total)):
            char = r.character or "—"
            stars = "★" * r.stars + "☆" * (4 - r.stars) if r.stars else "—"
            pnum = f"#{r.print_num}" if r.print_num else "—"
            edn = f"◈{r.edition}" if r.edition else "—"
            lines.append(
                f"| `{r.code}` | {r.base_value} | {r.effort_total} | {stars} | {pnum} | {edn} | {char} |"
            )
        lines.append("")

    return "\n".join(lines)
