# Executar com Docker

O container executa o mesmo `experiments/run_factorial.py` documentado no
[runbook](experiments/runbook.md). A imagem usa o código local, instala as
versões travadas em `poetry.lock` e roda como usuário sem privilégios.

## Pré-requisitos

- Docker Engine ou Docker Desktop;
- dataset preparado em `data/`;
- diretório `results/` gravável pelo usuário do container;
- imagens JPG em `images/` quando `text_and_image` for usado.

## Construir a imagem

No Linux ou WSL, use seu UID/GID para que os resultados não sejam criados como
`root`:

```console
docker build \
  --build-arg UID="$(id -u)" \
  --build-arg GID="$(id -g)" \
  -t matrix-locust:local .
```

No Docker Desktop, os valores padrão `1000:1000` normalmente são suficientes:

```console
docker build -t matrix-locust:local .
```

Confirme a imagem:

```console
docker run --rm matrix-locust:local --help
```

## Smoke test sem Prometheus

```console
mkdir -p results

docker run --rm \
  -v "$PWD/data:/app/data:ro" \
  -v "$PWD/images:/app/images:ro" \
  -v "$PWD/results:/app/results" \
  matrix-locust:local \
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
  --output-dir results/docker-smoke
```

Os caminhos informados ao runner são caminhos internos do container. Os
artefatos aparecem no host em `results/docker-smoke/`.

## Campanha com Prometheus no host

Docker Desktop disponibiliza o host como `host.docker.internal`:

```console
docker run --rm \
  -v "$PWD/data:/app/data:ro" \
  -v "$PWD/images:/app/images:ro" \
  -v "$PWD/results:/app/results" \
  matrix-locust:local \
  --host https://matrix-test.atlab.ufc.br \
  --prometheus-url http://host.docker.internal:9091 \
  --instance matrix-test.atlab.ufc.br \
  --data-dir data/homeserver \
  --output-dir results/docker-factorial
```

No Docker Engine para Linux, acrescente o mapeamento do host:

```console
--add-host host.docker.internal:host-gateway
```

Se o Prometheus estiver em outro servidor, use seu endereço diretamente. O
serviço precisa escutar em uma interface acessível ao container; `127.0.0.1`
dentro do container aponta para o próprio container.

## Execução manual do Locust

Substitua o entrypoint padrão:

```console
docker run --rm \
  --entrypoint locust \
  -p 8089:8089 \
  -v "$PWD/data:/app/data:ro" \
  -v "$PWD/images:/app/images:ro" \
  -e MATRIX_DATA_DIR=data/homeserver \
  -e MATRIX_PERSIST_TOKENS=false \
  matrix-locust:local \
  -f locust-run-users.py \
  --host https://matrix-test.atlab.ufc.br
```

A interface fica em `http://localhost:8089`.

## Dois homeservers federados

Execute um container por homeserver, com datasets e resultados separados. Por
exemplo, acrescente `-d` para deixá-los em background:

```console
docker run -d --name matrix-load-home01 \
  -v "$PWD/data:/app/data:ro" \
  -v "$PWD/images:/app/images:ro" \
  -v "$PWD/results:/app/results" \
  matrix-locust:local \
  --host https://srv.home01.example.com \
  --data-dir data/federation/exports/home01 \
  --loads 25 50 75 \
  --no-prometheus \
  --output-dir results/docker-federation-home01

docker run -d --name matrix-load-home02 \
  -v "$PWD/data:/app/data:ro" \
  -v "$PWD/images:/app/images:ro" \
  -v "$PWD/results:/app/results" \
  matrix-locust:local \
  --host https://srv.home02.example.com \
  --data-dir data/federation/exports/home02 \
  --loads 25 50 75 \
  --no-prometheus \
  --output-dir results/docker-federation-home02
```

Acompanhe e aguarde:

```console
docker logs -f matrix-load-home01
docker wait matrix-load-home01 matrix-load-home02
docker rm matrix-load-home01 matrix-load-home02
```

Veja as regras de carga global e sincronização em
[experiments/federation.md](experiments/federation.md).

## Permissões de resultados

Se o container não puder escrever no bind mount, reconstrua a imagem com seu
UID/GID ou ajuste a propriedade do diretório `results/`. Evite executar a
imagem como `root` apenas para contornar permissões.

## Acesso negado ao daemon

Antes do build, este comando precisa funcionar sem `sudo`:

```console
docker info
```

No Docker Desktop com WSL, habilite a integração para a distribuição utilizada
e reabra a sessão WSL. No Docker Engine nativo, adicione o usuário ao grupo
`docker` conforme a política do ambiente e inicie uma nova sessão. Evite tornar
`/var/run/docker.sock` gravável por todos: acesso ao socket equivale a controle
administrativo do host.

## Atualizar dependências ou código

Reconstrua a imagem depois de alterar `poetry.lock`, `pyproject.toml` ou o
código:

```console
docker build -t matrix-locust:local .
```

O `.dockerignore` impede o envio de credenciais, datasets, resultados, caches e
ambientes virtuais locais para o contexto de build.
