# Subindo o CelerOS Hub com o denv

Este é o roteiro de deploy do hub (**os.celer.tec.br**) — do zero ao dia a dia,
com as pegadinhas que a prática já cobrou. O repo é a aplicação: a stack vive em
`stacks/.celeros-hub/` (stack **externa** do denv), o conteúdo vai por rsync ao
NFS e a API viaja numa imagem Docker versionada no registry
(`maikramer/celeros-hub`).

```
o que você mudou?
├── content/            (wiki, portal, help, apps)   → denv sync apply      (~20 s, sem restart)
├── compose/secrets/DNS (stacks/, env/)              → denv update -t atual (re-aplica in-place)
└── api/app.py          (código da API)              → version → build → update (rolling)
```

Todo caminho termina em verificação: `/health` + `/api/info` + a página que mudou.

---

## 1. Primeira subida (uma vez por cluster)

Pré-requisitos: cluster Swarm com a stack `traefik` de pé (entrypoint
`websecure` + TLS da zona), NFS montado, Docker logado no registry, repo clonado.

### 1.1 Registre a stack externa

```bash
denv external add ~/GitClones/CelerOS-Server
```

O denv passa a ler a definição direto do repo (`stacks/.celeros-hub/service.yml`).

### 1.2 Crie os secrets de token

Sem eles o hub sobe, mas o `/admin` responde `503` (desabilitado por design —
sem credencial configurada, melhor desligado que aberto). A partir da pasta
`env/` local (gitignored):

```bash
python3 tools/make_tokens.py > env/tokens.json   # edite nomes/escopos a gosto
docker secret create celeros_hub_admin_token env/root-token.txt
docker secret create celeros_hub_tokens       env/tokens.json
```

Secrets são imutáveis no Swarm — rotacionar é `docker secret rm` + `create` +
`denv update` (o hub relê o arquivo a cada request; sem restart de processo).
Detalhes e escopos: [README › Segurança](README.md#segurança).

### 1.3 DNS & TLS (uma vez por domínio)

Na zona `celer.tec.br` do Cloudflare: registro `os` → IP público do Traefik
(**187.95.46.25**). *Proxied* funciona com o Origin Cert existente; sem proxy,
configure certresolver próprio. O container do hub **não publica porta no
host** — só o Traefik o alcança.

### 1.4 Sync + primeiro deploy

```bash
denv sync apply celeros-hub -y --no-tui      # conteúdo/ → NFS
denv update celeros-hub -t 0.7.0 --no-tui    # tag = HUB_VERSION vigente
```

Na primeira vez o `update` **cria** a stack (create-or-update; nas seguintes só
re-aplica). Se a imagem da tag ainda não existir no registry, faça o ciclo de
código (seção 3) antes.

### 1.5 Confira

```bash
curl -s os.celer.tec.br/health        # {"status":"ok","version":"…"}
denv status | grep celeros            # RUNNING 1/1
```

---

## 2. Dia a dia — conteúdo: `sync`

Wiki, portal, help e o catálogo semeado são **arquivos no NFS**; o hub lê do
disco a cada request. Publicar conteúdo não toca no container:

```bash
# …edite content/…, commit + push…
denv sync apply celeros-hub -y --no-tui

# conferir o que iria antes (opcional):
denv sync diff celeros-hub
```

Notas importantes:

- O rsync **não deleta** o que só existe no servidor: apps publicados pela API
  admin em runtime (gravados no NFS pelo volume rw) sobrevivem ao sync. O
  contrário também vale — remover do repo não remove da loja; para isso use
  `DELETE /admin/apps/<pkg>`.
- **Mudou CSS/JS do portal ou da wiki?** O Cloudflare cacheia assets estáticos
  (`.css`/`.js`/`.webmanifest`) — o mount não manda `no-cache` para eles.
  Convenção: **cache-bust** — bump na query da URL no HTML que referencia
  (`/style.css?v=0703`). HTML e catálogos (`/store/*.json`, `/api/info`) são
  sempre frescos (`cf-cache-status: DYNAMIC`).

---

## 3. Dia a dia — código: o ciclo completo (com a dança do marker)

Mudou `api/app.py`? Cinco passos — dois deles são o git-guard do denv
conversando com você:

```bash
# 1) suba HUB_VERSION em stacks/.celeros-hub/api/app.py (ex.: 0.7.0 → 0.8.0)
#    A TAG da imagem É a versão do serviço: tag nova no registry força build
#    de verdade (tag reescrita deixava o update pular o build e rodar código
#    velho).

# 2) commit + push do código — o git-guard aborta sync/deploy com working
#    tree sujo ("repo denv não está em dia").

# 3) build + push da imagem:
denv build --service celeros-hub --tag 0.8.0 --push
#    ATENÇÃO: o build reescreve o MARKER `ARG BASE_TAG` em
#    stacks/.celeros-hub/dockerfile.celeros-hub — o repo fica sujo de propósito
#    (marcador da última imagem buildada).

# 4) commit + push do marker, senão o update a seguir aborta no git-guard
#    (a pegadinha clássica: o próprio build suja o repo que o guard exige limpo):
git add stacks/.celeros-hub/dockerfile.celeros-hub
git commit -m "marker de build: ARG BASE_TAG=0.8.0" && git push

# 5) rolling update (update = sync + docker stack deploy in-place):
denv update celeros-hub -t 0.8.0 --no-tui
```

Semântica do `update`: **nunca** remove a stack nem dispara backup de boot;
container novo só quando imagem/config mudam de verdade. Re-aplicar igual é
no-op e o **uptime preserva** — uptime alto após um update de conteúdo é o
comportamento correto, não um deploy que falhou.

### Verificação pós-deploy (30 segundos de ritual)

```bash
curl -s os.celer.tec.br/health | jq .          # versão nova?
curl -s os.celer.tec.br/api/info | jq '{uptime:.uptime_s, api:.firmware.api_level, apps:.store.apps}'
# uptime zerado   = container rolou (imagem/config mudou)
# uptime preservado = no-op intencional (mesma tag, mesmo compose)
curl -s -o /dev/null -w '%{http_code}\n' os.celer.tec.br/docs/   # o que mudou, no ar?
denv status | grep celeros-hub                                   # RUNNING 1/1
```

O healthcheck do Swarm bate `/health` a cada 30 s (start period 30 s); o painel
do denv consome `/health` + `/api/info` como JSON de monitoramento.

---

## 4. Operação de rotina

| Comando | Para quê |
|---|---|
| `denv status` | saúde de todas as stacks (score, réplicas) |
| `denv logs celeros-hub` | logs do serviço com filtros |
| `denv exec celeros-hub …` | comando dentro do container |
| `denv restart celeros-hub` | restart controlado |
| `denv diagnose celeros-hub` | diagnóstico do serviço (`diagnose-stack` para a stack) |
| `denv sync diff celeros-hub` | o que o sync levaria (local vs remoto) |
| `denv update celeros-hub -t <atual>` | re-aplicar compose/secrets sem trocar imagem |

---

## 5. Anatomia da stack

| Arquivo | Papel |
|---|---|
| `stacks/.celeros-hub/service.yml` | metadados da stack denv, healthcheck `/health`, monitoring (`/health` + `/api/info`), registry e a definição do **sync** (rsync `content/` → NFS; `required_dirs` content+audit — o audit fica fora do rsync de propósito) |
| `stacks/.celeros-hub/docker-compose.yml` | serviço único: imagem por `BASE_TAG`, secrets montados, volumes NFS rw (content + audit), healthcheck wget interno, placement `dockergpu`, Traefik `Host(os.celer.tec.br)` na rede pública, limites 0.5 CPU / 256 MB |
| `stacks/.celeros-hub/dockerfile.celeros-hub` | python:3.12-alpine + fastapi/uvicorn/multipart pinados; copia **um único arquivo** (`api/app.py`) — e o marker `ARG BASE_TAG` que o denv reescreve a cada build |
| `stacks/.celeros-hub/api/app.py` | o hub (~850 linhas): rotas, auth, catálogo na rota, admin, mounts |
| `env/` (gitignored) | `root-token.txt`, `tokens.json`, `env.sh` — fonte local dos Docker secrets |

---

## 6. Rodar local (dev)

```bash
pip install fastapi==0.115.12 "uvicorn[standard]==0.34.2" python-multipart==0.0.20

BASE_URL=http://127.0.0.1:8901 CONTENT_DIR=$PWD/content \
HUB_ADMIN_TOKEN=devtoken HUB_DOCS=1 \
  python3 -m uvicorn --app-dir stacks/.celeros-hub/api app:app --port 8901
# Swagger em http://127.0.0.1:8901/api/docs (HUB_DOCS=1)
```

Ajuda e OTA usam URLs absolutas de produção nos arquivos de conteúdo — para
testar local, mapeie `os.celer.tec.br` para `127.0.0.1` no `/etc/hosts` ou
aponte o `BASE_URL` de teste. Antes de qualquer deploy de código:
`python3 tools/test_api.py` (67 checks offline, sem rede e sem secrets).

---

## 7. Troubleshooting — os reais

| Sintoma | Causa / caminho |
|---|---|
| `update` aborta: "repo denv não está em dia" | git-guard: working tree sujo — quase sempre o **marker BASE_TAG** que o próprio build reescreveu. Commit + push do marker e rode de novo (`--no-git-check` só consciente) |
| Mudança no CSS/JS não aparece | cache do Cloudflare em asset estático — bump na query `?v=` da URL no HTML |
| Uptime não zerou após update | imagem/compose idênticos → Swarm não reinicia (no-op). Esperava rollover? confira se tag e compose mudaram de verdade |
| CLI de publish recebe `403 error code: 1010` | Bot Fight Mode do Cloudflare barra o TLS do python-urllib — publique via `curl` ou crie regra WAF Skip para `/admin/*` |
| Portal 404 / wiki não aparece | rodou o `denv sync apply`? arquivo está em `content/www/…`? |
| `/admin` devolve 503 | nenhum secret de token montado — esperado até configurar |
| App publicado "sumiu" após sync | não some: sync não deleta runtime; pacote sem `app.json`+`main.js` é pulado do catálogo |
| Downloads zeraram após redeploy | contador vive em `/data/stats` na camada do container — conhecido, sem volume dedicado ainda |

---

A mesma documentação, com diagramas e capturas: **[os.celer.tec.br/docs/ops-deploy](https://os.celer.tec.br/docs/ops-deploy.html)**.
