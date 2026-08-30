#!/usr/bin/env python3
"""
Passo 6/7 (federação) — Separa users/tokens por homeserver para o Locust.

POR QUE ESTE PASSO EXISTE:
O `users.csv` e o `tokens.csv` do setup contêm os usuários dos DOIS
homeservers. Mas o Locust é executado com um `--host` único. Se você
apontar o Locust para o home01 usando o `users.csv` completo, metade
dos usuários (os do home02) vai tentar logar no servidor errado e
falhar — poluindo as métricas com erros que não têm nada a ver com
capacidade do servidor.

Este passo gera um diretório de dataset por homeserver:

    data/federation/exports/home01/users.csv
    data/federation/exports/home01/tokens.csv

Para rodar o teste, selecione o diretório com `MATRIX_DATA_DIR`. A flag
`--activate` imprime o comando adequado sem sobrescrever a massa completa.

TOPOLOGIAS DE TESTE (ver README para detalhes):
  A) Carga de um lado só (foi o que os scripts originais faziam):
     ativa home01 e roda o Locust com --host <URL do home01>. O tráfego
     de federação é gerado servidor-a-servidor, porque as salas têm
     membros do home02.
  B) Carga dos dois lados: rode duas instâncias do Locust, cada uma com
     o par de arquivos e o --host do seu homeserver.

Exemplos:
    poetry run python setup_federation/06_export_locust_files.py
    poetry run python setup_federation/06_export_locust_files.py --activate home01
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common_fed import (  # noqa: E402
    TOKENS_FIELDS, describe, get_logger, load_federation_config,
    read_csv_rows, write_csv_rows,
)

USERS_FIELDS = ["username", "password"]


def main():
    fedcfg = load_federation_config()
    log = get_logger("fed.export_locust", fedcfg.base.log_level)

    parser = argparse.ArgumentParser(
        description="Gera users/tokens separados por homeserver para o Locust")
    parser.add_argument("--users", type=Path, default=fedcfg.users_csv,
                        help=f"users.csv de entrada (padrão: {fedcfg.users_csv})")
    parser.add_argument("--tokens", type=Path, default=fedcfg.tokens_csv,
                        help=f"tokens.csv de entrada (padrão: {fedcfg.tokens_csv})")
    parser.add_argument("--outdir", type=Path, default=fedcfg.exports_dir,
                        help=f"Diretório de saída (padrão: {fedcfg.exports_dir})")
    parser.add_argument("--activate", metavar="HOMESERVER", default=None,
                        help="Exibe o comando Locust usando o dataset desse homeserver")
    args = parser.parse_args()

    describe(fedcfg, log)

    users = read_csv_rows(args.users)
    tokens = read_csv_rows(args.tokens)
    if not users:
        sys.exit(f"ERRO: nenhum usuário em {args.users}. Rode o passo 1 primeiro.")

    if args.activate and args.activate not in fedcfg.homeservers:
        sys.exit(f"ERRO: homeserver '{args.activate}' desconhecido. "
                  f"Disponíveis: {fedcfg.keys}")

    users_by_hs = defaultdict(list)
    tokens_by_hs = defaultdict(list)
    for row in users:
        hs = fedcfg.homeserver_for(row["username"])
        if hs:
            users_by_hs[hs.key].append(row)
    for row in tokens:
        hs = fedcfg.homeserver_for(row["username"])
        if hs:
            tokens_by_hs[hs.key].append(row)

    args.outdir.mkdir(parents=True, exist_ok=True)
    exported = {}
    for key, hs in fedcfg.homeservers.items():
        homeserver_dir = args.outdir / key
        homeserver_dir.mkdir(parents=True, exist_ok=True)
        users_path = homeserver_dir / "users.csv"
        tokens_path = homeserver_dir / "tokens.csv"

        write_csv_rows(users_path, USERS_FIELDS, users_by_hs.get(key, []))
        write_csv_rows(tokens_path, TOKENS_FIELDS, tokens_by_hs.get(key, []))
        exported[key] = (users_path, tokens_path)

        with_token = sum(1 for r in tokens_by_hs.get(key, []) if r.get("access_token"))
        log.info("%-8s -> %s (%d usuários) | %s (%d com token)",
                  key, users_path, len(users_by_hs.get(key, [])),
                  tokens_path, with_token)

    if args.activate:
        hs = fedcfg.homeservers[args.activate]
        data_dir = args.outdir / args.activate
        log.info("Dataset selecionado: %s", data_dir)
        log.info("Rode o teste de carga com:")
        log.info("  MATRIX_DATA_DIR=%s poetry run python run.py locust-run-users.py --host %s",
                 data_dir, hs.url)
    else:
        log.info("Para ativar um homeserver para o teste de carga, rode:")
        log.info("  poetry run python setup_federation/06_export_locust_files.py --activate %s",
                  fedcfg.keys[0])


if __name__ == "__main__":
    main()
