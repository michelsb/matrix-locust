# Setup para federação

Este pipeline prepara usuários de dois ou mais homeservers em salas mistas.
Quando um usuário envia um evento, o Synapse precisa replicá-lo aos outros
servidores participantes.

- [README principal](../README.md)
- [Setup de um único homeserver](../setup_homeserver/README.md)
- [Execução e análise dos experimentos](../experiments/README.md)

## Como o cenário funciona

```text
usuários home01 ─┐
                 ├── sala mista ── eventos federados
usuários home02 ─┘
```

O pipeline:

1. cria usuários pertencentes a cada homeserver;
2. distribui usuários de domínios diferentes nas mesmas salas;
3. registra cada conta no servidor correto;
4. cria salas e envia convites remotos;
5. aceita os convites no homeserver de cada convidado;
6. exporta um dataset Locust por homeserver;
7. verifica se as salas realmente possuem membros federados.

O aceite do convite remoto é indispensável. Um convite pendente não significa
que o servidor remoto já participa da sala.

## Arquivos gerados

```text
data/federation/
├── users.csv
├── tokens.csv
├── rooms.json
├── rooms_status.csv
├── failed_users.txt
└── exports/
    ├── home01/
    │   ├── users.csv
    │   └── tokens.csv
    └── home02/
        ├── users.csv
        └── tokens.csv
```

Os arquivos na raiz de `data/federation/` representam todos os homeservers. Os
diretórios em `exports/` contêm somente os usuários que devem gerar carga a
partir daquele servidor. A exportação nunca sobrescreve a massa completa.

## Pré-requisitos

- Python 3.11–3.14 e Poetry 2.x;
- registro de testes habilitado nos homeservers;
- federação, DNS/delegação e certificados funcionando nos dois sentidos;
- portas server-to-server acessíveis;
- rate limits adequados ao teste.

```console
poetry install
cp setup_federation/.env.example setup_federation/.env
```

## Configuração

Exemplo mínimo:

```dotenv
FED_HOMESERVERS=home01,home02

FED_HOME01_URL=https://srv.home01.example.com
FED_HOME01_DOMAIN=home01.example.com
FED_HOME01_PREFIX=userh01

FED_HOME02_URL=https://srv.home02.example.com
FED_HOME02_DOMAIN=home02.example.com
FED_HOME02_PREFIX=userh02

MATRIX_DATA_DIR=data/federation
FED_EXPORTS_DIR=data/federation/exports
```

### URL não é necessariamente DOMAIN

| Campo | Finalidade | Exemplo |
|---|---|---|
| `URL` | endereço da API cliente | `https://srv.home01.example.com` |
| `DOMAIN` | `server_name`, usado no MXID e na federação | `home01.example.com` |
| `PREFIX` | identifica a qual servidor o usuário pertence | `userh01` |

Confirme o domínio retornado em `data/federation/tokens.csv`:

```text
@userh01.000001:home01.example.com
```

O texto depois dos dois-pontos deve corresponder a `FED_HOME01_DOMAIN`.

### Precedência da configuração

```text
variáveis do shell
    > setup_federation/.env
    > setup_homeserver/.env
    > valores padrão
```

## Execução recomendada

```console
setup_federation/run_all.sh 300
```

O total é distribuído entre os homeservers configurados. Para indicar qual
export deve ser destacado ao final:

```console
ACTIVATE=home01 setup_federation/run_all.sh 300
```

`ACTIVATE` não copia arquivos por cima da massa completa; apenas exibe o
comando Locust correspondente.

## Execução passo a passo

```console
poetry run python setup_federation/01_generate_users.py 300 --seed 42
poetry run python setup_federation/02_generate_rooms.py --seed 42
poetry run python setup_federation/03_register_users.py
poetry run python setup_federation/04_create_rooms.py
poetry run python setup_federation/05_accept_invites.py --passes 2
poetry run python setup_federation/06_export_locust_files.py --activate home01
poetry run python setup_federation/07_verify_federation.py
```

Convites federados podem demorar; aumente `--passes` se necessário.

## Executar carga de um lado

```console
MATRIX_DATA_DIR=data/federation/exports/home01 \
  poetry run python run.py locust-run-users.py \
  --host https://srv.home01.example.com
```

O `home01` recebe a carga cliente e envia eventos federados ao `home02` porque
as salas têm membros dos dois domínios.

Para usar o executor fatorial:

```console
poetry run python experiments/run_factorial.py \
  --host https://srv.home01.example.com \
  --data-dir data/federation/exports/home01 \
  --output-dir results/federation-home01
```

O executor atual controla um `--host` por campanha. Carga simultânea dos dois
lados exige duas instâncias coordenadas e deve usar diretórios de resultados
separados.

## Verificação durante ou depois do teste

```console
poetry run python setup_federation/07_verify_federation.py --check-messages
```

Verifique também em `rooms_status.csv`:

- `status` indica criação bem-sucedida;
- `federated=yes` indica membros de mais de um domínio;
- `homeservers` lista os participantes.

## Limpeza opcional

Inspecione primeiro:

```console
poetry run python setup_federation/99_cleanup_rooms.py --dry-run
```

Depois escolha explicitamente o modo documentado pelo `--help`. A limpeza pode
afetar salas remotas; nunca a execute contra ambientes fora do escopo do teste.

## Problemas comuns

### Convite remoto não chega

Confirme DNS/delegação, certificado, porta de federação, whitelist e o valor de
`DOMAIN`. Reexecute o passo 5 com mais passes.

### Usuários tentam login no servidor errado

Use o dataset exportado, não o `users.csv` completo:

```text
data/federation/exports/home01
```

### Todas as salas aparecem como não federadas

Confira prefixos únicos, domínios nos MXIDs, aceite dos convites e a saída do
passo 7 antes de iniciar medições.

## Segurança

Os `.env`, usuários, senhas e tokens são sensíveis e ignorados pelo Git. Use
homeservers e contas descartáveis e trate a limpeza de salas como operação
potencialmente destrutiva.
