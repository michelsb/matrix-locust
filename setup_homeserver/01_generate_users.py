#!/usr/bin/env python3
"""
Passo 1/5 — Gera a lista de usuários de teste.

Cria um arquivo CSV (padrão: users.csv) com as colunas
"username,password" para N usuários fictícios. Esse arquivo é a base
para todos os passos seguintes (registro, salas e convites) e também é
lido diretamente pelo próprio Locust durante o teste de carga.

Exemplos:
    python setup/01_generate_users.py
    python setup/01_generate_users.py 5000
    python setup/01_generate_users.py 5000 --seed 42
    python setup/01_generate_users.py 2000 \\
        --domains matrix-a.example.com,matrix-b.example.com \\
        --weights 0.7,0.3
"""

import argparse
import csv
import os
import random
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import get_logger, load_config  # noqa: E402

PASSWORD_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
PASSWORD_LENGTH = 16


def generate_password() -> str:
    # `secrets` é seguro para gerar credenciais (ao contrário de `random`).
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def parse_args(cfg, default_num_users):
    parser = argparse.ArgumentParser(
        description="Gera uma lista de usuários Matrix de teste em um arquivo .csv")
    parser.add_argument("num_users", type=int, nargs="?", default=None,
                        help="Quantidade de usuários a gerar "
                             f"(padrão: variável NUM_USERS, atualmente {default_num_users})")
    parser.add_argument("-o", "--output", type=Path, default=cfg.users_csv,
                        help=f"Arquivo .csv de saída (padrão: {cfg.users_csv})")
    parser.add_argument("-d", "--domains", default=None,
                        type=lambda s: [d.strip() for d in s.split(",") if d.strip()],
                        help="Domínios para testes federados, separados por vírgula")
    parser.add_argument("-w", "--weights", default=None,
                        type=lambda s: [float(x) for x in s.split(",")],
                        help="Pesos de distribuição entre os domínios (mesma ordem de --domains)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Semente aleatória, para gerar sempre o mesmo conjunto de usuários")
    return parser.parse_args()


def main():
    cfg = load_config(require_server=False)
    log = get_logger("generate_users", cfg.log_level)
    default_num_users = int(os.environ.get("NUM_USERS", 1000))
    args = parse_args(cfg, default_num_users)

    num_users = args.num_users or default_num_users

    domains = args.domains
    weights = args.weights
    if domains is None and os.environ.get("USER_DOMAINS"):
        domains = [d.strip() for d in os.environ["USER_DOMAINS"].split(",") if d.strip()]
    if weights is None and os.environ.get("USER_DOMAIN_WEIGHTS"):
        weights = [float(x) for x in os.environ["USER_DOMAIN_WEIGHTS"].split(",")]

    if weights and domains and len(weights) != len(domains):
        sys.exit("ERRO: --domains e --weights precisam ter a mesma quantidade de itens.")

    if args.seed is not None:
        random.seed(args.seed)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["username", "password"])
        writer.writeheader()
        for i in range(num_users):
            host = random.choices(domains, weights)[0] if domains else ""
            username = "user.test{:06d}:{}".format(i, host)
            password = generate_password()
            writer.writerow({"username": username, "password": password})

    log.info("Gerados %d usuários em %s", num_users, args.output)
    if domains:
        log.info("Domínios utilizados: %s", ", ".join(domains))


if __name__ == "__main__":
    main()
