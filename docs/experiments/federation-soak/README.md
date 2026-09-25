# Soak test federado de 8 horas

Este guia executa uma carga baixa e contínua nos homeservers `home01` e
`home02`. Os dois processos Locust são iniciados em paralelo pelo launcher
[`run_federation_soak.sh`](../../../experiments/run_federation_soak.sh).

> Embora possa ser usado como uma validação prolongada, este é tecnicamente um
> **soak test**: o objetivo é detectar degradação, vazamentos e acúmulo de filas
> ao longo do tempo. Para uma verificação rápida, use o smoke test do
> [runbook](../runbook.md).

## Perfil padrão

| Configuração | Por homeserver | Total da federação |
|---|---:|---:|
| Usuários simultâneos | 10 | 20 |
| Ações foreground | 0,5/s em média | 1/s em média |
| Proporção de imagens | 15% | 15% |
| Duração da carga | 8 horas | 8 horas em paralelo |
| Amostras | 480 | 960, armazenadas separadamente |

O workload `text_and_image` combina texto e imagens na mesma célula. Cada
usuário produz em média uma ação foreground a cada 20 segundos. O `/sync`
continua ativo e gera requisições adicionais; portanto, o RPS HTTP total não é
limitado a uma requisição por segundo.

O teste possui uma única execução por lado. Ele é apropriado para analisar
séries temporais, mas não constitui um conjunto de repetições independentes
para ANOVA ou Tukey.

## 1. Pré-requisitos

Execute os comandos a partir da raiz do projeto:

```console
poetry install
poetry run locust --version
```

Confira as URLs no arquivo local `setup_federation/.env`:

```ini
FED_HOME01_URL=https://home01-dev.ac.atlab.ufc.br
FED_HOME01_DOMAIN=home01-dev.ac.atlab.ufc.br
FED_HOME02_URL=https://home02-dev.ac.atlab.ufc.br
FED_HOME02_DOMAIN=home02-dev.ac.atlab.ufc.br
```

Valide os datasets, os tokens e as imagens:

```console
poetry run python setup_federation/07_verify_federation.py
test -s data/federation/exports/home01/users.csv
test -s data/federation/exports/home01/tokens.csv
test -s data/federation/exports/home02/users.csv
test -s data/federation/exports/home02/tokens.csv
ls images/*.jpg
bash -n experiments/run_federation_soak.sh
```

Cada export deve possuir pelo menos dez usuários. Para contar sem incluir o
cabeçalho:

```console
echo "home01: $(($(wc -l < data/federation/exports/home01/users.csv) - 1))"
echo "home02: $(($(wc -l < data/federation/exports/home02/users.csv) - 1))"
```

## 2. Executar sem Prometheus

Esta é a opção mais simples e elimina falhas causadas por indisponibilidade do
servidor de métricas. RPS, falhas e latências do Locust continuam sendo
coletados.

```console
bash experiments/run_federation_soak.sh
```

Não é necessário definir `COLLECT_PROMETHEUS=false`: esse já é o padrão.

## 3. Executar com Prometheus

Use esta opção para acrescentar CPU, memória e métricas internas do Synapse às
amostras. O launcher pressupõe que um Prometheus central contém as séries dos
dois homeservers.

Primeiro, confirme que o Prometheus responde:

```console
curl -fsS http://172.27.176.1:9091/-/ready
```

Depois, descubra os valores reais do label `instance`:

```console
curl -fsSG http://172.27.176.1:9091/api/v1/query \
  --data-urlencode 'query=count by (instance) (process_cpu_seconds_total)'
```

Os valores fornecidos em `HOME01_PROMETHEUS_INSTANCE` e
`HOME02_PROMETHEUS_INSTANCE` precisam coincidir exatamente com os labels
retornados. Para executar:

```console
COLLECT_PROMETHEUS=true \
PROMETHEUS_URL=http://172.27.176.1:9091 \
HOME01_PROMETHEUS_INSTANCE=home01-dev.ac.atlab.ufc.br \
HOME02_PROMETHEUS_INSTANCE=home02-dev.ac.atlab.ufc.br \
bash experiments/run_federation_soak.sh
```

Se o label `instance` for igual ao `FED_HOME01_DOMAIN` ou
`FED_HOME02_DOMAIN`, as duas variáveis `*_PROMETHEUS_INSTANCE` podem ser
omitidas:

```console
COLLECT_PROMETHEUS=true \
PROMETHEUS_URL=http://172.27.176.1:9091 \
bash experiments/run_federation_soak.sh
```

O catálogo completo, com origem e unidade de cada série, está em
[Métricas](../metrics.md).

## 4. Acompanhar a execução

O stdout dos dois processos aparecerá intercalado no terminal. Cada lado
também mantém seu próprio log:

```text
results/federation-soak-8h/<data-hora>/home01/campaign.log
results/federation-soak-8h/<data-hora>/home02/campaign.log
```

Em outro terminal, localize e acompanhe os logs mais recentes:

```console
run_dir=$(find results/federation-soak-8h -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)
tail -f "$run_dir/home01/campaign.log" "$run_dir/home02/campaign.log"
```

Use `Ctrl+C` no terminal do launcher para encerrar os dois processos. Uma
interrupção antecipada não produz uma janela experimental completa.

## 5. Resultados

Cada execução cria diretórios independentes:

```text
results/federation-soak-8h/<data-hora>/
├── home01/
│   ├── campaign.log
│   ├── all_samples.csv
│   ├── analysis/
│   └── users-10__text_and_image__rep-01/
└── home02/
    ├── campaign.log
    ├── all_samples.csv
    ├── analysis/
    └── users-10__text_and_image__rep-01/
```

Com Prometheus, os CSVs incluem as métricas remotas. Sem Prometheus, incluem
somente as métricas derivadas do Locust. Os diretórios home01 e home02 são dois
lados da mesma execução federada, e não duas repetições estatísticas.

## 6. Alterar a carga

Os padrões podem ser sobrescritos por variáveis de ambiente. Por exemplo,
cinco usuários por lado e uma ação a cada 30 segundos por usuário:

```console
USERS_PER_HOMESERVER=5 \
MESSAGE_RATE=0.033333 \
bash experiments/run_federation_soak.sh
```

Variáveis disponíveis:

| Variável | Padrão | Finalidade |
|---|---:|---|
| `USERS_PER_HOMESERVER` | `10` | Usuários simultâneos em cada lado |
| `MESSAGE_RATE` | `0.05` | Ações foreground/s por usuário |
| `IMAGE_RATIO` | `0.15` | Fração das ações que enviam imagem |
| `SPAWN_RATE` | `2` | Usuários iniciados por segundo |
| `TOTAL_SECONDS` | `28800` | Duração total da emissão de carga |
| `STABILIZATION_SECONDS` | `60` | Período inicial não medido |
| `COLLECTION_BUFFER_SECONDS` | `5` | Cauda para concluir a coleta |
| `SAMPLES` | `480` | Amostras por homeserver |
| `OUTPUT_ROOT` | gerado automaticamente | Diretório comum dos dois lados |
| `RUN_ID` | data e hora | Identificador da execução |

A etapa final de coleta e análise pode acrescentar alguns segundos ao tempo de
relógio. A emissão de carga permanece limitada ao valor de `TOTAL_SECONDS`.

## Solução de problemas

- **Um homeserver falhou:** consulte o `locust.log` dentro da célula daquele
  lado e o [guia de diagnóstico](../../troubleshooting.md).
- **Prometheus não encontra séries:** confira `/api/v1/query`, os labels
  `instance` e se o intervalo de retenção cobre as oito horas.
- **Poucos usuários:** diminua `USERS_PER_HOMESERVER` ou refaça o setup.
- **Erros 429/5xx:** reduza `MESSAGE_RATE` e confira os rate limits do ambiente
  de testes.
- **Execução em notebook remoto:** use `tmux` ou `screen` para que o processo
  sobreviva ao fechamento do terminal.

Para entender a arquitetura com dois processos e outras cargas federadas,
consulte [Executar carga federada](../federation.md).
