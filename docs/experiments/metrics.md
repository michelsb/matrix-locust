# Métricas, origem e análise estatística

Cada célula gera `samples.csv`; ao fim da campanha, `all_samples.csv` reúne as
observações de todas as células executadas. As consultas PromQL efetivamente
usadas também são gravadas no `metadata.json` de cada célula.

Navegação: [experimentos](design.md) · [runbook](runbook.md) ·
[carga federada](federation.md) · [solução de problemas](../troubleshooting.md)

## Catálogo consolidado

| Família | Fonte | Unidade observacional | Agregação inferencial |
|---|---|---|---|
| Carga, T1 e latência externa | histórico e timestamps do Locust | requisição injetada ou ponto temporal da janela | média por execução |
| CPU e métricas internas | API HTTP do Prometheus | ponto temporal da janela | média por execução |
| Spans internos | API Jaeger Query | span ou relação causal válida | média por execução e métrica |
| Auditoria do workload | eventos do próprio gerador | contador da célula inteira | uso descritivo |

As amostras temporais e os spans de uma mesma execução são correlacionados.
Por isso, a repetição independente — e não cada linha de amostra — é a unidade
usada em intervalos de confiança, ANOVA e Tukey.

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
| `message_interarrival_time_ms` | timestamps do gerador | ms | T1: média temporal dos intervalos globais entre injeções consecutivas de mensagens |
| `request_interarrival_all_ms` | início local de cada requisição | ms | Intervalo auxiliar entre requisições de qualquer endpoint, incluindo `/sync` |
| `request_interarrival_foreground_ms` | início local de cada requisição | ms | Intervalo auxiliar entre requisições, excluindo `/sync` |
| `request_interarrival_sync_ms` | início local de `/sync` | ms | Intervalo entre inícios de ciclos de long polling |
| `request_interarrival_text_send_ms` | início local de envio `m.text` | ms | Intervalo entre requisições de texto |
| `request_interarrival_image_send_ms` | início local de envio `m.image` | ms | Intervalo entre eventos de imagem |
| `request_interarrival_media_upload_ms` | início local de upload | ms | Intervalo entre uploads de mídia |
| `request_interarrival_other_ms` | início local dos demais endpoints | ms | Intervalo entre requisições não classificadas acima |
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

### T1 — intervalo entre chegadas de novas mensagens

T1 é coletado obrigatoriamente em toda célula executada por
`run_factorial.py`. Imediatamente antes de cada `room_send` de `m.text` ou
`m.image`, o Locust registra simultaneamente `time.time_ns()` para localizar a
injeção na janela e `time.monotonic_ns()` para medir o intervalo sem sofrer com
ajustes do relógio do sistema.

Para as mensagens globais ordenadas pelo relógio do processo gerador:

```text
T1_i = monotonic_start(message_i) - monotonic_start(message_(i-1))
```

Os dois envios precisam estar dentro da janela de medição. Timestamps da
estabilização não são usados como evento anterior do primeiro T1 medido. O
valor descreve a injeção agregada de mensagens por todos os usuários do gerador,
e não o intervalo individual de um único usuário. Sua relação aproximada com a
taxa é `message injection rate ≈ 1000 / mean(T1_ms)`.

No workload com imagem, o timestamp é registrado depois que o upload termina e
imediatamente antes do envio do evento `m.image`; o upload não conta como uma
nova mensagem. Tentativas são registradas independentemente do resultado da
requisição, pois T1 caracteriza a carga oferecida, não apenas sucessos.

Cada célula produz:

```text
message_arrivals.csv
message_interarrival_observations.csv
message_interarrival_summary.csv
```

Ao final da campanha são produzidos
`all_message_interarrival_observations.csv` e
`all_message_interarrival_summaries.csv`. A média por ponto temporal também
entra em `samples.csv` como `message_interarrival_time_ms`, portanto os resumos
por execução, IC 95%, ANOVA, Tukey e gráficos são gerados automaticamente. Se o
arquivo obrigatório de chegadas estiver ausente ou não houver dois envios na
janela, a célula falha em vez de publicar um T1 vazio.

O executor padrão usa um único processo Locust. Em execução distribuída, os
relógios monotônicos de workers diferentes não podem ser ordenados diretamente;
esse modo exigiria calcular T1 por worker ou centralizar os eventos antes da
análise.

### Intervalos por endpoint, com e sem `/sync`

Além do T1 canônico, o gerador registra o instante imediatamente anterior a
cada chamada de `MatrixUser.rest()`. Para cada escopo `s`, calcula:

```text
request_interarrival_s_i = monotonic_start(request_s_i)
                         - monotonic_start(request_s_(i-1))
```

`all` inclui todos os endpoints e `foreground` inclui todos exceto `/sync`.
Os demais escopos isolam `sync`, `text_send`, `image_send`, `media_upload` e
`other`. Os pares são formados somente entre requisições do mesmo escopo e
ambos os inícios devem estar dentro da janela experimental.

Essas métricas não substituem T1. Em particular,
`request_interarrival_sync_ms` mede o intervalo entre inícios de long polls; ele
depende do tempo de resposta/timeout do `/sync` e não representa a taxa de
injeção configurada por `--message-rate`. Para a variável experimental “chegada
de novas mensagens”, use sempre `message_interarrival_time_ms`. Para avaliar o
tráfego HTTP total ou retirar a interferência do `/sync`, compare respectivamente
`request_interarrival_all_ms` e `request_interarrival_foreground_ms`.

Cada célula também produz:

```text
request_arrivals.csv
request_interarrival_observations.csv
request_interarrival_summary.csv
```

Os consolidados são `all_request_interarrival_observations.csv` e
`all_request_interarrival_summaries.csv`. As médias temporais entram em
`samples.csv`; portanto recebem os mesmos resumos, IC 95%, gráficos, ANOVA e
Tukey das demais métricas. Escopos sem ao menos duas requisições na janela são
mantidos vazios, pois não existe intervalo observável.

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

## Métricas semânticas do Jaeger

Estas métricas são calculadas a partir dos spans OpenTracing do Synapse. Todas
as durações e diferenças são publicadas em milissegundos.

| Métrica | Fórmula por observação | Origem/interpretação |
|---|---|---|
| `request_authentication_latency` | `duration(get_user_by_req)` | Autenticação da requisição no Generic Worker |
| `generic_worker_processing_latency` | `start(outgoing_replication_request) - end(get_user_by_req)` | Processamento no Generic Worker após autenticação |
| `postgresql_session_verification_latency` | `duration(db.get_user_by_access_token)` | Consulta ao PostgreSQL para validar o token; cache hits não geram essa observação |
| `replication_dispatch_latency` | `start(ReplicationSendEventsRestServlet) - start(outgoing-replication-request)` | Despacho entre Generic Worker e Event Persister |
| `event_persister_pre_transaction_latency` | `start(db.txn[db.txn_desc=persist_events]) - start(persist_event_batch)` | Trabalho do persister antes da transação principal |
| `event_persister_processing_latency` | `mean(duration(ReplicationSendEventsRestServlet), message_send, run) - mean(event_persistence_transaction_latency, run)` | T6 estimado automaticamente por repetição; não representa uma observação span a span |
| `event_persistence_transaction_latency` | `duration(db.txn[db.txn_desc=persist_events])` | Transação final de persistência no PostgreSQL |
| `nginx_total_request_latency` | `duration(matrix-client-proxy)` | Tempo total observado no proxy para qualquer endpoint bem-sucedido |
| `nginx_worker_connection_latency` | `sum(nginx.upstream_connect_time) × 1000` | Tempo gasto conectando ao upstream, incluindo tentativas registradas pelo NGINX |
| `nginx_upstream_first_byte_latency` | `sum(nginx.upstream_header_time) × 1000` | Tempo acumulado até o recebimento do primeiro byte/cabeçalho dos upstreams |
| `nginx_upstream_processing_latency` | `(sum(nginx.upstream_header_time) - sum(nginx.upstream_connect_time)) × 1000` | Aproximação do processamento no worker antes do início da resposta |
| `nginx_upstream_response_transfer_latency` | `(sum(nginx.upstream_response_time) - sum(nginx.upstream_header_time)) × 1000` | Tempo de transferência da resposta após o primeiro byte |
| `nginx_proxy_client_overhead` | `(nginx.request_time - sum(nginx.upstream_response_time)) × 1000` | Parcela residual no proxy, cliente e rede fora da espera pelas respostas dos upstreams |

### Correspondência com T2–T7

Neste ambiente, “Ingress Controller” deve ser entendido como o pod NGINX que
atua como proxy reverso e balanceador. A correspondência abaixo distingue
medições diretas de aproximações, pois o NGINX Open Source não publica eventos
de entrada e saída de uma fila de upstream.

| Tempo desejado | Métrica(s) utilizável(is) | Correspondência | Interpretação correta |
|---|---|---|---|
| T2 — Accept and enqueue in Ingress Controller | `nginx_proxy_client_overhead` | Aproximação parcial | Captura o tempo residual fora do upstream, mas também pode incluir recepção da requisição, rede e envio da resposta; não isola aceite nem fila |
| T3 — Processing in Ingress Controller + worker allocation | `nginx_worker_connection_latency` | Observação parcial e disjunta de T2 | Mede somente o estabelecimento da conexão com o worker; seleção de rota e fila interna não são observáveis, e keep-alive pode produzir zero |
| T4 — Generic Worker processing | `generic_worker_processing_latency` | Direta | Mede o processamento após autenticação e antes do início do envio de replicação |
| T5 — PostgreSQL session verification | `postgresql_session_verification_latency` | Direta quando há consulta | Mede `duration(db.get_user_by_access_token)`; cache hits não produzem span de PostgreSQL |
| T6 — Event Persister processing | `event_persister_processing_latency` | Estimativa automática por repetição | Aproxima o processamento não-DB do Event Persister associado ao caminho `message_send`; a subtração ocorre entre médias da mesma repetição |
| T7 — Final database write into PostgreSQL | `event_persistence_transaction_latency` | Direta | Mede a duração da transação `db.txn` identificada por `db.txn_desc=persist_events` |

As fórmulas usadas no relatório são:

```text
T2 (aproximação) = nginx_proxy_client_overhead

T3 (componente observável) = nginx_worker_connection_latency
                           = sum(nginx.upstream_connect_time) * 1000

T4 = start(outgoing-replication-request)
     - end(get_user_by_req)

T5 = duration(db.get_user_by_access_token)

T6_run = mean(duration(ReplicationSendEventsRestServlet), scope=message_send, run)
         - mean(event_persistence_transaction_latency, scope=other, run)

T7 = duration(db.txn where db.txn_desc = "persist_events")
```

T2 e T3 são calculadas separadamente para cada requisição e não compartilham
termos. Somente observações HTTP 2xx válidas entram nas médias. Os ICs 95%, a
ANOVA e o Tukey usam uma média por execução independente, não os spans como
réplicas. Use `scope=message_send` para T2–T5 e para o servlet usado em T6. T7
aparece como `scope=other` porque o trace de persistência não é correlacionado
ao trace NGINX original. `scope=sync` descreve long polling e não representa o
fluxo de envio de texto.

T2 continua sendo um resíduo que pode conter recepção da requisição,
comunicação com o cliente, rede e processamento local. T3 representa somente a
parcela observável da alocação: estabelecer a conexão com o upstream. Ela não
mede matching de `location`, seleção interna, fila ou CPU do NGINX. Com
keep-alive, T3 pode ser legitimamente zero. `nginx_upstream_processing_latency`
é diagnóstico do worker Synapse após a conexão e não pertence a T3.

Portanto, T2 e T3 devem ser declarados no estudo como *proxy-based estimates*.
T4, T5 e T7 possuem correspondência direta com spans, respeitada a ausência de
T5 em cache hits. T6 é uma estimativa entre médias por repetição, pois o servlet
e a transação T7 não compartilham o mesmo trace. `replication_dispatch_latency` permanece como indicador auxiliar
do encaminhamento entre o Generic Worker e o Event Persister; ele não integra
T2–T7 nem é somado automaticamente a T6 porque as observações independentes nem
sempre podem ser correlacionadas uma a uma.

T2–T7 são indicadores de etapas selecionadas, não uma partição exata e
exaustiva de `nginx_total_request_latency`. Para uma comparação descritiva:

```text
covered_path_estimate = T2 + T3 + T4 + T5 + T6 + T7
coverage_percent = 100 * covered_path_estimate / nginx_total_request_latency
```

Use `≈`, nunca igualdade: existem trechos não instrumentados, T5 é condicional
a cache miss e T6/T7 vêm de populações de traces diferentes. A aproximação deve
usar médias por repetição e depois resumir as repetições; não trate spans ou os
31 pontos temporais como réplicas independentes.

Quando todos os componentes são coletados, o analisador gera automaticamente
`analysis/jaeger-derived/t2_t7_by_repetition.csv`,
`t2_t7_coverage_confidence_intervals.csv` e o gráfico
`plots/t2_t7_coverage.png`. Soma, resíduo e cobertura são calculados primeiro
em cada repetição completa. A coluna `complete=false` identifica execuções com
componentes ausentes e impede que uma soma parcial seja apresentada como
decomposição completa.

### Nomes recomendados para publicação

Os identificadores T2–T7 podem permanecer em diagramas, mas tabelas, gráficos
e texto devem usar nomes que expressem exatamente o que foi observado:

| ID | Nome recomendado em inglês | Métrica do projeto | Por que este nome |
|---|---|---|---|
| T2 | **Reverse-proxy residual overhead estimate** | `nginx_proxy_client_overhead` | O NGINX não expõe uma fila; o valor é o resíduo após retirar o tempo total atribuído aos upstreams |
| T3 | **Upstream worker connection latency** | `nginx_worker_connection_latency` | Nomeia apenas o trecho realmente observado e mantém T3 disjunto do resíduo usado como T2 |
| T4 | **Generic Worker post-authentication processing latency** | `generic_worker_processing_latency` | Mede o processamento após autenticação e antes do início da replicação; explica uma parcela relevante da latência total |
| T5 | **PostgreSQL access-token verification latency** | `postgresql_session_verification_latency` | O span mede diretamente a consulta `db.get_user_by_access_token`; autenticações atendidas por cache não pertencem a essa população |
| T6 | **Estimated Event-Persister non-database processing latency** | `event_persister_processing_latency` | Preserva o tempo total do servlet ligado a `message_send`, retirando a duração média atribuída a T7 na mesma repetição |
| T7 | **PostgreSQL event-persistence transaction latency** | `event_persistence_transaction_latency` | Corresponde à duração da transação PostgreSQL que persiste os eventos |

Outras métricas ajudam a explicar esses tempos sem substituí-los:

| Métrica | Uso diagnóstico |
|---|---|
| `request_authentication_latency` | Tempo completo de autenticação observado pelo Generic Worker, incluindo caminhos com e sem consulta ao banco |
| `replication_dispatch_latency` | Atraso auxiliar entre o início do cliente de replicação e a entrada no Event Persister; não pertence à soma principal T2–T7 |
| `event_persister_pre_transaction_latency` | Diagnóstico direto do trecho entre o início de `persist_event_batch` e a transação; permanece coletado, mas não é mais a definição publicada de T6 |
| `nginx_total_request_latency` | Permanência total da requisição no proxy |
| `nginx_worker_connection_latency` | Componente observável adotado como T3; pode ser zero com conexão reutilizada |
| `nginx_upstream_first_byte_latency` | Tempo acumulado até o primeiro byte do worker |
| `nginx_upstream_processing_latency` | Aproximação do processamento do worker após a conexão e antes do primeiro byte |
| `nginx_upstream_response_transfer_latency` | Transferência entre o primeiro byte e o fim da resposta |

### Relatórios automáticos de qualidade

Cada repetição com métricas semânticas gera:

```text
users-<load>__<workload>__rep-<NN>/jaeger/
├── data_quality.csv
└── DATA_QUALITY.md
```

Ao final da campanha, o runner consolida os resultados em:

```text
analysis/data_quality.csv
EXPERIMENT_REPORT.md
```

O relatório classifica cada métrica como `PASS`, `WARNING` ou `FAIL`. Ele
considera presença de observações válidas, percentual inválido, cobertura T5,
saturação das consultas Jaeger e concentração de T2/T3 em zero na resolução do
NGINX. Uma métrica `FAIL` permanece nos
CSVs para auditoria, mas não deve sustentar inferência sem correção da causa.

Os limites padrão e suas opções são:

| Opção | Padrão | Efeito |
|---|---:|---|
| `--quality-warn-invalid-percent` | 5 | `WARNING` acima deste percentual inválido |
| `--quality-fail-invalid-percent` | 20 | `FAIL` acima deste percentual inválido |
| `--quality-warn-t5-coverage-percent` | 90 | `WARNING` quando a cobertura T5 fica abaixo deste valor |
| `--quality-fail-t5-coverage-percent` | 70 | `FAIL` quando a cobertura T5 fica abaixo deste valor |
| `--quality-warn-zero-percent` | 80 | `WARNING` quando T2/T3 se concentram em zero na resolução do NGINX |

A cobertura T5 é calculada por repetição como:

```text
100 * valid(postgresql_session_verification_latency)
    / valid(request_authentication_latency)
```

Ela mede a presença da validação PostgreSQL entre as autenticações amostradas,
e não a taxa global de cache do Synapse. Uma cobertura baixa também pode indicar
traces incompletos, portanto deve ser investigada antes de ser atribuída apenas
ao cache.

Os nomes antigos `--quality-warn-t4-coverage-percent` e
`--quality-fail-t4-coverage-percent` permanecem aceitos como aliases de
compatibilidade, mas estão semanticamente obsoletos.

Para cada métrica e repetição, o valor publicado é
`mean(observações válidas dentro da janela experimental)`. A métrica de
despacho atravessa relógios de dois workers e só aceita diferenças no intervalo
`0 <= valor <= duration(outgoing-replication-request)`. Consulte o
[guia do Jaeger](jaeger.md) para configuração, artefatos e limitações.

As métricas `nginx_*` acima abrangem todos os endpoints com resposta HTTP 2xx e são
separadas posteriormente por escopo. Todas usam relógio e atributos de um único
span NGINX; não dependem de propagação nem da sincronização com o gerador.

`nginx_proxy_overhead` permanece aceito como alias obsoleto de
`nginx_proxy_client_overhead` para reproduzir comandos antigos. Não solicite os
dois nomes na mesma campanha, pois eles representam exatamente a mesma observação.

Essas métricas não exigem importar o access log do NGINX: o coletor lê os
atributos diretamente dos spans `matrix-client-proxy` no Jaeger. T1 é calculado
separadamente a partir de 100% das tentativas de mensagem registradas pelo
Locust; ele não usa a amostra de 10% do tracing. Os valores `upstream_*` podem conter uma
sequência quando há retry; nesse caso, as fórmulas somam os tempos registrados
para todas as tentativas. `nginx_upstream_processing_latency` continua sendo
uma aproximação até o primeiro byte, não tempo puro de CPU do worker.

As métricas removidas `nginx_message_acceptance_latency` e
`nginx_to_generic_worker_latency` subtraíam timestamps de máquinas diferentes e
dependiam de propagação não suportada de forma confiável pela API cliente do
Synapse. Elas não devem ser usadas na análise.

Como os tempos do NGINX são arredondados separadamente em milissegundos, as
diferenças derivadas podem chegar a aproximadamente `-1 ms`. Valores no intervalo
`[-1.1, 0)` são tratados como ruído e publicados como zero. O valor anterior à
normalização permanece em `raw_value_ms` no `derived_observations.csv`.
Valores abaixo de `-1.1 ms` continuam inválidos e não entram nos resumos.

Respostas não 2xx permanecem auditáveis em `spans.csv` por meio de
`http_status_code` e aparecem como observações inválidas em
`derived_observations.csv` quando a métrica se aplica. Elas não entram nas
médias, nos intervalos de confiança ou na ANOVA do caminho bem-sucedido.
Quando o span NGINX está presente, o mesmo status invalida as métricas internas
daquele trace. Traces do Event Persister sem o span NGINX não permitem recuperar
o status; essa é uma limitação da correlação interrompida entre os workers.

### Completude, retries e proveniência

`collection_status.csv` registra o estado de Locust, Prometheus e Jaeger por
célula. Uma falha Jaeger resulta em `cell_status=partial_jaeger_failed`; com
`--skip-existing`, a célula é executada novamente. `execution_provenance.json`
registra argumentos, versões de Python e Locust, plataforma, commit Git e o
estado do worktree. Use `--jaeger-sampling-rate 0.1` para documentar sampling de
10%; a opção não altera os servidores.

Consultas Prometheus usam retry exponencial. Os padrões são timeout de 30 s e
três retries, configuráveis por `--prometheus-timeout` e
`--prometheus-retries`.

### Escopos por endpoint

Os resumos de tracing possuem a coluna `scope`. `all` inclui todos os traces;
`sync`, `message_send`, `media_upload` e `other` isolam cada classe; e
`foreground` agrega `message_send` e `media_upload`, excluindo `/sync`. Assim,
os resultados totais continuam auditáveis e a análise principal pode usar o
mesmo conceito de foreground adotado para Locust e Prometheus.

## Configuração do scrape interval

O espaçamento aproximado entre observações é
`measurement_duration / (samples - 1)`. Na configuração recomendada, 31
observações cobrem 120 s, inclusive os extremos, e ficam separadas por 4 s.
Use `scrape_interval: 2s` para fornecer pelo menos duas coletas Prometheus por
ponto amostral:

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

Cada execução mantém o número configurado de observações brutas, que são
reduzidas a uma média em `run_summaries.csv`. Cada gráfico mostra carga no eixo X, uma linha por
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

ANOVA, IC e Tukey HSD usam somente as médias por execução. No desenho canônico
3×2, o Tukey compara as seis células (`carga / workload`) e grava os 15 pares
com p-valor ajustado; outros desenhos geram a quantidade correspondente de
comparações e gráficos. São necessárias pelo menos duas
repetições por célula; o padrão é 3 e pode ser alterado com `--repetitions`.

Métricas constantes, sem cobertura dos dois fatores ou sem repetição suficiente
continuam aparecendo nos resumos e gráficos descritivos, mas são omitidas de
ANOVA e Tukey. O motivo fica registrado em `skipped_inference.csv`; isso evita
testes degenerados e warnings de matriz de covariância sem posto completo.

Entre células, `--cooldown` aguarda 60 s por padrão. Para cinco repetições, são
30 células: aproximadamente 102,5 min de execução mais 29 min de cooldown,
totalizando cerca de **131,5 min (2 h 11 min 30 s)**. Com cooldown de 120 s, o
total seria aproximadamente 2 h 40 min 30 s.
