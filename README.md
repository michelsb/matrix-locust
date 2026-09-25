# Matrix Locust

Gerador de carga para homeservers Matrix/Synapse baseado em Locust. O projeto
prepara usuários e salas, executa experimentos fatoriais reproduzíveis, coleta
métricas do Locust, Prometheus e, opcionalmente, Jaeger, e produz gráficos,
ANOVA e Tukey HSD.

## Escolha o fluxo

| Objetivo | Guia |
|---|---|
| Navegar por toda a documentação | [Índice](docs/README.md) |
| Preparar e testar um único Synapse | [Setup de homeserver](docs/setup/homeserver.md) |
| Preparar salas entre dois ou mais homeservers | [Setup de federação](docs/setup/federation.md) |
| Executar o fatorial 3×2 e analisar resultados | [Experimentos](docs/experiments/design.md) |
| Copiar comandos prontos de execução | [Runbook](docs/experiments/runbook.md) |
| Disparar carga federada | [Carga federada](docs/experiments/federation.md) |
| Rodar o soak test federado de 8 horas | [Soak test home01/home02](docs/experiments/federation-soak/README.md) |
| Validar detalhadamente o ambiente | [Checklist de testes](docs/experiments/validation.md) |
| Consultar origem e unidade das métricas | [Catálogo de métricas](docs/experiments/metrics.md) |
| Coletar spans internos do Synapse | [Tracing com Jaeger](docs/experiments/jaeger.md) |
| Configurar tracing no Synapse e NGINX | [Setup de Jaeger tracing](docs/setup/jaeger-tracing.md) |
| Executar em container | [Docker](docs/docker.md) |
| Diagnosticar uma falha | [Solução de problemas](docs/troubleshooting.md) |

## Visão geral

```text
setup_homeserver/ ou setup_federation/
                  │
                  ▼
             data/<cenário>/
                  │
                  ▼
       experiments/run_factorial.py
          ├── Locust: T1, RPS e tempo de resposta
          ├── Prometheus: Synapse e processo
          ├── Jaeger: spans de operações internas
          └── número configurável de amostras por execução
                  │
                  ▼
              results/
          ├── CSVs e HTML brutos
          ├── resumos por execução
          ├── relatório automático de qualidade
          ├── gráficos com IC 95%
          ├── ANOVA fatorial
          └── Tukey HSD
```

Os dados gerados ficam separados do código:

```text
data/
├── homeserver/
└── federation/

results/
└── <artefatos da campanha>
```

`data/`, `results/`, `.env` e credenciais não devem ser versionados.

## Requisitos e instalação

- Python 3.11–3.14;
- Poetry 2.x;
- acesso ao homeserver Matrix;
- acesso ao Prometheus que coleta os workers Synapse desejados, caso métricas
  remotas sejam habilitadas; os jobs são descobertos dinamicamente.

```console
git clone <url-do-repositorio>
cd matrix-locust
poetry install
poetry run locust --version
```

O Poetry cria e gerencia o ambiente virtual. Não é necessário ativar `.venv`
manualmente; use `poetry run` nos comandos Python.

## Início rápido: um homeserver

1. Configure e execute o setup:

```console
cp setup_homeserver/.env.example setup_homeserver/.env
# edite MATRIX_SERVER no .env
setup_homeserver/run_all.sh 150
```

2. Execute primeiro o smoke test sem Prometheus:

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.example.com \
  --data-dir data/homeserver \
  --loads 5 \
  --workloads text_only \
  --repetitions 1 \
  --stabilization 10 \
  --measurement-duration 20 \
  --samples 6 \
  --cooldown 0 \
  --no-prometheus \
  --output-dir results/smoke-locust-only
```

3. Escolha a campanha no [runbook](docs/experiments/runbook.md):

| Situação | Característica principal |
|---|---|
| Smoke test | Uma célula curta, sem Prometheus |
| Carga baixa | 10/25 usuários e frequência controlada |
| Fatorial recomendado | 50/100/150 usuários, cinco repetições e Prometheus |
| Locust-only | Mesmo fatorial com `--no-prometheus` |
| Stress máximo | Sem espera entre ações com `--max-throughput` |
| Stress progressivo | População e frequência controlada maiores |
| Retomada | Preserva células prontas com `--skip-existing` |

Para carga em mais de um homeserver, siga o [guia de carga federada](docs/experiments/federation.md).
O desenho estatístico e o significado dos parâmetros ficam em
[experiments/design.md](docs/experiments/design.md).

Para métricas internas nomeadas, `--jaeger-metrics` resolve automaticamente os
serviços e operações necessários, incluindo o serviço `matrix-nginx`. Os
resumos de trace separam `all`, `sync`, cada endpoint e `foreground` (sem
`/sync`). As métricas do proxy usam durações locais do NGINX e não dependem de
propagação de contexto nem da sincronização com o gerador. Consulte as fórmulas no
[catálogo de métricas](docs/experiments/metrics.md#metricas-semanticas-do-jaeger)
e a execução no [guia do Jaeger](docs/experiments/jaeger.md).
Inclua `event_persister_processing_latency` para calcular T6 automaticamente
por repetição. Ao final, valide `collection_status.csv`; argumentos, versões e
commit da campanha ficam registrados em `execution_provenance.json`.

Para o proxy NGINX, T2 e T3 são mantidos como componentes disjuntos:

```text
T2 = nginx_proxy_client_overhead
T3 = nginx_worker_connection_latency
```

T2 é um resíduo do tempo fora do upstream, não uma medição direta de fila. T3
mede somente o estabelecimento da conexão com o worker e pode ser zero quando
há keep-alive. A antiga métrica composta
`nginx_proxy_worker_allocation_latency` foi removida porque sobrepunha T2 e T3.

Cada repetição gera `jaeger/DATA_QUALITY.md`; a campanha gera
`EXPERIMENT_REPORT.md` e `analysis/data_quality.csv`. Campanhas antigas podem
ser corrigidas sem nova carga com o procedimento de
[reanálise offline](docs/experiments/jaeger.md#reanalisar-campanhas-com-a-definicao-disjunta-de-t2t3).

T1 (`message_interarrival_time_ms`) é sempre medido pelo próprio gerador entre
injeções consecutivas de `m.text` e `m.image`. Ele usa relógio monotônico, inclui
somente pares dentro da janela experimental e independe de Prometheus, Jaeger e
sincronização com os servidores.

O runner também gera intervalos auxiliares por endpoint: `all` (com `/sync`),
`foreground` (sem `/sync`), `sync`, `text_send`, `image_send`, `media_upload` e
`other`. Eles não redefinem T1; servem para caracterizar a cadência HTTP. Consulte
[Métricas](docs/experiments/metrics.md#intervalos-por-endpoint-com-e-sem-sync).

## Segurança

- Use apenas contas descartáveis de teste.
- `users.csv` contém senhas e `tokens.csv` contém access tokens.
- Não exponha `/_synapse/metrics` publicamente.
- Desabilite ou eleve rate limits somente em ambientes controlados.
- Revise o destino antes de executar `--max-throughput`.

## Estrutura relevante

```text
matrix_locust/              cliente e usuários Locust
setup_homeserver/           preparação de servidor único
setup_federation/           preparação federada
experiments/                executor, análise e documentação
docs/                       documentação centralizada
    ├── README.md           índice completo
    ├── docker.md           execução em container
    ├── troubleshooting.md diagnóstico por sintoma
    ├── setup/              preparação dos datasets
    └── experiments/        runbook, desenho, métricas e federação
data/                       datasets locais ignorados pelo Git
results/                    resultados ignorados pelo Git
```

## Solução de problemas

Consulte [docs/troubleshooting.md](docs/troubleshooting.md) para
erros do Locust, Prometheus, Synapse, setup e federação.
