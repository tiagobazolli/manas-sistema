# -*- coding: utf-8 -*-
"""
categorizador.py — Motor de categorização (Semanas 5-6).

Implementa a ordem de precedência da especificação (seção 4.3):
  1) exclusões pessoais
  2) memória pessoal de entidade
  3) regras pessoais de categoria
  4) base global por CNPJ
  5) correspondência por valor recorrente já conhecido
  6) regras genéricas de palavra-chave (baseline)
  7) se nada bateu -> pergunta pro usuário
"""
import re
from datetime import datetime
from db import conectar, novo_id

_RE_CNPJ = re.compile(r"CNPJ:(\d{2}\.\d{3}\.\d{3}/\d{4}-?\d{2})")
_RE_DOC_MASCARADO = re.compile(r"\*{2,3}\.?\s?(\d{3}\.\d{3})[-.]?\*{0,3}")

# regras genéricas de baseline — as mesmas validadas na Fase 0, servem
# de última tentativa antes de perguntar pro usuário
_REGRAS_GENERICAS = [
    (r"credito salario", "Salário / Renda Fixa"),
    (r"credito juros|correcao monetaria|remuneracao aplicacao|reajuste monet|rendimento poupan", "Rendimentos e Investimentos"),
    (r"reembolso|estorno|devolucao pix", "Reembolsos e Devoluções"),
    (r"transporte publico", "Transporte"),
    (r"supermerc|mercado|carnes|acougue", "Alimentação"),
    (r"restaurant|lanch", "Lazer e Entretenimento"),
    (r"\bposto\b|autoposto", "Transporte"),
]


class Categorizacao:
    def __init__(self, categoria, confianca, origem, subcategoria=None):
        self.categoria = categoria
        self.confianca = confianca  # "alta" | "media" | "baixa"
        self.origem = origem        # de onde veio a decisão (pra debug/transparência)
        # só a memória pessoal (correção anterior do usuário) sabe subgrupo;
        # regra de palavra-chave/CNPJ genérico não tem essa informação
        self.subcategoria = subcategoria

    def __repr__(self):
        return f"<{self.categoria} | {self.confianca} | {self.origem}>"


def _em_periodo_exclusao(data_str: str, periodo_inicio: str, periodo_fim: str) -> bool:
    if not periodo_inicio or not periodo_fim:
        return True
    d = datetime.fromisoformat(data_str).date()
    return datetime.fromisoformat(periodo_inicio).date() <= d <= datetime.fromisoformat(periodo_fim).date()


def registrar_emprestimo(usuario_id: str, pessoa_identificador: str, valor: float, data: str, lancamento_id: str | None = None):
    """Chamado quando o usuário responde a uma pergunta dizendo que foi um
    empréstimo. A próxima transação dessa pessoa vira uma pergunta de
    conciliação, em vez de categorização normal."""
    con = conectar()
    con.execute(
        """INSERT INTO emprestimos_pendentes
           (id, usuario_id, pessoa_identificador, valor_original, data_emprestimo, lancamento_id, status)
           VALUES (?,?,?,?,?,?,'aguardando')""",
        (novo_id(), usuario_id, pessoa_identificador.upper(), valor, data, lancamento_id),
    )
    con.commit()
    con.close()


def quitar_emprestimo(emprestimo_id: str):
    con = conectar()
    con.execute("UPDATE emprestimos_pendentes SET status='quitado' WHERE id=?", (emprestimo_id,))
    con.commit()
    con.close()


def categorizar(usuario_id: str, descricao: str, valor: float, data: str,
                 conta_identificador: str | None = None) -> Categorizacao | None:
    """Devolve uma Categorizacao, ou None se nada bateu (precisa perguntar)."""
    con = conectar()
    cur = con.cursor()
    desc_upper = descricao.upper()

    # -1) empréstimo pendente com essa pessoa? pergunta específica de
    # conciliação, não tenta adivinhar sozinho (risco de falso positivo
    # com outra transação de valor parecido sem relação nenhuma)
    for row in cur.execute(
        "SELECT * FROM emprestimos_pendentes WHERE usuario_id=? AND status='aguardando'", (usuario_id,)
    ):
        eh_o_proprio_emprestimo = (abs(valor - row["valor_original"]) < 0.01 and data == row["data_emprestimo"])
        if row["pessoa_identificador"] in desc_upper and eh_o_proprio_emprestimo:
            con.close()
            return Categorizacao("EXCLUIDO_EMPRESTIMO", "alta", "saída original do empréstimo, já registrada")
        if row["pessoa_identificador"] in desc_upper and not eh_o_proprio_emprestimo:
            con.close()
            return Categorizacao(
                "PERGUNTA_CONCILIACAO_EMPRESTIMO", "alta",
                f"possível devolução do empréstimo de R${row['valor_original']:.2f} em {row['data_emprestimo']} "
                f"(id={row['id']})",
            )

    # 0) autotransferência — antes de qualquer outra coisa, porque não é
    # nem despesa nem receita de verdade, é o próprio dinheiro mudando de conta
    ident_nomes = [r["valor"] for r in cur.execute(
        "SELECT valor FROM identificadores_proprios WHERE usuario_id=? AND tipo='nome'", (usuario_id,)
    )]
    ident_docs = [r["valor"] for r in cur.execute(
        "SELECT valor FROM identificadores_proprios WHERE usuario_id=? AND tipo='documento_mascarado'", (usuario_id,)
    )]
    nome_bateu = any(
        " ".join(nome.split()[:2]) in desc_upper for nome in ident_nomes if len(nome.split()) >= 2
    )
    m_doc = _RE_DOC_MASCARADO.search(descricao)
    doc_bateu = m_doc and m_doc.group(1) in ident_docs

    if nome_bateu or doc_bateu:
        # aprendizado automático: se o nome apareceu junto de um documento
        # mascarado que ainda não conhecíamos, guarda esse fragmento — da
        # próxima vez ele resolve sozinho, mesmo sem o nome (caso Sicoob)
        if nome_bateu and m_doc and m_doc.group(1) not in ident_docs:
            cur.execute(
                "INSERT OR IGNORE INTO identificadores_proprios (id, usuario_id, tipo, valor) VALUES (?,?,?,?)",
                (novo_id(), usuario_id, "documento_mascarado", m_doc.group(1)),
            )
            con.commit()
        con.close()
        return Categorizacao("AUTOTRANSFERENCIA", "alta",
                              "nome próprio no PIX" if nome_bateu else "CPF mascarado já visto com seu nome")

    # 1) exclusões pessoais
    if conta_identificador:
        for row in cur.execute(
            "SELECT * FROM memoria_exclusao WHERE usuario_id=? AND conta_identificador=?",
            (usuario_id, conta_identificador),
        ):
            if _em_periodo_exclusao(data, row["periodo_inicio"], row["periodo_fim"]):
                excecoes = row["excecoes"] or "[]"
                if not any(exc.upper() in desc_upper for exc in eval(excecoes)):
                    con.close()
                    return Categorizacao("EXCLUIDO_TERCEIRO", "alta", f"exclusão: {row['motivo']}")

    # 2) memória pessoal de entidade (nome de texto ou cpf/cnpj mascarado)
    for row in cur.execute(
        "SELECT * FROM memoria_entidade WHERE usuario_id=?", (usuario_id,)
    ):
        if row["valor_match"].upper() in desc_upper:
            con.close()
            return Categorizacao(row["categoria"], "alta", f"memória pessoal: {row['descricao']}", subcategoria=row["subcategoria"])

    # 3) regras pessoais de categoria
    for row in cur.execute(
        "SELECT * FROM memoria_regra_categoria WHERE usuario_id=?", (usuario_id,)
    ):
        if re.search(row["padrao_regex"], desc_upper, re.IGNORECASE):
            con.close()
            return Categorizacao(row["categoria"], "alta", "regra pessoal")

    # 4) base global por CNPJ
    m_cnpj = _RE_CNPJ.search(descricao)
    if m_cnpj:
        row = cur.execute(
            "SELECT * FROM base_global_cnpj WHERE cnpj=?", (m_cnpj.group(1),)
        ).fetchone()
        if row:
            con.close()
            return Categorizacao(_segmento_para_categoria(row["segmento"]), "media",
                                  f"base global: {row['nome_empresa']}")

    # 5) regras genéricas de palavra-chave (baseline, compartilhado) — vem
    # antes do valor recorrente de propósito: um padrão de texto é um sinal
    # mais confiável que "esse valor parece com outro por coincidência"
    for padrao, categoria in _REGRAS_GENERICAS:
        if re.search(padrao, desc_upper, re.IGNORECASE):
            con.close()
            return Categorizacao(categoria, "baixa", "regra genérica")

    # 6) correspondência por valor recorrente já conhecido — só entra em
    # ação com tolerância mínima (praticamente exato, só cobre diferença
    # de arredondamento) e exigindo pelo menos 2 ocorrências anteriores
    # com a MESMA categoria. Um valor "parecido" (ex: R$99,99 vs R$100,00
    # de outra coisa completamente diferente) não deve bastar sozinho —
    # já causou categorização errada (internet virando Transporte só
    # porque tinha um posto de R$100 por perto).
    row = cur.execute(
        """SELECT categoria, COUNT(*) as n FROM lancamentos
           WHERE usuario_id=? AND ABS(valor-?)<0.005 AND tipo=? AND categoria IS NOT NULL
           GROUP BY categoria HAVING COUNT(*) >= 2 ORDER BY n DESC LIMIT 1""",
        (usuario_id, valor, "D"),
    ).fetchone()
    if row:
        con.close()
        return Categorizacao(row["categoria"], "media", "valor recorrente conhecido")

    con.close()
    return None  # nada bateu -> gera pergunta


def _segmento_para_categoria(segmento: str | None) -> str:
    mapa = {
        "energia elétrica": "Moradia",
        "internet/telecom": "Moradia",
        "cosméticos": "Saúde e Bem-estar",
        "educação/salário": "Salário / Renda Fixa",
        "marketplace online": "Compras Online",
    }
    return mapa.get(segmento, "Outros/Diversos")
