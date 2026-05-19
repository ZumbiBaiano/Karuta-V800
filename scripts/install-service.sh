#!/usr/bin/env bash
# Instala o karuta como systemd user service.
# Uso: ./scripts/install-service.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${SERVICE_DIR}/karuta.service"
TEMPLATE="${PROJECT_DIR}/scripts/karuta.service.template"

if [[ ! -f "${PROJECT_DIR}/.venv/bin/karuta" ]]; then
    echo "ERRO: ${PROJECT_DIR}/.venv/bin/karuta nao encontrado." >&2
    echo "Rode primeiro: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
    exit 1
fi

if [[ ! -f "${PROJECT_DIR}/config.yaml" ]]; then
    echo "ERRO: ${PROJECT_DIR}/config.yaml nao existe." >&2
    echo "Copie config.example.yaml para config.yaml e ajuste." >&2
    exit 1
fi

mkdir -p "${SERVICE_DIR}"

# Substitui placeholders no template
sed \
    -e "s|{{PROJECT_DIR}}|${PROJECT_DIR}|g" \
    -e "s|{{UID}}|$(id -u)|g" \
    "${TEMPLATE}" > "${SERVICE_FILE}"

systemctl --user daemon-reload
systemctl --user enable karuta.service

# Permite que o servico continue rodando mesmo apos logout (linger).
# Em WSL2 nao eh estritamente necessario porque o WSL desliga quando
# nao tem processos, mas nao machuca habilitar.
if command -v loginctl >/dev/null 2>&1; then
    sudo loginctl enable-linger "$(whoami)" 2>/dev/null || true
fi

cat <<EOF

karuta.service instalado em: ${SERVICE_FILE}

Comandos uteis:
  systemctl --user start    karuta     # iniciar
  systemctl --user stop     karuta     # parar
  systemctl --user restart  karuta     # reiniciar
  systemctl --user status   karuta     # status
  journalctl --user -u karuta -f       # logs ao vivo
  journalctl --user -u karuta -n 200   # ultimas 200 linhas
EOF
