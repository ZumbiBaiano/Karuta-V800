from __future__ import annotations

import time
from dataclasses import dataclass, field

from playwright.async_api import BrowserContext, Page, Playwright

from .browser import launch_for_account
from .config import AccountConfig, KarutaConfig
from .discord_page import click_pick_button, dump_recent_messages, goto_channel, send_message
from .drop_detector import DropDetector, DropEvent
from .logging_setup import account_logger


@dataclass
class Account:
    id: str
    cfg: AccountConfig
    button_index: int
    last_pick_at: float = 0.0
    kd_wait_until: float = 0.0  # timestamp ate quando essa conta NAO pode dar kd
    context: BrowserContext | None = field(default=None, repr=False)
    page: Page | None = field(default=None, repr=False)
    detector: DropDetector | None = field(default=None, repr=False)

    @property
    def log(self):
        return account_logger(self.id)

    async def start(self, pw: Playwright, app_cfg: KarutaConfig) -> None:
        self.log.info(f"abrindo navegador {self.cfg.browser}")
        self.context = await launch_for_account(pw, self.cfg)
        if self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = await self.context.new_page()

        self.log.info("indo para o canal")
        await goto_channel(self.page, app_cfg.guild_id, app_cfg.channel_id)

        self.detector = DropDetector(self.page, app_cfg.karuta_bot_id)
        await self.detector.install()
        self.log.info("pronto")

    async def stop(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
            self.page = None
            self.detector = None

    async def send_kd(self) -> None:
        assert self.page is not None
        self.log.info("enviando 'kd'")
        await send_message(self.page, "kd")

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

    async def wait_drop(self, timeout: float, after_ms: int = 0) -> DropEvent | None:
        assert self.detector is not None
        return await self.detector.wait_for_drop(timeout, after_ms=after_ms)

    def clear_drop_queue(self) -> None:
        if self.detector is not None:
            self.detector.clear_queue()

    def cooldown_remaining(self, cooldown_seconds: int) -> float:
        elapsed = time.time() - self.last_pick_at
        return max(0.0, cooldown_seconds - elapsed)
