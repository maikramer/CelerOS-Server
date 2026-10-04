#!/usr/bin/env python3
"""Testes da API do CelerOS Hub (sem rede: FastAPI TestClient).

Cobre o contrato do fluxo de ATUALIZACAO de apps:
  - publish feliz grava os campos gerenciados (size/md5/published_at/publisher)
  - catalogo (all.json) expoe changelog/size/md5/published_at/icon/requires/files
  - anti-downgrade (versao <= atual -> 409; force=1 passa)
  - dono por pacote (outro token -> 403; raiz sempre pode)
  - limites (teto em 2 niveis pela SOMA dos .js: >48KB exige requires psram,
    >128KB absoluto; >30KB por arquivo exige api 6; icon.png > 16 KB -> 413)
  - pacote multi-arquivo: extras gravados/servidos, campo gerenciado "files",
    colisao de nome, extensao invalida, tetos de asset/contagem
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


def make_zip(meta: dict, main_js: bytes, icon: bytes | None = None,
             extras: dict[str, bytes] | None = None,
             raw: list[tuple[str, bytes]] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app.json", json.dumps(meta, ensure_ascii=False))
        zf.writestr("main.js", main_js)
        if icon is not None:
            zf.writestr("icon.png", icon)
        for n, b in (extras or {}).items():
            zf.writestr(n, b)
        # entradas cruas p/ casos de erro (duplicatas, subpastas, ../)
        for n, b in (raw or []):
            zf.writestr(n, b)
    return buf.getvalue()


def meta_of(pkg, version="1.0.0", **extra):
    m = {"name": pkg.split(".")[-1].title(), "packageName": pkg,
         "version": version, "api": 3, "author": "CelerOS",
         "description": "app de teste", "category": "Teste",
         "changelog": f"{version} - teste"}
    m.update(extra)
    return m


def publish(client, token, pkg_meta, main_js=b"console.log('oi');\n",
            icon=None, extras=None, raw=None, force=""):
    return client.post(
        "/admin/apps",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("app.zip",
                        make_zip(pkg_meta, main_js, icon, extras, raw),
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
            expect(r.status_code == 200 and
                   r.json()["version"] == hubapi.HUB_VERSION,
                   "health 200 na versao do modulo (sem drift)")

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

            print("limites de tamanho (teto em 2 niveis, soma dos .js)")
            big31 = b"//" + b"x" * (31 * 1024)
            big49 = b"//" + b"x" * (49 * 1024)
            big129 = b"//" + b"x" * (129 * 1024)
            expect(publish(c, "tok-dev1", meta_of("celeros.big31"),
                           main_js=big31).status_code == 400,
                   "main.js > 30 KB com api < 6 -> 400")
            expect(publish(c, "tok-dev1", meta_of("celeros.big31api6", "1.0.0",
                                                  api=6),
                           main_js=big31).status_code == 200,
                   "main.js > 30 KB com api 6 -> 200")
            expect(publish(c, "tok-dev1", meta_of("celeros.big49", "1.0.0",
                                                  api=6),
                           main_js=big49).status_code == 400,
                   "main.js > 48 KB sem psram -> 400 (pede requires)")
            expect(publish(c, "tok-dev1", meta_of("celeros.big49ps", "1.0.0",
                                                  api=6,
                                                  requires=["psram"]),
                           main_js=big49).status_code == 200,
                   "main.js > 48 KB com requires psram -> 200")
            expect(publish(c, "tok-dev1", meta_of("celeros.big129", "1.0.0",
                                                  api=6,
                                                  requires=["psram"]),
                           main_js=big129).status_code == 413,
                   "main.js > 128 KB absoluto -> 413")
            expect(publish(c, "tok-dev1", meta_of("celeros.iconbig"),
                           icon=PNG + b"0" * 17 * 1024).status_code == 413,
                   "icon.png > 16 KB -> 413")
            expect(publish(c, "tok-dev1", meta_of("celeros.iconbad"),
                           icon=b"nao-e-png").status_code == 400,
                   "icon.png nao-PNG -> 400")

            print("pacote multi-arquivo (modulos + assets)")
            mod = b"exports.dobra = function (n) { return n * 2; };\n"
            wav = b"RIFF" + b"0" * 2048
            r = publish(c, "tok-dev1", meta_of("celeros.multi", "1.0.0", api=6),
                        extras={"util.js": mod, "alerta.wav": wav,
                                "dados.json": b'{"x":1}'},
                        icon=PNG)
            expect(r.status_code == 200,
                   f"publish multi-arquivo 200 (veio {r.status_code}: {r.text[:120]})")
            disk = json.loads(
                (content / "store/apps/celeros.multi/app.json").read_text())
            import hashlib
            fjs = disk.get("files", {})
            expect(set(fjs) == {"util.js", "alerta.wav", "dados.json"},
                   "app.json ganha files com os 3 extras")
            expect(fjs.get("util.js", {}).get("md5") == hashlib.md5(mod).hexdigest()
                   and fjs.get("alerta.wav", {}).get("size") == len(wav),
                   "files traz md5/size certos")
            entry = c.get("/store/all.json").json()["apps"]["celeros.multi"]
            expect(entry.get("files") == fjs, "entry do catalogo leva files")
            r = c.get("/store/apps/celeros.multi/util.js")
            expect(r.status_code == 200 and r.content == mod,
                   "extra .js servido em /store/apps/<pkg>/<nome>")
            expect(c.get("/store/apps/celeros.multi/alerta.wav").status_code
                   == 200, "asset .wav servido")
            # update sem extras limpa o campo e o arquivo antigo (swap de staging)
            r = publish(c, "tok-dev1", meta_of("celeros.multi", "1.0.1", api=6))
            expect(r.status_code == 200 and "files" not in json.loads(
                (content / "store/apps/celeros.multi/app.json").read_text()),
                "update sem extras remove files do app.json")
            expect(not (content / "store/apps/celeros.multi/util.js").exists(),
                   "update sem extras remove o arquivo antigo do disco")

            print("multi-arquivo: erros")
            m6 = lambda pkg, ver="1.0.0": meta_of(pkg, ver, api=6)  # noqa: E731
            expect(publish(c, "tok-dev1", m6("celeros.dup"),
                           raw=[("a/util.js", b"1"), ("b/util.js", b"2")]
                           ).status_code == 400,
                   "colisao de basename -> 400")
            expect(publish(c, "tok-dev1", m6("celeros.ext"),
                           extras={"leia-me.txt": b"oi"}).status_code == 400,
                   "extensao fora da allowlist -> 400")
            expect(publish(c, "tok-dev1", m6("celeros.nome"),
                           raw=[("meu arquivo.png", b"0")]).status_code == 400,
                   "nome com espaco -> 400")
            expect(publish(c, "tok-dev1", m6("celeros.soma"),
                           main_js=b"//" + b"x" * (40 * 1024),
                           extras={"mod.js": b"//" + b"y" * (20 * 1024)}
                           ).status_code == 400,
                   "soma dos .js > 48KB sem psram -> 400")
            expect(publish(c, "tok-dev1",
                           meta_of("celeros.somaps", "1.0.0", api=6,
                                   requires=["psram"]),
                           main_js=b"//" + b"x" * (40 * 1024),
                           extras={"mod.js": b"//" + b"y" * (20 * 1024)}
                           ).status_code == 200,
                   "soma dos .js > 48KB com psram -> 200")
            expect(publish(c, "tok-dev1", meta_of("celeros.modvelho"),
                           extras={"mod.js": big31}).status_code == 400,
                   "modulo > 30KB com api < 6 -> 400")
            expect(publish(c, "tok-dev1", m6("celeros.assetgde"),
                           extras={"grande.wav": b"0" * (150 * 1024)}
                           ).status_code == 413,
                   "asset > 128KB por arquivo -> 413")
            expect(publish(c, "tok-dev1", m6("celeros.assettot"),
                           extras={"a.wav": b"0" * (200 * 1024),
                                   "b.wav": b"0" * (100 * 1024)}
                           ).status_code == 413,
                   "assets somando > 256KB -> 413")
            expect(publish(c, "tok-dev1", m6("celeros.muitos"),
                           extras={f"m{i:02d}.js": b"1" for i in range(17)}
                           ).status_code == 400,
                   "17 arquivos extras -> 400")

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
