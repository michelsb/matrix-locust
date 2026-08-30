#!/usr/bin/env python3
"""
Limpeza (opcional) — Tira os usuários das salas / apaga as salas de teste.

Substitui o `federation/delete_all_rooms.py`. Dois modos:

  leave  (padrão, NÃO precisa de admin)
      Cada usuário sai de todas as suas salas. As salas continuam
      existindo no banco do servidor, mas ficam vazias. Suficiente
      para reaproveitar os mesmos usuários numa nova rodada de teste.

  purge  (precisa de token de ADMIN do Synapse)
      Usa a Admin API do Synapse para apagar e expurgar a sala do banco
      de verdade. Use quando precisar devolver o servidor ao estado
      limpo, ou quando o banco começar a inchar depois de várias
      rodadas.

CORREÇÃO em relação ao script original: ele chamava
`DELETE /_matrix/client/v3/admin/rooms/{room_id}/delete`, endpoint que
não existe — então o modo "delete" sempre falhava e caía no fallback de
"leave", dando a falsa impressão de que as salas tinham sido apagadas.
O endpoint correto da Admin API do Synapse é
`DELETE /_synapse/admin/v2/rooms/{room_id}`.

Exemplos:
    python3 setup_federation/99_cleanup_rooms.py --dry-run
    python3 setup_federation/99_cleanup_rooms.py --mode leave
    python3 setup_federation/99_cleanup_rooms.py --mode purge --only home01
"""

import argparse
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common_fed import (  # noqa: E402
    build_session, describe, get_logger, load_federation_config, read_csv_rows,
)


def joined_rooms(session, fedcfg, hs, token):
    resp = session.get(
        f"{hs.url}/_matrix/client/v3/joined_rooms",
        headers={"Authorization": f"Bearer {token}"},
        timeout=fedcfg.base.http_timeout,
    )
    resp.raise_for_status()
    return resp.json().get("joined_rooms", [])


def leave_room(session, fedcfg, hs, token, room_id):
    resp = session.post(
        f"{hs.url}/_matrix/client/v3/rooms/{room_id}/leave",
        headers={"Authorization": f"Bearer {token}"},
        json={},
        timeout=fedcfg.base.http_timeout,
    )
    resp.raise_for_status()


def purge_room(session, fedcfg, hs, admin_token, room_id):
    """Admin API do Synapse: apaga e expurga a sala do banco."""
    resp = session.delete(
        f"{hs.url}/_synapse/admin/v2/rooms/{room_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"block": False, "purge": True},
        timeout=max(fedcfg.base.http_timeout, 60),
    )
    resp.raise_for_status()
    return resp.json().get("delete_id", "")


def main():
    fedcfg = load_federation_config()
    log = get_logger("fed.cleanup", fedcfg.base.log_level)

    parser = argparse.ArgumentParser(description="Limpa as salas de teste de federação")
    parser.add_argument("-i", "--input", type=Path, default=fedcfg.tokens_csv,
                        help=f"tokens.csv (padrão: {fedcfg.tokens_csv})")
    parser.add_argument("--status", type=Path, default=fedcfg.rooms_status_csv,
                        help=f"rooms_status.csv, usado no modo purge (padrão: {fedcfg.rooms_status_csv})")
    parser.add_argument("--mode", choices=["leave", "purge"], default="leave",
                        help="leave = usuários saem das salas (padrão); "
                             "purge = Admin API do Synapse apaga as salas")
    parser.add_argument("--only", action="append", default=None, metavar="HOMESERVER",
                        help="Limpa só este homeserver (pode repetir)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Só mostra o que seria feito, sem alterar nada")
    parser.add_argument("--workers", type=int, default=fedcfg.base.workers,
                        help=f"Threads simultâneas (padrão: {fedcfg.base.workers})")
    args = parser.parse_args()

    describe(fedcfg, log)

    selected = set(args.only) if args.only else set(fedcfg.keys)
    unknown = selected - set(fedcfg.keys)
    if unknown:
        sys.exit(f"ERRO: homeserver(s) desconhecido(s) em --only: {sorted(unknown)}")

    session = build_session(fedcfg.base)

    # ---------------- modo purge (admin) ----------------
    if args.mode == "purge":
        rows = [r for r in read_csv_rows(args.status)
                 if r.get("room_id") and r.get("creator_hs") in selected]
        if not rows:
            sys.exit(f"ERRO: nenhuma sala com room_id em {args.status}. "
                      f"O modo purge depende do rooms_status.csv gerado no passo 4.")

        admin_tokens = {}
        for key in selected:
            env_name = f"FED_{key.upper().replace('-', '_')}_ADMIN_TOKEN"
            token = os.environ.get(env_name, "").strip()
            if token:
                admin_tokens[key] = token
            else:
                log.warning("Sem %s definido -- as salas de '%s' não serão expurgadas.",
                            env_name, key)
        if not admin_tokens:
            sys.exit("ERRO: o modo purge exige pelo menos um token de admin. "
                      "Defina FED_<HOMESERVER>_ADMIN_TOKEN no .env.")

        targets = [r for r in rows if r["creator_hs"] in admin_tokens]
        log.info("Modo purge: %d salas candidatas", len(targets))
        if args.dry_run:
            for r in targets[:20]:
                log.info("  [dry-run] apagaria %s (%s) em %s",
                          r["room_id"], r["room_name"], r["creator_hs"])
            log.info("[dry-run] nada foi alterado (%d salas no total).", len(targets))
            return

        ok = fail = 0
        for r in targets:
            hs = fedcfg.homeservers[r["creator_hs"]]
            try:
                purge_room(session, fedcfg, hs, admin_tokens[hs.key], r["room_id"])
                ok += 1
                log.debug("Sala %s expurgada em %s", r["room_id"], hs.key)
            except Exception as exc:
                fail += 1
                log.warning("Falha ao expurgar %s em %s: %s", r["room_id"], hs.key, exc)
        log.info("Purge concluído. Sucesso: %d | Falhas: %d", ok, fail)
        return

    # ---------------- modo leave (padrão) ----------------
    rows = [r for r in read_csv_rows(args.input) if r.get("access_token")]
    targets = [r for r in rows
                if (hs := fedcfg.homeserver_for(r["username"])) and hs.key in selected]
    if not targets:
        sys.exit(f"ERRO: nenhum usuário com token em {args.input}.")

    log.info("Modo leave: %d usuários sairão de todas as suas salas", len(targets))
    lock = threading.Lock()
    left = failures = 0

    def worker(row):
        nonlocal left, failures
        hs = fedcfg.homeserver_for(row["username"])
        token = row["access_token"]
        try:
            rooms = joined_rooms(session, fedcfg, hs, token)
        except Exception as exc:
            with lock:
                failures += 1
            log.warning("[%s] %s: erro ao listar salas -- %s", hs.key, row["username"], exc)
            return

        if args.dry_run:
            if rooms:
                log.info("  [dry-run] %s sairia de %d sala(s)", row["username"], len(rooms))
            return

        for room_id in rooms:
            try:
                leave_room(session, fedcfg, hs, token, room_id)
                with lock:
                    left += 1
            except Exception as exc:
                with lock:
                    failures += 1
                log.debug("[%s] %s não saiu de %s: %s", hs.key, row["username"], room_id, exc)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(worker, r) for r in targets]
        for i, future in enumerate(as_completed(futures), start=1):
            future.result()
            if i % 100 == 0:
                log.info("  progresso: %d/%d", i, len(targets))

    if args.dry_run:
        log.info("[dry-run] nada foi alterado.")
    else:
        log.info("Concluído. Saídas de sala: %d | Falhas: %d", left, failures)


if __name__ == "__main__":
    main()
