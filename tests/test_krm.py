"""
Parser do `krm`. As fixtures sao a saida REAL do Karuta, capturada em
2026-08-01 no canal #karuta-drops.
"""

from karuta.discord_page import duracao_para_segundos, parse_krm_reply

# Conta A (Zumbi_Baiano) as 17:19
KRM_A = """Reminders
🔕 Daily is ready
🔕 Vote is ready
🔕 Drop in 15 minutes
🔕 Grab in 7 minutes
🔕 Work is ready
🔕 Visit is ready"""

# Conta C: drop pronto, grab nao
KRM_C = """Reminders
🔕 Daily is ready
🔕 Vote is ready
🔕 Drop is ready
🔕 Grab in 7 minutes
🔕 Work is ready
🔕 Visit is ready"""


def test_parseia_minutos_por_extenso():
    """Regressao: '15 minutes' nao casava com o regex antigo de '5m 30s'."""
    cd = parse_krm_reply(KRM_A)
    assert cd["drop"] == 15 * 60
    assert cd["grab"] == 7 * 60


def test_ready_vira_zero():
    cd = parse_krm_reply(KRM_C)
    assert cd["drop"] == 0
    assert cd["grab"] == 7 * 60
    assert cd["daily"] == 0
    assert cd["work"] == 0


def test_grab_nunca_e_confundido_com_pronto():
    """
    O bug que fez o bot dropar sem grab: parse vazio -> chamador usava um krm
    antigo. Aqui garantimos que 'Grab in 7 minutes' nunca vira 0.
    """
    assert parse_krm_reply(KRM_A)["grab"] != 0


def test_texto_vazio_ou_sem_reminders():
    assert parse_krm_reply("") == {}
    assert parse_krm_reply("qualquer outra mensagem") == {}


def test_duracoes():
    assert duracao_para_segundos("15 minutes") == 900
    assert duracao_para_segundos("1 minute") == 60
    assert duracao_para_segundos("30 seconds") == 30
    assert duracao_para_segundos("1 hour 5 minutes") == 3900
    assert duracao_para_segundos("5m 30s") == 330
    assert duracao_para_segundos("is ready") is None


def test_duracao_soma_unidades_em_vez_de_parar_na_primeira():
    assert duracao_para_segundos("2 hours 30 minutes") == 2 * 3600 + 30 * 60


# --------------------------------------------------------------------------
# k!lu — resposta ambigua vs exata (capturado da conta B em 2026-08-01 17:38)
# --------------------------------------------------------------------------

# `k!lu Sunday` — nome ambiguo devolve MENU, nao dados.
LOOKUP_AMBIGUO = """Character Results
@willo, please select a character using the menu below.
Showing characters 1-10 of 34
1. ♡1592 · One Piece · Nico Robin
2. ♡459 · Honkai: Star Rail · Sunday
6. ♡1 · Wish Me Mell · Sunday
Select a character"""

# `k!lu` sem parametro — a carta certa.
LOOKUP_EXATO = """Character Lookup
Character · Sunday
Series · Wish Me Mell
Wishlisted · 1

Total generated · 83,610
Total in circulation · 6,896
Claim rate · 15%"""


def test_lookup_ambiguo_nao_produz_desejos():
    """
    Regressao: `k!lu <nome>` com nome comum devolve 'Character Results' com
    dezenas de personagens. Nada dali pode virar dado — o primeiro da lista
    era Nico Robin/One Piece quando a carta era Sunday/Wish Me Mell.
    """
    from karuta.avaliador import extrair_desejos

    assert extrair_desejos(LOOKUP_AMBIGUO) is None


def test_lookup_sem_parametro_traz_o_personagem_certo():
    from karuta.avaliador import (
        extrair_circulacao,
        extrair_desejos,
        extrair_personagem,
        extrair_serie,
        extrair_taxa_reivindicacao,
    )

    assert extrair_personagem(LOOKUP_EXATO) == "Sunday"
    assert extrair_serie(LOOKUP_EXATO) == "Wish Me Mell"
    assert extrair_desejos(LOOKUP_EXATO) == 1
    assert extrair_circulacao(LOOKUP_EXATO) == 6896
    assert extrair_taxa_reivindicacao(LOOKUP_EXATO) == 15


# --------------------------------------------------------------------------
# limpeza do texto bruto guardado em `avaliacoes`
# --------------------------------------------------------------------------

def test_limpar_resposta_corta_o_lixo_do_discord():
    """
    O inner_text do Discord traz nome, tag do servidor, "APP VERIFICADO",
    horario e o eco do comando antes da resposta. Guardar isso dobrava o
    tamanho e atrapalhava a leitura.
    """
    from karuta.db import limpar_resposta

    bruto = (
        "Zumbi_Baiano\n\xa0[HSR],\xa0\nTag do servidor: HSR\nHSR\n"
        "k!wi vllr3cf\nKaruta\nAPP VERIFICADO\nAPP\n — \n17:35\n"
        "sábado, 1 de agosto de 2026 às 17:35\n"
        "Worker Details\nCharacter · Uryoku Chouno (vllr3cf)\nEffort · 4"
    )
    limpo = limpar_resposta(bruto)
    assert limpo.startswith("Worker Details")
    assert "APP VERIFICADO" not in limpo
    assert "\xa0" not in limpo
    assert "Effort · 4" in limpo


def test_limpar_resposta_preserva_texto_sem_cabecalho_conhecido():
    from karuta.db import limpar_resposta

    assert limpar_resposta("mensagem qualquer") == "mensagem qualquer"
    assert limpar_resposta("") == ""


def test_limpar_resposta_pega_o_primeiro_cabecalho():
    """Se houver mais de um cabecalho, corta no mais cedo."""
    from karuta.db import limpar_resposta

    t = "lixo\nCard Details\nabc\nCharacter Lookup\ndef"
    assert limpar_resposta(t).startswith("Card Details")


# --------------------------------------------------------------------------
# paginacao: emojis de "proxima pagina"
# --------------------------------------------------------------------------

def test_conjunto_de_emojis_cobre_inventario_e_colecao():
    """
    O Karuta usa botoes diferentes por comando: o inventario (k!i/k!bi) tem
    2 botoes (⬅️ ➡️) e a colecao (k!c) tem 4 (⏪ ◀ ▶ ⏩). A heuristica antiga
    exigia 4 botoes e clicava o indice 2, entao a paginacao do inventario
    nunca avancava — lia so a primeira pagina.
    """
    from karuta.discord_page import _EMOJIS_PROXIMA

    assert "➡️" in _EMOJIS_PROXIMA   # inventario / bits
    assert "▶" in _EMOJIS_PROXIMA or "▶️" in _EMOJIS_PROXIMA  # colecao
    assert "⏩" in _EMOJIS_PROXIMA
