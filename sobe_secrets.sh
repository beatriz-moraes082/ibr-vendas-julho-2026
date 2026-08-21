#!/usr/bin/env bash
# Envia para os secrets do GitHub as credenciais que estão no .env local.
#
#   ./sobe_secrets.sh              envia todas as chaves conhecidas
#   ./sobe_secrets.sh KOMMO_TOKEN  envia só a que você nomear
#
# Nenhum valor é impresso na tela nem entra no histórico do shell.
# O developer token do Google não vive no .env — para ele use:
#   gh secret set GOOGLE_ADS_DEVELOPER_TOKEN --repo <repo>

set -uo pipefail

REPO="beatriz-moraes082/ibr-vendas-julho-2026"
ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.env"

CHAVES_PADRAO=(
  KOMMO_TOKEN
  META_TOKEN
  GOOGLE_ADS_CLIENT_ID
  GOOGLE_ADS_CLIENT_SECRET
  GOOGLE_ADS_REFRESH_TOKEN
)

if [ ! -f "$ENV_FILE" ]; then
  echo "Não achei o .env em $ENV_FILE" >&2
  exit 1
fi

if [ "$#" -gt 0 ]; then
  CHAVES=("$@")
else
  CHAVES=("${CHAVES_PADRAO[@]}")
fi

falhas=0
for K in "${CHAVES[@]}"; do
  # cut -f2- preserva valores que contenham '=' (JWT, por exemplo)
  V=$(grep "^${K}=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r\n')
  if [ -z "$V" ]; then
    printf '  %-28s sem valor no .env — pulado\n' "$K"
    continue
  fi
  if printf '%s' "$V" | gh secret set "$K" --repo "$REPO" >/dev/null 2>&1; then
    printf '  %-28s enviado (%s caracteres)\n' "$K" "${#V}"
  else
    printf '  %-28s FALHOU\n' "$K"
    falhas=$((falhas + 1))
  fi
done

echo
echo "Secrets agora no repositório:"
gh secret list --repo "$REPO" | sed 's/^/  /'

exit $falhas
