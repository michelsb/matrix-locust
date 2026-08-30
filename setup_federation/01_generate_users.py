#!/usr/bin/env python3
"""
Passo 1/7 (federação) — Gera os usuários de TODOS os homeservers.

Gera um único `users.csv` contendo os usuários dos dois (ou mais)
homeservers, distinguidos pelo prefixo do username:

    username,password
    userh01.000000,<senha>
    userh02.000000,<senha>

Por que o prefixo importa: é ele que diz a que homeserver cada usuário
pertence. Todos os passos seguintes usam esse prefixo para decidir onde
registrar o usuário, onde fazer login e qual domínio usar no Matrix ID
(ver setup_federation/common_fed.py).

IMPORTANTE — por que aqui NÃO usamos o sufixo ":dominio":
O `setup/` de servidor único grava o username como "localpart:dominio".
Esse sufixo é interpretado pelo próprio Locust
(`MatrixUser.set_user`), que reescreve o host do usuário para
`https://matrix.<dominio>` -- uma convenção fixa no código. Se a URL do
seu homeserver não seguir exatamente o padrão `matrix.<dominio>`
(ex.: `srv.home01-stg...`), esse recurso aponta para um host inexistente.
Por isso aqui gravamos apenas o localpart, e o roteamento é feito pelos
scripts de setup, que conhecem a URL real de cada homeserver.

Exemplos:
    python3 setup_federation/01_generate_users.py 1000
    python3 setup_federation/01_generate_users.py 1000 --seed 42
    python3 setup_federation/01_generate_users.py --per-homeserver 500
"""

import argparse
import csv
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common_fed import describe, get_logger, load_federation_config  # noqa: E402

PASSWORD_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
PASSWORD_LENGTH = 16


def generate_password() -> str:
    # `secrets` é criptograficamente seguro (ao contrário de `random`).
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def main():
    fedcfg = load_federation_config()
    log = get_logger("fed.generate_users", fedcfg.base.log_level)

    default_total = int(os.environ.get("NUM_USERS", 1000))

    parser = argparse.ArgumentParser(
        description="Gera os usuários de teste de todos os homeservers federados")
    parser.add_argument("num_users", type=int, nargs="?", default=None,
                        help=f"Total de usuários, dividido igualmente entre os "
                             f"homeservers (padrão: NUM_USERS={default_total})")
    parser.add_argument("--per-homeserver", type=int, default=None,
                        help="Quantidade de usuários POR homeserver "
                             "(alternativa a num_users)")
    parser.add_argument("-o", "--output", type=Path, default=fedcfg.users_csv,
                        help=f"Arquivo .csv de saída (padrão: {fedcfg.users_csv})")
    parser.add_argument("--pad", type=int, default=6,
                        help="Dígitos de preenchimento no username (padrão: 6 -> userh01.000042)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Semente aleatória (afeta apenas a ordem, não as senhas)")
    args = parser.parse_args()

    describe(fedcfg, log)

    num_hs = len(fedcfg.homeservers)
    if args.per_homeserver is not None:
        per_hs = args.per_homeserver
    else:
        total = args.num_users or default_total
        per_hs = total // num_hs
        if per_hs * num_hs != total:
            log.warning("%d usuários não divide igualmente entre %d homeservers; "
                        "usando %d por homeserver (total %d).",
                        total, num_hs, per_hs, per_hs * num_hs)
    if per_hs < 1:
        sys.exit("ERRO: é preciso pelo menos 1 usuário por homeserver.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(args.output, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["username", "password"])
        writer.writeheader()
        for hs in fedcfg.homeservers.values():
            for i in range(per_hs):
                username = f"{hs.prefix}.{i:0{args.pad}d}"
                writer.writerow({"username": username, "password": generate_password()})
                written += 1
            log.info("  %s: %d usuários (%s.%s ... %s)",
                      hs.key, per_hs, hs.prefix, "0" * args.pad,
                      hs.mxid(f"{hs.prefix}.{per_hs - 1:0{args.pad}d}"))

    log.info("Gerados %d usuários no total em %s", written, args.output)


if __name__ == "__main__":
    main()
