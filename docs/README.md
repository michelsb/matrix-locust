# Documentação do Matrix Locust

Use este índice para encontrar o guia adequado. A apresentação e a instalação
rápida permanecem no [README principal](../README.md).

## Preparar os dados

| Cenário | Documento |
|---|---|
| Um único Synapse | [Setup de homeserver](setup/homeserver.md) |
| Salas entre dois ou mais Synapses | [Setup de federação](setup/federation.md) |

Os guias de setup terminam quando usuários, tokens e salas estão prontos e
validados. A execução da carga fica nos documentos de experimentos.

## Executar e analisar

| Objetivo | Documento |
|---|---|
| Entender fatores, janela e análise estatística | [Desenho experimental](experiments/design.md) |
| Copiar comandos para smoke, carga baixa, fatorial ou stress | [Runbook](experiments/runbook.md) |
| Gerar carga em múltiplos homeservers | [Carga federada](experiments/federation.md) |
| Validar o ambiente antes da campanha | [Checklist de validação](experiments/validation.md) |
| Entender cada coluna e consulta PromQL | [Métricas](experiments/metrics.md) |
| Construir e executar o container | [Docker](docker.md) |
| Diagnosticar erros | [Solução de problemas](troubleshooting.md) |

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
experiments/metrics.md e experiments/design.md
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
