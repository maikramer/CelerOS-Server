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
#       "*" (ambos). Revogacao/rotacao por token, sem afetar os demais.
#   - rate limit de falhas de auth por IP (X-Forwarded-For do Traefik; so o
#     reverse proxy alcanca o container — nao ha porta publicada no host).
#   - auditoria em APPEND-ONLY (AUDIT_DIR): ts, ip, agente, acao, alvo, erro.
#   - uploads: teto de tamanho (request e descomprimido), whitelist de
#     arquivos do zip, checagem de PNG, packageName/semver/canal validados.
#   - OTA: rejeita versao MENOR que a atual do canal (anti-rollback; force=1
#     para excecao) e grava firmware_sha256 no manifest.
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
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

HUB_VERSION = "0.2.0"
BASE_URL = os.environ.get("BASE_URL", "https://os.celer.tec.br").rstrip("/")
CONTENT_DIR = Path(os.environ.get("CONTENT_DIR", "/data/content"))
AUDIT_DIR = Path(os.environ.get("AUDIT_DIR", "/data/audit"))
MAX_UPLOAD = 48 * 1024 * 1024   # teto por upload (firmware ~2 MB; folga p/ zip)
MAX_UNPACKED = 64 * 1024 * 1024  # teto total descomprimido (anti zip-bomb)
MAX_BODY = MAX_UPLOAD + 1024 * 1024
AUTH_FAILS_LIMIT = 10           # falhas de auth...
AUTH_FAILS_WINDOW = 600         # ...dentro desta janela (s)...
AUTH_BLOCK_SECS = 900           # ...bloqueiam o IP por este tempo
STARTED_AT = time.time()

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
PKG_NAME = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)+$")  # ex.: celeros.demo
CHANNEL = re.compile(r"^[a-z0-9_.-]+$")
SLUG = re.compile(r"^[a-z0-9_-]+$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

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

VALID_SCOPES = {"apps", "updates", "*"}


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
        if (apps_root / pkg / "icon.png").exists():
            entry["icon"] = f"{BASE_URL}/store/apps/{pkg}/icon.png"
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


@app.get("/store/{slug}.json")
def store_category(slug: str):
    if slug == "all":
        raise HTTPException(404, "use /store/all.json")
    cat = catalog_categories().get(slug)
    if not cat:
        raise HTTPException(404, "categoria nao encontrada")
    return {"category": cat["name"], "apps": cat["apps"]}


# --------------------------------------------------------------------------- #
# health / info
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    return {"status": "ok", "version": HUB_VERSION}


@app.get("/api/info")
def info():
    apps = scan_apps()
    updates = {}
    upd_root = CONTENT_DIR / "updates"
    if upd_root.is_dir():
        for ch in sorted(upd_root.iterdir()):
            doc = read_json(ch / "update.json") if ch.is_dir() else None
            if doc:
                updates[ch.name] = {
                    "version": doc.get("version"),
                    "hasFirmware": (ch / "firmware.bin").exists(),
                    "updated": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime((ch / "update.json").stat().st_mtime)),
                }
    return {
        "service": "celeros-hub",
        "version": HUB_VERSION,
        "base_url": BASE_URL,
        "uptime_s": int(time.time() - STARTED_AT),
        "store": {
            "apps": len(apps),
            "categories": sorted({
                str(m.get("category") or "Apps") for m in apps.values()}),
        },
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
    meta.setdefault("category", "Apps")
    return pkg


def _extract_package(data: bytes) -> tuple[dict, dict[str, bytes]]:
    """Zip do pacote -> (app.json, {arquivo: bytes})."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(400, "zip invalido")
    total = sum(i.file_size for i in zf.infolist())
    if total > MAX_UNPACKED:
        raise HTTPException(413, "conteudo descomprimido grande demais")
    names = [n for n in zf.namelist() if not n.endswith("/")]
    flat = {}
    for n in names:
        flat[Path(n).name] = zf.read(n)  # aceita pasta raiz ou arquivos soltos
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
                      agent: Agent = Depends(require_scope("apps"))):
    if _body_too_big(request):
        raise HTTPException(413, "upload grande demais")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "pacote grande demais")
    meta, files = _extract_package(data)
    pkg = _validate_meta(meta)
    if "icon.png" in files and files["icon.png"][:8] != PNG_MAGIC:
        raise HTTPException(400, "icon.png nao e um PNG")
    dest = CONTENT_DIR / "store" / "apps" / pkg
    staging = dest.with_name(dest.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    allowed = {"app.json", "main.js", "icon.png"}
    for name, blob in files.items():
        if name in allowed:
            (staging / name).write_bytes(blob)
    if not (staging / "app.json").exists() or not (staging / "main.js").exists():
        shutil.rmtree(staging)
        raise HTTPException(400, "pacote sem app.json/main.js")
    if dest.exists():
        shutil.rmtree(dest)
    staging.rename(dest)
    audit(request, agent, "app:publicar", f"{pkg}@{meta.get('version')}")
    return {"ok": True, "package": pkg, "version": meta.get("version"),
            "store": {"apps": len(scan_apps())}}


@app.delete("/admin/apps/{pkg}")
def delete_app(pkg: str, request: Request,
               agent: Agent = Depends(require_scope("apps"))):
    if not PKG_NAME.match(pkg):
        raise HTTPException(400, "packageName invalido")
    dest = CONTENT_DIR / "store" / "apps" / pkg
    if not dest.is_dir():
        raise HTTPException(404, "pacote nao encontrado")
    shutil.rmtree(dest)
    audit(request, agent, "app:remover", pkg)
    return {"ok": True, "removed": pkg, "store": {"apps": len(scan_apps())}}


# --------------------------------------------------------------------------- #
# admin: publicar firmware OTA (escopo "updates")
# --------------------------------------------------------------------------- #

def _semver_tuple(v: str) -> tuple:
    return tuple(int(p) for p in v.split("."))


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
    meta.setdefault("api_version", 2)

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
    (dest / "update.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(request, agent, "ota:publicar", f"{channel}@{meta['version']}",
          err="" if has_bin else "manifest-only")
    return {"ok": True, "channel": channel, "version": meta["version"],
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
