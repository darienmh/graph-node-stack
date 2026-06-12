# graph-node-stack

Self-hosted [graph-node](https://github.com/graphprotocol/graph-node) indexer stack
for **any EVM chain**, behind Traefik with automatic TLS. Everything server-specific
lives in `.env`, so the same files deploy unchanged to any box.

This is the configuration distilled from a live deployment: a single parametrised
`docker-compose.yml` (no override files), Postgres tuned via env vars, a small
nginx RPC load-balancer, an API-key forwardAuth gate, and a Traefik front with
Cloudflare DNS-01 certificates.

`graph-node` itself supports any EVM network (Ethereum, Polygon, BSC, Arbitrum,
Base, Optimism, …). The included `config.toml` ships configured for **Polygon
(`matic`)** as a worked example — point it at a different chain by editing one
line plus the RPCs (see [Using a different chain](#using-a-different-chain)).

## Architecture

```
                          ┌── graph.<domain>        :8000  GraphQL query   (API key)
   Cloudflare (proxy) ──► │── graph-admin.<domain>  :8020  admin JSON-RPC  (API key)
        │   Traefik       │── graph-status.<domain> :8030  index status    (public)
        │   (DNS-01 TLS)  └── graph-ipfs.<domain>   :5001  IPFS add/version (API key)
        ▼
   ┌─────────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌────────────┐
   │ graph-node  │──►│ postgres │   │   ipfs   │   │  rpc-lb  │   │ graph-auth │
   │  v0.43.0    │   │    16    │   │  kubo    │   │  nginx   │   │ forwardAuth│
   └──────┬──────┘   └──────────┘   └──────────┘   └────┬─────┘   └────────────┘
          │                                             │
          └── single provider http://rpc-lb:8545 ───────┘── round-robin ► RPC_1/2/3
```

| Service | Image | Role |
|---|---|---|
| `graph-node` | `graphprotocol/graph-node:v0.43.0` | the indexer; query/admin/status/metrics ports |
| `postgres` | `postgres:16` | entity store + chain cache (tuned via `PG_*`) |
| `ipfs` | `ipfs/kubo:v0.34.1` | subgraph manifest + WASM store |
| `rpc-lb` | `nginx:alpine` | round-robins requests across `RPC_1/2/3` |
| `graph-auth` | `python:3.12-alpine` | forwardAuth target validating `API_KEYS` |
| `traefik` | `traefik:v3.6` | TLS termination + routing (Cloudflare DNS-01) |

All graph-node ports are also bound to `127.0.0.1` for SSH-tunnel access without
going through Traefik.

## Layout

```
.
├── docker-compose.yml        # graph-node stack (parametrised via .env)
├── config.toml               # graph-node config (chain name + single rpc-lb provider)
├── auth.py                   # forwardAuth: validates X-Api-Key / Bearer / Basic
├── rpc-lb/
│   └── default.conf.template # nginx round-robin over RPC_1/2/3 (envsubst at start)
├── traefik/
│   ├── docker-compose.yml     # Traefik v3.6 on the external `proxy` network
│   ├── traefik.yml            # static config: entrypoints + cloudflare resolver
│   └── dynamic/
│       └── middlewares.yml    # graph-apikey forwardAuth -> graph-auth:8080
├── .env.example
└── traefik/.env.example
```

## Prerequisites

- Docker + Docker Compose v2
- A Cloudflare-managed domain (for DNS-01 certs and the public hostnames)
- 3 RPC endpoints for your target chain (e.g. Chainstack, Alchemy, Infura)
- Disk: budget for the chain's block/eth_call cache. graph-node's `chain1` schema
  grows with the indexed range — busy chains reach hundreds of GB over time. On a
  small disk, enable pruning (see below).

## Deploy

```bash
# 1. Shared external network for Traefik <-> services
docker network create proxy

# 2. Traefik
cd traefik
cp .env.example .env            # set CF_EDIT_ZONE (Cloudflare Zone:DNS:Edit token)
touch acme.json && chmod 600 acme.json
docker compose up -d
cd ..

# 3. graph-node stack
cp .env.example .env            # set POSTGRES_PASSWORD, RPC_1/2/3, API_KEYS,
                                # GRAPH_HOST_*, and PG_* if not on a ~7.6 GB box
docker compose up -d

# 4. DNS: point graph / graph-admin / graph-status / graph-ipfs (your GRAPH_HOST_*)
#    A-records at this server's IP, proxied through Cloudflare.
```

Traefik issues one Let's Encrypt certificate per host via DNS-01 (no inbound
:80 challenge needed). First issuance takes ~30–60s per host.

### Deploy a subgraph

Via the admin endpoint (over the tunnel or `graph-admin.<domain>` with an API key):

```bash
graph create <name>  --node http://localhost:8020         # or https://graph-admin.<domain>
graph deploy <name>  --node http://localhost:8020 --ipfs http://localhost:5001
```

### Query

```
https://graph.<domain>/subgraphs/id/<DEPLOYMENT_ID>
  -H "X-Api-Key: <one of API_KEYS>"
```

`graph-status.<domain>` is public (read-only index status). `graph-admin` and the
IPFS add endpoint require an API key.

## Using a different chain

The stack works with any EVM network; only two things are chain-specific.

1. **RPCs** — set `RPC_1/2/3` in `.env` to endpoints for your chain. `rpc-lb`
   round-robins across them; graph-node only sees `http://rpc-lb:8545`.

2. **Chain name in `config.toml`** — change the `[chains.<name>]` section. This
   name is **not arbitrary**: it must match the `network:` field in the manifest
   (`subgraph.yaml`) of the subgraph you deploy. graph-node uses it to map the
   subgraph to its RPC provider.

   ```toml
   [chains.matic]          # Polygon. Rename to your chain, e.g. [chains.mainnet]
   shard = "primary"
   provider = [ { label = "rpc-lb", url = "http://rpc-lb:8545", features = [] } ]
   ```

   Common names: `mainnet` (Ethereum), `matic` (Polygon), `bsc`, `arbitrum-one`,
   `base`, `optimism`. Use whatever your subgraph's manifest declares.

   > Note: graph-node interpolates `${VAR}` into TOML *values* (the connection
   > string, provider URLs) but not into section *headers*, so the chain name
   > can't be an env var — edit it directly, or template `config.toml` with
   > `envsubst` if you need it env-driven.

Nothing else changes: Postgres, IPFS, the rpc-lb pattern, auth and Traefik are
chain-agnostic.

## Restoring an existing subgraph (optional)

To start already-synced from a logical copy (dump without the `chain1` cache +
the IPFS repo), instead of syncing from genesis:

```bash
# 1. start only postgres (fresh volume initialises the role with POSTGRES_PASSWORD)
docker compose up -d postgres

# 2. restore the dump (everything except chain1 cache)
docker cp graph-node.dump graph-postgres:/tmp/dump
docker exec graph-postgres pg_restore -U graph-node -d graph-node --no-owner -j2 /tmp/dump

# 3. load the IPFS repo into a clean volume BEFORE starting ipfs
docker volume create graph-node_graph_ipfs
docker run --rm -v graph-node_graph_ipfs:/d -v "$PWD":/in alpine \
  sh -c 'tar xzf /in/ipfs-data.tar.gz -C /d && rm -f /d/repo.lock /d/api'

# 4. start the rest; graph-node resumes and refills chain1 from RPC
docker compose up -d
```

graph-node indexing is deterministic: two indexers restored from the same dump
converge to identical entities.

## Notes

- **Cloudflare token IP filtering.** If `CF_EDIT_ZONE` is IP-restricted, it must
  allow this server's egress IP, and Docker must not enable IPv6 on the bridge
  (containers egress via the host IPv4). A token bound to another IP fails with
  error `9109`.
- **File descriptors.** `graph-node` and `postgres` set `nofile` to 65536 to avoid
  "Too many open files" under load.
- **Block-cache pruning.** When `chain1` grows large, truncate it (replace `matic`
  with your chain name):
  ```bash
  docker exec graph-node graphman --config /etc/graph-node/config.toml chain truncate matic
  ```
- **Monitoring.** Pair with a status dashboard reading `:8030` (index status) and
  `:8040` (Prometheus metrics).

## Security

Real secrets live only in the uncommitted `.env` files (`.gitignore`d), along with
`traefik/acme.json`. Rotate `POSTGRES_PASSWORD`, RPC tokens and `API_KEYS` if they
are ever exposed.
