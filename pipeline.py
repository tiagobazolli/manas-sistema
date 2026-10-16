# -*- coding: utf-8 -*-
"""
pipeline.py — Orquestrador: decide a camada certa e devolve transações
no formato comum, pronto para o motor de categorização (Semanas 5-6).
"""
from extrator import detectar_camada, extrair_ofx
from extrator_pdf_texto import parse_sicoob, parse_santander, parse_caixa_texto
from extrator_visao import extrair_texto_por_visao
from parser_caixa_ocr import parse_caixa_ocr
from parser_generico import parse_generico

# qual parser de texto usar por banco — na Semana 5-6 isso vira uma
# detecção automática (procurar "SICOOB"/"Santander" no cabeçalho do PDF)
PARSERS_TEXTO = {
    "sicoob": parse_sicoob,
    "santander": parse_santander,
    "caixa": parse_caixa_texto,
}


def processar_extrato(caminho: str, banco: str | None = None) -> dict:
    """Processa um extrato de qualquer camada e devolve:
    {"camada_usada": str, "transacoes": [...], "nao_reconhecidas": [...]}
    """
    camada = detectar_camada(caminho)

    if camada == "ofx":
        return {"camada_usada": "ofx", "transacoes": extrair_ofx(caminho), "nao_reconhecidas": []}

    if camada == "pdf_texto":
        # banco sem parser próprio (PicPay, Banco do Brasil, não identificado...)
        # passa pelo leitor genérico, em vez de travar o envio
        txns = PARSERS_TEXTO.get(banco, parse_generico)(caminho)
        return {"camada_usada": "pdf_texto", "transacoes": txns, "nao_reconhecidas": []}

    if camada == "pdf_visao":
        texto = extrair_texto_por_visao(caminho)
        # por enquanto só temos parser de visão pra Caixa; outros bancos
        # entram aqui conforme aparecerem no teste com o grupo pequeno
        if banco == "caixa":
            txns, falhas = parse_caixa_ocr(texto)
            return {"camada_usada": "pdf_visao", "transacoes": txns, "nao_reconhecidas": falhas}
        raise RuntimeError("Esse PDF parece ser uma imagem (escaneado ou print), e por enquanto "
                           "só consigo ler esse tipo da Caixa. Se o app do banco tiver a opção, "
                           "baixa o extrato em OFX ou em PDF normal e manda de novo.")

    raise ValueError(f"Camada não tratada: {camada}")


if __name__ == "__main__":
    testes = [
        ("/mnt/user-data/uploads/sicredi_agosto.ofx", None),
        ("/mnt/user-data/uploads/sicoob_agosto.pdf", "sicoob"),
        ("/mnt/user-data/uploads/santander_agosto.pdf", "santander"),
        ("/mnt/user-data/uploads/caixa_agosto.pdf", "caixa"),
    ]
    print(f"{'Arquivo':<30}{'Camada':<14}{'Transações':<12}{'Não reconhecidas'}")
    print("-" * 75)
    for caminho, banco in testes:
        r = processar_extrato(caminho, banco)
        nome = caminho.split("/")[-1]
        print(f"{nome:<30}{r['camada_usada']:<14}{len(r['transacoes']):<12}{len(r['nao_reconhecidas'])}")
