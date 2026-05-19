from __future__ import annotations

import asyncio
from dataclasses import dataclass

from playwright.async_api import Page

from .discord_page import is_drop_text

# JS injetado na pagina. Observa o container de mensagens com MutationObserver
# e chama __karuta_on_drop(messageId, text) quando ve uma mensagem do bot
# Karuta cujo texto contem "is dropping N cards!".
_OBSERVER_JS = """
(botId) => {
  if (window.__karutaObserverInstalled) return;
  window.__karutaObserverInstalled = true;

  const DROP_RE = /is dropping \\d+ cards?!/i;

  function extractMessageId(node) {
    const li = node.closest && node.closest('li[id^="chat-messages-"]');
    if (!li) return null;
    const parts = li.id.split('-');
    return parts[parts.length - 1];
  }

  function inspect(node) {
    if (!(node instanceof HTMLElement)) return;
    const text = node.innerText || '';
    if (!DROP_RE.test(text)) return;
    const messageId = extractMessageId(node);
    if (!messageId) return;
    if (window.__karutaSeen && window.__karutaSeen.has(messageId)) return;
    (window.__karutaSeen = window.__karutaSeen || new Set()).add(messageId);
    if (window.__karuta_on_drop) {
      window.__karuta_on_drop(messageId, text);
    }
  }

  // Marca todas as mensagens ja existentes no chat como ja vistas, para
  // nao reagirmos a drops antigos quando a pagina carregar.
  window.__karutaSeen = new Set();
  document.querySelectorAll('li[id^="chat-messages-"]').forEach((li) => {
    const parts = li.id.split('-');
    window.__karutaSeen.add(parts[parts.length - 1]);
  });

  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      m.addedNodes.forEach(inspect);
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}
"""


# Discord snowflake epoch: 2015-01-01T00:00:00.000Z em ms
_DISCORD_EPOCH_MS = 1420070400000


def snowflake_to_unix_ms(snowflake: str) -> int:
    """Converte um Discord snowflake (id) para timestamp Unix em ms."""
    try:
        return (int(snowflake) >> 22) + _DISCORD_EPOCH_MS
    except (TypeError, ValueError):
        return 0


@dataclass
class DropEvent:
    message_id: str
    text: str

    @property
    def timestamp_ms(self) -> int:
        return snowflake_to_unix_ms(self.message_id)


class DropDetector:
    """
    Instala um MutationObserver no DOM do Discord e empurra DropEvents
    para uma asyncio.Queue cada vez que aparece uma mensagem de drop.
    """

    def __init__(self, page: Page, karuta_bot_id: str):
        self._page = page
        self._bot_id = karuta_bot_id
        self.queue: asyncio.Queue[DropEvent] = asyncio.Queue()

    async def install(self) -> None:
        await self._page.expose_function("__karuta_on_drop", self._on_drop)
        await self._page.evaluate(_OBSERVER_JS, self._bot_id)

    def _on_drop(self, message_id: str, text: str) -> None:
        if not is_drop_text(text):
            return
        self.queue.put_nowait(DropEvent(message_id=message_id, text=text))

    def clear_queue(self) -> None:
        """Descarta todos os DropEvents pendentes."""
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def wait_for_drop(self, timeout: float, after_ms: int = 0) -> DropEvent | None:
        """
        Aguarda o proximo drop. Se after_ms > 0, ignora drops com timestamp
        anterior a after_ms (snowflake decodificado) e continua esperando.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if after_ms > 0 and event.timestamp_ms < after_ms:
                # drop velho; descarta e segue esperando
                continue
            return event
