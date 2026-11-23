"""
Avaliacao das cartas recem-pegas, rodando no intervalo entre um `kd` e o
proximo.

Fluxo dentro do orquestrador:

    kd -> drop -> 3 picks -> [ AVALIACAO AQUI ] -> espera cooldown -> kd ...

Por que aqui: o cooldown de pick eh ~11min e o de drop 30min, entao ha tempo
de sobra ocioso. Cada carta custa 3 comandos ao Karuta (`k!wi`, `k!ci` e
`k!lu`), com gap de ~11s por causa do rate limit — 3 cartas dao ~99s, bem
dentro da janela.

A carta so entra na tabela mestre DEPOIS de avaliada, junto com a
classificacao (job / desejada / burn). Assim `cartas` nunca tem linha pela
metade.

Os comandos rodam sequencialmente entre contas (nao em paralelo) pelo mesmo
motivo do `_refresh_cooldowns`: 6 mensagens simultaneas no canal chamam
atencao e o Karuta responde com anti-spam.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from loguru import logger

from .account import Account
from .scanner import (
    BASE_VALUE_RE,
    EFFORT_TOTAL_RE,
    CommandRateLimiter,
    _latest_karuta_text,
    _send_with_cooldown_retry,
)

# ==========================================================================
# Parsers — escritos a partir da saida real do Karuta, capturada no DM.
# ==========================================================================

# `k!ci <code>` (Card Details) devolve uma linha resumo:
#   vllpzmj · ☆☆☆☆ · #18321 · ◈2 · Hell's Paradise: Jigokuraku · Adachi Matagorou
# Estrelas vem como 4 simbolos, ★ preenchida e ☆ vazia.
LINHA_CARTA_RE = re.compile(
    r"([a-z0-9]{7})\s*·\s*([★☆]{1,5})\s*·\s*#(\d+)\s*·\s*◈(\d+)\s*·\s*(.+?)\s*·\s*(.+?)\s*(?:\n|$)"
)
# "Dropped in damaged condition"
CONDICAO_RE = re.compile(r"Dropped in\s+(\w+)\s+condition", re.IGNORECASE)

# `k!lu <personagem>` (Character Lookup):
#   Character · Adachi Matagorou
#   Series · Hell's Paradise: Jigokuraku
#   Wishlisted · 1
#   Total in circulation · 3,033
#   Claim rate · 31%
DESEJOS_RE = re.compile(r"Wishlisted\s*·\s*([\d,]+)", re.IGNORECASE)
CIRCULACAO_RE = re.compile(r"Total in circulation\s*·\s*([\d,]+)", re.IGNORECASE)
TAXA_RE = re.compile(r"Claim rate\s*·\s*(\d+)\s*%", re.IGNORECASE)
SERIE_RE = re.compile(r"Series\s*[·:]\s*([^\n]+)", re.IGNORECASE)
PERSONAGEM_RE = re.compile(r"Character\s*[·:]\s*([^\n(]+)", re.IGNORECASE)


def _inteiro(txt: str | None) -> int | None:
    if not txt:
        return None
    try:
        return int(txt.replace(",", ""))
    except ValueError:
        return None


def extrair_desejos(texto: str) -> int | None:
    """Quantos jogadores tem o personagem na wishlist (`k!lu`)."""
    m = DESEJOS_RE.search(texto or "")
    return _inteiro(m.group(1)) if m else None


def extrair_circulacao(texto: str) -> int | None:
    m = CIRCULACAO_RE.search(texto or "")
    return _inteiro(m.group(1)) if m else None


def extrair_taxa_reivindicacao(texto: str) -> int | None:
    m = TAXA_RE.search(texto or "")
    return _inteiro(m.group(1)) if m else None


def extrair_serie(texto: str) -> str:
    m = SERIE_RE.search(texto or "")
    return m.group(1).strip() if m else ""


def extrair_personagem(texto: str) -> str:
    m = PERSONAGEM_RE.search(texto or "")
    return m.group(1).strip() if m else ""


# Sufixo de variante que o `k!ci` traz mas o `k!lu` costuma omitir, ex:
# "Flying Boss Type Alca-Noise (Shark)" vs "Flying Boss Type Alca-Noise".
_SUFIXO_VARIANTE_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _normalizar_nome(nome: str) -> str:
    return _SUFIXO_VARIANTE_RE.sub("", (nome or "").strip()).casefold()


def mesmo_personagem(a: str, b: str) -> bool:
    """
    Diz se dois nomes se referem ao mesmo personagem.

    Comparacao normalizada porque `k!ci` e `k!lu` escrevem o nome de formas
    ligeiramente diferentes — o primeiro inclui o sufixo de variante entre
    parenteses, o segundo nao. Comparar literalmente descartava wishlist
    valida (visto em producao com 'Alca-Noise (Shark)').

    Ainda rejeita nomes de fato diferentes: a guarda existe pra nao gravar a
    popularidade do personagem errado.
    """
    na, nb = _normalizar_nome(a), _normalizar_nome(b)
    if not na or not nb:
        return False
    return na == nb or na.startswith(nb) or nb.startswith(na)


# `kb <codigo>` (Burn Card) — o que a carta renderia se queimada:
#   Burn Card
#   @fulano, you will receive:
#    7 Gold
#    1 Dust (★☆☆☆)
# Os emojis 💰/✨ nao aparecem no innerText (viram <img>), entao nao entram
# no regex.
OURO_RE = re.compile(r"([\d,]+)\s*Gold\b", re.IGNORECASE)
POEIRA_RE = re.compile(r"([\d,]+)\s*Dust\s*\(([★☆]+)\)", re.IGNORECASE)

# `k!i` (Inventory) e `k!bi` (Bits) usam EXATAMENTE o mesmo formato de linha:
#   "226 · gold · Gold"  /  "2,659 · clay bit · Clay Bit"
# So muda o rotulo do rodape ("items" vs "bits"), por isso um parser so.
ITEM_INVENTARIO_RE = re.compile(r"^\s*([\d,]+)\s*·\s*([^·\n]+?)\s*·\s*([^\n]+?)\s*$",
                                re.MULTILINE)
# "Showing items 1–10 of 12" / "Showing bits 1–10 of 13" (travessao ou hifen)
PAGINA_INVENTARIO_RE = re.compile(
    r"Showing (?:items|bits)\s+(\d+)\s*[–-]\s*(\d+)\s+of\s+([\d,]+)", re.IGNORECASE
)


@dataclass
class RetornoBurn:
    """O que o `kb` diz que a carta renderia."""

    ouro: int = 0
    poeira: int = 0
    tier: str = ""


def extrair_retorno_burn(texto: str) -> RetornoBurn | None:
    """Le a previa do `kb`. None se o texto nao for 'Burn Card'."""
    if "Burn Card" not in (texto or ""):
        return None
    r = RetornoBurn()
    if (m := OURO_RE.search(texto)) is not None:
        r.ouro = int(m.group(1).replace(",", ""))
    if (m := POEIRA_RE.search(texto)) is not None:
        r.poeira = int(m.group(1).replace(",", ""))
        r.tier = m.group(2)
    return r


def extrair_itens_inventario(texto: str) -> dict[str, tuple[str, int]]:
    """
    Le o `k!i` e devolve {chave: (nome, quantidade)}.

    Ignora as linhas de cabecalho e rodape, que nao tem o formato de tres
    campos separados por '·'.
    """
    itens: dict[str, tuple[str, int]] = {}
    for m in ITEM_INVENTARIO_RE.finditer(texto or ""):
        qtd_txt, chave, nome = m.group(1), m.group(2).strip(), m.group(3).strip()
        if not chave or "Showing" in nome:
            continue
        try:
            itens[chave] = (nome, int(qtd_txt.replace(",", "")))
        except ValueError:
            continue
    return itens


def total_itens_inventario(texto: str) -> int | None:
    """Total segundo o rodape ('Showing items|bits 1-10 of 12')."""
    m = PAGINA_INVENTARIO_RE.search(texto or "")
    return int(m.group(3).replace(",", "")) if m else None


@dataclass
class DetalhesCarta:
    """O que `k!ci` devolve sobre a carta em si."""

    codigo: str = ""
    estrelas: int = 0
    numero_print: int = 0
    edicao: int = 0
    serie: str = ""
    personagem: str = ""
    condicao: str = ""
    bruto: str = field(default="", repr=False)


def extrair_detalhes_carta(texto: str) -> DetalhesCarta | None:
    """Parseia a linha resumo do `k!ci`. None se o texto nao for Card Details."""
    m = LINHA_CARTA_RE.search(texto or "")
    if not m:
        return None
    cond = CONDICAO_RE.search(texto or "")
    return DetalhesCarta(
        codigo=m.group(1),
        estrelas=m.group(2).count("★"),  # ☆ eh vazia, nao conta
        numero_print=int(m.group(3)),
        edicao=int(m.group(4)),
        serie=m.group(5).strip(),
        personagem=m.group(6).strip(),
        condicao=cond.group(1).lower() if cond else "",
    )


@dataclass
class Avaliacao:
    """Resultado da avaliacao de uma carta (os 3 comandos juntos)."""

    codigo: str
    conta_id: str
    # k!wi
    valor_base: int | None = None
    esforco_total: int | None = None
    # k!ci
    estrelas: int = 0
    numero_print: int = 0
    edicao: int = 0
    condicao: str = ""
    # k!lu
    desejos: int | None = None
    circulacao: int | None = None
    taxa_reivindicacao: int | None = None
    # kb (previa de queima — NAO queima, so mostra)
    ouro_burn: int | None = None
    poeira_burn: int | None = None
    poeira_tier: str = ""
    # de qualquer um deles
    personagem: str = ""
    serie: str = ""

    bruto_worker: str = field(default="", repr=False)
    bruto_carta: str = field(default="", repr=False)
    bruto_personagem: str = field(default="", repr=False)
    bruto_burn: str = field(default="", repr=False)

    @property
    def avaliada(self) -> bool:
        """So consideramos avaliada se o worker info respondeu."""
        return self.valor_base is not None or self.esforco_total is not None


@dataclass
class RegrasClassificacao:
    """
    Limiares que decidem em quais tabelas especializadas a carta entra.

    Calibrados com os dados reais das primeiras 18 cartas de drop (2026-08-01),
    que revelaram duas coisas:

    1. `esforco` eh praticamente `2 x valor_base` (35/17, 29/14, 23/10, 4/2,
       0/0), e `ouro_burn` eh ~`4 x valor_base`. Ou seja, os tres medem a
       MESMA dimensao. Manter limiares independentes para eles so cria
       inconsistencia — por isso `valor_base_min_job` acompanha metade de
       `esforco_min_job`.
    2. `desejos` eh o unico eixo realmente independente (popularidade do
       personagem), entao merece criterio proprio.

    Os valores anteriores (esforco 90, desejos 100) eram chute meu: nenhuma
    carta de drop chegava perto, e TUDO caia em burn.
    """

    # p75 do esforco observado (17,5). Slots de job sao poucos, entao faz
    # sentido reservar a tabela pro quartil superior.
    esforco_min_job: int = 18
    # Metade do esforco, pela relacao 2:1. Redundante de proposito: se um dos
    # dois nao parsear, o outro ainda classifica.
    valor_base_min_job: int = 9
    # p90 dos desejos observados (6). Personagem realmente popular no Karuta
    # tem centenas — este eh um piso, nao um teto.
    desejos_min: int = 8
    # Burn agora decide pelo RETORNO real da queima, nao por valor abstrato:
    # 10 de ouro eh menos que o mediano observado (17).
    ouro_max_burn: int = 10
    # Usados so quando o `kb` nao respondeu e nao ha ouro pra comparar.
    valor_base_max_burn: int = 4
    esforco_max_burn: int = 8


def classificar(av: Avaliacao, regras: RegrasClassificacao) -> dict[str, str]:
    """
    Decide os subconjuntos da carta. Retorna {nome: motivo}.
    Uma carta pode ser job E desejada ao mesmo tempo.
    """
    fora: dict[str, str] = {}
    esforco = av.esforco_total or 0
    valor = av.valor_base or 0
    desejos = av.desejos or 0

    motivos_job = []
    if esforco >= regras.esforco_min_job:
        motivos_job.append(f"esforco {esforco} >= {regras.esforco_min_job}")
    if valor >= regras.valor_base_min_job:
        motivos_job.append(f"valor base {valor} >= {regras.valor_base_min_job}")
    if motivos_job:
        fora["job"] = " e ".join(motivos_job)

    if av.desejos is not None and desejos >= regras.desejos_min:
        fora["desejada"] = f"{desejos} desejos >= {regras.desejos_min}"

    if fora:
        return fora  # serve pra algo: nao eh candidata a queima

    # Burn: prefere o ouro real do `kb`; so cai no valor_base/esforco quando
    # o `kb` nao respondeu.
    if av.ouro_burn is not None:
        if av.ouro_burn <= regras.ouro_max_burn:
            fora["burn"] = f"renderia so {av.ouro_burn} de ouro (<= {regras.ouro_max_burn})"
    elif valor <= regras.valor_base_max_burn and esforco <= regras.esforco_max_burn:
        fora["burn"] = (
            f"sem previa de queima; valor base {valor} <= "
            f"{regras.valor_base_max_burn} e esforco {esforco} <= "
            f"{regras.esforco_max_burn}"
        )
    return fora


async def identificar_carta_recente(
    conta: Account,
    limitador: CommandRateLimiter,
    comando_carta: str = "k!ci",
) -> DetalhesCarta | None:
    """
    Descobre qual carta ESTA conta acabou de pegar, mandando `k!ci` SEM
    parametro — o Karuta responde com a ultima carta obtida pelo usuario que
    enviou o comando.

    Por que assim, e nao lendo a confirmacao no canal: as confirmacoes vem
    todas no mesmo canal ("@fulano took the X card") e a unica forma de
    separa-las seria pelo nome de exibicao — que NAO eh unico no Discord.
    Aqui as contas b e c tem o mesmo nome ("willo") apesar de serem contas
    diferentes, e o casamento por nome pegaria a carta errada.

    Com `k!ci` sem parametro cada conta pergunta pela propria carta, e a
    resposta ainda vem completa (codigo, ★, #print, ◈edicao, serie, condicao).
    """
    if not comando_carta.strip():
        return None
    page = await conta.canal_info()
    if not await _send_with_cooldown_retry(
        page, comando_carta, limitador, settle_seconds=3.5
    ):
        return None
    texto = await _latest_karuta_text(
        page, contains_any=["Card Details", "Dropped on", "Owned by"]
    )
    det = extrair_detalhes_carta(texto)
    if det is None:
        logger.debug(f"'{comando_carta}' sem parametro nao parseou: {texto[:200]!r}")
        return None
    det.bruto = texto
    return det


async def avaliar_carta(
    conta: Account,
    detalhes: DetalhesCarta,
    limitador: CommandRateLimiter,
    comando_personagem: str = "k!lu",
    comando_burn: str = "",
) -> Avaliacao:
    """
    Completa a avaliacao da carta ja identificada:

      k!wi <codigo>      -> valor base + esforco  (decide job)
      k!lu <personagem>  -> wishlists, circulacao, claim rate (decide desejada)

    O `k!lu` busca por NOME de personagem, nao por codigo — por isso depende
    do `k!ci` ter rodado antes. Sem nome, a etapa eh pulada em vez de mandar
    busca vazia.

    Nao levanta excecao: o que falhar volta vazio e a carta fica pendente.
    """
    page = await conta.canal_info()
    av = Avaliacao(
        codigo=detalhes.codigo,
        conta_id=conta.id,
        estrelas=detalhes.estrelas,
        numero_print=detalhes.numero_print,
        edicao=detalhes.edicao,
        condicao=detalhes.condicao,
        personagem=detalhes.personagem,
        serie=detalhes.serie,
        bruto_carta=detalhes.bruto,
    )

    # --- worker info: valor base + esforco ---
    if await _send_with_cooldown_retry(
        page, f"k!wi {av.codigo}", limitador, settle_seconds=3.5
    ):
        texto = await _latest_karuta_text(
            page, contains_any=["Worker Details", "Base value", "Effort"]
        )
        av.bruto_worker = texto
        if (m := BASE_VALUE_RE.search(texto)) is not None:
            av.valor_base = int(m.group(1))
        if (m := EFFORT_TOTAL_RE.search(texto)) is not None:
            av.esforco_total = int(m.group(1))
        av.personagem = av.personagem or extrair_personagem(texto)

    # --- character lookup: o quanto o personagem eh desejado ---
    # SEM parametro, de novo pela ultima carta obtida. Passar o nome quebra:
    # `k!lu Sunday` devolve "Character Results" com 34 personagens e espera
    # selecao num menu — o bot fica sem resposta. E o primeiro da lista nao
    # eh o certo (era Nico Robin/One Piece, quando a carta era Sunday/Wish Me
    # Mell), entao "chutar o primeiro" gravaria dado errado.
    if comando_personagem.strip():
        if await _send_with_cooldown_retry(
            page, comando_personagem, limitador, settle_seconds=3.5,
        ):
            texto = await _latest_karuta_text(
                page,
                contains_any=["Character Lookup", "Wishlisted", "Total generated"],
            )
            av.bruto_personagem = texto
            nome_lookup = extrair_personagem(texto)
            # Confere que o lookup fala da MESMA carta que o k!ci trouxe.
            if av.personagem and nome_lookup and not mesmo_personagem(
                nome_lookup, av.personagem
            ):
                conta.log.warning(
                    f"lookup veio de '{nome_lookup}' mas a carta eh "
                    f"'{av.personagem}'; ignorando desejos"
                )
            else:
                av.desejos = extrair_desejos(texto)
                av.circulacao = extrair_circulacao(texto)
                av.taxa_reivindicacao = extrair_taxa_reivindicacao(texto)
                av.serie = av.serie or extrair_serie(texto)
                av.personagem = av.personagem or nome_lookup

    # --- previa de queima: quanto ouro/poeira a carta renderia ---
    # ATENCAO: o `kb` NAO queima. Ele mostra o retorno e deixa dois botoes
    # (confirmar/cancelar) pendentes na mensagem. O bot nunca os clica: seus
    # cliques miram em `reactions` com emoji, e a confirmacao usa <button>.
    if comando_burn.strip():
        if await _send_with_cooldown_retry(
            page, f"{comando_burn} {av.codigo}", limitador, settle_seconds=3.5
        ):
            texto = await _latest_karuta_text(
                page, contains_any=["Burn Card", "you will receive"]
            )
            av.bruto_burn = texto
            if (r := extrair_retorno_burn(texto)) is not None:
                av.ouro_burn = r.ouro
                av.poeira_burn = r.poeira
                av.poeira_tier = r.tier

    if not av.avaliada:
        conta.log.warning(f"avaliacao de {av.codigo} falhou; fica pendente")
    else:
        conta.log.info(
            f"avaliou {av.codigo} ({av.personagem or '?'}): base={av.valor_base} "
            f"esforco={av.esforco_total} desejos={av.desejos} "
            f"★{av.estrelas} #{av.numero_print} ◈{av.edicao}"
            + (f" burn={av.ouro_burn}g+{av.poeira_burn}p" if av.ouro_burn is not None else "")
        )
    return av


async def ler_recursos(
    conta: Account,
    limitador: CommandRateLimiter,
    comando: str = "k!i",
    marcador: str = "Inventory",
    max_paginas: int = 6,
) -> dict[str, tuple[str, int]]:
    """
    Le um inventario de recursos da conta, paginando ate o fim.

    Serve tanto para itens (`k!i`, marcador "Inventory") quanto para bits
    (`k!bi`, marcador "Bits"): o formato das linhas eh o mesmo, so muda o
    cabecalho e o rodape.

    Roda raramente (ver `intervalo_inventario_minutos`): muda devagar e cada
    pagina custa um comando ao Karuta.
    """
    from .discord_page import click_next_page_reaction, find_karuta_message_id

    if not comando.strip():
        return {}
    page = await conta.canal_info()
    if not await _send_with_cooldown_retry(
        page, comando, limitador, settle_seconds=3.0
    ):
        conta.log.warning(f"{marcador}: sem resposta")
        return {}

    msg_id = await find_karuta_message_id(page, marcador)
    if not msg_id:
        conta.log.warning(f"{marcador}: nao achei a mensagem")
        return {}

    itens: dict[str, tuple[str, int]] = {}
    total: int | None = None
    faixa: tuple[int, int] | None = None

    for _ in range(max_paginas):
        try:
            texto = (await page.locator(f'li[id$="-{msg_id}"]').inner_text()) or ""
        except Exception:
            break
        itens.update(extrair_itens_inventario(texto))
        total = total_itens_inventario(texto)
        faixa = _faixa_inventario(texto)

        if total is not None and len(itens) >= total:
            break
        if not await click_next_page_reaction(page, msg_id):
            break
        # Espera a faixa ("Showing items 1-10 of 12") mudar de verdade. Antes
        # havia um sleep fixo de 2s: quando o Karuta demorava mais que isso, a
        # mesma pagina era relida e o inventario ficava truncado em silencio.
        if not await _esperar_pagina_recursos(page, msg_id, faixa):
            conta.log.warning(f"{marcador}: pagina nao atualizou; parando aqui")
            break

    if total is not None and len(itens) < total:
        conta.log.warning(
            f"{marcador}: li {len(itens)} de {total} (paginacao incompleta)"
        )
    else:
        conta.log.info(f"{marcador}: {len(itens)} itens lidos")
    return itens


def _faixa_inventario(texto: str) -> tuple[int, int] | None:
    m = PAGINA_INVENTARIO_RE.search(texto or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


async def _esperar_pagina_recursos(
    page, msg_id: str, faixa_anterior: tuple[int, int] | None,
    timeout_segundos: float = 8.0,
) -> bool:
    """True quando a faixa exibida muda (a pagina virou de fato)."""
    import time as _t

    limite = _t.time() + timeout_segundos
    while _t.time() < limite:
        try:
            texto = (await page.locator(f'li[id$="-{msg_id}"]').inner_text()) or ""
        except Exception:
            return False
        if _faixa_inventario(texto) not in (None, faixa_anterior):
            return True
        await asyncio.sleep(0.5)
    return False
