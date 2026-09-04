#!/usr/bin/env python3
"""
Configuração e roteamento compartilhados pelo setup de FEDERAÇÃO.

Diferença central em relação ao setup de servidor único (`setup/`):
aqui existem N homeservers, e cada usuário pertence a um deles. Todo o
roteamento ("em qual servidor esse usuário faz login?", "qual domínio
entra no Matrix ID dele?") acontece a partir do **prefixo** do username:

    userh01.000042  ->  homeserver "home01"  ->  @userh01.000042:home01-stg.exemplo.br
    userh02.000042  ->  homeserver "home02"  ->  @userh02.000042:home02-stg.exemplo.br

Os helpers genéricos (sessão HTTP com retry, escrita atômica de CSV,
logger) são reaproveitados de `setup/common.py` — este módulo apenas
acrescenta a camada multi-homeserver, sem modificar aquele arquivo.

Veja `.env.example` para a lista completa de variáveis suportadas.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_SETUP_DIR = _HERE.parent / "setup_homeserver"

# Carrega o .env DESTA pasta antes de qualquer outra coisa.
#
# Isso precisa acontecer aqui, e antes do `import common`, porque o
# `load_dotenv()` de setup/common.py roda na importação e procura o
# arquivo a partir da pasta setup/ (ou do diretório de trabalho) --
# ou seja, ele nunca enxergaria setup_federation/.env.
#
# `load_dotenv` não sobrescreve variáveis já presentes no ambiente, então
# a precedência fica: variáveis exportadas no shell > setup_federation/.env
# > setup/.env. É o comportamento esperado: dá para sobrepor qualquer
# parâmetro numa execução pontual sem editar arquivo nenhum.
try:
    from dotenv import load_dotenv

    load_dotenv(_HERE / ".env")
    load_dotenv(_SETUP_DIR / ".env")
except ImportError:
    # python-dotenv é opcional; sem ele, valem só as variáveis do shell.
    pass

# Reaproveita os utilitários genéricos do setup de servidor único.
if str(_SETUP_DIR) not in sys.path:
    sys.path.insert(0, str(_SETUP_DIR))

from common import (  # noqa: E402
    Config, build_session, get_logger, load_config,
    read_csv_rows, write_csv_rows,
)

__all__ = [
    "Homeserver", "FederationConfig", "load_federation_config",
    "build_session", "get_logger", "read_csv_rows", "write_csv_rows",
    "TOKENS_FIELDS",
]

TOKENS_FIELDS = ["username", "user_id", "access_token", "next_batch"]
HOMESERVER_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass
class Homeserver:
    """Um homeserver participante do teste de federação."""

    key: str      # identificador curto usado nos comandos/arquivos, ex.: "home01"
    url: str      # URL base da API cliente, ex.: "https://srv.home01-stg.exemplo.br"
    domain: str   # server_name do Matrix (o que entra no MXID), ex.: "home01-stg.exemplo.br"
    prefix: str   # prefixo dos usernames desse servidor, ex.: "userh01"

    def mxid(self, username: str) -> str:
        """Monta o Matrix ID completo de um usuário deste homeserver."""
        return f"@{username.lstrip('@')}:{self.domain}"


@dataclass
class FederationConfig:
    base: Config                      # timeouts, retries, TLS, workers (de setup/common.py)
    homeservers: Dict[str, Homeserver]

    users_csv: Path = field(default_factory=lambda: Path("data/federation/users.csv"))
    rooms_json: Path = field(default_factory=lambda: Path("data/federation/rooms.json"))
    tokens_csv: Path = field(default_factory=lambda: Path("data/federation/tokens.csv"))
    rooms_status_csv: Path = field(default_factory=lambda: Path("data/federation/rooms_status.csv"))
    failed_users_txt: Path = field(default_factory=lambda: Path("data/federation/failed_users.txt"))
    exports_dir: Path = field(default_factory=lambda: Path("data/federation/exports"))

    # ---- roteamento ------------------------------------------------

    def homeserver_for(self, username: str) -> Optional[Homeserver]:
        """Descobre a que homeserver um username pertence, pelo prefixo.

        Usa o prefixo mais longo que casar, para que prefixos parecidos
        (ex.: "user" e "userh01") não fiquem ambíguos.
        """
        local = username.lstrip("@").split(":")[0]
        matches = [hs for hs in self.homeservers.values() if local.startswith(hs.prefix)]
        if not matches:
            return None
        return max(matches, key=lambda hs: len(hs.prefix))

    def mxid_for(self, username: str) -> Optional[str]:
        hs = self.homeserver_for(username)
        return hs.mxid(username) if hs else None

    @property
    def keys(self) -> List[str]:
        return list(self.homeservers.keys())


def load_federation_config() -> FederationConfig:
    """Lê a configuração dos homeservers das variáveis de ambiente.

    Espera:
        FED_HOMESERVERS=home01,home02
        FED_HOME01_URL=...   FED_HOME01_DOMAIN=...   FED_HOME01_PREFIX=...
        FED_HOME02_URL=...   FED_HOME02_DOMAIN=...   FED_HOME02_PREFIX=...
    """
    # `require_server=False`: no cenário federado não existe um único
    # MATRIX_SERVER; aproveitamos só os parâmetros de HTTP/TLS/workers.
    base = load_config(require_server=False)

    raw_keys = os.environ.get("FED_HOMESERVERS", "").strip()
    if not raw_keys:
        sys.exit(
            "ERRO: a variável FED_HOMESERVERS não está definida.\n"
            "Configure o arquivo setup_federation/.env "
            "(copie de setup_federation/.env.example), ex.:\n"
            "  FED_HOMESERVERS=home01,home02"
        )

    keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    if len(keys) < 2:
        sys.exit(
            f"ERRO: FED_HOMESERVERS precisa listar pelo menos 2 homeservers "
            f"para um teste de federação (encontrado: {keys})."
        )

    duplicate_keys = sorted({key for key in keys if keys.count(key) > 1})
    if duplicate_keys:
        sys.exit(
            "ERRO: FED_HOMESERVERS contém homeserver(s) repetido(s): "
            f"{duplicate_keys}."
        )

    invalid_keys = [key for key in keys if not HOMESERVER_KEY_RE.fullmatch(key)]
    if invalid_keys:
        sys.exit(
            "ERRO: os nomes em FED_HOMESERVERS devem começar com uma letra e "
            "conter apenas letras, números, '_' ou '-'. "
            f"Inválido(s): {invalid_keys}."
        )

    # '-' e '_' viram o mesmo nome de variável (ex.: foo-bar e foo_bar
    # usam ambos FED_FOO_BAR_URL). Rejeitar a colisão evita que dois
    # homeservers leiam silenciosamente a mesma configuração.
    env_keys = [key.upper().replace("-", "_") for key in keys]
    duplicate_env_keys = sorted({key for key in env_keys if env_keys.count(key) > 1})
    if duplicate_env_keys:
        sys.exit(
            "ERRO: nomes de homeserver colidem nas variáveis FED_<HS>_*: "
            f"{duplicate_env_keys}. Use nomes distintos também após trocar '-' por '_'."
        )

    homeservers: Dict[str, Homeserver] = {}
    for key, env_key in zip(keys, env_keys):
        url = os.environ.get(f"FED_{env_key}_URL", "").strip().rstrip("/")
        domain = os.environ.get(f"FED_{env_key}_DOMAIN", "").strip()
        prefix = os.environ.get(f"FED_{env_key}_PREFIX", "").strip()

        missing = [
            name for name, value in (
                (f"FED_{env_key}_URL", url),
                (f"FED_{env_key}_DOMAIN", domain),
                (f"FED_{env_key}_PREFIX", prefix),
            ) if not value
        ]
        if missing:
            sys.exit(
                f"ERRO: configuração incompleta para o homeserver '{key}'.\n"
                f"Faltando: {', '.join(missing)}\n\n"
                "Atenção: URL e DOMAIN normalmente NÃO são iguais.\n"
                "  URL    = onde a API cliente responde  (ex.: https://srv.home01.exemplo.br)\n"
                "  DOMAIN = o server_name do Matrix, que aparece no MXID\n"
                "           (ex.: home01.exemplo.br) -- é o que vale para federação."
            )

        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        homeservers[key] = Homeserver(key=key, url=url, domain=domain, prefix=prefix)

    prefixes = [hs.prefix for hs in homeservers.values()]
    if len(set(prefixes)) != len(prefixes):
        sys.exit(f"ERRO: os prefixos de usuário precisam ser únicos por homeserver: {prefixes}")

    domains = [hs.domain for hs in homeservers.values()]
    if len(set(domains)) != len(domains):
        sys.exit(f"ERRO: os domínios precisam ser únicos por homeserver: {domains}")

    data_dir = Path(os.environ.get("MATRIX_DATA_DIR", "data/federation"))
    data_dir.mkdir(parents=True, exist_ok=True)

    return FederationConfig(
        base=base,
        homeservers=homeservers,
        users_csv=Path(os.environ.get("USERS_CSV", data_dir / "users.csv")),
        rooms_json=Path(os.environ.get("ROOMS_JSON", data_dir / "rooms.json")),
        tokens_csv=Path(os.environ.get("TOKENS_CSV", data_dir / "tokens.csv")),
        rooms_status_csv=Path(os.environ.get("ROOMS_STATUS_CSV", data_dir / "rooms_status.csv")),
        failed_users_txt=Path(os.environ.get("FAILED_USERS_TXT", data_dir / "failed_users.txt")),
        exports_dir=Path(os.environ.get("FED_EXPORTS_DIR", data_dir / "exports")),
    )


def describe(fedcfg: FederationConfig, log) -> None:
    """Loga o mapeamento de homeservers -- útil para conferir antes de
    disparar qualquer coisa contra o ambiente."""
    log.info("Homeservers configurados:")
    for hs in fedcfg.homeservers.values():
        log.info("  %-8s url=%s  domain=%s  prefixo=%s*", hs.key, hs.url, hs.domain, hs.prefix)
