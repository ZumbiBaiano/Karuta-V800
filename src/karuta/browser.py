from __future__ import annotations

from playwright.async_api import BrowserContext, Playwright

from .config import AccountConfig

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _clean_singleton_locks(profile_dir) -> None:
    """Remove arquivos Singleton* deixados por crash anterior, se houver."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except OSError:
            pass


def _window_position(profile_dir) -> str:
    """
    Posicao inicial da janela, em cascata por conta.

    Sem isso o Chromium escolhe a posicao sozinho e as 3 janelas abrem
    espalhadas de forma imprevisivel (o display do WSLg aqui tem 3840px de
    largura). O offset mantem as tres proximas e sobrepostas so o suficiente
    pra dar pra alternar entre elas.
    """
    suffix = profile_dir.name[-1:].lower()
    slot = {"a": 0, "b": 1, "c": 2}.get(suffix, 0)
    return f"{40 + slot * 60},{40 + slot * 60}"


async def launch_for_account(
    pw: Playwright,
    account: AccountConfig,
    headless: bool = False,
) -> BrowserContext:
    """
    Abre um BrowserContext persistente para a conta.

    - Chrome: usa channel="chrome" se executable_path nao foi dado.
    - Brave / Opera GX: ambos sao Chromium-based, basta apontar executable_path.
    """
    account.profile_dir.mkdir(parents=True, exist_ok=True)
    _clean_singleton_locks(account.profile_dir)

    kwargs: dict = {
        "user_data_dir": str(account.profile_dir),
        "headless": headless,
        "user_agent": USER_AGENT,
        "viewport": {"width": 1280, "height": 800},
        "args": [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            f"--window-position={_window_position(account.profile_dir)}",
            "--window-size=1280,800",
            # WSLg: sem isso a janela abre em "[WARN:COPY MODE]" e fica
            # cinza/sem pintar. Forcar X11 + desligar GPU usa o caminho de
            # render por software, que o WSLg consegue exibir.
            "--ozone-platform=x11",
            "--disable-gpu",
        ],
        "ignore_default_args": ["--enable-automation"],
    }

    if account.executable_path is not None:
        kwargs["executable_path"] = str(account.executable_path)
    # Sem executable_path: usa o Chromium bundle do Playwright.

    return await pw.chromium.launch_persistent_context(**kwargs)
