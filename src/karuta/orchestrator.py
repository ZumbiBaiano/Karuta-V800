from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from loguru import logger
from playwright.async_api import async_playwright

from .account import Account
from .config import KarutaConfig
from .discord_page import check_cooldowns_via_krm, find_recent_kd_wait

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

    async def run(self) -> None:
        async with async_playwright() as pw:
            try:
                await self._start_all(pw)
                await self._main_loop()
            finally:
                await self._stop_all()

    async def _refresh_cooldowns(self) -> None:
        """
        Cada conta envia `krm` para o Karuta retornar os cooldowns reais
        (grab + drop) e ajusta last_pick_at e kd_wait_until.
        Sequencial para nao floodar o canal com 3 krm simultaneos.
        """
        logger.info("checando cooldowns via 'krm' antes do kd...")
        for acc in self.accounts.values():
            try:
                cd = await check_cooldowns_via_krm(acc.page)
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
                if grab > 0:
                    acc.last_pick_at = now - (self.cfg.pick_cooldown_seconds - grab)
                else:
                    acc.last_pick_at = 0
            if drop is not None:
                acc.kd_wait_until = now + drop if drop > 0 else 0
            acc.log.info(
                f"krm: grab={grab if grab is not None else '?'}s "
                f"drop={drop if drop is not None else '?'}s"
            )

    async def _start_all(self, pw) -> None:
        await asyncio.gather(*(a.start(pw, self.cfg) for a in self.accounts.values()))

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
                await asyncio.sleep(15)
                continue

            await self._wait_until_all_ready()
            # Antes de cada kd, refresca cooldowns reais via krm.
            await self._refresh_cooldowns()
            await self._wait_until_all_ready()
            dropper = self._pick_next_dropper()
            if dropper is None:
                # Todas em cooldown de kd; espera 30s e tenta de novo
                soonest = min(a.kd_wait_until for a in self.accounts.values())
                wait = max(5.0, soonest - time.time())
                logger.info(f"todas as contas em cooldown de kd; aguardando {wait:.0f}s")
                await asyncio.sleep(min(wait, 60))
                continue

            if self.dry_run:
                logger.info(f"[dry-run] esperando drop alheio (sem enviar 'kd')")
                drop = await dropper.wait_drop(timeout=3600)
                if drop:
                    logger.info(f"[dry-run] drop visto: msg={drop.message_id}")
                continue

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
            pick_tasks = []
            for acc, drop in zip(self.accounts.values(), results):
                if drop is None:
                    logger.warning(f"{acc.id}: nao viu o drop a tempo")
                    continue
                pick_tasks.append(acc.pick(drop.message_id))
            await asyncio.gather(*pick_tasks, return_exceptions=True)

            self._next_dropper_idx = (self._next_dropper_idx + 1) % len(self.cfg.rotation_order)
            self._save_state()

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
