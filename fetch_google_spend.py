#!/usr/bin/env python3
"""Google Ads → data/google_spend.json

Gasto por campanha por dia, no mesmo formato que fetch_meta_spend.py produz
para o Meta — é o que faz CPL, CAC e ROAS enxergarem os leads de Google, que
hoje entram no funil sem custo associado.

A chave de junção é o NOME DA CAMPANHA: os leads do Kommo trazem o nome da
campanha do Google no campo `audience` (via utm_campaign), então o mapa aqui
é {nome_da_campanha: {'YYYY-MM-DD': gasto}}.

Credenciais (env ou .env):
    GOOGLE_ADS_DEVELOPER_TOKEN     API Center da MCC
    GOOGLE_ADS_CLIENT_ID           OAuth client (Desktop app)
    GOOGLE_ADS_CLIENT_SECRET       idem
    GOOGLE_ADS_REFRESH_TOKEN       gerado por google_oauth_setup.py
    GOOGLE_ADS_CUSTOMER_ID         conta anunciante (só dígitos ou com hífen)
    GOOGLE_ADS_LOGIN_CUSTOMER_ID   opcional — a MCC, se a conta estiver sob uma
"""

import json
import os
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import requests

API_VERSION = "v25"
BASE = f"https://googleads.googleapis.com/{API_VERSION}"
TOKEN_URL = "https://oauth2.googleapis.com/token"

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date.today()


def _load_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()


def _req(nome):
    v = os.environ.get(nome, "").strip()
    if not v:
        raise SystemExit(f"ABORTA: {nome} não definido (env ou .env).")
    return v


def _so_digitos(cid):
    return "".join(c for c in cid if c.isdigit())


DEV_TOKEN = _req("GOOGLE_ADS_DEVELOPER_TOKEN")
CLIENT_ID = _req("GOOGLE_ADS_CLIENT_ID")
CLIENT_SECRET = _req("GOOGLE_ADS_CLIENT_SECRET")
REFRESH_TOKEN = _req("GOOGLE_ADS_REFRESH_TOKEN")
CUSTOMER_ID = _so_digitos(_req("GOOGLE_ADS_CUSTOMER_ID"))
LOGIN_CID = _so_digitos(os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", ""))


def access_token():
    """Troca o refresh_token por um access token de vida curta.

    O refresh_token não expira sozinho — é isso que permite a coleta agendada
    rodar indefinidamente, diferente do Meta.
    """
    r = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=60)
    if not r.ok:
        raise SystemExit(
            f"ABORTA: Google recusou o refresh_token ({r.status_code}).\n"
            f"{r.text[:300]}\n"
            f"Se for invalid_grant, o refresh foi revogado — rode google_oauth_setup.py de novo."
        )
    return r.json()["access_token"]


def search(query, token):
    """searchStream devolve uma lista de chunks, cada um com seus results."""
    hdrs = {
        "Authorization": f"Bearer {token}",
        "developer-token": DEV_TOKEN,
        "Content-Type": "application/json",
    }
    if LOGIN_CID:
        hdrs["login-customer-id"] = LOGIN_CID

    r = requests.post(f"{BASE}/customers/{CUSTOMER_ID}/googleAds:searchStream",
                      headers=hdrs, json={"query": query}, timeout=180)

    if r.status_code in (401, 403):
        raise SystemExit(
            f"ABORTA: Google Ads recusou o acesso ({r.status_code}).\n"
            f"{r.text[:400]}\n"
            f"Causas comuns: developer token sem aprovação, conta {CUSTOMER_ID} "
            f"não gerenciada pela MCC, ou GOOGLE_ADS_LOGIN_CUSTOMER_ID faltando."
        )
    if not r.ok:
        raise SystemExit(f"ABORTA: Google Ads respondeu {r.status_code} — {r.text[:400]}")

    linhas = []
    for chunk in r.json():
        linhas.extend(chunk.get("results", []))
    return linhas


def main():
    print("\n" + "=" * 62)
    print(f"  Google Ads → data/google_spend.json")
    print(f"  Conta: {CUSTOMER_ID}" + (f" · via MCC {LOGIN_CID}" if LOGIN_CID else ""))
    print(f"  Período: {PERIOD_START} → {PERIOD_END}")
    print("=" * 62 + "\n")

    token = access_token()
    print("  OAuth ok\n")

    ini, fim = PERIOD_START.isoformat(), PERIOD_END.isoformat()

    print("  Gasto por campanha por dia...")
    linhas = search(f"""
        SELECT campaign.name, segments.date, metrics.cost_micros,
               metrics.impressions, metrics.clicks
        FROM campaign
        WHERE segments.date BETWEEN '{ini}' AND '{fim}'
          AND metrics.cost_micros > 0
    """, token)

    spend = defaultdict(lambda: defaultdict(float))
    imps, clks = defaultdict(float), defaultdict(float)
    for l in linhas:
        nome = l["campaign"]["name"]
        dia = l["segments"]["date"]
        m = l.get("metrics", {})
        # cost_micros: milionésimos da moeda da conta.
        spend[nome][dia] += int(m.get("costMicros", 0)) / 1_000_000
        imps[nome] += int(m.get("impressions", 0))
        clks[nome] += int(m.get("clicks", 0))

    print(f"    {len(spend)} campanhas com gasto:")
    for k, dias in sorted(spend.items(), key=lambda x: -sum(x[1].values()))[:12]:
        print(f"      R$ {sum(dias.values()):>10,.2f}  {k}")

    print("\n  Status das campanhas...")
    status = {}
    for l in search("SELECT campaign.name, campaign.status FROM campaign", token):
        status[l["campaign"]["name"]] = l["campaign"].get("status", "UNKNOWN")
    print(f"    {len(status)} campanhas")

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "account": CUSTOMER_ID,
        "period": {"start": str(PERIOD_START), "end": str(PERIOD_END)},
        "campaign": {k: {d: round(v, 2) for d, v in dias.items()}
                     for k, dias in spend.items()},
        "campaign_totals": {k: {"impressions": imps[k], "clicks": clks[k]} for k in spend},
        "campaign_status": status,
    }

    out_path = Path(__file__).resolve().parent / "data/google_spend.json"
    # Mesma proteção do Meta: coleta vazia não sobrescreve dado bom.
    if not spend and out_path.exists():
        print("\n  ⚠️  API não devolveu gasto nenhum. Mantendo o arquivo anterior.")
        return

    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    total = sum(sum(d.values()) for d in spend.values())
    print(f"\n  ✅ Salvo em {out_path} · investimento total R$ {total:,.2f}\n")


if __name__ == "__main__":
    main()
