# Coleta de tracing com Jaeger

O executor pode consultar os spans OpenTracing emitidos pelo Synapse e
armazenados no Jaeger. A coleta é opcional e usa exatamente a mesma janela de
medição aplicada às métricas do Locust e do Prometheus.

Navegação: [experimentos](design.md) · [runbook](runbook.md) ·
[catálogo de métricas](metrics.md) · [solução de problemas](../troubleshooting.md)

## Escolha do modo de coleta

| Modo | Quando usar | Configuração |
|---|---|---|
| Semântico, recomendado | Medir as latências internas definidas pelo projeto | Informe somente `--jaeger-metrics`; serviços e operações são planejados automaticamente |
| Genérico, avançado | Explorar spans ainda não modelados | Informe `--jaeger-operations` e, opcionalmente, `--jaeger-services` |

Os modos podem ser combinados, mas isso aumenta o volume consultado. Antes da
primeira célula, o log `jaeger-plan` mostra os serviços e operações que serão
consultados.

## Janela experimental

```text
ramp-up        estabilização          medição           buffer
──────────┬──────────────────┬──────────────────────┬──────────
          │ não medir        │ spans aceitos        │ não medir
                             ↑                      ↑
                       measure_start          measure_end
```

O Jaeger é consultado com uma margem de 30 segundos antes e depois da janela
para localizar traces que cruzem suas bordas. Após o download, somente spans
que satisfaçam `measure_start <= início do span < measure_end` são preservados.
A margem não amplia a amostra.

O buffer de coleta mantém o Locust vivo depois da medição;
`--jaeger-flush-wait` garante uma espera mínima após seu encerramento para que
spans pendentes apareçam no backend.

## Pré-requisitos

Antes de executar a carga, aplique o
[setup de tracing do Synapse e do NGINX](../setup/jaeger-tracing.md). Esse guia
documenta portas, sampling, propagação W3C, atributos do proxy e validação.

Confirme a API Jaeger Query, removendo qualquer vírgula final da URL:

```console
curl -fsS http://10.101.53.46:16686/api/services
```

No ambiente verificado, os serviços usam nomes como `matrix-test.atlab.ufc.br
master`, `generic`, `events_persister`, `federation_sender`, `media_repository`
e `stream_writer`. Liste as operações reais de um serviço com:

```console
curl -fsSG http://10.101.53.46:16686/api/operations \
  --data-urlencode 'service=matrix-test.atlab.ufc.br master'
```

O Synapse precisa estar com OpenTracing habilitado e a retenção do backend deve
cobrir toda a execução.

Ao solicitar `event_persister_processing_latency`, o executor descobre
automaticamente os serviços e operações necessários e calcula T6 como uma
diferença de médias dentro de cada repetição. Não informe serviços ou operações
manualmente apenas para essa métrica. Se o sampling probabilístico for 10%, use
`--jaeger-sampling-rate 0.1` para registrar essa configuração nos metadados; a
opção é declarativa e não reconfigura o Synapse ou o NGINX.

Para métricas do proxy, o serviço OpenTelemetry do NGINX deve chamar-se
`matrix-nginx`, a operação deve chamar-se `matrix-client-proxy` e os spans devem
expor `http.target`, `http.status_code`, `nginx.request_time`,
`nginx.upstream_connect_time`, `nginx.upstream_header_time`,
`nginx.upstream_response_time` e os atributos de upstream. NGINX e Synapse são
amostrados e analisados independentemente; o
coletor não exige propagação de contexto entre eles.

A porta `16686` expõe a API JSON utilizada pela interface web do Jaeger. Essa
API é adequada à versão validada neste ambiente, mas é considerada interna pelo
Jaeger 1.x; uma atualização do backend deve ser acompanhada por um novo smoke
test do coletor.

## Coleta genérica de spans

Exemplo fatorial com cinco repetições independentes:

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --prometheus-url http://172.27.176.1:9091 \
  --instance matrix-test.atlab.ufc.br \
  --jaeger-url http://10.101.53.46:16686 \
  --jaeger-service-prefix matrix-test.atlab.ufc.br \
  --jaeger-operations db.query db.txn db.connection \
  --loads 50 100 150 \
  --workloads text_only text_and_image \
  --repetitions 5 \
  --stabilization 60 \
  --measurement-duration 120 \
  --samples 31 \
  --collection-buffer 5 \
  --output-dir results/factorial-with-jaeger
```

O Jaeger permanece desabilitado quando `--jaeger-url` não é informado. Use
`--no-jaeger` para desabilitá-lo explicitamente em um comando reutilizado.

### Smoke test genérico do coletor

Para validar a integração, limite inicialmente os serviços e operações. Isso
evita consultar todas as atividades de background do deployment:

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --data-dir data/homeserver \
  --loads 50 \
  --workloads text_only \
  --spawn-rate 5 \
  --message-rate 0.2 \
  --repetitions 1 \
  --stabilization 10 \
  --measurement-duration 20 \
  --samples 6 \
  --collection-buffer 5 \
  --cooldown 0 \
  --no-prometheus \
  --jaeger-url http://10.101.53.46:16686 \
  --jaeger-service-prefix matrix-test.atlab.ufc.br \
  --jaeger-services \
    "matrix-test.atlab.ufc.br generic" \
    "matrix-test.atlab.ufc.br events_persister" \
  --jaeger-operations db.query db.txn \
  --jaeger-query-padding 5 \
  --jaeger-workers 4 \
  --output-dir results/smoke-jaeger-50-text-v2
```

Depois de validar o custo das consultas, amplie os serviços, operações e a
margem para a campanha definitiva.

## Parâmetros

| Opção | Padrão | Finalidade |
|---|---:|---|
| `--jaeger-url` | vazio | Ativa a coleta e define a URL do Jaeger Query |
| `--jaeger-service-prefix` | valor de `--instance` | Descobre dinamicamente os serviços Synapse |
| `--jaeger-services` | todos descobertos | Limita a coleta a nomes exatos de serviços |
| `--jaeger-metrics` | vazio | Métricas semânticas; serviços e operações são automáticos |
| `--jaeger-operations` | contextual | Spans genéricos adicionais; não é necessário com `--jaeger-metrics` |
| `--jaeger-query-padding` | `30` | Margem de consulta em segundos |
| `--jaeger-chunk-duration` | `300` | Tamanho de cada consulta temporal |
| `--jaeger-query-limit` | `1000` | Máximo de traces por consulta |
| `--jaeger-min-chunk-duration` | `1` | Menor subdivisão automática, em segundos |
| `--jaeger-workers` | `4` | Consultas simultâneas ao Jaeger |
| `--jaeger-timeout` | `30` | Timeout HTTP em segundos |
| `--jaeger-retries` | `3` | Novas tentativas por consulta |
| `--jaeger-flush-wait` | `5` | Espera mínima após o Locust |
| `--jaeger-include-db-statements` | desativado | Persiste a tag sensível `db.statement` |
| `--no-jaeger` | desativado | Força a execução sem Jaeger |

Se uma consulta atingir o limite, o executor divide automaticamente seu
intervalo ao meio, repetindo o processo até obter respostas completas ou chegar
a `--jaeger-min-chunk-duration`. Um aviso só permanece quando até o menor
intervalo está saturado. Os spans são deduplicados por `trace_id` e `span_id`.

## Artefatos

Cada célula cria:

```text
users-50__text_only__rep-01/jaeger/
├── spans.csv
├── samples.csv
├── repetition_summary.csv
├── derived_observations.csv
├── derived_samples.csv
├── derived_repetition_summary.csv
└── collection_metadata.json
```

- `spans.csv`: um registro por span selecionado, incluindo endpoint, HTTP e os
  tempos/upstream específicos do NGINX;
- `samples.csv`: contagem, média, mediana, p95, p99 e máximo por ponto temporal,
  serviço e operação;
- `repetition_summary.csv`: um resumo por serviço/operação para aquela execução;
- `collection_metadata.json`: janela, parâmetros, consultas, saturações e erros.

Na raiz da campanha, `collection_status.csv` deixa explícitas células parciais
e `execution_provenance.json` registra argumentos, versões, plataforma e commit.
Uma célula com falha no Jaeger não é considerada completa por
`--skip-existing`.

Os resumos possuem `scope=all`, uma visão por endpoint e
`scope=foreground`, que agrega envios de mensagem e uploads sem `/sync`. A
análise cria gráficos e tabelas separados para cada escopo; os arquivos sem
sufixo preservam a visão `all`, enquanto `_foreground`, `_sync` e os demais
sufixos identificam as visões filtradas.

No nível da campanha são gerados:

```text
all_jaeger_samples.csv
all_jaeger_repetition_summaries.csv
analysis/jaeger/
├── confidence_intervals.csv
├── all_metrics_by_scenario.csv
├── anova/
├── tukey/
├── plots/all_metrics_mean_ms.png
├── plots/all_metrics_p95_ms.png
├── plots/all_metrics_normalized_mean.png
└── skipped_inference.csv
```

Os gráficos `all_metrics_*` reúnem todas as operações coletadas. As versões
absolutas usam escala logarítmica em milissegundos e IC 95%, evitando que
métricas pequenas desapareçam ao lado das maiores. A versão normalizada divide
cada média pelo valor médio daquela própria métrica em todos os cenários; ela
serve para comparar variações relativas, não latências absolutas. Os números
usados nos três gráficos ficam em `all_metrics_by_scenario.csv`.

Todos os tempos são convertidos de microssegundos para milissegundos.
Intervalos sem spans ficam ausentes, não são preenchidos com latência zero.

## Unidade estatística

Spans e amostras temporais de uma célula são correlacionados e não são tratados
como repetições. A análise usa um valor de `repetition_summary.csv` por execução,
serviço, operação e métrica. Os gráficos temporais usam as amostras para revelar
picos; intervalos de confiança, ANOVA e Tukey usam os resumos por repetição.

## Métricas semânticas

O executor também calcula métricas que dependem de tags ou do pareamento de
spans dentro do mesmo trace. O
[catálogo canônico](metrics.md#metricas-semanticas-do-jaeger) documenta seus
nomes, fórmulas, unidades e significado.

Nesta instalação, o span equivalente ao nome conceitual
`outgoing-client-request` chama-se `outgoing-replication-request`, possui
`span.kind=client` e é pareado ao servlet pelo pai causal compartilhado.

Ative somente as métricas desejadas. Não informe `--jaeger-services` nem
`--jaeger-operations`: o plano mínimo é calculado automaticamente a partir do
catálogo acima.

```console
--jaeger-metrics \
  request_authentication_latency \
  generic_worker_processing_latency \
  postgresql_session_verification_latency \
  replication_dispatch_latency \
  event_persister_pre_transaction_latency \
  event_persister_processing_latency \
  event_persistence_transaction_latency \
  nginx_total_request_latency \
  nginx_worker_connection_latency \
  nginx_upstream_first_byte_latency \
  nginx_upstream_processing_latency \
  nginx_upstream_response_transfer_latency \
  nginx_proxy_client_overhead
```

As observações ficam em `derived_observations.csv`.
Não há coleta automática de `matrix-experiment.json`: todas as métricas NGINX
documentadas aqui são obtidas diretamente dos spans do Jaeger. T1 é sempre
medido pelo Locust sobre todas as tentativas de `room_send`, fora do sampling do
Jaeger, conforme o [catálogo de métricas](metrics.md#t1--intervalo-entre-chegadas-de-novas-mensagens).
O access log pode ser mantido apenas para auditoria operacional.
`replication_dispatch_latency` atravessa os relógios dos workers `generic` e
`events_persister`; uma observação só entra na média quando obedece
`0 <= valor <= duration(outgoing-replication-request)`. Violações são
marcadas como `valid=false` e `clock skew` em vez de contaminar o resultado.
Sincronize os nós com NTP/chrony para obter essa métrica de forma confiável.

As métricas gerais `nginx_total_request_latency`,
`nginx_worker_connection_latency`, `nginx_upstream_first_byte_latency`,
`nginx_upstream_processing_latency`, `nginx_upstream_response_transfer_latency`
e `nginx_proxy_client_overhead` são calculadas para
todos os endpoints com resposta 2xx e publicadas nos escopos `all`, `sync`,
`foreground` e por endpoint. Elas usam somente durações e atributos do próprio
NGINX. O sampling do proxy é independente do sampler do Synapse e fica na
configuração descrita no [guia de setup](../setup/jaeger-tracing.md).

### Relatório de qualidade

O runner gera `jaeger/data_quality.csv` e `jaeger/DATA_QUALITY.md` para cada
repetição. Ao terminar a campanha, cria `analysis/data_quality.csv` e
`EXPERIMENT_REPORT.md`. Esses arquivos mostram métricas ausentes, contagens
válidas e inválidas, cobertura T5 (consulta PostgreSQL), problemas de ordem
causal em `replication_dispatch_latency`, limitação de
resolução em T2/T3 e saturação do Jaeger. Os limites são configuráveis pelas
opções `--quality-*` descritas no [catálogo de métricas](metrics.md#relatorios-automaticos-de-qualidade).

`FAIL` é uma decisão sobre a utilização estatística da métrica afetada, não uma
exclusão do dado bruto. As observações continuam preservadas para auditoria.

### Reanalisar campanhas com a definição disjunta de T2/T3

Campanhas antigas que contenham `nginx_proxy_worker_allocation_latency` podem
ser corrigidas sem repetir a carga. O utilitário remove apenas essa métrica
composta, adota `nginx_proxy_client_overhead` como T2 e
`nginx_worker_connection_latency` como o componente observável de T3, e então
regenera qualidade, consolidados, ICs e gráficos:

```console
poetry run python experiments/reanalyze_existing_jaeger.py \
  results/<campaign-1> \
  results/<campaign-2>
```

Antes de alterar os artefatos, ele preserva cópias em
`pre-t2-t3-correction/` dentro da campanha e de cada diretório `jaeger/`. A
análise anterior fica em `analysis/jaeger-derived-pre-t2-t3-correction/`. O
script trabalha somente com os CSVs e metadados existentes; não acessa Matrix,
Jaeger ou Prometheus e não dispara nova carga.

### Experimento apenas com 50 usuários e texto

Com sampling de 10%, cinco repetições e três minutos medidos por repetição:

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --data-dir data/homeserver \
  --loads 50 \
  --workloads text_only \
  --spawn-rate 5 \
  --message-rate 0.2 \
  --repetitions 5 \
  --stabilization 60 \
  --measurement-duration 180 \
  --samples 31 \
  --collection-buffer 5 \
  --cooldown 60 \
  --no-prometheus \
  --jaeger-url http://10.101.53.46:16686 \
  --jaeger-service-prefix matrix-test.atlab.ufc.br \
  --jaeger-metrics \
    request_authentication_latency \
    generic_worker_processing_latency \
    postgresql_session_verification_latency \
    replication_dispatch_latency \
    event_persister_pre_transaction_latency \
    event_persister_processing_latency \
    event_persistence_transaction_latency \
  --jaeger-workers 4 \
  --output-dir results/internal-latency-users-50-text-only
```

`all_jaeger_derived_repetition_summaries.csv` contém uma linha por métrica e
repetição. A análise específica fica em `analysis/jaeger-derived/`. Como esse
recorte possui apenas uma carga e um workload, são gerados resultados
descritivos e intervalos de confiança entre repetições; não há desenho 3×2 para
ANOVA fatorial ou Tukey entre cenários.

## Falhas e segurança

O preflight ocorre antes da campanha. Se a URL não responder ou o prefixo não
encontrar serviços, nenhuma carga é iniciada. Se o Jaeger falhar depois que uma
célula já terminou, a falha é registrada em `collection_metadata.json`, os
resultados Locust/Prometheus são preservados e a campanha continua.

Por padrão, `db.statement` não é persistido, pois pode conter dados sensíveis e
aumentar muito os arquivos. Confira também o sampling do Synapse: sampling de
100% pode alterar o desempenho observado e pressionar o storage do Jaeger.
