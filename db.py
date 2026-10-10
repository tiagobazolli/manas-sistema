"""Armazenamento do Manas.

Usa o Postgres da Railway quando DATABASE_URL existe (produção) e um
arquivo SQLite local quando não existe (teste no computador).

Todos os dados ficam numa única tabela, manas_docs, separada das tabelas
do Assessor.IA: cada linha é um registro (uma venda, uma entrada de
estoque, uma conta...) guardado como JSON.
"""
import json
import os
import sqlite3
import threading

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USA_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if USA_POSTGRES:
    import psycopg2
    import psycopg2.extras

_SQLITE_PATH = os.environ.get("MANAS_SQLITE", "manas_local.db")
_lock = threading.Lock()

COLECOES = {"entradas", "vendas", "contas", "pagamentos", "despesas", "repasses", "config"}


def _conectar():
    if USA_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    conn = sqlite3.connect(_SQLITE_PATH)
    return conn


def _ph():
    return "%s" if USA_POSTGRES else "?"


def criar_tabela():
    tipo = "JSONB" if USA_POSTGRES else "TEXT"
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS manas_docs (
                colecao TEXT NOT NULL,
                id TEXT NOT NULL,
                data {tipo} NOT NULL,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (colecao, id)
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def _ler_json(valor):
    if isinstance(valor, (dict, list)):
        return valor
    return json.loads(valor)


def listar_tudo():
    """Devolve {colecao: [{id, data}, ...]} com todos os registros."""
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute("SELECT colecao, id, data FROM manas_docs ORDER BY colecao, id")
        saida = {c: [] for c in COLECOES}
        for colecao, id_, data in cur.fetchall():
            saida.setdefault(colecao, []).append({"id": id_, "data": _ler_json(data)})
        return saida
    finally:
        conn.close()


def contar():
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM manas_docs")
        return cur.fetchone()[0]
    finally:
        conn.close()


def obter(colecao, id_):
    p = _ph()
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT data FROM manas_docs WHERE colecao={p} AND id={p}", (colecao, id_))
        row = cur.fetchone()
        return _ler_json(row[0]) if row else None
    finally:
        conn.close()


def gravar(colecao, id_, data):
    """Grava o registro inteiro (cria ou substitui)."""
    p = _ph()
    texto = json.dumps(data, ensure_ascii=False)
    conn = _conectar()
    try:
        cur = conn.cursor()
        if USA_POSTGRES:
            cur.execute(
                """INSERT INTO manas_docs (colecao, id, data, atualizado_em)
                   VALUES (%s, %s, %s::jsonb, CURRENT_TIMESTAMP)
                   ON CONFLICT (colecao, id)
                   DO UPDATE SET data = EXCLUDED.data, atualizado_em = CURRENT_TIMESTAMP""",
                (colecao, id_, texto),
            )
        else:
            cur.execute(
                f"""INSERT INTO manas_docs (colecao, id, data, atualizado_em)
                    VALUES ({p}, {p}, {p}, CURRENT_TIMESTAMP)
                    ON CONFLICT (colecao, id)
                    DO UPDATE SET data = excluded.data, atualizado_em = CURRENT_TIMESTAMP""",
                (colecao, id_, texto),
            )
        conn.commit()
    finally:
        conn.close()


def _mesclar(base, novo):
    for k, v in novo.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _mesclar(base[k], v)
        else:
            base[k] = v
    return base


def atualizar(colecao, id_, campos):
    """Mescla campos num registro existente. Devolve False se não existir."""
    with _lock:
        atual = obter(colecao, id_)
        if atual is None:
            return False
        gravar(colecao, id_, _mesclar(atual, campos))
        return True


def excluir(colecao, id_):
    p = _ph()
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM manas_docs WHERE colecao={p} AND id={p}", (colecao, id_))
        conn.commit()
    finally:
        conn.close()
