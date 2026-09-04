#!/usr/bin/env python3
"""
Passo 2/7 (federação) — Gera salas mistas entre os homeservers configurados.

Este é o passo que efetivamente "cria" o cenário de federação. Uma sala
só gera tráfego federado se tiver membros de mais de um homeserver:
quando um usuário do home01 manda mensagem numa sala onde há um usuário
do home02, o home01 precisa entregar esse evento ao home02 via
federação (server-to-server).

Gera `rooms.json` no mesmo formato do setup de servidor único, para
manter compatibilidade com o resto do projeto:

    { "Room 0": ["userh01.000001", "userh02.000005", ...], ... }

Cada sala federada recebe, por construção, pelo menos um usuário de
CADA homeserver configurado. O tamanho das salas segue uma distribuição
de Pareto (regra 80/20), como no cenário de servidor único.

Exemplos:
    python3 setup_federation/02_generate_rooms.py
    python3 setup_federation/02_generate_rooms.py --num-rooms 200 --seed 42
    python3 setup_federation/02_generate_rooms.py --federated-ratio 0.8
"""

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common_fed import (  # noqa: E402
    describe, get_logger, load_federation_config, read_csv_rows,
)

PARETO_ALPHA = 1.161  # regra 80/20


def main():
    fedcfg = load_federation_config()
    log = get_logger("fed.generate_rooms", fedcfg.base.log_level)

    parser = argparse.ArgumentParser(
        description="Gera salas com membros de múltiplos homeservers (federadas)")
    parser.add_argument("-i", "--input", type=Path, default=fedcfg.users_csv,
                        help=f"Arquivo .csv de usuários (padrão: {fedcfg.users_csv})")
    parser.add_argument("-o", "--output", type=Path, default=fedcfg.rooms_json,
                        help=f"Arquivo .json de saída (padrão: {fedcfg.rooms_json})")
    parser.add_argument("--num-rooms", type=int, default=None,
                        help="Quantidade de salas (padrão: número de usuários)")
    parser.add_argument("--federated-ratio", type=float, default=1.0,
                        help="Fração das salas que devem ser federadas, de 0 a 1 "
                             "(padrão: 1.0 = todas). O restante vira sala local, "
                             "com membros de um homeserver só -- útil como grupo de controle.")
    parser.add_argument("--max-room-size", type=int, default=None,
                        help="Limite de membros por sala (padrão: total de usuários)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Semente aleatória, para reproduzir a mesma distribuição")
    args = parser.parse_args()

    if not 0.0 <= args.federated_ratio <= 1.0:
        sys.exit("ERRO: --federated-ratio precisa estar entre 0 e 1.")

    if args.seed is not None:
        random.seed(args.seed)

    describe(fedcfg, log)

    rows = read_csv_rows(args.input)
    if not rows:
        sys.exit(f"ERRO: nenhum usuário encontrado em {args.input}. "
                  f"Rode primeiro o passo 1 (01_generate_users.py).")

    # Agrupa os usuários por homeserver, usando o prefixo do username.
    by_hs = defaultdict(list)
    unknown = []
    for row in rows:
        hs = fedcfg.homeserver_for(row["username"])
        if hs is None:
            unknown.append(row["username"])
        else:
            by_hs[hs.key].append(row["username"])

    if unknown:
        log.warning("%d usuários não casaram com nenhum prefixo de homeserver e "
                    "serão ignorados (ex.: %s)", len(unknown), unknown[:3])

    missing = [k for k in fedcfg.keys if not by_hs.get(k)]
    if missing:
        sys.exit(f"ERRO: não há usuários para o(s) homeserver(s) {missing}. "
                  f"Sem usuários de todos os participantes não é possível gerar as salas. "
                  f"Confira os prefixos em .env e rode o passo 1 novamente.")

    for key in fedcfg.keys:
        log.info("  %s: %d usuários disponíveis", key, len(by_hs[key]))

    total_users = sum(len(v) for v in by_hs.values())
    num_rooms = args.num_rooms or total_users
    max_size = args.max_room_size or total_users
    hs_keys = fedcfg.keys

    rooms = {}
    federated_count = 0
    for i in range(num_rooms):
        size = round(random.paretovariate(PARETO_ALPHA))
        size = max(2, min(size, max_size))

        make_federated = random.random() < args.federated_ratio
        # Uma sala federada precisa de pelo menos 1 membro de cada homeserver.
        if make_federated and size < len(hs_keys):
            size = len(hs_keys)

        if make_federated:
            members = []
            # 1) garante a presença de cada homeserver
            for key in hs_keys:
                members.append(random.choice(by_hs[key]))
            # 2) completa o restante das vagas sorteando de qualquer servidor
            remaining = size - len(members)
            if remaining > 0:
                pool = [u for key in hs_keys for u in by_hs[key] if u not in members]
                if pool:
                    members.extend(random.sample(pool, min(remaining, len(pool))))
            random.shuffle(members)
            federated_count += 1
        else:
            # Sala local: todos os membros do mesmo homeserver (controle).
            key = random.choice(hs_keys)
            pool = by_hs[key]
            members = random.sample(pool, min(size, len(pool)))
            if len(members) < 2:
                continue

        rooms[f"Room {i}"] = members

    if not rooms:
        sys.exit("ERRO: nenhuma sala pôde ser gerada. Aumente a quantidade de usuários.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(rooms, f)

    sizes = [len(m) for m in rooms.values()]
    log.info("%d salas geradas em %s (min=%d, max=%d, media=%.1f)",
              len(rooms), args.output, min(sizes), max(sizes), sum(sizes) / len(sizes))
    log.info("  federadas (membros de +1 homeserver): %d", federated_count)
    log.info("  locais (um homeserver só):            %d", len(rooms) - federated_count)


if __name__ == "__main__":
    main()
