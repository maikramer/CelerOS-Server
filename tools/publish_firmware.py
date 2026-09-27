#!/usr/bin/env python3
"""Publica firmware OTA em um canal do CelerOS Hub.

Envia firmware.bin (opcional) + metadados para POST /admin/updates/<canal>.
O servidor grava content/updates/<canal>/{firmware.bin,update.json}; o
update.json segue o esquema v2 que o OtaManager do dispositivo le, com
firmware_url relativa ("firmware.bin").

Canais das placas: esp32 (CYD) e smartdisplay_4848S040 (SmartDisplay 4").
Token: --token ou env CELER_HUB_TOKEN.

Uso:
    python3 tools/publish_firmware.py esp32 build-cyd/KryonOS.bin \
        --version 1.3.0 --changelog "- novidade" [--minor] [--security] \
        [--hub https://os.celer.tec.br] [--token TOKEN]

Publicar so o manifest (sem binario): omita o caminho do firmware — o
dispositivo cai no fluxo legado (mostra changelog, sem botao INSTALL).
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from urllib import error, request

CHANNELS = ("esp32", "smartdisplay_4848S040")


def die(msg: str) -> None:
    print(f"erro: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="publica firmware OTA no hub")
    ap.add_argument("channel", help=f"canal: {' | '.join(CHANNELS)}")
    ap.add_argument("firmware", nargs="?", help="caminho do firmware.bin (opcional)")
    ap.add_argument("--version", required=True, help="versao publicada (x.y.z)")
    ap.add_argument("--changelog", default="", help="novidades (linhas com -)")
    ap.add_argument("--guide", default="Abra Settings > Atualizacao e toque em INSTALL.")
    ap.add_argument("--major", action="store_true")
    ap.add_argument("--minor", action="store_true")
    ap.add_argument("--security", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="permite publicar versao MENOR que a atual do canal "
                         "(o servidor bloqueia rollback por padrao)")
    ap.add_argument("--api-version", type=int, default=2)
    ap.add_argument("--hub", default=os.environ.get("CELER_HUB", "https://os.celer.tec.br"))
    ap.add_argument("--token", default=os.environ.get("CELER_HUB_TOKEN", ""))
    args = ap.parse_args()

    if args.channel not in CHANNELS:
        print(f"aviso: canal fora das placas conhecidas ({', '.join(CHANNELS)}); "
              "publicando mesmo assim (ex.: canal beta)", file=sys.stderr)
    import re
    if not re.match(r"^\d+\.\d+\.\d+$", args.version):
        die("--version deve ser semver x.y.z")
    token = args.token
    if not token:
        die("sem token: use --token ou export CELER_HUB_TOKEN=...")

    bin_blob = b""
    if args.firmware:
        fw = Path(args.firmware).expanduser().resolve()
        if not fw.is_file():
            die(f"firmware nao encontrado: {fw}")
        bin_blob = fw.read_bytes()
        if not bin_blob:
            die(f"firmware vazio: {fw}")

    meta = {
        "version": args.version,
        "api_version": args.api_version,
        "major_update": args.major,
        "minor_update": args.minor,
        "security_update": args.security,
        "changelog": args.changelog,
        "guide": args.guide,
    }
    meta_json = json.dumps(meta, ensure_ascii=False)

    boundary = "----celeroshub" + uuid.uuid4().hex[:12]
    parts = []
    if bin_blob:
        parts.append(
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"firmware\"; "
            "filename=\"firmware.bin\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n".encode()
            + bin_blob + b"\r\n")
    if args.force:
        parts.append(
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"force\"\r\n\r\n"
            "1\r\n".encode())
    parts.append(
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"update.json\"\r\n"
        "Content-Type: application/json\r\n\r\n"
        f"{meta_json}\r\n".encode())
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = request.Request(
        f"{args.hub.rstrip('/')}/admin/updates/{args.channel}",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
    try:
        with request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read().decode())
    except error.HTTPError as e:
        die(f"servidor recusou ({e.code}): {e.read().decode(errors='replace')}")
    except error.URLError as e:
        die(f"falha de rede: {e.reason}")

    fw = "com firmware.bin" if out.get("firmware") else "só manifest (sem INSTALL)"
    print(f"ok: canal {out.get('channel')} v{out.get('version')} publicado ({fw})")
    if out.get("sha256"):
        print(f"    sha256 {out['sha256']}")
    print(f"    {out.get('url')}")


if __name__ == "__main__":
    main()
