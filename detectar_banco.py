# -*- coding: utf-8 -*-
"""
detectar_banco.py — identifica qual banco é o extrato pelo texto/cabeçalho,
pra não precisar perguntar isso ao usuário toda vez.
"""
import subprocess

_MARCADORES = {
    "caixa": ["CAIXA", "SISTEMA DE COOPERATIVAS"],  # o segundo é só pra não confundir com Sicoob
    "sicoob": ["SICOOB", "SISTEMA DE COOPERATIVAS DE CRÉDITO"],
    "santander": ["SANTANDER"],
    "sicredi": ["SICREDI"],
    "bb": ["BANCO DO BRASIL"],
    "picpay": ["PICPAY"],
}


# mesma ordem de prioridade da busca no texto inteiro, lá embaixo
_ORDEM_CABECALHO = [
    ("sicoob", ["SICOOB"]),
    ("santander", ["SANTANDER"]),
    ("sicredi", ["SICREDI"]),
    # o PDF do app do BB não escreve o nome do banco em lugar nenhum (só
    # o logo, que é imagem) — "Saldos por data-base" é frase própria dele
    ("bb", ["BANCO DO BRASIL", "BB.COM.BR", "SALDOS POR DATA-BASE", "DATAS BASE DISPON"]),
    ("picpay", ["PICPAY"]),
    ("caixa", ["CAIXA ECON", "CAIXA.GOV"]),
]


def detectar_banco_pdf(caminho: str) -> str | None:
    texto = subprocess.run(
        ["pdftotext", "-layout", caminho, "-"], capture_output=True, text=True
    ).stdout.upper()

    # primeiro olha só o cabeçalho (começo do texto): extrato de um banco
    # costuma citar outros bancos nos lançamentos (ex. Pix pra alguém do
    # PicPay num extrato do Sicoob), e o nome no topo é o que vale
    cabecalho = texto[:1500]
    for banco, marcas in _ORDEM_CABECALHO:
        if any(m in cabecalho for m in marcas):
            return banco

    if "SICOOB" in texto:
        return "sicoob"
    if "SANTANDER" in texto:
        return "santander"
    if "SICREDI" in texto:
        return "sicredi"
    if "BANCO DO BRASIL" in texto:
        return "bb"
    if "PICPAY" in texto:
        return "picpay"
    if "CAIXA" in texto or not texto.strip():
        # PDF escaneado da Caixa não tem texto nenhum extraível por aqui;
        # na prática, se não achamos nenhum outro banco E o texto vier vazio,
        # assumimos Caixa e confirmamos depois de rodar a visão (heurística
        # razoável pro MVP — poucos bancos no grupo de teste)
        return "caixa"
    return None


import re

# Código do banco (padrão FEBRABAN/Bacen) que aparece em <BANKID> no OFX —
# bem mais confiável que procurar o nome no texto: testando com um OFX
# real do Sicredi, a palavra "SICREDI" não aparece em lugar nenhum do
# arquivo (só o nome jurídico da cooperativa central), só o código 748
# identifica o banco de forma garantida.
_BANKID_PARA_BANCO = {
    "748": "sicredi",
    "756": "sicoob",
    "033": "santander",
    "104": "caixa",
    "001": "bb",
    "380": "picpay",
}


def detectar_banco_ofx(caminho: str) -> str | None:
    """Detecta o banco de um extrato OFX. Primeiro tenta pelo <BANKID>
    (código oficial do banco, sempre presente e confiável); se não achar,
    cai pro mesmo esquema de procurar o nome no texto do PDF, como
    reforço pra bancos que não estão no mapa acima."""
    with open(caminho, encoding="cp1252", errors="replace") as f:
        texto = f.read().upper()

    bankid_m = re.search(r"<BANKID>\s*0*(\d+)", texto)
    if bankid_m:
        codigo = bankid_m.group(1).zfill(3)
        if codigo in _BANKID_PARA_BANCO:
            return _BANKID_PARA_BANCO[codigo]

    if "SICOOB" in texto:
        return "sicoob"
    if "SANTANDER" in texto:
        return "santander"
    if "SICREDI" in texto:
        return "sicredi"
    if "BANCO DO BRASIL" in texto:
        return "bb"
    if "PICPAY" in texto:
        return "picpay"
    if "CAIXA" in texto:
        return "caixa"
    return None
