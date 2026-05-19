#!/usr/bin/env bash
# Remove o karuta systemd user service.
set -euo pipefail

SERVICE_FILE="${HOME}/.config/systemd/user/karuta.service"

if systemctl --user is-active karuta >/dev/null 2>&1; then
    systemctl --user stop karuta || true
fi

systemctl --user disable karuta 2>/dev/null || true
rm -f "${SERVICE_FILE}"
systemctl --user daemon-reload

echo "karuta.service removido."
