# Setup para um único homeserver

Este pipeline cria a massa usada pelo Locust em testes contra um único
homeserver Matrix. Para voltar ao ponto de entrada, consulte o
[README principal](../../README.md). Para executar a carga depois do setup, siga o
[guia de experimentos](../experiments/design.md).
Para gerar carga simultânea em dois ou mais servidores, use o
[setup de federação](federation.md) e o
[guia de carga federada](../experiments/federation.md).

## O que é gerado

```text
data/homeserver/
├── users.csv           usuários e senhas
├── tokens.csv          MXIDs, access tokens e cursores de sync
├── rooms.json          distribuição dos usuários nas salas
├── rooms_status.csv    resultado da criação das salas
└── failed_users.txt    registros que falharam
```

O Locust usa `data/homeserver/` por padrão. Para outro dataset, defina
`MATRIX_DATA_DIR` ou passe `--data-dir` ao executor.

## Pré-requisitos

- Python 3.11–3.14 e Poetry 2.x;
- registro sem CAPTCHA ou confirmação de e-mail;
- rate limits desativados ou dimensionados para o setup e a carga;
- conectividade até a API cliente do homeserver.

Instale as dependências uma única vez na raiz:

```console
poetry install
```

## Configuração

```console
cp setup_homeserver/.env.example setup_homeserver/.env
```

Edite pelo menos:

```dotenv
MATRIX_SERVER=https://matrix-test.example.com
```

Variáveis úteis:

```dotenv
MATRIX_DOMAIN=matrix-test.example.com
VERIFY_TLS=true
NUM_USERS=150
WORKERS=8
HTTP_TIMEOUT=30
HTTP_RETRIES=3
REQUEST_SLEEP=0
MATRIX_DATA_DIR=data/homeserver
```

As opções individuais `USERS_CSV`, `TOKENS_CSV`, `ROOMS_JSON`,
`ROOMS_STATUS_CSV` e `FAILED_USERS_TXT` sobrescrevem arquivos específicos,
mas normalmente basta alterar `MATRIX_DATA_DIR`.

## Execução recomendada

Sempre execute a partir da raiz do repositório:

```console
setup_homeserver/run_all.sh 150
```

Sem argumento, o script usa `NUM_USERS` do `.env`. As etapas são:

```text
01_generate_users.py
        │ users.csv
        ▼
02_generate_rooms.py
        │ rooms.json
        ▼
03_register_users.py
        │ tokens.csv
        ▼
04_create_rooms.py
        │ rooms_status.csv
        ▼
05_accept_invites.py
        │ tokens.csv atualizado
        ▼
dataset pronto
```

Os passos são reexecutáveis: resultados concluídos são reaproveitados e falhas
podem ser tentadas novamente.

## Execução passo a passo

### 1. Gerar usuários

```console
poetry run python setup_homeserver/01_generate_users.py 150 --seed 42
```

### 2. Gerar salas

```console
poetry run python setup_homeserver/02_generate_rooms.py --seed 42
```

### 3. Registrar contas

```console
poetry run python setup_homeserver/03_register_users.py
```

Opções úteis:

```console
poetry run python setup_homeserver/03_register_users.py --workers 16
poetry run python setup_homeserver/03_register_users.py --force
```

### 4. Criar salas e convites

```console
poetry run python setup_homeserver/04_create_rooms.py
```

### 5. Aceitar convites

```console
poetry run python setup_homeserver/05_accept_invites.py
```

O passo atualiza `next_batch` em `tokens.csv`, reduzindo a quantidade de
histórico que o primeiro `/sync` do Locust precisa processar.

## Verificação antes da carga

```console
test -s data/homeserver/users.csv
test -s data/homeserver/tokens.csv
test -s data/homeserver/rooms.json
```

Quando os três arquivos existirem, execute o
[smoke test do runbook](../experiments/runbook.md#smoke-test-sem-prometheus).
Os demais comandos de carga ficam no mesmo documento; o desenho e a análise
estatística estão no [desenho experimental](../experiments/design.md).

## Recomeçar ou manter múltiplas massas

Não é necessário apagar a massa atual. Escolha outro diretório:

```console
MATRIX_DATA_DIR=data/homeserver-500 setup_homeserver/run_all.sh 500
```

Para realmente recomeçar, remova somente o dataset desejado e execute o
pipeline novamente. Nunca apague toda a raiz do projeto.

## Problemas comuns

Consulte [troubleshooting.md](../troubleshooting.md#setup-de-usuários).

## Segurança

`users.csv` e `tokens.csv` são sensíveis e estão ignorados pelo Git. Use
somente contas descartáveis e mantenha `setup_homeserver/.env` fora do controle
de versão.
