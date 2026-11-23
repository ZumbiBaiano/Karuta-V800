from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass

from loguru import logger
from playwright.async_api import Error as PWError, Page, TimeoutError as PWTimeoutError

DISCORD_CHANNEL_URL = "https://discord.com/channels/{guild}/{channel}"

# Quanto esperar a caixa de mensagem na entrada do canal.
# Um navegador sozinho renderiza em <10s, mas o bot sobe as 3 contas em
# paralelo; com a CPU disputada (jogo aberto, por exemplo) a largada tripla
# passa folgada dos 30s que havia aqui antes e as 3 contas morriam no setup.
CHANNEL_READY_TIMEOUT_MS = 90_000


def channel_url(guild_id: str, channel_id: str) -> str:
    return DISCORD_CHANNEL_URL.format(guild=guild_id, channel=channel_id)


class ChannelAccessError(RuntimeError):
    """A conta esta logada mas nao consegue abrir o canal."""


# Fundo escurecido que o Discord poe atras de um modal.
OVERLAY_SELECTOR = '[class*="scrim"]'


async def dismiss_overlays(page: Page, tentativas: int = 3) -> bool:
    """
    Fecha modais sobrepostos ao canal. Retorna True se havia algum.

    O Discord abre popups por cima do chat (changelog "Novidades", avisos de
    recurso novo) e o fundo escurecido deles come os cliques no textbox. O
    sintoma eh traicoeiro: o Playwright enxerga o textbox visivel e habilitado,
    tenta clicar, o scrim intercepta o evento, e ele so falha 30s depois — a
    conta fica sem conseguir mandar comando nenhum. Escape fecha esses modais.
    """
    if await page.locator(OVERLAY_SELECTOR).count() == 0:
        return False
    for _ in range(tentativas):
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        if await page.locator(OVERLAY_SELECTOR).count() == 0:
            logger.info("modal do Discord dispensado com Escape")
            return True
    logger.warning("modal do Discord nao fechou com Escape — cliques podem falhar")
    return True


async def goto_channel(page: Page, guild_id: str, channel_id: str) -> None:
    # O timeout da NAVEGACAO tambem precisa da folga, nao so o da caixa de
    # texto abaixo: com as 3 contas subindo juntas o goto passou dos 30s
    # padrao do Playwright e derrubou o start inteiro, mesmo ja havendo 90s
    # de margem para o passo seguinte.
    await page.goto(
        channel_url(guild_id, channel_id),
        wait_until="domcontentloaded",
        timeout=CHANNEL_READY_TIMEOUT_MS,
    )
    try:
        # Espera a caixa de mensagem aparecer (indica que o canal carregou).
        await page.locator('div[role="textbox"]').first.wait_for(
            timeout=CHANNEL_READY_TIMEOUT_MS
        )
    except PWTimeoutError:
        pass
    else:
        await dismiss_overlays(page)
        return

    # Sem textbox: distinguir os motivos, porque o timeout cru nao diz nada.
    url = page.url
    if "/login" in url:
        raise ChannelAccessError(
            "sessao expirou — rode 'karuta login <conta> --creds' de novo"
        )
    if channel_id not in url:
        # Discord tira o canal da URL quando a conta nao pode ve-lo.
        raise ChannelAccessError(
            f"sem acesso ao canal {channel_id} (Discord redirecionou para {url}). "
            "A conta esta no servidor mas sem permissao de ver o canal: "
            "verifique cargo, ou se falta aceitar as regras do servidor."
        )
    raise ChannelAccessError(
        f"canal carregou mas a caixa de mensagem nao apareceu em "
        f"{CHANNEL_READY_TIMEOUT_MS // 1000}s (url={url})"
    )


async def human_delay(min_s: float = 0.3, max_s: float = 1.0) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def send_message(page: Page, text: str) -> None:
    """
    Foca a caixa de texto do Discord e envia o texto.
    Discord nao tem name fixo no textbox em todos os locales, entao usa role + last.
    Limpa qualquer conteudo residual antes de digitar pra evitar merge com texto
    anterior (ex: 'kk!c' = leftover 'k' + 'k!c').
    """
    # Modais podem surgir depois do setup (o Discord solta changelog a
    # qualquer momento), entao reconfere antes de cada envio. Quando nao ha
    # nada aberto isso custa so um count().
    await dismiss_overlays(page)
    box = page.locator('div[role="textbox"]').last
    await box.click()
    await human_delay()
    # Limpa o textbox: select-all + delete (cobre drafts, texto residual, etc).
    await box.press("Control+A")
    await box.press("Delete")
    await box.type(text, delay=random.randint(40, 90))
    await human_delay(0.1, 0.3)
    await box.press("Enter")


@dataclass
class CardTake:
    """Carta que o Karuta confirmou que a conta levou, apos um pick."""

    code: str
    character: str = ""
    condition: str = ""


# "@willo took the Wirth Madl card vllpfgs! Wow, it appears to be in mint
#  condition!" / "@Madson took the Noein card vlg0bc9! Its condition is quite
#  poor." — o nome do personagem pode ter espacos, entao usa non-greedy.
TAKE_RE = re.compile(
    r"took the\s+(.+?)\s+card\s+([a-z0-9]{7})\b",
    re.IGNORECASE,
)

# Condicoes do Karuta, da melhor pra pior. "damaged" cobre "badly damaged".
_CONDITIONS = ("mint", "excellent", "good", "poor", "damaged")


def parse_take_message(text: str) -> CardTake | None:
    """
    Extrai code/personagem/condicao da confirmacao de pick do Karuta.
    Retorna None se o texto nao for uma confirmacao.
    """
    m = TAKE_RE.search(text or "")
    if not m:
        return None
    low = text.lower()
    condition = next((c for c in _CONDITIONS if c in low), "")
    return CardTake(
        code=m.group(2).lower(),
        character=m.group(1).strip(),
        condition=condition,
    )


async def capturar_usuario(page: Page) -> str:
    """
    Nome de usuario da conta logada, lido do painel inferior esquerdo.

    Necessario porque num drop as 3 contas pegam cartas e o Karuta manda 3
    confirmacoes no mesmo canal — a unica forma de saber qual eh a nossa eh
    comparando com o proprio nome.
    """
    for sel in (
        '[class*="panels"] [class*="nameTag"]',
        'section[aria-label] [class*="nameTag"]',
    ):
        try:
            el = page.locator(sel).first
            await el.wait_for(state="attached", timeout=5000)
            nome = ((await el.inner_text()) or "").strip().split("\n")[0]
            if nome:
                return nome
        except Exception:
            continue
    return ""


async def find_own_take(
    page: Page,
    usuario: str,
    ignorar_codigos: set[str] | None = None,
    lookback: int = 15,
) -> CardTake | None:
    """
    Acha a confirmacao de pick desta conta entre as mensagens recentes.

    Filtra por '@<usuario> took the' — a tentativa anterior usava a classe
    'mentioned' do <li>, que simplesmente nao existe no DOM atual do Discord
    (a classe eh 'messageListItem__...'), e por isso nunca achava nada.

    `ignorar_codigos` evita devolver a mesma carta duas vezes quando duas
    sessoes compartilham o mesmo usuario do Discord.
    """
    if not usuario:
        return None
    ignorar = ignorar_codigos or set()
    marcador = f"@{usuario} took the".lower()
    try:
        msgs = page.locator('li[id^="chat-messages-"]')
        count = await msgs.count()
        for i in range(count - 1, max(-1, count - 1 - lookback), -1):
            text = (await msgs.nth(i).inner_text()) or ""
            if marcador not in text.lower():
                continue
            take = parse_take_message(text)
            if take and take.code not in ignorar:
                return take
    except Exception:
        pass
    return None


_REACTION_NAMES = ["one", "two", "three"]

# Emoji de cada carta, na ordem dos botoes. Vem do atributo `data-name` do
# <img> dentro da reaction.
#
# Por que emoji e nao aria-label: o label do Discord muda de idioma E de
# estado. No mesmo drop ja apareceu como "clique para reagir com two" e como
# "two, 2 reações, pressione para reagir". Depender do texto quebra o pick
# de forma intermitente; o data-name eh estavel.
EMOJIS_CARTA = ("1️⃣", "2️⃣", "3️⃣")

# Reactions que correspondem as cartas. Qualquer outra reaction num drop eh
# candidata a icone de evento (Karuta adiciona um 4o botao durante eventos).
CARD_REACTION_NAMES = frozenset(_REACTION_NAMES)

# Le as reactions da mensagem direto do DOM, devolvendo emoji + label.
# Escopado ao container de reactions pra nao pegar outros botoes do <li>
# (o wrapper da imagem do drop tem aria-label 'Imagem' e virava falso
# positivo de icone de evento).
_JS_REACOES = """
(msgId) => {
  const li = document.querySelector(`li[id$="-${msgId}"]`);
  if (!li) return [];
  const out = [];
  li.querySelectorAll('[class*="reactions"] [role="button"]').forEach(el => {
    const img = el.querySelector('img[data-name], img.emoji, img[alt]');
    out.push({
      emoji: img ? (img.getAttribute('data-name') || img.getAttribute('alt') || '') : '',
      label: el.getAttribute('aria-label') || '',
    });
  });
  return out;
}
"""


async def listar_reacoes(page: Page, message_id: str) -> list[dict]:
    """Reactions do drop: [{'emoji': '1️⃣', 'label': 'one, 2 reações...'}]."""
    try:
        return await page.evaluate(_JS_REACOES, message_id)
    except Exception:
        return []


def _seletor_reacao(emoji: str) -> str:
    """Localiza a reaction pelo emoji, independente de idioma."""
    return f'[class*="reactions"] [role="button"]:has(img[data-name="{emoji}"])'

# aria-labels do Discord que sao botoes da mensagem, nao reactions. Ficam no
# mesmo <li> e apareceriam como falso-positivo de icone de evento.
_NON_REACTION_LABEL_HINTS = (
    "add reaction", "adicionar reação", "adicionar reacao",
    "more", "mais", "reply", "responder", "forward", "encaminhar",
    "edit", "editar", "pin", "fixar", "thread", "tópico", "topico",
)


def reaction_name(aria_label: str) -> str:
    """
    Extrai o nome do emoji do aria-label de uma reaction.
    Discord usa "<nome>, N reactions, press to react" (e variantes traduzidas),
    entao o nome eh tudo antes da primeira virgula.
    """
    return (aria_label or "").split(",", 1)[0].strip().lower()


def event_icon_names(reacoes: list[dict]) -> list[str]:
    """
    Dadas as reactions do drop, devolve os EMOJIS que nao sao carta — ou seja,
    os icones de evento.

    Decide pelo emoji, nao pelo aria-label: o label muda de idioma e de estado
    (ja apareceu como 'clique para reagir com two' e como 'two, 2 reações,
    pressione para reagir' no mesmo drop), o que gerava falso positivo e fazia
    o bot tentar clicar nas proprias cartas.

    Puro de proposito: da pra testar sem navegador.
    """
    out: list[str] = []
    for r in reacoes:
        emoji = (r.get("emoji") or "").strip()
        if not emoji or emoji in EMOJIS_CARTA:
            continue
        # Reserva: se o emoji nao veio, tenta pelo nome no inicio do label.
        nome = reaction_name(r.get("label", ""))
        if nome in CARD_REACTION_NAMES:
            continue
        if emoji not in out:
            out.append(emoji)
    return out


async def find_event_icons(page: Page, message_id: str) -> list[str]:
    """
    Emojis de reaction do drop que nao correspondem as 3 cartas.
    Durante eventos o Karuta adiciona um 4o icone que da item de evento.
    """
    return event_icon_names(await listar_reacoes(page, message_id))


async def click_event_icon(
    page: Page,
    message_id: str,
    emoji: str,
    timeout_ms: int = 5000,
) -> bool:
    """
    Clica a reaction do icone de evento pelo emoji.

    Confere a CONTAGEM antes e depois em vez de devolver True no despacho do
    clique. Sem isso o retorno so dizia "cliquei", nunca "a reaction entrou" —
    e o bot registrava sucesso mesmo quando nada acontecia (2026-08-06: 6
    cliques marcados ok, zero itens no inventario).

    Contagem que nao sobe pode significar reaction ja dada (o clique REMOVE),
    drop expirado ou clique que nao pegou. Nos tres casos nao houve coleta.
    """
    try:
        msg = page.locator(f'li[id$="-{message_id}"]')
        await msg.wait_for(state="visible", timeout=timeout_ms)
        icon = msg.locator(_seletor_reacao(emoji)).last
        await icon.wait_for(state="visible", timeout=timeout_ms)
        antes = await _contagem_reacao(page, message_id, emoji)
        await icon.click()
        await asyncio.sleep(1.5)  # Discord confirma a reaction no servidor
        depois = await _contagem_reacao(page, message_id, emoji)
        if depois > antes:
            return True
        logger.warning(
            f"icone '{emoji}' em {message_id}: contagem {antes} -> {depois} "
            "(reaction nao entrou; sem coleta)"
        )
        return False
    except PWTimeoutError:
        return False


async def _contagem_reacao(page: Page, message_id: str, emoji: str) -> int:
    """Quantas pessoas reagiram com `emoji` na mensagem. -1 se nao achou."""
    for r in await listar_reacoes(page, message_id):
        if (r.get("emoji") or "").strip() != emoji:
            continue
        # O aria-label comeca com o nome e traz a contagem: "🍉, 2 reações".
        m = re.search(r"(\d+)", r.get("label", ""))
        return int(m.group(1)) if m else -1
    return -1


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
    emoji = EMOJIS_CARTA[button_index]
    name = _REACTION_NAMES[button_index]
    # Modal do Discord por cima do chat intercepta o clique na reaction, e o
    # Playwright so desiste depois de ~30s — tarde demais, a carta ja foi.
    # Este e o caminho mais sensivel a tempo do bot, entao vale o count()
    # extra: quando nao ha modal aberto, dismiss_overlays sai na hora.
    await dismiss_overlays(page)
    # Mira na mensagem exata do drop pelo message_id (sufixo do id do <li>).
    # Karuta adiciona as reactions 1, 2, 3 em sequencia com pequeno delay,
    # entao wait_for fica pollando ate a nossa surgir.
    drop_msg = page.locator(f'li[id$="-{message_id}"]')
    try:
        await drop_msg.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeoutError:
        return False

    # 1) Pelo emoji (data-name) — estavel entre idiomas e estados.
    try:
        reaction = drop_msg.locator(_seletor_reacao(emoji)).last
        await reaction.wait_for(state="visible", timeout=timeout_ms)
        await reaction.click()
        return True
    except PWTimeoutError:
        pass

    # 2) Reserva: aria-label em ingles ("one, 2 reactions, ..."). Mantido
    #    caso o Discord mude a estrutura do <img> da reaction.
    try:
        reaction = drop_msg.locator(f'div[role="button"][aria-label^="{name},"]').last
        await reaction.wait_for(state="visible", timeout=3000)
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


# Varre as ultimas mensagens devolvendo id + emojis das reactions, numa
# unica ida ao browser (em vez de uma chamada por mensagem).
_JS_DROPS_RECENTES = """
(lookback) => {
  const lis = Array.from(document.querySelectorAll('li[id^="chat-messages-"]'));
  const out = [];
  for (const li of lis.slice(-lookback)) {
    const emojis = [];
    li.querySelectorAll('[class*="reactions"] [role="button"]').forEach(el => {
      const img = el.querySelector('img[data-name], img.emoji, img[alt]');
      const e = img ? (img.getAttribute('data-name') || img.getAttribute('alt') || '') : '';
      if (e) emojis.push(e);
    });
    if (!emojis.length) continue;
    const parts = li.id.split('-');
    out.push({ id: parts[parts.length - 1], emojis });
  }
  return out.reverse();
}
"""


def tem_reacoes_de_carta(emojis: list[str]) -> bool:
    """
    True se as reactions sao de um drop (tem 1/2/3).

    Separado e puro para dar pra testar sem navegador: eh este criterio que
    decide o que a varredura considera drop, e foi justamente ele que faltava
    quando o filtro era por texto.
    """
    return any(e in EMOJIS_CARTA for e in emojis)


async def listar_drops_recentes(page: Page, lookback: int = 15) -> list[str]:
    """
    IDs das mensagens de drop entre as ultimas `lookback` do canal.

    Identifica drop pelas REACTIONS de carta (1/2/3), nao pelo texto.

    Por que nao pelo texto: quando o Karuta solta um drop EXTRA, so o primeiro
    traz a frase "is dropping N cards!"; o segundo vem so com a imagem e as
    reactions. Confirmado no canal em 2026-08-06 20:32, onde o drop extra
    tinha 1/2/3 + 🍉 e nenhuma frase de drop — e era justamente o que tinha o
    icone de evento. Filtrar por `is_drop_text` deixava passar exatamente a
    mensagem que interessa.

    Mais recente primeiro.
    """
    try:
        msgs = await page.evaluate(_JS_DROPS_RECENTES, lookback)
    except PWError:
        return []
    ids: list[str] = []
    for m in msgs:
        msg_id = (m.get("id") or "").strip()
        if not msg_id.isdigit():
            continue
        # Drop = tem as reactions de carta. Sem isso, qualquer mensagem que
        # alguem reagiu com emoji viraria candidata e o bot clicaria nela.
        if not tem_reacoes_de_carta(m.get("emojis", [])):
            continue
        ids.append(msg_id)
    return ids


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


# Emojis de "proxima pagina". O Karuta usa conjuntos diferentes por comando:
# a colecao (k!c) tem 4 botoes ⏪◀▶⏩, o inventario (k!i/k!bi) tem 2: ⬅️➡️.
# Por isso a busca eh pelo emoji, nao por posicao — depender do indice fazia
# a paginacao do inventario falhar em silencio (lia so a 1a pagina).
_EMOJIS_PROXIMA = ("➡️", "➡", "▶️", "▶", "⏩")

_JS_BOTAO_PROXIMA = """
([msgId, emojis]) => {
  const li = document.querySelector(`li[id$="-${msgId}"]`);
  if (!li) return -1;
  const bs = Array.from(li.querySelectorAll('button'));
  for (let i = 0; i < bs.length; i++) {
    const img = bs[i].querySelector('img[alt], img[data-name]');
    const e = img ? (img.getAttribute('alt') || img.getAttribute('data-name') || '') : '';
    if (emojis.some(x => e.includes(x))) return bs[i].disabled ? -2 : i;
  }
  return -1;
}
"""


async def click_next_page_reaction(
    page: Page,
    message_id: str,
    timeout_ms: int = 5000,
) -> bool:
    """
    Clica o botao de proxima pagina. Retorna True se conseguiu.

    Estrategia principal: achar o botao pelo emoji (⬅️➡️ no inventario,
    ⏪◀▶⏩ na colecao). Botao desabilitado significa ultima pagina -> False.
    """
    msg = page.locator(f'li[id$="-{message_id}"]')

    # 1) Pelo emoji — funciona com qualquer quantidade de botoes.
    try:
        idx = await page.evaluate(_JS_BOTAO_PROXIMA, [message_id, list(_EMOJIS_PROXIMA)])
        if idx == -2:
            return False  # ultima pagina
        if idx >= 0:
            btn = msg.locator("button").nth(idx)
            await btn.wait_for(state="visible", timeout=timeout_ms)
            await btn.click()
            return True
    except Exception:
        pass

    # 2) Reserva: heuristica antiga por posicao (colecao tem 4 botoes).
    try:
        all_buttons = msg.locator("button")
        count = await all_buttons.count()
        if count >= 4:
            btn = all_buttons.nth(2)  # ▶ next
            try:
                disabled = await btn.is_disabled()
            except Exception:
                disabled = False
            if disabled:
                # Ja na ultima pagina
                return False
            try:
                await btn.wait_for(state="visible", timeout=timeout_ms)
                await btn.click()
                return True
            except Exception:
                pass
    except Exception:
        pass

    # 2) Fallback: tenta por aria-label (caso Karuta mude pra buttons rotulados)
    button_selectors = [
        'button[aria-label*="arrow_forward" i]',
        'button[aria-label*="next page" i]',
        'button[aria-label*="next" i]',
        'button[aria-label*="forward" i]',
        'button[aria-label*="proxima" i]',
        'button[aria-label*="próxima" i]',
    ]
    reaction_selectors = [
        f'div[role="button"][aria-label^="{p}"]' for p in _NEXT_PAGE_LABEL_PREFIXES
    ]
    for sel in button_selectors + reaction_selectors:
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


async def is_next_page_disabled(page: Page, message_id: str) -> bool:
    """True se o botao ▶ esta desabilitado (estamos na ultima pagina)."""
    msg = page.locator(f'li[id$="-{message_id}"]')
    try:
        all_buttons = msg.locator("button")
        if await all_buttons.count() >= 4:
            return await all_buttons.nth(2).is_disabled()
    except Exception:
        pass
    return False


async def dump_message_html(page: Page, message_id: str, save_path) -> bool:
    """Salva o outerHTML da mensagem para inspecionar estrutura DOM."""
    from pathlib import Path

    msg = page.locator(f'li[id$="-{message_id}"]')
    try:
        html = await msg.evaluate("el => el.outerHTML")
        p = Path(save_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
        return True
    except Exception:
        return False


async def list_message_buttons(page: Page, message_id: str) -> list[dict]:
    """Lista todos os <button> dentro da mensagem com seus aria-labels e textos."""
    msg = page.locator(f'li[id$="-{message_id}"]')
    buttons = msg.locator("button")
    out: list[dict] = []
    try:
        count = await buttons.count()
        for i in range(count):
            b = buttons.nth(i)
            try:
                label = await b.get_attribute("aria-label")
                text = (await b.inner_text()) or ""
                out.append({"index": i, "aria_label": label or "", "text": text.strip()[:50]})
            except Exception:
                pass
    except Exception:
        pass
    return out


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


# ----------------------------------------------------------------------
# Multitrade: transferencia de cartas em lote entre duas contas
# ----------------------------------------------------------------------
#
# O fluxo tem TRES emoji diferentes, em tres etapas, e confundi-los quebra
# no meio sem erro visivel:
#   1. quem recebe o pedido aceita com  ☑️
#   2. os DOIS lados travam com         🔒
#   3. os DOIS lados confirmam com      ✅
MT_ACEITAR = "☑️"
MT_TRAVAR = "🔒"
MT_CONFIRMAR = "✅"

# A sessao morre em 3 minutos — tudo abaixo corre contra esse relogio.
MT_EXPIRA_SEGUNDOS = 180


async def mt_localizar_sessao(
    page: Page, lookback: int = 8, ignorar: str | None = None,
) -> str | None:
    """
    ID da mensagem de uma sessao de trade ABERTA, se houver.

    Casa so com pedido pendente ou sessao em andamento. NAO casa com "The
    trade has been accepted": essa e a sessao ja encerrada, e agarra-la fazia
    o lote seguinte mandar os codigos pra uma sessao morta — os codigos sumiam
    sem erro e a transferencia falhava sem explicacao.

    `ignorar` descarta um id conhecido (o da sessao anterior), cobrindo o caso
    em que o k!multitrade novo ainda nao chegou por rate limit: sem isso a
    busca devolveria a sessao velha como se fosse a nova.
    """
    return await page.evaluate(
        """([n, ignorar, aceitar]) => {
             const lis=[...document.querySelectorAll('li[id^="chat-messages-"]')];
             for (let i=lis.length-1; i>=Math.max(0,lis.length-n); i--) {
               const id = lis[i].id.split('-').pop();
               if (ignorar && id === ignorar) continue;
               const t = lis[i].innerText || '';
               if (/trade has been/i.test(t)) continue;   // sessao encerrada
               if (!/Trade Request|trade session/i.test(t)) continue;
               // Exige o botao de aceitar presente. So o texto nao basta:
               // sessao cancelada ou expirada mantem o texto e perde os
               // botoes, e agarra-la fazia o lote inteiro falhar sem causa
               // aparente — principalmente entre execucoes, quando o
               // `ignorar` do processo anterior ja nao existe.
               const temAceitar = [...lis[i].querySelectorAll('button')]
                 .some(b => {
                   const im = b.querySelector('img[data-name]');
                   return im && im.getAttribute('data-name') === aceitar
                          && !b.hasAttribute('disabled');
                 });
               if (temAceitar) return id;
             }
             return null;
           }""",
        [lookback, ignorar, MT_ACEITAR],
    )


async def mt_botoes(page: Page, message_id: str) -> list[str]:
    """Emojis dos botoes atualmente na mensagem da sessao."""
    return await page.evaluate(
        """(mid) => {
             const li = document.querySelector(`li[id$="-${mid}"]`);
             if (!li) return [];
             return [...li.querySelectorAll('button')]
               .map(b => b.querySelector('img[data-name]'))
               .filter(Boolean)
               .map(i => i.getAttribute('data-name'));
           }""",
        message_id,
    )


async def mt_clicar(page: Page, message_id: str, emoji: str, timeout_ms: int = 10_000) -> bool:
    """
    Clica um botao da sessao pelo emoji.

    Pelo emoji e nao por indice de proposito: a ordem muda entre etapas e o
    ❌ vem ANTES do ✅ — indexar por posicao cancelaria a troca.
    """
    await dismiss_overlays(page)
    sel = f'li[id$="-{message_id}"] button:has(img[data-name="{emoji}"])'
    try:
        btn = page.locator(sel).first
        await btn.wait_for(state="visible", timeout=timeout_ms)
        await btn.click()
        return True
    except PWTimeoutError:
        logger.debug(f"botao {emoji} nao apareceu na sessao {message_id}")
        return False


async def mt_adicionar_cartas(page: Page, codigos: list[str]) -> None:
    """
    Poe as cartas na sessao: um unico envio com os codigos separados por virgula.

    Usa fill() em vez do send_message normal porque aquele digita em ritmo
    humano (40-90ms/caractere). Com 30 codigos sao ~270 caracteres, ou seja
    ~20s so digitando — e a sessao inteira dura 180s. fill() e instantaneo.
    """
    await dismiss_overlays(page)
    box = page.locator('div[role="textbox"]').last
    await box.click()
    await box.press("Control+A")
    await box.press("Delete")
    await box.fill(",".join(codigos))
    await box.press("Enter")


async def mt_esperar(
    page: Page,
    message_id: str,
    condicao,
    timeout: float = 20.0,
    intervalo: float = 0.4,
) -> bool:
    """
    Espera a sessao satisfazer `condicao(texto, emojis)`, checando a cada 0.4s.

    Substitui os sleep fixos que havia em cada etapa. Alem de mais rapido no
    caso comum (o Karuta costuma responder em <1s), e mais confiavel: o sleep
    cego ora esperava demais, ora seguia antes do Karuta ter processado.
    """
    import time as _t

    limite = _t.time() + timeout
    while _t.time() < limite:
        estado = await page.evaluate(
            """(mid) => {
                 const li = document.querySelector(`li[id$="-${mid}"]`);
                 if (!li) return null;
                 const emojis = [...li.querySelectorAll('button')]
                   .map(b => b.querySelector('img[data-name]'))
                   .filter(Boolean).map(i => i.getAttribute('data-name'));
                 return {texto: li.innerText || '', emojis};
               }""",
            message_id,
        )
        if estado and condicao(estado["texto"], estado["emojis"]):
            return True
        await asyncio.sleep(intervalo)
    return False


async def mt_aceita(page: Page, message_id: str) -> bool:
    """A sessao chegou ao estado final de aceite?"""
    txt = await page.evaluate(
        """(mid) => {
             const li = document.querySelector(`li[id$="-${mid}"]`);
             return li ? li.innerText : '';
           }""",
        message_id,
    )
    return "trade has been accepted" in (txt or "").lower()


async def estado_da_pagina(page: Page) -> dict:
    """
    Estado fora das mensagens que costuma explicar um clique que nao passa.

    Existe porque o dump de mensagens sozinho e cego pro motivo mais comum:
    um modal do Discord vive em layerContainer, irmao do chat, e nunca cai
    dentro dos <li> — foi assim que uma investigacao de falha de pick deu
    "sem modal" quando ninguem tinha olhado o lugar certo.
    """
    try:
        return {
            "scrims": await page.locator(OVERLAY_SELECTOR).count(),
            "dialogs": await page.locator('[role="dialog"]').count(),
            "url": page.url,
            "visivel": await page.evaluate(
                "() => document.visibilityState"
            ),
        }
    except Exception as e:
        return {"erro": str(e)[:200]}


async def dump_recent_messages(page: Page, out_path) -> None:
    """
    Salva HTML das ultimas 5 mensagens do canal (debug de localizadores),
    precedido do estado da pagina — veja estado_da_pagina.
    """
    try:
        estado = await estado_da_pagina(page)
        messages = page.locator('li[id^="chat-messages-"]')
        count = await messages.count()
        recent = [f"<!-- estado da pagina: {estado} -->"]
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


# Unidades por extenso, como o `krm` realmente escreve ("in 15 minutes").
_UNIDADES = (
    (re.compile(r"(\d+)\s*(?:hours?|hrs?|h)\b", re.IGNORECASE), 3600),
    (re.compile(r"(\d+)\s*(?:minutes?|mins?|m)\b", re.IGNORECASE), 60),
    (re.compile(r"(\d+)\s*(?:seconds?|secs?|s)\b", re.IGNORECASE), 1),
)


def duracao_para_segundos(trecho: str) -> int | None:
    """
    Converte "15 minutes", "1 hour 5 minutes", "5m 30s", "45s" em segundos.
    None se nao houver nenhuma duracao reconhecivel.

    Soma todas as unidades encontradas em vez de parar na primeira: assim
    "1 hour 5 minutes" nao vira 3600.
    """
    total = 0
    achou = False
    for rx, mult in _UNIDADES:
        for m in rx.finditer(trecho or ""):
            total += int(m.group(1)) * mult
            achou = True
    return total if achou else None


# Uma linha por acao: "Drop is ready" / "Grab in 7 minutes".
_KRM_LINHA_RE = re.compile(
    r"^[^\w\n]*(Daily|Vote|Drop|Grab|Effect|Work|Visit)\b[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

_ACOES_KRM = {
    "drop": "drop", "grab": "grab", "effect": "effect",
    "daily": "daily", "vote": "vote", "work": "work", "visit": "visit",
}


def parse_krm_reply(text: str) -> dict[str, int]:
    """
    Le a resposta do `krm` e devolve {acao: segundos_restantes}, com 0 para
    "is ready".

    Formato real do Karuta (uma acao por linha):

        🔕 Daily is ready
        🔕 Drop in 15 minutes
        🔕 Grab in 7 minutes

    A versao anterior procurava "5m 30s" e falhava em "15 minutes" — o `\\b`
    depois do `m` nao casa dentro de "minutes". Como devolvia vazio, o
    chamador caia numa resposta antiga do `krm` e o bot dropava achando que
    tinha grab disponivel.
    """
    out: dict[str, int] = {}
    if not text:
        return out
    for m in _KRM_LINHA_RE.finditer(text):
        acao = m.group(1).lower()
        resto = m.group(2)
        chave = _ACOES_KRM.get(acao)
        if chave is None or chave in out:
            continue
        # Duracao vence "ready": "will be ready in 2 minutes" nao esta pronto.
        segundos = duracao_para_segundos(resto)
        if segundos is not None:
            out[chave] = segundos
        elif re.search(r"\b(ready|now)\b", resto, re.IGNORECASE):
            out[chave] = 0
    return out


async def check_cooldowns_via_krm(page, lookback: int = 12, settle_sec: float = 2.5) -> dict[str, int]:
    """
    Envia `krm` no canal e parseia a resposta MAIS RECENTE do Karuta.
    Retorna dict com cooldowns em segundos (drop/grab/...).

    So considera a primeira mensagem de "Reminders" achada de tras pra frente.
    Antes, se essa nao parseasse, o laco seguia para mensagens mais antigas e
    devolvia um `krm` de minutos atras — o bot lia "grab pronto" com base em
    dado velho e dropava sem grab disponivel. Melhor devolver vazio e o
    chamador tratar como desconhecido.
    """
    await send_message(page, "krm")
    await asyncio.sleep(settle_sec)
    try:
        messages = page.locator('li[id^="chat-messages-"]')
        count = await messages.count()
        for i in range(count - 1, max(-1, count - 1 - lookback), -1):
            text = (await messages.nth(i).inner_text()) or ""
            if "Reminders" not in text:
                continue
            return parse_krm_reply(text)
    except Exception:
        pass
    return {}
