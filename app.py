# -*- coding: utf-8 -*-
"""
app.py — Backend web do Assessor.IA (pivô da Semana 7: tiramos a
dependência de WhatsApp/Twilio/Meta, que travou em problemas de
plataforma fora do nosso controle). Agora o usuário interage direto
pela página web: sobe o extrato, responde as pendências ali mesmo.

Reaproveita 100% do que já foi validado nas Semanas 3-6 (pipeline de
extração + categorizador) — só a "porta de entrada" mudou.
"""
import calendar
import hmac
import os
import tempfile
from datetime import date, datetime, timedelta
from html import escape
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from db import conectar, novo_id, criar_schema
from pipeline import processar_extrato
from categorizador import categorizar, registrar_emprestimo
from detectar_banco import detectar_banco_pdf, detectar_banco_ofx
from extrator import extrair_saldo_ofx
import saldo_contas as sc
from parser_fatura import eh_fatura, parse_fatura
from parser_manual import extrair_valor_e_descricao
import contas_fixas as cf

app = Flask(__name__)
CORS(app)  # mantido por segurança, mesmo servindo tudo da mesma origem agora

CATEGORIAS_DESPESA = [
    "Moradia", "Transporte", "Alimentação", "Saúde e Bem-estar", "Educação",
    "Assinaturas e Serviços", "Lazer e Entretenimento", "Família",
    "Impostos e Taxas", "Investimentos e Reserva", "Compras Online",
    "Pets", "Cuidados Pessoais e Beleza", "Presentes Dados", "Viagem", "Outros/Diversos",
]
CATEGORIAS_RECEITA = [
    "Salário / Renda Fixa", "Rendimentos e Investimentos", "Trabalho Extra / Comissões",
    "Reembolsos e Devoluções", "Presentes e Doações Recebidas", "Outros/Diversos",
]
CATEGORIAS_VALIDAS = CATEGORIAS_DESPESA + CATEGORIAS_RECEITA  # usado na validação do backend, aceita as duas listas

_CATEGORIAS_ESPECIAIS = ("AUTOTRANSFERENCIA", "EXCLUIDO_TERCEIRO", "EXCLUIDO_EMPRESTIMO", "PERGUNTA_CONCILIACAO_EMPRESTIMO")

# Categoria provisória pro pagamento em bloco da fatura do cartão, quando
# reconhecido sozinho via conta fixa marcada "é fatura de cartão de
# crédito" (contas_fixas.eh_fatura_cartao) — conta normal no resumo (não
# está na lista de ignoradas) até a fatura detalhada daquele mês chegar e
# o app.py trocar pra 'Fatura decomposta (ver itens)', que aí sim é
# ignorada (pra não contar o gasto duas vezes, uma em bloco e outra item
# a item). Nunca deixa o gasto invisível enquanto isso não acontece.
CATEGORIA_FATURA_PROVISORIA = "Cartão de Crédito (fatura)"

# Rótulo amigável pra cada conta_identificador usado em lancamentos, pro
# saldo por banco no painel — "fatura_cartao" e "dinheiro" não são contas
# bancárias de verdade, mas aparecem como linha própria mesmo assim (são
# dinheiro que já saiu/entrou e não tem como atribuir a um banco específico).
BANCOS_LABEL = {
    "sicoob": "Sicoob",
    "santander": "Santander",
    "sicredi": "Sicredi",
    "caixa": "Caixa",
    "bb": "Banco do Brasil",
    "picpay": "PicPay",
    "desconhecida": "Banco não identificado",
    "fatura_cartao": "Fatura detalhada (itens do cartão)",
    "dinheiro": "Dinheiro / lançado na mão",
}

# Subgrupos sugeridos por categoria — pra quem não lembra o que era um
# lançamento meses depois (ex: "DEB.CTA.FATURA" não diz nada sozinho, mas
# Moradia > Água já diz). Opcional: lançamento sem subgrupo continua
# funcionando normal, só não aparece detalhado nessa camada extra.
# Categoria fora daqui simplesmente não tem sugestão — o usuário ainda
# pode criar uma na hora, do mesmo jeito que já funciona pra categoria.
SUBCATEGORIAS_PADRAO = {
    "Moradia": ["Água", "Luz", "Internet", "Gás", "Aluguel/Financiamento", "Condomínio"],
    "Transporte": ["Combustível", "Transporte Público", "Aplicativo (Uber/99)", "Manutenção do Veículo", "Estacionamento/Pedágio"],
    "Alimentação": ["Supermercado", "Feira/Hortifruti", "Padaria"],
    "Saúde e Bem-estar": ["Plano de Saúde", "Farmácia", "Consultas/Exames", "Academia"],
    "Assinaturas e Serviços": ["Streaming", "Telefonia", "Software/Apps"],
    "Lazer e Entretenimento": ["Restaurante/Bar", "Viagem", "Cinema/Shows", "Hobbies"],
    "Família": ["Filhos", "Pais", "Presentes"],
    "Educação": ["Mensalidade", "Material", "Cursos"],
    "Impostos e Taxas": ["IPVA", "IPTU", "Tarifas Bancárias"],
    "Compras Online": ["Marketplace", "Roupas/Calçados"],
}


def _ajustar_categoria_por_tipo(categoria: str, tipo: str) -> str:
    """A memória (entidade/CNPJ) não sabe se a transação é entrada ou
    saída — só sabe "quem é". Se um crédito bate com uma categoria que
    só faz sentido pra despesa (ex: um estorno do Mercado Livre virando
    'Compras Online'), é quase sempre reembolso/devolução, não receita
    de verdade — reclassifica pra não poluir o gráfico de receitas."""
    if tipo == "C" and categoria not in CATEGORIAS_RECEITA and categoria not in _CATEGORIAS_ESPECIAIS:
        return "Reembolsos e Devoluções"
    return categoria


# =========================================================
# Usuários (participantes do teste)
# =========================================================
def criar_usuario(nome: str) -> str:
    uid = novo_id()
    con = conectar()
    # 'identificador' guarda um apelido/nome só pra exibição — não é
    # mais um número de WhatsApp, já que não dependemos mais dele
    con.execute("INSERT INTO usuarios (id, nome, whatsapp_numero, criado_em) VALUES (?,?,?,?)",
                (uid, nome, uid, datetime.utcnow().isoformat(timespec="seconds")))
    con.commit()
    con.close()
    return uid


def proxima_pergunta_pendente(usuario_id: str):
    con = conectar()
    row = con.execute(
        "SELECT * FROM perguntas_pendentes WHERE usuario_id=? AND respondida=0 ORDER BY rowid LIMIT 1",
        (usuario_id,),
    ).fetchone()
    con.close()
    return row


def criar_pergunta(usuario_id: str, lancamento_id: str, tipo: str, texto: str):
    con = conectar()
    con.execute(
        "INSERT INTO perguntas_pendentes (id, usuario_id, lancamento_id, tipo_pergunta, texto_pergunta) VALUES (?,?,?,?,?)",
        (novo_id(), usuario_id, lancamento_id, tipo, texto),
    )
    con.commit()
    con.close()


def salvar_lancamento(usuario_id, t, origem, conta_identificador, categoria=None, confianca=None, subcategoria=None):
    con = conectar()
    lid = novo_id()
    con.execute(
        """INSERT INTO lancamentos
           (id, usuario_id, data, descricao_original, valor, tipo, categoria, subcategoria, origem, conta_identificador, confianca_categoria)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (lid, usuario_id, t["data"], t["descricao"], t["valor"], t["tipo"], categoria, subcategoria, origem, conta_identificador, confianca),
    )
    con.commit()
    con.close()
    # baixa automática de conta fixa — só faz sentido pra despesa já
    # categorizada (as outras chamadas, de pendência, não passam categoria).
    # Envolto em try/except porque isso é um bônus: um bug aqui nunca pode
    # impedir o lançamento em si de ser salvo.
    if categoria and t["tipo"] == "D":
        try:
            cf.tentar_baixar_automatico(usuario_id, lid)
        except Exception:
            pass
    return lid


# =========================================================
# Processar um extrato recebido (mesma lógica de antes, só que quem
# chama agora é a rota de upload, não um webhook de mensageria)
# =========================================================
def _conciliar_fatura_pendente(usuario_id: str, total_fatura: float | None) -> bool:
    """Depois de decompor uma fatura, procura o lançamento em bloco
    correspondente (o 'DEB.CTA.FATURA' do extrato da conta) e resolve
    sozinho, pra não contar o mesmo dinheiro duas vezes (uma em bloco,
    outra item a item). Dois jeitos desse lançamento em bloco existir:
    1) ainda com uma pergunta pendente (fluxo de sempre, categoria
       desconhecida esperando resposta do usuário), ou
    2) já resolvido sozinho como CATEGORIA_FATURA_PROVISORIA, via conta
       fixa marcada "é fatura de cartão de crédito" — sem pergunta
       nenhuma pra responder, só precisa trocar a categoria provisória
       pela definitiva agora que o detalhamento chegou."""
    if total_fatura is None:
        return False
    con = conectar()
    lanc = con.execute(
        """SELECT l.id as lancamento_id, p.id as pergunta_id FROM lancamentos l
           JOIN perguntas_pendentes p ON p.lancamento_id = l.id AND p.respondida=0
           WHERE l.usuario_id=? AND UPPER(l.descricao_original) LIKE ?
           AND ABS(l.valor - ?) < 0.02 LIMIT 1""",
        (usuario_id, "%FATURA%", total_fatura),
    ).fetchone()
    lancamento_id = lanc["lancamento_id"] if lanc else None
    pergunta_id = lanc["pergunta_id"] if lanc else None

    if not lancamento_id:
        lanc2 = con.execute(
            """SELECT id FROM lancamentos WHERE usuario_id=? AND UPPER(descricao_original) LIKE ?
               AND ABS(valor - ?) < 0.02 AND categoria=? AND status_conciliacao != 'conciliado' LIMIT 1""",
            (usuario_id, "%FATURA%", total_fatura, CATEGORIA_FATURA_PROVISORIA),
        ).fetchone()
        lancamento_id = lanc2["id"] if lanc2 else None

    if not lancamento_id:
        con.close()
        return False

    con.execute("UPDATE lancamentos SET categoria='Fatura decomposta (ver itens)', status_conciliacao='conciliado' WHERE id=?",
                (lancamento_id,))
    if pergunta_id:
        con.execute("UPDATE perguntas_pendentes SET respondida=1, resposta='conciliado automaticamente com a fatura detalhada' WHERE id=?",
                    (pergunta_id,))
    con.commit()
    con.close()
    return True


def processar_extrato_recebido(usuario_id: str, caminho_arquivo: str) -> dict:
    ext = caminho_arquivo.rsplit(".", 1)[-1].lower()
    texto_bruto = ""

    if ext == "pdf":
        import subprocess
        leitura = subprocess.run(["pdftotext", "-layout", caminho_arquivo, "-"], capture_output=True, text=True)
        if "password" in (leitura.stderr or "").lower():
            return {"erro": "Esse PDF tem senha, e eu não consigo abrir PDF protegido. Baixa o extrato "
                            "de novo sem senha (ou em OFX, se o app do banco oferecer) e manda aqui."}
        texto_bruto = leitura.stdout
        if eh_fatura(texto_bruto):
            return _processar_fatura(usuario_id, caminho_arquivo)

    if ext == "pdf":
        banco = detectar_banco_pdf(caminho_arquivo)
    elif ext == "ofx":
        banco = detectar_banco_ofx(caminho_arquivo)
    else:
        banco = None
    conta_id = banco or "desconhecida"

    try:
        resultado = processar_extrato(caminho_arquivo, banco)
    except RuntimeError as e:
        return {"erro": str(e)}
    transacoes = resultado["transacoes"]
    if not transacoes:
        texto_sem_mov = (texto_bruto if ext == "pdf" else "").upper()
        if any(m in texto_sem_mov for m in ("SEM TRANSA", "SEM MOVIMENTA", "NENHUMA MOVIMENTA", "NENHUM LANÇAMENTO")):
            return {"erro": "Esse extrato não tem nenhuma movimentação no período escolhido. "
                            "Gera de novo no app do banco com um período que tenha lançamentos e manda aqui."}
        nome_banco = f" ({BANCOS_LABEL.get(banco, banco)})" if banco else ""
        return {"erro": f"Abri o arquivo, mas não achei nenhum lançamento nesse extrato{nome_banco}. "
                        "Se puder, manda em OFX — ou me avisa que eu ajusto a leitura desse modelo de PDF."}

    n_alta_media, n_perguntas = 0, 0
    for t in transacoes:
        cat = categorizar(usuario_id, t["descricao"], t["valor"], t["data"], conta_id)
        if cat is not None and cat.categoria not in _CATEGORIAS_ESPECIAIS:
            cat.categoria = _ajustar_categoria_por_tipo(cat.categoria, t["tipo"])
        if cat is None:
            conta_fatura = None
            try:
                conta_fatura = cf.tentar_casar_fatura(usuario_id, t)
            except Exception:
                pass  # mesma lógica do try/except de baixa automática — bônus, nunca trava o salvamento
            if conta_fatura:
                lid = salvar_lancamento(usuario_id, t, f"extrato_{'ofx' if ext=='ofx' else 'pdf'}", conta_id,
                                         categoria=CATEGORIA_FATURA_PROVISORIA)
                try:
                    cf.registrar_baixa_fatura(usuario_id, conta_fatura["id"], lid, t["valor"], t["data"])
                except Exception:
                    pass
                n_alta_media += 1  # reconhecido sozinho — não vira pergunta
            else:
                lid = salvar_lancamento(usuario_id, t, f"extrato_{'ofx' if ext=='ofx' else 'pdf'}", conta_id)
                criar_pergunta(usuario_id, lid, "categoria_desconhecida",
                                f"Não reconheci: \"{t['descricao']}\" de R${t['valor']:.2f} em {t['data']}. Qual categoria?")
                n_perguntas += 1
        elif cat.categoria == "PERGUNTA_CONCILIACAO_EMPRESTIMO":
            lid = salvar_lancamento(usuario_id, t, f"extrato_{'ofx' if ext=='ofx' else 'pdf'}", conta_id)
            criar_pergunta(usuario_id, lid, "conciliacao_emprestimo",
                            f"{cat.origem.capitalize()}. O PIX de R${t['valor']:.2f} em {t['data']} é essa devolução?")
            n_perguntas += 1
        else:
            salvar_lancamento(usuario_id, t, f"extrato_{'ofx' if ext=='ofx' else 'pdf'}", conta_id,
                               categoria=cat.categoria, confianca=cat.confianca, subcategoria=cat.subcategoria)
            if cat.confianca in ("alta", "media"):
                n_alta_media += 1

    if ext == "ofx" and banco:
        try:
            saldo_ofx = extrair_saldo_ofx(caminho_arquivo)
            if saldo_ofx:
                sc.capturar_saldo_ofx(usuario_id, banco, BANCOS_LABEL.get(banco, banco.capitalize()),
                                       saldo_ofx["saldo"], saldo_ofx["data"])
        except Exception:
            pass  # saldo é um bônus — nunca pode travar o processamento do extrato em si

    return {
        "banco": banco or "não identificado",
        "total_lancamentos": len(transacoes),
        "categorizados_automaticamente": n_alta_media,
        "pendencias_criadas": n_perguntas,
    }


def _processar_fatura(usuario_id: str, caminho_arquivo: str) -> dict:
    resultado = parse_fatura(caminho_arquivo)
    transacoes = resultado["transacoes"]
    conta_id = "fatura_cartao"

    n_alta_media, n_perguntas = 0, 0
    for t in transacoes:
        cat = categorizar(usuario_id, t["descricao"], t["valor"], t["data"], conta_id)
        if cat is not None and cat.categoria not in _CATEGORIAS_ESPECIAIS:
            cat.categoria = _ajustar_categoria_por_tipo(cat.categoria, t["tipo"])
        if cat is None:
            lid = salvar_lancamento(usuario_id, t, "fatura_cartao", conta_id)
            criar_pergunta(usuario_id, lid, "categoria_desconhecida",
                            f"Não reconheci na fatura: \"{t['descricao']}\" de R${t['valor']:.2f} em {t['data']}. Qual categoria?")
            n_perguntas += 1
        elif cat.categoria == "PERGUNTA_CONCILIACAO_EMPRESTIMO":
            lid = salvar_lancamento(usuario_id, t, "fatura_cartao", conta_id)
            criar_pergunta(usuario_id, lid, "conciliacao_emprestimo",
                            f"{cat.origem.capitalize()}. A compra de R${t['valor']:.2f} em {t['data']} é essa devolução?")
            n_perguntas += 1
        else:
            salvar_lancamento(usuario_id, t, "fatura_cartao", conta_id, categoria=cat.categoria, confianca=cat.confianca, subcategoria=cat.subcategoria)
            if cat.confianca in ("alta", "media"):
                n_alta_media += 1

    conciliou = _conciliar_fatura_pendente(usuario_id, resultado["total_fatura"])

    return {
        "banco": "fatura de cartão",
        "total_lancamentos": len(transacoes),
        "categorizados_automaticamente": n_alta_media,
        "pendencias_criadas": n_perguntas,
        "conciliado_com_extrato": conciliou,
    }


# =========================================================
# Responder a uma pergunta pendente (agora vem do formulário web,
# não mais de uma mensagem de texto livre)
# =========================================================
def processar_resposta(usuario_id: str, pergunta, texto_resposta: str) -> None:
    con = conectar()
    texto_upper = texto_resposta.upper()

    if pergunta["tipo_pergunta"] == "conciliacao_emprestimo":
        if any(p in texto_upper for p in ("SIM", "É ESSA", "FOI ESSA")):
            con.execute(
                "UPDATE lancamentos SET categoria='Empréstimo devolvido', status_conciliacao='conciliado' WHERE id=?",
                (pergunta["lancamento_id"],),
            )
        else:
            con.execute("UPDATE lancamentos SET status_conciliacao='novo' WHERE id=?", (pergunta["lancamento_id"],))
        con.execute("UPDATE perguntas_pendentes SET respondida=1, resposta=? WHERE id=?", (texto_resposta, pergunta["id"]))
        con.commit()
        con.close()

    elif "EMPRESTIM" in texto_upper or "EMPRESTEI" in texto_upper:
        lanc = con.execute("SELECT * FROM lancamentos WHERE id=?", (pergunta["lancamento_id"],)).fetchone()
        con.execute("UPDATE lancamentos SET categoria='Empréstimo (saída)' WHERE id=?", (pergunta["lancamento_id"],))
        con.execute("UPDATE perguntas_pendentes SET respondida=1, resposta=? WHERE id=?", (texto_resposta, pergunta["id"]))
        con.commit()
        con.close()
        registrar_emprestimo(usuario_id, lanc["descricao_original"][:30], lanc["valor"], lanc["data"], lanc["id"])

    else:
        # a categoria só é aceita se for exatamente uma válida — padrão
        # OU personalizada desse usuário (o formulário manda um valor de
        # lista suspensa, não mais texto livre) — qualquer coisa fora
        # disso cai em Outros/Diversos em vez de criar bagunça
        personalizadas = [r["nome"] for r in con.execute(
            "SELECT nome FROM categorias_personalizadas WHERE usuario_id=?", (usuario_id,)
        )]
        todas_validas = CATEGORIAS_VALIDAS + personalizadas
        categoria = next((c for c in todas_validas if c.lower() == texto_resposta.strip().lower()), "Outros/Diversos")
        con.execute("UPDATE lancamentos SET categoria=? WHERE id=?", (categoria, pergunta["lancamento_id"]))
        con.execute("UPDATE perguntas_pendentes SET respondida=1, resposta=? WHERE id=?", (texto_resposta, pergunta["id"]))
        con.commit()

        lanc = con.execute("SELECT descricao_original FROM lancamentos WHERE id=?", (pergunta["lancamento_id"],)).fetchone()
        con.execute(
            """INSERT OR IGNORE INTO memoria_entidade (id, usuario_id, tipo_match, valor_match, categoria, descricao)
               VALUES (?,?,?,?,?,?)""",
            (novo_id(), usuario_id, "nome_texto", lanc["descricao_original"][:40].upper(), categoria, "aprendido pela web"),
        )
        con.commit()
        con.close()


# =========================================================
# Rotas da API — é isso que o painel web chama diretamente
# =========================================================
@app.route("/api/usuarios", methods=["POST"])
def rota_criar_usuario():
    """Tiago usa essa rota (ou um botão no painel) pra criar o acesso de
    cada participante do teste. Devolve o ID que vira o link pessoal
    de cada um: painel.com/?usuario=ID"""
    nome = (request.get_json(silent=True) or {}).get("nome", "").strip() or "Participante"
    uid = criar_usuario(nome)
    return jsonify({"usuario_id": uid, "nome": nome})


@app.route("/api/categorias/<usuario_id>")
def rota_listar_categorias(usuario_id):
    """Devolve a lista de categorias (padrão + as que esse usuário já
    criou), separadas por tipo — é o que alimenta a lista suspensa."""
    con = conectar()
    personalizadas = con.execute(
        "SELECT nome, tipo FROM categorias_personalizadas WHERE usuario_id=? ORDER BY nome", (usuario_id,)
    ).fetchall()
    con.close()
    despesa = CATEGORIAS_DESPESA[:-1] + [p["nome"] for p in personalizadas if p["tipo"] == "despesa"] + [CATEGORIAS_DESPESA[-1]]
    receita = CATEGORIAS_RECEITA[:-1] + [p["nome"] for p in personalizadas if p["tipo"] == "receita"] + [CATEGORIAS_RECEITA[-1]]
    return jsonify({"despesa": despesa, "receita": receita})


@app.route("/api/categorias/<usuario_id>", methods=["POST"])
def rota_criar_categoria(usuario_id):
    """Cria uma categoria nova pra esse usuário — fica guardada e
    reaproveitada dali pra frente, nunca mais texto solto criando
    bagunça (é a lição do bug do "roles")."""
    dados = request.get_json(silent=True) or {}
    nome = (dados.get("nome") or "").strip()
    tipo = dados.get("tipo") if dados.get("tipo") in ("despesa", "receita") else "despesa"
    if not nome:
        return jsonify({"erro": "Faltou o nome da categoria"}), 400

    listas_padrao = CATEGORIAS_DESPESA if tipo == "despesa" else CATEGORIAS_RECEITA
    if nome.lower() in [c.lower() for c in listas_padrao]:
        return jsonify({"erro": f'"{nome}" já existe nas categorias padrão'}), 400

    con = conectar()
    ja_existe = con.execute(
        "SELECT id FROM categorias_personalizadas WHERE usuario_id=? AND LOWER(nome)=LOWER(?) AND tipo=?",
        (usuario_id, nome, tipo),
    ).fetchone()
    if ja_existe:
        con.close()
        return jsonify({"erro": f'"{nome}" já existe'}), 400

    con.execute(
        "INSERT INTO categorias_personalizadas (id, usuario_id, nome, tipo) VALUES (?,?,?,?)",
        (novo_id(), usuario_id, nome, tipo),
    )
    con.commit()
    con.close()
    return jsonify({"ok": True, "nome": nome, "tipo": tipo})


@app.route("/api/subcategorias/<usuario_id>")
def rota_listar_subcategorias(usuario_id):
    """Devolve os subgrupos disponíveis pra uma categoria (sugeridos +
    os que esse usuário já criou) — alimenta o select de subgrupo."""
    categoria = request.args.get("categoria")
    if not categoria:
        return jsonify({"erro": "Faltou o parâmetro categoria"}), 400
    con = conectar()
    personalizados = [r["nome"] for r in con.execute(
        "SELECT nome FROM subcategorias_personalizadas WHERE usuario_id=? AND categoria=? ORDER BY nome",
        (usuario_id, categoria),
    )]
    con.close()
    padrao = SUBCATEGORIAS_PADRAO.get(categoria, [])
    todos = padrao + [p for p in personalizados if p not in padrao]
    return jsonify(todos)


@app.route("/api/subcategorias/<usuario_id>", methods=["POST"])
def rota_criar_subcategoria(usuario_id):
    """Cria um subgrupo novo pra uma categoria — mesma lógica da
    categoria personalizada: fica salvo e some do texto livre."""
    dados = request.get_json(silent=True) or {}
    categoria = (dados.get("categoria") or "").strip()
    nome = (dados.get("nome") or "").strip()
    if not categoria or not nome:
        return jsonify({"erro": "Faltou categoria ou nome do subgrupo"}), 400

    padrao = SUBCATEGORIAS_PADRAO.get(categoria, [])
    if nome.lower() in [s.lower() for s in padrao]:
        return jsonify({"erro": f'"{nome}" já existe nos subgrupos sugeridos'}), 400

    con = conectar()
    ja_existe = con.execute(
        "SELECT id FROM subcategorias_personalizadas WHERE usuario_id=? AND categoria=? AND LOWER(nome)=LOWER(?)",
        (usuario_id, categoria, nome),
    ).fetchone()
    if ja_existe:
        con.close()
        return jsonify({"erro": f'"{nome}" já existe'}), 400

    con.execute(
        "INSERT INTO subcategorias_personalizadas (id, usuario_id, categoria, nome) VALUES (?,?,?,?)",
        (novo_id(), usuario_id, categoria, nome),
    )
    con.commit()
    con.close()
    return jsonify({"ok": True, "categoria": categoria, "nome": nome})


@app.route("/api/upload/<usuario_id>", methods=["POST"])
def rota_upload(usuario_id):
    if "arquivo" not in request.files:
        return jsonify({"erro": "Nenhum arquivo enviado"}), 400
    arquivo = request.files["arquivo"]
    ext = "." + arquivo.filename.rsplit(".", 1)[-1].lower()
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        arquivo.save(f.name)
        caminho = f.name

    import hashlib
    with open(caminho, "rb") as f:
        hash_arquivo = hashlib.sha256(f.read()).hexdigest()
    con = conectar()
    ja_processado = con.execute(
        "SELECT nome_arquivo FROM arquivos_processados WHERE usuario_id=? AND hash_arquivo=?",
        (usuario_id, hash_arquivo),
    ).fetchone()
    if ja_processado:
        con.close()
        return jsonify({"erro": f"Esse arquivo já foi processado antes ({ja_processado['nome_arquivo']}) — nada foi duplicado."})

    try:
        resultado = processar_extrato_recebido(usuario_id, caminho)
    except Exception:
        app.logger.exception("Falha ao processar extrato %s", arquivo.filename)
        con.close()
        return jsonify({"erro": "Não consegui ler esse arquivo — o problema foi do nosso lado, não da sua internet. "
                                "Já fica registrado aqui pra corrigir; enquanto isso, se o banco oferecer OFX, tenta por ele."}), 500
    if resultado.get("erro"):
        # não marca o arquivo como processado: depois de corrigido, a
        # pessoa pode mandar o mesmo arquivo de novo sem cair no "já foi processado"
        con.close()
        return jsonify(resultado)
    con.execute(
        "INSERT INTO arquivos_processados (id, usuario_id, hash_arquivo, nome_arquivo, processado_em) VALUES (?,?,?,?,?)",
        (novo_id(), usuario_id, hash_arquivo, arquivo.filename, __import__("datetime").datetime.utcnow().isoformat()),
    )
    con.commit()
    con.close()
    return jsonify(resultado)


@app.route("/api/registrar-manual/<usuario_id>", methods=["POST"])
def rota_registrar_manual(usuario_id):
    """Registra um gasto em dinheiro — por texto digitado ou foto de um
    comprovante/nota fiscal. Não vem de extrato bancário nenhum, então
    fica marcado com origem própria pra nunca se confundir com um
    movimento de conta."""
    import datetime

    if "foto" in request.files:
        arquivo = request.files["foto"]
        ext = "." + arquivo.filename.rsplit(".", 1)[-1].lower()
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            arquivo.save(f.name)
            caminho = f.name
        try:
            from extrator_visao import extrair_texto_por_visao
            texto_ocr = extrair_texto_por_visao(caminho)
        except RuntimeError as e:
            return jsonify({"erro": str(e)}), 400
        extraido = extrair_valor_e_descricao(texto_ocr)
    else:
        dados = request.get_json(silent=True) or {}
        texto = (dados.get("texto") or "").strip()
        if not texto:
            return jsonify({"erro": "Manda um texto ou uma foto do comprovante"}), 400
        extraido = extrair_valor_e_descricao(texto)

    if extraido is None:
        return jsonify({"erro": "Não consegui achar um valor em dinheiro nessa descrição — tenta incluir o número (ex: \"30 reais\")."}), 400

    t = {
        "data": datetime.date.today().isoformat(),
        "descricao": extraido["descricao"],
        "valor": extraido["valor"],
        "tipo": "D",
    }
    cat = categorizar(usuario_id, t["descricao"], t["valor"], t["data"], "dinheiro")
    origem = "manual_foto" if "foto" in request.files else "manual_texto"

    if cat is None:
        lid = salvar_lancamento(usuario_id, t, origem, "dinheiro")
        criar_pergunta(usuario_id, lid, "categoria_desconhecida",
                        f"Não reconheci: \"{t['descricao']}\" de R${t['valor']:.2f} (registrado em dinheiro). Qual categoria?")
        return jsonify({"valor": t["valor"], "descricao": t["descricao"], "categorizado": False})

    salvar_lancamento(usuario_id, t, origem, "dinheiro", categoria=cat.categoria, confianca=cat.confianca, subcategoria=cat.subcategoria)
    return jsonify({"valor": t["valor"], "descricao": t["descricao"], "categoria": cat.categoria, "categorizado": True})


@app.route("/api/responder/<usuario_id>", methods=["POST"])
def rota_responder(usuario_id):
    dados = request.get_json(silent=True) or {}
    pergunta_id = dados.get("pergunta_id")
    resposta_texto = (dados.get("resposta") or "").strip()
    if not pergunta_id or not resposta_texto:
        return jsonify({"erro": "Faltou pergunta_id ou resposta"}), 400

    con = conectar()
    pergunta = con.execute("SELECT * FROM perguntas_pendentes WHERE id=? AND usuario_id=?", (pergunta_id, usuario_id)).fetchone()
    con.close()
    if not pergunta:
        return jsonify({"erro": "Pergunta não encontrada"}), 404

    processar_resposta(usuario_id, pergunta, resposta_texto)
    con = conectar()
    n = con.execute("SELECT COUNT(*) as n FROM perguntas_pendentes WHERE usuario_id=? AND respondida=0", (usuario_id,)).fetchone()["n"]
    con.close()
    return jsonify({"ok": True, "pendencias_restantes": n})


# =========================================================
# Rotas do painel (resumo financeiro) — igual antes
# =========================================================
CATEGORIAS_IGNORADAS_NO_RESUMO = ("AUTOTRANSFERENCIA", "EXCLUIDO_TERCEIRO", "EXCLUIDO_EMPRESTIMO",
                                    "Empréstimo (saída)", "Empréstimo devolvido", "Fatura decomposta (ver itens)")


def _historico_mensal(con, usuario_id):
    """Entrou e saiu de cada mês, numa consulta só — alimenta o gráfico de
    barras do "Desde o início". Mesmas regras do _resumo_periodo (ignora
    autotransferência/empréstimo/fatura decomposta; guardar dinheiro em
    'Investimentos e Reserva' não conta como gasto), pra bater com os
    totais que aparecem mês a mês."""
    ignoradas = CATEGORIAS_IGNORADAS_NO_RESUMO
    linhas = con.execute(
        "SELECT SUBSTR(data,1,7) as mes, "
        "SUM(CASE WHEN tipo='C' THEN valor ELSE 0 END) as receita, "
        "SUM(CASE WHEN tipo='D' AND (categoria IS NULL OR categoria != 'Investimentos e Reserva') THEN valor ELSE 0 END) as despesa "
        "FROM lancamentos WHERE usuario_id=? AND (categoria IS NULL OR categoria NOT IN (%s)) "
        "GROUP BY SUBSTR(data,1,7) ORDER BY SUBSTR(data,1,7)" % ",".join("?" * len(ignoradas)),
        (usuario_id, *ignoradas),
    ).fetchall()
    return [
        {"mes": r["mes"], "receita": round(r["receita"] or 0, 2), "despesa": round(r["despesa"] or 0, 2),
         "saldo": round((r["receita"] or 0) - (r["despesa"] or 0), 2)}
        for r in linhas
    ]


def _resumo_periodo(con, usuario_id, filtro_mes=None):
    filtro_sql = " AND SUBSTR(data,1,7) = ?" if filtro_mes else ""
    params_extra = (filtro_mes,) if filtro_mes else ()

    receita = con.execute(
        "SELECT COALESCE(SUM(valor),0) as t FROM lancamentos "
        "WHERE usuario_id=? AND tipo='C' AND (categoria IS NULL OR categoria NOT IN (%s))%s"
        % (",".join("?" * len(CATEGORIAS_IGNORADAS_NO_RESUMO)), filtro_sql),
        (usuario_id, *CATEGORIAS_IGNORADAS_NO_RESUMO, *params_extra),
    ).fetchone()["t"]

    despesa = con.execute(
        "SELECT COALESCE(SUM(valor),0) as t FROM lancamentos "
        "WHERE usuario_id=? AND tipo='D' AND (categoria IS NULL OR categoria NOT IN (%s)) "
        "AND (categoria IS NULL OR categoria != 'Investimentos e Reserva')%s"
        % (",".join("?" * len(CATEGORIAS_IGNORADAS_NO_RESUMO)), filtro_sql),
        (usuario_id, *CATEGORIAS_IGNORADAS_NO_RESUMO, *params_extra),
    ).fetchone()["t"]

    categorias = [
        dict(r) for r in con.execute(
            "SELECT categoria as nome, SUM(valor) as valor FROM lancamentos "
            "WHERE usuario_id=? AND tipo='D' AND categoria IS NOT NULL "
            "AND categoria NOT IN (%s) AND categoria != 'Investimentos e Reserva'%s "
            "GROUP BY categoria ORDER BY valor DESC"
            % (",".join("?" * len(CATEGORIAS_IGNORADAS_NO_RESUMO)), filtro_sql),
            (usuario_id, *CATEGORIAS_IGNORADAS_NO_RESUMO, *params_extra),
        )
    ]

    categorias_receita = [
        dict(r) for r in con.execute(
            "SELECT categoria as nome, SUM(valor) as valor FROM lancamentos "
            "WHERE usuario_id=? AND tipo='C' AND categoria IS NOT NULL "
            "AND categoria NOT IN (%s)%s "
            "GROUP BY categoria ORDER BY valor DESC"
            % (",".join("?" * len(CATEGORIAS_IGNORADAS_NO_RESUMO)), filtro_sql),
            (usuario_id, *CATEGORIAS_IGNORADAS_NO_RESUMO, *params_extra),
        )
    ]

    return {
        "receita_total": round(receita, 2),
        "despesa_total": round(despesa, 2),
        "saldo": round(receita - despesa, 2),
        "categorias": categorias,
        "categorias_receita": categorias_receita,
    }


@app.route("/api/resumo/<usuario_id>")
def resumo(usuario_id):
    con = conectar()
    mes_pedido = request.args.get("mes")  # ?mes=2026-07, opcional — senão usa o mais recente
    row_ultimo_mes = con.execute(
        "SELECT SUBSTR(MAX(data),1,7) as m FROM lancamentos WHERE usuario_id=?", (usuario_id,)
    ).fetchone()
    mes_exibido = mes_pedido or (row_ultimo_mes["m"] if row_ultimo_mes else None)

    row_usuario = con.execute("SELECT nome FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if row_usuario:
        # o painel chama essa rota toda vez que abre — serve de "último acesso"
        # pra página de acompanhamento do teste
        con.execute("UPDATE usuarios SET ultimo_acesso=? WHERE id=?",
                    (datetime.utcnow().isoformat(timespec="seconds"), usuario_id))
        con.commit()

    resposta = {
        "usuario_nome": row_usuario["nome"] if row_usuario else None,
        "mes_atual": {"mes": mes_exibido, **_resumo_periodo(con, usuario_id, mes_exibido)} if mes_exibido else None,
        "periodo_completo": _resumo_periodo(con, usuario_id),
        "historico_mensal": _historico_mensal(con, usuario_id),
        "pendencias": [
            {"id": r["id"], "texto": r["texto_pergunta"], "tipo": r["tipo_pergunta"], "tipo_lancamento": r["tipo_lanc"]}
            for r in con.execute(
                "SELECT p.id, p.texto_pergunta, p.tipo_pergunta, l.tipo as tipo_lanc FROM perguntas_pendentes p "
                "JOIN lancamentos l ON l.id = p.lancamento_id "
                "WHERE p.usuario_id=? AND p.respondida=0", (usuario_id,)
            )
        ],
    }
    con.close()
    return jsonify(resposta)


# =========================================================
# Painel visual (layout novo): média dos últimos 3 meses por categoria,
# gasto acumulado dia a dia e os pontos de atenção do mês. A comparação
# é sempre com o histórico do próprio usuário — não depende de orçamento
# cadastrado (a aba Orçamentos ainda é "em breve").
# =========================================================
def _mes_anterior(mes: str, n: int) -> str:
    ano, m = int(mes[:4]), int(mes[5:7]) - n
    while m <= 0:
        m += 12
        ano -= 1
    return f"{ano:04d}-{m:02d}"


def _brl(v: float) -> str:
    """R$ sem centavos, no formato brasileiro (R$ 1.234)."""
    return "R$ " + f"{v:,.0f}".replace(",", ".")


def _pontos_de_atencao(cats, despesa, receita, projecao, media_receita, normal_ate_hoje, mes_corrente, n_hist):
    alertas = []

    # 1) categoria que passou do normal (média dos meses anteriores)
    excessos = []
    for c in cats:
        media = c["media"]
        if not media or media < 50:
            continue  # média pequena demais vira alarme à toa (R$ 10 -> R$ 20 = +100%)
        excesso = c["valor"] - media
        if c["valor"] > media * 1.15 and excesso >= 30:
            texto = f"{_brl(c['valor'])} no mês. Seu normal é {_brl(media)}."
            if c["sub_principal"]:
                texto += f" A maior parte em {c['sub_principal']}."
            excessos.append((excesso, {
                "nivel": "alto" if c["valor"] > media * 1.3 else "medio",
                "rotulo": "Passou do normal",
                "titulo": f"{c['nome']} +{round(c['dif'] * 100)}%",
                "texto": texto,
                "categoria": c["nome"],
            }))
    excessos.sort(key=lambda x: -x[0])
    alertas += [a for _, a in excessos]

    # 2) ritmo do mês — só no mês corrente, quando dá pra projetar o fim do mês
    if projecao:
        base_receita = receita if receita > 0 else (media_receita or 0)
        if base_receita > 0 and projecao > base_receita:
            alertas.append({"nivel": "alto", "rotulo": "Ritmo do mês",
                            "titulo": f"No ritmo atual, faltam {_brl(projecao - base_receita)}",
                            "texto": f"Se continuar assim, o mês fecha com uns {_brl(projecao)} de gastos, mais do que entrou.",
                            "categoria": None})
        elif base_receita > 0 and base_receita - projecao < base_receita * 0.1:
            alertas.append({"nivel": "medio", "rotulo": "Ritmo do mês",
                            "titulo": f"No ritmo atual, sobra só {_brl(base_receita - projecao)}",
                            "texto": f"Se continuar assim, o mês fecha com uns {_brl(projecao)} de gastos.",
                            "categoria": None})
        elif normal_ate_hoje and despesa > normal_ate_hoje * 1.1 and despesa - normal_ate_hoje >= 50:
            alertas.append({"nivel": "medio", "rotulo": "Ritmo do mês",
                            "titulo": f"Gastando {round((despesa / normal_ate_hoje - 1) * 100)}% mais rápido",
                            "texto": f"Até hoje você costuma gastar {_brl(normal_ate_hoje)} e já foram {_brl(despesa)}. Nesse ritmo, fecha em {_brl(projecao)}.",
                            "categoria": None})

    # 3) elogio — só com o mês fechado (no meio do mês tudo parece "abaixo")
    if not mes_corrente and n_hist:
        bons = [c for c in cats if c["media"] and c["media"] >= 100 and c["valor"] < c["media"] * 0.9]
        if bons:
            c = max(bons, key=lambda c: c["media"] - c["valor"])
            alertas.append({"nivel": "bom", "rotulo": "Mandou bem",
                            "titulo": f"{c['nome']} {round(c['dif'] * 100)}%",
                            "texto": f"{_brl(c['media'] - c['valor'])} abaixo do seu normal. Continue assim.",
                            "categoria": c["nome"]})

    ordem = {"alto": 0, "medio": 1, "bom": 2}
    alertas.sort(key=lambda a: ordem[a["nivel"]])
    return alertas[:4]


def painel_do_mes(con, usuario_id: str, mes: str, hoje: date) -> dict:
    anteriores = [_mes_anterior(mes, i) for i in (1, 2, 3)]
    todos = [mes] + anteriores
    ignoradas = CATEGORIAS_IGNORADAS_NO_RESUMO
    linhas = con.execute(
        "SELECT data, valor, tipo, categoria, subcategoria FROM lancamentos "
        "WHERE usuario_id=? AND SUBSTR(data,1,7) IN (%s) "
        "AND (categoria IS NULL OR categoria NOT IN (%s))"
        % (",".join("?" * len(todos)), ",".join("?" * len(ignoradas))),
        (usuario_id, *todos, *ignoradas),
    ).fetchall()

    dias = calendar.monthrange(int(mes[:4]), int(mes[5:7]))[1]
    por_mes = {m: {"despesa": 0.0, "receita": 0.0, "cats": {}, "diario": [0.0] * 31} for m in todos}
    subs = {}
    for r in linhas:
        m, valor, cat = r["data"][:7], r["valor"], r["categoria"]
        b = por_mes[m]
        if r["tipo"] == "C":
            b["receita"] += valor
            continue
        if cat == "Investimentos e Reserva":
            continue  # mesma regra do resumo: guardar dinheiro não é consumo
        b["despesa"] += valor
        b["diario"][min(max(int(r["data"][8:10]), 1), 31) - 1] += valor
        if cat:
            b["cats"][cat] = b["cats"].get(cat, 0.0) + valor
            if m == mes and r["subcategoria"]:
                subs.setdefault(cat, {})
                subs[cat][r["subcategoria"]] = subs[cat].get(r["subcategoria"], 0.0) + valor

    # só conta na média o mês que tem algum lançamento (usuário novo, com
    # 1 mês de histórico, compara com esse 1 mês — e sem nenhum, não compara)
    hist = [m for m in anteriores if por_mes[m]["despesa"] > 0 or por_mes[m]["receita"] > 0]
    n = len(hist)
    media_despesa = round(sum(por_mes[m]["despesa"] for m in hist) / n, 2) if n else None
    media_receita = round(sum(por_mes[m]["receita"] for m in hist) / n, 2) if n else None

    atual = por_mes[mes]
    cats = []
    for nome, valor in sorted(atual["cats"].items(), key=lambda x: -x[1]):
        media = round(sum(por_mes[m]["cats"].get(nome, 0.0) for m in hist) / n, 2) if n else None
        sub_principal = max(subs[nome].items(), key=lambda x: x[1])[0] if nome in subs else None
        cats.append({
            "nome": nome, "valor": round(valor, 2), "media": media,
            "dif": round((valor - media) / media, 4) if media else None,
            "sub_principal": sub_principal,
        })

    def acumular(valores):
        saida, soma = [], 0.0
        for v in valores:
            soma += v
            saida.append(round(soma, 2))
        return saida

    mes_corrente = mes == hoje.strftime("%Y-%m")
    dia_ref = min(hoje.day, dias) if mes_corrente else dias
    diario = atual["diario"][:dias - 1] + [sum(atual["diario"][dias - 1:])]  # dia 31 de mês curto cai no último dia
    acumulado = acumular(diario[:dia_ref])
    despesa, receita = round(atual["despesa"], 2), round(atual["receita"], 2)

    # "Seu normal" dia a dia: média do gasto acumulado dos meses anteriores
    # em cada dia. Respeita o formato do mês (aluguel no dia 1, fatura no dia
    # 10...), em vez de supor que o gasto se espalha igual pelos 30 dias.
    normal_acumulado = None
    if n:
        curvas = [acumular(por_mes[m]["diario"][:dias - 1] + [sum(por_mes[m]["diario"][dias - 1:])]) for m in hist]
        normal_acumulado = [round(sum(c[d] for c in curvas) / n, 2) for d in range(dias)]

    # Projeção do fim do mês: o que já foi gasto + o que normalmente ainda
    # sai até o fim do mês. Sem histórico, regra de três simples — só a
    # partir do dia 10, antes disso o aluguel/contas do começo distorcem tudo.
    projecao = None
    if mes_corrente and dia_ref < dias:
        if normal_acumulado:
            projecao = round(despesa + max(normal_acumulado[-1] - normal_acumulado[dia_ref - 1], 0), 2)
        elif dia_ref >= 10:
            projecao = round(despesa / dia_ref * dias, 2)

    return {
        "mes": mes,
        "mes_corrente": mes_corrente,
        "dia_referencia": dia_ref,
        "dias_no_mes": dias,
        "receita": receita,
        "despesa": despesa,
        "acumulado": acumulado,
        "normal_acumulado": normal_acumulado,
        "projecao": projecao,
        "meses_historico": n,
        "media_despesa_3m": media_despesa,
        "media_receita_3m": media_receita,
        "categorias": cats,
        "alertas": _pontos_de_atencao(cats, despesa, receita, projecao, media_receita,
                                      normal_acumulado[dia_ref - 1] if normal_acumulado else None, mes_corrente, n),
    }


@app.route("/api/painel/<usuario_id>")
def rota_painel(usuario_id):
    """Dados dos gráficos e dos pontos de atenção do Início (layout novo).
    ?mes=AAAA-MM opcional — senão usa o mês mais recente com lançamento."""
    con = conectar()
    mes = request.args.get("mes")
    if not mes:
        row = con.execute("SELECT SUBSTR(MAX(data),1,7) as m FROM lancamentos WHERE usuario_id=?", (usuario_id,)).fetchone()
        mes = row["m"] if row else None
    if not mes or len(mes) != 7:
        con.close()
        return jsonify(None)
    try:
        resposta = painel_do_mes(con, usuario_id, mes, date.today())
    except ValueError:
        return jsonify({"erro": "mês inválido — use AAAA-MM"}), 400
    finally:
        con.close()
    return jsonify(resposta)


@app.route("/api/contas-fixas/<usuario_id>")
def rota_listar_contas_fixas(usuario_id):
    """Lista as contas fixas cadastradas (ativas e inativas) — alimenta a
    tela de gerenciamento, na aba Lançamentos."""
    return jsonify(cf.listar_contas_fixas(usuario_id))


@app.route("/api/contas-fixas/<usuario_id>", methods=["POST"])
def rota_criar_conta_fixa(usuario_id):
    """Cadastra uma conta fixa nova (ex: Água, Moradia, todo dia 10,
    ~R$120) — categoria e subgrupo já vêm elencados daqui pra frente.
    Se eh_fatura_cartao vier marcado, é a fatura do cartão de crédito:
    categoria/subgrupo não fazem sentido aqui (o detalhamento de verdade
    vem de cada compra da fatura), então usa a categoria provisória
    reservada pra isso, ignorando o que vier do formulário."""
    dados = request.get_json(silent=True) or {}
    nome = (dados.get("nome") or "").strip()
    eh_fatura_cartao = bool(dados.get("eh_fatura_cartao"))
    categoria = CATEGORIA_FATURA_PROVISORIA if eh_fatura_cartao else (dados.get("categoria") or "").strip()
    subcategoria = None if eh_fatura_cartao else ((dados.get("subcategoria") or "").strip() or None)
    try:
        dia_vencimento = int(dados.get("dia_vencimento"))
    except (TypeError, ValueError):
        return jsonify({"erro": "Dia de vencimento inválido"}), 400
    if not nome or not categoria:
        return jsonify({"erro": "Faltou nome ou categoria"}), 400
    if not (1 <= dia_vencimento <= 31):
        return jsonify({"erro": "Dia de vencimento precisa ser entre 1 e 31"}), 400
    valor_bruto = dados.get("valor_esperado")
    try:
        valor_esperado = float(valor_bruto) if valor_bruto not in (None, "") else None
    except (TypeError, ValueError):
        valor_esperado = None

    cid = cf.criar_conta_fixa(usuario_id, nome, categoria, subcategoria, dia_vencimento, valor_esperado, eh_fatura_cartao)
    return jsonify({"ok": True, "id": cid})


@app.route("/api/contas-fixas/<usuario_id>/<conta_fixa_id>/ativa", methods=["POST"])
def rota_definir_ativa_conta_fixa(usuario_id, conta_fixa_id):
    """Pausa/reativa uma conta fixa (ex: cancelou a assinatura) sem perder
    o histórico de baixas já feitas."""
    dados = request.get_json(silent=True) or {}
    cf.definir_ativa(usuario_id, conta_fixa_id, bool(dados.get("ativo", True)))
    return jsonify({"ok": True})


@app.route("/api/contas-fixas/<usuario_id>/<conta_fixa_id>", methods=["PUT"])
def rota_editar_conta_fixa(usuario_id, conta_fixa_id):
    """Edita uma conta fixa já cadastrada — pra corrigir nome, categoria,
    dia ou valor quando o usuário cadastrou errado. Mesma validação do
    cadastro novo."""
    dados = request.get_json(silent=True) or {}
    nome = (dados.get("nome") or "").strip()
    eh_fatura_cartao = bool(dados.get("eh_fatura_cartao"))
    categoria = CATEGORIA_FATURA_PROVISORIA if eh_fatura_cartao else (dados.get("categoria") or "").strip()
    subcategoria = None if eh_fatura_cartao else ((dados.get("subcategoria") or "").strip() or None)
    try:
        dia_vencimento = int(dados.get("dia_vencimento"))
    except (TypeError, ValueError):
        return jsonify({"erro": "Dia de vencimento inválido"}), 400
    if not nome or not categoria:
        return jsonify({"erro": "Faltou nome ou categoria"}), 400
    if not (1 <= dia_vencimento <= 31):
        return jsonify({"erro": "Dia de vencimento precisa ser entre 1 e 31"}), 400
    valor_bruto = dados.get("valor_esperado")
    try:
        valor_esperado = float(valor_bruto) if valor_bruto not in (None, "") else None
    except (TypeError, ValueError):
        valor_esperado = None

    ok = cf.atualizar_conta_fixa(usuario_id, conta_fixa_id, nome, categoria, subcategoria, dia_vencimento, valor_esperado, eh_fatura_cartao)
    if not ok:
        return jsonify({"erro": "Conta fixa não encontrada"}), 404
    return jsonify({"ok": True})


@app.route("/api/contas-fixas/<usuario_id>/<conta_fixa_id>", methods=["DELETE"])
def rota_excluir_conta_fixa(usuario_id, conta_fixa_id):
    """Apaga a conta fixa (e as baixas associadas) — pra quando o usuário
    cadastrou errado. Não afeta nenhum lançamento real, só o cadastro e
    o rastreamento de vencimento."""
    ok = cf.excluir_conta_fixa(usuario_id, conta_fixa_id)
    if not ok:
        return jsonify({"erro": "Conta fixa não encontrada"}), 404
    return jsonify({"ok": True})


@app.route("/api/contas-a-vencer/<usuario_id>")
def rota_contas_a_vencer(usuario_id):
    """Contas pendentes vencendo nos próximos N dias (inclui atrasadas) —
    alimenta o widget 'Contas a vencer' do dashboard."""
    dias = request.args.get("dias", default=15, type=int)
    return jsonify(cf.contas_a_vencer(usuario_id, dias))


@app.route("/api/contas-fixas/<usuario_id>/baixar", methods=["POST"])
def rota_baixar_conta_fixa(usuario_id):
    """Dá baixa manual numa conta — fallback de quando a baixa automática
    não achou o lançamento sozinha (valor mudou demais, etc)."""
    dados = request.get_json(silent=True) or {}
    baixa_id = dados.get("baixa_id")
    if not baixa_id:
        return jsonify({"erro": "Faltou baixa_id"}), 400
    ok = cf.marcar_baixa_manual(usuario_id, baixa_id, dados.get("lancamento_id"))
    if not ok:
        return jsonify({"erro": "Baixa não encontrada"}), 404
    return jsonify({"ok": True})


# =========================================================
# Saldo disponível por conta/banco — bem diferente do "entrou - saiu" do
# período: é quanto tem de fato disponível em cada conta agora. Entra
# sozinho quando um extrato OFX traz o saldo real (LEDGERBAL), ou é
# digitado/corrigido na mão pelo usuário.
# =========================================================
@app.route("/api/saldo-disponivel/<usuario_id>")
def rota_listar_saldo_disponivel(usuario_id):
    contas = sc.listar_saldos(usuario_id)
    total = round(sum(c["saldo"] for c in contas), 2)
    return jsonify({"contas": contas, "total": total})


@app.route("/api/saldo-disponivel/<usuario_id>", methods=["POST"])
def rota_definir_saldo_disponivel(usuario_id):
    """Cadastra ou atualiza o saldo de uma conta na mão. 'banco' é opcional
    — se vier, edita a conta existente com essa chave; se não vier, cria
    uma nova a partir do nome digitado."""
    dados = request.get_json(silent=True) or {}
    nome = (dados.get("nome") or "").strip()
    try:
        valor = float(dados.get("valor"))
    except (TypeError, ValueError):
        return jsonify({"erro": "Valor inválido"}), 400
    if not nome:
        return jsonify({"erro": "Faltou o nome da conta"}), 400
    banco = (dados.get("banco") or "").strip() or sc.normalizar_banco(nome)
    sc.definir_saldo(usuario_id, banco, nome, valor, origem="manual")
    return jsonify({"ok": True, "banco": banco})


@app.route("/api/saldo-disponivel/<usuario_id>/<banco>", methods=["DELETE"])
def rota_excluir_saldo_disponivel(usuario_id, banco):
    ok = sc.excluir_saldo(usuario_id, banco)
    if not ok:
        return jsonify({"erro": "Conta não encontrada"}), 404
    return jsonify({"ok": True})


@app.route("/api/meses/<usuario_id>")
def meses_disponiveis(usuario_id):
    """Lista os meses (AAAA-MM) que já têm lançamento, do mais recente pro
    mais antigo — é o que alimenta o seletor de mês no painel."""
    con = conectar()
    linhas = con.execute(
        "SELECT DISTINCT SUBSTR(data,1,7) as mes FROM lancamentos WHERE usuario_id=? ORDER BY mes DESC",
        (usuario_id,),
    ).fetchall()
    con.close()
    return jsonify([r["mes"] for r in linhas])


@app.route("/api/detalhe/<usuario_id>")
def detalhe_categoria(usuario_id):
    """Lista os lançamentos individuais que formam uma categoria (com
    filtro de mês opcional) — é o "abrir a caixa" quando o total de uma
    categoria parece grande ou estranho demais pra ser só um tipo de gasto."""
    categoria = request.args.get("categoria")
    mes = request.args.get("mes")
    tipo_param = (request.args.get("tipo") or "").upper()  # "C" ou "D", vem do gráfico que foi clicado
    if not categoria:
        return jsonify({"erro": "Faltou o parâmetro categoria"}), 400

    con = conectar()
    filtro_mes = " AND SUBSTR(data,1,7) = ?" if mes else ""
    # algumas categorias (ex: "Outros/Diversos") existem tanto na lista de despesa
    # quanto na de receita, então não dá pra adivinhar o tipo só pelo nome — o
    # gráfico que foi clicado já sabe se é despesa ou receita e manda explícito.
    # tipo_param cobre isso; a inferência por nome fica só de fallback pra chamadas antigas.
    if tipo_param in ("C", "D"):
        tipo_lanc = tipo_param
    else:
        tipo_lanc = "C" if categoria in CATEGORIAS_RECEITA and categoria not in CATEGORIAS_DESPESA else "D"
    params = (usuario_id, categoria, *( (mes,) if mes else () ))
    linhas = con.execute(
        f"SELECT id, data, descricao_original, valor, subcategoria FROM lancamentos "
        f"WHERE usuario_id=? AND categoria=? AND tipo='{tipo_lanc}'{filtro_mes} ORDER BY valor DESC",
        params,
    ).fetchall()
    con.close()
    return jsonify([
        {"id": r["id"], "data": r["data"], "descricao": r["descricao_original"], "valor": r["valor"], "subcategoria": r["subcategoria"]}
        for r in linhas
    ])


@app.route("/api/recategorizar/<usuario_id>", methods=["POST"])
def rota_recategorizar(usuario_id):
    """Corrige a categoria de um lançamento específico direto pela tela
    de detalhe. Além de corrigir esse, aprende pra próxima vez (memória
    de entidade) e já corrige de uma vez qualquer outro lançamento que
    tenha caído no mesmo erro (mesma pessoa/empresa)."""
    dados = request.get_json(silent=True) or {}
    lancamento_id = dados.get("lancamento_id")
    nova_categoria = (dados.get("categoria") or "").strip()
    # subgrupo é opcional — string vazia/ausente vira "sem subgrupo" (NULL),
    # não impede a correção de categoria de funcionar sozinha
    nova_subcategoria = (dados.get("subcategoria") or "").strip() or None
    if not lancamento_id or not nova_categoria:
        return jsonify({"erro": "Faltou lancamento_id ou categoria"}), 400

    con = conectar()
    lanc = con.execute(
        "SELECT descricao_original FROM lancamentos WHERE id=? AND usuario_id=?", (lancamento_id, usuario_id)
    ).fetchone()
    if not lanc:
        con.close()
        return jsonify({"erro": "Lançamento não encontrado"}), 404

    valor_match = lanc["descricao_original"][:40].upper()

    # corrige esse lançamento e qualquer outro com a mesma "assinatura" de texto
    afetados = con.execute(
        "SELECT id FROM lancamentos WHERE usuario_id=? AND UPPER(descricao_original) LIKE ?",
        (usuario_id, f"%{valor_match}%"),
    ).fetchall()
    for a in afetados:
        con.execute("UPDATE lancamentos SET categoria=?, subcategoria=? WHERE id=?", (nova_categoria, nova_subcategoria, a["id"]))

    # aprende pra próxima vez — corrige se já existia mapeamento errado, cria se não existia
    existente = con.execute(
        "SELECT id FROM memoria_entidade WHERE usuario_id=? AND tipo_match='nome_texto' AND valor_match=?",
        (usuario_id, valor_match),
    ).fetchone()
    if existente:
        con.execute("UPDATE memoria_entidade SET categoria=?, subcategoria=? WHERE id=?", (nova_categoria, nova_subcategoria, existente["id"]))
    else:
        con.execute(
            """INSERT INTO memoria_entidade (id, usuario_id, tipo_match, valor_match, categoria, subcategoria, descricao)
               VALUES (?,?,?,?,?,?,?)""",
            (novo_id(), usuario_id, "nome_texto", valor_match, nova_categoria, nova_subcategoria, "corrigido pelo usuário"),
        )

    con.commit()
    con.close()
    return jsonify({"ok": True, "lancamentos_corrigidos": len(afetados)})


@app.route("/api/excluir-lancamento/<usuario_id>", methods=["POST"])
def rota_excluir_lancamento(usuario_id):
    """Exclui um lançamento manualmente pela tela de detalhe — usado
    para tirar itens lançados errado (ex: parcela de fatura que caiu no
    mês/ano errado antes da correção do parser) sem precisar de script."""
    dados = request.get_json(silent=True) or {}
    lancamento_id = dados.get("lancamento_id")
    if not lancamento_id:
        return jsonify({"erro": "Faltou lancamento_id"}), 400

    con = conectar()
    lanc = con.execute(
        "SELECT id FROM lancamentos WHERE id=? AND usuario_id=?", (lancamento_id, usuario_id)
    ).fetchone()
    if not lanc:
        con.close()
        return jsonify({"erro": "Lançamento não encontrado"}), 404

    con.execute("DELETE FROM perguntas_pendentes WHERE lancamento_id=? AND usuario_id=?", (lancamento_id, usuario_id))
    con.execute("DELETE FROM lancamentos WHERE id=? AND usuario_id=?", (lancamento_id, usuario_id))
    con.commit()
    con.close()
    return jsonify({"ok": True})


# =========================================================
# Acompanhamento do teste (só pro Tiago): quem já abriu o link e quem já
# enviou extrato. Protegida por uma chave definida na variável de ambiente
# ADMIN_CHAVE do Railway — sem ela configurada, a página fica desligada.
# Mostra só contagens e datas, nunca valores ou lançamentos de ninguém.
# =========================================================
def _hora_brasil(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso[:19]) - timedelta(hours=3)  # gravado em UTC
    except ValueError:
        return escape(iso)
    return dt.strftime("%d/%m %H:%M")


def dados_acompanhamento(con) -> list[dict]:
    usuarios = con.execute("SELECT id, nome, criado_em, ultimo_acesso FROM usuarios ORDER BY criado_em, nome").fetchall()
    saida = []
    for u in usuarios:
        arq = con.execute(
            "SELECT COUNT(*) as n, MIN(processado_em) as primeiro, MAX(processado_em) as ultimo "
            "FROM arquivos_processados WHERE usuario_id=?", (u["id"],)
        ).fetchone()
        n_lanc = con.execute("SELECT COUNT(*) as n FROM lancamentos WHERE usuario_id=?", (u["id"],)).fetchone()["n"]
        n_pend = con.execute(
            "SELECT COUNT(*) as n FROM perguntas_pendentes WHERE usuario_id=? AND respondida=0", (u["id"],)
        ).fetchone()["n"]
        if arq["n"] > 0:
            situacao = "extrato"
        elif u["ultimo_acesso"] or n_lanc > 0:
            situacao = "acessou"
        else:
            situacao = "nao_acessou"
        saida.append({
            "id": u["id"], "nome": u["nome"], "criado_em": u["criado_em"], "ultimo_acesso": u["ultimo_acesso"],
            "extratos": arq["n"], "primeiro_extrato": arq["primeiro"], "ultimo_extrato": arq["ultimo"],
            "lancamentos": n_lanc, "pendencias": n_pend, "situacao": situacao,
        })
    return saida


@app.route("/acompanhamento")
def pagina_acompanhamento():
    chave_certa = os.environ.get("ADMIN_CHAVE", "")
    chave = request.args.get("chave", "")
    if not chave_certa:
        return ("Página desligada: crie a variável ADMIN_CHAVE no Railway (Variables) e abra "
                "/acompanhamento?chave=SUA_CHAVE.", 403, {"Content-Type": "text/plain; charset=utf-8"})
    if not hmac.compare_digest(chave.encode(), chave_certa.encode()):
        return ("Chave errada.", 403, {"Content-Type": "text/plain; charset=utf-8"})

    con = conectar()
    try:
        linhas = dados_acompanhamento(con)
    finally:
        con.close()

    rotulos = {"extrato": ("Já enviou extrato", "ok"), "acessou": ("Acessou, sem extrato", "meio"), "nao_acessou": ("Ainda não acessou", "nao")}
    total = len(linhas)
    n_acessou = sum(1 for l in linhas if l["situacao"] != "nao_acessou")
    n_extrato = sum(1 for l in linhas if l["situacao"] == "extrato")
    base = request.host_url.rstrip("/")
    corpo = []
    for l in linhas:
        rotulo, classe = rotulos[l["situacao"]]
        link = f"{base}/?usuario={l['id']}"
        corpo.append(f"""
        <tr>
          <td><b>{escape(l['nome'])}</b><div class="sub">criado {_hora_brasil(l['criado_em'])}</div></td>
          <td><span class="chip {classe}">{rotulo}</span></td>
          <td>{_hora_brasil(l['ultimo_acesso'])}</td>
          <td>{l['extratos']}<div class="sub">{'1º em ' + _hora_brasil(l['primeiro_extrato']) if l['extratos'] else ''}</div></td>
          <td>{l['lancamentos']}</td>
          <td>{l['pendencias']}</td>
          <td><button type="button" data-link="{escape(link)}">copiar link</button></td>
        </tr>""")
    html = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Acompanhamento do teste · Assessor.IA</title>
<style>
  :root {{ --bg:#F3F6F5; --paper:#fff; --ink:#14231F; --muted:#66756F; --line:#E3EAE7; --primary:#0F8F78; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#110F1C; --paper:#1C1930; --ink:#F1EEFF; --muted:#A49FC2; --line:#2E2A4A; --primary:#9B7BFF; color-scheme: dark; }} }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system, 'Segoe UI', sans-serif; padding:24px 16px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }} p {{ color:var(--muted); margin:0 0 18px; font-size:14px; }}
  .kpis {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
  .kpi {{ background:var(--paper); border:1px solid var(--line); border-radius:14px; padding:12px 16px; min-width:140px; }}
  .kpi b {{ display:block; font-size:24px; }} .kpi span {{ font-size:12px; color:var(--muted); }}
  .tabela {{ overflow-x:auto; background:var(--paper); border:1px solid var(--line); border-radius:14px; }}
  table {{ border-collapse:collapse; width:100%; min-width:720px; font-size:14px; }}
  th, td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }}
  .sub {{ font-size:11.5px; color:var(--muted); }}
  .chip {{ font-size:12px; font-weight:700; padding:3px 9px; border-radius:999px; white-space:nowrap; }}
  .ok {{ background:#E3F5EC; color:#135C3A; }} .meio {{ background:#FFF1D6; color:#6B4500; }} .nao {{ background:#FDE8E4; color:#7A2414; }}
  button {{ font:inherit; font-size:12px; border:1px solid var(--line); background:var(--bg); color:var(--ink); border-radius:8px; padding:5px 9px; cursor:pointer; }}
</style></head><body>
<h1>Acompanhamento do teste</h1>
<p>Quem já abriu o link e quem já enviou extrato. Horários de Brasília. Atualize a página pra ver o mais recente.</p>
<div class="kpis">
  <div class="kpi"><b>{total}</b><span>participantes criados</span></div>
  <div class="kpi"><b>{n_acessou}</b><span>já acessaram</span></div>
  <div class="kpi"><b>{n_extrato}</b><span>já enviaram extrato</span></div>
</div>
<div class="tabela"><table>
  <thead><tr><th>Participante</th><th>Situação</th><th>Último acesso</th><th>Extratos</th><th>Lançamentos</th><th>Pendências</th><th>Link</th></tr></thead>
  <tbody>{''.join(corpo) or '<tr><td colspan="7">Nenhum participante criado ainda.</td></tr>'}</tbody>
</table></div>
<script>
  document.querySelectorAll("button[data-link]").forEach(function (b) {{
    b.addEventListener("click", function () {{
      navigator.clipboard.writeText(b.dataset.link).then(function () {{ b.textContent = "copiado!"; }},
        function () {{ prompt("Copie o link:", b.dataset.link); }});
    }});
  }});
</script>
</body></html>"""
    return html


@app.route("/")
def pagina_painel():
    """Serve a própria página do painel — é a URL que você compartilha
    com cada participante (com ?usuario=ID no final)."""
    return send_from_directory(os.path.dirname(__file__), "painel.html")


if __name__ == "__main__":
    criar_schema()
    porta = int(os.environ.get("PORT", 5000))  # Railway define PORT sozinho
    app.run(host="0.0.0.0", port=porta)
