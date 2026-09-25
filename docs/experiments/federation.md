# Executar carga federada

Este guia descreve a execução depois que o
[setup de federação](../setup/federation.md) criou salas mistas e
exportou um dataset por homeserver. Para comandos de um único servidor,
consulte o [runbook](runbook.md).

## Modelo de execução

Cada processo Locust aceita um único `--host`. Para originar carga cliente nos
dois lados da federação, execute dois processos:

```text
processo home01                       processo home02
--host FED_HOME01_URL                 --host FED_HOME02_URL
--data-dir exports/home01             --data-dir exports/home02
--output-dir results/...home01        --output-dir results/...home02
```

Um homeserver sem processo Locust ainda recebe tráfego federado quando possui
membros nas salas mistas.

## Verificar os datasets

```console
test -s data/federation/exports/home01/users.csv
test -s data/federation/exports/home01/tokens.csv
test -s data/federation/exports/home02/users.csv
test -s data/federation/exports/home02/tokens.csv
poetry run python setup_federation/07_verify_federation.py
```

## Carga a partir de um lado

```console
poetry run python experiments/run_factorial.py \
  --host https://srv.home01.example.com \
  --data-dir data/federation/exports/home01 \
  --no-prometheus \
  --output-dir results/federation-home01
```

Os eventos enviados pelo home01 são federados ao home02 por causa das salas
mistas.

## Carga simultânea em dois lados

### Dois terminais

Terminal do home01:

```console
poetry run python experiments/run_factorial.py \
  --host https://srv.home01.example.com \
  --data-dir data/federation/exports/home01 \
  --loads 25 50 75 \
  --workloads text_only text_and_image \
  --repetitions 3 \
  --no-prometheus \
  --output-dir results/federation-simultaneous-home01
```

Terminal do home02:

```console
poetry run python experiments/run_factorial.py \
  --host https://srv.home02.example.com \
  --data-dir data/federation/exports/home02 \
  --loads 25 50 75 \
  --workloads text_only text_and_image \
  --repetitions 3 \
  --no-prometheus \
  --output-dir results/federation-simultaneous-home02
```

Use os mesmos fatores, repetições, seed, tempos e ritmo. A seed padrão 42
mantém a mesma ordem randomizada quando os fatores forem iguais.

### Um terminal Bash

```bash
poetry run python experiments/run_factorial.py \
  --host https://srv.home01.example.com \
  --data-dir data/federation/exports/home01 \
  --loads 25 50 75 \
  --workloads text_only text_and_image \
  --repetitions 3 \
  --no-prometheus \
  --output-dir results/federation-simultaneous-home01 &
pid_home01=$!

poetry run python experiments/run_factorial.py \
  --host https://srv.home02.example.com \
  --data-dir data/federation/exports/home02 \
  --loads 25 50 75 \
  --workloads text_only text_and_image \
  --repetitions 3 \
  --no-prometheus \
  --output-dir results/federation-simultaneous-home02 &
pid_home02=$!

wait "$pid_home01"
status_home01=$?
wait "$pid_home02"
status_home02=$?
echo "home01=$status_home01 home02=$status_home02"
```

O stdout pode aparecer intercalado, mas cada processo mantém seu próprio
`campaign.log`.

## Soak test de 8 horas com carga baixa

O launcher [`experiments/run_federation_soak.sh`](../../experiments/run_federation_soak.sh)
executa os dois lados em paralelo, encerra ambos ao receber `Ctrl+C` e permite
coleta com ou sem Prometheus. Configuração, validação, execução e recuperação
estão centralizadas no
[guia específico do soak test federado](federation-soak/README.md).

## Prometheus nos dois lados

Remova `--no-prometheus`. Com um Prometheus central, use a mesma URL e uma
`instance` diferente por Synapse:

```console
# home01
--prometheus-url http://prometheus.example.com:9091 \
--instance matrix-home01.example.com

# home02
--prometheus-url http://prometheus.example.com:9091 \
--instance matrix-home02.example.com
```

Com Prometheus separados, use também URLs diferentes. Uma `instance` incorreta
atribui métricas remotas ao lado errado.

## Carga global

`--loads` vale por processo. Dois processos com `--loads 50` produzem
aproximadamente 100 usuários simultâneos no conjunto. Para aproximadamente 50
usuários globais com divisão equilibrada, use `--loads 25` em cada lado.

Cada export precisa conter pelo menos a maior carga atribuída ao seu processo.

## Três ou mais homeservers

Execute um processo por homeserver que deve originar carga, sempre com seu
`--host`, export e diretório de resultados. Não é obrigatório executar Locust
em todos os participantes.

## Sincronização e análise

Os processos começam próximos, mas não possuem barreira distribuída. Coleta do
Prometheus, encerramento de usuários e variações do sistema podem deslocar
células posteriores. O método oferece simultaneidade aproximada; sincronização
rigorosa exige um orquestrador com barreiras.

Analise os diretórios separadamente. Eles representam os dois lados da mesma
execução, não repetições estatísticas independentes. Para retomar, repita cada
comando com `--skip-existing`; nunca compartilhe o mesmo `--output-dir`.
