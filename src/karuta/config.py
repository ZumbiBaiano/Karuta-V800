from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BrowserKind = Literal["chrome", "brave", "opera_gx"]
AccountId = Literal["a", "b", "c"]


class AccountConfig(BaseModel):
    browser: BrowserKind
    profile_dir: Path
    executable_path: Path | None = None

    @field_validator("profile_dir", "executable_path", mode="before")
    @classmethod
    def _expand(cls, v):
        if v is None:
            return None
        return Path(os.path.expanduser(str(v)))


class EventIconConfig(BaseModel):
    """
    Durante eventos o Karuta adiciona um 4o icone no drop, alem das 3 cartas.
    Aqui define quem pega e se pega alem da carta ou no lugar dela.
    """

    enabled: bool = True
    # Conta que clica no icone. Somente ela tenta — as outras ignoram.
    grabber: AccountId = "a"
    # "extra"   -> grabber pega a carta dela E o icone
    # "instead" -> grabber pega SO o icone, abrindo mao da carta
    mode: Literal["extra", "instead"] = "extra"
    # O Karuta as vezes solta um drop EXTRA logo depois do nosso `kd`. Nao da
    # pra tirar carta dele (o grab ja foi usado no drop principal), mas o
    # icone de evento NAO tem cooldown — entao cada drop extra eh um item a
    # mais de graca para o grabber.
    drops_extras: bool = True
    # Quanto esperar por esse drop extra depois do principal.
    #
    # Era 12s e nao bastava: em 2026-08-06 21:05 o drop principal saiu as
    # :12 e o EXTRA so as :29 — 17s depois, ja fora da janela. Quem acabou
    # clicando foi a varredura de 60s, aos :56, e ai o Karuta nao deu mais o
    # item (o do drop principal, clicado 6s apos o post, funcionou).
    #
    # 60s cobre folgado esse atraso. Custa adiar a avaliacao das cartas no
    # mesmo tanto, o que eh irrelevante: ha ~11min ociosos ate o proximo kd.
    janela_drops_extras: float = 60.0
    # Varredura periodica do canal atras de QUALQUER drop com icone de evento,
    # inclusive de outros jogadores. Carta desses drops nao da pra pegar (grab
    # em cooldown), mas o icone nao tem cooldown nenhum. 0 desativa.
    varredura_segundos: float = 60.0
    # Quantas mensagens do canal olhar em cada varredura.
    varredura_lookback: int = 15
    # Idade maxima de um drop para a varredura ainda tentar o icone. Drops
    # velhos ja expiraram e clicar neles so gera ruido. Usa o timestamp
    # embutido no snowflake, entao vale inclusive na 1a passada apos subir —
    # foi assim que o drop de 2026-08-06 20:47 se perdeu: estava na tela ha
    # 1min, vivo, e a regra antiga de "1a passada nao clica" o descartou.
    idade_maxima_drop_segundos: float = 180.0


class BancoConfig(BaseModel):
    """
    PostgreSQL. Se `ativo` for false, ou o banco estiver fora do ar, o bot
    continua pegando cartas normalmente e so nao registra nada.
    """

    ativo: bool = True
    # Pode ser sobrescrito pela env KARUTA_DB_DSN (util pra manter a senha
    # fora do arquivo).
    dsn: str = "postgresql://postgres:password@localhost:5432/Karuta"


class AvaliacaoConfig(BaseModel):
    """
    Avaliacao das cartas pegas, feita no intervalo entre um kd e o proximo.

    Os limiares decidem em quais tabelas especializadas a carta entra
    (cartas_job / cartas_desejadas / cartas_burn).
    """

    ativa: bool = True
    # Comandos confirmados via `k!help` (atalhos oficiais do Karuta):
    #   k!ci = cardinfo  -> ★, #print, ◈edicao, serie, condicao
    #   k!lu = lookup    -> wishlists, circulacao, claim rate (por personagem)
    # Deixar vazio pula a etapa. NAO usar `k!i`: eh o inventario.
    comando_carta: str = "k!ci"
    comando_personagem: str = "k!lu"
    # Previa de queima: mostra ouro/poeira que a carta renderia. NAO queima —
    # deixa dois botoes pendentes que o bot nunca clica. Vazio desativa.
    comando_burn: str = "kb"
    # Inventario (materiais da conta). Muda devagar, entao roda num intervalo
    # longo em vez de a cada drop. 0 desativa.
    comando_inventario: str = "k!i"
    # Bits sao um inventario separado (k!bits, atalho k!bi), com o mesmo
    # formato de linha do k!i.
    comando_bits: str = "k!bi"
    intervalo_inventario_minutos: int = 60
    # Segundos entre comandos enviados ao Karuta (rate limit ~10s).
    intervalo_comandos: float = 11.0

    esforco_min_job: int = 18
    valor_base_min_job: int = 9
    desejos_min: int = 8
    ouro_max_burn: int = 10
    valor_base_max_burn: int = 4
    esforco_max_burn: int = 8


class KarutaConfig(BaseModel):
    guild_id: str
    # Canal do drop: eh nele que rodam `kd`, `krm` e os cliques de pick.
    channel_id: str
    # Canal separado para os comandos de INFORMACAO (k!ci, k!wi, k!lu, kb,
    # k!i, k!bi). Manter isso fora do canal de drop deixa o historico do drop
    # limpo — la ficam so os `kd` e as reactions disputadas.
    # Vazio = usa o mesmo canal do drop (comportamento antigo).
    info_channel_id: str = ""
    karuta_bot_id: str = "646937666251915264"
    pick_cooldown_seconds: int = 660
    drop_timeout_seconds: int = 8
    # Sob WSLg em copy mode a janela do Chromium abre mas nao pinta nada, e
    # o bot nao precisa de janela pra funcionar. Headless tambem deixa rodar
    # como service sem depender de X/WSLg.
    headless: bool = True
    # Imagens das cartas sao o maior custo de memoria do bot e ele nao olha
    # pixel nenhum: le reacoes e textos por atributo do DOM. Ligue so pra
    # depurar com print legivel.
    carregar_imagens: bool = False
    rotation_order: list[AccountId] = Field(default_factory=lambda: ["a", "b", "c"])
    pick_button_index: dict[AccountId, int]
    event_icon: EventIconConfig = Field(default_factory=EventIconConfig)
    banco: BancoConfig = Field(default_factory=BancoConfig)
    avaliacao: AvaliacaoConfig = Field(default_factory=AvaliacaoConfig)
    accounts: dict[AccountId, AccountConfig]

    @field_validator("pick_button_index")
    @classmethod
    def _validate_buttons(cls, v):
        if sorted(v.values()) != [0, 1, 2]:
            raise ValueError("pick_button_index deve cobrir exatamente {0, 1, 2}")
        return v

    @property
    def canal_info_id(self) -> str:
        """
        Canal onde os comandos de informacao rodam.

        Cai no canal de drop quando `info_channel_id` esta vazio, para que
        um config antigo continue funcionando sem alteracao.
        """
        return self.info_channel_id.strip() or self.channel_id


class Settings(BaseSettings):
    """Variaveis de ambiente (.env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="KARUTA_",
        extra="ignore",
    )

    config: Path = Path("./config.yaml")
    log_level: str = "INFO"


def load_config(path: Path | None = None) -> KarutaConfig:
    settings = Settings()
    cfg_path = path or settings.config
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Config nao encontrado em {cfg_path}. "
            "Copie config.example.yaml para config.yaml e ajuste."
        )
    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return KarutaConfig.model_validate(raw)
