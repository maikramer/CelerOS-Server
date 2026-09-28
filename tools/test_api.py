#!/usr/bin/env python3
"""Testes da API do CelerOS Hub (sem rede: FastAPI TestClient).

Cobre o contrato do fluxo de ATUALIZACAO de apps:
  - publish feliz grava os campos gerenciados (size/md5/published_at/publisher)
  - catalogo (all.json) expoe changelog/size/md5/published_at/icon
  - anti-downgrade (versao <= atual -> 409; force=1 passa)
  - dono por pacote (outro token -> 403; raiz sempre pode)
  - limites (main.js > 30 KB e icon.png > 16 KB -> 413; PNG invalido -> 400)
  - contador de downloads em /api/info

Uso (precisa de fastapi + httpx no interpretador):
    python3 tools/test_api.py
"""

import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API_PATH = REPO / "stacks" / ".celeros-hub" / "api" / "app.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32  # magic + lixo (suficiente p/ o hub)

fails: list[str] = []


def expect(cond, msg):
    tag = "ok " if cond else "FALHOU"
    print(f"  [{tag}] {msg}")
    if not cond:
        fails.append(msg)


def make_zip(meta: dict, main_js: bytes, icon: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app.json", json.dumps(meta, ensure_ascii=False))
        zf.writestr("main.js", main_js)
        if icon is not None:
            zf.writestr("icon.png", icon)
    return buf.getvalue()


def meta_of(pkg, version="1.0.0", **extra):
    m = {"name": pkg.split(".")[-1].title(), "packageName": pkg,
         "version": version, "api": 3, "author": "CelerOS",
         "description": "app de teste", "category": "Teste",
         "changelog": f"{version} - teste"}
    m.update(extra)
    return m


def publish(client, token, pkg_meta, main_js=b"console.log('oi');\n",
            icon=None, force=""):
    return client.post(
        "/admin/apps",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("app.zip", make_zip(pkg_meta, main_js, icon),
                        "application/zip")},
        data={"force": force})


def main() -> None:
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        content = tmp / "content"
        for d in ("www/assets", "store/apps", "updates", "help"):
            (content / d).mkdir(parents=True)
        (tmp / "audit").mkdir()
        (tmp / "stats").mkdir()
        tokens = tmp / "tokens.json"
        tokens.write_text(json.dumps([
            {"name": "dev1", "token": "tok-dev1", "scopes": ["apps"]},
            {"name": "dev2", "token": "tok-dev2", "scopes": ["apps"]},
        ]), encoding="utf-8")

        os.environ["CONTENT_DIR"] = str(content)
        os.environ["AUDIT_DIR"] = str(tmp / "audit")
        os.environ["STATS_DIR"] = str(tmp / "stats")
        os.environ["HUB_ADMIN_TOKEN"] = "tok-root"
        os.environ["HUB_TOKENS_FILE"] = str(tokens)

        import importlib.util
        spec = importlib.util.spec_from_file_location("hubapi", API_PATH)
        hubapi = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hubapi)

        with TestClient(hubapi.app) as c:
            print("health / infos")
            r = c.get("/health")
            expect(r.status_code == 200 and r.json()["version"] == "0.3.0",
                   "health 200 na versao nova")

            print("publish feliz (dev1)")
            r = publish(c, "tok-dev1", meta_of("celeros.testapp"),
                        icon=PNG)
            expect(r.status_code == 200, f"publish 200 (veio {r.status_code}: {r.text[:120]})")
            expect(bool(r.json().get("md5")) and r.json().get("size", 0) > 0,
                   "resposta traz md5/size")
            disk = json.loads(
                (content / "store/apps/celeros.testapp/app.json").read_text())
            import hashlib
            expect(disk.get("publisher") == "dev1", "app.json ganha publisher")
            expect(disk.get("md5") == hashlib.md5(
                b"console.log('oi');\n").hexdigest(), "app.json ganha md5 certo")
            expect(bool(disk.get("published_at")), "app.json ganha published_at")

            print("catalogo enriquecido")
            entry = c.get("/store/all.json").json()["apps"]["celeros.testapp"]
            expect(entry.get("changelog") == "1.0.0 - teste",
                   "entry tem changelog")
            expect(entry.get("size") == disk["size"] and
                   entry.get("md5") == disk["md5"], "entry tem size/md5")
            expect("published_at" in entry, "entry tem published_at")
            expect(entry.get("icon", "").endswith("icon.png"),
                   "entry tem icon quando existe")

            print("contador de downloads")
            r = c.get("/store/apps/celeros.testapp/main.js")
            expect(r.status_code == 200 and
                   r.headers["content-type"].startswith("text/javascript"),
                   "download do main.js 200")
            c.get("/store/apps/celeros.testapp/main.js")
            expect(c.get("/api/info").json()["downloads"]
                   .get("celeros.testapp") == 2, "/api/info conta 2 downloads")
            expect(c.get("/store/apps/celeros.naoexiste/main.js").status_code
                   == 404, "download de pacote inexistente 404")

            print("anti-downgrade / republicacao")
            expect(publish(c, "tok-dev1",
                           meta_of("celeros.testapp")).status_code == 409,
                   "mesma versao sem force -> 409")
            expect(publish(c, "tok-dev1", meta_of("celeros.testapp"),
                           force="1").status_code == 200,
                   "mesma versao com force=1 -> 200")
            expect(publish(c, "tok-dev1",
                           meta_of("celeros.testapp", "1.0.1")).status_code
                   == 200, "versao maior passa direto")
            expect(publish(c, "tok-dev1",
                           meta_of("celeros.testapp", "1.0.0")).status_code
                   == 409, "downgrade -> 409")

            print("dono por pacote")
            r = publish(c, "tok-dev2", meta_of("celeros.testapp", "1.0.2"))
            expect(r.status_code == 403 and "dev1" in r.json()["detail"],
                   "outro dev nao publica por cima (403)")
            expect(publish(c, "tok-dev2", meta_of("celeros.testapp", "1.0.2"),
                           force="1").status_code == 403,
                   "force nao quebra a regra de dono")
            expect(publish(c, "tok-root", meta_of("celeros.testapp", "1.0.2"))
                   .status_code == 200, "raiz publica por cima")
            expect(publish(c, "tok-dev1", meta_of("celeros.testapp", "1.0.3"))
                   .status_code == 403, "dono antigo perde o pacote p/ root")
            expect(c.delete("/admin/apps/celeros.testapp",
                            headers={"Authorization": "Bearer tok-dev2"})
                   .status_code == 403, "nao-dono nao remove (403)")
            expect(c.delete("/admin/apps/celeros.testapp",
                            headers={"Authorization": "Bearer tok-root"})
                   .status_code == 200, "raiz remove")

            print("limites de tamanho")
            big31 = b"//" + b"x" * (31 * 1024)
            big49 = b"//" + b"x" * (49 * 1024)
            expect(publish(c, "tok-dev1", meta_of("celeros.big31"),
                           main_js=big31).status_code == 400,
                   "main.js > 30 KB com api < 6 -> 400")
            expect(publish(c, "tok-dev1", meta_of("celeros.big31api6", "1.0.0",
                                                  api=6),
                           main_js=big31).status_code == 200,
                   "main.js > 30 KB com api 6 -> 200")
            expect(publish(c, "tok-dev1", meta_of("celeros.big49", "1.0.0",
                                                  api=6),
                           main_js=big49).status_code == 413,
                   "main.js > 48 KB -> 413")
            expect(publish(c, "tok-dev1", meta_of("celeros.iconbig"),
                           icon=PNG + b"0" * 17 * 1024).status_code == 413,
                   "icon.png > 16 KB -> 413")
            expect(publish(c, "tok-dev1", meta_of("celeros.iconbad"),
                           icon=b"nao-e-png").status_code == 400,
                   "icon.png nao-PNG -> 400")

            print("sem token / escopo")
            expect(c.post("/admin/apps").status_code in (401, 403),
                   "publish sem token e recusado")

    print()
    if fails:
        print(f"FALHAS: {len(fails)}")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("OK: todos os testes da API passaram")


if __name__ == "__main__":
    main()
