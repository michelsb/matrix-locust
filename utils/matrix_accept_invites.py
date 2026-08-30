#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matrix_accept_invites_csv.py
----------------------------
Lê um CSV no formato:
    username,user_id,access_token,next_batch

Para cada linha:
- Usa o access_token se existir;
- Caso contrário, tenta fazer login por senha (se houver coluna "password" no CSV ou --default-password);
- Faz /sync para listar convites e aceita todos;
- Atualiza next_batch e (opcionalmente) access_token no CSV de saída.

Exemplos de uso:
    python matrix_accept_invites_csv.py users.csv --homeserver https://matrix-dev.dv.techsmart.space --write-back
    python matrix_accept_invites_csv.py users.csv --homeserver https://matrix.example.com --default-password 123456 --write-back
    python matrix_accept_invites_csv.py users.csv --homeserver https://matrix.example.com --domain matrix-dev.dv.techsmart.space --out-csv users_out.csv

Requisitos:
    pip install requests
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from copy import deepcopy
from typing import Dict, List, Optional, Tuple

import requests

API_PREFIX = "/_matrix/client/v3"
DEFAULT_TIMEOUT = 25

DOMAIN_TO_HOMESERVER = {
    "home01-dev.ac.atlab.ufc.br": "https://home01-dev.ac.atlab.ufc.br",
    "home02-dev.ac.atlab.ufc.br": "https://home02-dev.ac.atlab.ufc.br",
}


def normalize_user_id(user_id: str, username: Optional[str], domain: Optional[str]) -> str:
    """
    Normaliza o user_id (MXID). Se vier vazio e houver username+domain, monta @username:domain.
    Se vier apenas localpart (sem "@", ":"), completa com domínio.
    """
    user_id = (user_id or "").strip()
    if user_id.startswith("@") and ":" in user_id:
        return user_id
    # Sem user_id válido, tenta montar
    if not user_id:
        if not username:
            raise ValueError("Linha sem user_id e sem username.")
        if not domain:
            raise ValueError("Para montar user_id com username é necessário --domain.")
        local = username.lstrip("@")
        return f"@{local}:{domain}"
    # Tem algo, mas sem "@:" — considera localpart e completa com domínio
    if ":" not in user_id:
        if not domain:
            raise ValueError("user_id sem domínio. Forneça --domain.")
        uid = user_id.lstrip("@")
        return f"@{uid}:{domain}"
    # Tem ":", mas não começa com "@"
    if ":" in user_id and not user_id.startswith("@"):
        return "@" + user_id
    return user_id


def extract_domain_from_user_id(user_id: str) -> Optional[str]:
    if not user_id.startswith("@") or ":" not in user_id:
        return None
    return user_id.split(":", 1)[1]


def normalize_homeserver_url(homeserver: str) -> str:
    """Normaliza a URL do homeserver para incluir protocolo."""
    hs = homeserver.strip()
    if not hs:
        raise ValueError("Homeserver vazio.")
    if not hs.startswith("http://") and not hs.startswith("https://"):
        hs = "https://" + hs
    return hs.rstrip("/")


def resolve_homeserver_url(row: Dict[str, str], args) -> str:
    """Resolve a URL de homeserver para a linha do CSV."""
    # Usa homeserver direto da linha, se existir
    row_hs = (row.get(args.homeserver_col) or "").strip()
    if row_hs:
        return normalize_homeserver_url(row_hs)

    # Usa domínio direto da linha para montar homeserver
    row_domain = (row.get(args.domain_col) or "").strip()
    if row_domain:
        return normalize_homeserver_url(row_domain)

    # Usa o domínio do user_id, se estiver completo
    user_id = (row.get("user_id") or "").strip()
    domain = extract_domain_from_user_id(user_id)
    if domain:
        if domain in DOMAIN_TO_HOMESERVER:
            return DOMAIN_TO_HOMESERVER[domain]
        return normalize_homeserver_url(domain)

    # Fallback para o argumento global --homeserver
    if args.homeserver:
        return normalize_homeserver_url(args.homeserver)

    raise ValueError("Não foi possível resolver o homeserver para a linha. Informe --homeserver, --homeserver-col ou --domain.")


def do_login(hs: str, user_id: str, password: str, verify) -> Tuple[str, str]:
    url = hs.rstrip("/") + API_PREFIX + "/login"
    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user_id},
        "password": password,
        "initial_device_display_name": "matrix-accept-invites",
        "device_id": "matrix_accept_invites_csv_py",
    }
    r = requests.post(url, json=payload, timeout=DEFAULT_TIMEOUT, verify=verify)
    if r.status_code != 200:
        raise RuntimeError(f"Login falhou para {user_id}: {r.status_code} {r.text}")
    j = r.json()
    return j["access_token"], j.get("device_id", "")


def do_sync(hs: str, token: str, since: Optional[str], verify) -> Dict:
    url = hs.rstrip("/") + API_PREFIX + "/sync"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"timeout": "0", "set_presence": "offline"}
    # Usar since se existir (economiza payload); convites aparecem mesmo assim.
    if since:
        params["since"] = since
    r = requests.get(url, headers=headers, params=params, timeout=DEFAULT_TIMEOUT, verify=verify)
    if r.status_code != 200:
        raise RuntimeError(f"/sync falhou: {r.status_code} {r.text}")
    return r.json()


def join_room(hs: str, token: str, room_id: str, verify) -> None:
    url = hs.rstrip("/") + API_PREFIX + f"/rooms/{room_id}/join"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.post(url, json={}, headers=headers, timeout=DEFAULT_TIMEOUT, verify=verify)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Join falhou para {room_id}: {r.status_code} {r.text}")


def main():
    p = argparse.ArgumentParser(description="Aceita convites de salas para usuários Matrix listados em CSV.")
    p.add_argument("csv_path", help="Caminho do CSV de entrada (username,user_id,access_token,next_batch[,password])")
    p.add_argument("--homeserver", help="Base URL do homeserver. Ex.: https://matrix-dev.dv.techsmart.space")
    p.add_argument("--homeserver-col", default="homeserver", help="Nome da coluna do CSV que contém o homeserver ou domínio.")
    p.add_argument("--domain", help="Domínio para completar user_id quando necessário.")
    p.add_argument("--domain-col", default="domain", help="Nome da coluna do CSV que contém o domínio para montar o user_id.")
    p.add_argument("--default-password", help="Senha padrão para login quando access_token estiver vazio e não houver coluna password.")
    p.add_argument("--password-col", default="password", help="Nome da coluna de senha no CSV (opcional).")
    p.add_argument("--sleep", type=float, default=0.3, help="Espera entre operações para evitar rate limits.")
    p.add_argument("--insecure", action="store_true", help="Ignora verificação TLS (não recomendado).")
    p.add_argument("--ca-bundle", help="Caminho para CA bundle customizado (.pem).")
    p.add_argument("--write-back", action="store_true", help="Sobrescreve o CSV de entrada com access_token/next_batch atualizados.")
    p.add_argument("--out-csv", help="Escreve CSV de saída aqui (em vez de sobrescrever).")
    args = p.parse_args()

    if args.insecure and args.ca_bundle:
        print("Use --insecure OU --ca-bundle, não ambos.", file=sys.stderr)
        sys.exit(2)
    verify = False if args.insecure else (args.ca_bundle or True)

    # Ler CSV
    with open(args.csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        headers = [h for h in (reader.fieldnames or [])]
        if not headers:
            print("CSV sem cabeçalho.", file=sys.stderr)
            sys.exit(1)

        # Garante colunas padrão
        needed = ["username", "user_id", "access_token", "next_batch"]
        for col in needed:
            if col not in headers:
                headers.append(col)
        if args.password_col not in headers:
            # a coluna de senha é opcional; só adiciona se for escrever de volta
            pass

        rows = [deepcopy(row) for row in reader]

    processed = 0
    accepted_total = 0
    failed = 0

    for i, row in enumerate(rows, start=2):
        username = (row.get("username") or "").strip()
        user_id = (row.get("user_id") or "").strip()
        access_token = (row.get("access_token") or "").strip()
        next_batch = (row.get("next_batch") or "").strip()
        password = (row.get(args.password_col) or "").strip()

        try:
            user_id = normalize_user_id(user_id, username, row.get(args.domain_col) or args.domain)
        except Exception as e:
            print(f"[L{i}] ERRO user_id: {e}")
            failed += 1
            continue

        try:
            homeserver = resolve_homeserver_url(row, args)
        except Exception as e:
            print(f"[L{i}] ERRO homeserver: {e}")
            failed += 1
            continue

        token = access_token
        if not token:
            if not password and not args.default_password:
                print(f"[L{i}] Sem access_token e sem senha para {user_id}. Pulei.")
                failed += 1
                continue
            pwd = password or args.default_password
            try:
                token, _ = do_login(homeserver, user_id, pwd, verify=verify)
                # Atualiza token no CSV em memória
                row["access_token"] = token
                time.sleep(args.sleep)
            except Exception as e:
                print(f"[L{i}] ERRO login {user_id} em {homeserver}: {e}")
                failed += 1
                continue

        # Sync e aceitar convites
        try:
            sync = do_sync(homeserver, token, since=next_batch, verify=verify)
            invites = list((sync.get("rooms", {}) or {}).get("invite", {}).keys())
            if invites:
                print(f"[{user_id} @ {homeserver}] Convites: {len(invites)}")
                for rid in invites:
                    try:
                        join_room(homeserver, token, rid, verify=verify)
                        print(f"  ✔ joined {rid}")
                        accepted_total += 1
                        time.sleep(args.sleep)
                    except Exception as je:
                        print(f"  [join ERRO] {rid}: {je}")
            else:
                print(f"[{user_id} @ {homeserver}] Sem convites.")

            # Atualiza next_batch
            nb = sync.get("next_batch")
            if nb:
                row["next_batch"] = nb

        except Exception as e:
            # Se 401, tente relogar se possível
            msg = str(e)
            if "401" in msg and (password or args.default_password):
                try:
                    token, _ = do_login(homeserver, user_id, (password or args.default_password), verify=verify)
                    row["access_token"] = token
                    sync = do_sync(homeserver, token, since=next_batch, verify=verify)
                    invites = list((sync.get("rooms", {}) or {}).get("invite", {}).keys())
                    for rid in invites:
                        try:
                            join_room(homeserver, token, rid, verify=verify)
                            print(f"  ✔ joined {rid}")
                            accepted_total += 1
                            time.sleep(args.sleep)
                        except Exception as je:
                            print(f"  [join ERRO] {rid}: {je}")
                    nb = sync.get("next_batch")
                    if nb:
                        row["next_batch"] = nb
                except Exception as ee:
                    print(f"[L{i}] ERRO sync/login pós-401 {user_id} em {homeserver}: {ee}")
                    failed += 1
                    continue
            else:
                print(f"[L{i}] ERRO sync {user_id} em {homeserver}: {e}")
                failed += 1
                continue

        processed += 1
        time.sleep(args.sleep)

    print("\n==== RESUMO ====")
    print(f"Linhas processadas: {processed}")
    print(f"Convites aceitos: {accepted_total}")
    print(f"Falhas: {failed}")

    # Escrever CSV de saída, se solicitado
    out_path = None
    if args.write_back:
        out_path = args.csv_path
    elif args.out_csv:
        out_path = args.out_csv

    if out_path:
        # Garante que headers contenham as colunas que vamos escrever
        for col in ["username", "user_id", "access_token", "next_batch"]:
            if col not in headers:
                headers.append(col)
        if args.password_col in rows[0] and args.password_col not in headers:
            headers.append(args.password_col)

        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            w.writeheader()
            for row in rows:
                w.writerow(row)

if __name__ == "__main__":
    main()
