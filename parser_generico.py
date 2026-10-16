# -*- coding: utf-8 -*-
"""
parser_generico.py — leitor "genérico" de extrato em PDF com texto, pra
banco que ainda não tem parser próprio (PicPay, Banco do Brasil e
qualquer outro que aparecer no grupo de teste).

Não conhece o layout de nenhum banco: procura linhas com data + valor em
reais e tenta descobrir se é entrada ou saída pelo que estiver em volta
(sinal "-", "D"/"C", "(-)"/"(+)", ou palavras como "enviado"/"recebido").
Linhas com data mas sem valor viram a data "corrente" das linhas
seguintes (extratos que agrupam por dia), e linhas soltas logo abaixo de
um lançamento são juntadas à descrição dele (nome de quem recebeu o Pix,
por exemplo), que ajuda muito o categorizador.

É um "melhor esforço": o que ele não tiver certeza continua passando
pelo categorizador normal, que vira pergunta pro usuário quando não
reconhece — nunca some nada em silêncio.
"""
import re
import subprocess

_MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
          "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}

# 15/09/2026, 15/09/26, 15/09, 15-09-2026
_RE_DATA_NUM = re.compile(r"^(\d{2})[/-](\d{2})(?:[/-](\d{4}|\d{2}))?(?!\d)")
# 15 de setembro de 2026, 15 set 2026, 15 SET
_RE_DATA_EXT = re.compile(r"^(\d{1,2})\s+(?:de\s+)?([a-zç]{3})[a-zç]*\.?(?:\s+(?:de\s+)?(\d{4}))?\b", re.IGNORECASE)
_RE_HORA = re.compile(r"^(?:às\s+|-\s*)?\d{2}:\d{2}(?::\d{2})?\s*")

# valor em reais: "- R$ 1.234,56", "R$ -50,00", "50,00 D", "50,00 (-)", "-50,00"
_RE_VALOR = re.compile(
    r"(?P<pre>[-+−]\s*)?(?:R\$\s*)?(?P<pre2>[-+−]\s*)?"
    r"(?P<num>\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})(?!\d)"
    r"(?P<pos>\s*\([+-]\)|\s*[-+](?![\d])|\s?[CD]\b)?"
)

_PALAVRAS_PULAR = (
    "SALDO", "S A L D O", "TOTAL", "LIMITE", "PERÍODO", "PERIODO", "AGÊNCIA",
    "AGENCIA", "CONTA CORRENTE:", "CPF", "CNPJ:", "EXTRATO", "PÁGINA", "PAGINA",
    "OUVIDORIA", "SAC ", "DATA ", "LANÇAMENTOS FUTUROS", "LANCAMENTOS FUTUROS",
)
# palavras que, quando não há sinal nenhum no valor, indicam entrada
_PALAVRAS_CREDITO = (
    "RECEBID", "RECEBIMENTO", "DEPOSITO", "DEPÓSITO", "CRÉDITO", "CREDITO",
    "ESTORNO", "RENDIMENTO", "RESGATE", "REEMBOLSO", "DEVOLU", "SALARIO",
    "SALÁRIO", "PROVENTO", "CASHBACK", "ENTRADA",
)


def _texto_pdf(caminho: str) -> str:
    return subprocess.run(["pdftotext", "-layout", caminho, "-"], capture_output=True, text=True).stdout


def _br_valor(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def _ano_referencia(texto: str, ano_padrao: int) -> int:
    """Ano mais citado em datas completas do texto (cabeçalho de período,
    por exemplo) — usado pras linhas que só trazem dia/mês."""
    anos = re.findall(r"\b\d{2}/\d{2}/(20\d{2})\b", texto)
    if not anos:
        return ano_padrao
    return int(max(set(anos), key=anos.count))


def _ler_data(linha: str, ano_ref: int):
    """Se a linha começa com uma data, devolve (data_iso, resto_da_linha)."""
    m = _RE_DATA_NUM.match(linha)
    if m:
        dia, mes, ano = int(m.group(1)), int(m.group(2)), m.group(3)
        if ano and len(ano) == 2:
            ano = "20" + ano
        ano = int(ano) if ano else ano_ref
    else:
        m = _RE_DATA_EXT.match(linha)
        if not m or m.group(2).lower()[:3] not in _MESES:
            return None, linha
        dia, mes = int(m.group(1)), _MESES[m.group(2).lower()[:3]]
        ano = int(m.group(3)) if m.group(3) else ano_ref
    if not (1 <= dia <= 31 and 1 <= mes <= 12):
        return None, linha
    resto = linha[m.end():].strip()
    # BB repete a data do movimento logo depois da do balancete
    m2 = _RE_DATA_NUM.match(resto)
    if m2:
        resto = resto[m2.end():].strip()
    resto = _RE_HORA.sub("", resto).strip()
    return f"{ano:04d}-{mes:02d}-{dia:02d}", resto


def _tipo_do_valor(m, linha_inteira: str) -> str | None:
    pre = (m.group("pre") or m.group("pre2") or "").strip()
    pos = (m.group("pos") or "").strip()
    if pre in ("-", "−") or pos in ("D", "(-)", "-"):
        return "D"
    if pre == "+" or pos in ("C", "(+)", "+"):
        return "C"
    return None


def _tipo_por_palavra(descricao: str) -> str:
    d = descricao.upper()
    if any(p in d for p in ("ENVIADO", "ENVIADA", "PAGAMENTO", "COMPRA", "SAQUE", "TARIFA", "SAÍDA", "SAIDA", "DÉBITO", "DEBITO")):
        return "D"
    if any(p in d for p in _PALAVRAS_CREDITO):
        return "C"
    return "D"


def _deve_pular(texto: str) -> bool:
    t = texto.upper()
    return any(p in t for p in _PALAVRAS_PULAR)


def _limpar_descricao(s: str) -> str:
    # tira o que é coluna e não descrição: agência/lote/documento (número
    # solto), data e hora repetidas no detalhe, e a coluna "Entrada/Saída"
    tokens = [t for t in s.split()
              if not re.fullmatch(r"\d+|\d{2}/\d{2}(?:/\d{2,4})?|\d{2}:\d{2}(?::\d{2})?", t)]
    s = " ".join(tokens)
    s = re.sub(r"\s+(Entrada|Sa[ií]da)$", "", s, flags=re.IGNORECASE)
    return s.strip(" -–|")


def parse_generico_texto(texto: str, ano_padrao: int = 2026) -> tuple[list[dict], list[str]]:
    """Devolve (transacoes, linhas_com_data_que_nao_entendi)."""
    ano_ref = _ano_referencia(texto, ano_padrao)
    transacoes, nao_reconhecidas = [], []
    data_atual = None
    descricao_pendente = ""   # texto com data mas sem valor, à espera do valor na linha seguinte
    ultima_foi_transacao = False
    linhas_desde_transacao = 0

    linhas = [l.strip() for l in texto.splitlines() if l.strip()]

    def _proxima_eh_so_valor(i):
        """A linha seguinte é só um valor (sem texto)? Então esta linha é
        a descrição do PRÓXIMO lançamento, não detalhe do anterior."""
        if i + 1 >= len(linhas):
            return False
        prox = linhas[i + 1]
        return bool(_RE_VALOR.search(prox)) and not re.search(r"[A-Za-zÀ-ú]", re.sub(r"R\$|\b[CD]\b", "", prox))

    def _descricao_na_linha_seguinte(i):
        """Layout do app do BB: "10/09/2026   R$ 0,19" numa linha e a
        descrição ("Juros") sozinha na linha de baixo."""
        if i + 1 >= len(linhas):
            return None
        prox = linhas[i + 1]
        if _RE_VALOR.search(prox) or _ler_data(prox, ano_ref)[0] or _deve_pular(prox):
            return None
        return prox if re.search(r"[A-Za-zÀ-ú]", prox) and len(prox) <= 80 else None

    consumidas = set()
    tipo_inferido = []  # índices de transações cujo tipo saiu só de palavra-chave
    for i, linha in enumerate(linhas):
        if i in consumidas:
            continue
        data, resto = _ler_data(linha, ano_ref)
        valores = list(_RE_VALOR.finditer(resto))

        if _deve_pular(resto if data else linha):
            if data:
                data_atual = data
            ultima_foi_transacao = False
            descricao_pendente = ""
            continue

        if valores:
            if data:
                data_atual = data
            if not data_atual:
                continue
            m = valores[0]  # quando tem dois valores, o segundo costuma ser o saldo
            descricao = _limpar_descricao(resto[: m.start()] + " " + resto[m.end(): valores[1].start() if len(valores) > 1 else len(resto)])
            # descrição sem letra nenhuma (só a coluna "Saldo" sobrando etc.)
            if not re.search(r"[A-Za-zÀ-ú]", descricao):
                if descricao_pendente:
                    descricao = descricao_pendente
                else:
                    seguinte = _descricao_na_linha_seguinte(i)
                    if seguinte:
                        descricao = _limpar_descricao(seguinte)
                        consumidas.add(i + 1)
            elif descricao_pendente and not data:
                descricao = f"{descricao_pendente} {descricao}"
            descricao_pendente = ""
            # menos de 3 letras não é descrição de lançamento — é resto de
            # cabeçalho, ex. "16 de setembro de 2026 a   R$ 91,12" (período
            # + saldo final, no PicPay)
            if len(re.findall(r"[A-Za-zÀ-ú]", descricao)) < 3:
                nao_reconhecidas.append(linha)
                ultima_foi_transacao = False
                continue
            tipo = _tipo_do_valor(m, resto)
            if not tipo:
                m_col = re.search(r"\b(Entrada|Sa[ií]da|Cr[eé]dito|D[eé]bito)\b\s*$", resto, re.IGNORECASE)
                if m_col:
                    tipo = "C" if m_col.group(1).lower().startswith(("e", "c")) else "D"
            if not tipo:
                tipo = _tipo_por_palavra(descricao)
                tipo_inferido.append(len(transacoes))
            valor = _br_valor(m.group("num"))
            if valor == 0:
                continue
            transacoes.append({"data": data_atual, "descricao": descricao, "valor": valor, "tipo": tipo})
            ultima_foi_transacao = True
            linhas_desde_transacao = 0
            continue

        # linha sem valor nenhum
        if data:
            if resto and ultima_foi_transacao and linhas_desde_transacao < 2 and data == data_atual and not _proxima_eh_so_valor(i):
                # detalhe do lançamento anterior (BB: "16/09 14:20 Fulano de Tal")
                transacoes[-1]["descricao"] = _limpar_descricao(f"{transacoes[-1]['descricao']} {resto}")
                linhas_desde_transacao += 1
                continue
            data_atual = data
            descricao_pendente = _limpar_descricao(resto)
            ultima_foi_transacao = False
            continue

        if not re.search(r"[A-Za-zÀ-ú]", linha) or len(linha) > 80:
            continue
        if ultima_foi_transacao and linhas_desde_transacao < 2 and not _proxima_eh_so_valor(i):
            transacoes[-1]["descricao"] = _limpar_descricao(f"{transacoes[-1]['descricao']} {linha}")
            linhas_desde_transacao += 1
        elif data_atual:
            descricao_pendente = _limpar_descricao(f"{descricao_pendente} {linha}")
            ultima_foi_transacao = False

    # Valor sem sinal nenhum: se o mesmo extrato marca as saídas com "-"
    # (app do BB, PicPay), então o que veio sem sinal é entrada. Extrato de
    # poupança sem sinal também: o que entra é rendimento/depósito.
    tem_saida_com_sinal = any(i not in tipo_inferido and t["tipo"] == "D" for i, t in enumerate(transacoes))
    eh_poupanca = "POUPAN" in texto[:1500].upper()
    if tem_saida_com_sinal or eh_poupanca:
        for i in tipo_inferido:
            d = transacoes[i]["descricao"].upper()
            if eh_poupanca and not tem_saida_com_sinal and any(p in d for p in ("RESGATE", "SAQUE", "TRANSF", "ENVIAD", "PAGAMENTO", "TARIFA")):
                continue
            transacoes[i]["tipo"] = "C"

    return transacoes, nao_reconhecidas


def parse_generico(caminho: str, ano: int = 2026) -> list[dict]:
    transacoes, _ = parse_generico_texto(_texto_pdf(caminho), ano)
    return transacoes
