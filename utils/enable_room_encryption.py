#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Habilita criptografia ponta-a-ponta (m.room.encryption) em salas já existentes.

Fluxo:
- Lê um ou mais CSVs de usuários (ex.: users01.csv users02.csv)
- Usa access_token da linha; se não houver, tenta login com password/default-password
- Lista salas em que o usuário participa
- Tenta habilitar m.room.encryption quando ainda não estiver habilitada

Observações importantes:
- Não criptografa retroativamente eventos antigos. Apenas mensagens futuras virarão m.room.encrypted.
- O usuário precisa ter permissão para enviar state event no room (normalmente criador/admin).

Exemplos:
  python3 enable_room_encryption.py users01.csv --homeserver https://home01-dev.ac.atlab.ufc.br --domain home01-dev.ac.atlab.ufc.br
  python3 enable_room_encryption.py users01.csv users02.csv --write-back
  python3 enable_room_encryption.py users01.csv users02.csv --room-name-contains Home01
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from copy import deepcopy
from typing import Dict, List, Optional, Set, Tuple

import requests

API_PREFIX = "/_matrix/client/v3"
DEFAULT_TIMEOUT = 25
ENCRYPTION_ALGORITHM = "m.megolm.v1.aes-sha2"

DOMAIN_TO_HOMESERVER = {
    "home01-dev.ac.atlab.ufc.br": "https://home01-dev.ac.atlab.ufc.br",
    "home02-dev.ac.atlab.ufc.br": "https://home02-dev.ac.atlab.ufc.br",
}

USERNAME_PREFIX_TO_DOMAIN = {
    "userh01.": "home01-dev.ac.atlab.ufc.br",
    "userhome01.": "home01-dev.ac.atlab.ufc.br",
    "userh02.": "home02-dev.ac.atlab.ufc.br",
    "userhome02.": "home02-dev.ac.atlab.ufc.br",
}


def infer_domain_from_username(username: Optional[str]) -> Optional[str]:
    local = (username or "").strip().lstrip("@")
    if not local:
        return None

    if ":" in local:
        return local.split(":", 1)[1]

    for prefix, domain in USERNAME_PREFIX_TO_DOMAIN.items():
        if local.startswith(prefix):
            return domain

    return None


def normalize_homeserver_url(homeserver: str) -> str:
    hs = homeserver.strip()
    if not hs:
        raise ValueError("Homeserver vazio.")
    if not hs.startswith("http://") and not hs.startswith("https://"):
        hs = "https://" + hs
    return hs.rstrip("/")


def extract_domain_from_user_id(user_id: str) -> Optional[str]:
    if not user_id.startswith("@") or ":" not in user_id:
        return None
    return user_id.split(":", 1)[1]


def normalize_user_id(user_id: str, username: Optional[str], domain: Optional[str]) -> str:
    user_id = (user_id or "").strip()
    if user_id.startswith("@") and ":" in user_id:
        return user_id

    if not domain:
        domain = infer_domain_from_username(username)

    if not user_id:
        if not username:
            raise ValueError("Linha sem user_id e sem username.")
        if not domain:
            raise ValueError("Para montar user_id com username e necessario --domain.")
        return f"@{username.lstrip('@')}:{domain}"

    if ":" not in user_id:
        if not domain:
            raise ValueError("user_id sem dominio. Forneca --domain.")
        return f"@{user_id.lstrip('@')}:{domain}"

    if not user_id.startswith("@"):
        return "@" + user_id

    return user_id


def resolve_homeserver_url(row: Dict[str, str], args) -> str:
    row_hs = (row.get(args.homeserver_col) or "").strip()
    if row_hs:
        return normalize_homeserver_url(row_hs)

    row_domain = (row.get(args.domain_col) or "").strip()
    if row_domain:
        if row_domain in DOMAIN_TO_HOMESERVER:
            return DOMAIN_TO_HOMESERVER[row_domain]
        return normalize_homeserver_url(row_domain)

    user_id = (row.get("user_id") or "").strip()
    domain = extract_domain_from_user_id(user_id)
    if domain:
        if domain in DOMAIN_TO_HOMESERVER:
            return DOMAIN_TO_HOMESERVER[domain]
        return normalize_homeserver_url(domain)

    username = (row.get("username") or "").strip()
    inferred_domain = infer_domain_from_username(username)
    if inferred_domain:
        if inferred_domain in DOMAIN_TO_HOMESERVER:
            return DOMAIN_TO_HOMESERVER[inferred_domain]
        return normalize_homeserver_url(inferred_domain)

    if args.homeserver:
        return normalize_homeserver_url(args.homeserver)

    raise ValueError(
        "Nao foi possivel resolver homeserver para a linha. Informe --homeserver, --homeserver-col ou --domain."
    )


def do_login(hs: str, user_id: str, password: str, verify) -> str:
    url = hs + API_PREFIX + "/login"
    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user_id},
        "password": password,
        "initial_device_display_name": "matrix-enable-room-encryption",
    }
    r = requests.post(url, json=payload, timeout=DEFAULT_TIMEOUT, verify=verify)
    if r.status_code != 200:
        raise RuntimeError(f"Login falhou para {user_id}: {r.status_code} {r.text}")
    return r.json()["access_token"]


def get_joined_rooms(hs: str, token: str, verify) -> List[str]:
    url = hs + API_PREFIX + "/joined_rooms"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, verify=verify)
    if r.status_code != 200:
        raise RuntimeError(f"joined_rooms falhou: {r.status_code} {r.text}")
    return list(r.json().get("joined_rooms", []))


def get_room_name(hs: str, token: str, room_id: str, verify) -> Optional[str]:
    url = hs + API_PREFIX + f"/rooms/{room_id}/state/m.room.name"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, verify=verify)
    if r.status_code == 200:
        return (r.json().get("name") or "").strip() or None
    return None


def is_room_encrypted(hs: str, token: str, room_id: str, verify) -> bool:
    url = hs + API_PREFIX + f"/rooms/{room_id}/state/m.room.encryption"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, verify=verify)
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    raise RuntimeError(f"state m.room.encryption falhou: {r.status_code} {r.text}")


def enable_room_encryption(hs: str, token: str, room_id: str, verify) -> None:
    url = hs + API_PREFIX + f"/rooms/{room_id}/state/m.room.encryption"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"algorithm": ENCRYPTION_ALGORITHM}
    r = requests.put(url, json=payload, headers=headers, timeout=DEFAULT_TIMEOUT, verify=verify)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"set m.room.encryption falhou: {r.status_code} {r.text}")


def get_room_power_levels(hs: str, token: str, room_id: str, verify) -> Optional[Dict]:
    url = hs + API_PREFIX + f"/rooms/{room_id}/state/m.room.power_levels"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, verify=verify)
    if r.status_code == 200:
        return r.json()
    if r.status_code in (403, 404):
        return None
    raise RuntimeError(f"state m.room.power_levels falhou: {r.status_code} {r.text}")


def read_csv_rows(csv_path: str) -> Tuple[List[str], List[Dict[str, str]]]:
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        headers = [h for h in (reader.fieldnames or [])]
        if not headers:
            raise ValueError(f"CSV sem cabecalho: {csv_path}")
        rows = [deepcopy(row) for row in reader]
    return headers, rows


def write_csv_rows(csv_path: str, headers: List[str], rows: List[Dict[str, str]]) -> None:
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def should_process_room(room_name: Optional[str], room_name_contains: Optional[str]) -> bool:
    if not room_name_contains:
        return True
    if not room_name:
        return False
    return room_name_contains.lower() in room_name.lower()


def process_all_csvs(args, verify) -> Tuple[int, int, int, int, int]:
    users_processed = 0
    rooms_seen = 0
    rooms_enabled = 0
    rooms_already = 0
    failures = 0

    sessions: List[Dict[str, object]] = []
    room_to_session_indexes: Dict[str, List[int]] = {}

    for csv_path in args.csv_paths:
        print(f"\n=== Processando {csv_path} ===")
        headers, rows = read_csv_rows(csv_path)

        for col in ["username", "user_id", "access_token", "next_batch"]:
            if col not in headers:
                headers.append(col)

        for i, row in enumerate(rows, start=2):
            username = (row.get("username") or "").strip()
            user_id = (row.get("user_id") or "").strip()
            access_token = (row.get("access_token") or "").strip()
            password = (row.get(args.password_col) or "").strip()

            try:
                user_id = normalize_user_id(user_id, username, row.get(args.domain_col) or args.domain)
                row["user_id"] = user_id
                homeserver = resolve_homeserver_url(row, args)
            except Exception as e:
                print(f"[{csv_path}:L{i}] ERRO dados de usuario: {e}")
                failures += 1
                continue

            token = access_token
            if not token:
                if not password and not args.default_password:
                    print(f"[{csv_path}:L{i}] Sem token e sem senha para {user_id}. Pulei.")
                    failures += 1
                    continue
                try:
                    token = do_login(homeserver, user_id, (password or args.default_password), verify=verify)
                    row["access_token"] = token
                except Exception as e:
                    print(f"[{csv_path}:L{i}] ERRO login {user_id}: {e}")
                    failures += 1
                    continue

            try:
                joined_rooms = get_joined_rooms(homeserver, token, verify=verify)
            except Exception as e:
                print(f"[{csv_path}:L{i}] ERRO joined_rooms {user_id}: {e}")
                failures += 1
                continue

            users_processed += 1
            print(f"[{user_id}] salas associadas: {len(joined_rooms)}")

            session_index = len(sessions)
            sessions.append(
                {
                    "user_id": user_id,
                    "homeserver": homeserver,
                    "token": token,
                    "joined_rooms": set(joined_rooms),
                }
            )

            for room_id in joined_rooms:
                if room_id not in room_to_session_indexes:
                    room_to_session_indexes[room_id] = []
                room_to_session_indexes[room_id].append(session_index)

            time.sleep(args.sleep)

        if args.write_back:
            write_csv_rows(csv_path, headers, rows)

    rooms_seen = len(room_to_session_indexes)

    print(f"\n=== Processando salas unicas: {rooms_seen} ===")
    for room_id, idx_list in room_to_session_indexes.items():
        if not idx_list:
            continue

        base = sessions[idx_list[0]]
        base_hs = str(base["homeserver"])
        base_token = str(base["token"])

        try:
            room_name = get_room_name(base_hs, base_token, room_id, verify=verify)
        except Exception:
            room_name = None

        if not should_process_room(room_name, args.room_name_contains):
            continue

        label = room_name or room_id

        try:
            if is_room_encrypted(base_hs, base_token, room_id, verify=verify):
                rooms_already += 1
                print(f"  [ja criptografada] {label}")
                continue
        except Exception as e:
            print(f"  [ERRO verificando criptografia] {label}: {e}")
            failures += 1
            continue

        candidates = list(idx_list)
        try:
            power = get_room_power_levels(base_hs, base_token, room_id, verify=verify)
            if power:
                users_levels = power.get("users", {})
                users_default = int(power.get("users_default", 0))

                def user_level(idx: int) -> int:
                    uid = str(sessions[idx]["user_id"])
                    return int(users_levels.get(uid, users_default))

                candidates.sort(key=user_level, reverse=True)
        except Exception as e:
            print(f"  [aviso] nao foi possivel ler power levels de {label}: {e}")

        enabled = False
        last_forbidden: Optional[str] = None
        last_error: Optional[str] = None

        for idx in candidates:
            sess = sessions[idx]
            hs = str(sess["homeserver"])
            token = str(sess["token"])
            uid = str(sess["user_id"])

            try:
                enable_room_encryption(hs, token, room_id, verify=verify)
                rooms_enabled += 1
                enabled = True
                print(f"  [criptografia habilitada] {label} por {uid}")
                break
            except Exception as e:
                msg = str(e)
                if "403" in msg or "M_FORBIDDEN" in msg:
                    last_forbidden = f"{uid}: {msg}"
                    continue
                last_error = f"{uid}: {msg}"

        if not enabled:
            if last_forbidden:
                print(f"  [sem permissao em todos candidatos] {label}: {last_forbidden}")
            elif last_error:
                print(f"  [ERRO sala] {label}: {last_error}")
            else:
                print(f"  [ERRO sala] {label}: nenhuma sessao valida para tentar")
            failures += 1

        time.sleep(args.sleep)

    return users_processed, rooms_seen, rooms_enabled, rooms_already, failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Habilita m.room.encryption em salas Matrix existentes.")
    parser.add_argument("csv_paths", nargs="+", help="Um ou mais CSVs de usuarios.")
    parser.add_argument("--homeserver", help="Homeserver padrao, ex.: https://home01-dev.ac.atlab.ufc.br")
    parser.add_argument("--homeserver-col", default="homeserver", help="Coluna do CSV com homeserver/dominio.")
    parser.add_argument("--domain", help="Dominio para completar user_id quando necessario.")
    parser.add_argument("--domain-col", default="domain", help="Coluna do CSV com dominio do usuario.")
    parser.add_argument("--password-col", default="password", help="Coluna de senha no CSV.")
    parser.add_argument("--default-password", help="Senha padrao quando nao houver token e nao houver senha na linha.")
    parser.add_argument("--room-name-contains", help="Processa apenas salas cujo m.room.name contenha este texto.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Pausa entre requisicoes para evitar rate limit.")
    parser.add_argument("--insecure", action="store_true", help="Ignora verificacao TLS.")
    parser.add_argument("--ca-bundle", help="CA bundle customizado (.pem).")
    parser.add_argument("--write-back", action="store_true", help="Atualiza user_id/access_token no(s) CSV(s).")
    args = parser.parse_args()

    if args.insecure and args.ca_bundle:
        print("Use --insecure OU --ca-bundle, nao ambos.", file=sys.stderr)
        sys.exit(2)

    verify = False if args.insecure else (args.ca_bundle or True)

    try:
        total_users, total_rooms_seen, total_rooms_enabled, total_rooms_already, total_failures = process_all_csvs(
            args=args,
            verify=verify,
        )
    except Exception as e:
        print(f"[ERRO FATAL] execucao: {e}")
        sys.exit(1)

    print("\n==== RESUMO ====")
    print(f"Usuarios processados: {total_users}")
    print(f"Salas unicas vistas: {total_rooms_seen}")
    print(f"Salas com criptografia habilitada agora: {total_rooms_enabled}")
    print(f"Salas ja criptografadas: {total_rooms_already}")
    print(f"Falhas: {total_failures}")


if __name__ == "__main__":
    main()
