# =============================================================================
# CelerOS Hub — servidor da loja de apps, canais OTA e central de ajuda.
#
# Um servico unico (FastAPI/uvicorn) que:
#   - serve o conteudo estatico de CONTENT_DIR:
#       /            -> portal (content/www)
#       /store/*     -> catalogo da App Store (compativel com o cliente JS)
#       /updates/*   -> canais OTA por placa (update.json + firmware.bin)
#       /help/*      -> central de ajuda online (index/categories/artigos)
#   - expoe /health e /api/info (denv/monitoramento)
#
# SEGURANCA (escrita restrita; leitura publica por design — a loja do
# dispositivo e o OTA nao autenticam):
#   /admin/* exige Bearer token (comparacao constant-time) e ESCOPO:
#     - root : HUB_ADMIN_TOKEN(_FILE)             -> escopo "*"
#     - nomeados: HUB_TOKENS_FILE (JSON)          -> [{name, token, scopes}]
#       escopos: "apps" (publicar/remover apps), "updates" (firmware/OTA),
#       "deps" (repositorio de dependencias), "*" (todos). Revogacao/
#       rotacao por token, sem afetar os demais.
#   - rate limit de falhas de auth por IP (X-Forwarded-For do Traefik; so o
#     reverse proxy alcanca o container — nao ha porta publicada no host).
#   - auditoria em APPEND-ONLY (AUDIT_DIR): ts, ip, agente, acao, alvo, erro.
#   - uploads: teto de tamanho (request e descomprimido), whitelist de
#     arquivos do zip, checagem de PNG, packageName/semver/canal validados.
#   - apps: publish calcula os campos gerenciados (size, md5, published_at,
#     publisher) e grava no app.json; rejeita main.js > 30 KB (o device
#     trunca em 32 KB), versao <= atual (anti-downgrade; force=1 excecao) e
#     republicacao/remocao por nao-dono, api acima de FIRMWARE_API_LEVEL e
#     NOME de exibicao repetido entre pacotes (force=1 excecao). Downloads
#     de main.js e das deps sao contados em STATS_DIR/downloads.json (no
#     volume NFS em producao) e expostos em /api/info.
#   - deps: repositorio de dependencias JS compartilhadas entre apps (a game
#     engine/fisica, ~53KB que antes eram vendorizados por jogo). App declara
#     "deps" {nome: "^x.y.z"} no app.json; o publish valida nome/range/existe
#     versao que satisfaca, soma o .js das deps resolvidas no teto de compile
#     (o device compila a dep no heap de cada app) e exige api do app >=
#     minApi da dep. Publicacao em /admin/deps (zip <nome>.js + dep.json),
#     indice em /store/deps.json, arquivo servido pelo mount estatico
#     /store/deps/<nome>/<versao>/; a loja do device grava o cache em
#     /local/modules/<nome>/<versao>/ e o require resolve de la (API 29).
#   - OTA: rejeita versao MENOR que a atual do canal (anti-rollback; force=1
#     para excecao) e grava firmware_sha256 no manifest. api_version e
#     validado (1..999; default = FIRMWARE_API_LEVEL — o device recusa
#     manifest abaixo do nivel dele) e variant de SKU tambem (ex.:
#     "smartdisplay-y8"; device Y recusa manifest sem a sua variante).
#   - /api/docs desligado por padrao (HUB_DOCS=1 para ligar em dev).
#
# O catalogo e gerado NA ROTA a partir do scan de content/store/apps/ (o disco
# e a fonte da verdade e o BASE_URL vem do ambiente). Pacotes de app sao
# estaticos em /store/apps/<pkg>/.
# =============================================================================

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import time
import zipfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HUB_VERSION = "0.9.0"
# Nivel de API do firmware CelerOS atual (fonte: CELEROS_API_LEVEL em
# main/CMakeLists.txt do CelerOS). O OtaManager do dispositivo RECUSA
# manifest sem api_version ou com nivel abaixo do dele — apps instalados
# que exigem API maior parariam de rodar — entao o default do publish e o
# nivel vigente (o antigo default 2 publicava manifest que todo device
# atual recusava). 27 = matilha (CelerNet.* 26, Pack.* 27, playMusic 25);
# 28 = canvas nativo (setNativeCanvas/pushSprite); 29 = pool de sprites;
# 30 = dependencias compartilhadas (require fora da pasta do app);
# 31 = verlet nativo (System.verlet*, JsPhysics.cpp);
# 32 = System.sfx (efeito misturado na trilha, nao bloqueia), caixas sujas
# multiplas no present e textWidth em pixels fisicos no canvas nativo.
# Tambem e o TETO do publish de apps: api acima disto nenhum firmware roda.
FIRMWARE_API_LEVEL = 32
# Placas do firmware (main/Boards/<placa>/Board.cpp -> otaChannel). Serve o
# portal (/api/info) com nomes amigaveis; o hub NAO restringe canais —
# canais beta/extra seguem publicaveis.
BOARDS = {
    "esp32": {
        "name": "CYD 2.8\"",
        "desc": "ESP32 classico 320x240 sem PSRAM (ESP32-2432S028R e afins;"
                " inclui a variante VSPI)",
    },
    "smartdisplay_4848S040": {
        "name": "SmartDisplay 4\"",
        "desc": "ESP32-S3 480x480 com PSRAM; a variante Y (reles) exige"
                " manifest com variant proprio",
    },
    "waveshare_amoled206": {
        "name": "Watch AMOLED 2.06\"",
        "desc": "Waveshare ESP32-S3R8 de pulso: watchfaces, Phone Link e"
                " deep sleep com sentinela ULP",
    },
    "spotpear_zzpet": {
        "name": "Cao robotico (ZZPET)",
        "desc": "SpotBear/ZZPET ESP32-S3R8: wake word \"hi celer\" e comandos"
                " de voz em portugues/ingles",
    },
    "devkit": {
        "name": "Devkit barebone",
        "desc": "ESP32 4MB sem tela (LED + botao BOOT) para apps de"
                " sensor/atuador",
    },
}
BASE_URL = os.environ.get("BASE_URL", "https://os.celer.tec.br").rstrip("/")
CONTENT_DIR = Path(os.environ.get("CONTENT_DIR", "/data/content"))
AUDIT_DIR = Path(os.environ.get("AUDIT_DIR", "/data/audit"))
STATS_DIR = Path(os.environ.get("STATS_DIR", "/data/stats"))
MAX_UPLOAD = 48 * 1024 * 1024   # teto por upload (firmware ~2 MB; folga p/ zip)
MAX_UNPACKED = 64 * 1024 * 1024  # teto total descomprimido (anti zip-bomb)
MAX_BODY = MAX_UPLOAD + 1024 * 1024
MAX_MAIN_JS = 48 * 1024   # download e streaming (Net.download), sem teto de 32KB
# Teto em 2 niveis (mesma regra do celerhub.py): acima de MAX_MAIN_JS o app
# precisa declarar "psram" em requires — sem PSRAM a RAM interna nao fecha o
# compile (medido na CYD: 61KB roda, 82KB nao compila). Com a declaracao o
# teto absoluto e MAX_MAIN_JS_PSRAM: as placas S3 com PSRAM compilam o fonte
# num bloco so de PSRAM (~6-7MB contiguos livres no launch), entao 1MB (1/8
# da PSRAM) cabe com folga — o preco e a abertura (compile linear) e espaco
# na littlefs. Era 128KB e recusava o Detona 0.6.0 (duelo pela malha) com o
# cliente ja em 1MB. O celerhub.py sobe os .js ENXUTOS (sem comentarios,
# como o device compila): a soma medida aqui e o custo real.
MAX_MAIN_JS_PSRAM = 1024 * 1024
VALID_REQUIRES = ("psram",)
MAX_ICON = 16 * 1024      # PNG 64x64 nao passa de poucos KB; teto folgado
# Acima disso o app PRECISA declarar api >= 6: firmwares antigos instalavam
# via Net.get, que trunca o corpo em 32KB (main.js corrompido na instalacao)
STREAM_SAFE_MAIN_JS = 30 * 1024
# Pacote multi-arquivo (modulos .js + assets): FLAT, sem subpastas. Modulos
# entram na soma do teto de compile; assets (nao-.js) tem tetos proprios —
# nao passam pelo Duktape, custam so espaco em disco. O publish computa o
# campo gerenciado "files" {nome: {size, md5}} (viaja no app.json e no
# catalogo) para a loja instalar e verificar tudo. Mesmo contrato do
# celerhub.py.
ASSET_EXTS = (".js", ".png", ".wav", ".json", ".bin")
FILE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_ASSET_FILE = 128 * 1024    # por arquivo extra
MAX_ASSETS_TOTAL = 256 * 1024  # soma dos extras nao-.js
MAX_EXTRA_FILES = 16           # arquivos alem de app.json/main.js/icon.png
AUTH_FAILS_LIMIT = 10           # falhas de auth...
AUTH_FAILS_WINDOW = 600         # ...dentro desta janela (s)...
AUTH_BLOCK_SECS = 900           # ...bloqueiam o IP por este tempo
STARTED_AT = time.time()

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
PKG_NAME = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)+$")  # ex.: celeros.demo
# Dependencias compartilhadas: mesmo formato de packageName, segmentos com
# hifen (a identidade publica do modulo: nome do require, pasta no cache do
# device e chave no repositorio).
DEP_NAME = re.compile(r"^[a-z0-9]+(\.[a-z0-9-]+)+$")  # ex.: celeros.engine
DEP_RANGE = re.compile(r"^\^?\d+\.\d+\.\d+$")  # "^1.2.0" (major) ou exata
MAX_DEP_JS = 128 * 1024     # teto por modulo de dep (engine ~36KB)
MAX_APP_DEPS = 8            # deps por app (espelha o mapa do firmware)
CHANNEL = re.compile(r"^[a-z0-9_.-]+$")
VARIANT = re.compile(r"^[a-z0-9][a-z0-9.-]{0,31}$")  # ex.: smartdisplay-y8
SLUG = re.compile(r"^[a-z0-9_-]+$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _semver_tuple(v: str) -> tuple:
    return tuple(int(p) for p in v.split("."))


def _range_satisfies(rng: str, version: str) -> bool:
    """'^x.y.z' = mesma major, >= base; sem '^' = versao exata."""
    if not DEP_RANGE.match(rng) or not SEMVER.match(version):
        return False
    if not rng.startswith("^"):
        return rng == version
    b, v = _semver_tuple(rng[1:]), _semver_tuple(version)
    return v[0] == b[0] and v >= b

DOCS_ON = os.environ.get("HUB_DOCS", "") == "1"
app = FastAPI(title="CelerOS Hub", version=HUB_VERSION,
              docs_url="/api/docs" if DOCS_ON else None,
              redoc_url=None, openapi_url="/api/openapi.json" if DOCS_ON else None)


@app.middleware("http")
async def harden_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("x-content-type-options", "nosniff")
    resp.headers.setdefault("x-frame-options", "DENY")
    resp.headers.setdefault("referrer-policy", "no-referrer")
    return resp


# --------------------------------------------------------------------------- #
# autenticacao: root (env/secret) + tokens nomeados com escopo
# --------------------------------------------------------------------------- #

VALID_SCOPES = {"apps", "updates", "deps", "*"}


def _root_token() -> str:
    tok = os.environ.get("HUB_ADMIN_TOKEN", "")
    if not tok:
        path = os.environ.get("HUB_ADMIN_TOKEN_FILE", "")
        if path and Path(path).exists():
            tok = Path(path).read_text(encoding="utf-8").strip()
    return tok


def _named_tokens() -> list[dict]:
    """Tokens nomeados de HUB_TOKENS_FILE (JSON). Lido por request: rotacao
    de secret invalida o token no proximo acesso, sem restart."""
    path = os.environ.get("HUB_TOKENS_FILE", "")
    if not path or not Path(path).exists():
        return []
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for t in doc if isinstance(doc, list) else []:
        if not isinstance(t, dict):
            continue
        scopes = t.get("scopes") or []
        if isinstance(scopes, str):
            scopes = [scopes]
        scopes = [s for s in scopes if s in VALID_SCOPES]
        if t.get("name") and t.get("token") and scopes:
            out.append({"name": str(t["name"]), "token": str(t["token"]),
                        "scopes": scopes})
    return out


class Agent:
    __slots__ = ("name", "scopes")

    def __init__(self, name: str, scopes: list[str]):
        self.name = name
        self.scopes = scopes

    def has(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


ANON = Agent("anonimo", [])


def client_ip(request: Request) -> str:
    # Atras do Traefik: o primeiro hop do XFF e o cliente real. Confiamos
    # porque o container so e alcancavel pela rede overlay (sem porta no host).
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


# ---- rate limit de falhas de auth (memoria; escala de dezenas de admins) ----
_failures: dict[str, list] = {}  # ip -> [count, window_start, blocked_until]


def _auth_gate(request: Request) -> None:
    ip = client_ip(request)
    now = time.time()
    entry = _failures.get(ip)
    if entry and entry[2] > now:
        raise HTTPException(429, "muitas tentativas; tente mais tarde",
                            headers={"retry-after": str(int(entry[2] - now))})


def _auth_fail(request: Request) -> None:
    ip = client_ip(request)
    now = time.time()
    entry = _failures.get(ip)
    if not entry or now - entry[1] > AUTH_FAILS_WINDOW:
        entry = [0, now, 0.0]
        _failures[ip] = entry
    entry[0] += 1
    if entry[0] >= AUTH_FAILS_LIMIT:
        entry[2] = now + AUTH_BLOCK_SECS
        entry[0] = 0


def authenticate(request: Request) -> Agent:
    """Valida o Bearer contra root + tokens nomeados. 403 sem credencial
    valida (503 se o hub nao tem nenhum token configurado)."""
    _auth_gate(request)
    root = _root_token()
    named = _named_tokens()
    if not root and not named:
        raise HTTPException(503, "admin desabilitado: configure os secrets")
    got = request.headers.get("authorization", "")
    if got.startswith("Bearer "):
        cand = got[7:]
        if root and secrets.compare_digest(cand, root):
            return Agent("root", ["*"])
        for t in named:
            if secrets.compare_digest(cand, t["token"]):
                return Agent(t["name"], t["scopes"])
    _auth_fail(request)
    audit(request, ANON, "auth", "falha", ok=False, err="token invalido")
    raise HTTPException(403, "credencial invalida")


def require_scope(scope: str):
    def dep(request: Request) -> Agent:
        agent = authenticate(request)
        if not agent.has(scope):
            audit(request, agent, "auth", f"escopo:{scope}", ok=False,
                  err=f"{agent.name} sem escopo {scope}")
            raise HTTPException(403, f"token sem escopo '{scope}'")
        return agent
    return dep


# --------------------------------------------------------------------------- #
# auditoria (append-only em AUDIT_DIR; fora do rsync do content)
# --------------------------------------------------------------------------- #

def audit(request: Request, agent: Agent, action: str, target: str,
          ok: bool = True, err: str = "") -> None:
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ip": client_ip(request),
        "agent": agent.name,
        "action": action,
        "target": target,
        "ok": ok,
    }
    if err:
        rec["err"] = err[:200]
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_DIR / "audit.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # auditoria nunca derruba a request


@app.get("/admin/whoami")
def whoami(agent: Agent = Depends(authenticate)):
    return {"name": agent.name, "scopes": agent.scopes}


# --------------------------------------------------------------------------- #
# contador de downloads (STATS_DIR/downloads.json: {pkg: n}; sobrevive a
# remocao/republish do pacote — e historico de downloads, nao estoque).
# Tráfego e baixo: escrita atomica (tmp+rename) a cada incremento resolve.
# --------------------------------------------------------------------------- #

_stats: dict | None = None


def _load_stats() -> dict:
    global _stats
    if _stats is None:
        doc = read_json(STATS_DIR / "downloads.json")
        _stats = ({k: int(v) for k, v in doc.items()}
                  if isinstance(doc, dict) else {})
    return _stats


def _count_download(pkg: str) -> None:
    stats = _load_stats()
    stats[pkg] = stats.get(pkg, 0) + 1
    try:
        STATS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATS_DIR / "downloads.json.tmp"
        tmp.write_text(json.dumps(stats), encoding="utf-8")
        tmp.rename(STATS_DIR / "downloads.json")
    except OSError:
        pass  # estatistica nunca derruba o download


# --------------------------------------------------------------------------- #
# catalogo da loja (DINAMICO: gerado por rota a partir do disco — o BASE_URL
# vem do ambiente do servidor em execucao, nunca fica assado em arquivo).
# O cliente da loja no dispositivo le:
#   /store/index.json  {categories: {nome: url}}
#   /store/all.json | /store/<cat>.json  {apps: {id: {meta, app, api}}}
#   /store/apps/<pkg>/app.json {packageName, name, description, author, ...}
# --------------------------------------------------------------------------- #

def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def scan_apps() -> dict[str, dict]:
    """packageName -> app.json de cada pacote em store/apps/."""
    out: dict[str, dict] = {}
    root = CONTENT_DIR / "store" / "apps"
    if not root.is_dir():
        return out
    for pkg_dir in sorted(root.iterdir()):
        meta = read_json(pkg_dir / "app.json") if pkg_dir.is_dir() else None
        if not meta or not meta.get("packageName"):
            continue
        if not (pkg_dir / "main.js").exists():
            continue  # pacote incompleto nao entra no catalogo
        out[meta["packageName"]] = meta
    return out


def scan_deps() -> dict[str, dict[str, dict]]:
    """nome -> {versao: dep.json} de cada dep em store/deps/<nome>/<v>/.
    O disco e a fonte da verdade (mesmo modelo do scan de apps); a pasta da
    versao carrega <nome>.js + dep.json."""
    out: dict[str, dict[str, dict]] = {}
    root = CONTENT_DIR / "store" / "deps"
    if not root.is_dir():
        return out
    for name_dir in sorted(root.iterdir()):
        if not name_dir.is_dir():
            continue
        for ver_dir in sorted(name_dir.iterdir()):
            meta = read_json(ver_dir / "dep.json") if ver_dir.is_dir() else None
            if not meta or meta.get("name") != name_dir.name:
                continue
            if not (ver_dir / f"{name_dir.name}.js").exists():
                continue  # versao incompleta nao entra no indice
            out.setdefault(name_dir.name, {})[ver_dir.name] = meta
    return out


def _pick_dep(name: str, ranges: list[str], index: dict) -> str:
    """Maior versao do nome que satisfaca TODOS os ranges coletados."""
    versions = index.get(name) or {}
    ok = [v for v in versions
          if all(_range_satisfies(r, v) for r in ranges)]
    if not ok:
        raise HTTPException(400, f"dep '{name}' sem versao que satisfaca "
                                 f"{' / '.join(ranges)} no repositorio")
    return max(ok, key=_semver_tuple)


def _resolve_app_deps(app_deps: dict) -> dict[str, str]:
    """Resolve o grafo de deps (diretas + transitivas) -> {nome: versao}.

    BFS pelos ranges; um nome pode chegar por varios caminhos com ranges
    diferentes — a versao final satisfaz todos eles. Limitacao aceita (as
    deps oficiais nao tem transitivas): transitivas sao coletadas pela
    versao escolhida NA PRIMEIRA visita; se um range posterior muda o pick,
    as transitivas da nova versao nao sao re-coletadas."""
    index = scan_deps()
    wanted: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    queue: list[tuple[str, str]] = list(app_deps.items())
    while queue:
        name, rng = queue.pop(0)
        if (name, rng) in seen:
            continue  # mesmo par por outro caminho (e guard de ciclo)
        seen.add((name, rng))
        wanted.setdefault(name, []).append(rng)
        pick = _pick_dep(name, wanted[name], index)
        for d, r in (index[name][pick].get("deps") or {}).items():
            queue.append((str(d), str(r)))
    return {n: _pick_dep(n, rs, index) for n, rs in wanted.items()}


def catalog_categories() -> dict[str, dict]:
    """slug -> {name, apps: {pkg: entrada compativel com o cliente JS}}.

    A entrada leva meta/app/api (o que o cliente da loja le) e tambem os
    campos de exibicao do app.json (name, description, ...) — o portal usa
    sem ter que baixar cada app.json; o cliente do dispositivo ignora extras.
    """
    cats: dict[str, dict] = {}
    apps_root = CONTENT_DIR / "store" / "apps"
    for pkg, meta in scan_apps().items():
        cat = str(meta.get("category") or "Apps")
        slug = re.sub(r"[^a-z0-9_-]+", "-", cat.lower()).strip("-") or "apps"
        if not SLUG.match(slug):
            slug = "apps"
        entry = {
            "meta": f"{BASE_URL}/store/apps/{pkg}/app.json",
            "app": f"{BASE_URL}/store/apps/{pkg}/main.js",
            "api": int(meta.get("api") or 1),
            "name": str(meta.get("name") or pkg),
            "version": str(meta.get("version") or "1.0.0"),
            "author": str(meta.get("author") or ""),
            "description": str(meta.get("description") or ""),
            "category": cat,
        }
        # campos do fluxo de atualizacao: a loja do device compara versao,
        # valida md5 apos baixar e mostra changelog; o portal mostra o resto.
        # Campos gerenciados pelo publish (size/md5/published_at) sao
        # opcionais: pacotes antigos publicados a mao nao os tem.
        if meta.get("changelog"):
            entry["changelog"] = str(meta["changelog"])
        if meta.get("size"):
            entry["size"] = int(meta["size"])
        if meta.get("md5"):
            entry["md5"] = str(meta["md5"])
        if meta.get("published_at"):
            entry["published_at"] = str(meta["published_at"])
        # Requisitos de hardware (requires psram destrava o teto de 128KB no
        # publish): a loja do device le o campo para o badge "Requer PSRAM"
        # e o bloqueio de install em placa sem PSRAM.
        if meta.get("requires"):
            entry["requires"] = list(meta["requires"])
        # Manifesto multi-arquivo {nome: {size, md5}}: a loja instala e
        # verifica cada um; clientes antigos ignoram o campo.
        if meta.get("files"):
            entry["files"] = meta["files"]
        # Dependencias JS compartilhadas {nome: range}: a loja resolve contra
        # /store/deps.json no install, baixa para /local/modules e grava o
        # deps.json resolvido na pasta do app; clientes antigos ignoram.
        if meta.get("deps"):
            entry["deps"] = meta["deps"]
        if (apps_root / pkg / "icon.png").exists():
            entry["icon"] = f"{BASE_URL}/store/apps/{pkg}/icon.png"
        n_down = _load_stats().get(pkg, 0)
        if n_down:
            entry["downloads"] = n_down  # portal ordena/mostra; o device ignora
        cats.setdefault(slug, {"name": cat, "apps": {}})
        cats[slug]["apps"][pkg] = entry
    return cats


def catalog_updated() -> str:
    root = CONTENT_DIR / "store" / "apps"
    mtimes = [d.stat().st_mtime for d in root.iterdir() if d.is_dir()]
    t = max(mtimes) if mtimes else time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


@app.get("/store/index.json")
def store_index():
    index_cats: dict[str, str] = {"Todos": f"{BASE_URL}/store/all.json"}
    for slug, c in sorted(catalog_categories().items()):
        if c["name"] != "Todos":
            index_cats[c["name"]] = f"{BASE_URL}/store/{slug}.json"
    return {"name": "CelerOS App Store", "base": BASE_URL,
            "updated": catalog_updated(), "categories": index_cats}


@app.get("/store/all.json")
def store_all():
    apps = {k: v for c in catalog_categories().values()
            for k, v in c["apps"].items()}
    return {"category": "Todos", "updated": catalog_updated(), "apps": apps}


def _deps_updated() -> str:
    root = CONTENT_DIR / "store" / "deps"
    mt = [f.stat().st_mtime for f in root.rglob("*") if f.is_file()] \
        if root.is_dir() else []
    t = max(mt) if mt else time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


@app.get("/store/deps.json")
def store_deps():
    """Indice do repositorio de deps: {nome: {versao: {size, md5, minApi,
    deps, url}}}. A loja do device resolve os ranges do app.json contra
    este indice e baixa cada arquivo de url (servido pelo mount estatico).
    Declarada ANTES de /store/{slug}.json para nao virar categoria."""
    out: dict[str, dict] = {}
    for name, versions in scan_deps().items():
        out[name] = {v: {"size": int(m.get("size") or 0),
                         "md5": str(m.get("md5") or ""),
                         "minApi": int(m.get("minApi") or 1),
                         "deps": m.get("deps") or {},
                         "url": f"{BASE_URL}/store/deps/{name}/{v}/{name}.js"}
                     for v, m in versions.items()}
    return {"updated": _deps_updated(), "deps": out}


@app.get("/store/{slug}.json")
def store_category(slug: str):
    if slug == "all":
        raise HTTPException(404, "use /store/all.json")
    cat = catalog_categories().get(slug)
    if not cat:
        raise HTTPException(404, "categoria nao encontrada")
    return {"category": cat["name"], "apps": cat["apps"]}


@app.get("/store/deps/{name}/{version}/{fname}")
def download_dep_js(name: str, version: str, fname: str):
    """Download de dep com contagem ("dep:<nome>" no mesmo contador) — rota
    na frente do mount /store, mesmo path e cache-control."""
    if not DEP_NAME.match(name) or not SEMVER.match(version) or fname != f"{name}.js":
        raise HTTPException(404, "dependencia nao encontrada")
    f = CONTENT_DIR / "store" / "deps" / name / version / fname
    if not f.is_file():
        raise HTTPException(404, "dependencia nao encontrada")
    _count_download(f"dep:{name}")
    return FileResponse(f, media_type="text/javascript",
                        headers={"cache-control": "no-cache"})


@app.get("/store/apps/{pkg}/main.js")
def download_app_js(pkg: str):
    """Download do codigo do app com contagem (rota na frente do mount
    /store; mesmo path, mesmo content-type, cache-control igual)."""
    if not PKG_NAME.match(pkg):
        raise HTTPException(404, "pacote nao encontrado")
    f = CONTENT_DIR / "store" / "apps" / pkg / "main.js"
    if not f.is_file():
        raise HTTPException(404, "pacote nao encontrado")
    _count_download(pkg)
    return FileResponse(f, media_type="text/javascript",
                        headers={"cache-control": "no-cache"})


# --------------------------------------------------------------------------- #
# health / info
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    return {"status": "ok", "version": HUB_VERSION}


@app.get("/api/info")
def info():
    apps = scan_apps()
    deps = scan_deps()
    updates = {}
    upd_root = CONTENT_DIR / "updates"
    if upd_root.is_dir():
        for ch in sorted(upd_root.iterdir()):
            doc = read_json(ch / "update.json") if ch.is_dir() else None
            if doc:
                updates[ch.name] = {
                    "version": doc.get("version"),
                    "api_version": doc.get("api_version"),
                    "variant": doc.get("variant") or "",
                    "hasFirmware": (ch / "firmware.bin").exists(),
                    "updated": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime((ch / "update.json").stat().st_mtime)),
                }
    stats = _load_stats()
    return {
        "service": "celeros-hub",
        "version": HUB_VERSION,
        "base_url": BASE_URL,
        "uptime_s": int(time.time() - STARTED_AT),
        "firmware": {"api_level": FIRMWARE_API_LEVEL},
        "boards": BOARDS,
        "store": {
            "apps": len(apps),
            "categories": sorted({
                str(m.get("category") or "Apps") for m in apps.values()}),
            "dep_packages": len(deps),
            "dep_versions": sum(len(v) for v in deps.values()),
        },
        "downloads": {k: v for k, v in sorted(stats.items()) if not k.startswith("dep:")},
        "downloads_total": sum(v for k, v in stats.items() if not k.startswith("dep:")),
        "dep_downloads": {k[4:]: v for k, v in sorted(stats.items()) if k.startswith("dep:")},
        "updates": updates,
    }


# --------------------------------------------------------------------------- #
# admin: publicar apps (escopo "apps")
# --------------------------------------------------------------------------- #

def _validate_meta(meta: dict) -> dict:
    pkg = str(meta.get("packageName") or "")
    if not PKG_NAME.match(pkg):
        raise HTTPException(400, "packageName invalido (ex.: celeros.minhaapp)")
    if not SEMVER.match(str(meta.get("version") or "")):
        raise HTTPException(400, "version deve ser semver x.y.z")
    for f in ("name", "description", "author"):
        if not str(meta.get(f) or "").strip():
            raise HTTPException(400, f"app.json sem '{f}'")
    try:
        meta["api"] = int(meta.get("api") or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "api deve ser inteiro")
    if not 1 <= meta["api"] <= FIRMWARE_API_LEVEL:
        # nenhum firmware publicado roda o app: ele ficaria no catalogo como
        # "Requer API N" para sempre (ou ate o hub subir o nivel junto)
        raise HTTPException(400, f"api {meta['api']} fora de 1..{FIRMWARE_API_LEVEL} "
                                 f"(nivel do firmware atual)")
    requires = meta.get("requires") or []
    if not isinstance(requires, list) or any(r not in VALID_REQUIRES for r in requires):
        raise HTTPException(400, "requires invalido "
                                 f"(valores: {', '.join(VALID_REQUIRES)})")
    deps = meta.get("deps")
    if deps is not None:
        if not isinstance(deps, dict):
            raise HTTPException(400, "deps deve ser objeto "
                                     '{nome: "^x.y.z"} (ex.: celeros.engine)')
        if len(deps) > MAX_APP_DEPS:
            raise HTTPException(400, f"deps: max {MAX_APP_DEPS} entradas")
        for d, r in deps.items():
            if not DEP_NAME.match(str(d)):
                raise HTTPException(400, f"dep com nome invalido: {d} "
                                         "(use o formato celeros.algo)")
            if not DEP_RANGE.match(str(r)):
                raise HTTPException(400, f"dep {d}: versao deve ser "
                                         '"^1.0.0" ou exata "1.0.0"')
    meta.setdefault("category", "Apps")
    return pkg


def _extract_package(data: bytes) -> tuple[dict, dict[str, bytes]]:
    """Zip do pacote -> (app.json, {arquivo: bytes}).

    Flat: aceita pasta raiz ou arquivos soltos. Nome invalido ou colisao de
    basename e rejeitado na hora (antes o ultimo ganhava em silencio)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(400, "zip invalido")
    total = sum(i.file_size for i in zf.infolist())
    if total > MAX_UNPACKED:
        raise HTTPException(413, "conteudo descomprimido grande demais")
    flat: dict[str, bytes] = {}
    for n in zf.namelist():
        if n.endswith("/"):
            continue
        base = Path(n).name
        if not FILE_NAME.match(base):
            raise HTTPException(400, f"nome de arquivo invalido: {base} "
                                     f"([A-Za-z0-9._-], ate 64 chars)")
        if base in flat:
            raise HTTPException(400, f"arquivo duplicado no pacote: {base}")
        flat[base] = zf.read(n)
    if "app.json" not in flat or "main.js" not in flat:
        raise HTTPException(400, "pacote precisa conter app.json e main.js")
    meta = None
    try:
        meta = json.loads(flat["app.json"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "app.json invalido")
    return meta, flat


def _body_too_big(request: Request) -> bool:
    clen = request.headers.get("content-length")
    try:
        return bool(clen) and int(clen) > MAX_BODY
    except ValueError:
        return False


@app.post("/admin/apps")
async def publish_app(request: Request,
                      file: UploadFile = File(...),
                      force: str = Form(""),
                      agent: Agent = Depends(require_scope("apps"))):
    if _body_too_big(request):
        raise HTTPException(413, "upload grande demais")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "pacote grande demais")
    meta, files = _extract_package(data)
    pkg = _validate_meta(meta)
    # Teto em 2 niveis pela SOMA dos .js (main.js + modulos): e a soma que
    # ocupa a RAM de compile no device. Assets (nao-.js) tem tetos proprios
    # — nao passam pelo Duktape, custam so espaco em disco.
    extras = {n: b for n, b in files.items()
              if n not in ("app.json", "main.js", "icon.png")}
    if len(extras) > MAX_EXTRA_FILES:
        raise HTTPException(400, f"pacote com {len(extras)} arquivos extras "
                                 f"(max {MAX_EXTRA_FILES})")
    js_sum = len(files["main.js"])
    assets_total = 0
    for n, b in extras.items():
        if Path(n).suffix.lower() not in ASSET_EXTS:
            raise HTTPException(400, f"arquivo extra invalido: {n} (extensoes: "
                                     f"{', '.join(ASSET_EXTS)})")
        if len(b) > MAX_ASSET_FILE:
            raise HTTPException(413, f"{n} tem {len(b)} bytes "
                                     f"(max {MAX_ASSET_FILE})")
        if n.endswith(".js"):
            js_sum += len(b)
            if len(b) > STREAM_SAFE_MAIN_JS and meta["api"] < 6:
                raise HTTPException(400, f"{n} > {STREAM_SAFE_MAIN_JS} bytes exige "
                                         f"api >= 6 no app.json (firmware antigo "
                                         f"trunca o download em 32KB)")
        else:
            assets_total += len(b)
    if assets_total > MAX_ASSETS_TOTAL:
        raise HTTPException(413, f"assets somam {assets_total} bytes "
                                 f"(max {MAX_ASSETS_TOTAL})")
    psram_decl = "psram" in (meta.get("requires") or [])
    # Dependencias compartilhadas entram NA MESMA soma do teto: o
    # compartilhado e o armazenamento/flash, mas o device ainda compila a
    # dep no heap de cada app que a requer. Cada dep resolvida tambem
    # exige api do app >= minApi dela (device com api menor que a dep nao
    # pode rodar o app que a declara).
    if meta.get("deps"):
        if meta["api"] < 30:
            raise HTTPException(400, "deps exige api >= 30 no app.json "
                                     "(o require so resolve dependencia na "
                                     "API 30)")
        resolved = _resolve_app_deps(meta["deps"])
        dep_index = scan_deps()
        for dname, dver in resolved.items():
            dmeta = dep_index.get(dname, {}).get(dver) or {}
            js_sum += int(dmeta.get("size") or 0)
            dmin = int(dmeta.get("minApi") or 1)
            if meta["api"] < dmin:
                raise HTTPException(400, f"dep {dname}@{dver} exige api >= "
                                         f"{dmin} (app declara {meta['api']})")
    if js_sum > MAX_MAIN_JS_PSRAM:
        raise HTTPException(413, f"soma dos .js ({js_sum} bytes) acima do teto "
                                 f"absoluto ({MAX_MAIN_JS_PSRAM})")
    if js_sum > MAX_MAIN_JS and not psram_decl:
        raise HTTPException(400, f"soma dos .js ({js_sum} bytes): acima de "
                                 f"{MAX_MAIN_JS} exige \"psram\" em requires no "
                                 f"app.json (sem PSRAM a RAM interna nao fecha o "
                                 f"compile)")
    if len(files["main.js"]) > STREAM_SAFE_MAIN_JS and meta["api"] < 6:
        raise HTTPException(400, f"main.js > {STREAM_SAFE_MAIN_JS} bytes exige "
                                 f"api >= 6 no app.json (firmware antigo "
                                 f"trunca o download em 32KB)")
    if "icon.png" in files:
        if files["icon.png"][:8] != PNG_MAGIC:
            raise HTTPException(400, "icon.png nao e um PNG")
        if len(files["icon.png"]) > MAX_ICON:
            raise HTTPException(413, f"icon.png grande demais (max {MAX_ICON})")

    # nome de exibicao UNICO no catalogo: dois pacotes com o mesmo nome (o
    # legado com.kryonos.physicsdrop 1.0.0 x celeros.physicsdrop 4.0.1)
    # deixavam a loja mostrar o velho no lugar do novo. force=1 aceita.
    nome = str(meta.get("name") or "").strip().casefold()
    if force != "1":
        for opkg, ometa in scan_apps().items():
            if opkg != pkg and str(ometa.get("name") or "").strip().casefold() == nome:
                raise HTTPException(409, f"nome '{meta.get('name')}' ja e de {opkg} "
                                         f"(renomeie ou force=1)")

    # dono e anti-downgrade: o publish de atualizacao respeita quem publicou
    # primeiro e nunca retrocede versao (force=1 exceta ambos; raiz "*" sempre
    # pode) — mesmo contrato do canal OTA.
    dest = CONTENT_DIR / "store" / "apps" / pkg
    cur = read_json(dest / "app.json") if dest.is_dir() else None
    if cur:
        owner = str(cur.get("publisher") or "")
        if owner and owner != agent.name and "*" not in agent.scopes:
            audit(request, agent, "app:publicar", pkg, ok=False,
                  err=f"pertence a {owner}")
            raise HTTPException(403, f"pacote pertence a '{owner}'")
        if cur.get("version") and force != "1" and \
                _semver_tuple(meta["version"]) <= \
                _semver_tuple(str(cur["version"])):
            raise HTTPException(409, f"versao {meta['version']} <= atual "
                                     f"{cur['version']} (force=1 p/ forcar)")

    staging = dest.with_name(dest.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    # campos gerenciados pelo hub: recomputados a cada publish, o app.json do
    # dev manda no resto. Eles viajam para o device no app.json e no catalogo.
    main_blob = files["main.js"]
    meta["size"] = len(main_blob)
    meta["md5"] = hashlib.md5(main_blob).hexdigest()
    meta["published_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["publisher"] = agent.name
    # manifesto multi-arquivo: {nome: {size, md5}} de cada extra. Pacote sem
    # extras limpa o campo (update que removeu modulos/assets nao deixa lixo
    # no catalogo).
    if extras:
        meta["files"] = {n: {"size": len(b),
                             "md5": hashlib.md5(b).hexdigest()}
                         for n, b in sorted(extras.items())}
    else:
        meta.pop("files", None)
    (staging / "main.js").write_bytes(main_blob)
    if "icon.png" in files:
        (staging / "icon.png").write_bytes(files["icon.png"])
    for n, b in extras.items():
        (staging / n).write_bytes(b)
    (staging / "app.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not (staging / "app.json").exists() or not (staging / "main.js").exists():
        shutil.rmtree(staging)
        raise HTTPException(400, "pacote sem app.json/main.js")
    if dest.exists():
        shutil.rmtree(dest)
    staging.rename(dest)
    audit(request, agent, "app:publicar", f"{pkg}@{meta.get('version')}")
    return {"ok": True, "package": pkg, "version": meta.get("version"),
            "md5": meta["md5"], "size": meta["size"],
            "store": {"apps": len(scan_apps())}}


@app.delete("/admin/apps/{pkg}")
def delete_app(pkg: str, request: Request,
               agent: Agent = Depends(require_scope("apps"))):
    if not PKG_NAME.match(pkg):
        raise HTTPException(400, "packageName invalido")
    dest = CONTENT_DIR / "store" / "apps" / pkg
    if not dest.is_dir():
        raise HTTPException(404, "pacote nao encontrado")
    cur = read_json(dest / "app.json")
    owner = str((cur or {}).get("publisher") or "")
    if owner and owner != agent.name and "*" not in agent.scopes:
        audit(request, agent, "app:remover", pkg, ok=False,
              err=f"pertence a {owner}")
        raise HTTPException(403, f"pacote pertence a '{owner}'")
    shutil.rmtree(dest)
    audit(request, agent, "app:remover", pkg)
    return {"ok": True, "removed": pkg, "store": {"apps": len(scan_apps())}}


# --------------------------------------------------------------------------- #
# admin: repositorio de dependencias JS (escopo "deps")
#
# Dep = modulo compartilhado entre apps (engine/fisica): identidade com
# ponto (celeros.engine), uma versao semver por pasta, arquivo unico
# <nome>.js + dep.json {name, version, minApi, deps?} no zip. O hub grava
# em store/deps/<nome>/<versao>/ com campos gerenciados (size/md5/
# published_at/publisher) e o indice /store/deps.json e gerado na rota.
# Dono por NOME (publisher da maior versao); anti-downgrade por versao com
# force=1 para republicar/remediar.
# --------------------------------------------------------------------------- #

def _dep_owner(name: str) -> str:
    """Publisher da maior versao existente do nome (dono do namespace)."""
    versions = scan_deps().get(name) or {}
    if not versions:
        return ""
    top = max(versions, key=_semver_tuple)
    return str(versions[top].get("publisher") or "")


@app.post("/admin/deps")
async def publish_dep(request: Request,
                      file: UploadFile = File(...),
                      force: str = Form(""),
                      agent: Agent = Depends(require_scope("deps"))):
    if _body_too_big(request):
        raise HTTPException(413, "upload grande demais")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "pacote grande demais")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(400, "zip invalido")
    if sum(i.file_size for i in zf.infolist()) > MAX_UNPACKED:
        raise HTTPException(413, "conteudo descomprimido grande demais")
    flat: dict[str, bytes] = {}
    for n in zf.namelist():
        if n.endswith("/"):
            continue
        base = Path(n).name
        if not FILE_NAME.match(base):
            raise HTTPException(400, f"nome de arquivo invalido: {base}")
        if base in flat:
            raise HTTPException(400, f"arquivo duplicado: {base}")
        flat[base] = zf.read(n)
    if len(flat) != 2 or "dep.json" not in flat or \
            sum(1 for k in flat if k.endswith(".js")) != 1:
        raise HTTPException(400, "zip da dep deve conter apenas <nome>.js "
                                 "e dep.json")
    try:
        meta = json.loads(flat["dep.json"].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, ValueError):
        raise HTTPException(400, "dep.json invalido")
    if not isinstance(meta, dict):
        raise HTTPException(400, "dep.json invalido")
    name = str(meta.get("name") or "")
    version = str(meta.get("version") or "")
    if not DEP_NAME.match(name):
        raise HTTPException(400, "name invalido (formato celeros.algo)")
    if not SEMVER.match(version):
        raise HTTPException(400, "version deve ser semver x.y.z")
    js = flat.get(f"{name}.js")
    if js is None:
        raise HTTPException(400, f"zip sem {name}.js")
    if not js.strip():
        raise HTTPException(400, "modulo vazio")
    if len(js) > MAX_DEP_JS:
        raise HTTPException(413, f"{name}.js: {len(js)} bytes "
                                 f"(max {MAX_DEP_JS})")
    try:
        min_api = int(meta.get("minApi") or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "minApi deve ser inteiro")
    if not 1 <= min_api <= 999:
        raise HTTPException(400, "minApi deve ser 1..999")
    sub = meta.get("deps") or {}
    if not isinstance(sub, dict) or len(sub) > MAX_APP_DEPS:
        raise HTTPException(400, f"deps deve ser objeto com ate "
                                 f"{MAX_APP_DEPS} entradas")
    for d, r in sub.items():
        if str(d) == name:
            raise HTTPException(400, "dep nao pode depender de si mesma")
        if not DEP_NAME.match(str(d)) or not DEP_RANGE.match(str(r)):
            raise HTTPException(400, f"dep transitiva invalida: {d}")
    meta["deps"] = sub

    owner = _dep_owner(name)
    if owner and owner != agent.name and "*" not in agent.scopes:
        audit(request, agent, "dep:publicar", name, ok=False,
              err=f"pertence a {owner}")
        raise HTTPException(403, f"dep pertence a '{owner}'")
    existing = scan_deps().get(name) or {}
    if existing:
        top = max(existing, key=_semver_tuple)
        if _semver_tuple(version) <= _semver_tuple(top) and force != "1":
            raise HTTPException(409, f"versao {version} <= atual {top} "
                                     f"(force=1 p/ republicar)")
    if sub:
        try:
            _resolve_app_deps(sub)
        except HTTPException as e:
            raise HTTPException(400, f"deps transitivas nao resolvem: "
                                     f"{e.detail}")

    dest = CONTENT_DIR / "store" / "deps" / name / version
    staging = dest.parent / ".tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    meta["size"] = len(js)
    meta["md5"] = hashlib.md5(js).hexdigest()
    meta["published_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["publisher"] = agent.name
    (staging / f"{name}.js").write_bytes(js)
    (staging / "dep.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if dest.exists():
        shutil.rmtree(dest)  # republicacao (force=1)
    staging.rename(dest)
    audit(request, agent, "dep:publicar", f"{name}@{version}")
    return {"ok": True, "dep": name, "version": version,
            "md5": meta["md5"], "size": meta["size"],
            "url": f"{BASE_URL}/store/deps/{name}/{version}/{name}.js"}


@app.delete("/admin/deps/{name}/{version}")
def delete_dep(name: str, version: str, request: Request,
               agent: Agent = Depends(require_scope("deps"))):
    if not DEP_NAME.match(name) or not SEMVER.match(version):
        raise HTTPException(400, "dep/versao invalida")
    dest = CONTENT_DIR / "store" / "deps" / name / version
    if not dest.is_dir():
        raise HTTPException(404, "versao nao encontrada")
    owner = _dep_owner(name)
    if owner and owner != agent.name and "*" not in agent.scopes:
        audit(request, agent, "dep:remover", f"{name}@{version}", ok=False,
              err=f"pertence a {owner}")
        raise HTTPException(403, f"dep pertence a '{owner}'")
    # Consumidor sem alternativa: remover a unica versao que satisfaz o
    # range de um app (ou de uma dep transitiva) quebraria o install dele.
    dep_index = scan_deps()
    versions_left = [v for v in (dep_index.get(name) or {}) if v != version]

    def _orphaned_range(wanted: dict) -> str:
        for d, r in wanted.items():
            if d != name:
                continue
            if not any(_range_satisfies(str(r), v) for v in versions_left):
                return str(r)
        return ""

    for pkg, meta in scan_apps().items():
        rng = _orphaned_range(meta.get("deps") or {})
        if rng:
            audit(request, agent, "dep:remover", f"{name}@{version}",
                  ok=False, err=f"app {pkg} depende")
            raise HTTPException(409, f"app {pkg} depende de {name} '{rng}' "
                                     f"e nao sobra versao que satisfaca")
    for dname, versions in dep_index.items():
        for v, dmeta in versions.items():
            if dname == name and v == version:
                continue
            rng = _orphaned_range(dmeta.get("deps") or {})
            if rng:
                raise HTTPException(409, f"dep {dname}@{v} depende de {name} "
                                         f"'{rng}' sem alternativa")
    shutil.rmtree(dest)
    audit(request, agent, "dep:remover", f"{name}@{version}")
    return {"ok": True, "removed": f"{name}@{version}"}


# --------------------------------------------------------------------------- #
# admin: publicar firmware OTA (escopo "updates")
# --------------------------------------------------------------------------- #

@app.post("/admin/updates/{channel}")
async def publish_update(
        channel: str,
        request: Request,
        firmware: UploadFile | None = File(None),
        force: str = Form(""),
        agent: Agent = Depends(require_scope("updates")),
):
    if not CHANNEL.match(channel):
        raise HTTPException(400, "canal invalido")
    if _body_too_big(request):
        raise HTTPException(413, "upload grande demais")
    form = await request.form()
    try:
        meta = json.loads(str(form.get("update.json") or "{}"))
    except ValueError:
        raise HTTPException(400, "update.json invalido")
    if not SEMVER.match(str(meta.get("version") or "")):
        raise HTTPException(400, "update.json precisa de version x.y.z")
    for f in ("changelog", "guide"):
        meta.setdefault(f, "")
    for f in ("major_update", "minor_update", "security_update"):
        meta[f] = bool(meta.get(f))
    # api_version: o device RECUSA manifest sem o campo ou abaixo do nivel
    # dele (parser do OtaManager: max 3 digitos). Default = nivel vigente.
    raw_api = meta.get("api_version")
    if raw_api is None or raw_api == "":
        api_version = FIRMWARE_API_LEVEL
    else:
        try:
            api_version = int(raw_api)
        except (TypeError, ValueError):
            raise HTTPException(400, "api_version deve ser inteiro")
        if not 1 <= api_version <= 999:
            raise HTTPException(400, "api_version deve ser 1..999")
    meta["api_version"] = api_version
    # Variante de SKU (ex.: SmartDisplay Y com reles, "smartdisplay-y8"):
    # device com reles recusa manifest sem a SUA variante; device padrao
    # aceita qualquer um — distribuir imagem de variante em canal proprio
    # para nao alcancar aparelho padrao.
    variant = str(meta.get("variant") or "")
    if variant:
        if not VARIANT.match(variant):
            raise HTTPException(400, "variant invalido (ex.: smartdisplay-y8)")
    else:
        meta.pop("variant", None)

    # anti-rollback: nunca publica versao menor que a atual do canal
    dest = CONTENT_DIR / "updates" / channel
    cur = read_json(dest / "update.json") if dest.is_dir() else None
    if cur and cur.get("version") and \
            _semver_tuple(meta["version"]) < _semver_tuple(str(cur["version"])) \
            and force != "1":
        raise HTTPException(409, f"versao {meta['version']} < atual "
                                 f"{cur['version']} do canal (force=1 p/ forcar)")

    has_bin = firmware is not None and (firmware.filename or "") != ""
    if has_bin:
        meta["firmware_url"] = "firmware.bin"  # relativo ao update.json
        blob = await firmware.read(MAX_UPLOAD + 1)
        if len(blob) > MAX_UPLOAD:
            raise HTTPException(413, "firmware grande demais")
        if not blob:
            raise HTTPException(400, "firmware.bin vazio")
        meta["firmware_sha256"] = hashlib.sha256(blob).hexdigest()
    else:
        meta.pop("firmware_url", None)
        meta.pop("firmware_sha256", None)

    dest.mkdir(parents=True, exist_ok=True)
    if has_bin:
        tmp = dest / "firmware.bin.tmp"
        tmp.write_bytes(blob)
        tmp.rename(dest / "firmware.bin")  # atomica: nunca expõe bin pela metade
    else:
        # manifest-only: o manifest e a fonte da verdade do canal. Bin nao
        # referenciado (firmware_url ausente) sai do disco — senao o
        # /api/info mentia hasFirmware e sobraria bin de versao anterior.
        (dest / "firmware.bin").unlink(missing_ok=True)
    (dest / "update.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(request, agent, "ota:publicar", f"{channel}@{meta['version']}",
          err="" if has_bin else "manifest-only")
    return {"ok": True, "channel": channel, "version": meta["version"],
            "api_version": meta["api_version"],
            "variant": meta.get("variant", ""),
            "firmware": has_bin,
            "sha256": meta.get("firmware_sha256"),
            "url": f"{BASE_URL}/updates/{channel}/update.json"}


@app.delete("/admin/updates/{channel}")
def delete_update(channel: str, request: Request,
                  agent: Agent = Depends(require_scope("updates"))):
    if not CHANNEL.match(channel):
        raise HTTPException(400, "canal invalido")
    dest = CONTENT_DIR / "updates" / channel
    if not dest.is_dir():
        raise HTTPException(404, "canal nao encontrado")
    shutil.rmtree(dest)
    audit(request, agent, "ota:remover", channel)
    return {"ok": True, "removed": channel}


# --------------------------------------------------------------------------- #
# estaticos (montados por ultimo; / por ultimo de todos)
# --------------------------------------------------------------------------- #

class NoCacheStatic(StaticFiles):
    """JSON/binarios pequenos: revalidacao barata, dispositivos sao poucos."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["cache-control"] = "no-cache"
        return resp


app.mount("/store", NoCacheStatic(directory=CONTENT_DIR / "store"), name="store")
app.mount("/updates", NoCacheStatic(directory=CONTENT_DIR / "updates"), name="updates")
app.mount("/help", NoCacheStatic(directory=CONTENT_DIR / "help"), name="help")
app.mount("/assets", StaticFiles(directory=CONTENT_DIR / "www" / "assets"), name="assets")
app.mount("/", StaticFiles(directory=CONTENT_DIR / "www", html=True), name="www")


@app.exception_handler(404)
def not_found(request: Request, exc):
    if request.url.path.startswith("/api/") or request.url.path.startswith("/admin/"):
        return JSONResponse({"detail": "nao encontrado"}, status_code=404)
    return JSONResponse({"detail": getattr(exc, "detail", "not found")},
                        status_code=404)
