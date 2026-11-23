"""
Scanner: itera a colecao de uma conta via k!c, extrai codes,
e avalia o Base value de cada carta via k!workerinfo.

Uso tipico: filtrar workers com base value >= threshold pra montar
um time bom de worker. Veja `karuta scan --help`.

Roda separado do bot principal de drop. As mensagens sao enviadas no canal de
info (`info_channel_id` no config.yaml), o mesmo dos demais comandos de
consulta; sem esse canal configurado, caem no canal de drop.
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
    dump_message_html,
    find_karuta_message_id,
    is_next_page_disabled,
    list_message_buttons,
    list_message_reactions,
    send_message,
)

# Empiricamente o cooldown do k!wi no Karuta eh ~10s (com 6s de gap, Karuta
# respondia "wait 4s" -> 6+4=10). 11s elimina os cooldown-retries: cada k!wi
# direto, sem espera extra. Acaba sendo MAIS RAPIDO que 6s (que retentava sempre).
DEFAULT_COMMAND_GAP_SECONDS = 11.0


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
#
# IMPORTANTE: todo comando novo precisa ter o cabecalho da resposta dele aqui.
# Sem isso `_classify_after` devolve 'none', o comando eh reenviado 3x e
# desiste — foi o que aconteceu com `k!ci`, cuja resposta comeca com
# "Card Details" e nao casava com nenhum marker.
_KARUTA_OK_MARKERS = (
    "Worker Details",      # k!wi
    "Card Collection",     # k!c
    "Cards owned by",      # k!c
    "Base value",          # k!wi
    "Card Details",        # k!ci
    "Character Lookup",    # k!lu
    "Wishlisted",          # k!lu
    "Dropped on",          # k!ci
    "Burn Card",           # kb
    "Inventory",           # k!i
    "Items carried by",    # k!i
    "Bits carried by",     # k!bi
)


def _snowflake_of(li_id: str | None) -> int:
    """
    Extrai o snowflake (ID time-ordered) do id do <li>.
    Formato: 'chat-messages-<channel>-<snowflake>'.
    """
    if not li_id:
        return 0
    parts = li_id.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return 0


async def _last_snowflake(page: Page) -> int:
    """Retorna o maior snowflake entre as ultimas mensagens renderizadas."""
    messages = page.locator('li[id^="chat-messages-"]')
    best = 0
    try:
        count = await messages.count()
        for i in range(max(0, count - 8), count):
            lid = await messages.nth(i).get_attribute("id")
            best = max(best, _snowflake_of(lid))
    except Exception:
        pass
    return best


async def _classify_after(
    page: Page, after_snowflake: int,
) -> tuple[str, int | None]:
    """
    Olha mensagens com snowflake > after_snowflake e classifica a MAIS RECENTE
    relevante. Robusto a virtualizacao do Discord (nao depende de count).
    """
    last_kind = "none"
    last_cd: int | None = None
    last_sf = 0
    try:
        messages = page.locator('li[id^="chat-messages-"]')
        count = await messages.count()
        for i in range(count):
            lid = await messages.nth(i).get_attribute("id")
            sf = _snowflake_of(lid)
            if sf <= after_snowflake or sf < last_sf:
                continue
            text = (await messages.nth(i).inner_text()) or ""
            m = COOLDOWN_RE.search(text)
            if m:
                last_kind, last_cd, last_sf = "cooldown", int(m.group(1)), sf
            elif any(marker in text for marker in _KARUTA_OK_MARKERS):
                last_kind, last_cd, last_sf = "ok", None, sf
    except Exception:
        pass
    return (last_kind, last_cd)


async def _send_with_cooldown_retry(
    page: Page,
    text: str,
    limiter: CommandRateLimiter,
    settle_seconds: float,
    max_retries: int = 2,
) -> bool:
    """
    Envia comando ate receber resposta valida ou esgotar tentativas.
    Trata cooldown e respostas atrasadas (Karuta as vezes responde tarde
    e a resposta cai 'antes' do pre_count do proximo retry).

    Para evitar isso: capturamos `initial_pre_count` UMA VEZ no comeco e
    classificamos as mensagens contra essa baseline em todos os retries.
    """
    # Baseline por snowflake (robusto a virtualizacao), capturado UMA VEZ.
    baseline_sf = await _last_snowflake(page)

    for attempt in range(max_retries + 1):
        await _paced_send(page, text, limiter)
        await asyncio.sleep(settle_seconds)

        kind, cd = await _classify_after(page, baseline_sf)
        if kind == "ok":
            return True
        if kind == "cooldown":
            wait = (cd or 0) + 3.0
            logger.warning(
                f"scanner: cooldown {cd}s detectado para '{text}', aguardando {wait:.1f}s"
            )
            await asyncio.sleep(wait)
            continue

        # 'none' = sem resposta. Espera mais 5s e re-checa SEM re-enviar.
        logger.debug(f"scanner: sem resposta para '{text}', aguardando +5s")
        await asyncio.sleep(5.0)
        kind, cd = await _classify_after(page, baseline_sf)
        if kind == "ok":
            return True
        if kind == "cooldown":
            wait = (cd or 0) + 3.0
            logger.warning(
                f"scanner: cooldown atrasado {cd}s detectado, aguardando {wait:.1f}s"
            )
            await asyncio.sleep(wait)
            continue
        # Vai pra proxima tentativa de send (se houver)

    logger.warning(
        f"scanner: sem resposta valida pra '{text}' apos {max_retries + 1} tentativas. "
        f"Pausando 20s pra evitar zumbi-mode"
    )
    await asyncio.sleep(20.0)
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
WISHLIST_RE = re.compile(r"♡\s*(\d+)")

# Palavras de 7 chars que nao sao codes (pra excluir falsos positivos do CARD_CODE_RE).
# Codes do Karuta PODEM ser so letras (ex: vfdcdxw) — entao NAO exigimos digito.
# O filtro real eh o #<print> proximo (em parse_collection_entries).
_NOT_CODES = {
    "karuta", "showing", "healthy", "injured", "current", "collect",
    "minutes", "seconds", "watcher", "tonight", "details", "warning",
    "loading", "perfect", "manager", "another", "worker", "effort",
    "wellctd",
}


def _looks_like_code(s: str) -> bool:
    """
    Codes do Karuta sao 7 chars lowercase alfanumericos. Podem ser so letras.
    Excluimos apenas a blocklist; o sinal forte eh o #<print> que segue o code.
    """
    return s not in _NOT_CODES

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
    # Desejos (♡N). So aparece quando o k!c vai ordenado por wishlist; com
    # 'k!c' puro a linha nao traz esse prefixo e o campo fica 0.
    wishlist: int = 0


def parse_collection_entries(text: str, window: int = 90) -> list[CollectionEntry]:
    """
    Parser tolerante: ancora no code (7 chars alfanum), depois busca
    forward por #<print> e ◈<edition> dentro de uma janela curta (~90 chars,
    o #<print> sempre vem logo apos o code: 'code · ★★★★ · #print').

    Ignora codes que nao sejam seguidos por #<print> proximo — isso filtra
    falsos positivos (palavras de 7 letras minusculas em nomes/series).
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

        # Desejos vem ANTES do code ('♡781 · vfxb9gd · ...'), entao olha pra
        # tras numa janela curta. Janela curta de proposito: larga demais
        # pegaria o ♡ da carta anterior da lista.
        antes = text[max(0, m.start() - 24) : m.start()]
        wl_m = WISHLIST_RE.search(antes)

        entries.append(
            CollectionEntry(
                code=code,
                stars=stars,
                print_num=int(print_m.group(1)),
                edition=edition,
                wishlist=int(wl_m.group(1)) if wl_m else 0,
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


async def fetch_worker_info(
    account: Account,
    code: str,
    limiter: CommandRateLimiter,
    settle_seconds: float = 3.5,
) -> CardScan | None:
    """Envia k!wi <code> e parseia base value + effort total + nome."""
    page = await account.canal_info()
    ok = await _send_with_cooldown_retry(
        page, f"k!wi {code}", limiter, settle_seconds=settle_seconds,
    )
    if not ok:
        return None
    text = await _latest_karuta_text(
        page,
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
    page = await account.canal_info()
    msg = page.locator(f'li[id$="-{message_id}"]')
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


async def _wait_page_changed(
    account: Account,
    message_id: str,
    old_range: tuple[int, int, int] | None,
    timeout_seconds: float = 8.0,
) -> tuple[int, int, int] | None:
    """
    Espera o range da collection mudar (Karuta editou o embed apos clicar ▶).
    Retorna o novo range_info, ou None se nao mudou no timeout.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        _, _, new_range = await _read_collection_message(account, message_id)
        if new_range is not None and new_range != old_range:
            return new_range
        await asyncio.sleep(0.5)
    return None


def _dump_collected_codes(account: Account, entries: list[CollectionEntry]) -> None:
    """Salva os codes coletados na fase 1 (insurance se a fase 2 crashar)."""
    try:
        from pathlib import Path
        path = Path("./logs") / f"collected_codes_{account.id}_{int(time.time())}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"{e.code}\t★{e.stars}\t#{e.print_num}\t◈{e.edition}" for e in entries
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        account.log.info(f"scanner: codes coletados salvos em {path}")
    except Exception:
        pass


async def _collect_all_entries(
    account: Account,
    limiter: CommandRateLimiter,
    max_pages: int,
    comando: str = "k!c",
) -> list[CollectionEntry]:
    """
    FASE 1: envia o k!c uma vez e pagina via botao ▶ ate o fim, coletando
    todas as CollectionEntry. NAO faz k!wi aqui — paginacao rapida mantem
    os botoes do embed vivos (eles expiram apos minutos de inatividade).

    `comando` permite pedir a colecao ordenada (ex: "k!c sort by wishlist
    descending"), que e como o `karuta transferir --da-colecao` acha as
    melhores cartas sem precisar avaliar as 2000+ uma a uma.
    """
    page = await account.canal_info()

    account.log.info(f"scanner: [fase 1] {comando}")
    ok = await _send_with_cooldown_retry(
        page, comando, limiter, settle_seconds=2.5,
    )
    if not ok:
        account.log.error(f"scanner: [fase 1] falha no '{comando}' inicial")
        return []

    await asyncio.sleep(1.5)
    msg_id = await find_karuta_message_id(page, "Card Collection")
    if not msg_id:
        account.log.error("scanner: [fase 1] nao achei mensagem do Card Collection")
        return []
    account.log.info(f"scanner: [fase 1] collection msg id = {msg_id}")

    all_entries: list[CollectionEntry] = []
    seen: set[str] = set()

    for page_num in range(1, max_pages + 1):
        text, entries, range_info = await _read_collection_message(account, msg_id)

        if not entries:
            account.log.warning(f"scanner: [fase 1] 0 entries na pagina {page_num}")
            await _dump_text(account, f"page{page_num}_no_entries", text)
            break

        new_entries = [e for e in entries if e.code not in seen]
        for e in new_entries:
            seen.add(e.code)
            all_entries.append(e)

        if range_info is not None:
            start, end, total = range_info
            account.log.info(
                f"scanner: [fase 1] pagina {page_num} cards {start}-{end}/{total} "
                f"(+{len(new_entries)} novos | coletado: {len(all_entries)})"
            )
        else:
            account.log.info(
                f"scanner: [fase 1] pagina {page_num} (+{len(new_entries)} novos)"
            )

        # Ultima pagina?
        if range_info is not None and range_info[1] >= range_info[2]:
            account.log.info("scanner: [fase 1] ultima pagina alcancada")
            break
        if await is_next_page_disabled(page, msg_id):
            account.log.info("scanner: [fase 1] botao ▶ desabilitado, fim")
            break

        # Clica ▶ pra proxima pagina
        clicked = await click_next_page_reaction(page, msg_id)
        if not clicked:
            buttons = await list_message_buttons(page, msg_id)
            reactions = await list_message_reactions(page, msg_id)
            account.log.warning(
                f"scanner: [fase 1] nao consegui clicar ▶ na pagina {page_num}. "
                f"buttons={buttons} reactions={reactions}"
            )
            html_path = f"./logs/scanner_msg_html_{account.id}_p{page_num}_{int(time.time())}.html"
            await dump_message_html(page, msg_id, html_path)
            account.log.warning(f"scanner: [fase 1] HTML dumpado em {html_path}")
            account.log.warning(
                f"scanner: [fase 1] encerrando coleta com {len(all_entries)} codes parciais"
            )
            break

        # Espera o embed atualizar
        new_range = await _wait_page_changed(account, msg_id, range_info, timeout_seconds=8.0)
        if new_range is None:
            account.log.warning(
                "scanner: [fase 1] embed nao atualizou apos ▶ — pode ter expirado. "
                f"Encerrando coleta com {len(all_entries)} codes parciais"
            )
            break

    account.log.info(f"scanner: [fase 1] completa — {len(all_entries)} codes coletados")
    return all_entries


async def scan_account(
    account: Account,
    base_min: int = 40,
    effort_min: int = 90,
    command_gap_seconds: float = DEFAULT_COMMAND_GAP_SECONDS,
    max_cards: int | None = None,
    max_pages: int | None = None,
    prefilter: PrefilterRule | None = None,
    on_match: "callable | None" = None,
) -> list[CardScan]:
    """
    Scan em DUAS FASES (resolve o problema de botoes do embed que expiram
    durante k!wi demorado):

      FASE 1 — Coleta: envia k!c e pagina via ▶ rapido, juntando todos os
               codes. Botoes ficam vivos porque a paginacao eh rapida.
      FASE 2 — Avaliacao: roda k!wi em cada code coletado. A mensagem do
               embed pode expirar aqui — nao importa, ja temos os codes.

    Escreve matches incrementalmente via `on_match`.
    """
    t0 = time.time()
    assert account.info_page is not None
    limiter = CommandRateLimiter(min_gap_seconds=command_gap_seconds)

    HARD_PAGE_CAP = 200
    user_max_pages = max_pages if (max_pages and max_pages > 0) else HARD_PAGE_CAP

    # ===== FASE 1 =====
    account.log.info("scanner: === FASE 1: coletando codes (paginacao rapida) ===")
    all_entries = await _collect_all_entries(account, limiter, user_max_pages)
    if not all_entries:
        account.log.error("scanner: fase 1 nao coletou nenhum code, abortando conta")
        return []
    _dump_collected_codes(account, all_entries)

    # ===== FASE 2 =====
    account.log.info(
        f"scanner: === FASE 2: k!wi de {len(all_entries)} cards "
        f"(estimado ~{len(all_entries) * command_gap_seconds / 60:.0f} min) ==="
    )
    results: list[CardScan] = []
    cards_evaluated = 0
    cards_skipped_prefilter = 0
    consecutive_failures = 0
    CIRCUIT_BREAKER_THRESHOLD = 5  # 5 falhas seguidas = aborta a conta

    for i, entry in enumerate(all_entries, 1):
        if max_cards is not None and cards_evaluated >= max_cards:
            account.log.info(f"scanner: atingiu max_cards={max_cards}")
            break

        if prefilter is not None and not prefilter.keeps(entry):
            cards_skipped_prefilter += 1
            continue

        info = await fetch_worker_info(account, entry.code, limiter=limiter)
        cards_evaluated += 1
        if info is None:
            consecutive_failures += 1
            account.log.warning(
                f"scanner: nao consegui ler {entry.code} "
                f"(falhas consecutivas: {consecutive_failures}/{CIRCUIT_BREAKER_THRESHOLD})"
            )
            if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                account.log.error(
                    "scanner: muitas falhas seguidas, abortando essa conta. "
                    "Karuta provavelmente em anti-spam mode. Aguarde e rode de novo."
                )
                return results
            continue
        consecutive_failures = 0  # reset apos sucesso

        info.stars = entry.stars
        info.print_num = entry.print_num
        info.edition = entry.edition

        passes_base = info.base_value >= base_min
        passes_effort = info.effort_total >= effort_min
        if passes_base or passes_effort:
            reason = []
            if passes_base:
                reason.append(f"base>={base_min}")
            if passes_effort:
                reason.append(f"effort>={effort_min}")
            results.append(info)
            account.log.info(
                f"scanner: ✓ {entry.code} base={info.base_value} "
                f"effort={info.effort_total} [{'+'.join(reason)}] "
                f"★{entry.stars} #{entry.print_num} ◈{entry.edition} {info.character}"
            )
            if on_match is not None:
                on_match(account.id, info)
        else:
            account.log.debug(
                f"scanner: skip {entry.code} base={info.base_value} "
                f"effort={info.effort_total}"
            )

        if i % 25 == 0:
            account.log.info(
                f"scanner: [fase 2] progresso {i}/{len(all_entries)} "
                f"({len(results)} matches ate agora)"
            )

    extra = (
        f", {cards_skipped_prefilter} prefilter-skipped"
        if prefilter is not None and cards_skipped_prefilter
        else ""
    )
    account.log.info(
        f"scanner: conta {account.id} -> {len(results)} matches "
        f"(base>={base_min} ou effort>={effort_min}) "
        f"de {cards_evaluated} avaliadas{extra} em {time.time() - t0:.1f}s"
    )
    return results


def render_markdown(
    results_by_account: dict[str, list[CardScan]],
    base_min: int,
    effort_min: int = 90,
) -> str:
    lines = [
        f"# Cartas Worker — Base ≥ {base_min} OU Effort ≥ {effort_min}",
        "",
        f"_Gerado por `karuta scan` em {time.strftime('%Y-%m-%d %H:%M:%S')}_",
        "",
    ]
    total = sum(len(r) for r in results_by_account.values())
    lines.append(
        f"**Total: {total} cartas** com **base ≥ {base_min}** OU "
        f"**effort ≥ {effort_min}** nas {len(results_by_account)} contas."
    )
    lines.append("")

    for aid, results in results_by_account.items():
        lines.append(f"## Conta {aid.upper()} — {len(results)} cartas")
        lines.append("")
        if not results:
            lines.append("_Nenhuma carta encontrada acima dos thresholds._")
            lines.append("")
            continue
        # Ordena por Effort decrescente (mais importante pro worker)
        lines.append("| Code | **Effort** | Base | ★ | Print | ◈ | Personagem |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in sorted(results, key=lambda x: (-x.effort_total, -x.base_value)):
            char = r.character or "—"
            stars = "★" * r.stars + "☆" * (4 - r.stars) if r.stars else "—"
            pnum = f"#{r.print_num}" if r.print_num else "—"
            edn = f"◈{r.edition}" if r.edition else "—"
            lines.append(
                f"| `{r.code}` | **{r.effort_total}** | {r.base_value} | "
                f"{stars} | {pnum} | {edn} | {char} |"
            )
        lines.append("")

    return "\n".join(lines)
