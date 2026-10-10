# Manas · Caixa e Estoque

Sistema de vendas, estoque, contas a pagar e fechamento mensal do brechó Manas moda sustentável.

## Como funciona
- `app.py`: servidor (Flask). Tela de senha, página do sistema e API de dados.
- `db.py`: guarda tudo na tabela `manas_docs` do Postgres (separada das tabelas do Assessor.IA).
- `static/`: página do sistema, tela de entrada, ícones e arquivos do app instalável.
- `dados_iniciais.json`: configurações e contas fixas carregadas só na primeira vez que o banco está vazio.

## Variáveis na Railway
| Variável | Valor |
|---|---|
| `DATABASE_URL` | referência ao Postgres do projeto (`${{Postgres.DATABASE_URL}}`) |
| `MANAS_SENHA` | senha que a loja usa para entrar |
| `SECRET_KEY` | texto longo e aleatório (protege o login) |

## Teste no computador
`pip install -r requirements.txt` e depois `MANAS_SENHA=teste python app.py`. Sem `DATABASE_URL`, usa um arquivo SQLite local.
