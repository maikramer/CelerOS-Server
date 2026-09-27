#!/usr/bin/env python3
"""Publica um app no CelerOS Hub.

Empacota a pasta do app (app.json + main.js [+ icon.png]) num zip e envia
para POST /admin/apps. Token: --token ou env CELER_HUB_TOKEN.

Uso:
    python3 tools/publish_app.py caminho/da/pasta-do-app \
        [--hub https://os.celer.tec.br] [--token TOKEN]

Validacoes do servidor: packageName (ex.: celeros.meuapp), version semver,
campos obrigatorios; catalogo (store/*.json) e regenerado no publish.
"""

import argparse
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib import error, request

REQUIRED = ("name", "packageName", "version", "author", "description")


def die(msg: str) -> None:
    print(f"erro: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="publica um app no CelerOS Hub")
    ap.add_argument("folder", help="pasta do app (app.json + main.js)")
    ap.add_argument("--hub", default=os.environ.get("CELER_HUB", "https://os.celer.tec.br"))
    ap.add_argument("--token", default=os.environ.get("CELER_HUB_TOKEN", ""))
    args = ap.parse_args()

    src = Path(args.folder).expanduser().resolve()
    meta_path = src / "app.json"
    code_path = src / "main.js"
    if not meta_path.is_file() or not code_path.is_file():
        die(f"{src} precisa conter app.json e main.js")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except ValueError as e:
        die(f"app.json invalido: {e}")
    for f in REQUIRED:
        if not str(meta.get(f) or "").strip():
            die(f"app.json sem campo obrigatorio: {f}")

    # sanity local: packageName/version sao validados de novo no servidor
    import re
    if not re.match(r"^[a-z0-9]+(\.[a-z0-9]+)+$", meta["packageName"]):
        die("packageName deve ser tipo celeros.meuapp (minusculo, com ponto)")
    if not re.match(r"^\d+\.\d+\.\d+$", meta["version"]):
        die("version deve ser semver x.y.z")

    token = args.token
    if not token:
        die("sem token: use --token ou export CELER_HUB_TOKEN=...")

    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / "app.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(meta_path, "app.json")
            zf.write(code_path, "main.js")
            icon = src / "icon.png"
            if icon.is_file():
                zf.write(icon, "icon.png")
        blob = zpath.read_bytes()

    # multipart (campo "file") como o endpoint FastAPI espera
    boundary = "----celeroshub7d1f2c"
    part = (
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"file\"; filename=\"app.zip\"\r\n"
        "Content-Type: application/zip\r\n\r\n"
    ).encode()
    req = request.Request(
        f"{args.hub.rstrip('/')}/admin/apps",
        data=part + blob + f"\r\n--{boundary}--\r\n".encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
    try:
        with request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read().decode())
    except error.HTTPError as e:
        die(f"servidor recusou ({e.code}): {e.read().decode(errors='replace')}")
    except error.URLError as e:
        die(f"falha de rede: {e.reason}")

    print(f"ok: {out.get('package')} v{out.get('version')} publicado "
          f"({out.get('store', {}).get('apps')} apps no catalogo)")


if __name__ == "__main__":
    main()
