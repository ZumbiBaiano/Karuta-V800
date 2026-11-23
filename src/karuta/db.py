"""
Persistencia em PostgreSQL. Nomenclatura toda em portugues (pt-BR).

Modelo
------
`cartas` eh a tabela mestre: TODA carta que passamos a possuir entra nela.
As tabelas especializadas sao subconjuntos classificados, e uma carta pode
estar em mais de um (ex: boa pra job E desejada por outros jogadores):

    cartas ────┬── cartas_desejadas   personagem que outros jogadores querem
               ├── cartas_job         boa pra trabalhar (worker/job)
               └── cartas_burn        candidata a queimar

`cartas_pegas` eh o historico de picks (um registro por clique, mesmo os que
falharam). `avaliacoes` guarda o texto bruto de cada resposta do Karuta, para
dar pra reprocessar sem precisar consultar o bot de novo.

Escopo: so entram cartas obtidas a partir de agora (do proximo `kd` em
diante). Nao ha carga retroativa da colecao antiga.

Todas as tabelas separam por `conta_id` ('a', 'b' ou 'c').

O banco eh OPCIONAL: se cair, o bot de drop continua pegando cartas e apenas
loga aviso. Perder registro nao pode custar um pick.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg
from loguru import logger
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DSN_PADRAO = "postgresql://postgres:password@localhost:5432/Karuta"

# Classificacao -> tabela. Mapa explicito de proposito: "desejada" vira
# "cartas_desejadas" (plural), entao montar o nome por concatenacao gera
# "cartas_desejada" e quebra.
TABELAS_CLASSIFICACAO = {
    "desejada": "cartas_desejadas",
    "job": "cartas_job",
    "burn": "cartas_burn",
}
CLASSIFICACOES = tuple(TABELAS_CLASSIFICACAO)

ESQUEMA = """
-- ---------------------------------------------------------------------
-- Contas do bot
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contas (
    id           TEXT PRIMARY KEY CHECK (id IN ('a', 'b', 'c')),
    apelido      TEXT        NOT NULL DEFAULT '',
    criada_em    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Tabela mestre: todas as cartas que possuimos
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cartas (
    codigo            TEXT PRIMARY KEY
                      CHECK (codigo ~ '^[a-z0-9]{7}$'),
    conta_id          TEXT        NOT NULL REFERENCES contas(id),
    personagem        TEXT        NOT NULL DEFAULT '',
    serie             TEXT        NOT NULL DEFAULT '',
    estrelas          SMALLINT    NOT NULL DEFAULT 0
                      CHECK (estrelas BETWEEN 0 AND 5),
    numero_print      INTEGER     NOT NULL DEFAULT 0 CHECK (numero_print >= 0),
    edicao            SMALLINT    NOT NULL DEFAULT 0 CHECK (edicao >= 0),
    condicao          TEXT        NOT NULL DEFAULT '',

    -- avaliacao (k!wi = worker info)
    valor_base        INTEGER     CHECK (valor_base   IS NULL OR valor_base   >= 0),
    esforco_total     INTEGER     CHECK (esforco_total IS NULL OR esforco_total >= 0),
    -- do k!lu (character lookup), sobre o PERSONAGEM e nao a carta
    desejos            INTEGER    CHECK (desejos IS NULL OR desejos >= 0),
    circulacao         INTEGER    CHECK (circulacao IS NULL OR circulacao >= 0),
    taxa_reivindicacao SMALLINT   CHECK (taxa_reivindicacao IS NULL
                                    OR taxa_reivindicacao BETWEEN 0 AND 100),

    -- do `kb` (burn card): quanto a carta renderia se queimada
    ouro_burn         INTEGER     CHECK (ouro_burn IS NULL OR ouro_burn >= 0),
    poeira_burn       INTEGER     CHECK (poeira_burn IS NULL OR poeira_burn >= 0),
    poeira_tier       TEXT        NOT NULL DEFAULT '',

    origem            TEXT        NOT NULL DEFAULT 'drop'
                      CHECK (origem IN ('drop', 'scan', 'manual')),
    mensagem_drop_id  TEXT,
    obtida_em         TIMESTAMPTZ NOT NULL DEFAULT now(),
    avaliada_em       TIMESTAMPTZ,
    criada_em         TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizada_em     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Historico de picks (inclui tentativas falhas)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cartas_pegas (
    id                BIGSERIAL PRIMARY KEY,
    conta_id          TEXT        NOT NULL REFERENCES contas(id),
    mensagem_drop_id  TEXT        NOT NULL,
    indice_botao      SMALLINT    CHECK (indice_botao BETWEEN 0 AND 3),
    codigo            TEXT,
    personagem        TEXT        NOT NULL DEFAULT '',
    condicao          TEXT        NOT NULL DEFAULT '',
    sucesso           BOOLEAN     NOT NULL,
    pega_em           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Texto bruto de cada avaliacao, pra poder reprocessar depois
-- ---------------------------------------------------------------------
-- Uma linha por comando enviado, inclusive os que falharam. `extraido` guarda
-- o que aquele comando rendeu, em JSONB: cada comando produz campos
-- diferentes, e colunas fixas viravam uma floresta de NULL (o k!ci nao
-- preenchia nenhuma das metricas).
CREATE TABLE IF NOT EXISTS avaliacoes (
    id            BIGSERIAL PRIMARY KEY,
    codigo        TEXT        NOT NULL,
    conta_id      TEXT        NOT NULL REFERENCES contas(id),
    comando       TEXT        NOT NULL,
    sucesso       BOOLEAN     NOT NULL DEFAULT TRUE,
    texto_bruto   TEXT        NOT NULL DEFAULT '',
    extraido      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    avaliada_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Subconjuntos classificados
-- ---------------------------------------------------------------------
-- Personagens que outros jogadores querem (alto numero de wishlists).
CREATE TABLE IF NOT EXISTS cartas_desejadas (
    codigo         TEXT PRIMARY KEY REFERENCES cartas(codigo) ON DELETE CASCADE,
    conta_id       TEXT        NOT NULL REFERENCES contas(id),
    -- copiados de `cartas` pra tabela ser legivel sozinha num cliente SQL,
    -- sem precisar de join. Sao reconstruidos por `banco reclassificar`.
    personagem     TEXT        NOT NULL DEFAULT '',
    serie          TEXT        NOT NULL DEFAULT '',
    desejos        INTEGER     NOT NULL DEFAULT 0,
    motivo         TEXT        NOT NULL DEFAULT '',
    classificada_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Boas pra trabalhar (job/worker): o que importa eh esforco e valor base.
CREATE TABLE IF NOT EXISTS cartas_job (
    codigo         TEXT PRIMARY KEY REFERENCES cartas(codigo) ON DELETE CASCADE,
    conta_id       TEXT        NOT NULL REFERENCES contas(id),
    -- copiados de `cartas` pra tabela ser legivel sozinha num cliente SQL,
    -- sem precisar de join. Sao reconstruidos por `banco reclassificar`.
    personagem     TEXT        NOT NULL DEFAULT '',
    serie          TEXT        NOT NULL DEFAULT '',
    esforco_total  INTEGER     NOT NULL DEFAULT 0,
    valor_base     INTEGER     NOT NULL DEFAULT 0,
    motivo         TEXT        NOT NULL DEFAULT '',
    classificada_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Candidatas a queimar: nao servem pra job nem sao desejadas.
CREATE TABLE IF NOT EXISTS cartas_burn (
    codigo         TEXT PRIMARY KEY REFERENCES cartas(codigo) ON DELETE CASCADE,
    conta_id       TEXT        NOT NULL REFERENCES contas(id),
    -- copiados de `cartas` pra tabela ser legivel sozinha num cliente SQL,
    -- sem precisar de join. Sao reconstruidos por `banco reclassificar`.
    personagem     TEXT        NOT NULL DEFAULT '',
    serie          TEXT        NOT NULL DEFAULT '',
    valor_base     INTEGER     NOT NULL DEFAULT 0,
    motivo         TEXT        NOT NULL DEFAULT '',
    queimada       BOOLEAN     NOT NULL DEFAULT FALSE,
    queimada_em    TIMESTAMPTZ,
    classificada_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Inventario: materiais que cada conta tem (gold, dust, itens de evento...)
-- ---------------------------------------------------------------------
-- Atualizado por `k!i` num intervalo longo, nao a cada drop: muda devagar e
-- cada leitura custa varios comandos (o inventario eh paginado).
-- `chave` eh o identificador do Karuta ("good dust"), `nome` eh o rotulo
-- legivel ("Dust (★★☆☆)").
-- `tipo` separa os dois inventarios do Karuta, que tem formato identico mas
-- comandos diferentes: 'item' vem do `k!i`, 'bit' vem do `k!bi`.
CREATE TABLE IF NOT EXISTS inventario (
    conta_id       TEXT        NOT NULL REFERENCES contas(id),
    tipo           TEXT        NOT NULL DEFAULT 'item'
                   CHECK (tipo IN ('item', 'bit')),
    chave          TEXT        NOT NULL,
    nome           TEXT        NOT NULL DEFAULT '',
    quantidade     INTEGER     NOT NULL DEFAULT 0 CHECK (quantidade >= 0),
    atualizado_em  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conta_id, tipo, chave)
);

-- Quando cada inventario foi lido pela ultima vez.
-- Existe separado de `inventario` porque conta com inventario VAZIO nao gera
-- linha nenhuma la; sem isso ela era reconsultada a cada ciclo pra sempre,
-- gastando 2 comandos por ciclo a toa.
CREATE TABLE IF NOT EXISTS inventario_leituras (
    conta_id   TEXT        NOT NULL REFERENCES contas(id),
    tipo       TEXT        NOT NULL,
    itens      INTEGER     NOT NULL DEFAULT 0,
    lido_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conta_id, tipo)
);

-- Historico de quantidades, pra dar pra ver evolucao ao longo do tempo.
CREATE TABLE IF NOT EXISTS inventario_historico (
    id             BIGSERIAL PRIMARY KEY,
    conta_id       TEXT        NOT NULL REFERENCES contas(id),
    tipo           TEXT        NOT NULL DEFAULT 'item',
    chave          TEXT        NOT NULL,
    quantidade     INTEGER     NOT NULL,
    registrado_em  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Icones de evento pegos
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eventos_icone (
    id                BIGSERIAL PRIMARY KEY,
    conta_id          TEXT        NOT NULL REFERENCES contas(id),
    mensagem_drop_id  TEXT        NOT NULL,
    icone             TEXT        NOT NULL,
    sucesso           BOOLEAN     NOT NULL,
    pego_em           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Indices
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS cartas_conta_idx     ON cartas (conta_id);
CREATE INDEX IF NOT EXISTS cartas_esforco_idx   ON cartas (esforco_total DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS cartas_desejos_idx   ON cartas (desejos DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS cartas_personagem_idx ON cartas (personagem);
-- fila do avaliador: cartas ainda sem k!wi
CREATE INDEX IF NOT EXISTS cartas_pendentes_idx ON cartas (conta_id)
    WHERE avaliada_em IS NULL;
CREATE INDEX IF NOT EXISTS pegas_conta_idx      ON cartas_pegas (conta_id, pega_em DESC);
CREATE INDEX IF NOT EXISTS pegas_codigo_idx     ON cartas_pegas (codigo);
CREATE INDEX IF NOT EXISTS avaliacoes_codigo_idx ON avaliacoes (codigo, avaliada_em DESC);
CREATE INDEX IF NOT EXISTS job_conta_idx        ON cartas_job (conta_id, esforco_total DESC);
CREATE INDEX IF NOT EXISTS desejadas_conta_idx  ON cartas_desejadas (conta_id, desejos DESC);
CREATE INDEX IF NOT EXISTS burn_conta_idx       ON cartas_burn (conta_id) WHERE NOT queimada;

-- ---------------------------------------------------------------------
-- atualizada_em automatico
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION tocar_atualizada_em() RETURNS TRIGGER AS $$
BEGIN
    NEW.atualizada_em = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS cartas_atualizada_em ON cartas;
CREATE TRIGGER cartas_atualizada_em
    BEFORE UPDATE ON cartas
    FOR EACH ROW EXECUTE FUNCTION tocar_atualizada_em();

-- ---------------------------------------------------------------------
-- Visoes de consulta
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_resumo_por_conta AS
SELECT c.id AS conta_id,
       COUNT(ca.codigo)                                    AS total_cartas,
       COUNT(ca.avaliada_em)                               AS avaliadas,
       -- COUNT(ca.codigo), nao COUNT(*): com LEFT JOIN e conta sem carta,
       -- COUNT(*) contaria a linha vazia do join como 1 pendente.
       COUNT(ca.codigo) FILTER (WHERE ca.avaliada_em IS NULL) AS pendentes,
       (SELECT COUNT(*) FROM cartas_job     j WHERE j.conta_id = c.id) AS boas_job,
       (SELECT COUNT(*) FROM cartas_desejadas d WHERE d.conta_id = c.id) AS desejadas,
       (SELECT COUNT(*) FROM cartas_burn    b WHERE b.conta_id = c.id
                                             AND NOT b.queimada)        AS burn_pendente,
       MAX(ca.esforco_total)                               AS melhor_esforco,
       MAX(ca.valor_base)                                  AS melhor_valor_base,
       MAX(ca.desejos)                                     AS mais_desejada
  FROM contas c
  LEFT JOIN cartas ca ON ca.conta_id = c.id
 GROUP BY c.id;

CREATE OR REPLACE VIEW vw_melhores_job AS
SELECT j.conta_id, j.codigo, ca.personagem, j.esforco_total, j.valor_base,
       ca.estrelas, ca.numero_print, ca.edicao, ca.condicao, j.motivo
  FROM cartas_job j
  JOIN cartas ca USING (codigo)
 ORDER BY j.esforco_total DESC, j.valor_base DESC;
"""

# Tabelas da primeira versao (em ingles), substituidas por este esquema.
LIMPEZA_LEGADO = """
DROP TABLE IF EXISTS picks;
DROP TABLE IF EXISTS event_grabs;
DROP TABLE IF EXISTS cards;
"""

# `CREATE TABLE IF NOT EXISTS` nao altera tabela que ja existe, entao colunas
# adicionadas depois precisam de ALTER explicito. Tudo idempotente: rodar
# varias vezes nao quebra nem duplica.
MIGRACOES = """
ALTER TABLE cartas ADD COLUMN IF NOT EXISTS circulacao INTEGER;
ALTER TABLE cartas ADD COLUMN IF NOT EXISTS taxa_reivindicacao SMALLINT;

-- avaliacoes: colunas fixas de metrica viraram JSONB. Migra o que ja existe
-- antes de remove-las, pra nao perder o historico.
-- nome e serie nas tabelas especializadas, pra dar pra ler cada uma sozinha
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['cartas_desejadas', 'cartas_job', 'cartas_burn'] LOOP
        EXECUTE format(
            'ALTER TABLE %I ADD COLUMN IF NOT EXISTS personagem TEXT NOT NULL DEFAULT ''''', t);
        EXECUTE format(
            'ALTER TABLE %I ADD COLUMN IF NOT EXISTS serie TEXT NOT NULL DEFAULT ''''', t);
    END LOOP;
END $$;

ALTER TABLE cartas ADD COLUMN IF NOT EXISTS ouro_burn INTEGER;
ALTER TABLE cartas ADD COLUMN IF NOT EXISTS poeira_burn INTEGER;
ALTER TABLE cartas ADD COLUMN IF NOT EXISTS poeira_tier TEXT NOT NULL DEFAULT '';

-- inventario ganhou `tipo` (item vs bit); a PK precisa inclui-lo, senao
-- "gold" (item) e "gold bit" colidiriam entre inventarios diferentes.
ALTER TABLE inventario ADD COLUMN IF NOT EXISTS tipo TEXT NOT NULL DEFAULT 'item';
ALTER TABLE inventario_historico ADD COLUMN IF NOT EXISTS tipo TEXT NOT NULL DEFAULT 'item';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.key_column_usage
         WHERE constraint_name = 'inventario_pkey' AND column_name = 'tipo'
    ) THEN
        ALTER TABLE inventario DROP CONSTRAINT IF EXISTS inventario_pkey;
        ALTER TABLE inventario ADD PRIMARY KEY (conta_id, tipo, chave);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'inventario_tipo_check'
    ) THEN
        ALTER TABLE inventario ADD CONSTRAINT inventario_tipo_check
            CHECK (tipo IN ('item', 'bit'));
    END IF;
END $$;

ALTER TABLE avaliacoes ADD COLUMN IF NOT EXISTS sucesso BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE avaliacoes ADD COLUMN IF NOT EXISTS extraido JSONB NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'avaliacoes' AND column_name = 'valor_base'
    ) THEN
        UPDATE avaliacoes SET extraido = extraido
            || jsonb_strip_nulls(jsonb_build_object(
                   'valor_base', valor_base,
                   'esforco_total', esforco_total,
                   'desejos', desejos))
         WHERE extraido = '{}'::jsonb;
        ALTER TABLE avaliacoes DROP COLUMN valor_base;
        ALTER TABLE avaliacoes DROP COLUMN esforco_total;
        ALTER TABLE avaliacoes DROP COLUMN desejos;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cartas_circulacao_check'
    ) THEN
        ALTER TABLE cartas ADD CONSTRAINT cartas_circulacao_check
            CHECK (circulacao IS NULL OR circulacao >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cartas_taxa_check'
    ) THEN
        ALTER TABLE cartas ADD CONSTRAINT cartas_taxa_check
            CHECK (taxa_reivindicacao IS NULL
                   OR taxa_reivindicacao BETWEEN 0 AND 100);
    END IF;
END $$;
"""


def resolver_dsn(dsn_config: str | None = None) -> str:
    """
    Precedencia: env KARUTA_DB_DSN > banco.dsn do config.yaml > padrao.
    O env existe pra manter senha fora de arquivo versionado.
    """
    return os.environ.get("KARUTA_DB_DSN") or dsn_config or DSN_PADRAO


def conectar(dsn: str | None = None) -> psycopg.Connection:
    """Conexao com autocommit: cada operacao daqui eh curta e independente."""
    return psycopg.connect(resolver_dsn(dsn), row_factory=dict_row, autocommit=True)


def criar_esquema(conn: psycopg.Connection, limpar_legado: bool = False) -> None:
    """Cria o que falta e aplica migracoes. Seguro de rodar a cada start."""
    if limpar_legado:
        conn.execute(LIMPEZA_LEGADO)
    conn.execute(ESQUEMA)
    conn.execute(MIGRACOES)


def garantir_contas(conn: psycopg.Connection, ids: list[str]) -> None:
    """`cartas` referencia `contas`, entao as contas precisam existir antes."""
    for cid in ids:
        conn.execute(
            "INSERT INTO contas (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (cid,)
        )


@contextmanager
def opcional(dsn: str | None = None, ativo: bool = True):
    """
    Conexao que nunca derruba o chamador: rende a conexao ou None.
    Usado no caminho do bot de drop, onde falha de banco nao pode custar pick.
    """
    if not ativo:
        yield None
        return
    conn = None
    try:
        conn = conectar(dsn)
    except Exception as e:
        logger.warning(f"banco indisponivel ({e}); seguindo sem gravar")
        yield None
        return
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==========================================================================
# escrita
# ==========================================================================

def registrar_pick(
    conn: psycopg.Connection,
    conta_id: str,
    mensagem_drop_id: str,
    indice_botao: int | None,
    sucesso: bool,
    codigo: str | None = None,
    personagem: str = "",
    condicao: str = "",
) -> int | None:
    """
    Grava a tentativa de pick no historico e devolve o id da linha.

    NAO insere em `cartas`: a tabela mestre so recebe carta ja avaliada. Mas
    o pick eh gravado na hora, sem esperar a avaliacao — ela leva ~100s e ja
    perdemos registros por ser interrompida no meio.

    O `codigo` costuma vir vazio aqui (so descobrimos depois, no `k!ci`);
    use `completar_pick` quando ele aparecer.
    """
    linha = conn.execute(
        """
        INSERT INTO cartas_pegas
            (conta_id, mensagem_drop_id, indice_botao, codigo, personagem,
             condicao, sucesso)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (conta_id, mensagem_drop_id, indice_botao, codigo, personagem,
         condicao, sucesso),
    ).fetchone()
    return linha["id"] if linha else None


def completar_pick(
    conn: psycopg.Connection,
    pick_id: int,
    codigo: str,
    personagem: str = "",
    condicao: str = "",
) -> None:
    """Preenche o codigo da carta no pick, depois que o `k!ci` o revelou."""
    conn.execute(
        """
        UPDATE cartas_pegas
           SET codigo = %s,
               personagem = COALESCE(NULLIF(%s, ''), personagem),
               condicao = COALESCE(NULLIF(%s, ''), condicao)
         WHERE id = %s
        """,
        (codigo, personagem, condicao, pick_id),
    )


# Cabecalhos com que o Karuta comeca cada resposta. O inner_text do Discord
# vem cheio de coisa antes disso (nome, tag do servidor, "APP VERIFICADO",
# horario, o eco do proprio comando) — cortar melhora muito a leitura e
# reduz o tamanho pela metade.
_CABECALHOS_RESPOSTA = (
    "Worker Details",
    "Card Details",
    "Character Lookup",
    "Character Results",
    "Card Collection",
    "Reminders",
)


def limpar_resposta(texto: str) -> str:
    """Corta o texto a partir do cabecalho da resposta do Karuta."""
    if not texto:
        return ""
    corte = min(
        (texto.find(h) for h in _CABECALHOS_RESPOSTA if texto.find(h) >= 0),
        default=-1,
    )
    limpo = texto[corte:] if corte >= 0 else texto
    return limpo.replace("\xa0", " ").strip()


def registrar_avaliacao_bruta(
    conn: psycopg.Connection,
    codigo: str,
    conta_id: str,
    comando: str,
    texto_bruto: str,
    extraido: dict | None = None,
) -> None:
    """
    Guarda a resposta do Karuta pra permitir reparse futuro.

    Registra TAMBEM os comandos que falharam (texto vazio, sucesso=False):
    antes eles sumiam sem deixar rastro, e uma carta com 2 linhas em vez de 3
    nao explicava o motivo.
    """
    limpo = limpar_resposta(texto_bruto)
    conn.execute(
        """
        INSERT INTO avaliacoes
            (codigo, conta_id, comando, sucesso, texto_bruto, extraido)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (codigo, conta_id, comando, bool(limpo), limpo[:20000],
         Jsonb(extraido or {})),
    )


def salvar_carta(
    conn: psycopg.Connection,
    codigo: str,
    conta_id: str,
    personagem: str = "",
    serie: str = "",
    estrelas: int = 0,
    numero_print: int = 0,
    edicao: int = 0,
    condicao: str = "",
    valor_base: int | None = None,
    esforco_total: int | None = None,
    desejos: int | None = None,
    circulacao: int | None = None,
    taxa_reivindicacao: int | None = None,
    ouro_burn: int | None = None,
    poeira_burn: int | None = None,
    poeira_tier: str = "",
    origem: str = "drop",
    mensagem_drop_id: str | None = None,
) -> None:
    """
    Insere/atualiza na tabela mestre.

    Em conflito, campos vazios nao sobrescrevem os ja preenchidos (COALESCE/
    NULLIF): a confirmacao do drop traz personagem/condicao, o k!wi traz
    esforco/valor, o k!ci traz atributos e o k!lu traz desejos — cada fonte
    completa a outra.
    """
    avaliada = valor_base is not None or esforco_total is not None
    conn.execute(
        f"""
        INSERT INTO cartas
            (codigo, conta_id, personagem, serie, estrelas, numero_print,
             edicao, condicao, valor_base, esforco_total, desejos,
             circulacao, taxa_reivindicacao, ouro_burn, poeira_burn,
             poeira_tier, origem, mensagem_drop_id, avaliada_em)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s,
                {'now()' if avaliada else 'NULL'})
        ON CONFLICT (codigo) DO UPDATE SET
            personagem    = COALESCE(NULLIF(EXCLUDED.personagem, ''), cartas.personagem),
            serie         = COALESCE(NULLIF(EXCLUDED.serie, ''), cartas.serie),
            estrelas      = GREATEST(cartas.estrelas, EXCLUDED.estrelas),
            numero_print  = COALESCE(NULLIF(EXCLUDED.numero_print, 0), cartas.numero_print),
            edicao        = COALESCE(NULLIF(EXCLUDED.edicao, 0), cartas.edicao),
            condicao      = COALESCE(NULLIF(EXCLUDED.condicao, ''), cartas.condicao),
            valor_base    = COALESCE(EXCLUDED.valor_base, cartas.valor_base),
            esforco_total = COALESCE(EXCLUDED.esforco_total, cartas.esforco_total),
            desejos       = COALESCE(EXCLUDED.desejos, cartas.desejos),
            circulacao    = COALESCE(EXCLUDED.circulacao, cartas.circulacao),
            taxa_reivindicacao = COALESCE(EXCLUDED.taxa_reivindicacao,
                                          cartas.taxa_reivindicacao),
            ouro_burn     = COALESCE(EXCLUDED.ouro_burn, cartas.ouro_burn),
            poeira_burn   = COALESCE(EXCLUDED.poeira_burn, cartas.poeira_burn),
            poeira_tier   = COALESCE(NULLIF(EXCLUDED.poeira_tier, ''),
                                     cartas.poeira_tier),
            avaliada_em   = COALESCE(EXCLUDED.avaliada_em, cartas.avaliada_em)
        """,
        (codigo, conta_id, personagem, serie, estrelas, numero_print, edicao,
         condicao, valor_base, esforco_total, desejos, circulacao,
         taxa_reivindicacao, ouro_burn, poeira_burn, poeira_tier, origem,
         mensagem_drop_id),
    )


def classificar(
    conn: psycopg.Connection,
    codigo: str,
    conta_id: str,
    classificacoes: dict[str, str],
    valor_base: int = 0,
    esforco_total: int = 0,
    desejos: int = 0,
    personagem: str = "",
    serie: str = "",
) -> None:
    """
    Coloca a carta nas tabelas especializadas.

    `classificacoes` mapeia nome -> motivo, ex:
        {"job": "esforco 112 >= 90", "desejada": "312 desejos >= 100"}
    A carta continua na mestre independente disso; aqui so entram os
    subconjuntos aos quais ela pertence.
    """
    if "desejada" in classificacoes:
        conn.execute(
            """
            INSERT INTO cartas_desejadas
                (codigo, conta_id, personagem, serie, desejos, motivo)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (codigo) DO UPDATE SET
                personagem = EXCLUDED.personagem, serie = EXCLUDED.serie,
                desejos = EXCLUDED.desejos, motivo = EXCLUDED.motivo
            """,
            (codigo, conta_id, personagem, serie, desejos,
             classificacoes["desejada"]),
        )
    if "job" in classificacoes:
        conn.execute(
            """
            INSERT INTO cartas_job
                (codigo, conta_id, personagem, serie, esforco_total, valor_base, motivo)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (codigo) DO UPDATE SET
                personagem    = EXCLUDED.personagem, serie = EXCLUDED.serie,
                esforco_total = EXCLUDED.esforco_total,
                valor_base    = EXCLUDED.valor_base,
                motivo        = EXCLUDED.motivo
            """,
            (codigo, conta_id, personagem, serie, esforco_total, valor_base,
             classificacoes["job"]),
        )
    if "burn" in classificacoes:
        conn.execute(
            """
            INSERT INTO cartas_burn
                (codigo, conta_id, personagem, serie, valor_base, motivo)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (codigo) DO UPDATE SET
                personagem = EXCLUDED.personagem, serie = EXCLUDED.serie,
                valor_base = EXCLUDED.valor_base, motivo = EXCLUDED.motivo
            """,
            (codigo, conta_id, personagem, serie, valor_base,
             classificacoes["burn"]),
        )


def salvar_inventario(
    conn: psycopg.Connection, conta_id: str, itens: dict[str, tuple[str, int]],
    tipo: str = "item", zerar_ausentes: bool = True,
) -> int:
    """
    Substitui o inventario da conta por `itens` = {chave: (nome, quantidade)}.

    Itens que sumiram sao zerados em vez de apagados: assim da pra distinguir
    "acabou" de "nunca teve", e o historico nao ganha buracos.

    ATENCAO: `itens` precisa ser o inventario COMPLETO. Passar um subconjunto
    zera todo o resto — foi assim que um teste apagou as quantidades reais.
    Use `zerar_ausentes=False` pra atualizacao parcial.

    Grava no historico so quando a quantidade muda, pra nao inflar a tabela a
    cada leitura.
    """
    gravados = 0
    for chave, (nome, qtd) in itens.items():
        anterior = conn.execute(
            "SELECT quantidade FROM inventario"
            " WHERE conta_id=%s AND tipo=%s AND chave=%s",
            (conta_id, tipo, chave),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO inventario
                (conta_id, tipo, chave, nome, quantidade, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (conta_id, tipo, chave) DO UPDATE SET
                nome = COALESCE(NULLIF(EXCLUDED.nome, ''), inventario.nome),
                quantidade = EXCLUDED.quantidade,
                atualizado_em = now()
            """,
            (conta_id, tipo, chave, nome, qtd),
        )
        if anterior is None or anterior["quantidade"] != qtd:
            conn.execute(
                "INSERT INTO inventario_historico"
                " (conta_id, tipo, chave, quantidade) VALUES (%s, %s, %s, %s)",
                (conta_id, tipo, chave, qtd),
            )
        gravados += 1

    if itens and zerar_ausentes:
        # RETURNING pra registrar no historico tambem os que foram zerados.
        # Sem isso a queda pra zero sumia sem deixar rastro, e nao dava pra
        # saber quando o item acabou.
        zerados = conn.execute(
            """
            UPDATE inventario SET quantidade = 0, atualizado_em = now()
             WHERE conta_id = %s AND tipo = %s AND chave <> ALL(%s)
               AND quantidade <> 0
            RETURNING chave
            """,
            (conta_id, tipo, list(itens)),
        ).fetchall()
        for z in zerados:
            conn.execute(
                "INSERT INTO inventario_historico"
                " (conta_id, tipo, chave, quantidade) VALUES (%s, %s, %s, 0)",
                (conta_id, tipo, z["chave"]),
            )
    return gravados


def listar_inventario(
    conn: psycopg.Connection, conta_id: str | None = None, incluir_zerados: bool = False
) -> list[dict]:
    sql = "SELECT * FROM inventario WHERE TRUE"
    params: list = []
    if conta_id:
        sql += " AND conta_id = %s"
        params.append(conta_id)
    if not incluir_zerados:
        sql += " AND quantidade > 0"
    sql += " ORDER BY conta_id, quantidade DESC"
    return conn.execute(sql, tuple(params)).fetchall()


def registrar_leitura_inventario(
    conn: psycopg.Connection, conta_id: str, tipo: str, itens: int
) -> None:
    """
    Marca que este inventario foi consultado agora.

    Chamado mesmo quando a leitura volta VAZIA — eh justamente o caso que
    precisa ser registrado, senao a conta eh reconsultada a cada ciclo.
    """
    conn.execute(
        """
        INSERT INTO inventario_leituras (conta_id, tipo, itens, lido_em)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (conta_id, tipo) DO UPDATE SET
            itens = EXCLUDED.itens, lido_em = now()
        """,
        (conta_id, tipo, itens),
    )


def inventario_desatualizado(
    conn: psycopg.Connection, conta_id: str, minutos: int, tipo: str = "item"
) -> bool:
    """True se faz mais de `minutos` que este inventario da conta foi lido."""
    r = conn.execute(
        "SELECT EXTRACT(EPOCH FROM (now() - lido_em)) / 60 AS idade"
        "  FROM inventario_leituras WHERE conta_id = %s AND tipo = %s",
        (conta_id, tipo),
    ).fetchone()
    if r is None or r["idade"] is None:
        return True
    return float(r["idade"]) >= minutos


def limpar_classificacoes(conn: psycopg.Connection) -> None:
    """Esvazia as tabelas especializadas, preservando a mestre."""
    for tabela in TABELAS_CLASSIFICACAO.values():
        conn.execute(f"DELETE FROM {tabela}")


def cartas_avaliadas(conn: psycopg.Connection) -> list[dict]:
    """Todas as cartas ja avaliadas, pra reclassificacao em lote."""
    return conn.execute(
        "SELECT codigo, conta_id, personagem, serie, valor_base, esforco_total,"
        "       desejos, ouro_burn"
        "  FROM cartas WHERE avaliada_em IS NOT NULL ORDER BY codigo"
    ).fetchall()


def registrar_icone_evento(
    conn: psycopg.Connection, conta_id: str, mensagem_drop_id: str,
    icone: str, sucesso: bool,
) -> None:
    conn.execute(
        "INSERT INTO eventos_icone (conta_id, mensagem_drop_id, icone, sucesso)"
        " VALUES (%s, %s, %s, %s)",
        (conta_id, mensagem_drop_id, icone, sucesso),
    )


def drops_com_icone_ja_clicado(
    conn: psycopg.Connection, conta_id: str, horas: int = 6,
) -> set[str]:
    """
    Drops em que esta conta JA clicou o icone, nas ultimas `horas`.

    Existe porque `_drops_vistos` vive so em memoria: todo restart do bot
    esquecia o que ja tinha sido clicado, e um drop recente virava candidato
    de novo. Como no Discord clicar de novo REMOVE a reaction, o bot desfazia
    a propria coleta — flagrado em 2026-08-06 23:22, contagem 2 -> 1.

    Janela curta porque drop velho nem eh candidato (ver
    `idade_maxima_drop_segundos`); a consulta so precisa cobrir o que ainda
    esta no prazo.
    """
    linhas = conn.execute(
        "SELECT DISTINCT mensagem_drop_id FROM eventos_icone"
        " WHERE conta_id = %s AND pego_em > now() - make_interval(hours => %s)",
        (conta_id, horas),
    ).fetchall()
    return {r["mensagem_drop_id"] for r in linhas}


# ==========================================================================
# leitura
# ==========================================================================

def codigos_avaliados(conn: psycopg.Connection, conta_id: str | None = None) -> set[str]:
    """Cartas que ja passaram por avaliacao — o scan pode pular todas."""
    sql = "SELECT codigo FROM cartas WHERE avaliada_em IS NOT NULL"
    params: tuple = ()
    if conta_id:
        sql += " AND conta_id = %s"
        params = (conta_id,)
    return {r["codigo"] for r in conn.execute(sql, params).fetchall()}


def codigos_pendentes(conn: psycopg.Connection, conta_id: str | None = None) -> list[str]:
    """Cartas conhecidas que ainda nao foram avaliadas."""
    sql = "SELECT codigo FROM cartas WHERE avaliada_em IS NULL"
    params: tuple = ()
    if conta_id:
        sql += " AND conta_id = %s"
        params = (conta_id,)
    return [r["codigo"] for r in conn.execute(sql + " ORDER BY obtida_em", params).fetchall()]


def melhores_job(
    conn: psycopg.Connection, limite: int = 50, conta_id: str | None = None
) -> list[dict]:
    sql = "SELECT * FROM vw_melhores_job"
    params: list = []
    if conta_id:
        sql += " WHERE conta_id = %s"
        params.append(conta_id)
    sql += " LIMIT %s"
    params.append(limite)
    return conn.execute(sql, tuple(params)).fetchall()


def listar(
    conn: psycopg.Connection,
    tabela: str,
    limite: int = 50,
    conta_id: str | None = None,
) -> list[dict]:
    """Lista uma das tabelas especializadas com dados da mestre."""
    if tabela not in set(TABELAS_CLASSIFICACAO.values()):
        raise ValueError(f"tabela invalida: {tabela}")
    ordem = {
        "cartas_job": "t.esforco_total DESC",
        "cartas_desejadas": "t.desejos DESC",
        "cartas_burn": "t.valor_base ASC",
    }[tabela]
    sql = f"""
        -- personagem e serie ja vivem em `t`; aqui so o que falta.
        SELECT t.*, c.estrelas, c.numero_print, c.edicao, c.condicao
          FROM {tabela} t JOIN cartas c USING (codigo)
    """
    params: list = []
    if conta_id:
        sql += " WHERE t.conta_id = %s"
        params.append(conta_id)
    sql += f" ORDER BY {ordem} LIMIT %s"
    params.append(limite)
    return conn.execute(sql, tuple(params)).fetchall()


def resumo(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM vw_resumo_por_conta ORDER BY conta_id").fetchall()
