#!/usr/bin/env python3
"""Importa o catalogo da App Store upstream (KryonOS) para o CelerOS Hub.

Busca o index.json do repo Haris16-code/KryonOS-AppStore, baixa app.json +
main.js de cada app e grava em content/store/apps/<packageName>/ com
category=Comunidade. Apps que ja existem localmente (ex.: celeros.*) NAO
sao sobrescritos. Depois de importar, o catalogo e regenerado ao subir o
servidor e publicar/rodear qualquer /admin (ou rode o rebuild manualmente
publicando qualquer app).

Uso: python3 tools/import_kryonos_catalog.py [--index URL] [--dry-run]
"""

import argparse
import json
import sys
import time
import unicodedata
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse

UPSTREAM_INDEX = ("https://raw.githubusercontent.com/Haris16-code/"
                  "KryonOS-AppStore/refs/heads/main/index.json")
ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = ROOT / "content" / "store" / "apps"


def unaccent(s: str) -> str:
    """A fonte do dispositivo nao tem glifos acentuados -> ASCII."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def fetch(url: str, as_bytes: bool = False):
    with request.urlopen(request.Request(url, headers={"user-agent": "celeros-hub-import"}), timeout=30) as r:
        data = r.read()
    return data if as_bytes else data.decode("utf-8")


def fetch_json(url: str):
    try:
        return json.loads(fetch(url))
    except (error.URLError, error.HTTPError, ValueError) as e:
        print(f"  aviso: falha em {url}: {e}", file=sys.stderr)
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default=UPSTREAM_INDEX)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    idx = fetch_json(args.index)
    if not idx or not idx.get("categories"):
        sys.exit("erro: index upstream indisponivel")

    imported, skipped, failed = 0, 0, 0
    for cname, curl in idx["categories"].items():
        cat = fetch_json(curl)
        if not cat or not cat.get("apps"):
            failed += 1
            continue
        print(f"categoria: {cname}")
        for aid, entry in cat["apps"].items():
            if not isinstance(entry, dict) or not entry.get("meta") or not entry.get("app"):
                continue
            meta = fetch_json(entry["meta"])
            if not meta or not meta.get("packageName"):
                failed += 1
                continue
            pkg = meta["packageName"]
            dest = APPS_DIR / pkg
            if dest.exists():
                skipped += 1
                continue
            try:
                code = fetch(entry["app"], as_bytes=True)
            except (error.URLError, error.HTTPError) as e:
                print(f"  aviso: main.js de {pkg}: {e}", file=sys.stderr)
                failed += 1
                continue
            meta.setdefault("category", "Comunidade")
            if not meta.get("api"):
                meta["api"] = int(entry.get("api") or 1)
            meta.pop("metaUrl", None)  # rufo do catalogo upstream
            for f in ("name", "description", "author", "category"):
                if isinstance(meta.get(f), str):
                    meta[f] = unaccent(meta[f])
            print(f"  + {pkg} v{meta.get('version')} ({meta.get('name')})")
            if not args.dry_run:
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "app.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                (dest / "main.js").write_bytes(code)
            imported += 1
            time.sleep(0.2)  # educado com o raw.githubusercontent

    print(f"\nfim: {imported} importados, {skipped} ja existentes, "
          f"{failed} falhas")
    if args.dry_run:
        print("(dry-run: nada foi gravado)")


if __name__ == "__main__":
    main()
