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
    carregar_imagens: bool = True,
) -> BrowserContext:
    """
    Abre um BrowserContext persistente para a conta.

    - Chrome: usa channel="chrome" se executable_path nao foi dado.
    - Brave / Opera GX: ambos sao Chromium-based, basta apontar executable_path.

    carregar_imagens=False corta o maior custo de memoria do bot: as imagens
    das cartas. O padrao e True porque os fluxos de login PRECISAM delas — o
    login por QR depende do codigo renderizar, e os prints de diagnostico
    ficariam em branco. Quem desliga e o laco principal, que le tudo por
    atributo do DOM e nunca por pixel.
    """
    account.profile_dir.mkdir(parents=True, exist_ok=True)
    _clean_singleton_locks(account.profile_dir)

    args = [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-gpu",
    ]

    if headless:
        # Mesmo com --disable-gpu o Chromium ainda sobe um processo de GPU
        # separado rodando SwiftShader (render por software). Sem janela pra
        # pintar, ele nao serve pra nada e custava ~143 MB por conta.
        args += ["--in-process-gpu", "--disable-software-rasterizer"]
    else:
        # So fazem sentido com janela: no WSLg, sem forcar X11 o Chromium
        # tenta Wayland, nao acha o socket e a janela abre vazia. O
        # window-position evita as 3 janelas espalhadas pelo display.
        args += [
            "--ozone-platform=x11",
            f"--window-position={_window_position(account.profile_dir)}",
            "--window-size=1280,800",
        ]

    if not carregar_imagens:
        args.append("--blink-settings=imagesEnabled=false")

    kwargs: dict = {
        "user_data_dir": str(account.profile_dir),
        "headless": headless,
        "user_agent": USER_AGENT,
        "viewport": {"width": 1280, "height": 800},
        "args": args,
        "ignore_default_args": ["--enable-automation"],
    }

    if account.executable_path is not None:
        kwargs["executable_path"] = str(account.executable_path)
    # Sem executable_path: usa o Chromium bundle do Playwright.

    return await pw.chromium.launch_persistent_context(**kwargs)
