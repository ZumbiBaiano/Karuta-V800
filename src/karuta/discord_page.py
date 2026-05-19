from __future__ import annotations

import asyncio
import random
import re

from playwright.async_api import Page, TimeoutError as PWTimeoutError

DISCORD_CHANNEL_URL = "https://discord.com/channels/{guild}/{channel}"


def channel_url(guild_id: str, channel_id: str) -> str:
    return DISCORD_CHANNEL_URL.format(guild=guild_id, channel=channel_id)


async def goto_channel(page: Page, guild_id: str, channel_id: str) -> None:
    await page.goto(channel_url(guild_id, channel_id), wait_until="domcontentloaded")
    # Espera a caixa de mensagem aparecer (indica que o canal carregou).
    await page.locator('div[role="textbox"]').first.wait_for(timeout=30_000)


async def human_delay(min_s: float = 0.3, max_s: float = 1.0) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def send_message(page: Page, text: str) -> None:
    """
    Foca a caixa de texto do Discord e envia o texto.
    Discord nao tem name fixo no textbox em todos os locales, entao usa role + last.
    Limpa qualquer conteudo residual antes de digitar pra evitar merge com texto
    anterior (ex: 'kk!c' = leftover 'k' + 'k!c').
    """
    box = page.locator('div[role="textbox"]').last
    await box.click()
    await human_delay()
    # Limpa o textbox: select-all + delete (cobre drafts, texto residual, etc).
    await box.press("Control+A")
    await box.press("Delete")
    await box.type(text, delay=random.randint(40, 90))
    await human_delay(0.1, 0.3)
    await box.press("Enter")


_REACTION_NAMES = ["one", "two", "three"]


async def click_pick_button(
    page: Page,
    message_id: str,
    button_index: int,
    timeout_ms: int = 5000,
) -> bool:
    """
    Karuta usa reactions (emojis 1, 2, 3) e nao botoes interativos.
    Cada reaction eh um <div role="button" aria-label="<name>, ...">,
    onde <name> eh "one", "two" ou "three".

    button_index: 0 -> reaction "one", 1 -> "two", 2 -> "three".
    Retorna True se conseguiu clicar.
    """
    name = _REACTION_NAMES[button_index]
    try:
        # Mira na mensagem exata do drop pelo message_id (sufixo do id do <li>).
        # Karuta adiciona as reactions 1, 2, 3 em sequencia com pequeno delay,
        # entao wait_for fica pollando ate a nossa surgir.
        drop_msg = page.locator(f'li[id$="-{message_id}"]')
        await drop_msg.wait_for(state="visible", timeout=timeout_ms)
        reaction = drop_msg.locator(f'div[role="button"][aria-label^="{name},"]').last
        await reaction.wait_for(state="visible", timeout=timeout_ms)
        await reaction.click()
        return True
    except PWTimeoutError:
        return False


# Nomes de emoji que Karuta tipicamente usa pra paginacao "next".
# (Discord define aria-label como "<emoji_name>, ...")
_NEXT_PAGE_LABEL_PREFIXES = [
    "arrow_forward",
    "track_next",
    "next",
    "fast_forward",
    "right",
    "▶",
]


async def find_karuta_message_id(page: Page, marker: str, lookback: int = 12) -> str | None:
    """
    Acha o ID da mensagem mais recente que contem `marker` no texto.
    Retorna o sufixo do <li id="chat-messages-<channel>-<msg>">.
    """
    messages = page.locator('li[id^="chat-messages-"]')
    count = await messages.count()
    for i in range(count - 1, max(-1, count - 1 - lookback), -1):
        text = (await messages.nth(i).inner_text()) or ""
        if marker in text:
            li_id = await messages.nth(i).get_attribute("id")
            if li_id:
                # id formato: chat-messages-<channel>-<msg>
                parts = li_id.rsplit("-", 1)
                if len(parts) == 2:
                    return parts[1]
    return None


async def list_message_reactions(page: Page, message_id: str) -> list[str]:
    """Lista as aria-labels das reactions na mensagem (debug)."""
    msg = page.locator(f'li[id$="-{message_id}"]')
    reactions = msg.locator('div[role="button"][aria-label]')
    try:
        count = await reactions.count()
        labels: list[str] = []
        for i in range(count):
            label = await reactions.nth(i).get_attribute("aria-label")
            if label:
                labels.append(label)
        return labels
    except Exception:
        return []


async def click_next_page_reaction(
    page: Page,
    message_id: str,
    timeout_ms: int = 5000,
) -> bool:
    """
    Clica na reaction de 'proxima pagina' do embed identificado por message_id.
    Tenta varios nomes conhecidos. Retorna True se conseguiu clicar.
    """
    msg = page.locator(f'li[id$="-{message_id}"]')
    for prefix in _NEXT_PAGE_LABEL_PREFIXES:
        sel = f'div[role="button"][aria-label^="{prefix}"]'
        btn = msg.locator(sel).last
        try:
            if await btn.count() == 0:
                continue
            await btn.wait_for(state="visible", timeout=timeout_ms)
            await btn.click()
            return True
        except Exception:
            continue
    return False


async def wait_for_message_text_change(
    page: Page,
    message_id: str,
    previous_text_marker: str,
    timeout_seconds: float = 10.0,
    poll_interval: float = 0.5,
) -> bool:
    """
    Espera ate o texto da mensagem mudar (i.e., nao conter mais
    `previous_text_marker`). Util pra detectar quando a paginacao atualizou.
    """
    msg = page.locator(f'li[id$="-{message_id}"]')
    import time as _t
    deadline = _t.time() + timeout_seconds
    while _t.time() < deadline:
        try:
            text = (await msg.inner_text()) or ""
            if previous_text_marker not in text:
                return True
        except Exception:
            pass
        await asyncio.sleep(poll_interval)
    return False


async def dump_recent_messages(page: Page, out_path) -> None:
    """Salva HTML das ultimas 5 mensagens do canal (debug de localizadores)."""
    try:
        messages = page.locator('li[id^="chat-messages-"]')
        count = await messages.count()
        recent = []
        for i in range(max(0, count - 5), count):
            html = await messages.nth(i).evaluate("el => el.outerHTML")
            recent.append(html)
        from pathlib import Path
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("\n\n<!-- ---- -->\n\n".join(recent), encoding="utf-8")
    except Exception:
        pass


_DROP_PHRASE = re.compile(r"is dropping \d+ cards?!", re.IGNORECASE)
# "@user, you must wait 27 minutes before dropping more cards"
# "you must wait 30 seconds..."
_WAIT_PHRASE = re.compile(
    r"must wait\s+(\d+)\s+(minute|second)s?\s+before\s+dropping",
    re.IGNORECASE,
)


def is_drop_text(text: str) -> bool:
    """Detecta a frase de drop do Karuta. Tolerante a variacoes."""
    return bool(_DROP_PHRASE.search(text or ""))


def parse_kd_wait_seconds(text: str) -> int | None:
    """
    Se 'text' for uma resposta do Karuta tipo
    'you must wait 27 minutes before dropping more cards',
    retorna o tempo a esperar em segundos. Senao, None.
    """
    m = _WAIT_PHRASE.search(text or "")
    if not m:
        return None
    n = int(m.group(1))
    return n * 60 if m.group(2).lower().startswith("min") else n


async def find_recent_kd_wait(page, lookback: int = 8) -> int | None:
    """
    Procura nas ultimas 'lookback' mensagens do canal a resposta do Karuta
    de cooldown ('you must wait X minutes...'). Retorna segundos a esperar.
    """
    try:
        messages = page.locator('li[id^="chat-messages-"]')
        count = await messages.count()
        for i in range(count - 1, max(-1, count - 1 - lookback), -1):
            text = await messages.nth(i).inner_text()
            wait = parse_kd_wait_seconds(text)
            if wait is not None:
                return wait
    except Exception:
        pass
    return None


def parse_krm_reply(text: str) -> dict[str, int]:
    """
    Recebe o texto bruto da resposta do `krm` e retorna
    {'drop': segundos, 'grab': segundos, 'effect': segundos}.
    Cada chave so eh incluida se a acao for mencionada no texto.

    Formatos suportados:
      "Drop is ready", "Grab: ready", "Grab is ready!"
      "Grab will be ready in 5m 30s"
      "Grab: 5m 30s", "Grab: 12m", "Grab: 45s"
    """
    out: dict[str, int] = {}
    if not text:
        return out
    for action_name, key in (("Drop", "drop"), ("Grab", "grab"), ("Effect", "effect")):
        # pega ate 80 caracteres apos o nome da acao (ate quebra de linha)
        m = re.search(rf"{action_name}\b[^\n]{{0,80}}", text, re.IGNORECASE)
        if not m:
            continue
        segment = m.group(0)
        # Tenta pegar tempo primeiro: "ready in 2m 5s" tem "ready" mas nao
        # esta pronto. Se tem tempo, vence.
        ms = re.search(r"(\d+)\s*m\s+(\d+)\s*s", segment, re.IGNORECASE)
        if ms:
            out[key] = int(ms.group(1)) * 60 + int(ms.group(2))
            continue
        ms = re.search(r"(\d+)\s*m\b", segment, re.IGNORECASE)
        if ms:
            out[key] = int(ms.group(1)) * 60
            continue
        ms = re.search(r"(\d+)\s*s\b", segment, re.IGNORECASE)
        if ms:
            out[key] = int(ms.group(1))
            continue
        # Sem tempo: ready/now significa pronto.
        if re.search(r"\b(ready|now)\b", segment, re.IGNORECASE):
            out[key] = 0
    return out


async def check_cooldowns_via_krm(page, lookback: int = 12, settle_sec: float = 2.5) -> dict[str, int]:
    """
    Envia `krm` no canal e parseia a resposta mais recente do Karuta.
    Retorna dict com cooldowns em segundos (drop/grab/effect).
    """
    await send_message(page, "krm")
    await asyncio.sleep(settle_sec)
    try:
        messages = page.locator('li[id^="chat-messages-"]')
        count = await messages.count()
        for i in range(count - 1, max(-1, count - 1 - lookback), -1):
            text = (await messages.nth(i).inner_text()) or ""
            if "Grab" in text or "Drop" in text:
                cooldowns = parse_krm_reply(text)
                if cooldowns:
                    return cooldowns
    except Exception:
        pass
    return {}
