#!/usr/bin/env python3
"""
Configuração e utilitários compartilhados pelos scripts de setup.

Toda configuração de ambiente (URL do servidor, domínio, timeouts,
concorrência, etc.) é lida de variáveis de ambiente / arquivo `.env`
por este módulo, para que nenhum script precise de valores fixos no
código-fonte (hostnames, credenciais, etc.).

Veja `.env.example` para a lista completa de variáveis suportadas.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit(
        "ERRO: python-dotenv não está instalado. Execute:\n"
        "  poetry install"
    )

# Usa o diretório deste módulo, independentemente do diretório a partir
# do qual os scripts (ou `python -c`) foram executados.
ENV_FILE = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_FILE)

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_PREFIX = "/_matrix/client/v3"


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


@dataclass
class Config:
    matrix_server: str
    domain: str
    verify_tls: bool = True
    http_timeout: int = 30
    http_retries: int = 3
    http_backoff: float = 0.5
    request_sleep: float = 0.0
    workers: int = 8
    log_level: str = "INFO"

    users_csv: Path = field(default_factory=lambda: Path("data/homeserver/users.csv"))
    rooms_json: Path = field(default_factory=lambda: Path("data/homeserver/rooms.json"))
    tokens_csv: Path = field(default_factory=lambda: Path("data/homeserver/tokens.csv"))
    failed_users_txt: Path = field(default_factory=lambda: Path("data/homeserver/failed_users.txt"))
    rooms_status_csv: Path = field(default_factory=lambda: Path("data/homeserver/rooms_status.csv"))


def load_config(require_server: bool = True) -> Config:
    matrix_server = os.environ.get("MATRIX_SERVER", "").rstrip("/")
    if not matrix_server and require_server:
        sys.exit(
            "ERRO: a variável de ambiente MATRIX_SERVER não está definida.\n"
            "Configure-a no arquivo setup_homeserver/.env "
            "(copie setup_homeserver/.env.example) "
            "ou exporte-a no shell, ex.:\n"
            "  export MATRIX_SERVER=https://matrix.example.com"
        )
    if matrix_server and not matrix_server.startswith(("http://", "https://")):
        matrix_server = f"https://{matrix_server}"

    domain = os.environ.get("MATRIX_DOMAIN", "").strip()
    if not domain:
        domain = urlparse(matrix_server).hostname or ""

    data_dir = Path(os.environ.get("MATRIX_DATA_DIR", "data/homeserver"))
    data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        matrix_server=matrix_server,
        domain=domain,
        verify_tls=_env_bool("VERIFY_TLS", True),
        http_timeout=_env_int("HTTP_TIMEOUT", 30),
        http_retries=_env_int("HTTP_RETRIES", 3),
        http_backoff=_env_float("HTTP_BACKOFF", 0.5),
        request_sleep=_env_float("REQUEST_SLEEP", 0.0),
        workers=_env_int("WORKERS", 8),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        users_csv=Path(os.environ.get("USERS_CSV", data_dir / "users.csv")),
        rooms_json=Path(os.environ.get("ROOMS_JSON", data_dir / "rooms.json")),
        tokens_csv=Path(os.environ.get("TOKENS_CSV", data_dir / "tokens.csv")),
        failed_users_txt=Path(os.environ.get("FAILED_USERS_TXT", data_dir / "failed_users.txt")),
        rooms_status_csv=Path(os.environ.get("ROOMS_STATUS_CSV", data_dir / "rooms_status.csv")),
    )


def get_logger(name: str, level: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"
        ))
        logger.addHandler(handler)
    logger.setLevel(level or "INFO")
    return logger


def build_session(cfg: Config) -> requests.Session:
    """Sessão HTTP com retry/backoff automático para erros transitórios
    (rate limit e indisponibilidade momentânea do servidor)."""
    session = requests.Session()
    retry = Retry(
        total=cfg.http_retries,
        backoff_factor=cfg.http_backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=None,  # também tenta novamente em POST
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.verify = cfg.verify_tls
    return session


def split_username(raw: str) -> "tuple[str, Optional[str]]":
    """Separa o campo "username" do users.csv em (localpart, domínio_alvo).

    O `generate_users.py` grava o username como "localpart:dominio" para
    testes federados (ou "localpart:" sem domínio nos testes de um único
    servidor). Esse sufixo é apenas um roteamento lógico: NÃO faz parte
    do username real no Matrix e precisa ser removido antes de qualquer
    chamada à API (é exatamente o que `MatrixUser.set_user` faz no
    restante do projeto). Retorna o domínio embutido (ou None se vazio),
    para permitir montar o Matrix ID completo do usuário.
    """
    raw = raw.strip()
    if raw.startswith("@") and ":" in raw[1:]:
        # já é um Matrix ID completo (ex.: vindo de tokens.csv)
        local, _, dom = raw[1:].partition(":")
        return local, (dom or None)
    local, _, dom = raw.partition(":")
    return local, (dom or None)


def mxid(username: str, domain: str) -> str:
    """Constrói o Matrix ID completo (@localpart:domain) a partir do
    campo "username" bruto (users.csv/tokens.csv), usando `domain` como
    padrão quando não houver domínio federado embutido."""
    local, embedded_domain = split_username(username)
    return f"@{local}:{embedded_domain or domain}"


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv_rows(path: Path, fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    """Escreve o CSV em um arquivo temporário e faz `rename` atômico, para
    nunca deixar um arquivo de saída corrompido/parcial em caso de falha."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    tmp_path.replace(path)
