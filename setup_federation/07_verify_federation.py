#!/usr/bin/env python3
"""
Passo 7/7 (federação) — Verifica se a federação está realmente ativa.

Substitui o `federation/monitor_federation.py`. Amostra usuários de
cada homeserver e, para cada sala em que eles estão, verifica de quais
DOMÍNIOS são os membros e os remetentes das mensagens.

Correções em relação ao script original:
  * a detecção de "membro remoto" agora compara o domínio real do MXID
    (a parte depois do ":") com o domínio do homeserver local. O
    original fazia busca por substring ("'home02' in mxid") com uma
    cláusula morta no meio da expressão — funcionava por coincidência
    com aqueles nomes específicos e quebra com qualquer outro;
  * o access_token vai no header Authorization, e não como parâmetro de
    query (essa forma está depreciada na spec do Matrix desde a v1.11 e
    pode ser recusada por versões recentes do Synapse);
  * amostra e limites são configuráveis, e não fixos em 5;
  * o script termina com exit code != 0 quando não encontra federação,
    para poder ser usado em pipeline/CI.

Exemplos:
    python3 setup_federation/07_verify_federation.py
    python3 setup_federation/07_verify_federation.py --sample 20 --check-messages
    python3 setup_federation/07_verify_federation.py --only home01
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common_fed import (  # noqa: E402
    build_session, describe, get_logger, load_federation_config, read_csv_rows,
)


def domain_of(mxid: str) -> str:
    """Extrai o domínio de um MXID (@user:dominio) -- a única forma
    correta de saber a que servidor um usuário pertence."""
    return mxid.split(":", 1)[1] if ":" in mxid else ""


def api_get(session, fedcfg, hs, path, token, params=None):
    resp = session.get(
        f"{hs.url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=fedcfg.base.http_timeout,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    fedcfg = load_federation_config()
    log = get_logger("fed.verify", fedcfg.base.log_level)

    parser = argparse.ArgumentParser(description="Verifica se a federação está ativa")
    parser.add_argument("-i", "--input", type=Path, default=fedcfg.tokens_csv,
                        help=f"tokens.csv (padrão: {fedcfg.tokens_csv})")
    parser.add_argument("--only", action="append", default=None, metavar="HOMESERVER",
                        help="Verifica só este homeserver (pode repetir)")
    parser.add_argument("--sample", type=int, default=5,
                        help="Usuários amostrados por homeserver (padrão: 5)")
    parser.add_argument("--max-rooms", type=int, default=10,
                        help="Salas inspecionadas por usuário (padrão: 10)")
    parser.add_argument("--check-messages", action="store_true",
                        help="Também inspeciona as mensagens recentes de cada sala "
                             "(mais lento; útil DURANTE o teste de carga)")
    parser.add_argument("--message-limit", type=int, default=20,
                        help="Mensagens lidas por sala com --check-messages (padrão: 20)")
    args = parser.parse_args()

    describe(fedcfg, log)

    selected = set(args.only) if args.only else set(fedcfg.keys)
    unknown = selected - set(fedcfg.keys)
    if unknown:
        sys.exit(f"ERRO: homeserver(s) desconhecido(s) em --only: {sorted(unknown)}")

    rows = [r for r in read_csv_rows(args.input) if r.get("access_token")]
    if not rows:
        sys.exit(f"ERRO: nenhum token em {args.input}. Rode o passo 3 primeiro.")

    known_domains = {hs.domain for hs in fedcfg.homeservers.values()}
    session = build_session(fedcfg.base)

    totals = Counter()
    remote_domains_seen = Counter()
    remote_senders_seen = Counter()

    for key in fedcfg.keys:
        if key not in selected:
            continue
        hs = fedcfg.homeservers[key]
        candidates = [r for r in rows if fedcfg.homeserver_for(r["username"]) is hs]
        sample = candidates[:args.sample]

        print(f"\n{'=' * 78}")
        print(f"HOMESERVER {key}  ({hs.url}  |  server_name: {hs.domain})")
        print("=" * 78)

        if not sample:
            print("  (nenhum usuário com token para este homeserver)")
            continue

        for row in sample:
            username, token = row["username"], row["access_token"]
            try:
                joined = api_get(session, fedcfg, hs,
                                  "/_matrix/client/v3/joined_rooms", token)
                rooms = joined.get("joined_rooms", [])
            except Exception as exc:
                print(f"\n  {username}: ERRO ao listar salas -- {exc}")
                totals["errors"] += 1
                continue

            print(f"\n  {username}: {len(rooms)} sala(s)")
            if not rooms:
                print("    (usuário não entrou em nenhuma sala -- rode o passo 5)")
                continue

            for room_id in rooms[:args.max_rooms]:
                totals["rooms_checked"] += 1
                try:
                    members_data = api_get(
                        session, fedcfg, hs,
                        f"/_matrix/client/v3/rooms/{room_id}/joined_members", token)
                    members = list(members_data.get("joined", {}).keys())
                except Exception as exc:
                    print(f"    {room_id[:24]}...: ERRO ao listar membros -- {exc}")
                    totals["errors"] += 1
                    continue

                # Um membro é "remoto" se o domínio do MXID dele for
                # diferente do server_name do homeserver que consultamos.
                remote = [m for m in members if domain_of(m) != hs.domain]
                for m in remote:
                    remote_domains_seen[domain_of(m)] += 1

                if not remote:
                    continue

                totals["federated_rooms"] += 1
                print(f"    ✓ {room_id[:24]}... {len(members)} membros "
                      f"({len(remote)} remotos: {', '.join(sorted({domain_of(m) for m in remote}))})")

                if not args.check_messages:
                    continue

                try:
                    msgs = api_get(
                        session, fedcfg, hs,
                        f"/_matrix/client/v3/rooms/{room_id}/messages", token,
                        params={"dir": "b", "limit": args.message_limit})
                    events = msgs.get("chunk", [])
                except Exception as exc:
                    print(f"      ERRO ao ler mensagens -- {exc}")
                    totals["errors"] += 1
                    continue

                msgs_total = [e for e in events if e.get("type") == "m.room.message"]
                msgs_remote = [e for e in msgs_total
                                if domain_of(e.get("sender", "")) != hs.domain]
                totals["messages_checked"] += len(msgs_total)
                totals["messages_remote"] += len(msgs_remote)

                for e in msgs_remote:
                    remote_senders_seen[domain_of(e.get("sender", ""))] += 1
                if msgs_remote:
                    print(f"      📨 {len(msgs_remote)}/{len(msgs_total)} mensagens "
                          f"vindas de servidores remotos")
                    for e in msgs_remote[:2]:
                        body = (e.get("content", {}).get("body") or "")[:48]
                        print(f"         - {e.get('sender')}: {body}")

    # ---------------------------------------------------------------
    print(f"\n{'=' * 78}")
    print("RESUMO DA FEDERAÇÃO")
    print("=" * 78)
    print(f"Salas inspecionadas:            {totals['rooms_checked']}")
    print(f"Salas com membros remotos:      {totals['federated_rooms']}")
    if args.check_messages:
        print(f"Mensagens inspecionadas:        {totals['messages_checked']}")
        print(f"Mensagens de servidor remoto:   {totals['messages_remote']}")
    if totals["errors"]:
        print(f"Erros durante a verificação:    {totals['errors']}")

    if remote_domains_seen:
        print("\nDomínios remotos encontrados nas salas:")
        for domain, count in remote_domains_seen.most_common():
            flag = "" if domain in known_domains else "  <- domínio NÃO configurado no .env"
            print(f"  {domain}: {count} membro(s){flag}")

    print()
    if totals["federated_rooms"] == 0:
        print("✗ NENHUMA FEDERAÇÃO DETECTADA")
        print()
        print("  Possíveis causas, em ordem de probabilidade:")
        print("  1. O passo 5 (05_accept_invites.py) não rodou, ou os usuários remotos")
        print("     não conseguiram entrar -- os convites ficaram pendentes.")
        print("  2. FED_<HS>_DOMAIN está diferente do server_name real do Synapse.")
        print("     Confira o 'user_id' em tokens.csv: o domínio dele é o valor correto.")
        print("  3. As salas foram geradas sem membros dos dois lados -- confira a coluna")
        print("     'federated' em rooms_status.csv.")
        print("  4. A federação está bloqueada entre os servidores (DNS/.well-known/TLS/")
        print("     firewall, ou federation_domain_whitelist no homeserver.yaml).")
        sys.exit(1)

    print("✓ FEDERAÇÃO ATIVA")
    if args.check_messages and totals["messages_remote"] == 0:
        print()
        print("  Obs.: há salas federadas, mas nenhuma mensagem de servidor remoto ainda.")
        print("  Isso é esperado ANTES do teste de carga. Rode este script novamente")
        print("  durante/depois do teste, com --check-messages, para ver o tráfego real.")


if __name__ == "__main__":
    main()
