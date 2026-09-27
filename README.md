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
│   ├── updates/<canal>/          # esp32 | smartdisplay_4848S040 (+beta)
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

Secret do token admin (uma vez, no Swarm):

```bash
printf 'SEU-TOKEN-FORTE' | docker secret create celeros_hub_admin_token -
```

Build + deploy (a tag segue a versão da base python — padrão do datacenter;
rebuild com a mesma tag + `denv update` re-publica código/conteúdo novo):

```bash
denv start celeros-hub -t 3.12.10     # primeira vez
denv update celeros-hub -t 3.12.10    # depois de mudar codigo/conteudo
```

O sync leva `content/` para `/mnt/nfs/celeros-hub/content` (montado rw — a
API admin publica em runtime). O DNS de `os.celer.tec.br` aponta para o
Traefik do cluster (entrypoint `websecure`).

## Publicar

Token: `--token` ou `export CELER_HUB_TOKEN=...` (o mesmo do secret).

```bash
# app (pasta com app.json + main.js [+ icon.png])
python3 tools/publish_app.py minha-pasta-do-app --token $CELER_HUB_TOKEN

# firmware OTA
python3 tools/publish_firmware.py esp32 build-cyd/KryonOS.bin \
  --version 1.3.0 --changelog "- novidade" --token $CELER_HUB_TOKEN
python3 tools/publish_firmware.py smartdisplay_4848S040 build-smartdisplay/KryonOS.bin \
  --version 1.3.0 --changelog "- novidade" --token $CELER_HUB_TOKEN
```

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
  `version`, `api_version`, flags `major/minor/security_update`, `changelog`,
  `guide`, `firmware_url` (relativa resolve contra o diretório do manifest).
  Instala só se `version` > versão gravada no dispositivo.
- **Ajuda** (cliente: `data/apps/Help/main.js`): `index.json`
  `{categories:[{name,url}]}` → `{articles:[{title,url}]}` → `{content}`.
  Textos SEM acentos: a fonte do dispositivo não tem glifos acentuados.

## Validado

- Fluxo completo do cliente simulado (index → categorias → meta → main.js)
  com 19 apps em 6 categorias; ajuda (2 categorias, 6 artigos); OTA dos dois
  canais; `/health` e `/api/info` (consumidos pelo denv).
- Publish de app e firmware via CLI (multipart), DELETE de app/canal, 403 sem
  token, atomicidade do `firmware.bin` (write→rename).
- Portal renderizado e conferido no navegador (loja/OTA/ajuda/devs).
