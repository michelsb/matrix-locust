#!/usr/bin/env python3
"""
Passo 2/5 — Gera a distribuição de salas e seus membros.

Lê o arquivo de usuários (padrão: users.csv) e cria um arquivo JSON
(padrão: rooms.json) descrevendo um conjunto de salas e quais usuários
participam de cada uma. O tamanho das salas segue uma distribuição de
Pareto ("regra 80/20"), para imitar o comportamento real de servidores
de chat: a maioria das salas é pequena, mas algumas poucas são bem
grandes.

Exemplos:
    python setup/02_generate_rooms.py
    python setup/02_generate_rooms.py --seed 42
"""

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import get_logger, load_config, read_csv_rows  # noqa: E402

PARETO_ALPHA = 1.161  # "regra 80/20". Ver: https://en.wikipedia.org/wiki/Pareto_distribution


def parse_args(cfg):
    parser = argparse.ArgumentParser(
        description="Gera a distribuição de salas a partir de um arquivo de usuários")
    parser.add_argument("-i", "--input", type=Path, default=cfg.users_csv,
                        help=f"Arquivo .csv de usuários de entrada (padrão: {cfg.users_csv})")
    parser.add_argument("-o", "--output", type=Path, default=cfg.rooms_json,
                        help=f"Arquivo .json de salas de saída (padrão: {cfg.rooms_json})")
    parser.add_argument("--seed", type=int, default=None,
                        help="Semente aleatória, para gerar sempre a mesma distribuição de salas")
    return parser.parse_args()


def main():
    cfg = load_config(require_server=False)
    log = get_logger("generate_rooms", cfg.log_level)
    args = parse_args(cfg)

    if args.seed is not None:
        random.seed(args.seed)

    rows = read_csv_rows(args.input)
    if not rows:
        sys.exit(f"ERRO: nenhum usuário encontrado em {args.input}. "
                  f"Rode primeiro o passo 1 (01_generate_users.py).")
    users = [row["username"] for row in rows]
    num_users = len(users)
    log.info("%d usuários carregados de %s", num_users, args.input)

    # Gera tamanhos de sala a partir de uma distribuição de Pareto,
    # descartando salas com menos de 2 membros.
    room_sizes = []
    for _ in range(num_users):
        size = round(random.paretovariate(PARETO_ALPHA))
        size = min(size, num_users)
        if size < 2:
            continue
        room_sizes.append(size)

    if not room_sizes:
        sys.exit("ERRO: nenhuma sala com 2+ membros foi gerada. Rode novamente "
                  "ou aumente a quantidade de usuários.")

    room_members = {}
    for i, size in enumerate(room_sizes):
        room_members[f"Room {i}"] = random.sample(users, size)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as jsonfile:
        json.dump(room_members, jsonfile)

    avg = sum(room_sizes) / len(room_sizes)
    log.info("%d salas geradas em %s (min=%d, max=%d, media=%.1f)",
              len(room_sizes), args.output, min(room_sizes), max(room_sizes), avg)

    # Estatísticas de participação, só para visibilidade do operador.
    membership_count = {}
    for members in room_members.values():
        for member in members:
            membership_count[member] = membership_count.get(member, 0) + 1
    roomless = sum(1 for u in users if membership_count.get(u, 0) == 0)
    log.info("%d usuários não foram colocados em nenhuma sala", roomless)


if __name__ == "__main__":
    main()
