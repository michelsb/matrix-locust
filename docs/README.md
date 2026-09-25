# Documentação do Matrix Locust

Use este índice para encontrar o guia adequado. A apresentação e a instalação
rápida permanecem no [README principal](../README.md).

Os endereços `*.atlab.ufc.br` e `172.27.176.1` nos comandos são exemplos do
ambiente de laboratório. Substitua-os pelos endpoints e labels da campanha ao
executar em outro ambiente; mantenha credenciais somente nos arquivos `.env`
ignorados pelo Git.

## Preparar os dados

| Cenário | Documento |
|---|---|
| Um único Synapse | [Setup de homeserver](setup/homeserver.md) |
| Salas entre dois ou mais Synapses | [Setup de federação](setup/federation.md) |
| Habilitar tracing no Synapse e no NGINX | [Setup de Jaeger tracing](setup/jaeger-tracing.md) |

Os guias de setup terminam quando usuários, tokens e salas estão prontos e
validados. A execução da carga fica nos documentos de experimentos.

## Executar e analisar

| Objetivo | Documento |
|---|---|
| Entender fatores, janela e análise estatística | [Desenho experimental](experiments/design.md) |
| Copiar comandos para smoke, carga baixa, fatorial ou stress | [Runbook](experiments/runbook.md) |
| Gerar carga em múltiplos homeservers | [Carga federada](experiments/federation.md) |
| Executar o soak test federado de 8 horas | [Soak test home01/home02](experiments/federation-soak/README.md) |
| Validar o ambiente antes da campanha | [Checklist de validação](experiments/validation.md) |
| Entender cada coluna e consulta PromQL | [Métricas](experiments/metrics.md) |
| Medir operações internas com tracing | [Jaeger](experiments/jaeger.md) |
| Construir e executar o container | [Docker](docker.md) |
| Diagnosticar erros | [Solução de problemas](troubleshooting.md) |

O guia do Jaeger também descreve as métricas locais do NGINX e as visões `all`,
`sync`, `foreground` e por endpoint. NGINX e Synapse são amostrados de forma
independente. Para o NGINX, T2 usa `nginx_proxy_client_overhead` e T3 usa
`nginx_worker_connection_latency`; são componentes disjuntos e explicitamente
documentados como observações parciais. O mesmo guia ensina a reanalisar CSVs
antigos sem disparar uma nova carga.

Ao final de cada execução, comece por `EXPERIMENT_REPORT.md`. Ele consolida os
estados `PASS`, `WARNING` e `FAIL` dos relatórios `DATA_QUALITY.md` de cada
repetição e aponta os artefatos estatísticos correspondentes.

T1, o intervalo global entre injeções consecutivas de mensagens, é coletado em
todas as campanhas por `run_factorial.py`. Sua definição, artefatos e tratamento
estatístico ficam no [catálogo de métricas](experiments/metrics.md#t1--intervalo-entre-chegadas-de-novas-mensagens).

## Fluxo recomendado

```text
instalação
   ↓
setup/homeserver.md ou setup/federation.md
   ↓
experiments/validation.md
   ↓
experiments/runbook.md ou experiments/federation.md
   ↓
experiments/metrics.md, experiments/design.md e, opcionalmente, jaeger.md
```

Para a maioria dos usos com um único servidor:

1. prepare o dataset com [setup/homeserver.md](setup/homeserver.md);
2. execute o smoke test de [experiments/runbook.md](experiments/runbook.md#smoke-test-sem-prometheus);
3. escolha a campanha no mesmo runbook;
4. interprete os resultados com [experiments/design.md](experiments/design.md)
   e [experiments/metrics.md](experiments/metrics.md).

Para federação:

1. prepare salas mistas com [setup/federation.md](setup/federation.md);
2. valide os dois lados;
3. siga [experiments/federation.md](experiments/federation.md).
