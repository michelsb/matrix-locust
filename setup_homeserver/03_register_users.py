#!/usr/bin/env python3
"""
Passo 3/5 — Registra os usuários de teste no homeserver.

Lê o arquivo de usuários (padrão: users.csv) e registra cada um via
`POST /_matrix/client/v3/register`, salvando o resultado (user_id,
access_token) em um CSV (padrão: tokens.csv) no formato já esperado
pelo restante do projeto matrix-locust (matrix_locust/users/matrixuser.py
carrega esse mesmo arquivo para pular o login durante o teste de carga).

Principais cuidados em relação ao script original:
  * O campo "username" do users.csv pode conter um sufixo ":dominio"
    usado apenas para testes federados (ver setup/common.py). Esse
    sufixo é removido antes de registrar — enviar o sufixo cru como
    username costumava causar falhas de registro no servidor.
  * Suporta o fluxo de UIA (User-Interactive-Auth) com "m.login.dummy",
    completando a etapa de sessão quando o servidor exige.
  * É reexecutável: por padrão pula usuários que já estão no CSV de
    saída, então uma execução interrompida pode ser simplesmente
    repetida (use --force para registrar tudo novamente).
  * Registra em paralelo (WORKERS) e grava o progresso incrementalmente,
    para não perder o trabalho já feito em caso de erro/queda.

Exemplos:
    python setup/03_register_users.py
    python setup/03_register_users.py --force
    python setup/03_register_users.py -i users.csv -o tokens.csv
"""

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    build_session, get_logger, load_config, mxid, read_csv_rows,
    split_username, write_csv_rows,
)

TOKENS_FIELDS = ["username", "user_id", "access_token", "next_batch"]
FLUSH_EVERY = 25


def parse_args(cfg):
    parser = argparse.ArgumentParser(description="Registra usuários de teste no homeserver Matrix")
    parser.add_argument("-i", "--input", type=Path, default=cfg.users_csv,
                        help=f"Arquivo .csv de usuários (padrão: {cfg.users_csv})")
    parser.add_argument("-o", "--output", type=Path, default=cfg.tokens_csv,
                        help=f"Arquivo .csv de saída com os tokens (padrão: {cfg.tokens_csv})")
    parser.add_argument("--failed", type=Path, default=cfg.failed_users_txt,
                        help=f"Arquivo com a lista de usuários que falharam (padrão: {cfg.failed_users_txt})")
    parser.add_argument("--force", action="store_true",
                        help="Registra novamente mesmo usuários já presentes no CSV de saída")
    parser.add_argument("--workers", type=int, default=cfg.workers,
                        help=f"Threads simultâneas de registro (padrão: {cfg.workers})")
    return parser.parse_args()


def register_user(session, cfg, local_username, password, log):
    """Registra um usuário via UIA (m.login.dummy). Se o usuário já
    existir (M_USER_IN_USE), tenta logar para recuperar um token válido
    -- útil ao re-executar depois de uma falha parcial."""
    url = f"{cfg.matrix_server}/_matrix/client/v3/register"
    payload = {
        "username": local_username,
        "password": password,
        "auth": {"type": "m.login.dummy"},
    }
    resp = session.post(url, json=payload, timeout=cfg.http_timeout)

    if resp.status_code == 401:
        # Fluxo de User-Interactive-Auth: completa a etapa "m.login.dummy"
        # usando a sessão retornada pelo servidor.
        session_id = (resp.json() or {}).get("session")
        if session_id:
            payload["auth"]["session"] = session_id
            resp = session.post(url, json=payload, timeout=cfg.http_timeout)

    if resp.status_code == 200:
        data = resp.json()
        return {
            "username": local_username,
            "user_id": data["user_id"],
            "access_token": data["access_token"],
            "next_batch": "",
        }, None

    if resp.status_code == 400 and "M_USER_IN_USE" in resp.text:
        log.debug("Usuário %s já existe, tentando login...", local_username)
        return login_existing_user(session, cfg, local_username, password)

    return None, f"HTTP {resp.status_code}: {resp.text[:200]}"


def login_existing_user(session, cfg, local_username, password):
    url = f"{cfg.matrix_server}/_matrix/client/v3/login"
    user_id = mxid(local_username, cfg.domain)
    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user_id},
        "password": password,
    }
    resp = session.post(url, json=payload, timeout=cfg.http_timeout)
    if resp.status_code == 200:
        data = resp.json()
        return {
            "username": local_username,
            "user_id": data["user_id"],
            "access_token": data["access_token"],
            "next_batch": "",
        }, None
    return None, f"login HTTP {resp.status_code}: {resp.text[:200]}"


def main():
    cfg = load_config()
    log = get_logger("register_users", cfg.log_level)
    args = parse_args(cfg)

    users = read_csv_rows(args.input)
    if not users:
        sys.exit(f"ERRO: nenhum usuário encontrado em {args.input}. "
                  f"Rode primeiro o passo 1 (01_generate_users.py).")

    already_done = {}
    if not args.force:
        for row in read_csv_rows(args.output):
            if row.get("username") and row.get("access_token"):
                already_done[row["username"]] = row

    pending = []
    for row in users:
        local, _ = split_username(row["username"])
        if local in already_done:
            continue
        pending.append((local, row["password"]))

    skipped = len(users) - len(pending)
    if skipped:
        log.info("%d usuários já registrados em %s, pulando (use --force para refazer)",
                  skipped, args.output)
    if not pending:
        log.info("Nada a fazer: todos os usuários já estão registrados.")
        return

    log.info("Registrando %d usuários em %s com %d workers...",
              len(pending), cfg.matrix_server, args.workers)

    session = build_session(cfg)
    results = dict(already_done)
    failed = []
    lock = threading.Lock()

    def flush():
        write_csv_rows(args.output, TOKENS_FIELDS,
                        [results[k] for k in sorted(results)])
        args.failed.parent.mkdir(parents=True, exist_ok=True)
        with open(args.failed, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(failed)) + ("\n" if failed else ""))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(register_user, session, cfg, local, password, log): local
            for local, password in pending
        }
        done = 0
        for future in as_completed(futures):
            local = futures[future]
            try:
                row, error = future.result()
            except Exception as exc:  # erro inesperado de rede/parsing
                row, error = None, str(exc)

            with lock:
                if row:
                    results[local] = row
                else:
                    failed.append(local)
                    log.warning("Falha ao registrar %s: %s", local, error)
                done += 1
                if done % FLUSH_EVERY == 0:
                    flush()
                    log.info("Progresso: %d/%d", done, len(pending))

    flush()
    log.info("Concluido. Sucesso: %d | Falhas: %d | Total em %s: %d",
              len(pending) - len(failed), len(failed), args.output, len(results))
    if failed:
        log.warning("Usuários com falha registrados em %s. Rode o script novamente "
                    "para tentar apenas os pendentes.", args.failed)


if __name__ == "__main__":
    main()
