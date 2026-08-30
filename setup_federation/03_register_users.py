#!/usr/bin/env python3
"""
Passo 3/7 (federação) — Registra cada usuário no SEU homeserver.

Lê `users.csv` e registra cada usuário no homeserver correspondente ao
prefixo do seu username (userh01.* -> home01, userh02.* -> home02).
O resultado vai para `tokens.csv`, no formato já esperado pelo restante
do projeto:

    username,user_id,access_token,next_batch

O `user_id` gravado é o MXID real devolvido pelo servidor — é ele que
comprova se o `server_name` (FED_<HS>_DOMAIN) está correto. Se o MXID
voltar com um domínio diferente do que você configurou, a federação vai
falhar mais adiante, e o script avisa.

É reexecutável: por padrão pula quem já tem token em `tokens.csv`
(use --force para refazer). Usuários já existentes no servidor
(M_USER_IN_USE) têm o token recuperado via login.

Exemplos:
    python3 setup_federation/03_register_users.py
    python3 setup_federation/03_register_users.py --only home01
    python3 setup_federation/03_register_users.py --force --workers 16
"""

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common_fed import (  # noqa: E402
    TOKENS_FIELDS, build_session, describe, get_logger,
    load_federation_config, read_csv_rows, write_csv_rows,
)

FLUSH_EVERY = 25


def register_user(session, fedcfg, hs, username, password, log):
    """Registra (ou recupera por login) um usuário no homeserver `hs`."""
    url = f"{hs.url}/_matrix/client/v3/register"
    payload = {
        "username": username,
        "password": password,
        "auth": {"type": "m.login.dummy"},
    }
    timeout = fedcfg.base.http_timeout
    resp = session.post(url, json=payload, timeout=timeout)

    if resp.status_code == 401:
        # Fluxo UIA: repete completando a sessão devolvida pelo servidor.
        session_id = (resp.json() or {}).get("session")
        if session_id:
            payload["auth"]["session"] = session_id
            resp = session.post(url, json=payload, timeout=timeout)

    if resp.status_code == 200:
        data = resp.json()
        return _row(username, data), None

    if resp.status_code == 400 and "M_USER_IN_USE" in resp.text:
        log.debug("[%s] %s já existe, recuperando token via login", hs.key, username)
        return login_user(session, fedcfg, hs, username, password)

    return None, f"[{hs.key}] registro HTTP {resp.status_code}: {resp.text[:200]}"


def login_user(session, fedcfg, hs, username, password):
    url = f"{hs.url}/_matrix/client/v3/login"
    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
    }
    resp = session.post(url, json=payload, timeout=fedcfg.base.http_timeout)
    if resp.status_code == 200:
        return _row(username, resp.json()), None
    return None, f"[{hs.key}] login HTTP {resp.status_code}: {resp.text[:200]}"


def _row(username, data):
    return {
        "username": username,
        "user_id": data["user_id"],
        "access_token": data["access_token"],
        "next_batch": "",
    }


def main():
    fedcfg = load_federation_config()
    log = get_logger("fed.register_users", fedcfg.base.log_level)

    parser = argparse.ArgumentParser(description="Registra os usuários em seus respectivos homeservers")
    parser.add_argument("-i", "--input", type=Path, default=fedcfg.users_csv,
                        help=f"Arquivo .csv de usuários (padrão: {fedcfg.users_csv})")
    parser.add_argument("-o", "--output", type=Path, default=fedcfg.tokens_csv,
                        help=f"Arquivo .csv de tokens de saída (padrão: {fedcfg.tokens_csv})")
    parser.add_argument("--failed", type=Path, default=fedcfg.failed_users_txt,
                        help=f"Arquivo com os usuários que falharam (padrão: {fedcfg.failed_users_txt})")
    parser.add_argument("--only", action="append", default=None, metavar="HOMESERVER",
                        help="Registra só neste homeserver (pode repetir). Padrão: todos")
    parser.add_argument("--force", action="store_true",
                        help="Registra novamente mesmo quem já tem token")
    parser.add_argument("--workers", type=int, default=fedcfg.base.workers,
                        help=f"Threads simultâneas (padrão: {fedcfg.base.workers})")
    args = parser.parse_args()

    describe(fedcfg, log)

    selected = set(args.only) if args.only else set(fedcfg.keys)
    unknown = selected - set(fedcfg.keys)
    if unknown:
        sys.exit(f"ERRO: homeserver(s) desconhecido(s) em --only: {sorted(unknown)}. "
                  f"Disponíveis: {fedcfg.keys}")

    users = read_csv_rows(args.input)
    if not users:
        sys.exit(f"ERRO: nenhum usuário em {args.input}. Rode o passo 1 primeiro.")

    already = {}
    if not args.force:
        for row in read_csv_rows(args.output):
            if row.get("username") and row.get("access_token"):
                already[row["username"]] = row

    pending = []
    unrouted = 0
    for row in users:
        username = row["username"].strip()
        hs = fedcfg.homeserver_for(username)
        if hs is None:
            unrouted += 1
            continue
        if hs.key not in selected:
            continue
        if username in already:
            continue
        pending.append((hs, username, row["password"]))

    if unrouted:
        log.warning("%d usuários ignorados por não casarem com nenhum prefixo configurado", unrouted)
    if already:
        log.info("%d usuários já registrados, pulando (use --force para refazer)", len(already))
    if not pending:
        log.info("Nada a fazer: todos os usuários selecionados já estão registrados.")
        return

    log.info("Registrando %d usuários com %d workers...", len(pending), args.workers)

    session = build_session(fedcfg.base)
    results = dict(already)
    failed = []
    domain_warnings = set()
    lock = threading.Lock()

    def flush():
        write_csv_rows(args.output, TOKENS_FIELDS, [results[k] for k in sorted(results)])
        args.failed.parent.mkdir(parents=True, exist_ok=True)
        with open(args.failed, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(failed)) + ("\n" if failed else ""))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(register_user, session, fedcfg, hs, username, password, log): (hs, username)
            for hs, username, password in pending
        }
        done = 0
        for future in as_completed(futures):
            hs, username = futures[future]
            try:
                row, error = future.result()
            except Exception as exc:
                row, error = None, f"[{hs.key}] exceção: {exc}"

            with lock:
                if row:
                    results[username] = row
                    # Confere se o MXID devolvido bate com o server_name configurado.
                    actual_domain = row["user_id"].split(":", 1)[-1]
                    if actual_domain != hs.domain and hs.key not in domain_warnings:
                        domain_warnings.add(hs.key)
                        log.warning(
                            "ATENÇÃO [%s]: o servidor devolveu MXID '%s', mas FED_%s_DOMAIN "
                            "está como '%s'. Corrija o .env para '%s', senão os convites "
                            "federados vão falhar.",
                            hs.key, row["user_id"], hs.key.upper(), hs.domain, actual_domain)
                else:
                    failed.append(username)
                    log.warning("Falha em %s: %s", username, error)
                done += 1
                if done % FLUSH_EVERY == 0:
                    flush()
                    log.info("Progresso: %d/%d", done, len(pending))

    flush()
    log.info("Concluído. Sucesso: %d | Falhas: %d | Total em %s: %d",
              len(pending) - len(failed), len(failed), args.output, len(results))
    if failed:
        log.warning("Falhas listadas em %s. Rode de novo para tentar só os pendentes.", args.failed)


if __name__ == "__main__":
    main()
