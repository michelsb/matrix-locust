#!/usr/bin/env bash
# Executa o setup completo do cenário de FEDERAÇÃO, na ordem correta.
# Deve ser rodado a partir da raiz do repositório.
#
# Uso:
#   setup_federation/run_all.sh [NUM_USERS]
#
# Variáveis úteis:
#   ACTIVATE=home01   homeserver que será usado como gerador de carga
#                     (padrão: o primeiro de FED_HOMESERVERS)
#
# Para em qualquer erro (set -e), para que o problema seja corrigido
# antes de seguir adiante.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

NUM_USERS="${1:-}"
PYTHON=(poetry run python)

echo "==> [1/7] Gerando usuários dos homeservers configurados..."
"${PYTHON[@]}" "${SCRIPT_DIR}/01_generate_users.py" ${NUM_USERS}

echo "==> [2/7] Gerando salas mistas entre os homeservers..."
"${PYTHON[@]}" "${SCRIPT_DIR}/02_generate_rooms.py"

echo "==> [3/7] Registrando cada usuário no seu homeserver..."
"${PYTHON[@]}" "${SCRIPT_DIR}/03_register_users.py"

echo "==> [4/7] Criando as salas e convidando os membros remotos..."
"${PYTHON[@]}" "${SCRIPT_DIR}/04_create_rooms.py"

echo "==> [5/7] Aceitando os convites (federated join)..."
# 2 passes: convites federados podem levar alguns segundos para chegar
# ao homeserver remoto.
"${PYTHON[@]}" "${SCRIPT_DIR}/05_accept_invites.py" --passes 2

echo "==> [6/7] Exportando users/tokens por homeserver para o Locust..."
if [[ -n "${ACTIVATE:-}" ]]; then
  "${PYTHON[@]}" "${SCRIPT_DIR}/06_export_locust_files.py" --activate "${ACTIVATE}"
else
  "${PYTHON[@]}" "${SCRIPT_DIR}/06_export_locust_files.py"
fi

echo "==> [7/7] Verificando se a federação está ativa..."
"${PYTHON[@]}" "${SCRIPT_DIR}/07_verify_federation.py"

echo
echo "==> Setup de federação concluído."
echo "    Arquivos em data/federation/:"
echo "      users.csv, rooms.json, tokens.csv, rooms_status.csv"
echo "      exports/<homeserver>/users.csv, exports/<homeserver>/tokens.csv"
echo
echo "    Próximo passo: ative um homeserver e rode o teste de carga."
echo "      poetry run python setup_federation/06_export_locust_files.py --activate home01"
echo "      poetry run python experiments/run_factorial.py --host <URL do home01> --data-dir data/federation/exports/home01 --no-prometheus"
