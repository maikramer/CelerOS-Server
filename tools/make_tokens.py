#!/usr/bin/env python3
"""Gera o JSON de tokens nomeados do CelerOS Hub (secret celeros_hub_tokens).

Cada token tem nome (identifica no log de auditoria) e escopos:
  apps    -> publicar/remover apps da loja
  updates -> publicar firmware OTA nos canais
  *       -> tudo

Uso:
    python3 tools/make_tokens.py                      # 1 p/ voce + 1 p/ CI
    python3 tools/make_tokens.py --only maikeu        # regenera so um
    python3 tools/make_tokens.py --scopes updates     # CI so publica OTA

O JSON vai para stdout (guarde num arquivo com permissao 600 e crie o
secret); os tokens em claro so aparecem aqui — o hub guarda o secret, o
audit log guarda apenas o NOME de quem publicou.

Rotacao/revogacao: gere um JSON novo sem o token revogado e recrie o secret
+ `denv update celeros-hub -t <tag>` (secrets sao imutaveis no Swarm).
"""

import argparse
import json
import secrets
import sys


def gen() -> str:
    return "chk_" + secrets.token_urlsafe(32)


def main() -> None:
    ap = argparse.ArgumentParser(description="gera tokens do CelerOS Hub")
    ap.add_argument("--only", help="gera apenas este nome (regeneracao)")
    ap.add_argument("--scopes", default="*",
                    help="escopos dos tokens gerados (apps|updates|*)")
    ap.add_argument("--ci-name", default="ci")
    args = ap.parse_args()

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    for s in scopes:
        if s not in ("apps", "updates", "*"):
            print(f"erro: escopo invalido: {s}", file=sys.stderr)
            sys.exit(1)

    tokens = []
    if args.only:
        tokens.append({"name": args.only, "token": gen(), "scopes": scopes})
    else:
        tokens.append({"name": "maikeu", "token": gen(), "scopes": ["*"]})
        tokens.append({"name": args.ci_name, "token": gen(), "scopes": scopes})

    print("# tokens em claro — guarde com 600; NAO faca commit")
    for t in tokens:
        print(f"# {t['name']}: {t['token']}")
    print()
    print(json.dumps(tokens, indent=2))


if __name__ == "__main__":
    main()
