from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from loguru import logger
from playwright.async_api import async_playwright

from . import db
from .account import Account
from .avaliador import (
    RegrasClassificacao,
    avaliar_carta,
    classificar,
    identificar_carta_recente,
    ler_recursos,
)
from .config import KarutaConfig
from .discord_page import check_cooldowns_via_krm, find_recent_kd_wait
from .drop_detector import snowflake_to_unix_ms
from .scanner import CommandRateLimiter

STATE_FILE = Path("./state.json")


class Orchestrator:
    def __init__(self, cfg: KarutaConfig, dry_run: bool = False, pick_only: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.pick_only = pick_only
        self.accounts: dict[str, Account] = {
            aid: Account(id=aid, cfg=acc, button_index=cfg.pick_button_index[aid])
            for aid, acc in cfg.accounts.items()
        }
        self._next_dropper_idx = 0
        # Serializa o uso da aba de DROP entre o ciclo principal e a varredura
        # de eventos. Sem isso a varredura poderia clicar numa reaction no meio
        # da janela de pick, que eh disputada e dura poucos segundos.
        self._lock_canal = asyncio.Lock()
        # Drops que a varredura ja inspecionou, para nao reprocessar.
        self._drops_vistos: set[str] = set()

    async def run(self) -> None:
        async with async_playwright() as pw:
            vigia: asyncio.Task | None = None
            try:
                await self._start_all(pw)
                if self.cfg.event_icon.enabled and self.cfg.event_icon.varredura_segundos > 0:
                    vigia = asyncio.create_task(self._vigiar_eventos())
                await self._main_loop()
            finally:
                if vigia is not None:
                    vigia.cancel()
                await self._stop_all()

    async def _vigiar_eventos(self) -> None:
        """
        Varre o canal periodicamente atras de drops com icone de evento.

        Cobre o que o fluxo principal nao ve: drops de OUTROS jogadores e
        drops extras que expiraram antes do proximo ciclo. Como o icone de
        evento nao tem cooldown, cada um desses eh um item de graca — so a
        carta exige grab, e essa nem tentamos.

        Roda em paralelo ao ciclo principal, mas pega `_lock_canal` antes de
        tocar na aba: a janela de pick dura poucos segundos e nao pode ser
        atrapalhada por um clique de varredura.
        """
        from .discord_page import listar_drops_recentes

        ec = self.cfg.event_icon
        grabber = self.accounts.get(ec.grabber)
        if grabber is None:
            logger.warning(f"varredura: grabber '{ec.grabber}' nao existe")
            return

        logger.info(
            f"varredura de eventos ATIVA: a cada {ec.varredura_segundos:.0f}s, "
            f"olhando {ec.varredura_lookback} mensagens, grabber='{ec.grabber}', "
            f"idade max {ec.idade_maxima_drop_segundos:.0f}s"
        )

        while True:
            await asyncio.sleep(ec.varredura_segundos)
            try:
                async with self._lock_canal:
                    page = await grabber.canal_drops()
                    ids = await listar_drops_recentes(page, ec.varredura_lookback)
                    novos = [i for i in ids if i not in self._drops_vistos]
                    self._drops_vistos.update(ids)
                    # Idade vem do snowflake, nao de "ja estava na tela": um
                    # drop de 1min continua vivo mesmo se o bot acabou de subir.
                    agora_ms = time.time() * 1000
                    vivos = [
                        i for i in novos
                        if agora_ms - snowflake_to_unix_ms(i)
                        <= ec.idade_maxima_drop_segundos * 1000
                    ]
                    if novos:
                        # Sem esta linha a varredura era muda quando nao achava
                        # icone, e nao dava pra saber se estava viva.
                        logger.info(
                            f"varredura: {len(novos)} drop(s) novo(s), "
                            f"{len(vivos)} ainda no prazo; {len(ids)} na tela"
                        )
                    for msg_id in vivos:
                        icones = await grabber.event_icons(msg_id)
                        if not icones:
                            continue
                        logger.info(
                            f"varredura: drop {msg_id} tem icone(s) {icones}"
                        )
                        await self._grab_event_icon(msg_id, icones)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — laco supervisor: uma falha
                # de varredura nunca pode derrubar a task e deixar o bot sem
                # vigia de eventos pelo resto da execucao.
                logger.warning(f"varredura de eventos falhou: {e}")

    async def _refresh_cooldowns(self) -> None:
        """
        Cada conta envia `krm` para o Karuta retornar os cooldowns reais
        (grab + drop) e ajusta last_pick_at e kd_wait_until.
        Sequencial para nao floodar o canal com 3 krm simultaneos.

        Roda no canal de DROP de proposito: o `krm` acompanha o `kd`, e a
        passagem por aqui tambem devolve o foco a essa aba antes da janela
        de pick — a avaliacao anterior deixou a aba de info em primeiro plano.
        """
        logger.info("checando cooldowns via 'krm' antes do kd...")
        for acc in self.accounts.values():
            acc.cooldowns_conhecidos = False
            try:
                # Sob o lock: a varredura de eventos usa a mesma aba, e uma
                # reaction clicada no meio do krm bagunçaria a leitura da
                # resposta mais recente do Karuta.
                async with self._lock_canal:
                    cd = await check_cooldowns_via_krm(await acc.canal_drops())
            except Exception as e:
                acc.log.warning(f"falha no krm: {e}")
                continue
            now = time.time()
            grab = cd.get("grab")
            drop = cd.get("drop")
            if grab is None and drop is None:
                acc.log.warning("nao consegui parsear resposta do krm")
                continue
            if grab is not None:
                acc.grab_wait_until = now + grab if grab > 0 else 0.0
                if grab > 0:
                    acc.last_pick_at = now - (self.cfg.pick_cooldown_seconds - grab)
                else:
                    acc.last_pick_at = 0
            if drop is not None:
                acc.kd_wait_until = now + drop if drop > 0 else 0
            acc.cooldowns_conhecidos = grab is not None
            acc.log.info(
                f"krm: grab={grab if grab is not None else '?'}s "
                f"drop={drop if drop is not None else '?'}s"
            )

    def _espera_para_grab_de_todas(self) -> float:
        """
        Segundos ate TODAS as contas terem grab disponivel.

        O drop so vale a pena com as tres podendo pegar carta: sem grab, o
        clique na reaction nao rende nada e a carta daquela conta se perde.
        Conta com cooldown desconhecido conta como indisponivel — melhor
        esperar do que queimar um drop.
        """
        agora = time.time()
        esperas = []
        for acc in self.accounts.values():
            if not acc.cooldowns_conhecidos:
                acc.log.warning("cooldown de grab desconhecido; nao vou dropar")
                return 60.0
            esperas.append(max(0.0, acc.grab_wait_until - agora))
        return max(esperas) if esperas else 0.0

    async def _start_all(self, pw) -> None:
        """
        Sobe as 3 contas. Se alguma falhar, reporta QUAL e por que — antes um
        traceback de timeout escondia que o problema era permissao no Discord.
        """
        # `cartas` referencia `contas`, entao as contas precisam existir antes
        # do primeiro insert. Idempotente.
        with db.opcional(self.cfg.banco.dsn, self.cfg.banco.ativo) as conn:
            if conn is not None:
                try:
                    db.criar_esquema(conn)
                    db.garantir_contas(conn, list(self.accounts))
                except Exception as e:
                    logger.warning(f"nao consegui preparar o banco: {e}")

        # Recarrega os drops ja clicados: `_drops_vistos` vive so em memoria e
        # todo restart o zerava, fazendo o bot reclicar icone recente — o que
        # no Discord REMOVE a reaction e desfaz a coleta.
        with db.opcional(self.cfg.banco.dsn, self.cfg.banco.ativo) as conn:
            if conn is not None:
                try:
                    ja = db.drops_com_icone_ja_clicado(conn, self.cfg.event_icon.grabber)
                    self._drops_vistos.update(ja)
                    if ja:
                        logger.info(
                            f"{len(ja)} drop(s) com icone ja clicado carregados "
                            "do banco; nao serao reclicados"
                        )
                except Exception as e:  # noqa: BLE001 — banco fora do ar nao
                    # pode impedir o bot de subir; sem a lista ele so corre o
                    # risco de reclicar, que a checagem de contagem detecta.
                    logger.warning(f"nao consegui carregar icones ja clicados: {e}")

        accounts = list(self.accounts.values())
        results = await asyncio.gather(
            *(a.start(pw, self.cfg) for a in accounts), return_exceptions=True
        )
        failed = [
            (acc.id, r) for acc, r in zip(accounts, results) if isinstance(r, BaseException)
        ]
        if not failed:
            return
        for aid, err in failed:
            logger.error(f"conta '{aid}' nao subiu: {err}")
        nomes = ", ".join(aid for aid, _ in failed)
        raise RuntimeError(f"conta(s) {nomes} nao conseguiram entrar no canal; abortando")

    async def _stop_all(self) -> None:
        await asyncio.gather(*(a.stop() for a in self.accounts.values()), return_exceptions=True)

    async def _main_loop(self) -> None:
        while True:
            if self.pick_only:
                # Modo de teste: nao envia kd; pega qualquer drop que aparecer.
                logger.info("[pick-only] esperando drop alheio para testar pick...")
                for a in self.accounts.values():
                    a.clear_drop_queue()
                drop_a = await list(self.accounts.values())[0].wait_drop(timeout=3600)
                if drop_a is None:
                    continue
                logger.info(f"[pick-only] drop visto: msg={drop_a.message_id}")
                # Cada conta confirma o drop e pickeia em paralelo
                results = await asyncio.gather(
                    *(a.wait_drop(timeout=2) for a in self.accounts.values())
                )
                pick_tasks = [
                    acc.pick((drop or drop_a).message_id)
                    for acc, drop in zip(self.accounts.values(), results)
                ]
                await asyncio.gather(*pick_tasks, return_exceptions=True)
                # Mesma regra do fluxo normal: so o grabber pega o icone.
                # Cobrir aqui permite testar evento sem esperar o cooldown.
                if self.cfg.event_icon.enabled:
                    await self._grab_event_icon(drop_a.message_id, [])
                await asyncio.sleep(15)
                continue

            await self._wait_until_all_ready()
            # Antes de cada kd, refresca cooldowns reais via krm.
            await self._refresh_cooldowns()

            # Regra central: so dropa com as TRES contas de grab pronto.
            # Dropar sem isso desperdicia o drop — a conta sem grab clica na
            # reaction e nao leva carta.
            espera_grab = self._espera_para_grab_de_todas()
            if espera_grab > 0:
                pendentes = [
                    f"{a.id}:{max(0.0, a.grab_wait_until - time.time()):.0f}s"
                    for a in self.accounts.values()
                    if a.grab_wait_until > time.time()
                ]
                logger.info(
                    f"grab indisponivel em {', '.join(pendentes) or 'alguma conta'}"
                    f" — aguardando {espera_grab:.0f}s antes do kd"
                )
                await asyncio.sleep(min(espera_grab + 2, 120))
                continue

            dropper = self._pick_next_dropper()
            if dropper is None:
                # Todas em cooldown de kd: dorme o tempo INTEIRO que falta.
                #
                # Antes dormia `min(wait, 60)` e caia no `continue`, que refaz
                # `_refresh_cooldowns` — ou seja, 3 `krm` por minuto durante os
                # ~19min de espera. Em 2026-08-02 20:10 isso passou de 50 krm
                # seguidos e o Karuta parou de responder (anti-spam); o bot
                # ficou preso em "cooldown de grab desconhecido" sem dropar.
                #
                # O valor vem do proprio Karuta via `krm`, entao da pra confiar
                # nele. O teto de 30min eh so sanidade: eh o cooldown de drop,
                # nenhuma espera legitima passa disso.
                soonest = min(a.kd_wait_until for a in self.accounts.values())
                wait = max(5.0, soonest - time.time())
                logger.info(f"todas as contas em cooldown de kd; aguardando {wait:.0f}s")
                await asyncio.sleep(min(wait + 2, 30 * 60))
                continue

            if self.dry_run:
                logger.info(f"[dry-run] esperando drop alheio (sem enviar 'kd')")
                drop = await dropper.wait_drop(timeout=3600)
                if drop:
                    logger.info(f"[dry-run] drop visto: msg={drop.message_id}")
                continue

            # Todo o trecho kd -> pick -> icone fica sob o lock: a varredura de
            # eventos usa a mesma aba e um clique dela no meio da janela de
            # pick (poucos segundos, disputada) custaria o drop.
            async with self._lock_canal:
                logger.info(f"vez de '{dropper.id}' enviar kd")
                # Limpa filas dos detectores para descartar drops alheios
                # acumulados antes do nosso kd
                for a in self.accounts.values():
                    a.clear_drop_queue()
                # Marca o instante imediatamente antes do kd; usado para filtrar
                # drops por timestamp de snowflake.
                kd_sent_ms = int(time.time() * 1000) - 500  # 0.5s de folga
                await dropper.send_kd()
                # Cooldown de drop do Karuta eh 30min; previne tentativas paralelas
                dropper.kd_wait_until = time.time() + 30 * 60

                # Espera as 3 contas detectarem o drop (apenas drops apos kd_sent_ms)
                results = await asyncio.gather(
                    *(
                        a.wait_drop(timeout=self.cfg.drop_timeout_seconds, after_ms=kd_sent_ms)
                        for a in self.accounts.values()
                    )
                )
                ids = {d.message_id for d in results if d}
                if not ids:
                    # Sem drop: pode ser cooldown do Karuta. Le a resposta dele.
                    wait_s = await find_recent_kd_wait(dropper.page)
                    if wait_s is not None:
                        dropper.kd_wait_until = time.time() + wait_s + 5
                        logger.warning(
                            f"{dropper.id}: Karuta diz para esperar {wait_s}s "
                            f"(cooldown ativo). pulando para proxima conta."
                        )
                    else:
                        logger.error("nenhuma conta detectou o drop dentro do timeout (motivo desconhecido)")
                    # Avanca rotacao independente de o motivo
                    self._next_dropper_idx = (self._next_dropper_idx + 1) % len(self.cfg.rotation_order)
                    self._save_state()
                    await asyncio.sleep(3)
                    continue
                if len(ids) > 1:
                    logger.warning(f"contas viram message_ids diferentes: {ids}; usando o do dropper")

                # Picks em paralelo: cada conta usa wait_for(timeout=20s) para
                # tolerar reactions que aparecem com delay. Janela de pick do
                # Karuta eh ~30s; sequencial com 15s entre cada estourava.
                drops_by_id = {
                    acc.id: drop for acc, drop in zip(self.accounts.values(), results)
                }
                own = drops_by_id.get(dropper.id)
                drop_id = (own or next(d for d in results if d)).message_id

                ec = self.cfg.event_icon
                # No modo "instead" precisamos saber do icone ANTES de escolher o
                # pick: o grabber abre mao da carta. No modo "extra" nao precisa,
                # entao nao atrasamos os picks — a janela de pick eh disputada.
                icons: list[str] = []
                if ec.enabled and ec.mode == "instead":
                    icons = await self._poll_event_icons(drop_id, attempts=2, interval=1.0)

                pick_tasks = []
                pickers: list[Account] = []
                for acc, drop in zip(self.accounts.values(), results):
                    if drop is None:
                        logger.warning(f"{acc.id}: nao viu o drop a tempo")
                        continue
                    if icons and ec.enabled and ec.mode == "instead" and acc.id == ec.grabber:
                        acc.log.info("evento: abrindo mao da carta para pegar o icone")
                        continue
                    pick_tasks.append(acc.pick(drop.message_id))
                    pickers.append(acc)
                ok_por_conta = await asyncio.gather(*pick_tasks, return_exceptions=True)

                if ec.enabled:
                    await self._grab_event_icon(drop_id, icons)
                    if ec.drops_extras:
                        await self._pegar_icones_de_drops_extras(drop_id, kd_sent_ms)
                    # Nosso proprio drop ja foi tratado; a varredura nao precisa
                    # reabrir ele depois.
                    self._drops_vistos.add(drop_id)

            # Avaliacao + gravacao acontecem aqui, dentro do intervalo ocioso
            # antes do proximo kd.
            await self._registrar_e_avaliar(drop_id, pickers, ok_por_conta)

            self._next_dropper_idx = (self._next_dropper_idx + 1) % len(self.cfg.rotation_order)
            self._save_state()

    async def _registrar_e_avaliar(
        self, drop_id: str, pickers: list[Account], resultados: list
    ) -> None:
        """
        Depois dos picks: grava os picks NA HORA, depois descobre qual carta
        cada conta levou, avalia (k!ci + k!wi + k!lu) e grava CADA carta assim
        que a avaliacao dela termina.

        Gravacao incremental de proposito. Antes tudo era gravado num bloco no
        fim, depois das 3 contas; como a avaliacao leva ~100s, qualquer
        interrupcao no meio perdia ate os picks, que ja eram conhecidos.

        Roda no intervalo entre drops: o cooldown de pick eh ~11min, entao nao
        competimos com a janela de pick, que eh disputada.

        Sequencial entre contas para nao mandar varios comandos de uma vez no
        canal; o Karuta responde com anti-spam se floodar.
        """
        if not pickers:
            return

        # 1) Picks: conhecidos agora, gravados agora. Nao dependem da avaliacao.
        ids_pick = self._gravar_picks(drop_id, pickers, resultados)

        cfg_av = self.cfg.avaliacao
        if not cfg_av.ativa:
            return

        limitador = CommandRateLimiter(min_gap_seconds=cfg_av.intervalo_comandos)
        regras = RegrasClassificacao(
            esforco_min_job=cfg_av.esforco_min_job,
            valor_base_min_job=cfg_av.valor_base_min_job,
            desejos_min=cfg_av.desejos_min,
            ouro_max_burn=cfg_av.ouro_max_burn,
            valor_base_max_burn=cfg_av.valor_base_max_burn,
            esforco_max_burn=cfg_av.esforco_max_burn,
        )

        for acc, res in zip(pickers, resultados):
            if res is not True:
                continue
            # `k!ci` sem parametro devolve a ultima carta DESTA conta. Nao da
            # pra ler as confirmacoes do canal: elas identificam por nome de
            # exibicao, que nao eh unico — b e c se chamam "willo" apesar de
            # serem contas diferentes.
            det = await identificar_carta_recente(
                acc, limitador, comando_carta=cfg_av.comando_carta
            )
            if det is None:
                acc.log.warning("nao consegui identificar a carta pega")
                continue
            # Guarda contra pick que falhou sem avisar: o `k!ci` devolveria a
            # carta do ciclo anterior e gravariamos duplicado.
            if det.codigo == acc.ultimo_codigo:
                acc.log.warning(
                    f"k!ci devolveu {det.codigo}, a mesma do ciclo anterior — "
                    "provavelmente nao pegou carta nova; ignorando"
                )
                continue
            acc.ultimo_codigo = det.codigo

            av = await avaliar_carta(
                acc, det, limitador,
                comando_personagem=cfg_av.comando_personagem,
                comando_burn=cfg_av.comando_burn,
            )
            grupos = classificar(av, regras)
            self._gravar_carta(drop_id, acc, det, av, grupos, ids_pick.get(acc.id))
            if grupos:
                acc.log.info(
                    f"{av.codigo} classificada como "
                    f"{', '.join(grupos)} ({'; '.join(grupos.values())})"
                )
            else:
                acc.log.info(f"{av.codigo} ficou so na tabela mestre")

        # Ainda dentro da janela ociosa: atualiza inventarios vencidos.
        await self._atualizar_inventarios()

    async def _atualizar_inventarios(self) -> None:
        """
        Le o inventario das contas cujo dado esta velho.

        Roda depois da avaliacao, ainda dentro da janela ociosa entre drops.
        Nao roda a cada ciclo de proposito: materiais mudam devagar e o
        inventario eh paginado, custando varios comandos por conta.
        """
        cfg_av = self.cfg.avaliacao
        minutos = cfg_av.intervalo_inventario_minutos
        if minutos <= 0:
            return

        # (tipo, comando, marcador do cabecalho da resposta)
        fontes = [
            ("item", cfg_av.comando_inventario, "Inventory"),
            ("bit", cfg_av.comando_bits, "Bits"),
        ]
        limitador = CommandRateLimiter(min_gap_seconds=cfg_av.intervalo_comandos)

        for tipo, comando, marcador in fontes:
            if not comando.strip():
                continue
            with db.opcional(self.cfg.banco.dsn, self.cfg.banco.ativo) as conn:
                if conn is None:
                    return
                try:
                    vencidas = [
                        acc for acc in self.accounts.values()
                        if db.inventario_desatualizado(conn, acc.id, minutos, tipo)
                    ]
                except Exception as e:
                    logger.warning(f"nao consegui checar validade de {tipo}: {e}")
                    continue
            if not vencidas:
                continue

            for acc in vencidas:
                try:
                    itens = await ler_recursos(
                        acc, limitador, comando=comando, marcador=marcador
                    )
                except Exception as e:
                    acc.log.warning(f"falha lendo {marcador}: {e}")
                    continue
                with db.opcional(self.cfg.banco.dsn, self.cfg.banco.ativo) as conn:
                    if conn is None:
                        return
                    try:
                        if itens:
                            db.salvar_inventario(conn, acc.id, itens, tipo=tipo)
                            acc.log.info(f"{marcador} gravado ({len(itens)} itens)")
                        # Registra a leitura mesmo vazia: conta sem inventario
                        # nao gera linha em `inventario` e seria reconsultada
                        # a cada ciclo pra sempre.
                        db.registrar_leitura_inventario(
                            conn, acc.id, tipo, len(itens)
                        )
                    except Exception as e:
                        acc.log.warning(f"falha gravando {marcador}: {e}")

    def _gravar_picks(
        self, drop_id: str, pickers: list[Account], resultados: list
    ) -> dict[str, int]:
        """Grava os picks e devolve {conta_id: id_da_linha}."""
        ids: dict[str, int] = {}
        with db.opcional(self.cfg.banco.dsn, self.cfg.banco.ativo) as conn:
            if conn is None:
                return ids
            for acc, res in zip(pickers, resultados):
                try:
                    pid = db.registrar_pick(
                        conn,
                        conta_id=acc.id,
                        mensagem_drop_id=drop_id,
                        indice_botao=acc.button_index,
                        sucesso=res is True,
                    )
                    if pid is not None:
                        ids[acc.id] = pid
                except Exception as e:
                    acc.log.warning(f"falha gravando pick: {e}")
        return ids

    def _gravar_carta(
        self, drop_id: str, acc: Account, det, av, grupos: dict, pick_id: int | None
    ) -> None:
        """Grava uma carta avaliada: bruto, mestre, classificacao e o pick."""
        cfg_av = self.cfg.avaliacao
        with db.opcional(self.cfg.banco.dsn, self.cfg.banco.ativo) as conn:
            if conn is None:
                return
            try:
                # Uma linha por comando, inclusive os que falharam (bruto
                # vazio) — assim da pra ver POR QUE faltou um dado.
                for comando, bruto, extraido in (
                    ("k!wi", av.bruto_worker, {
                        "valor_base": av.valor_base,
                        "esforco_total": av.esforco_total,
                    }),
                    (cfg_av.comando_carta, av.bruto_carta, {
                        "estrelas": av.estrelas,
                        "numero_print": av.numero_print,
                        "edicao": av.edicao,
                        "condicao": av.condicao,
                        "serie": av.serie,
                    }),
                    (cfg_av.comando_personagem, av.bruto_personagem, {
                        "desejos": av.desejos,
                        "circulacao": av.circulacao,
                        "taxa_reivindicacao": av.taxa_reivindicacao,
                    }),
                    (cfg_av.comando_burn, av.bruto_burn, {
                        "ouro": av.ouro_burn,
                        "poeira": av.poeira_burn,
                        "tier": av.poeira_tier,
                    }),
                ):
                    db.registrar_avaliacao_bruta(
                        conn, av.codigo, acc.id, comando, bruto,
                        {k: v for k, v in extraido.items() if v not in (None, "")},
                    )
                db.salvar_carta(
                    conn,
                    codigo=av.codigo,
                    conta_id=acc.id,
                    personagem=av.personagem or det.personagem,
                    serie=av.serie,
                    estrelas=av.estrelas,
                    numero_print=av.numero_print,
                    edicao=av.edicao,
                    condicao=av.condicao or det.condicao,
                    valor_base=av.valor_base,
                    esforco_total=av.esforco_total,
                    desejos=av.desejos,
                    circulacao=av.circulacao,
                    taxa_reivindicacao=av.taxa_reivindicacao,
                    ouro_burn=av.ouro_burn,
                    poeira_burn=av.poeira_burn,
                    poeira_tier=av.poeira_tier,
                    origem="drop",
                    mensagem_drop_id=drop_id,
                )
                if grupos:
                    db.classificar(
                        conn,
                        codigo=av.codigo,
                        conta_id=acc.id,
                        classificacoes=grupos,
                        valor_base=av.valor_base or 0,
                        esforco_total=av.esforco_total or 0,
                        desejos=av.desejos or 0,
                        personagem=av.personagem or det.personagem,
                        serie=av.serie,
                    )
                if pick_id is not None:
                    db.completar_pick(
                        conn, pick_id, av.codigo,
                        av.personagem or det.personagem, av.condicao,
                    )
            except Exception as e:
                acc.log.warning(f"falha gravando carta {av.codigo}: {e}")

    async def _poll_event_icons(
        self, drop_message_id: str, attempts: int = 6, interval: float = 1.5
    ) -> list[str]:
        """
        Procura icones de evento no drop, repetindo porque o Karuta adiciona as
        reactions em sequencia — na primeira olhada elas podem nao estar todas
        no DOM ainda.
        """
        grabber = self.accounts.get(self.cfg.event_icon.grabber)
        if grabber is None or grabber.page is None:
            return []
        for _ in range(attempts):
            try:
                icons = await grabber.event_icons(drop_message_id)
            except Exception as e:
                grabber.log.warning(f"falha ao ler reactions do drop: {e}")
                return []
            if icons:
                return icons
            await asyncio.sleep(interval)
        return []

    async def _pegar_icones_de_drops_extras(
        self, drop_principal_id: str, kd_sent_ms: int
    ) -> None:
        """
        Pega icones de evento nos drops EXTRAS que o Karuta solta junto do nosso.

        Por que existe: `wait_drop` consome UM evento por conta, e o resto da
        fila era descartado no `clear_drop_queue` do ciclo seguinte. Um drop
        extra do Karuta passava batido — visto em 2026-08-06 00:40, onde o
        canal mostrou dois drops e o bot registrou so um.

        So o ICONE eh pego, nunca a carta: o grab de carta ja foi gasto no
        drop principal e clicar sem grab nao rende nada. O icone de evento nao
        tem cooldown, entao cada drop extra vale um item a mais.

        Roda ANTES da avaliacao de proposito — ela leva ~2min e o drop expira
        muito antes disso.
        """
        from .discord_page import listar_drops_recentes

        ec = self.cfg.event_icon
        grabber = self.accounts.get(ec.grabber)
        if grabber is None or grabber.page is None:
            return

        # Varre o DOM em vez de esperar no DropDetector: o observer so dispara
        # com a frase "is dropping N cards!", e o drop extra do Karuta nao a
        # traz (visto em 2026-08-06 20:32). Pelo detector ele nunca chegaria.
        limite = time.time() + ec.janela_drops_extras
        # Parte de `_drops_vistos`, o registro que atravessa os ciclos. Antes
        # este set nascia so com o drop principal, e um drop antigo ainda na
        # tela era redetectado como "extra" a cada kd — visto em 2026-08-06
        # 21:16, reclicando o drop das 21:05. Clique repetido no Discord
        # REMOVE a reaction, ou seja, desfazia a propria captura.
        vistos = set(self._drops_vistos)
        vistos.add(drop_principal_id)
        while time.time() < limite:
            await asyncio.sleep(2.0)
            try:
                ids = await listar_drops_recentes(
                    grabber.page, ec.varredura_lookback
                )
            except Exception as e:  # noqa: BLE001 — nunca derrubar o ciclo
                logger.warning(f"busca de drop extra falhou: {e}")
                return
            agora_ms = time.time() * 1000
            for msg_id in ids:
                if msg_id in vistos:
                    continue
                vistos.add(msg_id)
                self._drops_vistos.add(msg_id)
                # Mesma guarda de idade da varredura: drop velho ja expirou e
                # clicar nele so gera ruido.
                idade = (agora_ms - snowflake_to_unix_ms(msg_id)) / 1000
                if idade > ec.idade_maxima_drop_segundos:
                    logger.info(f"drop {msg_id} tem {idade:.0f}s; velho demais")
                    continue
                logger.info(f"drop EXTRA detectado: {msg_id} ({idade:.0f}s)")
                # icons=[] faz o _grab_event_icon procurar sozinho; sem icone
                # ele sai sem clicar nada.
                await self._grab_event_icon(msg_id, [])

    async def _grab_event_icon(self, drop_message_id: str, icons: list[str]) -> None:
        """
        Se o drop trouxe icone de evento, so o grabber configurado clica nele.
        As outras contas ignoram de proposito — foi o pedido: apenas uma conta
        deve ficar com o icone.
        """
        grabber = self.accounts.get(self.cfg.event_icon.grabber)
        if grabber is None or grabber.page is None:
            logger.warning(
                f"event_icon.grabber='{self.cfg.event_icon.grabber}' nao existe; pulando"
            )
            return
        if not icons:
            icons = await self._poll_event_icons(drop_message_id)
        if not icons:
            return  # drop normal, sem evento
        logger.info(f"evento detectado no drop {drop_message_id}: icone(s) {icons}")
        pegos = []
        for name in icons:
            pegos.append((name, await grabber.pick_event_icon(drop_message_id, name)))

        with db.opcional(self.cfg.banco.dsn, self.cfg.banco.ativo) as conn:
            if conn is None:
                return
            try:
                for name, ok in pegos:
                    db.registrar_icone_evento(
                        conn, grabber.id, drop_message_id, name, ok
                    )
            except Exception as e:
                logger.warning(f"falha gravando icone de evento: {e}")

    def _pick_next_dropper(self) -> Account | None:
        """
        Avanca _next_dropper_idx ate achar uma conta que possa dar kd
        (kd_wait_until expirado). Retorna None se todas estao em cooldown.
        """
        n = len(self.cfg.rotation_order)
        now = time.time()
        for _ in range(n):
            aid = self.cfg.rotation_order[self._next_dropper_idx]
            acc = self.accounts[aid]
            if acc.kd_wait_until <= now:
                return acc
            self._next_dropper_idx = (self._next_dropper_idx + 1) % n
        return None

    async def _wait_until_all_ready(self) -> None:
        while True:
            remaining = max(
                a.cooldown_remaining(self.cfg.pick_cooldown_seconds)
                for a in self.accounts.values()
            )
            if remaining <= 0:
                return
            logger.info(f"cooldown: maior restante = {remaining:.0f}s; aguardando...")
            await asyncio.sleep(min(remaining, 30))

    def _save_state(self) -> None:
        state = {
            "next_dropper_idx": self._next_dropper_idx,
            "last_pick_at": {aid: a.last_pick_at for aid, a in self.accounts.items()},
        }
        STATE_FILE.write_text(json.dumps(state, indent=2))
