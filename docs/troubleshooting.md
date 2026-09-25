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

## Jaeger

### O preflight não encontra serviços ou operações

Confirme primeiro `http://SERVIDOR:16686/api/services` e o valor de
`--jaeger-service-prefix`. No modo semântico, não informe manualmente serviços
ou operações: a linha `jaeger-plan` mostra o plano mínimo calculado antes de a
carga começar.

### Consulta atinge `limit=1000`

O coletor subdivide automaticamente a janela. Um warning final significa que
até `--jaeger-min-chunk-duration` ficou saturado. Reduza esse valor, diminua a
duração da medição ou refine a coleta. Não interprete uma consulta saturada
como amostra completa.

### Métrica semântica não possui observações

Confira `jaeger/collection_metadata.json` e `derived_observations.csv`. Com
sampling probabilístico de 10%, cache hits e relações causais não amostradas
podem produzir lacunas; aumente a duração ou as repetições independentes antes
de aumentar o sampling. A ausência de `db.get_user_by_access_token`, por
exemplo, pode representar cache hit e não latência zero.

### NGINX e Synapse aparecem com trace IDs diferentes

Esse é o comportamento esperado na abordagem independente. As métricas
mantidas usam durações locais de cada componente e não exigem uma árvore única
entre NGINX e Synapse. Confira apenas se ambos aparecem no `spans.csv` e se o
sampling de cada um está configurado conforme o
[guia de setup](setup/jaeger-tracing.md).

Se uma métrica `nginx_*` estiver ausente, verifique em `spans.csv` se as tags
`nginx.upstream_connect_time`, `nginx.upstream_header_time`,
`nginx.upstream_response_time` e `nginx.request_time` existem e não contêm
apenas `-`. Use `nginx_proxy_client_overhead`; `nginx_proxy_overhead` é somente
um alias para compatibilidade com campanhas antigas.

### T2 e T3 aparecem sobrepostos ou T3 fica zerado

O mapeamento atual é disjunto: T2 corresponde a
`nginx_proxy_client_overhead` e T3 a `nginx_worker_connection_latency`. Não use
`nginx_proxy_worker_allocation_latency`; essa métrica antiga somava T2 à
conexão e foi removida. Para corrigir resultados existentes sem repetir a
carga, execute `experiments/reanalyze_existing_jaeger.py` conforme o
[guia do Jaeger](experiments/jaeger.md#reanalisar-campanhas-com-a-definicao-disjunta-de-t2t3).

T3 igual a zero é esperado quando o NGINX reutiliza a conexão upstream. Isso
não significa ausência de seleção ou encaminhamento: apenas indica que
`$upstream_connect_time`, com resolução de milissegundos, não observou uma nova
conexão. Consulte `DATA_QUALITY.md` antes de interpretar a métrica.

### Observações de despacho são inválidas por `clock skew`

`replication_dispatch_latency` compara timestamps de workers diferentes.
Sincronize os hosts com NTP/chrony. O coletor marca diferenças impossíveis como
inválidas e não as inclui na média.

### Coleta demora depois que o Locust termina

Essa etapa consulta e deduplica spans após a janela. Ajuste
`--jaeger-workers`, `--jaeger-chunk-duration` e `--jaeger-timeout` com cuidado;
mais paralelismo também aumenta a pressão sobre o Jaeger. Consulte o
[guia completo](experiments/jaeger.md).

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
