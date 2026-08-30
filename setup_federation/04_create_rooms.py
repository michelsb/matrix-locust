#!/usr/bin/env python3
"""
Passo 4/7 (federação) — Cria as salas e convida os membros remotos.

Para cada sala de `rooms.json`:
  * o PRIMEIRO usuário da lista é o criador, e a sala é criada no
    homeserver DELE (é lá que a sala passa a existir de fato);
  * os demais membros são convidados pelo MXID completo, cada um com o
    domínio do seu próprio homeserver
    (ex.: @userh02.000005:home02-stg.exemplo.br).

Convidar um MXID de outro domínio é o primeiro momento em que a
federação é exercitada: o homeserver do criador precisa resolver o
servidor remoto (.well-known / DNS SRV), abrir a conexão
server-to-server e entregar o convite. Se a federação estiver mal
configurada, é aqui que aparece o primeiro erro.

Melhorias em relação ao `federation/setup_federation.py` original:
  * não limita a 10 salas (o original tinha um `[:10]` fixo);
  * reaproveita o access_token do passo 3 em vez de logar a cada sala;
  * é reexecutável (pula salas já criadas, ver rooms_status.csv);
  * paraleliza a criação e grava o progresso incrementalmente;
  * registra, por sala, quais homeservers participam.

Exemplos:
    python3 setup_federation/04_create_rooms.py
    python3 setup_federation/04_create_rooms.py --only-federated
    python3 setup_federation/04_create_rooms.py --limit 10   # teste rápido
"""

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common_fed import (  # noqa: E402
    build_session, describe, get_logger, load_federation_config,
    read_csv_rows, write_csv_rows,
)

STATUS_FIELDS = ["room_name", "creator", "creator_hs", "room_id",
                  "homeservers", "num_invited", "federated", "status", "error"]
FLUSH_EVERY = 25


class TokenCache:
    """Resolve e memoiza o access_token de cada criador, evitando um
    login por sala quando o mesmo usuário cria várias."""

    def __init__(self, session, fedcfg, tokens, passwords, log):
        self.session = session
        self.fedcfg = fedcfg
        self.tokens = dict(tokens)
        self.passwords = passwords
        self.log = log
        self.lock = threading.Lock()

    def get(self, hs, username):
        with self.lock:
            token = self.tokens.get(username)
        if token:
            return token, None

        password = self.passwords.get(username)
        if not password:
            return None, f"{username} sem access_token e sem senha"

        url = f"{hs.url}/_matrix/client/v3/login"
        payload = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": username},
            "password": password,
        }
        resp = self.session.post(url, json=payload, timeout=self.fedcfg.base.http_timeout)
        if resp.status_code != 200:
            return None, f"[{hs.key}] login HTTP {resp.status_code}: {resp.text[:200]}"
        token = resp.json()["access_token"]
        with self.lock:
            self.tokens[username] = token
        return token, None


def build_room_plan(fedcfg, room_name, members):
    """Resolve criador, convidados e homeservers envolvidos numa sala."""
    creator = members[0]
    creator_hs = fedcfg.homeserver_for(creator)
    if creator_hs is None:
        return None, f"criador '{creator}' não casa com nenhum prefixo de homeserver"

    invitees, hs_keys = [], {creator_hs.key}
    for username in members[1:]:
        hs = fedcfg.homeserver_for(username)
        if hs is None:
            continue
        invitees.append(hs.mxid(username))
        hs_keys.add(hs.key)

    return {
        "room_name": room_name,
        "creator": creator,
        "creator_hs": creator_hs,
        "invitees": invitees,
        "homeservers": sorted(hs_keys),
        "federated": len(hs_keys) > 1,
    }, None


def create_room(session, fedcfg, tokens, plan, preset, log):
    hs = plan["creator_hs"]
    base = {
        "room_name": plan["room_name"],
        "creator": plan["creator"],
        "creator_hs": hs.key,
        "homeservers": "|".join(plan["homeservers"]),
        "num_invited": len(plan["invitees"]),
        "federated": "yes" if plan["federated"] else "no",
    }

    token, error = tokens.get(hs, plan["creator"])
    if not token:
        return {**base, "room_id": "", "status": "failed", "error": error}

    resp = session.post(
        f"{hs.url}/_matrix/client/v3/createRoom",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": plan["room_name"],
            "topic": f"Teste de federação: {plan['room_name']}",
            "invite": plan["invitees"],
            "preset": preset,
            "is_direct": False,
        },
        timeout=fedcfg.base.http_timeout,
    )

    if resp.status_code == 200:
        room_id = resp.json().get("room_id", "")
        log.debug("[%s] sala '%s' criada (%s) com %d convites",
                  hs.key, plan["room_name"], room_id, len(plan["invitees"]))
        return {**base, "room_id": room_id, "status": "created", "error": ""}

    return {**base, "room_id": "", "status": "failed",
            "error": f"[{hs.key}] HTTP {resp.status_code}: {resp.text[:250]}"}


def main():
    fedcfg = load_federation_config()
    log = get_logger("fed.create_rooms", fedcfg.base.log_level)

    parser = argparse.ArgumentParser(description="Cria as salas federadas nos homeservers")
    parser.add_argument("-i", "--input", type=Path, default=fedcfg.rooms_json,
                        help=f"Arquivo .json de salas (padrão: {fedcfg.rooms_json})")
    parser.add_argument("--users", type=Path, default=fedcfg.users_csv,
                        help=f"users.csv, para login de fallback (padrão: {fedcfg.users_csv})")
    parser.add_argument("--tokens", type=Path, default=fedcfg.tokens_csv,
                        help=f"tokens.csv do passo 3 (padrão: {fedcfg.tokens_csv})")
    parser.add_argument("-o", "--output", type=Path, default=fedcfg.rooms_status_csv,
                        help=f"Arquivo .csv de status (padrão: {fedcfg.rooms_status_csv})")
    parser.add_argument("--only-federated", action="store_true",
                        help="Cria apenas as salas que têm membros de mais de um homeserver")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cria no máximo N salas (útil para um teste rápido de fumaça)")
    parser.add_argument("--preset", default="private_chat",
                        choices=["private_chat", "public_chat", "trusted_private_chat"],
                        help="Preset da sala (padrão: private_chat)")
    parser.add_argument("--force", action="store_true",
                        help="Recria mesmo as salas já criadas com sucesso")
    parser.add_argument("--workers", type=int, default=fedcfg.base.workers,
                        help=f"Threads simultâneas (padrão: {fedcfg.base.workers})")
    args = parser.parse_args()

    describe(fedcfg, log)

    if not args.input.exists():
        sys.exit(f"ERRO: {args.input} não encontrado. Rode o passo 2 primeiro.")
    with open(args.input, "r", encoding="utf-8") as f:
        rooms = json.load(f)

    tokens_by_user = {r["username"]: r["access_token"]
                       for r in read_csv_rows(args.tokens) if r.get("access_token")}
    passwords = {r["username"]: r["password"] for r in read_csv_rows(args.users)}
    if not tokens_by_user:
        log.warning("Nenhum token em %s -- todos os criadores farão login por senha. "
                    "Rode o passo 3 antes, se possível.", args.tokens)

    results = {r["room_name"]: r for r in read_csv_rows(args.output)}
    done_ok = {name for name, r in results.items() if r.get("status") == "created"}

    plans = []
    for name, members in rooms.items():
        if len(members) < 2:
            continue
        if not args.force and name in done_ok:
            continue
        plan, error = build_room_plan(fedcfg, name, members)
        if plan is None:
            log.warning("Sala '%s' ignorada: %s", name, error)
            continue
        if args.only_federated and not plan["federated"]:
            continue
        plans.append(plan)

    if not args.force and done_ok:
        log.info("%d salas já criadas, pulando (use --force para recriar)", len(done_ok))
    if args.limit:
        plans = plans[:args.limit]
        log.info("--limit aplicado: %d salas nesta execução", len(plans))
    if not plans:
        log.info("Nada a fazer: nenhuma sala pendente.")
        return

    num_fed = sum(1 for p in plans if p["federated"])
    log.info("Criando %d salas (%d federadas) com %d workers...",
              len(plans), num_fed, args.workers)

    session = build_session(fedcfg.base)
    tokens = TokenCache(session, fedcfg, tokens_by_user, passwords, log)
    lock = threading.Lock()

    def flush():
        write_csv_rows(args.output, STATUS_FIELDS, [results[k] for k in sorted(results)])

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(create_room, session, fedcfg, tokens, p, args.preset, log)
                    for p in plans]
        completed = 0
        for future in as_completed(futures):
            row = future.result()
            with lock:
                results[row["room_name"]] = row
                if row["status"] != "created":
                    log.warning("Falha na sala '%s': %s", row["room_name"], row["error"])
                completed += 1
                if completed % FLUSH_EVERY == 0:
                    flush()
                    log.info("Progresso: %d/%d", completed, len(plans))

    flush()
    created = sum(1 for r in results.values() if r["status"] == "created")
    created_fed = sum(1 for r in results.values()
                       if r["status"] == "created" and r.get("federated") == "yes")
    failed = len(results) - created
    log.info("Concluído. Criadas: %d (federadas: %d) | Falhas: %d | Detalhes em %s",
              created, created_fed, failed, args.output)
    if created_fed == 0:
        log.warning("NENHUMA sala federada foi criada -- o teste de carga não vai gerar "
                    "tráfego de federação. Confira os prefixos/domínios no .env.")


if __name__ == "__main__":
    main()
