#!/usr/bin/env python3
"""
Passo 5/5 — Aceita os convites de sala pendentes para cada usuário.

Lê o arquivo de tokens (padrão: tokens.csv, gerado no passo 3) e, para
cada usuário: faz `/sync` para listar convites pendentes, entra
(`/join`) em cada sala convidada e atualiza `next_batch`/`access_token`
de volta no mesmo CSV.

Atualizar o `tokens.csv` com o `next_batch` mais recente é importante:
é o mesmo arquivo lido pelo teste de carga real
(matrix_locust/users/matrixuser.py), então isso evita que o próprio
Locust precise re-sincronizar todo o histórico ao iniciar o teste.

Exemplos:
    python setup/05_accept_invites.py
    python setup/05_accept_invites.py --workers 16
"""

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    build_session, get_logger, load_config, mxid, read_csv_rows,
    split_username, write_csv_rows,
)

TOKENS_FIELDS = ["username", "user_id", "access_token", "next_batch"]


def parse_args(cfg):
    parser = argparse.ArgumentParser(description="Aceita convites de sala pendentes para usuários Matrix")
    parser.add_argument("-i", "--input", type=Path, default=cfg.tokens_csv,
                        help=f"Arquivo .csv de tokens (padrão: {cfg.tokens_csv})")
    parser.add_argument("--users", type=Path, default=cfg.users_csv,
                        help=f"Arquivo .csv de usuários, para login de fallback (padrão: {cfg.users_csv})")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="Arquivo .csv de saída (padrão: sobrescreve o de entrada)")
    parser.add_argument("--workers", type=int, default=cfg.workers,
                        help=f"Usuários processados em paralelo (padrão: {cfg.workers})")
    return parser.parse_args()


def do_login(session, cfg, local_username, password):
    url = f"{cfg.matrix_server}/_matrix/client/v3/login"
    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": mxid(local_username, cfg.domain)},
        "password": password,
    }
    resp = session.post(url, json=payload, timeout=cfg.http_timeout)
    resp.raise_for_status()
    return resp.json()["access_token"]


def do_sync(session, cfg, token, since):
    url = f"{cfg.matrix_server}/_matrix/client/v3/sync"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"timeout": "0", "set_presence": "offline"}
    if since:
        params["since"] = since
    resp = session.get(url, headers=headers, params=params, timeout=cfg.http_timeout)
    resp.raise_for_status()
    return resp.json()


def join_room(session, cfg, token, room_id):
    url = f"{cfg.matrix_server}/_matrix/client/v3/rooms/{room_id}/join"
    headers = {"Authorization": f"Bearer {token}"}
    resp = session.post(url, headers=headers, json={}, timeout=cfg.http_timeout)
    resp.raise_for_status()


def process_user(session, cfg, row, passwords_by_user, log):
    local, _ = split_username(row["username"])
    token = row.get("access_token") or ""
    next_batch = row.get("next_batch") or ""

    if not token:
        password = passwords_by_user.get(local)
        if not password:
            return row, f"sem access_token e sem senha para {local}"
        try:
            token = do_login(session, cfg, local, password)
            row["access_token"] = token
        except Exception as exc:
            return row, f"login falhou: {exc}"

    try:
        sync = do_sync(session, cfg, token, next_batch)
    except Exception as exc:
        # Token pode ter expirado -- tenta relogar uma vez.
        password = passwords_by_user.get(local)
        if not password:
            return row, f"sync falhou: {exc}"
        try:
            token = do_login(session, cfg, local, password)
            row["access_token"] = token
            sync = do_sync(session, cfg, token, next_batch)
        except Exception as exc2:
            return row, f"sync falhou após relogin: {exc2}"

    invites = list((sync.get("rooms", {}) or {}).get("invite", {}).keys())
    joined = 0
    for room_id in invites:
        try:
            join_room(session, cfg, token, room_id)
            joined += 1
        except Exception as exc:
            log.warning("Falha ao entrar em %s para %s: %s", room_id, local, exc)

    if sync.get("next_batch"):
        row["next_batch"] = sync["next_batch"]

    log.debug("%s: %d convite(s) aceito(s) de %d", local, joined, len(invites))
    return row, None


def main():
    cfg = load_config()
    log = get_logger("accept_invites", cfg.log_level)
    args = parse_args(cfg)

    rows = read_csv_rows(args.input)
    if not rows:
        sys.exit(f"ERRO: nenhum usuário encontrado em {args.input}. "
                  f"Rode primeiro o passo 3 (03_register_users.py).")

    passwords_by_user = {}
    for row in read_csv_rows(args.users):
        local, _ = split_username(row["username"])
        passwords_by_user[local] = row["password"]

    log.info("Processando convites de %d usuários com %d workers...", len(rows), args.workers)

    session = build_session(cfg)
    lock = threading.Lock()
    total_accepted = 0
    failed = 0

    def worker(row):
        nonlocal total_accepted, failed
        updated_row, error = process_user(session, cfg, row, passwords_by_user, log)
        if cfg.request_sleep:
            time.sleep(cfg.request_sleep)
        with lock:
            if error:
                failed += 1
                log.warning("Falha para %s: %s", updated_row.get("username"), error)
        return updated_row

    updated_rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(worker, row) for row in rows]
        for i, future in enumerate(as_completed(futures), start=1):
            updated_rows.append(future.result())
            if i % 100 == 0:
                log.info("Progresso: %d/%d", i, len(rows))

    output_path = args.output or args.input
    write_csv_rows(output_path, TOKENS_FIELDS, updated_rows)

    log.info("Concluido. Usuários processados: %d | Falhas: %d | Atualizado: %s",
              len(updated_rows), failed, output_path)


if __name__ == "__main__":
    main()
