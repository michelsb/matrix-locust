#!/usr/bin/env bash
# Executa os 5 passos de setup do matrix-locust em sequência, na ordem
# correta. Deve ser rodado a partir da raiz do repositório.
#
# Uso:
#   setup_homeserver/run_all.sh [NUM_USERS]
#
# Se NUM_USERS não for informado, usa o valor da variável de ambiente
# NUM_USERS (ou o padrão de setup_homeserver/01_generate_users.py).
#
# O script para imediatamente se qualquer passo falhar (set -e), para
# que o problema seja corrigido antes de prosseguir para o próximo.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

NUM_USERS="${1:-}"
PYTHON=(poetry run python)

echo "==> [1/5] Gerando usuários..."
"${PYTHON[@]}" "${SCRIPT_DIR}/01_generate_users.py" ${NUM_USERS}

echo "==> [2/5] Gerando salas..."
"${PYTHON[@]}" "${SCRIPT_DIR}/02_generate_rooms.py"

echo "==> [3/5] Registrando usuários no homeserver..."
"${PYTHON[@]}" "${SCRIPT_DIR}/03_register_users.py"

echo "==> [4/5] Criando salas e enviando convites..."
"${PYTHON[@]}" "${SCRIPT_DIR}/04_create_rooms.py"

echo "==> [5/5] Aceitando convites pendentes..."
"${PYTHON[@]}" "${SCRIPT_DIR}/05_accept_invites.py"

echo "==> Setup concluído. Arquivos gerados em data/homeserver/:"
echo "    users.csv, rooms.json, tokens.csv, rooms_status.csv"
echo "    Agora você pode rodar o teste de carga (ver README principal)."
