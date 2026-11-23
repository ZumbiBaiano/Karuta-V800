import textwrap
from pathlib import Path

from karuta.config import KarutaConfig, load_config
from karuta.discord_page import event_icon_names, reaction_name


def test_reaction_name_pega_antes_da_virgula():
    assert reaction_name("one, 2 reactions, press to react") == "one"
    assert reaction_name("pumpkin, 1 reaction") == "pumpkin"
    assert reaction_name("") == ""


# Reactions como o DOM real do Discord devolve (capturado do drop
# 1533203752293765221, com a conta em pt-BR).
REACOES_REAIS = [
    {"emoji": "1️⃣", "label": "one, 2 reações, pressione para remover sua reação"},
    {"emoji": "2️⃣", "label": "two, 2 reações, pressione para reagir"},
    {"emoji": "3️⃣", "label": "three, 2 reações, pressione para reagir"},
]


def test_drop_normal_nao_tem_icone_de_evento():
    assert event_icon_names(REACOES_REAIS) == []


def test_labels_em_portugues_nao_viram_falso_positivo():
    """
    Regressao do primeiro run real: com o label 'clique para reagir com two'
    o parser antigo tratava as proprias cartas como icone de evento.
    """
    reacoes = [
        {"emoji": "1️⃣", "label": "clique para remover one"},
        {"emoji": "2️⃣", "label": "clique para reagir com two"},
        {"emoji": "3️⃣", "label": "clique para reagir com three"},
    ]
    assert event_icon_names(reacoes) == []


def test_drop_de_evento_expoe_o_quarto_icone():
    reacoes = [*REACOES_REAIS, {"emoji": "🎃", "label": "pumpkin, 1 reação"}]
    assert event_icon_names(reacoes) == ["🎃"]


def test_nao_repete_icone_duplicado():
    reacoes = [{"emoji": "⭐", "label": "star, 1"}, {"emoji": "⭐", "label": "star, 2"}]
    assert event_icon_names(reacoes) == ["⭐"]


def test_reaction_sem_emoji_cai_no_nome_do_label():
    """Se o <img> nao vier, ainda dá pra descartar carta pelo nome."""
    reacoes = [{"emoji": "", "label": "one, 1 reação"}]
    assert event_icon_names(reacoes) == []


def _cfg(extra: str = "") -> str:
    return textwrap.dedent(
        f"""
        guild_id: "111"
        channel_id: "222"
        pick_button_index: {{a: 0, b: 1, c: 2}}
        {extra}
        accounts:
          a: {{browser: chrome, profile_dir: ./p/a}}
          b: {{browser: brave, profile_dir: ./p/b}}
          c: {{browser: opera_gx, profile_dir: ./p/c}}
        """
    )


def test_event_icon_tem_default_sem_precisar_declarar():
    cfg = KarutaConfig.model_validate(__import__("yaml").safe_load(_cfg()))
    assert cfg.event_icon.enabled is True
    assert cfg.event_icon.grabber == "a"
    assert cfg.event_icon.mode == "extra"


def test_event_icon_lido_do_yaml(tmp_path: Path, monkeypatch):
    extra = "event_icon: {enabled: true, grabber: b, mode: instead}"
    p = tmp_path / "config.yaml"
    p.write_text(_cfg(extra))
    monkeypatch.chdir(tmp_path)
    cfg = load_config(p)
    assert cfg.event_icon.grabber == "b"
    assert cfg.event_icon.mode == "instead"


# ---------------------------------------------------------------------------
# Deteccao de drop pelas reactions (em vez do texto).
#
# Regressao de 2026-08-06: o Karuta soltou um drop EXTRA que so tinha imagem e
# reactions (1/2/3 + 🍉), sem a frase "is dropping N cards!". A varredura
# filtrava por texto e ignorava exatamente o drop que tinha o icone de evento.
# ---------------------------------------------------------------------------
from karuta.discord_page import tem_reacoes_de_carta


def test_drop_extra_sem_frase_eh_reconhecido_pelas_reactions():
    # O caso real: drop extra, so reactions, com o icone do evento junto.
    assert tem_reacoes_de_carta(["1️⃣", "2️⃣", "3️⃣", "🍉"]) is True


def test_drop_normal_eh_reconhecido():
    assert tem_reacoes_de_carta(["1️⃣", "2️⃣", "3️⃣"]) is True


def test_mensagem_qualquer_com_emoji_nao_eh_drop():
    # Sem isso o bot clicaria em reaction de mensagem de gente do servidor.
    assert tem_reacoes_de_carta(["🍉"]) is False
    assert tem_reacoes_de_carta(["😀", "❤️"]) is False


def test_sem_reactions_nao_eh_drop():
    assert tem_reacoes_de_carta([]) is False
