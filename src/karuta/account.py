from __future__ import annotations

import time
from dataclasses import dataclass, field

from playwright.async_api import BrowserContext, Page, Playwright
from playwright.async_api import Error as PlaywrightError

from .browser import launch_for_account
from .config import AccountConfig, KarutaConfig
from .discord_page import (
    click_event_icon,
    click_pick_button,
    dump_recent_messages,
    find_event_icons,
    goto_channel,
    send_message,
)
from .drop_detector import DropDetector, DropEvent
from .logging_setup import account_logger


@dataclass
class Account:
    id: str
    cfg: AccountConfig
    button_index: int
    last_pick_at: float = 0.0
    kd_wait_until: float = 0.0  # timestamp ate quando essa conta NAO pode dar kd
    # Timestamp ate quando esta conta NAO consegue pegar carta. Vem do `krm`.
    # Sem grab, clicar na reaction nao rende carta — por isso o drop so sai
    # quando as tres estao com grab pronto.
    grab_wait_until: float = 0.0
    # True depois de um `krm` que respondeu; enquanto for False nao dropamos,
    # pra nao decidir com cooldown desconhecido.
    cooldowns_conhecidos: bool = False
    # Codigo da ultima carta identificada por esta conta. Serve pra detectar
    # pick que falhou sem avisar: se o `k!ci` devolver o mesmo codigo do ciclo
    # anterior, nao veio carta nova.
    ultimo_codigo: str = ""
    context: BrowserContext | None = field(default=None, repr=False)
    # Aba do canal de DROP: `kd`, `krm` e os cliques de pick acontecem aqui.
    page: Page | None = field(default=None, repr=False)
    # Aba do canal de INFO: k!ci, k!wi, k!lu, kb, k!i, k!bi. Quando nao ha
    # canal de info configurado, aponta para a MESMA page do drop.
    info_page: Page | None = field(default=None, repr=False)
    detector: DropDetector | None = field(default=None, repr=False)

    @property
    def log(self):
        return account_logger(self.id)

    async def start(self, pw: Playwright, app_cfg: KarutaConfig) -> None:
        mode = "headless" if app_cfg.headless else "com janela"
        self.log.info(f"abrindo navegador {self.cfg.browser} ({mode})")
        self.context = await launch_for_account(
            pw,
            self.cfg,
            headless=app_cfg.headless,
            carregar_imagens=app_cfg.carregar_imagens,
        )
        if self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = await self.context.new_page()

        self.log.info("indo para o canal de drop")
        await goto_channel(self.page, app_cfg.guild_id, app_cfg.channel_id)

        self.detector = DropDetector(self.page, app_cfg.karuta_bot_id)
        await self.detector.install()

        canal_info = app_cfg.canal_info_id
        if canal_info == app_cfg.channel_id:
            self.info_page = self.page
        else:
            # Aba separada em vez de navegar a mesma page: trocar de canal
            # descartaria o MutationObserver do detector e cegaria o bot pra
            # drops durante toda a avaliacao.
            self.log.info("abrindo aba do canal de info")
            self.info_page = await self.context.new_page()
            await goto_channel(self.info_page, app_cfg.guild_id, canal_info)
            # Devolve o foco ao canal de drop: eh onde a janela de pick eh
            # disputada, e aba em segundo plano perde prioridade de render.
            await self.page.bring_to_front()

        self.log.info("pronto")

    async def stop(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
            self.page = None
            self.info_page = None
            self.detector = None

    async def canal_drops(self) -> Page:
        """
        Aba do canal de drop, trazida para frente.

        Chromium desprioriza aba em segundo plano (timers throttled, render
        pausado). Como as duas abas dividem o mesmo contexto, so uma pode
        estar visivel — entao quem vai usar a aba pede o foco antes.
        """
        assert self.page is not None
        await self._focar(self.page)
        return self.page

    async def canal_info(self) -> Page:
        """Aba do canal de info, trazida para frente. Ver `canal_drops`."""
        assert self.info_page is not None
        await self._focar(self.info_page)
        return self.info_page

    async def _focar(self, page: Page) -> None:
        # Sem canal de info separado as duas referencias sao a mesma aba e
        # nao ha nada a alternar.
        if self.info_page is self.page:
            return
        try:
            await page.bring_to_front()
        except PlaywrightError as e:
            # Nao eh fatal: sem foco a aba so fica mais lenta pra renderizar.
            self.log.debug(f"bring_to_front falhou: {e}")

    async def send_kd(self) -> None:
        self.log.info("enviando 'kd'")
        # `kd` sempre no canal de drop, mesmo com canal de info configurado.
        await send_message(await self.canal_drops(), "kd")

    async def pick(self, message_id: str, timeout_ms: int = 20000) -> bool:
        assert self.page is not None
        # Cooldown comeca no momento da tentativa, pickou ou nao — assim
        # o orquestrador nao queima 30min de drop por nada se o pick falhar.
        self.last_pick_at = time.time()
        ok = await click_pick_button(self.page, message_id, self.button_index, timeout_ms)
        number = self.button_index + 1
        if ok:
            self.log.info(f"picou botao {number} (msg {message_id})")
        else:
            self.log.warning(f"falhou pick botao {number} (msg {message_id})")
            dump_path = f"./logs/dom_dump_{self.id}_{int(time.time())}.html"
            await dump_recent_messages(self.page, dump_path)
            self.log.warning(f"DOM salvo em {dump_path}")
        return ok

    async def event_icons(self, message_id: str) -> list[str]:
        """Nomes das reactions do drop que nao sao carta (icones de evento)."""
        assert self.page is not None
        return await find_event_icons(self.page, message_id)

    async def pick_event_icon(
        self, message_id: str, icon_name: str, timeout_ms: int = 20000
    ) -> bool:
        """Clica o icone de evento. Nao mexe em last_pick_at: o cooldown de
        grab do Karuta vale pra carta, e o icone eh uma coleta separada."""
        assert self.page is not None
        ok = await click_event_icon(self.page, message_id, icon_name, timeout_ms)
        if ok:
            self.log.info(f"pegou icone de evento '{icon_name}' (msg {message_id})")
        else:
            self.log.warning(f"falhou icone de evento '{icon_name}' (msg {message_id})")
        return ok

    async def wait_drop(self, timeout: float, after_ms: int = 0) -> DropEvent | None:
        assert self.detector is not None
        return await self.detector.wait_for_drop(timeout, after_ms=after_ms)

    def clear_drop_queue(self) -> None:
        if self.detector is not None:
            self.detector.clear_queue()

    def cooldown_remaining(self, cooldown_seconds: int) -> float:
        elapsed = time.time() - self.last_pick_at
        return max(0.0, cooldown_seconds - elapsed)
