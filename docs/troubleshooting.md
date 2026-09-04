# Solução de problemas

Use este guia depois dos passos de validação do [runbook](experiments/runbook.md). Os logs
principais são `results/<campanha>/campaign.log` e o `locust.log` de cada
célula.

## Locust

### Processo termina com código diferente de zero

Leia o final de `locust.log` e `locust_exceptions.csv`. Respostas HTTP com erro
são resultados experimentais e permanecem em `locust_failures.csv`; falhas do
processo, ausência de tráfego e janela incompleta interrompem a campanha.

### Histórico termina antes da janela

Confirme que o processo não foi morto e mantenha `--collection-buffer` maior
que zero. O runner aceita pequenas lacunas entre timestamps, mas exige que o
histórico completo cubra a janela medida.

### Nenhuma ação de primeiro plano

Verifique tokens, associação a salas e `workload_stats.json`. Confirme também
que `--message-rate` é maior que zero fora de `--max-throughput`.

## Prometheus

### Preflight ou consulta expira

Confirme a URL e a conectividade:

```console
curl -fsS http://172.27.176.1:9091/-/ready
```

Se métricas remotas não forem necessárias, use `--no-prometheus`. Não trate
uma campanha sem CPU como equivalente a uma campanha que coletou CPU.

### Nenhuma série de CPU

Confira `--instance`, targets e labels:

```console
curl -G http://172.27.176.1:9091/api/v1/query \
  --data-urlencode 'query=process_cpu_seconds_total'
```

Os jobs são descobertos dinamicamente; `redis-svc` é desconsiderado pelo
desenho atual.

### Poucos pontos

Use scrape interval de 2 s no job experimental e valide a quantidade com
`count_over_time(up[2m])`. Consulte [metrics.md](experiments/metrics.md).

## Synapse

### HTTP 429 ou `M_LIMIT_EXCEEDED`

Ajuste os rate limits do ambiente de teste ou reduza a carga. Durante o setup,
reduza também `WORKERS` ou aumente `REQUEST_SLEEP`.

### HTTP 500

O erro veio do homeserver e deve permanecer como observação experimental.
Correlacione o timestamp de `locust_failures.csv` com os logs dos workers e as
métricas de banco, reactor e filas.

### `/sync` domina RPS ou tempo de resposta

Isso pode ser normal: cada usuário mantém long polling e abre uma nova chamada
quando recebe evento ou atinge o timeout. Use as métricas `foreground_*` para
avaliar o primeiro plano sem `/sync` e as métricas `sync_*` separadamente.

## Setup de usuários

### `M_USER_IN_USE`

O usuário já existe. O registro tenta fazer login para recuperar o token;
normalmente basta reexecutar o passo.

### Certificado autoassinado

Use `VERIFY_TLS=false` apenas em laboratório controlado.

## Federação

### Convite remoto não chega

Confirme DNS/delegação, certificado, porta server-to-server, whitelist e
`DOMAIN`. Reexecute `05_accept_invites.py` com mais passes.

### Usuário conecta ao servidor errado

Use o export correspondente ao `--host`, nunca o CSV completo:

```text
data/federation/exports/home01
```

### Salas não aparecem como federadas

Confira prefixos únicos, domínios nos MXIDs e aceite dos convites. Execute:

```console
poetry run python setup_federation/07_verify_federation.py --check-messages
```

## Docker

### Permission denied em `/var/run/docker.sock`

Execute `docker info`. No Docker Desktop com WSL, confirme a integração da
distribuição e reabra a sessão. No Docker Engine nativo, confirme a associação
do usuário ao grupo `docker`. Não resolva expondo o socket para todos os
usuários. O guia completo está em [docker.md](docker.md).
