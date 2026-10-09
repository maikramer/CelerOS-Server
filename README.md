# CelerOS Hub

Servidor do ecossistema **CelerOS** (fork GPL-3.0 do KryonOS): **os.celer.tec.br**.
Loja de apps que a App Store do dispositivo lê, canais **OTA** por placa,
central de **ajuda online** e portal web com o branding Celer — tudo em um
serviço único, implantado no datacenter como **stack denv externa**.

```
dispositivo (CYD / SmartDisplay)                CelerOS Hub
  App Store   ──GET /store/index.json──────────► catálogo dinâmico
              ──GET /store/apps/<pkg>/main.js─► pacotes (NFS)
  Settings    ──GET /updates/<canal>/update.json► canal OTA da placa
              ──GET .../firmware.bin──────────► binário do firmware
  Ajuda       ──GET /help/index.json──────────► artigos
  navegador   ──GET /─────────────────────────► portal (loja/OTA/devs)
```

## Layout

```
CelerOS-Server/
├── stacks/.celeros-hub/          # stack denv (stack externa deste repo)
│   ├── service.yml               # metadados, sync (content/ -> NFS), build
│   ├── docker-compose.yml        # Swarm: traefik Host(os.celer.tec.br), secret
│   ├── dockerfile.celeros-hub    # python alpine + fastapi/uvicorn pinados
│   └── api/app.py                # o hub: estáticos + health + admin
├── content/                      # conteúdo servido (sincronizado ao NFS)
│   ├── www/                      # portal (index.html + assets de marca)
│   ├── store/apps/<pkg>/         # pacotes: app.json + main.js [+ icon.png]
│   ├── updates/<canal>/          # esp32 | smartdisplay_4848S040 |
│   │                             # waveshare_amoled206 | spotpear_zzpet | devkit
│   └── help/                     # index -> categorias -> artigos
├── branding/                     # artes originais (fonte da marca)
└── tools/                        # CLIs de publicação e manutenção
```

O **catálogo é gerado na rota** (`/store/index.json`, `/store/all.json`,
`/store/<cat>.json`) a partir do scan de `content/store/apps/` — o disco é a
fonte da verdade e as URLs absolutas usam o `BASE_URL` do ambiente do servidor
em execução (nada de URL de produção assada em arquivo).

## Rodar local (dev)

```bash
pip install fastapi==0.115.12 "uvicorn[standard]==0.34.2" python-multipart==0.0.20
BASE_URL=http://127.0.0.1:8901 CONTENT_DIR=$PWD/content \
HUB_ADMIN_TOKEN=devtoken \
  python3 -m uvicorn --app-dir stacks/.celeros-hub/api app:app --port 8901
```

Ajuda e OTA usam URLs absolutas de produção nos arquivos de conteúdo; para
testar localmente, mapeie `os.celer.tec.br` para `127.0.0.1` (ou edit os
JSONs de `content/help`).

## Deploy (denv)

Registro único (stack externa — o repo é a aplicação):

```bash
denv external add ~/GitClones/CelerOS-Server
```

Credenciais: a pasta local `env/` (gitignored) guarda root + tokens nomeados
e o `env/README.md` mostra como criar os secrets a partir dela
(`celeros_hub_admin_token` e `celeros_hub_tokens`).

DNS (uma vez, no Cloudflare da zona `celer.tec.br`): aponte
`os.celer.tec.br` para o IP público do Traefik (187.95.46.25) — proxied
funciona com o Origin Cert existente; sem proxy, entre com um certresolver
ou Origin Cert próprio do host.

**Roteiro completo — primeira subida, ciclo de código, verificação pós-deploy
e troubleshooting: [DEPLOY.md](DEPLOY.md).** O essencial do dia a dia:

```bash
# conteúdo (wiki/portal/help/apps): sem restart
denv sync apply celeros-hub -y --no-tui

# código da API: a TAG é a versão do hub (HUB_VERSION em api/app.py).
# O git-guard exige commit+push — inclusive do MARKER `ARG BASE_TAG` que o
# próprio build reescreve no dockerfile (a pegadinha clássica do fluxo):
denv build --service celeros-hub --tag 0.7.0 --push
git add stacks/.celeros-hub/dockerfile.celeros-hub
git commit -m "marker de build: ARG BASE_TAG=0.7.0" && git push
denv update celeros-hub -t 0.7.0
```

O sync leva `content/` para `/mnt/nfs/celeros-hub/content` (montado rw — a
API admin publica em runtime). O DNS de `os.celer.tec.br` aponta para o
Traefik do cluster (entrypoint `websecure`).

## Publicar

Token: `--token` ou `export CELER_HUB_TOKEN=...` (o mesmo do secret).

```bash
# app (pasta com app.json + main.js [+ icon.png])
python3 tools/publish_app.py minha-pasta-do-app --token $CELER_HUB_TOKEN

# firmware OTA — canal da placa:
#   esp32 (CYD) | smartdisplay_4848S040 | waveshare_amoled206 (watch)
#   spotpear_zzpet (cão) | devkit (barebone)
python3 tools/publish_firmware.py esp32 build-cyd/CelerOS.bin \
  --version 1.5.0 --changelog "- novidade" --token $CELER_HUB_TOKEN

# SmartDisplay Y (relés): imagem da variante em canal próprio + --variant
python3 tools/publish_firmware.py smartdisplay-y build-y/CelerOS.bin \
  --version 1.5.0 --variant smartdisplay-y8 --token $CELER_HUB_TOKEN
```

O `--api-version` do firmware tem default no hub (o nível atual, `FIRMWARE_API_LEVEL`
em `api/app.py` — hoje 32, espelhando o `CELEROS_API_LEVEL` do CelerOS): o
dispositivo **recusa** manifest sem `api_version` ou abaixo do nível dele. O
mesmo nível é o **teto** do publish de apps: `api` acima dele nenhum firmware
roda (400). Ao subir a API no firmware, suba aqui junto (hub novo + deploy).

Catálogo semente vindo do upstream (KryonOS-AppStore), uma vez:

```bash
python3 tools/import_kryonos_catalog.py   # --dry-run para inspecionar
```

Assets de marca (se as artes em `branding/` mudarem):

```bash
python3 tools/make_branding.py
```

## Segurança

**Leitura é pública por design** (a loja, o OTA e a ajuda do dispositivo não
autenticam). **Escrita só com token** — `POST/DELETE /admin/*` exigem
`Authorization: Bearer <token>` e **escopo**:

| escopo    | permite                          | para quem              |
|-----------|----------------------------------|------------------------|
| `*`       | tudo                             | você (root)            |
| `apps`    | publicar/remover apps da loja    | colaboradores          |
| `updates` | publicar firmware nos canais OTA | CI / publicador de fw  |

Credenciais vivem em **Docker secrets** (nunca em environment/commit):

```bash
# root (acesso total, retrocompativel):
printf 'SEU-TOKEN-FORTE' | docker secret create celeros_hub_admin_token -

# tokens nomeados (cada um com escopo; o nome aparece no log de auditoria):
python3 tools/make_tokens.py > tokens.json     # gera; edite nomes a gosto
docker secret create celeros_hub_tokens < tokens.json && shred -u tokens.json
```

Sem nenhum secret configurado, o `/admin` fica **desabilitado** (503). Rotação
e revogação são por token: gere um JSON novo e recrie o secret `celeros_hub_tokens`
+ `denv update` (secrets são imutáveis no Swarm; o hub relê o arquivo a cada
request, sem restart do processo). `GET /admin/whoami` confere nome/escopos de
um token — útil no CI.

**Demais camadas:**

- **Rate limit de falhas de auth** por IP (10 erros em 10 min → bloqueio de
  15 min). Confia no `X-Forwarded-For` do Traefik porque o container não tem
  porta publicada no host — só o reverse proxy o alcança.
- **Auditoria append-only** em `/mnt/nfs/celeros-hub/audit/audit.log` (fora do
  rsync de propósito): ts, IP, nome do token, ação, alvo, sucesso/falha —
  incluindo tentativas de auth.
- **Anti-rollback OTA**: publicar versão *menor* que a atual do canal retorna
  409 (`--force` na CLI para exceção consciente).
- **Integridade do firmware**: o publish grava `firmware_sha256` no
  `update.json` (campo extra; firmware atual ignora — verificação no
  dispositivo fica como evolução, junto com CA pinning no `esp_https_ota`,
  que hoje roda `setInsecure`).
- **Uploads**: teto por request e do total descomprimido do zip (anti
  zip-bomb), whitelist de arquivos do pacote, checagem de magic bytes do PNG,
  validação de `packageName`/semver/canal (sem path traversal).
- **Catálogo coerente**: nome de exibição **único** entre pacotes (409 sem
  `force=1` — o legado `com.kryonos.physicsdrop` escondia o
  `celeros.physicsdrop` 4.0.1 na loja do device) e `api` limitada ao nível do
  firmware. Teto da soma dos `.js` (app + deps): 48 KB sem PSRAM, **1 MB** com
  `requires: ["psram"]`; o `celerhub.py` sobe os `.js` enxutos (sem
  comentários, como o device compila), então a soma é o custo real.
- **Downloads**: contados por app e por dep (`/api/info` → `downloads`,
  `dep_downloads`; a entrada do catálogo traz `downloads`), persistidos em
  `STATS_DIR` no volume NFS da auditoria (`/data/audit/stats`) — sobrevivem a
  redeploy.
- **Superfície**: `/api/docs` (Swagger) desligado em produção
  (`HUB_DOCS=1` em dev); TLS termina no Traefik (entrypoint `websecure`).

**CI (exemplo GitHub Actions)** — token com escopo `updates` no secret
`CELER_HUB_TOKEN` do repo:

```yaml
- name: Publicar OTA
  run: |
    python3 CelerOS-Server/tools/publish_firmware.py esp32 \
      build-cyd/KryonOS.bin --version ${{ steps.ver.outputs.v }} \
      --changelog "${{ github.event.head_commit.message }}" \
      --hub https://os.celer.tec.br --token ${{ secrets.CELER_HUB_TOKEN }}
```

## Contratos servidos (compatibilidade com o firmware)

- **Loja** (cliente: `data/apps/App Store/main.js`): `index.json`
  `{categories:{nome:url}}` → categoria `{apps:{id:{meta,app,api}}}` →
  `app.json` `{packageName,name,description,author,version,api}`. Campos
  extras (name/version/author/... na entrada) são ignorados pelo cliente e
  usados pelo portal.
- **OTA v2** (cliente: `main/OTA/OtaManager.cpp`): `update.json` com
  `version`, `api_version` (validada pelo hub: 1..999, default = nível atual
  — o device **recusa** manifest abaixo do próprio nível), flags
  `major/minor/security_update`, `changelog`, `guide`, `variant` de SKU
  (ex.: `smartdisplay-y8`; device com relés recusa manifest sem a própria
  variante — publique imagem de variante em canal próprio) e `firmware_url`
  (relativa resolve contra o diretório do manifest). Instala só se `version`
  > versão gravada no dispositivo.
- **Ajuda** (cliente: `data/apps/Help/main.js`): `index.json`
  `{categories:[{name,url}]}` → `{articles:[{title,url}]}` → `{content}`.
  Textos SEM acentos: a fonte do dispositivo não tem glifos acentuados.

## Validado

- Fluxo completo do cliente simulado (index → categorias → meta → main.js)
  com 19 apps em 6 categorias; ajuda (3 categorias, 9 artigos); OTA dos dois
  canais; `/health` e `/api/info` (consumidos pelo denv).
- Publish de app e firmware via CLI (multipart), DELETE de app/canal, 403 sem
  token, atomicidade do `firmware.bin` (write→rename).
- `tools/test_api.py`: 110 checks via TestClient (apps, multi-arquivo, limites,
  nível de API e nome único, downloads de app e dep, deps, OTA —
  api_version/variant/anti-rollback).
- Portal renderizado e conferido no navegador (loja/placas/firmware/CelerOS/
  ajuda/devs).
