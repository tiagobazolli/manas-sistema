"""Manas · Caixa e Estoque — servidor.

Serve a página do sistema e guarda os dados no Postgres.
Acesso protegido por uma senha única da loja (variável MANAS_SENHA).
"""
import hmac
import json
import os
import re
from datetime import timedelta

from flask import Flask, jsonify, redirect, request, send_from_directory, session

import db

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY") or "troque-esta-chave-no-railway"
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=90),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RAILWAY_ENVIRONMENT")),
)

SENHA = os.environ.get("MANAS_SENHA", "")
ID_OK = re.compile(r"^[A-Za-z0-9_\-.:@+~]{1,120}$")


def _inicializar():
    db.criar_tabela()
    # Primeira vez: carrega os dados iniciais (configurações e contas já cadastradas no teste)
    if db.contar() == 0:
        caminho = os.path.join(BASE, "dados_iniciais.json")
        if os.path.exists(caminho):
            with open(caminho, encoding="utf-8") as f:
                for colecao, docs in json.load(f).items():
                    for id_, data in docs.items():
                        db.gravar(colecao, id_, data)


_inicializar()


def _logada():
    return session.get("ok") is True


# ---------- páginas ----------

@app.get("/")
def inicio():
    if not _logada():
        return redirect("/entrar")
    resp = send_from_directory(STATIC, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/entrar")
def tela_login():
    if _logada():
        return redirect("/")
    return send_from_directory(STATIC, "entrar.html")


@app.post("/entrar")
def fazer_login():
    if not SENHA:
        return redirect("/entrar?erro=config")
    senha = (request.form.get("senha") or "").strip()
    if hmac.compare_digest(senha.encode(), SENHA.encode()):
        session.permanent = True
        session["ok"] = True
        return redirect("/")
    return redirect("/entrar?erro=senha")


@app.get("/sair")
def sair():
    session.clear()
    return redirect("/entrar")


# Arquivos do app instalável (ícones, manifesto, service worker)
@app.get("/manifest.webmanifest")
def manifesto():
    return send_from_directory(STATIC, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(STATIC, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/static/<path:arquivo>")
def estaticos(arquivo):
    return send_from_directory(STATIC, arquivo)


# ---------- API de dados ----------

def _exige_login():
    if not _logada():
        return jsonify({"erro": "login"}), 401
    return None


def _valida(colecao, id_):
    return colecao in db.COLECOES and ID_OK.match(id_ or "")


@app.get("/api/dados")
def api_dados():
    bloqueio = _exige_login()
    if bloqueio:
        return bloqueio
    return jsonify(db.listar_tudo())


@app.route("/api/doc/<colecao>/<id_>", methods=["PUT", "PATCH", "DELETE"])
def api_doc(colecao, id_):
    bloqueio = _exige_login()
    if bloqueio:
        return bloqueio
    if not _valida(colecao, id_):
        return jsonify({"erro": "registro inválido"}), 400
    if request.method == "DELETE":
        db.excluir(colecao, id_)
        return jsonify({"ok": True})
    corpo = request.get_json(silent=True)
    if not isinstance(corpo, dict):
        return jsonify({"erro": "conteúdo inválido"}), 400
    if request.method == "PUT":
        db.gravar(colecao, id_, corpo)
        return jsonify({"ok": True})
    if not db.atualizar(colecao, id_, corpo):
        return jsonify({"erro": "registro não encontrado"}), 404
    return jsonify({"ok": True})


@app.get("/saude")
def saude():
    return jsonify({"ok": True, "banco": "postgres" if db.USA_POSTGRES else "sqlite"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=False)
