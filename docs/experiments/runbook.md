# Runbook dos experimentos

Este é o catálogo canônico de comandos para executar campanhas. Para entender
o desenho fatorial e a análise estatística, consulte o
[guia de experimentos](design.md). Para métricas, consulte
[metrics.md](metrics.md). Para múltiplos servidores, consulte
[federation.md](federation.md). Para medir operações internas do Synapse na
mesma janela, consulte [Coleta de tracing com Jaeger](jaeger.md).

## Antes de executar

```console
poetry install
test -s data/homeserver/users.csv
test -s data/homeserver/tokens.csv
```

O maior valor de `--loads` não pode exceder a quantidade de usuários válidos
no dataset. O workload `text_and_image` também exige pelo menos um JPG em
`images/`.

Use um `--output-dir` exclusivo para cada campanha. Não misture resultados com
cargas, ritmos ou fontes de métricas diferentes.

T1 (`message_interarrival_time_ms`) é sempre coletado, inclusive com
`--no-prometheus` e sem Jaeger. Não existe uma opção para habilitá-lo: o runner
registra todas as tentativas de envio e falha a célula caso não consiga formar
ao menos um intervalo dentro da janela.

O runner ainda calcula cadências HTTP auxiliares. Use
`request_interarrival_all_ms` para todas as requisições e
`request_interarrival_foreground_ms` para excluir `/sync`.
`request_interarrival_sync_ms` mede o intervalo entre inícios de long polls;
não é T1 nem a taxa de chegada de mensagens.

Em campanhas Jaeger, solicite `event_persister_processing_latency` para gerar
T6 automaticamente por repetição. Com sampling de 10%, registre
`--jaeger-sampling-rate 0.1`. Depois da execução, confira
`collection_status.csv`: apenas células com `cell_status=complete` devem ser
usadas para inferência. `execution_provenance.json` registra versões, commit e
argumentos usados.

## Prometheus opcional

O Prometheus é habilitado por padrão:

```console
--prometheus-url http://172.27.176.1:9091 \
--instance matrix-test.atlab.ufc.br
```

Para coletar somente métricas do Locust:

```console
--no-prometheus
```

Nesse modo não há preflight nem conexão remota, e `samples.csv` não recebe
colunas de CPU ou métricas internas do Synapse.

## Smoke test sem Prometheus

Valida autenticação, `/sync`, texto e geração dos arquivos locais:

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
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

## Carga baixa controlada

Este exemplo oferece uma ação a cada 10 segundos, em média, por usuário:

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --data-dir data/homeserver \
  --loads 10 25 \
  --workloads text_only text_and_image \
  --spawn-rate 2 \
  --message-rate 0.1 \
  --repetitions 3 \
  --no-prometheus \
  --output-dir results/low-load
```

## Fatorial recomendado com Prometheus

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --prometheus-url http://172.27.176.1:9091 \
  --instance matrix-test.atlab.ufc.br \
  --data-dir data/homeserver \
  --loads 50 100 150 \
  --workloads text_only text_and_image \
  --spawn-rate 5 \
  --message-rate 0.2 \
  --image-ratio 0.15 \
  --repetitions 5 \
  --stabilization 60 \
  --measurement-duration 120 \
  --samples 31 \
  --cooldown 60 \
  --output-dir results/factorial-prometheus
```

## Fatorial sem Prometheus

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --data-dir data/homeserver \
  --loads 50 100 150 \
  --workloads text_only text_and_image \
  --repetitions 5 \
  --no-prometheus \
  --output-dir results/factorial-locust-only
```

## Latências internas com Jaeger

Este perfil mede 50 usuários, texto e 0,2 ação/s por usuário. As métricas
selecionadas determinam automaticamente o conjunto mínimo de serviços e
operações; não passe `--jaeger-services` nem `--jaeger-operations`.

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
    nginx_total_request_latency \
    nginx_worker_connection_latency \
    nginx_upstream_first_byte_latency \
    nginx_upstream_processing_latency \
    nginx_upstream_response_transfer_latency \
    nginx_proxy_client_overhead \
  --output-dir results/internal-latency-users-50-text-only
```

Antes da primeira célula, confira no stdout a linha `jaeger-plan`. Ao final,
verifique se `collection_metadata.json` não registra erros ou saturações e se
`derived_observations.csv` contém observações válidas. Consulte as
[fórmulas e limitações](metrics.md#metricas-semanticas-do-jaeger).
O runner também cria `DATA_QUALITY.md` em cada repetição e um
`EXPERIMENT_REPORT.md` consolidado na raiz da campanha; não é necessário
calcular manualmente cobertura, percentuais inválidos ou a classificação das
métricas.
O planejador descobre `matrix-nginx` além dos serviços Synapse, sem exigir
`--jaeger-services` ou `--jaeger-operations`.
Os resumos gerais do NGINX incluem visões `all`, `sync`, `foreground` e por
endpoint. NGINX e Synapse usam sampling independente; nenhuma métrica mantida
depende de correlacionar trace IDs ou relógios entre esses dois componentes.
`replication_dispatch_latency` continua dependendo da propagação interna e dos
relógios dos próprios workers Synapse.

## Stress com capacidade máxima

`--max-throughput` remove a espera entre ações. O volume produzido passa a
depender da velocidade de resposta do servidor; não compare essa campanha
diretamente com o modo controlado.

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --prometheus-url http://172.27.176.1:9091 \
  --instance matrix-test.atlab.ufc.br \
  --data-dir data/homeserver \
  --loads 50 100 150 \
  --workloads text_only text_and_image \
  --max-throughput \
  --repetitions 3 \
  --output-dir results/stress-max-throughput
```

## Stress progressivo controlado

Mantém uma frequência oferecida independente da velocidade do servidor. Este
exemplo requer pelo menos 300 usuários válidos:

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --prometheus-url http://172.27.176.1:9091 \
  --instance matrix-test.atlab.ufc.br \
  --data-dir data/homeserver \
  --loads 100 200 300 \
  --workloads text_only text_and_image \
  --spawn-rate 10 \
  --message-rate 0.5 \
  --repetitions 3 \
  --output-dir results/stress-paced
```

## Retomar uma campanha

Repita exatamente a configuração original, com o mesmo diretório, e acrescente
`--skip-existing`:

```console
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --prometheus-url http://172.27.176.1:9091 \
  --instance matrix-test.atlab.ufc.br \
  --data-dir data/homeserver \
  --repetitions 5 \
  --output-dir results/factorial-prometheus \
  --skip-existing
```

Células com `samples.csv` são preservadas. Consulte `campaign.log` para
acompanhar o progresso e [troubleshooting.md](../troubleshooting.md) se uma célula
falhar.

## Execução manual do Locust

Para exploração fora do desenho fatorial:

```console
MATRIX_DATA_DIR=data/homeserver \
  poetry run locust -f locust-run-users.py \
  --host https://matrix-test.atlab.ufc.br
```

O executor fatorial é preferível para resultados científicos porque registra
configuração, dataset, timestamps e ordem randomizada.

## Todas as opções

```console
poetry run python experiments/run_factorial.py --help
```
