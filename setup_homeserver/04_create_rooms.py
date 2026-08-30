#!/usr/bin/env python3
"""
Passo 4/5 — Cria as salas no homeserver e convida os membros.

Lê a distribuição de salas (padrão: rooms.json) e, para cada sala, usa
o primeiro usuário da lista como criador e convida os demais via
`POST /_matrix/client/v3/createRoom`.

Diferença importante em relação ao script original: o criador de cada
sala é autenticado usando o access_token já obtido no passo 3
(tokens.csv), em vez de fazer login com usuário/senha a cada sala.
Isso evita uma chamada de login inteira por sala (uma economia grande
quando o mesmo usuário cria várias salas) e só recorre ao login por
senha (users.csv) se o usuário não tiver token disponível.

É reexecutável: salas já criadas com sucesso (rooms_status.csv) são
puladas por padrão (use --force para recriar tudo).

Exemplos:
    python setup/04_create_rooms.py
    python setup/04_create_rooms.py --force
"""

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    build_session, get_logger, load_config, mxid, read_csv_rows,
    split_username, write_csv_rows,
)

STATUS_FIELDS = ["room_name", "creator", "room_id", "num_invited", "status", "error"]
FLUSH_EVERY = 25


def parse_args(cfg):
    parser = argparse.ArgumentParser(description="Cria as salas no homeserver Matrix")
    parser.add_argument("-i", "--input", type=Path, default=cfg.rooms_json,
                        help=f"Arquivo .json de salas (padrão: {cfg.rooms_json})")
    parser.add_argument("--users", type=Path, default=cfg.users_csv,
                        help=f"Arquivo .csv de usuários, para login de fallback (padrão: {cfg.users_csv})")
    parser.add_argument("--tokens", type=Path, default=cfg.tokens_csv,
                        help=f"Arquivo .csv de tokens gerado no passo 3 (padrão: {cfg.tokens_csv})")
    parser.add_argument("-o", "--output", type=Path, default=cfg.rooms_status_csv,
                        help=f"Arquivo .csv com o status de cada sala (padrão: {cfg.rooms_status_csv})")
    parser.add_argument("--force", action="store_true",
                        help="Recria mesmo as salas já marcadas como criadas com sucesso")
    parser.add_argument("--workers", type=int, default=cfg.workers,
                        help=f"Threads simultâneas de criação de salas (padrão: {cfg.workers})")
    return parser.parse_args()


class TokenCache:
    """Resolve e cacheia o access_token de cada criador de sala, evitando
    logins repetidos quando o mesmo usuário cria várias salas."""

    def __init__(self, session, cfg, tokens_by_user, passwords_by_user, log):
        self.session = session
        self.cfg = cfg
        self.tokens = dict(tokens_by_user)
        self.passwords = passwords_by_user
        self.log = log
        self.lock = threading.Lock()

    def get_token(self, local_username):
        with self.lock:
            token = self.tokens.get(local_username)
        if token:
            return token, None

        password = self.passwords.get(local_username)
        if not password:
            return None, f"sem access_token e sem senha para {local_username}"

        url = f"{self.cfg.matrix_server}/_matrix/client/v3/login"
        payload = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": mxid(local_username, self.cfg.domain)},
            "password": password,
        }
        resp = self.session.post(url, json=payload, timeout=self.cfg.http_timeout)
        if resp.status_code != 200:
            return None, f"login HTTP {resp.status_code}: {resp.text[:200]}"

        token = resp.json()["access_token"]
        with self.lock:
            self.tokens[local_username] = token
        return token, None


def create_room(session, cfg, tokens, room_name, member_usernames, log):
    creator_raw, invitee_raws = member_usernames[0], member_usernames[1:]
    creator_local, _ = split_username(creator_raw)

    token, error = tokens.get_token(creator_local)
    if not token:
        return {"room_name": room_name, "creator": creator_local, "room_id": "",
                "num_invited": 0, "status": "failed", "error": error}

    invitees = [mxid(u, cfg.domain) for u in invitee_raws]
    url = f"{cfg.matrix_server}/_matrix/client/v3/createRoom"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"name": room_name, "invite": invitees}
    resp = session.post(url, headers=headers, json=payload, timeout=cfg.http_timeout)

    if resp.status_code == 200:
        room_id = resp.json().get("room_id", "")
        log.debug("Sala '%s' criada (%s) por %s com %d convites",
                  room_name, room_id, creator_local, len(invitees))
        return {"room_name": room_name, "creator": creator_local, "room_id": room_id,
                "num_invited": len(invitees), "status": "created", "error": ""}

    error = f"HTTP {resp.status_code}: {resp.text[:200]}"
    return {"room_name": room_name, "creator": creator_local, "room_id": "",
            "num_invited": len(invitees), "status": "failed", "error": error}


def main():
    cfg = load_config()
    log = get_logger("create_rooms", cfg.log_level)
    args = parse_args(cfg)

    if not args.input.exists():
        sys.exit(f"ERRO: {args.input} não encontrado. Rode primeiro o passo 2 (02_generate_rooms.py).")
    with open(args.input, "r", encoding="utf-8") as f:
        rooms = json.load(f)

    tokens_by_user = {row["username"]: row["access_token"]
                       for row in read_csv_rows(args.tokens) if row.get("access_token")}
    passwords_by_user = {}
    for row in read_csv_rows(args.users):
        local, _ = split_username(row["username"])
        passwords_by_user[local] = row["password"]

    if not tokens_by_user:
        log.warning("Nenhum token encontrado em %s -- todos os logins serão feitos por "
                    "senha (mais lento). Rode o passo 3 (03_register_users.py) antes, "
                    "se ainda não tiver feito.", args.tokens)

    already_done = set()
    if not args.force:
        for row in read_csv_rows(args.output):
            if row.get("status") == "created":
                already_done.add(row["room_name"])

    pending = {name: members for name, members in rooms.items() if name not in already_done}
    skipped = len(rooms) - len(pending)
    if skipped:
        log.info("%d salas já criadas em %s, pulando (use --force para recriar)",
                  skipped, args.output)
    if not pending:
        log.info("Nada a fazer: todas as salas já foram criadas.")
        return

    log.info("Criando %d salas em %s com %d workers...", len(pending), cfg.matrix_server, args.workers)

    session = build_session(cfg)
    tokens = TokenCache(session, cfg, tokens_by_user, passwords_by_user, log)
    results = {row["room_name"]: row for row in read_csv_rows(args.output)}

    lock = threading.Lock()

    def flush():
        write_csv_rows(args.output, STATUS_FIELDS,
                        [results[k] for k in sorted(results)])

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(create_room, session, cfg, tokens, name, members, log): name
            for name, members in pending.items() if len(members) >= 2
        }
        done = 0
        for future in as_completed(futures):
            row = future.result()
            with lock:
                results[row["room_name"]] = row
                if row["status"] != "created":
                    log.warning("Falha ao criar sala '%s': %s", row["room_name"], row["error"])
                done += 1
                if done % FLUSH_EVERY == 0:
                    flush()
                    log.info("Progresso: %d/%d", done, len(futures))

    flush()
    created = sum(1 for r in results.values() if r["status"] == "created")
    failed = len(results) - created
    log.info("Concluido. Criadas: %d | Falhas: %d | Detalhes em %s", created, failed, args.output)


if __name__ == "__main__":
    main()
