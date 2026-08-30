# Experimentos fatoriais de carga

Este diretório automatiza a execução, a coleta e a análise do experimento
fatorial completo do Matrix Synapse. O executor combina três níveis de carga
com dois tipos de workload, repete cada combinação de forma independente e
gera tabelas, gráficos, ANOVA e comparações de Tukey.

## Navegação

- [Guia principal](../README.md): instalação, visão geral e início rápido.
- [Setup de um homeserver](../setup_homeserver/README.md): cria usuários,
  tokens e salas para este experimento.
- [Setup de federação](../setup_federation/README.md): prepara dois
  homeservers; não é necessário para o fatorial descrito aqui.
- [Catálogo de métricas](METRICS.md): origem, unidade e PromQL de cada coluna,
  além da configuração recomendada do Prometheus.
- [Runbook de testes](TESTING.md): preflight, smoke test e comando completo da
  campanha recomendada.

## Desenho experimental

O desenho padrão tem seis células:

| Fator | Níveis |
|---|---|
| Usuários simultâneos | 50, 100 e 150 |
| Workload | `text_only` e `text_and_image` |

Os usuários entram à taxa de 5 usuários/s. Em cada repetição, a ordem das seis
células é aleatorizada com uma semente reproduzível. O padrão é executar três
repetições independentes por célula; use cinco quando o tempo disponível
permitir maior poder estatístico.

As 31 linhas de `samples.csv` são observações temporais de uma execução, não 31
réplicas independentes. A análise primeiro calcula a média dessas observações e
usa uma média por execução em IC 95%, ANOVA e Tukey. Isso evita
pseudorreplicação.

## Janela de cada execução

Cada célula passa pelas seguintes fases:

| Fase | Duração padrão | Entra na amostra? |
|---|---:|---|
| Ramp-up | `usuários / 5` s | Não |
| Estabilização após atingir a carga | 60 s | Não |
| Medição | 120 s | Sim, 31 pontos |
| Buffer para finalizar a coleta | 5 s | Não |

Assim, uma execução dura aproximadamente 195 s para 50 usuários, 205 s para
100 e 215 s para 150. O início da medição é calculado a partir do instante em
que o histórico do Locust confirma que a carga desejada foi atingida, e não
apenas a partir de uma estimativa do ramp-up.

## Ritmo das ações

O modo padrão produz carga controlada e independente da velocidade de resposta
do servidor:

- `--message-rate 0.2` define, por usuário, média de 0,2 ações/s — uma ação a
  cada 5 s em média;
- os intervalos são aleatórios e seguem uma distribuição exponencial, evitando
  que todos os usuários atuem ao mesmo tempo;
- em `text_only`, todas as ações enviam texto;
- em `text_and_image`, `--image-ratio 0.15` reserva em média 15% das ações para
  imagens e 85% para texto.

Por padrão, cada mensagem contém exatamente 10 palavras, mas seu conteúdo é
variado de forma reproduzível. O tamanho fixo reduz o ruído no fatorial
principal. A semente de cada usuário deriva da semente da campanha, da célula,
da repetição e da identidade do usuário: repetições podem ser reproduzidas sem
sincronizar artificialmente todos os clientes.

Para estudos complementares, `--text-length-profile` oferece:

| Perfil | Comportamento | Uso indicado |
|---|---|---|
| `fixed` | exatamente `--text-length-words` (padrão: 10) | fatorial principal |
| `short` | mediana próxima de 3 palavras | conversas muito curtas |
| `mixed` | mediana próxima de 10 palavras | tráfego variado |
| `long` | mediana próxima de 30 palavras | mensagens extensas |

Exemplo com conteúdo misto:

```bash
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --text-length-profile mixed
```

Por exemplo, 100 usuários a 0,2 ação/s oferecem aproximadamente 20 ações/s ao
cliente, antes de considerar as requisições auxiliares necessárias a cada
ação. Como o pacing acontece entre ações, um servidor lento não induz o
gerador a aumentar artificialmente a frequência.

Cada usuário também mantém um `/sync` em long polling. O timeout padrão é 30 s
e pode ser alterado com `--sync-timeout`. Assim que um evento chega ou o
timeout termina, o cliente abre outro `/sync`, sem espera artificial. Essa
o tempo de resposta é registrado separadamente e não entra em
`foreground_avg_response_time_ms`.

Para um teste de capacidade máxima, `--max-throughput` remove completamente a
espera. Nesse modo, cada usuário inicia a próxima ação assim que a anterior
termina; portanto, a taxa produzida passa a depender do tempo de resposta do sistema.
Não misture resultados dos modos controlado e máximo na mesma análise.

## Pré-requisitos

1. Instale as dependências na raiz:

   ```bash
   poetry install
   ```

2. Conclua o [setup de um homeserver](../setup_homeserver/README.md). O diretório
   `data/homeserver/` deve conter pelo menos `users.csv` e `tokens.csv`.
3. Coloque pelo menos um arquivo `.jpg` em `images/` para executar
   `text_and_image`.
4. Confirme que o Prometheus está acessível. Neste ambiente ele responde em
   `http://172.27.176.1:9091`; informe esse endereço com `--prometheus-url`
   (o fallback padrão do programa continua sendo `http://127.0.0.1:9091`).
5. Para que os 31 pontos tenham resolução real na janela de 120 s, configure o
   scrape interval do job experimental em 2 s, conforme
   [METRICS.md](METRICS.md#configuração-do-scrape-interval).

## Executar a campanha

Execução padrão, com três repetições por célula:

```bash
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --prometheus-url http://172.27.176.1:9091
```

Com cinco repetições independentes:

```bash
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --prometheus-url http://172.27.176.1:9091 \
  --repetitions 5
```

Com ritmo diferente e 20% de imagens:

```bash
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --message-rate 0.1 \
  --image-ratio 0.20
```

Teste separado de capacidade máxima:

```bash
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --max-throughput \
  --output-dir results/max-throughput
```

Para validar primeiro uma única célula:

```bash
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --loads 50 \
  --workloads text_only \
  --repetitions 1 \
  --output-dir results/smoke-test
```

Se uma campanha for interrompida, repita o mesmo comando acrescentando
`--skip-existing`. Células que já possuam `samples.csv` serão preservadas e
ignoradas.

## Opções principais

| Opção | Padrão | Função |
|---|---:|---|
| `--loads` | `50 100 150` | Níveis de usuários simultâneos |
| `--workloads` | ambos | `text_only`, `text_and_image` ou ambos |
| `--spawn-rate` | `5` | Usuários iniciados por segundo |
| `--message-rate` | `0.2` | Média de ações/s por usuário no modo controlado |
| `--image-ratio` | `0.15` | Fração de imagens no workload misto |
| `--text-length-profile` | `fixed` | Comprimento `fixed`, `short`, `mixed` ou `long` |
| `--text-length-words` | `10` | Palavras por texto no perfil fixo |
| `--sync-timeout` | `30` | Timeout em segundos do long polling `/sync` |
| `--max-throughput` | desativado | Remove a espera entre ações |
| `--repetitions` | `3` | Execuções independentes por célula |
| `--cooldown` | `60` | Pausa em segundos entre células |
| `--progress-interval` | `10` | Intervalo entre atualizações de progresso no terminal |
| `--stabilization` | `60` | Estabilização após atingir a carga |
| `--measurement-duration` | `120` | Duração da janela medida |
| `--samples` | `31` | Pontos temporais por execução |
| `--cpu-rate-window` | `30s` | Janela usada por `rate()` no Prometheus |
| `--data-dir` | `data/homeserver` | Dataset do setup |
| `--output-dir` | `results` | Destino de resultados e análises |
| `--skip-analysis` | desativado | Não gera a análise ao final |
| `--skip-existing` | desativado | Retoma campanha sem refazer células concluídas |

Consulte todas as opções com:

```bash
poetry run python experiments/run_factorial.py --help
```

## Tempo total estimado

Com os padrões atuais, cada repetição das seis células leva 20 min 30 s de
execução. Acrescenta-se cooldown entre células da campanha:

| Repetições | Células | Execução | Cooldowns | Total aproximado |
|---:|---:|---:|---:|---:|
| 3 | 18 | 61 min 30 s | 17 min | **1 h 18 min 30 s** |
| 5 | 30 | 102 min 30 s | 29 min | **2 h 11 min 30 s** |

Esse cálculo não inclui atrasos externos, como inicialização do processo ou
respostas mais lentas no encerramento. O executor imprime sua própria
estimativa antes de começar.

## Acompanhar a execução

A cada 10 segundos, por padrão, o terminal mostra:

- posição da célula na campanha e seu nome;
- fase atual: ramp-up, estabilização, medição ou buffer;
- avanço da fase;
- percentual e tempo decorrido da campanha;
- tempo restante e horário estimado de término.

Exemplo:

```text
[2026-08-29 14:20:10 -03] [progress] célula 4/18 users-100__text_and_image__rep-01 | fase medição 40s/2m 00s | campanha 21.8% | decorrido 17m 06s | restante estimado 1h 01m 24s | término ~15:21:34
```

Altere a frequência das atualizações, se desejado:

```bash
poetry run python experiments/run_factorial.py \
  --host https://matrix-test.atlab.ufc.br \
  --progress-interval 30
```

O mesmo acompanhamento fica persistido em `results/campaign.log`. A saída
detalhada do Locust permanece no `locust.log` de cada célula. O ETA considera
as execuções e os cooldowns planejados; consultas finais ao Prometheus e a
análise estatística podem acrescentar algum tempo.

## Resultados gerados

Cada célula recebe um diretório próprio, por exemplo:

```text
results/
├── campaign.log
├── users-50__text_only__rep-01/
│   ├── locust_stats.csv
│   ├── locust_stats_history.csv
│   ├── locust_failures.csv
│   ├── locust_exceptions.csv
│   ├── locust.log
│   ├── metadata.json
│   ├── report.html
│   ├── samples.csv
│   └── workload_stats.json
├── all_samples.csv
├── dataset_manifest.json
└── analysis/
    ├── run_summaries.csv
    ├── cpu_workers_by_scenario.csv
    ├── confidence_intervals.csv
    ├── anova_<metrica>.csv
    ├── anova_coefficients_ci95.csv
    ├── plots/<metrica>.png
    ├── plots/cpu_workers_stacked.png
    ├── tukey/tukey_<metrica>.csv
    └── plots/tukey/tukey_<metrica>.png
```

`metadata.json` registra fases, parâmetros, comando, consultas PromQL e hashes
do dataset. `all_samples.csv` reúne as 31 observações de todas as execuções.
`run_summaries.csv` contém a unidade experimental usada nos testes
estatísticos.

`plots/cpu_workers_stacked.png` compara todos os workers em um único gráfico:
cada barra corresponde a uma combinação de carga e workload, e cada segmento
corresponde a um job retornado pelo Prometheus. O CSV
`cpu_workers_by_scenario.csv` permite reutilizar os mesmos valores em outras
ferramentas de visualização.

Durante o fatorial, atualizações de `next_batch` permanecem somente na memória
do usuário e `tokens.csv` não é sobrescrito. Isso mantém o dataset imutável
entre células randomizadas e evita tráfego interno de atualização de tokens no
runner local. O runner manual continua persistindo tokens por padrão.

As métricas do Locust são mantidas em duas visões:

- `avg_response_time_ms` e `p95_response_time_ms`: agregado bruto de todos os endpoints,
  incluindo `/sync`;
- `foreground_avg_response_time_ms`: tempo de resposta médio de todos os endpoints de
  primeiro plano, excluindo somente `/sync`;
- prefixos `sync_`, `text_send_`, `image_send_` e `media_upload_`: RPS,
  tempo de resposta médio e p95 de cada endpoint.

Respostas HTTP com erro não interrompem uma célula: elas fazem parte do
resultado experimental e permanecem em `locust_failures.csv`,
`failures_per_second` e `foreground_failures_per_second`. Falhas operacionais
do processo Locust, ausência de tráfego ou uma janela incompleta continuam
interrompendo a campanha.

O agregado sem `/sync` usa a média dos endpoints ponderada pelo RPS. Não é
gerado um p95 combinado sem `/sync`, pois percentis agregados não podem ser
reconstruídos corretamente a partir dos percentis individuais; os p95 por
endpoint permanecem disponíveis.

`workload_stats.json` permite auditar a carga produzida durante toda a célula:
tentativas e sucessos de texto e imagem, palavras e bytes enviados, histograma
dos tamanhos textuais, bytes de upload e quantidade de escolhas de cada
imagem. Esses totais incluem ramp-up, estabilização, medição e buffer; as
métricas temporais de desempenho em `samples.csv` continuam restritas à janela
de medição.

Os gráficos principais mostram a média entre execuções e IC 95% de Student por
célula. A ANOVA tipo II avalia carga, workload e sua interação. O Tukey HSD faz
as 15 comparações par a par entre as seis células, com p-valores ajustados e
intervalos simultâneos de 95%. A análise só gera ANOVA e Tukey quando há pelo
menos duas repetições em cada célula; três é o mínimo prático recomendado.

## Reexecutar somente a análise

```bash
poetry run python experiments/analyze_results.py results/all_samples.csv \
  --output-dir results/analysis
```

Para interpretar cada métrica e conferir sua origem, continue em
[Métricas, origem e análise estatística](METRICS.md).

## Boas práticas de comparação

- Não altere dataset, configuração do Synapse ou infraestrutura durante uma
  campanha.
- Use diretórios de saída diferentes para ritmos, versões ou configurações
  diferentes.
- Mantenha o cooldown suficiente para o sistema retornar ao estado basal.
- Guarde `metadata.json` e `dataset_manifest.json` junto dos resultados.
- Examine falhas e tempos de resposta, não apenas RPS; throughput alto com erros não
  representa capacidade útil.
