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
DEPS (repositorio de dependencias JS compartilhadas):
  - publish de dep (zip <nome>.js + dep.json) grava campos gerenciados em
    store/deps/<nome>/<versao>/; indice /store/deps.json com size/md5/
    minApi/url; arquivo servido pelo mount estatico
  - app com deps: validacao de nome/range, existencia no repo, soma da dep
    no teto de .js (>48KB exige psram), minApi <= api do app, entry do
    catalogo leva deps
  - anti-downgrade por dep (409; force=1), dono por nome, escopo "deps"
    separado de "apps", delete protegido por consumidor (409 quando nao
    sobra versao que satisfaca o range do app)
OTA:
  - publish grava api_version (default = nivel vigente do firmware) e
    firmware_sha256; variant validado e exposto; /api/info traz boards/
    firmware.api_level/updates com api_version+variant; anti-rollback 409

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


def publish_ota(client, token, channel, meta, firmware: bytes | None = None,
                force=""):
    files = {}
    if firmware is not None:
        files["firmware"] = ("firmware.bin", firmware,
                             "application/octet-stream")
    return client.post(
        f"/admin/updates/{channel}",
        headers={"Authorization": f"Bearer {token}"},
        files=files,
        data={"update.json": json.dumps(meta, ensure_ascii=False),
              "force": force})


def dep_zip(name: str, version: str, js: bytes, min_api: int = 1,
            deps: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("dep.json", json.dumps({
            "name": name, "version": version, "minApi": min_api,
            "deps": deps or {}}))
        zf.writestr(f"{name}.js", js)
    return buf.getvalue()


def publish_dep(client, token, name, version, js=b"exports.ok = 1;\n",
                min_api=1, deps=None, force=""):
    return client.post(
        "/admin/deps",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("dep.zip", dep_zip(name, version, js, min_api, deps),
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
            {"name": "ci-fw", "token": "tok-ci", "scopes": ["updates"]},
            {"name": "libdev", "token": "tok-lib", "scopes": ["deps"]},
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
            entry2 = c.get("/store/all.json").json()["apps"]["celeros.testapp"]
            expect(entry2.get("downloads") == 2, "entry do catalogo traz downloads")
            expect((Path(os.environ["STATS_DIR"]) / "downloads.json").is_file(),
                   "contador persistido em STATS_DIR")

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
            big1025 = b"//" + b"x" * (1025 * 1024)
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
                           main_js=big129).status_code == 200,
                   "main.js de 129 KB com psram -> 200 (teto subiu para 1 MB)")
            expect(publish(c, "tok-dev1", meta_of("celeros.big1025", "1.0.0",
                                                  api=6,
                                                  requires=["psram"]),
                           main_js=big1025).status_code == 413,
                   "main.js > 1 MB absoluto -> 413")

            print("nivel de API e nome unico (0.9.0)")
            expect(publish(c, "tok-dev1", meta_of("celeros.futuro", "1.0.0",
                                                  api=hubapi.FIRMWARE_API_LEVEL + 1))
                   .status_code == 400, "api acima do firmware -> 400")
            expect(publish(c, "tok-dev1", meta_of("celeros.agora", "1.0.0",
                                                  api=hubapi.FIRMWARE_API_LEVEL))
                   .status_code == 200, "api = nivel do firmware -> 200")
            expect(publish(c, "tok-dev1", meta_of("celeros.zero", "1.0.0", api=0))
                   .status_code in (200, 400), "api 0 vira 1 (default) ou e recusada")
            m_dup = meta_of("celeros.outroagora", "1.0.0", name="Agora")
            r = publish(c, "tok-dev1", m_dup)
            expect(r.status_code == 409 and "celeros.agora" in r.text,
                   "nome repetido em outro pacote -> 409 dizendo o dono")
            m_dup2 = meta_of("celeros.outroagora", "1.0.0", name="  agora ")
            expect(publish(c, "tok-dev1", m_dup2).status_code == 409,
                   "nome repetido ignora caixa/espacos")
            expect(publish(c, "tok-dev1", meta_of("celeros.agora", "1.0.1",
                                                  api=hubapi.FIRMWARE_API_LEVEL))
                   .status_code == 200, "update do MESMO pacote nao colide consigo")
            expect(publish(c, "tok-dev1", m_dup, force="1").status_code == 200,
                   "force=1 aceita nome repetido")
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

            print("deps: repositorio de dependencias")
            engine_js = b"// engine\nexports.v = '1.0.0';\n"
            r = publish_dep(c, "tok-lib", "celeros.engine", "1.0.0",
                            js=engine_js, min_api=28)
            expect(r.status_code == 200,
                   f"publish de dep 200 (veio {r.status_code}: {r.text[:120]})")
            disk = json.loads((content / "store/deps/celeros.engine/1.0.0"
                               / "dep.json").read_text())
            expect(disk.get("publisher") == "libdev" and
                   disk.get("size") == len(engine_js) and
                   disk.get("md5") == hashlib.md5(engine_js).hexdigest(),
                   "dep.json ganha publisher/size/md5 gerenciados")
            idx = c.get("/store/deps.json").json()
            expect("deps" in idx and
                   "celeros.engine" in idx["deps"] and
                   "1.0.0" in idx["deps"]["celeros.engine"],
                   "indice /store/deps.json expoe a dep (nao vira categoria)")
            entry = idx["deps"]["celeros.engine"]["1.0.0"]
            expect(entry.get("minApi") == 28 and
                   entry.get("md5") == disk["md5"] and
                   entry.get("url", "").endswith(
                       "/store/deps/celeros.engine/1.0.0/celeros.engine.js"),
                   "entrada do indice traz minApi/md5/url")
            r = c.get("/store/deps/celeros.engine/1.0.0/celeros.engine.js")
            expect(r.status_code == 200 and r.content == engine_js,
                   "arquivo da dep servido pelo mount estatico")
            expect(c.get("/api/info").json().get("dep_downloads", {})
                   .get("celeros.engine") == 1, "download de dep contado em dep_downloads")
            expect("dep:celeros.engine" not in c.get("/api/info").json()["downloads"],
                   "dep nao polui o contador de apps")
            expect(c.get("/store/deps/celeros.engine/1.0.0/outro.js").status_code == 404 and
                   c.get("/store/deps/celeros.engine/9.9.9/celeros.engine.js").status_code == 404,
                   "dep com arquivo/versao errados -> 404")

            print("deps: publish de app que declara deps")
            r = publish(c, "tok-dev1", meta_of("celeros.jogo", "1.0.0", api=30,
                                               deps={"celeros.engine": "^1.0.0"}))
            expect(r.status_code == 200,
                   f"app com dep valida publica (veio {r.status_code}: {r.text[:120]})")
            entry = c.get("/store/all.json").json()["apps"]["celeros.jogo"]
            expect(entry.get("deps") == {"celeros.engine": "^1.0.0"},
                   "entry do catalogo leva deps")
            expect(publish(c, "tok-dev1", meta_of("celeros.quebrado", api=30,
                                                  deps={"celeros.fantasma": "^1.0.0"})
                           ).status_code == 400,
                   "dep inexistente no repo -> 400")
            expect(publish(c, "tok-dev1", meta_of("celeros.rng", api=30,
                                                  deps={"celeros.engine": "1.x"})
                           ).status_code == 400,
                   "range malformado -> 400")
            expect(publish(c, "tok-dev1", meta_of("celeros.apivelho", api=6,
                                                  deps={"celeros.engine": "^1.0.0"})
                           ).status_code == 400,
                   "app com deps e api < 30 -> 400 (require resolve dep na 30)")
            expect(publish_dep(c, "tok-lib", "celeros.exige99", "1.0.0",
                               min_api=99).status_code == 200,
                   "dep com minApi alto publica")
            expect(publish(c, "tok-dev1", meta_of("celeros.apibaixa", api=30,
                                                  deps={"celeros.exige99": "^1.0.0"})
                           ).status_code == 400,
                   "api do app < minApi da dep -> 400")

            print("deps: soma da dep no teto de .js")
            fat = b"//" + b"e" * (40 * 1024)
            expect(publish_dep(c, "tok-lib", "celeros.fat", "1.0.0", js=fat)
                   .status_code == 200, "dep de 40KB publica")
            main10 = b"//" + b"m" * (10 * 1024)
            expect(publish(c, "tok-dev1", meta_of("celeros.apertado", api=30,
                                                  deps={"celeros.fat": "^1.0.0"}),
                           main_js=main10).status_code == 400,
                   "pacote 10KB + dep 40KB sem psram -> 400 (soma > 48KB)")
            expect(publish(c, "tok-dev1", meta_of("celeros.apertadops", api=30,
                                                  requires=["psram"],
                                                  deps={"celeros.fat": "^1.0.0"}),
                           main_js=main10).status_code == 200,
                   "mesma soma com requires psram -> 200")

            print("deps: anti-downgrade / dono / escopo")
            expect(publish_dep(c, "tok-lib", "celeros.engine", "1.0.0",
                               js=engine_js).status_code == 409,
                   "republicar mesma versao -> 409")
            expect(publish_dep(c, "tok-lib", "celeros.engine", "1.0.0",
                               js=engine_js + b"// fix\n", force="1")
                   .status_code == 200, "force=1 republica a versao")
            expect(publish_dep(c, "tok-lib", "celeros.engine", "1.1.0")
                   .status_code == 200, "versao maior publica")
            expect(publish_dep(c, "tok-lib", "celeros.engine", "1.0.5")
                   .status_code == 409, "versao menor que a atual -> 409")
            expect(publish_dep(c, "tok-dev1", "celeros.engine", "1.2.0")
                   .status_code == 403, "token sem escopo deps -> 403")
            expect(publish_dep(c, "tok-lib", "celeros.engine", "1.3.0")
                   .status_code == 200, "dono publica versao nova")

            print("deps: transitivas")
            expect(publish_dep(c, "tok-lib", "celeros.bundle", "1.0.0",
                               deps={"celeros.engine": "^1.0.0"}).status_code
                   == 200, "dep com transitiva valida publica")
            expect(publish_dep(c, "tok-lib", "celeros.quebrada", "1.0.0",
                               deps={"celeros.fantasma": "^1.0.0"}).status_code
                   == 400, "transitiva inexistente -> 400")
            expect(publish_dep(c, "tok-lib", "celeros.loop", "1.0.0",
                               deps={"celeros.loop": "^1.0.0"}).status_code
                   == 400, "auto-dependencia -> 400")

            print("deps: delete protegido por consumidor")
            expect(publish(c, "tok-dev1", meta_of("celeros.preso", "1.0.0",
                                                  api=30,
                                                  deps={"celeros.engine": "^1.3.0"})
                           ).status_code == 200,
                   "app preso no range ^1.3.0 publica")
            r = c.delete("/admin/deps/celeros.engine/1.3.0",
                         headers={"Authorization": "Bearer tok-lib"})
            expect(r.status_code == 409 and "celeros.preso" in r.json()["detail"],
                   "delete da unica versao que satisfaz o range do app -> 409")
            r = c.delete("/admin/deps/celeros.engine/1.0.0",
                         headers={"Authorization": "Bearer tok-lib"})
            expect(r.status_code == 200,
                   "delete de versao com alternativa (1.1.0/1.3.0 satisfazem ^1.0.0)")
            expect(c.delete("/admin/deps/celeros.engine/9.9.9",
                            headers={"Authorization": "Bearer tok-lib"})
                   .status_code == 404, "delete de versao inexistente -> 404")
            expect(c.delete("/admin/deps/celeros.bundle/1.0.0",
                            headers={"Authorization": "Bearer tok-root"})
                   .status_code == 200, "root remove dep")
            info = c.get("/api/info").json()
            expect(info.get("store", {}).get("dep_packages", 0) >= 2 and
                   info["store"].get("dep_versions", 0) >= 3,
                   "/api/info conta pacotes/versoes de deps")

            print("OTA: manifest, api_version e variant")
            ota = lambda ver, **kw: {  # noqa: E731
                "version": ver, "changelog": f"{ver} - teste", **kw}
            blob = b"firmware-fake" + b"\x00" * 1024
            r = publish_ota(c, "tok-ci", "esp32", ota("1.2.0"), firmware=blob)
            expect(r.status_code == 200,
                   f"publish OTA 200 (veio {r.status_code}: {r.text[:120]})")
            expect(r.json().get("api_version") == hubapi.FIRMWARE_API_LEVEL,
                   "api_version default = nivel vigente do firmware")
            expect(r.json().get("sha256") == hashlib.sha256(blob).hexdigest(),
                   "sha256 do firmware na resposta")
            disk = json.loads(
                (content / "updates/esp32/update.json").read_text())
            expect(disk.get("api_version") == hubapi.FIRMWARE_API_LEVEL and
                   disk.get("firmware_sha256") == r.json()["sha256"],
                   "update.json gravado com api_version/sha256")
            expect((content / "updates/esp32/firmware.bin").read_bytes()
                   == blob, "firmware.bin gravado integro")
            r = publish_ota(c, "tok-ci", "esp32", ota("1.1.0"))
            expect(r.status_code == 409, "rollback OTA (versao menor) -> 409")
            r = publish_ota(c, "tok-ci", "esp32", ota("1.1.0"), force="1")
            expect(r.status_code == 200 and not r.json()["firmware"],
                   "force=1 publica manifest-only abaixo da versao")
            expect(not (content / "updates/esp32/firmware.bin").exists(),
                   "manifest-only remove firmware.bin do canal")
            r = publish_ota(c, "tok-ci", "esp32",
                            ota("1.2.1", variant="smartdisplay-y8"))
            expect(r.status_code == 200 and
                   r.json().get("variant") == "smartdisplay-y8",
                   "variant de SKU aceito e ecoado")
            expect(publish_ota(c, "tok-ci", "esp32",
                               ota("1.2.2", variant="Y8!")).status_code == 400,
                   "variant invalido -> 400")
            expect(publish_ota(c, "tok-ci", "esp32",
                               ota("1.2.3", api_version=0)).status_code == 400,
                   "api_version 0 -> 400 (device recusa)")
            expect(publish_ota(c, "tok-ci", "esp32",
                               ota("1.2.4", api_version="x")).status_code
                   == 400, "api_version nao-inteiro -> 400")
            expect(publish_ota(c, "tok-ci", "esp32",
                               ota("1.2.5", api_version=30)).status_code == 200,
                   "api_version explicito (30) passa")
            info = c.get("/api/info").json()
            expect(info["updates"]["esp32"].get("api_version") == 30 and
                   info["updates"]["esp32"].get("variant") == "",
                   "/api/info traz api_version/variant do canal")
            expect(info.get("firmware", {}).get("api_level")
                   == hubapi.FIRMWARE_API_LEVEL,
                   "/api/info expoe firmware.api_level")
            expect(isinstance(info.get("boards"), dict) and
                   "smartdisplay_4848S040" in info["boards"],
                   "/api/info expoe o mapa de placas")
            expect(info.get("downloads_total", -1) ==
                   sum(info["downloads"].values()),
                   "/api/info traz downloads_total consistente")
            expect(publish_ota(c, "tok-dev1", "esp32",
                               ota("9.9.9")).status_code == 403,
                   "token sem escopo updates -> 403")
            expect(c.delete("/admin/updates/esp32",
                            headers={"Authorization": "Bearer tok-ci"})
                   .status_code == 200, "remocao de canal OTA")

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
