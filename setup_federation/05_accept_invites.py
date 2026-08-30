#!/usr/bin/env python3
"""
Passo 5/7 (federação) — Aceita os convites; é aqui que a federação acontece.

ESTE PASSO NÃO EXISTIA nos scripts originais da pasta `federation/`.
Sem ele, os usuários remotos ficam apenas com o convite PENDENTE e
nunca entram de fato na sala — ou seja, no teste de carga o usuário do
home01 acaba mandando mensagem para uma sala onde ninguém do home02
entrou, e nenhum tráfego real de federação é gerado.

O que acontece aqui, e por que importa:
  * cada usuário faz `/sync` e `/join` no SEU PRÓPRIO homeserver;
  * quando um usuário do home02 entra numa sala criada no home01, o
    home02 executa o "federated join" — busca o estado da sala no
    home01 via API server-to-server e passa a replicar a sala
    localmente. É o handshake mais pesado da federação, e o que
    realmente valida se os dois servidores se enxergam.

O `next_batch` devolvido pelo `/sync` é gravado de volta em
`tokens.csv`, o mesmo arquivo lido pelo Locust — assim o teste de carga
já começa a partir do estado atual, sem ter que ressincronizar todo o
histórico.

Exemplos:
    python3 setup_federation/05_accept_invites.py
    python3 setup_federation/05_accept_invites.py --only home02
    python3 setup_federation/05_accept_invites.py --passes 2
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


def load_room_hints(fedcfg, status_path, log):
    """Mapeia room_id -> domínio do homeserver onde a sala foi criada.

    Esse domínio é enviado como `server_name` no join: é a dica que o
    homeserver local usa para saber com quem falar ao executar um
    federated join numa sala que ele ainda não conhece.
    """
    hints = {}
    for row in read_csv_rows(status_path):
        room_id = (row.get("room_id") or "").strip()
        creator_hs = (row.get("creator_hs") or "").strip()
        if room_id and creator_hs in fedcfg.homeservers:
            hints[room_id] = fedcfg.homeservers[creator_hs].domain
    if hints:
        log.debug("Carregadas %d dicas de server_name de %s", len(hints), status_path)
    return hints


def do_login(session, fedcfg, hs, username, password):
    resp = session.post(
        f"{hs.url}/_matrix/client/v3/login",
        json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": username},
            "password": password,
        },
        timeout=fedcfg.base.http_timeout,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def do_sync(session, fedcfg, hs, token, since):
    params = {"timeout": "0", "set_presence": "offline"}
    if since:
        params["since"] = since
    resp = session.get(
        f"{hs.url}/_matrix/client/v3/sync",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=fedcfg.base.http_timeout,
    )
    resp.raise_for_status()
    return resp.json()


def join_room(session, fedcfg, hs, token, room_id, via_domain):
    """Entra na sala pelo homeserver `hs`.

    Usa /join/{roomId} (e não /rooms/{roomId}/join) porque só essa
    variante aceita o parâmetro `server_name`, necessário para o
    federated join quando a sala é de outro servidor.
    """
    params = {}
    if via_domain and via_domain != hs.domain:
        params["server_name"] = via_domain
    resp = session.post(
        f"{hs.url}/_matrix/client/v3/join/{room_id}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        json={},
        timeout=fedcfg.base.http_timeout,
    )
    resp.raise_for_status()
    return bool(params)  # True se foi um join federado (sala de outro domínio)


def process_user(session, fedcfg, row, passwords, hints, log):
    username = row["username"].strip()
    hs = fedcfg.homeserver_for(username)
    if hs is None:
        return row, 0, 0, f"{username} não casa com nenhum prefixo de homeserver"

    token = (row.get("access_token") or "").strip()
    next_batch = (row.get("next_batch") or "").strip()
    password = passwords.get(username)

    if not token:
        if not password:
            return row, 0, 0, f"{username} sem access_token e sem senha"
        try:
            token = do_login(session, fedcfg, hs, username, password)
            row["access_token"] = token
        except Exception as exc:
            return row, 0, 0, f"[{hs.key}] login falhou: {exc}"

    try:
        sync = do_sync(session, fedcfg, hs, token, next_batch)
    except Exception as exc:
        if not password:
            return row, 0, 0, f"[{hs.key}] sync falhou: {exc}"
        try:  # token pode ter expirado: reloga uma vez
            token = do_login(session, fedcfg, hs, username, password)
            row["access_token"] = token
            sync = do_sync(session, fedcfg, hs, token, next_batch)
        except Exception as exc2:
            return row, 0, 0, f"[{hs.key}] sync falhou após relogin: {exc2}"

    invites = list((sync.get("rooms", {}) or {}).get("invite", {}).keys())
    joined = federated_joins = 0
    for room_id in invites:
        try:
            was_federated = join_room(session, fedcfg, hs, token,
                                       room_id, hints.get(room_id))
            joined += 1
            federated_joins += 1 if was_federated else 0
        except Exception as exc:
            log.warning("[%s] %s não conseguiu entrar em %s: %s",
                        hs.key, username, room_id, exc)

    if sync.get("next_batch"):
        row["next_batch"] = sync["next_batch"]

    if invites:
        log.debug("[%s] %s: %d/%d convites aceitos (%d federados)",
                  hs.key, username, joined, len(invites), federated_joins)
    return row, joined, federated_joins, None


def main():
    fedcfg = load_federation_config()
    log = get_logger("fed.accept_invites", fedcfg.base.log_level)

    parser = argparse.ArgumentParser(description="Aceita os convites pendentes de cada usuário")
    parser.add_argument("-i", "--input", type=Path, default=fedcfg.tokens_csv,
                        help=f"tokens.csv do passo 3 (padrão: {fedcfg.tokens_csv})")
    parser.add_argument("--users", type=Path, default=fedcfg.users_csv,
                        help=f"users.csv, para login de fallback (padrão: {fedcfg.users_csv})")
    parser.add_argument("--status", type=Path, default=fedcfg.rooms_status_csv,
                        help=f"rooms_status.csv do passo 4, usado para as dicas de "
                             f"server_name (padrão: {fedcfg.rooms_status_csv})")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="Arquivo de saída (padrão: sobrescreve o de entrada)")
    parser.add_argument("--only", action="append", default=None, metavar="HOMESERVER",
                        help="Processa só este homeserver (pode repetir)")
    parser.add_argument("--passes", type=int, default=1,
                        help="Repete a varredura N vezes. Convites federados podem "
                             "demorar a chegar; 2 passes costuma pegar os atrasados.")
    parser.add_argument("--workers", type=int, default=fedcfg.base.workers,
                        help=f"Usuários em paralelo (padrão: {fedcfg.base.workers})")
    args = parser.parse_args()

    describe(fedcfg, log)

    selected = set(args.only) if args.only else set(fedcfg.keys)
    unknown = selected - set(fedcfg.keys)
    if unknown:
        sys.exit(f"ERRO: homeserver(s) desconhecido(s) em --only: {sorted(unknown)}")

    all_rows = read_csv_rows(args.input)
    if not all_rows:
        sys.exit(f"ERRO: nenhum usuário em {args.input}. Rode o passo 3 primeiro.")

    passwords = {r["username"]: r["password"] for r in read_csv_rows(args.users)}
    hints = load_room_hints(fedcfg, args.status, log)
    if not hints:
        log.warning("Sem dicas de server_name (%s ausente ou vazio). Joins federados "
                    "ainda funcionam via convite, mas são menos robustos.", args.status)

    rows_by_user = {r["username"]: r for r in all_rows}
    targets = [r for r in all_rows
                if (hs := fedcfg.homeserver_for(r["username"])) and hs.key in selected]

    session = build_session(fedcfg.base)
    lock = threading.Lock()

    for current_pass in range(1, args.passes + 1):
        total_joined = total_federated = failures = 0
        log.info("Passe %d/%d: processando %d usuários com %d workers...",
                  current_pass, args.passes, len(targets), args.workers)

        def worker(row):
            nonlocal total_joined, total_federated, failures
            updated, joined, fed_joins, error = process_user(
                session, fedcfg, row, passwords, hints, log)
            with lock:
                total_joined += joined
                total_federated += fed_joins
                if error:
                    failures += 1
                    log.warning("Falha: %s", error)
                rows_by_user[updated["username"]] = updated
            return updated

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(worker, r) for r in targets]
            for i, future in enumerate(as_completed(futures), start=1):
                future.result()
                if i % 100 == 0:
                    log.info("  progresso: %d/%d", i, len(targets))

        output = args.output or args.input
        write_csv_rows(output, TOKENS_FIELDS, [rows_by_user[k] for k in sorted(rows_by_user)])
        log.info("Passe %d: %d convites aceitos (%d deles federados) | falhas: %d | salvo em %s",
                  current_pass, total_joined, total_federated, failures, output)

        if total_joined == 0 and current_pass < args.passes:
            log.info("Nenhum convite pendente restante; encerrando antes dos passes extras.")
            break

    log.info("Concluído. Rode o passo 7 (07_verify_federation.py) para confirmar "
              "que as salas têm membros dos dois homeservers.")


if __name__ == "__main__":
    main()
