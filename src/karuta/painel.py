"""
Painel web local (read-only) pra acompanhar as cartas de cada conta enquanto o
bot roda.

Usa so a stdlib (`http.server`) de proposito: o painel nao pode adicionar
dependencia nem concorrer com o bot. Toda consulta eh SELECT e abre/fecha a
propria conexao, entao banco fora do ar vira aviso na tela em vez de derrubar
o servidor — e nada aqui trava o caminho do pick.

Cobre exatamente as tres tabelas de carta pedidas: `cartas` (mestre),
`cartas_job` e `cartas_desejadas`.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from loguru import logger

from . import db

CONTAS_VALIDAS = ("a", "b", "c")
LIMITE_PADRAO = 500
LIMITE_MAX = 5000


# ==========================================================================
# consultas
# ==========================================================================

# Contagens por conta derivadas apenas das tres tabelas do painel. Nao uso
# `vw_resumo_por_conta` porque ela tambem soma `cartas_burn`, que nao faz
# parte deste painel.
SQL_RESUMO = """
SELECT ct.id AS conta_id,
       (SELECT COUNT(*)                FROM cartas c WHERE c.conta_id = ct.id) AS total,
       (SELECT COUNT(c.avaliada_em)    FROM cartas c WHERE c.conta_id = ct.id) AS avaliadas,
       (SELECT COUNT(*)                FROM cartas c WHERE c.conta_id = ct.id
                                        AND c.avaliada_em IS NULL)             AS pendentes,
       (SELECT COUNT(*)  FROM cartas_job       j WHERE j.conta_id = ct.id)     AS job,
       (SELECT COUNT(*)  FROM cartas_desejadas d WHERE d.conta_id = ct.id)     AS desejadas,
       (SELECT MAX(c.esforco_total)    FROM cartas c WHERE c.conta_id = ct.id) AS melhor_esforco,
       (SELECT MAX(c.valor_base)       FROM cartas c WHERE c.conta_id = ct.id) AS melhor_base,
       (SELECT MAX(c.desejos)          FROM cartas c WHERE c.conta_id = ct.id) AS mais_desejada,
       (SELECT MAX(c.obtida_em)        FROM cartas c WHERE c.conta_id = ct.id) AS ultima_carta
  FROM contas ct
 ORDER BY ct.id
"""

# `eh_job` / `eh_desejada` deixam a aba "cartas" mostrar a classificacao sem
# precisar cruzar as abas na mao.
SQL_CARTAS = """
SELECT c.conta_id, c.codigo, c.personagem, c.serie, c.estrelas, c.numero_print,
       c.edicao, c.condicao, c.valor_base, c.esforco_total, c.desejos,
       c.circulacao, c.taxa_reivindicacao, c.ouro_burn, c.poeira_burn,
       c.origem, c.obtida_em, c.avaliada_em,
       (j.codigo IS NOT NULL) AS eh_job,
       (d.codigo IS NOT NULL) AS eh_desejada
  FROM cartas c
  LEFT JOIN cartas_job       j ON j.codigo = c.codigo
  LEFT JOIN cartas_desejadas d ON d.codigo = c.codigo
 {filtro}
 ORDER BY {ordem}
 LIMIT %(limite)s
"""

# Os filtros rapidos da secao `cartas` precisam rodar no SQL, nao no
# navegador: a tabela tem milhares de linhas e o painel carrega so umas
# centenas. Filtrando no cliente, "avaliadas" dava zero — as mais recentes
# (que sao as que vinham) sao scans ainda sem k!wi.
# Cada filtro traz junto a ordenacao que faz sentido pra ele.
FILTROS_CARTAS = {
    "todas": ("", "c.obtida_em DESC"),
    "avaliadas": ("c.avaliada_em IS NOT NULL",
                  "c.esforco_total DESC NULLS LAST, c.avaliada_em DESC"),
    "pendentes": ("c.avaliada_em IS NULL", "c.obtida_em DESC"),
    "job": ("j.codigo IS NOT NULL", "c.esforco_total DESC NULLS LAST"),
    "desejada": ("d.codigo IS NOT NULL", "c.desejos DESC NULLS LAST"),
    "estrelas": ("c.estrelas >= 3",
                 "c.estrelas DESC, c.esforco_total DESC NULLS LAST"),
}

# Contagem de cada filtro, pra o chip mostrar o total real da tabela e nao o
# da pagina carregada.
SQL_CONTAGENS = """
SELECT COUNT(*)                                             AS todas,
       COUNT(c.avaliada_em)                                 AS avaliadas,
       COUNT(*) FILTER (WHERE c.avaliada_em IS NULL)        AS pendentes,
       COUNT(*) FILTER (WHERE j.codigo IS NOT NULL)         AS job,
       COUNT(*) FILTER (WHERE d.codigo IS NOT NULL)         AS desejada,
       COUNT(*) FILTER (WHERE c.estrelas >= 3)              AS estrelas
  FROM cartas c
  LEFT JOIN cartas_job       j ON j.codigo = c.codigo
  LEFT JOIN cartas_desejadas d ON d.codigo = c.codigo
 {filtro}
"""

SQL_JOB = """
SELECT j.conta_id, j.codigo, j.personagem, j.serie, j.esforco_total, j.valor_base,
       j.motivo, j.classificada_em,
       c.estrelas, c.numero_print, c.edicao, c.condicao, c.desejos
  FROM cartas_job j
  JOIN cartas c ON c.codigo = j.codigo
 {filtro}
 ORDER BY j.esforco_total DESC, j.valor_base DESC
 LIMIT %(limite)s
"""

SQL_DESEJADAS = """
SELECT d.conta_id, d.codigo, d.personagem, d.serie, d.desejos,
       d.motivo, d.classificada_em,
       c.estrelas, c.numero_print, c.edicao, c.condicao,
       c.valor_base, c.esforco_total, c.circulacao, c.taxa_reivindicacao
  FROM cartas_desejadas d
  JOIN cartas c ON c.codigo = d.codigo
 {filtro}
 ORDER BY d.desejos DESC
 LIMIT %(limite)s
"""

# --------------------------------------------------------------------------
# Agregados da visao geral.
#
# Rodam sobre a tabela INTEIRA, nao sobre as linhas que o painel carregou:
# "cartas por dia" tem que contar as 3.600, nao as 500 da pagina. Sao
# COUNT/GROUP BY em tabela de poucos milhares de linhas, barato o bastante
# pra rodar a cada refresh sem atrapalhar o bot.
# --------------------------------------------------------------------------

# Uma metrica so por grafico (cartas pegas), uma serie por conta.
#
# So `origem = 'drop'`: o `scan` importa a colecao inteira de uma vez, e esse
# pico (1.400 cartas numa hora) achata todas as outras barras pra menos de um
# pixel. Drop eh o que o bot faz de verdade, e eh isso que o grafico conta.
#
# Duas janelas: a de hora eh o pulso enquanto o bot roda; a de dia serve de
# reserva pra quando nao houve nada nas ultimas 24h (senao o grafico some).
SQL_POR_HORA = """
SELECT conta_id, date_trunc('hour', obtida_em) AS t, COUNT(*) AS n
  FROM cartas
 WHERE obtida_em >= (now() - interval '24 hours')
   AND origem = 'drop'
   {filtro}
 GROUP BY conta_id, t
 ORDER BY t
"""

SQL_POR_DIA = """
SELECT conta_id, obtida_em::date::text AS t, COUNT(*) AS n
  FROM cartas
 WHERE obtida_em >= (now() - interval '13 days')
   AND origem = 'drop'
   {filtro}
 GROUP BY conta_id, t
 ORDER BY t
"""

SQL_ESTRELAS = """
SELECT conta_id, estrelas, COUNT(*) AS n
  FROM cartas
 {filtro}
 GROUP BY conta_id, estrelas
 ORDER BY estrelas
"""

# Faixas de 10 em 10; o `LEAST` junta a cauda longa numa faixa "200+" pra o
# histograma nao virar 30 barras vazias por causa de uma carta de 171.
SQL_FAIXAS_JOB = """
SELECT LEAST(esforco_total / 10, 20) * 10 AS faixa, COUNT(*) AS n
  FROM cartas_job
 {filtro}
 GROUP BY faixa
 ORDER BY faixa
"""

SQL_TOP_SERIES = """
SELECT serie, COUNT(*) AS cartas, SUM(desejos) AS desejos
  FROM cartas_desejadas
 WHERE serie <> ''
   {filtro}
 GROUP BY serie
 ORDER BY desejos DESC
 LIMIT 10
"""

_FILTROS = {
    "cartas": "WHERE c.conta_id = %(conta)s",
    "job": "WHERE j.conta_id = %(conta)s",
    "desejadas": "WHERE d.conta_id = %(conta)s",
    # Nos agregados o filtro entra como AND/WHERE conforme a consulta ja
    # tenha ou nao um WHERE.
    "e_conta": "AND conta_id = %(conta)s",
    "so_conta": "WHERE conta_id = %(conta)s",
}


def _sql(base: str, chave: str, conta: str | None) -> str:
    return base.format(filtro=_FILTROS[chave] if conta else "")


def _onde_cartas(conta: str | None, filtro: str) -> tuple[str, str]:
    """WHERE + ORDER BY da secao `cartas`, juntando conta e filtro rapido."""
    extra, ordem = FILTROS_CARTAS.get(filtro, FILTROS_CARTAS["todas"])
    cond = [c for c in (("c.conta_id = %(conta)s" if conta else ""), extra) if c]
    return ("WHERE " + " AND ".join(cond)) if cond else "", ordem


def coletar(dsn: str | None, conta: str | None, limite: int, filtro: str = "todas") -> dict:
    """Le as tres tabelas numa conexao curta e devolve tudo serializavel."""
    limite = max(1, min(limite, LIMITE_MAX))
    params = {"limite": limite, "conta": conta}
    onde, ordem = _onde_cartas(conta, filtro)
    # As contagens dos chips ignoram o filtro rapido (senao o chip escolhido
    # mostraria o total dele e os outros zerariam), mas respeitam a conta.
    onde_conta, _ = _onde_cartas(conta, "todas")
    with db.conectar(dsn) as conn:
        resumo = conn.execute(SQL_RESUMO).fetchall()
        cartas = conn.execute(
            SQL_CARTAS.format(filtro=onde, ordem=ordem), params).fetchall()
        contagens = conn.execute(
            SQL_CONTAGENS.format(filtro=onde_conta), params).fetchone()
        job = conn.execute(_sql(SQL_JOB, "job", conta), params).fetchall()
        desejadas = conn.execute(_sql(SQL_DESEJADAS, "desejadas", conta), params).fetchall()
        graficos = {
            "por_hora": conn.execute(_sql(SQL_POR_HORA, "e_conta", conta), params).fetchall(),
            "por_dia": conn.execute(_sql(SQL_POR_DIA, "e_conta", conta), params).fetchall(),
            "estrelas": conn.execute(_sql(SQL_ESTRELAS, "so_conta", conta), params).fetchall(),
            "faixas_job": conn.execute(_sql(SQL_FAIXAS_JOB, "so_conta", conta), params).fetchall(),
            "top_series": conn.execute(_sql(SQL_TOP_SERIES, "e_conta", conta), params).fetchall(),
        }
    return {
        "resumo": resumo,
        "contagens": contagens,
        "filtro": filtro,
        "cartas": cartas,
        "job": job,
        "desejadas": desejadas,
        "graficos": graficos,
        "limite": limite,
        "conta": conta or "",
        # `truncado` avisa na tela quando o limite cortou linhas, pra ninguem
        # ler "47 cartas" achando que eh o total da conta.
        "truncado": {
            "cartas": len(cartas) >= limite,
            "job": len(job) >= limite,
            "desejadas": len(desejadas) >= limite,
        },
    }


def _json_default(valor):
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return float(valor)
    return str(valor)


# ==========================================================================
# servidor
# ==========================================================================

class _Handler(BaseHTTPRequestHandler):
    server_version = "karuta-painel"
    dsn: str | None = None

    def do_GET(self) -> None:  # nome exigido pela stdlib
        rota = urlparse(self.path)
        if rota.path in ("/", "/index.html"):
            self._estatico("painel.html")
            return
        if rota.path == "/api/dados":
            self._api(parse_qs(rota.query))
            return
        # Lista branca, nao caminho montado a partir da URL: `/../config.yaml`
        # nao tem como virar arquivo servido.
        nome = rota.path.lstrip("/")
        if nome in ESTATICOS:
            self._estatico(nome)
            return
        self._responder(404, "text/plain; charset=utf-8", b"nao encontrado")

    def _estatico(self, nome: str) -> None:
        corpo, tipo = ler_estatico(nome)
        self._responder(200 if corpo else 404, tipo, corpo or b"arquivo nao encontrado")

    def _api(self, query: dict[str, list[str]]) -> None:
        conta = (query.get("conta") or [""])[0].strip().lower()
        if conta and conta not in CONTAS_VALIDAS:
            conta = ""
        try:
            limite = int((query.get("limite") or [str(LIMITE_PADRAO)])[0])
        except ValueError:
            limite = LIMITE_PADRAO
        filtro = (query.get("filtro") or ["todas"])[0]
        if filtro not in FILTROS_CARTAS:
            filtro = "todas"
        try:
            dados = coletar(self.dsn, conta or None, limite, filtro)
        except Exception as e:
            # Banco pode piscar enquanto o bot trabalha; o painel mostra o erro
            # e continua tentando no proximo refresh.
            logger.warning(f"painel: consulta falhou ({e})")
            corpo = json.dumps({"erro": str(e)}).encode()
            self._responder(503, "application/json; charset=utf-8", corpo)
            return
        corpo = json.dumps(dados, default=_json_default).encode()
        self._responder(200, "application/json; charset=utf-8", corpo)

    def _responder(self, codigo: int, tipo: str, corpo: bytes) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(corpo)
        except BrokenPipeError:
            pass  # aba fechada no meio do download

    def log_message(self, formato: str, *args) -> None:
        # Sem isso cada poll do navegador imprime uma linha no stderr e enterra
        # o log do bot.
        logger.debug("painel: " + formato % args)


def abrir_navegador(url: str) -> bool:
    """
    Abre o painel no navegador. No WSL o `webbrowser` da stdlib costuma achar
    o `gio` e nao abrir nada (nao ha desktop), entao cai pro `explorer.exe`,
    que joga pro navegador padrao do Windows.
    """
    import shutil
    import subprocess
    import webbrowser

    try:
        if webbrowser.open(url):
            return True
    except Exception as e:
        logger.debug(f"webbrowser falhou: {e}")
    for prog in ("wslview", "explorer.exe", "xdg-open", "sensible-browser"):
        caminho = shutil.which(prog)
        if not caminho:
            continue
        try:
            # explorer.exe devolve codigo != 0 mesmo quando abre; o que importa
            # eh nao estourar excecao.
            subprocess.Popen([caminho, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception as e:
            logger.debug(f"{prog} falhou: {e}")
    logger.warning(f"nao consegui abrir navegador; acesse {url} na mao")
    return False


def servir(
    dsn: str | None = None,
    host: str = "127.0.0.1",
    porta: int = 8787,
) -> None:
    """Sobe o painel e bloqueia ate Ctrl+C."""
    handler = type("Handler", (_Handler,), {"dsn": dsn})
    servidor = ThreadingHTTPServer((host, porta), handler)
    servidor.daemon_threads = True
    logger.info(f"painel em http://{host}:{porta} (Ctrl+C para parar)")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        logger.info("painel encerrado")
    finally:
        servidor.server_close()


# ==========================================================================
# arquivos da pagina
# ==========================================================================

DIR_WEB = Path(__file__).parent / "web"

# Lista branca de nomes servidos, com o tipo de cada um. Nada fora daqui sai
# do processo, entao nao ha como pedir um arquivo arbitrario pela URL.
ESTATICOS = {
    "painel.html": "text/html; charset=utf-8",
    "painel.css": "text/css; charset=utf-8",
    "painel.js": "text/javascript; charset=utf-8",
}


def ler_estatico(nome: str) -> tuple[bytes | None, str]:
    """
    Le o arquivo do disco a cada request: mexer no HTML/CSS/JS e dar F5 basta,
    sem reiniciar o servidor (nem o bot). Sao tres arquivos pequenos e so o
    navegador local pede.
    """
    tipo = ESTATICOS.get(nome)
    if tipo is None:
        return None, "text/plain; charset=utf-8"
    try:
        return (DIR_WEB / nome).read_bytes(), tipo
    except OSError as e:
        logger.error(f"painel: nao achei {DIR_WEB / nome} ({e})")
        return None, "text/plain; charset=utf-8"
