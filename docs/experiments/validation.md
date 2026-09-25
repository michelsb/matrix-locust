# Checklist de validação do ambiente

Este guia descreve como validar o ambiente com uma carga curta e, depois,
executar a campanha fatorial com a configuração recomendada.

## Navegação

- [README principal](../../README.md)
- [Setup de um homeserver](../setup/homeserver.md)
- [Desenho e opções do experimento](design.md)
- [Comandos prontos de execução](runbook.md)
- [Carga em homeservers federados](federation.md)
- [Métricas e configuração do Prometheus](metrics.md)
- [Tracing e métricas internas com Jaeger](jaeger.md)
- [Solução de problemas](../troubleshooting.md)

## Configuração recomendada

| Parâmetro | Valor |
|---|---:|
| Cargas | 50, 100 e 150 usuários |
| Workloads | `text_only` e `text_and_image` |
| Entrada de usuários | 5 usuários/s |
| Ritmo por usuário | 0,2 ação/s, intervalo médio de 5 s |
| Texto | conteúdo variável, exatamente 10 palavras |
| Imagens | 15% das ações no workload misto |
| `/sync` | long polling com timeout de 30 s |
| Estabilização | 60 s após atingir a carga |
| Medição | 120 s, com 31 observações |
| Repetições | 3 por célula |
| Cooldown | 60 s entre células |
| Prometheus | `http://172.27.176.1:9091` |
| Instance | `matrix-test.atlab.ufc.br` |

Com três repetições, são 18 células e aproximadamente 1 h 18 min 30 s. O
executor mostra o progresso e o ETA no terminal e em `campaign.log`.

## 1. Validação local

Execute sempre a partir da raiz do repositório:

```bash
poetry install
poetry run locust --version
poetry run python -m py_compile \
  experiments/run_factorial.py \
  experiments/analyze_results.py \
  matrix_locust/users/matrixchatuser.py \
  matrix_locust/nio/locust_client.py
```

Confira a massa e as imagens sem imprimir senhas ou tokens:

```bash
poetry run python -c '
import csv
from pathlib import Path

directory = Path("data/homeserver")
for name in ("users.csv", "tokens.csv"):
    with (directory / name).open(newline="", encoding="utf-8") as handle:
        print(name, sum(1 for _ in csv.DictReader(handle)), "registros")

images = list(Path("images").glob("*.jpg"))
print("imagens JPG:", len(images))
'
```

Para as cargas recomendadas, `users.csv` e `tokens.csv` precisam ter pelo
menos 150 registros. O cenário misto precisa de pelo menos um `images/*.jpg`.

### Critérios de aceite

- `poetry install` e a compilação terminam sem erro;
- `users.csv` e `tokens.csv` contêm pelo menos o maior valor de `--loads`;
- existe pelo menos uma imagem JPG quando `text_and_image` é usado;
- o endpoint Matrix `/_matrix/client/versions` responde;
- os valores observados são registrados junto aos resultados, pois versões,
  quantidade de usuários e topologia podem mudar entre campanhas.

## 2. Preflight do Prometheus

Confirme que o servidor está pronto:

```bash
curl -fsS http://172.27.176.1:9091/-/ready
```

O resultado esperado é:

```text
Prometheus Server is Ready.
```

Confira quais jobs estão expondo a métrica de CPU:

```bash
curl -fsS --get \
  --data-urlencode 'query=count by (job) (process_cpu_seconds_total{instance="matrix-test.atlab.ufc.br"})' \
  http://172.27.176.1:9091/api/v1/query
```

Não existe uma lista fixa de workers no executor. Ele seleciona todas as séries
`process_cpu_seconds_total` que possuam a `instance` informada, descobre os
labels `job` presentes em cada célula e cria uma coluna `cpu_<job>_percent` para
cada um. Assim, workers adicionados ou removidos pelo Synapse são refletidos
automaticamente nos resultados.

`redis-svc` não precisa ser monitorado. Como atualmente não fornece
`process_cpu_seconds_total` para essa instance, ele simplesmente não entra nas
colunas de CPU nem invalida o experimento. O que importa é que os processos que
se deseja analisar estejam retornando séries de CPU.

Confira a resolução do scrape:

```bash
curl -fsS --get \
  --data-urlencode 'query=count_over_time(up{instance="matrix-test.atlab.ufc.br"}[2m])' \
  http://172.27.176.1:9091/api/v1/query
```

Com `scrape_interval: 2s`, cada série deve retornar aproximadamente 60.

Confira a métrica obrigatória de CPU:

```bash
curl -fsS --get \
  --data-urlencode 'query=count(process_cpu_seconds_total{instance="matrix-test.atlab.ufc.br"})' \
  http://172.27.176.1:9091/api/v1/query
```

Um resultado vazio ou zero impede o executor de concluir a coleta; não é
exigida uma quantidade específica ou um conjunto fixo de nomes.

## 3. Smoke test recomendado

O smoke test passa pelos dois workloads, incluindo upload de imagem, mas usa
somente 10 usuários, uma repetição e janelas curtas. Ele dura aproximadamente
1 min 20 s, mais o tempo de coleta e análise.

```bash
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --prometheus-url http://172.27.176.1:9091 \
  --instance matrix-test.atlab.ufc.br \
  --data-dir data/homeserver \
  --loads 10 \
  --workloads text_only text_and_image \
  --spawn-rate 5 \
  --message-rate 0.2 \
  --image-ratio 0.15 \
  --text-length-profile fixed \
  --text-length-words 10 \
  --sync-timeout 30 \
  --repetitions 1 \
  --stabilization 10 \
  --measurement-duration 20 \
  --samples 6 \
  --cooldown 5 \
  --output-dir results/smoke-test
```

Uma única repetição não é suficiente para ANOVA ou Tukey. O objetivo desse
comando é validar login, sync, texto, imagem, Locust, Prometheus e geração dos
arquivos.

### O que observar no terminal

O executor deve avançar por:

```text
campaign → cell → ramp-up → estabilização → medição → buffer
         → collect → analysis → done
```

Warnings sobre uma métrica Synapse ausente não encerram a célula. Ausência de
`process_cpu_seconds_total`, falha do Locust ou carga que não atinge 10 usuários
interrompem o teste.

O executor também invalida automaticamente células que terminem com zero
requisições Locust ou zero ações de primeiro plano. Isso evita aceitar como
válido um teste no qual os usuários foram criados, mas `/sync` ou as tarefas
falharam antes de produzir tráfego.

## 4. Conferir o smoke test

Confirme os artefatos:

```bash
find results/smoke-test -maxdepth 2 -type f | sort
```

Devem existir, no mínimo:

```text
results/smoke-test/campaign.log
results/smoke-test/all_samples.csv
results/smoke-test/dataset_manifest.json
results/smoke-test/users-10__text_only__rep-01/samples.csv
results/smoke-test/users-10__text_only__rep-01/workload_stats.json
results/smoke-test/users-10__text_only__rep-01/message_arrivals.csv
results/smoke-test/users-10__text_only__rep-01/message_interarrival_observations.csv
results/smoke-test/users-10__text_only__rep-01/message_interarrival_summary.csv
results/smoke-test/users-10__text_only__rep-01/request_arrivals.csv
results/smoke-test/users-10__text_only__rep-01/request_interarrival_observations.csv
results/smoke-test/users-10__text_only__rep-01/request_interarrival_summary.csv
results/smoke-test/users-10__text_and_image__rep-01/samples.csv
results/smoke-test/users-10__text_and_image__rep-01/workload_stats.json
```

Confirme que `samples.csv` contém `message_interarrival_time_ms` e que o número
de observações T1 é uma unidade menor que o número de injeções dentro da janela.
T1 deve existir mesmo quando Prometheus e Jaeger estiverem desabilitados.

Confirme também as colunas `request_interarrival_all_ms`,
`request_interarrival_foreground_ms` e `request_interarrival_sync_ms`. No
resumo por escopo, `all` inclui `/sync` e `foreground` o exclui. Um escopo com
menos de duas requisições pode legitimamente não ter uma linha de resumo.

Em campanhas com Jaeger, confirme também:

```console
test -f results/<campanha>/EXPERIMENT_REPORT.md
test -f results/<campanha>/analysis/data_quality.csv
find results/<campanha> -path '*/jaeger/DATA_QUALITY.md' -print
```

Para T2/T3, o catálogo atual deve conter `nginx_proxy_client_overhead` e
`nginx_worker_connection_latency`. A presença de
`nginx_proxy_worker_allocation_latency` fora dos diretórios de backup indica
uma campanha ainda não migrada; use o procedimento de
[reanálise offline](jaeger.md#reanalisar-campanhas-com-a-definicao-disjunta-de-t2t3).

Valide os CSVs e JSONs automaticamente:

```bash
poetry run python -c '
import csv
import json
from pathlib import Path

root = Path("results/smoke-test")
samples = list(root.glob("users-*/samples.csv"))
assert len(samples) == 2, f"esperava 2 samples.csv, encontrei {len(samples)}"
for path in samples:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 6, f"{path}: {len(rows)} amostras"
    required = {
        "rps", "avg_response_time_ms", "foreground_avg_response_time_ms",
        "sync_avg_response_time_ms", "cpu_total_percent",
    }
    assert required <= set(rows[0]), f"{path}: colunas ausentes"

for path in root.glob("users-*/workload_stats.json"):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["text"]["attempts"] > 0, f"{path}: nenhum texto tentado"

mixed = json.loads((root / "users-10__text_and_image__rep-01" / "workload_stats.json").read_text())
assert mixed["image"]["attempts"] > 0, "nenhuma imagem sorteada; repita o smoke test"
print("Smoke test validado com sucesso")
'
```

Com apenas 10 usuários e 20 segundos, existe uma pequena chance de nenhuma
imagem ser sorteada. Se isso ocorrer, não significa necessariamente defeito;
repita o smoke test ou use temporariamente `--image-ratio 0.50` apenas para
validar o caminho de mídia.

Examine ainda:

```bash
tail -n 50 results/smoke-test/campaign.log
tail -n 50 results/smoke-test/users-10__text_and_image__rep-01/locust.log
```

Não avance se houver falhas de login, HTTP 429, ausência de CPU ou se a carga
solicitada não tiver sido atingida.

Se o log mostrar tentativa de acesso a `matrix.<domínio>` ou erro DNS, confirme
que está usando a versão atual do código. O cliente deve respeitar exatamente
o endereço passado em `--host`; o domínio do MXID não é usado para reescrever a
URL da API cliente.

## 5. Campanha fatorial recomendada

Depois que o smoke test e os targets passarem, use o comando
[Fatorial recomendado com Prometheus](runbook.md#fatorial-recomendado-com-prometheus).

Não reutilize `results/smoke-test`, pois suas fases e cargas são diferentes.

## 6. Interromper e retomar

`Ctrl+C` encerra o processo Locust atual. Siga a receita
[Retomar uma campanha](runbook.md#retomar-uma-campanha) para não repetir células
que já possuem `samples.csv`.

Não altere seed, dataset, fatores ou parâmetros ao retomar. O executor mantém
a mesma ordem randomizada e combina as células concluídas no final.

## 7. Critérios de aceite da campanha

Antes de interpretar ANOVA e Tukey, confirme:

- 18 diretórios de células para três repetições;
- 31 linhas em cada `samples.csv`;
- 558 linhas em `all_samples.csv` (`18 × 31`);
- `workload_stats.json` em todas as células;
- CPU preenchida em todas as amostras;
- ausência de falhas relevantes ou HTTP 429;
- gráficos principais e de Tukey em `analysis/plots/`;
- tabelas ANOVA e Tukey em `analysis/`;
- três observações por célula em `analysis/run_summaries.csv`.

Consulte [Métricas, origem e análise estatística](metrics.md) para interpretar
cada coluna e [Experimentos fatoriais de carga](design.md) para entender o
modelo estatístico.
