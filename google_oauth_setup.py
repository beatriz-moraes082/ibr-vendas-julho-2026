#!/usr/bin/env python3
"""Gera o refresh_token do Google Ads e grava no .env local.

Roda uma vez, na máquina de quem tem acesso à conta. O refresh_token não
expira sozinho — depois disso a coleta se vira sem intervenção, diferente
do Meta, cujo token vence a cada ~60 dias.

    python3 google_oauth_setup.py

Nada é enviado para lugar nenhum além do próprio Google: o client_secret é
digitado com eco desligado e as credenciais terminam só no .env, que está
no .gitignore.
"""

import getpass
import http.server
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

ENV = Path(__file__).resolve().parent / ".env"
PORT = 8765
REDIRECT = f"http://localhost:{PORT}"
SCOPE = "https://www.googleapis.com/auth/adwords"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def env_atual():
    if not ENV.exists():
        return {}
    vals = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def grava_env(novos):
    """Reescreve só as chaves informadas, preservando o resto do arquivo."""
    texto = ENV.read_text() if ENV.exists() else ""
    for chave, valor in novos.items():
        linha = f"{chave}={valor}"
        texto, n = re.subn(rf"(?m)^{re.escape(chave)}=.*$", lambda m: linha, texto)
        if n == 0:
            texto = (texto.rstrip("\n") + "\n" if texto else "") + linha + "\n"
    ENV.write_text(texto)


class Handler(http.server.BaseHTTPRequestHandler):
    resultado = {}

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        campos = {k: v[0] for k, v in q.items()}
        # O navegador também pede /favicon.ico; sem esse filtro esse pedido
        # contaria como "resposta recebida" e mataria a espera cedo demais.
        if "code" not in campos and "error" not in campos:
            self.send_response(204)
            self.end_headers()
            return

        Handler.resultado = campos
        ok = "code" in campos
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("Autorizado. Pode fechar esta aba e voltar ao terminal."
               if ok else
               f"Falhou: {campos.get('error', 'sem código')}")
        self.wfile.write(f"<html><body style='font-family:system-ui;padding:40px'>"
                         f"<h2>{msg}</h2></body></html>".encode())

    def log_message(self, *a):
        pass  # silencia o log do http.server


def do_json_baixado():
    """Lê o client_secret_*.json que o Console oferece ao criar o client.

    Evita a colagem manual, que é onde o client_id costuma truncar.
    """
    candidatos = sorted(
        list(Path.home().glob("Downloads/client_secret*.json"))
        + list(Path(__file__).resolve().parent.glob("client_secret*.json")),
        key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidatos:
        try:
            dados = json.loads(p.read_text())
            bloco = dados.get("installed") or dados.get("web") or {}
            if bloco.get("client_id") and bloco.get("client_secret"):
                return p, bloco["client_id"].strip(), bloco["client_secret"].strip()
        except Exception:
            continue
    return None, None, None


def main():
    atual = env_atual()

    print("\n  Configuração OAuth do Google Ads")
    print("  ─────────────────────────────────")
    print("  Credenciais do OAuth client (App para computador) criado em")
    print("  https://console.cloud.google.com/apis/credentials\n")

    caminho, client_id, client_secret = do_json_baixado()
    if caminho:
        print(f"  Achei o JSON do client em {caminho.name}")
        if input("  usar esse? [S/n] ").strip().lower() != "n":
            print("  Usando client_id e client_secret do arquivo.\n")
            return continuar(client_id, client_secret)
        client_id = client_secret = None

    client_id = atual.get("GOOGLE_ADS_CLIENT_ID") or ""
    if client_id:
        print(f"  client_id já no .env (…{client_id[-24:]})")
        if input("  usar esse? [S/n] ").strip().lower() == "n":
            client_id = ""
    if not client_id:
        client_id = input("  client_id: ").strip()

    # Vale conferir antes de abrir o navegador: um client_id malformado só
    # aparece lá na frente como "Erro 401: invalid_client", que não diz o motivo.
    if not re.fullmatch(r"\d+-[a-z0-9]+\.apps\.googleusercontent\.com", client_id):
        print("\n  ⚠️  Esse client_id não tem o formato que o Google usa.")
        print("      Esperado:  123456789012-abcdef123456.apps.googleusercontent.com")
        print(f"      Recebido:  {client_id[:20]}{'…' if len(client_id) > 20 else ''}"
              f"  ({len(client_id)} caracteres)")
        if client_id.startswith("AIza"):
            print("      Isso parece uma API key, não um OAuth client ID.")
        elif len(client_id) <= 30 and "." not in client_id:
            print("      Isso parece o developer token ou o ID do projeto.")
        print("\n      Pegue o correto em https://console.cloud.google.com/apis/credentials")
        print("      seção 'OAuth 2.0 Client IDs' → o client do tipo Desktop app.")
        print("      Se a seção estiver vazia, o client ainda não foi criado.\n")
        if input("  Continuar mesmo assim? [s/N] ").strip().lower() != "s":
            raise SystemExit("  Cancelado.")

    client_secret = getpass.getpass("  client_secret (não aparece): ").strip()
    return continuar(client_id, client_secret)


def continuar(client_id, client_secret):
    """Roda o fluxo OAuth e grava o resultado. Comum aos dois caminhos de entrada."""
    if not client_id or not client_secret:
        raise SystemExit("  client_id e client_secret são obrigatórios.")

    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # sem isso não vem refresh_token
        "prompt": "consent",        # força vir mesmo se já autorizou antes
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    Handler.resultado = {}
    try:
        servidor = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        raise SystemExit(
            f"  ABORTA: não consegui abrir a porta {PORT} ({e}).\n"
            f"  Feche outra execução deste script que possa estar rodando."
        )

    # serve_forever numa thread: fica no ar até o code chegar. A versão
    # anterior fechava o servidor logo depois de abrir o navegador, então o
    # retorno do Google batia numa porta morta (ERR_CONNECTION_REFUSED).
    threading.Thread(target=servidor.serve_forever, daemon=True).start()

    print(f"\n  Abrindo o navegador para autorizar…")
    print(f"  Se não abrir, cole esta URL:\n\n  {url}\n")
    webbrowser.open(url)
    print("  Aguardando a autorização (até 5 min)…")

    limite = time.time() + 300
    while not Handler.resultado:
        if time.time() > limite:
            servidor.shutdown()
            servidor.server_close()
            raise SystemExit("  ABORTA: tempo esgotado sem resposta do Google.")
        time.sleep(0.3)

    servidor.shutdown()
    servidor.server_close()
    r = Handler.resultado
    if r.get("state") != state:
        raise SystemExit("  ABORTA: state não confere — possível interferência.")
    if "code" not in r:
        raise SystemExit(f"  ABORTA: {r.get('error', 'nenhum código recebido')}")

    print("  Trocando o código por tokens…")
    resp = requests.post(TOKEN_URL, data={
        "code": r["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    }, timeout=60)

    if not resp.ok:
        raise SystemExit(f"  ABORTA: Google respondeu {resp.status_code} — {resp.text[:300]}")

    tok = resp.json()
    refresh = tok.get("refresh_token")
    if not refresh:
        raise SystemExit(
            "  ABORTA: veio access_token mas não refresh_token.\n"
            "  Revogue o acesso em https://myaccount.google.com/permissions e rode de novo."
        )

    grava_env({
        "GOOGLE_ADS_CLIENT_ID": client_id,
        "GOOGLE_ADS_CLIENT_SECRET": client_secret,
        "GOOGLE_ADS_REFRESH_TOKEN": refresh,
    })

    print(f"\n  ✅ refresh_token gerado ({len(refresh)} caracteres) e gravado no .env")
    print("\n  Falta levar para o GitHub — rode cada um e cole o valor quando pedir:")
    print("\n    gh secret set GOOGLE_ADS_CLIENT_ID --repo beatriz-moraes082/mensuracao-ibr")
    print("    gh secret set GOOGLE_ADS_CLIENT_SECRET --repo beatriz-moraes082/mensuracao-ibr")
    print("    gh secret set GOOGLE_ADS_REFRESH_TOKEN --repo beatriz-moraes082/mensuracao-ibr")
    print("    gh secret set GOOGLE_ADS_DEVELOPER_TOKEN --repo beatriz-moraes082/mensuracao-ibr")
    print("\n  Os valores dos três primeiros estão no .env; o developer token vem do API Center.\n")


if __name__ == "__main__":
    main()
