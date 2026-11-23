"""
Isola os testes num banco separado.

Motivo concreto: `test_item_e_bit_com_mesma_chave_nao_colidem` chamava
`salvar_inventario` com um dicionario de UMA chave, e essa funcao zera tudo
que nao esta na lista (comportamento certo pra "item sumiu do inventario").
Rodando contra o banco de producao, isso zerou os 12 itens e 13 bits reais da
conta A.

A correcao de fundo eh esta: teste nunca toca o banco real. Aqui apontamos a
env `KARUTA_DB_DSN` — que `db.resolver_dsn` le com prioridade — para um banco
proprio, criado sob demanda.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import pytest

DSN_PRODUCAO = "postgresql://postgres:password@localhost:5432/Karuta"
BANCO_TESTE = "karuta_teste"


def _dsn_com_banco(dsn: str, banco: str) -> str:
    partes = urlsplit(dsn)
    return urlunsplit(partes._replace(path=f"/{banco}"))


def _criar_banco_se_faltar(dsn_base: str, banco: str) -> bool:
    """Cria o banco de teste. False se o PostgreSQL nao estiver acessivel."""
    try:
        import psycopg
    except ImportError:
        return False
    try:
        # `postgres` eh o banco administrativo; CREATE DATABASE nao roda
        # dentro de transacao, dai o autocommit.
        with psycopg.connect(_dsn_com_banco(dsn_base, "postgres"),
                             autocommit=True) as conn:
            existe = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (banco,)
            ).fetchone()
            if not existe:
                conn.execute(f'CREATE DATABASE "{banco}"')
        return True
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def banco_de_teste():
    """
    Aponta todo `db.conectar()` da suite para o banco de teste.

    autouse de proposito: nenhum teste deve precisar lembrar de opt-in pra
    nao destruir dado de producao.
    """
    dsn_base = os.environ.get("KARUTA_DB_DSN") or DSN_PRODUCAO
    if not _criar_banco_se_faltar(dsn_base, BANCO_TESTE):
        yield None
        return

    dsn_teste = _dsn_com_banco(dsn_base, BANCO_TESTE)
    anterior = os.environ.get("KARUTA_DB_DSN")
    os.environ["KARUTA_DB_DSN"] = dsn_teste

    try:
        from karuta import db

        with db.conectar() as conn:
            db.criar_esquema(conn)
            db.garantir_contas(conn, ["a", "b", "c"])
        yield dsn_teste
    finally:
        if anterior is None:
            os.environ.pop("KARUTA_DB_DSN", None)
        else:
            os.environ["KARUTA_DB_DSN"] = anterior
