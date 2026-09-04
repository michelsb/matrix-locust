# Métricas, origem e análise estatística

Cada célula gera `samples.csv`; ao fim da campanha, `all_samples.csv` reúne as
seis combinações fatoriais. As consultas PromQL efetivamente usadas também são
gravadas no `metadata.json` de cada célula.

Navegação: [experimentos](design.md) · [runbook](runbook.md) ·
[carga federada](federation.md) · [solução de problemas](../troubleshooting.md)

## Auditoria do workload

Além das métricas temporais, cada célula grava `workload_stats.json` diretamente
a partir das tarefas executadas pelo usuário Locust:

| Campo | Origem | Significado |
|---|---|---|
| `text.attempts` | tarefa `send_text` | Envios de texto tentados |
| `text.successes` / `failures` | resposta de `room_send` | Resultado dos envios de texto |
| `text.words` / `body_bytes` | corpo `m.text` gerado | Volume textual oferecido |
| `text.word_count_histogram` | gerador de mensagens | Distribuição exata do número de palavras |
| `image.attempts` | tarefa `send_image` | Ações de imagem tentadas |
| `image.upload_successes` | resposta do Media API | Uploads concluídos |
| `image.send_successes` | resposta de `room_send` | Eventos `m.image` concluídos |
| `image.uploaded_bytes` | arquivo JPG enviado | Volume total de mídia carregado |
| `image.files` | seleção aleatória de `images/*.jpg` | Frequência de cada imagem |

Esses contadores cobrem a célula inteira. Eles servem para confirmar que o
workload produzido foi comparável entre repetições; não substituem as métricas
de `samples.csv`, limitadas à janela experimental.

## Métricas do Locust

| Coluna | Origem | Unidade | Significado |
|---|---|---:|---|
| `rps` | `locust_stats_history.csv` | req/s | Requisições concluídas por segundo pelo gerador |
| `failures_per_second` | Locust | falhas/s | Respostas marcadas como falha pelo Locust |
| `avg_response_time_ms` | Locust | ms | Tempo de resposta médio agregado de todos os endpoints |
| `median_response_time_ms` | Locust | ms | Mediana agregada do tempo de resposta |
| `p95_response_time_ms` | Locust | ms | Percentil 95 do tempo de resposta agregado |
| `foreground_rps` | histórico por endpoint do Locust | req/s | Soma de todos os endpoints exceto `/sync` |
| `foreground_failures_per_second` | histórico por endpoint do Locust | falhas/s | Falhas totais exceto `/sync` |
| `foreground_avg_response_time_ms` | histórico por endpoint do Locust | ms | Média ponderada pelo RPS, excluindo `/sync` |
| `sync_{rps,avg_response_time_ms,p95_response_time_ms}` | `/_matrix/client/v3/sync` | req/s ou ms | Long polling isolado |
| `text_send_{rps,avg_response_time_ms,p95_response_time_ms}` | envio de `m.text` | req/s ou ms | Evento textual |
| `image_send_{rps,avg_response_time_ms,p95_response_time_ms}` | envio de `m.image` | req/s ou ms | Evento que referencia a mídia |
| `media_upload_{rps,avg_response_time_ms,p95_response_time_ms}` | `/_matrix/media/v3/upload` | req/s ou ms | Upload do arquivo JPG |

Falhas HTTP são mantidas como observações da execução e não fazem o runner
descartar a célula. Os detalhes ficam em `locust_failures.csv`; as taxas ficam
nas métricas `failures_per_second` e `foreground_failures_per_second`.

As colunas agregadas originais incluem `/sync` e são preservadas para mostrar
o custo total observado pelo Locust. A coluna `foreground_avg_response_time_ms` é o
tempo de resposta médio solicitado sem `/sync`. Um p95 combinado sem `/sync` não é
estimado, pois não é possível combinar corretamente percentis já agregados;
use os p95 específicos de cada endpoint.

## Métricas de processo e Synapse

Todas são consultadas no Prometheus para o label `instance` passado ao
executor. `WINDOW` corresponde a `--cpu-rate-window`, 30 s por padrão.
Quando a campanha usa `--no-prometheus`, estas colunas não são criadas; somente
as métricas do Locust são analisadas.

| Coluna | Métrica de origem | Agregação/unidade | Interpretação |
|---|---|---|---|
| `cpu_total_percent` | `process_cpu_seconds_total` | soma de workers, % de um núcleo | 250% equivale a 2,5 núcleos |
| `cpu_<job>_percent` | mesma | por `job`, % de um núcleo | CPU de cada worker Synapse |
| `memory_total_mib` | `process_resident_memory_bytes` | soma, MiB | Memória residente dos workers |
| `synapse_rps` | `synapse_http_server_response_count_total` | rate/s, soma | Respostas vistas internamente pelo Synapse |
| `synapse_http_errors_per_second` | `synapse_http_server_response_time_seconds_count` | códigos 4xx/5xx por segundo | Erros HTTP internos |
| `synapse_http_p95_ms` | `synapse_http_server_response_time_seconds_bucket` | p95, ms | Tempo de resposta HTTP interno |
| `reactor_tick_p95_ms` | `python_twisted_reactor_tick_time_bucket` | p95, ms | Atraso do event loop Twisted |
| `db_query_p95_ms` | `synapse_storage_query_time_bucket` | p95, ms | Tempo das queries SQL |
| `db_schedule_p95_ms` | `synapse_storage_schedule_time_bucket` | p95, ms | Espera para executar trabalho no banco |
| `events_persisted_per_second` | `synapse_storage_events_persisted_events_total` | rate/s, soma | Eventos efetivamente persistidos |
| `db_threadpool_utilization_percent` | `synapse_threadpool_{working,total}_threads` | maior utilização, % | Saturação do pool `database-master` |
| `replication_events_queue` | `synapse_replication_tcp_command_queue` | máximo | Backlog do stream `events` |
| `notifier_users` | `synapse_notifier_users` | soma | Usuários/listeners aguardando notificações |
| `open_fds_percent` | `process_{open,max}_fds` | maior utilização, % | Pressão sobre descritores de arquivo |
| `gc_time_ms_per_second` | `python_gc_time_sum` | rate, ms/s | Tempo consumido pelo garbage collector |

Histogramas são convertidos em p95 com `histogram_quantile(0.95, ...)`. Os
contadores são convertidos em taxas com `rate(...[WINDOW])`. Métricas ausentes
produzem coluna vazia e um warning no log; CPU ausente invalida a célula.

### Descoberta dinâmica dos workers

O executor não mantém uma lista de IPs ou nomes de workers. A consulta de CPU
filtra somente por `instance` e agrega todos os labels `job` devolvidos:

```promql
sum by (job) (
  rate(process_cpu_seconds_total{instance="matrix-test.atlab.ufc.br"}[30s])
) * 100
```

Cada job observado vira uma coluna `cpu_<job>_percent`, enquanto
`cpu_total_percent` soma todos eles. A lista encontrada em cada célula é
registrada em `metadata.json` como `observed_prometheus_jobs`. Se a topologia
mudar entre execuções, `all_samples.csv` preserva a união das colunas e deixa
vazias aquelas que não existiam em determinada célula.

O analisador também descobre essas colunas dinamicamente e gera, para cada job
com dados suficientes, gráfico, IC 95%, ANOVA e Tukey pelos mesmos critérios
usados nas métricas fixas.

Um target presente no Prometheus mas sem `process_cpu_seconds_total`, como um
Redis fora do escopo, não é incluído e não causa falha. A célula só é
invalidada quando nenhuma série de CPU é encontrada para a `instance`.

## Configuração do scrape interval

As 31 observações cobrem 120 s, inclusive os extremos, logo estão separadas por
4 s. O ambiente atual já coleta aproximadamente a cada 2 s. Para reproduzir
essa configuração, ajuste o bloco `scrape_config` que contém os targets
Synapse:

```yaml
scrape_configs:
  - job_name: synapse-workers
    scrape_interval: 2s
    scrape_timeout: 1s
    metrics_path: /_synapse/metrics
    static_configs:
      - targets: ["10.152.183.177:9000"]
        labels:
          instance: matrix-test.atlab.ufc.br
          job: appservice
      # ...demais workers...
```

`scrape_interval` e `scrape_timeout` ficam no mesmo nível de `job_name`, não
dentro de `static_configs`. Depois de validar o YAML, recarregue ou reinicie o
Prometheus. Confirme a resolução após dois minutos:

```promql
count_over_time(up{instance="matrix-test.atlab.ufc.br"}[2m])
```

Com 2 s, o resultado esperado é aproximadamente 60; com 4 s, aproximadamente
30. O custo de scrape aumenta para todos os targets desse bloco, portanto essa
resolução deve ser aplicada ao job experimental, não necessariamente a toda a
infraestrutura.

## Gráficos, IC e ANOVA

Ao final do fatorial, o executor cria `results/analysis/`:

```text
analysis/
├── plots/<metrica>.png
├── plots/cpu_workers_stacked.png
├── plots/tukey/tukey_<metrica>.png
├── tukey/tukey_<metrica>.csv
├── run_summaries.csv
├── cpu_workers_by_scenario.csv
├── confidence_intervals.csv
├── anova_<metrica>.csv
├── anova_coefficients_ci95.csv
├── skipped_inference.csv
└── STATISTICAL_WARNING.txt
```

Cada execução mantém suas 31 observações brutas, que são reduzidas a uma média
em `run_summaries.csv`. Cada gráfico mostra carga no eixo X, uma linha por
workload e a média dessas execuções independentes com IC 95% de Student. A
ANOVA tipo II usa o modelo:

`plots/cpu_workers_stacked.png` apresenta uma barra por cenário fatorial,
dividida entre todos os jobs descobertos dinamicamente no Prometheus. A altura
da barra representa a CPU total dos workers, em porcentagem de um núcleo, e a
haste preta mostra o IC 95% da CPU total entre execuções independentes. Os
valores usados no gráfico, inclusive a participação percentual de cada worker,
ficam em `cpu_workers_by_scenario.csv`.

```text
resposta ~ carga + workload + carga:workload
```

ANOVA, IC e Tukey HSD usam somente as médias por execução. O Tukey compara as
seis células (`carga / workload`), grava os 15 pares com p-valor ajustado e
produz o gráfico de intervalos simultâneos. São necessárias pelo menos duas
repetições por célula; o padrão é 3 e pode ser alterado com `--repetitions`.

Métricas constantes, sem cobertura dos dois fatores ou sem repetição suficiente
continuam aparecendo nos resumos e gráficos descritivos, mas são omitidas de
ANOVA e Tukey. O motivo fica registrado em `skipped_inference.csv`; isso evita
testes degenerados e warnings de matriz de covariância sem posto completo.

Entre células, `--cooldown` aguarda 60 s por padrão. Para cinco repetições, são
30 células: aproximadamente 102,5 min de execução mais 29 min de cooldown,
totalizando cerca de **131,5 min (2 h 11 min 30 s)**. Com cooldown de 120 s, o
total seria aproximadamente 2 h 40 min 30 s.
