# Configuração de tracing no Synapse e no NGINX

Este guia configura a coleta independente de spans do NGINX e do Synapse usada
pelo coletor de experimentos. Ele complementa o
[guia de execução](../experiments/jaeger.md): aqui ficam as mudanças da
infraestrutura; lá ficam os comandos e os artefatos da campanha.

> Aplique primeiro em ambiente de teste. Tracing aumenta CPU, rede e
> armazenamento e pode registrar caminhos, nomes de operações e metadados.

## Arquitetura e portas

```text
Locust ──HTTP──> NGINX ──HTTP──> workers Synapse
                  │                   │
             OTLP/gRPC          Jaeger Agent
                  └────────┬──────────┘
                           ▼
                     Jaeger backend
                           │
                    Query API :16686
```

Não envie spans para `16686`: essa é a porta da interface e da Query API usada
pelo `run_factorial.py`. O exportador NGINX deve apontar para o receptor
OTLP/gRPC do backend ou de um OpenTelemetry Collector, normalmente `4317`. O
Synapse OpenTracing normalmente envia ao Jaeger Agent, frequentemente por UDP
`6831`. Confirme as portas realmente expostas no seu deployment.

## 1. Configurar todos os processos Synapse

O bloco precisa existir na configuração efetiva do processo principal e de
cada worker que participará dos traces:

```yaml
# Habilita os spans internos do Synapse.
opentracing:
  enabled: true

  # Restrinja em produção aos homeservers realmente confiáveis.
  homeserver_whitelist:
    - ".*"

  jaeger_config:
    sampler:
      # Amostra aproximadamente 10% dos traces iniciados neste processo.
      type: probabilistic
      param: 0.1
    logging: false

    # Omita se JAEGER_AGENT_HOST/JAEGER_AGENT_PORT forem fornecidos ao pod.
    local_agent:
      reporting_host: jaeger-agent.observability.svc.cluster.local
      reporting_port: 6831
```

`homeserver_whitelist: [".*"]` é conveniente no laboratório, mas permissivo
demais para produção. Se os workers herdam o arquivo principal, evite blocos
divergentes nos arquivos individuais. Reinicie todos os processos depois da
alteração e confirme nos logs que o tracer foi inicializado sem erro.

O Synapse decide seu sampling internamente. Não é necessário receber contexto
de tracing do Locust ou do NGINX para as métricas mantidas pelo projeto.

## 2. Habilitar o módulo OpenTelemetry no NGINX

O NGINX precisa conter o módulo `ngx_otel_module`. Em instalações com módulo
dinâmico, carregue-o no início do `nginx.conf`, fora do bloco `http`:

```nginx
# Disponibiliza otel_exporter, otel_trace e as demais diretivas OTel.
load_module modules/ngx_otel_module.so;
```

Se aparecer `unknown directive "otel_exporter"`, a imagem não possui ou não
carregou o módulo. Instale o pacote compatível com a mesma versão do NGINX ou
use uma imagem que já o forneça.

## 3. Configuração global no `nginx.conf`

Coloque estas diretivas dentro de `http { ... }`. Não é necessário criar um
arquivo `00-opentelemetry.conf` separado:

```nginx
http {
    # Envia spans por OTLP/gRPC. Este endpoint não é a porta 16686 da GUI.
    otel_exporter {
        endpoint otel-collector.observability.svc.cluster.local:4317;
        interval 1s;
        batch_size 512;
        batch_count 4;
    }

    # Nome procurado automaticamente por --jaeger-metrics nginx_*.
    otel_service_name matrix-nginx;

    # Sampling independente e estável de aproximadamente 10% das requisições.
    split_clients "$request_id" $matrix_trace_enabled {
        10% on;
        *   off;
    }

    # ... upstreams, maps e servers existentes ...
}
```

NGINX e Synapse fazem amostragens independentes. Os resultados são agregados
por componente e não tentam subtrair timestamps de máquinas diferentes.

## 4. Instrumentar os locations Matrix

Adicione o bloco comentado aos locations que encaminham APIs Matrix, mantendo
as diretivas `proxy_pass` já existentes:

```nginx
location ~* ^(\/_matrix|\/_synapse\/client) {
    # Cria spans para a amostra independente definida no bloco http.
    otel_trace $matrix_trace_enabled;

    # Não extrai nem propaga contexto externo; cada componente mede durações locais.
    otel_trace_context ignore;

    # Nomes estáveis permitem que o coletor escolha serviço/operação sozinho.
    otel_span_name matrix-client-proxy;

    # Identificadores e tempos necessários às métricas nginx_*.
    otel_span_attr nginx.request_id $request_id;
    otel_span_attr nginx.request_time $request_time;
    otel_span_attr nginx.upstream_addr $upstream_addr;
    otel_span_attr nginx.upstream_connect_time $upstream_connect_time;
    otel_span_attr nginx.upstream_header_time $upstream_header_time;
    otel_span_attr nginx.upstream_response_time $upstream_response_time;
    # Volume e reutilização de conexão ajudam a interpretar transferências lentas.
    otel_span_attr nginx.request_length $request_length;
    otel_span_attr nginx.bytes_sent $bytes_sent;
    otel_span_attr nginx.connection $connection;
    otel_span_attr nginx.connection_requests $connection_requests;
    otel_span_attr matrix.worker_upstream $matrix_worker_upstream;

    proxy_pass http://$matrix_worker_upstream$request_uri;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Host $host;

    proxy_read_timeout 600s;
    client_max_body_size 64M;
}
```

`http.method`, `http.target` e `http.status_code` são atributos padrão do
módulo. Não use `$upstream_queue_time`: essa variável não existe no NGINX OSS e
faz o processo falhar com `unknown "upstream_queue_time" variable`.

Repita a instrumentação no `server` público e no interno caso ambos possam
receber a carga. Evite dois spans de proxy para a mesma passagem se um location
apenas redireciona internamente para o outro.

Para um smoke test curto com 100%, troque temporariamente `10% on` por
`100% on`. Restaure 10% antes da campanha definitiva.

## 5. Validar antes do experimento

Valide e recarregue o NGINX:

```console
nginx -t
nginx -s reload
```

Em Kubernetes, adapte para o nome do pod:

```console
kubectl exec -n synapse-separated deploy/reverse-proxy -- nginx -t
kubectl rollout restart -n synapse-separated deploy/reverse-proxy
kubectl logs -n synapse-separated deploy/reverse-proxy --tail=100
```

Confirme a API do Jaeger e os nomes esperados:

```console
curl -fsS http://10.101.53.46:16686/api/services
curl -fsSG http://10.101.53.46:16686/api/operations \
  --data-urlencode 'service=matrix-nginx'
```

A resposta deve incluir `matrix-nginx` e `matrix-client-proxy`. Depois execute
um smoke test do [runbook](../experiments/runbook.md#latencias-internas-com-jaeger).
Na célula resultante, valide:

```console
grep -E 'matrix-nginx|RoomSendEventRestServlet' \
  results/SEU-TESTE/users-50__text_only__rep-01/jaeger/spans.csv
```

O arquivo deve conter spans dos dois serviços, sem exigir que compartilhem o
mesmo `trace_id`. As colunas `nginx.*` devem estar preenchidas nos spans do
proxy.

## 6. Checklist

- todos os workers Synapse usam `opentracing.enabled: true`;
- os relógios dos nós estão sincronizados por NTP/chrony;
- NGINX possui e carrega `ngx_otel_module`;
- `otel_service_name` é `matrix-nginx`;
- `otel_span_name` é `matrix-client-proxy`;
- `split_clients` aplica sampling independente de 10% no NGINX;
- `otel_trace $matrix_trace_enabled` está no location Matrix;
- `otel_trace_context ignore` evita dependência de contexto externo;
- NGINX exporta para OTLP/gRPC, não para a porta `16686`;
- Jaeger Query responde em `/api/services` e `/api/operations`;
- `spans.csv` contém status, endpoint e tempos de upstream;
- NGINX e Synapse aparecem como séries independentes nos resultados.

Para interpretar fórmulas e filtros `all`, `sync`, `foreground` e por endpoint,
consulte o [catálogo de métricas](../experiments/metrics.md). Para falhas de
integração, consulte [solução de problemas](../troubleshooting.md#jaeger).

## Referências

- [Configuração OpenTracing do Synapse](https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html#opentracing)
- [Módulo OpenTelemetry oficial do NGINX](https://nginx.org/en/docs/ngx_otel_module.html)
