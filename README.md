# Matrix Locust

Gerador de carga para homeservers Matrix/Synapse baseado em Locust. O projeto
prepara usuários e salas, executa experimentos fatoriais reproduzíveis, coleta
métricas do Locust e do Prometheus e produz gráficos, ANOVA e Tukey HSD.

## Escolha o fluxo

| Objetivo | Guia |
|---|---|
| Preparar e testar um único Synapse | [Setup de homeserver](setup_homeserver/README.md) |
| Preparar salas entre dois ou mais homeservers | [Setup de federação](setup_federation/README.md) |
| Executar o fatorial 3×2 e analisar resultados | [Experimentos](experiments/README.md) |
| Validar o ambiente e executar a campanha recomendada | [Runbook de testes](experiments/TESTING.md) |
| Consultar origem e unidade das métricas | [Catálogo de métricas](experiments/METRICS.md) |

## Visão geral

```text
setup_homeserver/ ou setup_federation/
                  │
                  ▼
             data/<cenário>/
                  │
                  ▼
       experiments/run_factorial.py
          ├── Locust: RPS e tempo de resposta
          ├── Prometheus: Synapse e processo
          └── 31 amostras por execução
                  │
                  ▼
              results/
          ├── CSVs e HTML brutos
          ├── resumos por execução
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
- acesso ao Prometheus que coleta os workers Synapse desejados; os jobs são
  descobertos dinamicamente pelas métricas disponíveis.

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

2. Coloque pelo menos um JPG em `images/` para o workload com imagens.

3. Configure o Prometheus com scrape de 2 s, conforme o
   [catálogo de métricas](experiments/METRICS.md#configuração-do-scrape-interval).

4. Execute a campanha:

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.example.com \
  --prometheus-url http://172.27.176.1:9091 \
  --data-dir data/homeserver \
  --repetitions 3
```

O padrão usa carga controlada: 0,2 ação/s por usuário, 15% de imagens no
cenário misto, mensagens variáveis de 10 palavras, três repetições e cooldown
de 60 s. O tamanho fixo mantém o fatorial comparável; os perfis `short`,
`mixed` e `long` estão documentados no [guia de experimentos](experiments/README.md#ritmo-das-ações).

Durante a campanha, o terminal informa célula e fase atuais, percentual,
tempo decorrido e ETA a cada 10 segundos. Esse histórico é salvo em
`results/campaign.log`; consulte a seção
[Acompanhar a execução](experiments/README.md#acompanhar-a-execução) para
personalizar o intervalo.

O `/sync` usa long polling de 30 s por padrão. Seu tempo de resposta é coletado
separadamente: os resultados preservam o agregado bruto com `/sync`, oferecem
um agregado de primeiro plano sem `/sync` e métricas próprias para texto,
imagem e upload.

## Modos de carga

### Carga controlada

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.example.com \
  --message-rate 0.2 \
  --image-ratio 0.15
```

Os intervalos entre ações são aleatórios, com distribuição exponencial e média
de cinco segundos por usuário. No workload misto, 85% das ações são texto e
15% são imagem.

### Capacidade máxima

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.example.com \
  --max-throughput
```

Nesse modo não existe espera entre ações: cada usuário inicia a próxima assim
que a anterior termina. Use uma pasta de resultados diferente para não misturar
campanhas com modelos de carga distintos:

```console
--output-dir results/max-throughput
```

## Execução direta do Locust

Para exploração manual, sem o fatorial:

```console
MATRIX_DATA_DIR=data/homeserver \
  poetry run python run.py locust-run-users.py \
  --host https://matrix-test.example.com
```

O runner fatorial é recomendado para resultados científicos porque registra
configuração, dataset, timestamps, métricas e ordem randomizada.

## Reprodutibilidade estatística

Cada execução coleta 31 pontos na janela estacionária de 120 s. Esses pontos
são correlacionados e são resumidos em uma média por execução. ANOVA, IC 95% e
Tukey usam as execuções independentes, não os 31 pontos como réplicas.

```text
31 amostras → 1 resumo de execução
3 repetições × 6 células → 18 unidades experimentais
```

Use `--repetitions 5` quando precisar de maior poder estatístico. Com cooldown
de 60 s, cinco repetições duram aproximadamente 2 h 11 min 30 s.

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
data/                       datasets locais ignorados pelo Git
results/                    resultados ignorados pelo Git
test-suites/                suítes legadas do runner original
```

## Solução rápida de problemas

- `locust not found`: execute `poetry install` e use `poetry run`.
- `No JPG test images`: adicione um arquivo `images/*.jpg` ou rode apenas
  `--workloads text_only`.
- HTTP 429: ajuste os rate limits do Synapse ou reduza a carga.
- Métrica vazia: confirme `instance`, labels, targets e scrape do Prometheus.
- Carga solicitada não atingida: examine `locust.log` e erros de login/sync.
- Poucos valores do Prometheus: confirme `count_over_time(up[2m])`; para
  scrape de 2 s o resultado esperado é aproximadamente 60.
