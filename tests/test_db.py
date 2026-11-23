"""
Testes do banco. Os que precisam de PostgreSQL sao pulados automaticamente
se ele nao estiver acessivel, pra suite continuar rodando em maquina limpa.
"""

import pytest

from karuta.avaliador import (
    Avaliacao,
    RegrasClassificacao,
    classificar,
    extrair_circulacao,
    extrair_desejos,
    extrair_detalhes_carta,
    extrair_personagem,
    extrair_serie,
    extrair_taxa_reivindicacao,
)
from karuta.discord_page import parse_take_message

# --------------------------------------------------------------------------
# parsers (puros, sem banco)
# --------------------------------------------------------------------------

def test_parse_confirmacao_de_pick():
    t = "@willo took the Wirth Madl card vllpfgs! Wow, it appears to be in mint condition!"
    take = parse_take_message(t)
    assert take is not None
    assert take.code == "vllpfgs"
    assert take.character == "Wirth Madl"
    assert take.condition == "mint"


def test_parse_confirmacao_com_condicao_ruim():
    t = "@Madson took the Noein card vlg0bc9! Its condition is quite poor."
    take = parse_take_message(t)
    assert take.code == "vlg0bc9"
    assert take.character == "Noein"
    assert take.condition == "poor"


def test_parse_confirmacao_personagem_com_espacos():
    t = "@Zumbi_Baiano took the Adachi Matagorou card vllpzmj! Unfortunately, it was badly damaged."
    take = parse_take_message(t)
    assert take.character == "Adachi Matagorou"
    assert take.condition == "damaged"


def test_parse_ignora_mensagem_que_nao_e_confirmacao():
    assert parse_take_message("@willo is dropping 3 cards!") is None
    assert parse_take_message("") is None


# Saidas REAIS do Karuta, capturadas no DM da conta A em 2026-08-01.
# Servem de fixture pra garantir que os parsers casam com o formato de
# verdade, e nao com um formato imaginado.
SAIDA_CARDINFO = (
    "Card Details\n"
    "vllpzmj · ☆☆☆☆ · #18321 · ◈2 · Hell's Paradise: Jigokuraku · Adachi Matagorou\n"
    "\nDropped on 1 de agosto de 2026 às 15:34\n"
    "Dropped in server ID 1302406232623022091\n"
    "\nOwned by @Zumbi_Baiano\nGrabbed by @Zumbi_Baiano\nDropped by @Zumbi_Baiano\n"
    "\nDropped in damaged condition\nGrabbed after 7.03 seconds"
)

SAIDA_LOOKUP = (
    "Character Lookup\n"
    "Character · Adachi Matagorou\n"
    "Series · Hell's Paradise: Jigokuraku\n"
    "Wishlisted · 1\n"
    "\nTotal generated · 18,321\nTotal claimed · 5,589\nTotal burned · 2,556\n"
    "Total in circulation · 3,033\nClaim rate · 31%\n"
    "Average claim time · 4.7 seconds\n"
    "\nCirculation (★★★★) · 326\nCirculation (★★★☆) · 558\n"
    "Showing edition 2 of 2"
)


def test_parseia_cardinfo_real():
    det = extrair_detalhes_carta(SAIDA_CARDINFO)
    assert det is not None
    assert det.codigo == "vllpzmj"
    assert det.estrelas == 0          # ☆☆☆☆ = nenhuma preenchida
    assert det.numero_print == 18321
    assert det.edicao == 2
    assert det.serie == "Hell's Paradise: Jigokuraku"
    assert det.personagem == "Adachi Matagorou"
    assert det.condicao == "damaged"


def test_conta_apenas_estrelas_preenchidas():
    texto = SAIDA_CARDINFO.replace("☆☆☆☆", "★★★☆")
    assert extrair_detalhes_carta(texto).estrelas == 3


def test_cardinfo_ignora_texto_que_nao_e_card_details():
    assert extrair_detalhes_carta("Worker Details\n40 Base value") is None


def test_parseia_lookup_real():
    assert extrair_desejos(SAIDA_LOOKUP) == 1
    assert extrair_circulacao(SAIDA_LOOKUP) == 3033
    assert extrair_taxa_reivindicacao(SAIDA_LOOKUP) == 31
    assert extrair_serie(SAIDA_LOOKUP) == "Hell's Paradise: Jigokuraku"
    assert extrair_personagem(SAIDA_LOOKUP) == "Adachi Matagorou"


def test_lookup_com_milhar_separado():
    assert extrair_desejos("Wishlisted · 12,847") == 12847


def test_desejos_ausente_retorna_none():
    assert extrair_desejos("Character · Rem\nSeries · Re:Zero") is None


# --------------------------------------------------------------------------
# classificacao
# --------------------------------------------------------------------------

REGRAS = RegrasClassificacao()


def _av(**kw):
    base = dict(codigo="abcdef1", conta_id="a")
    base.update(kw)
    return Avaliacao(**base)


# Os valores abaixo sao de cartas REAIS avaliadas em 2026-08-01, pra os
# testes refletirem a faixa que o jogo de fato produz.

def test_classifica_como_job_por_esforco():
    """Eva Cristella: esforco 35, o maior observado."""
    grupos = classificar(
        _av(esforco_total=35, valor_base=17, desejos=0, ouro_burn=66), REGRAS
    )
    assert "job" in grupos
    assert "burn" not in grupos


def test_classifica_como_job_por_valor_base():
    """Se o esforco nao parsear, o valor base sozinho ainda classifica."""
    grupos = classificar(_av(esforco_total=0, valor_base=14, desejos=0), REGRAS)
    assert "job" in grupos


def test_carta_pode_ser_job_e_desejada():
    grupos = classificar(
        _av(esforco_total=29, valor_base=14, desejos=500, ouro_burn=57), REGRAS
    )
    assert set(grupos) == {"job", "desejada"}


def test_burn_decide_pelo_ouro_quando_disponivel():
    """Youko Hazama: renderia 1 de ouro — descartavel."""
    grupos = classificar(
        _av(esforco_total=0, valor_base=0, desejos=0, ouro_burn=1), REGRAS
    )
    assert list(grupos) == ["burn"]
    assert "1 de ouro" in grupos["burn"]


def test_ouro_acima_do_limiar_nao_vira_burn():
    """Rika Minami: 16 de ouro — fraca pra job, mas nao eh lixo."""
    grupos = classificar(
        _av(esforco_total=8, valor_base=4, desejos=6, ouro_burn=16), REGRAS
    )
    assert grupos == {}


def test_sem_previa_de_burn_cai_no_criterio_antigo():
    """Sem `kb`, decide por valor base e esforco."""
    grupos = classificar(_av(esforco_total=4, valor_base=2, desejos=0), REGRAS)
    assert list(grupos) == ["burn"]
    assert "sem previa" in grupos["burn"]


def test_carta_que_serve_pra_algo_nunca_eh_burn():
    """Job ou desejada tem prioridade: nao faz sentido queimar."""
    grupos = classificar(
        _av(esforco_total=35, valor_base=17, desejos=0, ouro_burn=1), REGRAS
    )
    assert "burn" not in grupos


def test_desejos_desconhecido_nao_classifica_como_desejada():
    grupos = classificar(
        _av(esforco_total=19, valor_base=8, desejos=None, ouro_burn=30), REGRAS
    )
    assert "desejada" not in grupos


# --------------------------------------------------------------------------
# integracao com PostgreSQL
# --------------------------------------------------------------------------

def _conn():
    from karuta import db
    try:
        return db.conectar()
    except Exception:
        return None


pg = pytest.mark.skipif(_conn() is None, reason="PostgreSQL indisponivel")


@pg
def test_fluxo_completo_grava_e_classifica():
    from karuta import db

    codigo = "zzt3st9"
    with db.conectar() as conn:
        db.criar_esquema(conn)
        db.garantir_contas(conn, ["a", "b", "c"])
        # limpa resquicio de execucao anterior
        conn.execute("DELETE FROM cartas WHERE codigo = %s", (codigo,))
        conn.execute("DELETE FROM cartas_pegas WHERE codigo = %s", (codigo,))

        db.registrar_pick(conn, "a", "999", 0, True, codigo, "Rem", "mint")
        db.salvar_carta(
            conn, codigo=codigo, conta_id="a", personagem="Rem", serie="Re:Zero",
            condicao="mint", valor_base=55, esforco_total=112, desejos=500,
        )
        db.classificar(
            conn, codigo, "a",
            {"job": "esforco 112 >= 90", "desejada": "500 desejos >= 100"},
            valor_base=55, esforco_total=112, desejos=500,
        )

        carta = conn.execute(
            "SELECT * FROM cartas WHERE codigo = %s", (codigo,)
        ).fetchone()
        assert carta["conta_id"] == "a"
        assert carta["esforco_total"] == 112
        assert carta["avaliada_em"] is not None  # avaliada ao salvar

        # esta na mestre E nos dois subconjuntos
        assert conn.execute(
            "SELECT count(*) n FROM cartas_job WHERE codigo = %s", (codigo,)
        ).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT count(*) n FROM cartas_desejadas WHERE codigo = %s", (codigo,)
        ).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT count(*) n FROM cartas_burn WHERE codigo = %s", (codigo,)
        ).fetchone()["n"] == 0

        conn.execute("DELETE FROM cartas WHERE codigo = %s", (codigo,))
        conn.execute("DELETE FROM cartas_pegas WHERE codigo = %s", (codigo,))


@pg
def test_salvar_carta_nao_apaga_campo_ja_preenchido():
    """Segunda fonte com campo vazio nao pode zerar o que a primeira gravou."""
    from karuta import db

    codigo = "zzt3st8"
    with db.conectar() as conn:
        db.criar_esquema(conn)
        db.garantir_contas(conn, ["a"])
        conn.execute("DELETE FROM cartas WHERE codigo = %s", (codigo,))

        db.salvar_carta(conn, codigo=codigo, conta_id="a",
                        personagem="Rem", condicao="mint")
        db.salvar_carta(conn, codigo=codigo, conta_id="a",
                        serie="Re:Zero", valor_base=42, esforco_total=90)

        r = conn.execute("SELECT * FROM cartas WHERE codigo = %s", (codigo,)).fetchone()
        assert r["personagem"] == "Rem"     # preservado
        assert r["condicao"] == "mint"      # preservado
        assert r["serie"] == "Re:Zero"      # adicionado
        assert r["valor_base"] == 42
        conn.execute("DELETE FROM cartas WHERE codigo = %s", (codigo,))


@pg
def test_codigo_invalido_e_rejeitado_pelo_check():
    import psycopg
    from karuta import db

    with db.conectar() as conn:
        db.criar_esquema(conn)
        db.garantir_contas(conn, ["a"])
        with pytest.raises(psycopg.errors.CheckViolation):
            db.salvar_carta(conn, codigo="MAIUSCULO", conta_id="a")


@pg
def test_resumo_nao_conta_pendente_sem_carta():
    """Regressao: LEFT JOIN fazia conta vazia aparecer com 1 pendente."""
    from karuta import db

    with db.conectar() as conn:
        db.criar_esquema(conn)
        db.garantir_contas(conn, ["a", "b", "c"])
        for linha in db.resumo(conn):
            if linha["total_cartas"] == 0:
                assert linha["pendentes"] == 0


# --------------------------------------------------------------------------
# marcadores de resposta do Karuta
# --------------------------------------------------------------------------

def test_todo_comando_usado_tem_marcador_de_resposta():
    """
    Regressao: `k!ci` responde com "Card Details", que nao estava em
    _KARUTA_OK_MARKERS. O retry achava que nao houve resposta, reenviava 3x e
    desistia — a avaliacao nunca completava.
    """
    from karuta.scanner import _KARUTA_OK_MARKERS

    # cabecalho da resposta de cada comando que o bot envia
    respostas = {
        "k!wi": "Worker Details",
        "k!c": "Card Collection",
        "k!ci": "Card Details",
        "k!lu": "Character Lookup",
    }
    for comando, cabecalho in respostas.items():
        assert any(m in cabecalho for m in _KARUTA_OK_MARKERS), (
            f"'{comando}' responde com {cabecalho!r} mas nenhum marcador casa"
        )


# --------------------------------------------------------------------------
# kb (previa de queima) e k!i (inventario) — saidas reais de 2026-08-01
# --------------------------------------------------------------------------

SAIDA_BURN = """Burn Card
@Zumbi_Baiano, you will receive:

 7 Gold
 1 Dust (★☆☆☆)"""

SAIDA_INVENTARIO = """Inventory
Items carried by @Zumbi_Baiano

 226 · gold · Gold
 2 · ticket · Ticket
 1 · night ooze frame · Frame: Night Ooze
 444 · good dust · Dust (★★☆☆)
 138 · damaged dust · Dust (☆☆☆☆)
 25 · sweet halloween candy · Sweet Halloween Candy
Showing items 1-10 of 12"""


def test_parseia_previa_de_burn():
    from karuta.avaliador import extrair_retorno_burn

    r = extrair_retorno_burn(SAIDA_BURN)
    assert r is not None
    assert r.ouro == 7
    assert r.poeira == 1
    assert r.tier == "★☆☆☆"


def test_burn_ignora_texto_que_nao_e_burn_card():
    from karuta.avaliador import extrair_retorno_burn

    assert extrair_retorno_burn("Worker Details\n2 Base value") is None


def test_parseia_inventario():
    from karuta.avaliador import extrair_itens_inventario

    itens = extrair_itens_inventario(SAIDA_INVENTARIO)
    assert itens["gold"] == ("Gold", 226)
    assert itens["good dust"] == ("Dust (★★☆☆)", 444)
    assert itens["sweet halloween candy"] == ("Sweet Halloween Candy", 25)
    # cabecalho e rodape nao viram item
    assert "Inventory" not in itens
    assert all("Showing items" not in nome for nome, _ in itens.values())


def test_inventario_le_o_total_para_saber_se_falta_pagina():
    from karuta.avaliador import extrair_itens_inventario, total_itens_inventario

    assert total_itens_inventario(SAIDA_INVENTARIO) == 12
    # so 6 na fixture: o paginador precisa saber que falta buscar mais
    assert len(extrair_itens_inventario(SAIDA_INVENTARIO)) < 12


def test_inventario_com_separador_de_milhar():
    from karuta.avaliador import extrair_itens_inventario

    itens = extrair_itens_inventario(" 1,234 · gold · Gold")
    assert itens["gold"] == ("Gold", 1234)


def test_inventario_vazio_e_valido():
    """
    Conta sem itens responde 'Showing items 0-0 of 0'. Isso eh resultado
    legitimo, nao falha — a conta c estava exatamente assim.
    """
    from karuta.avaliador import extrair_itens_inventario, total_itens_inventario

    vazio = "Inventory\nItems carried by @willo\nShowing items 0-0 of 0"
    assert extrair_itens_inventario(vazio) == {}
    assert total_itens_inventario(vazio) == 0


def test_faixa_inventario_detecta_virada_de_pagina():
    from karuta.avaliador import _faixa_inventario

    assert _faixa_inventario("Showing items 1-10 of 12") == (1, 10)
    assert _faixa_inventario("Showing items 11-12 of 12") == (11, 12)
    assert _faixa_inventario("sem rodape") is None


# --------------------------------------------------------------------------
# k!bi (bits) — mesmo formato do k!i, so muda o rodape
# --------------------------------------------------------------------------

SAIDA_BITS = """Bits
Bits carried by @Zumbi_Baiano

 2,659 · clay bit · Clay Bit
 2,586 · zinc bit · Zinc Bit
 797 · ice bit · Ice Bit
Showing bits 1-10 of 13"""


def test_parseia_bits_com_o_mesmo_parser_do_inventario():
    """
    `k!i` e `k!bi` tem formato de linha identico; so o rodape muda
    ("items" vs "bits"). Por isso o parser eh um so.
    """
    from karuta.avaliador import extrair_itens_inventario, total_itens_inventario

    itens = extrair_itens_inventario(SAIDA_BITS)
    assert itens["clay bit"] == ("Clay Bit", 2659)
    assert itens["ice bit"] == ("Ice Bit", 797)
    assert total_itens_inventario(SAIDA_BITS) == 13


def test_bits_vazio():
    from karuta.avaliador import extrair_itens_inventario, total_itens_inventario

    vazio = "Bits\nBits carried by @willo\nShowing bits 0-0 of 0"
    assert extrair_itens_inventario(vazio) == {}
    assert total_itens_inventario(vazio) == 0


@pg
def test_item_e_bit_com_mesma_chave_nao_colidem():
    """
    A PK inclui `tipo` justamente pra isso: um item e um bit podem ter a
    mesma chave sem um sobrescrever o outro.
    """
    from karuta import db

    with db.conectar() as conn:
        db.criar_esquema(conn)
        db.garantir_contas(conn, ["a"])
        conn.execute("DELETE FROM inventario WHERE conta_id='a' AND chave='teste x'")
        # zerar_ausentes=False: sem isso, um dicionario com uma chave zera
        # todo o resto do inventario. Foi exatamente esse o bug.
        db.salvar_inventario(conn, "a", {"teste x": ("Item X", 5)},
                             tipo="item", zerar_ausentes=False)
        db.salvar_inventario(conn, "a", {"teste x": ("Bit X", 9)},
                             tipo="bit", zerar_ausentes=False)
        linhas = conn.execute(
            "SELECT tipo, quantidade FROM inventario"
            " WHERE conta_id='a' AND chave='teste x' ORDER BY tipo"
        ).fetchall()
        assert [(r["tipo"], r["quantidade"]) for r in linhas] == [("bit", 9), ("item", 5)]
        conn.execute("DELETE FROM inventario WHERE conta_id='a' AND chave='teste x'")
        conn.execute("DELETE FROM inventario_historico WHERE conta_id='a' AND chave='teste x'")


@pg
def test_leitura_vazia_conta_para_o_intervalo():
    """
    Regressao: conta com inventario vazio nao gera linha em `inventario`,
    entao `inventario_desatualizado` respondia True pra sempre e ela era
    reconsultada a cada ciclo (2 comandos desperdicados por ciclo).
    """
    from karuta import db

    with db.conectar() as conn:
        db.criar_esquema(conn)
        db.garantir_contas(conn, ["c"])
        conn.execute("DELETE FROM inventario_leituras WHERE conta_id='c'")

        assert db.inventario_desatualizado(conn, "c", 60, "item") is True
        db.registrar_leitura_inventario(conn, "c", "item", 0)  # leitura vazia
        assert db.inventario_desatualizado(conn, "c", 60, "item") is False
        # intervalo 0 = sempre vencido
        assert db.inventario_desatualizado(conn, "c", 0, "item") is True
        # tipos sao independentes
        assert db.inventario_desatualizado(conn, "c", 60, "bit") is True

        conn.execute("DELETE FROM inventario_leituras WHERE conta_id='c'")


def test_nome_da_tabela_de_cada_classificacao_existe_no_esquema():
    """
    Regressao: o codigo montava o nome como f"cartas_{classificacao}", o que
    dava "cartas_desejada" — mas a tabela eh "cartas_desejadas". Quebrava o
    `banco reclassificar` e o `banco desejadas`.
    """
    from karuta.db import ESQUEMA, TABELAS_CLASSIFICACAO

    for classificacao, tabela in TABELAS_CLASSIFICACAO.items():
        assert f"CREATE TABLE IF NOT EXISTS {tabela} " in ESQUEMA, (
            f"'{classificacao}' aponta pra tabela inexistente: {tabela}"
        )


# --------------------------------------------------------------------------
# guarda de personagem do k!lu
# --------------------------------------------------------------------------

def test_sufixo_de_variante_nao_invalida_o_lookup():
    """
    Regressao vista em producao: o `k!ci` deu 'Flying Boss Type Alca-Noise
    (Shark)' e o `k!lu` deu 'Flying Boss Type Alca-Noise'. A comparacao
    literal descartava a wishlist de um personagem que era o mesmo.
    """
    from karuta.avaliador import mesmo_personagem

    assert mesmo_personagem(
        "Flying Boss Type Alca-Noise", "Flying Boss Type Alca-Noise (Shark)"
    )
    assert mesmo_personagem("Rem", "rem")
    assert mesmo_personagem("Sunday ", " Sunday")


def test_guarda_ainda_rejeita_personagem_diferente():
    """O objetivo da guarda continua: nao gravar popularidade de outra carta."""
    from karuta.avaliador import mesmo_personagem

    assert not mesmo_personagem("Nico Robin", "Sunday")
    assert not mesmo_personagem("Rem", "Ram")
    assert not mesmo_personagem("", "Rem")
    assert not mesmo_personagem("Rem", "")


@pg
def test_salvar_parcial_nao_zera_o_resto():
    """
    Regressao: `salvar_inventario` com dicionario parcial zerava todo o
    inventario restante. Um teste da suite fez isso contra o banco real e
    apagou 12 itens e 13 bits da conta A.
    """
    from karuta import db

    with db.conectar() as conn:
        db.criar_esquema(conn)
        db.garantir_contas(conn, ["a"])
        conn.execute("DELETE FROM inventario WHERE conta_id='a' AND tipo='item'")
        db.salvar_inventario(conn, "a", {"ouro": ("Gold", 100), "poeira": ("Dust", 50)})

        # parcial com a guarda ligada: preserva
        db.salvar_inventario(conn, "a", {"ouro": ("Gold", 120)},
                             zerar_ausentes=False)
        linhas = {r["chave"]: r["quantidade"] for r in db.listar_inventario(conn, "a")}
        assert linhas["ouro"] == 120
        assert linhas["poeira"] == 50   # NAO foi zerado

        # completo: o que sumiu vira zero, e isso vai pro historico
        conn.execute("DELETE FROM inventario_historico WHERE conta_id='a'")
        db.salvar_inventario(conn, "a", {"ouro": ("Gold", 130)})
        linhas = {r["chave"]: r["quantidade"]
                  for r in db.listar_inventario(conn, "a", incluir_zerados=True)}
        assert linhas["poeira"] == 0
        zerados = conn.execute(
            "SELECT chave FROM inventario_historico"
            " WHERE conta_id='a' AND quantidade=0"
        ).fetchall()
        assert [r["chave"] for r in zerados] == ["poeira"]

        conn.execute("DELETE FROM inventario WHERE conta_id='a'")
        conn.execute("DELETE FROM inventario_historico WHERE conta_id='a'")
