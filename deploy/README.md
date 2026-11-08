# Deploy on a Vultr VM

One VM runs everything: Postgres, the API (which also serves the screens in `web/`), the worker and Caddy for
HTTPS. Models run on Vultr Serverless Inference with the key of the same Vultr account. Nothing here buys
anything: the VM and the inference calls are paid from the account's credit.

## 1. The VM

- Ubuntu 24.04, 2 vCPU / 4 GB is enough. Note the public IPv4, for example `203.0.113.7`.
- Firewall: allow 22 (your address only), 80 and 443.
- Install Docker Engine with the Compose plugin (docs.docker.com/engine/install/ubuntu).

## 2. The code

```bash
git clone https://github.com/NadyKor15/coachcover coachcover && cd coachcover
cp .env.example .env
```

Fill `.env` (it stays on the VM, never in git):

- `POSTGRES_PASSWORD` — a long random string: `openssl rand -hex 24`;
- `SITE_ADDRESS` — the domain Caddy gets a certificate for. Without a domain of your own use sslip.io, which
  maps the IP into a name: `203-0-113-7.sslip.io` (dashes instead of dots);
- `COACHCOVER_MODEL_MODE=live` for real model calls, `replay` for the recorded answers;
- `COACHCOVER_JUDGE_RUNS_PER_HOUR=6`.

`VULTR_INFERENCE_API_KEY` is never written to `.env`. It comes from your secret store into the environment of
the command that starts the stack.

## 3. Start

Put the key in the environment from your secret store, for example with Infisical:

```bash
infisical run -- docker compose --profile edge up -d --build
```

Without a secret store on the VM, start with the key typed into the shell for this one command (it is not stored):

```bash
read -rs VULTR_INFERENCE_API_KEY && export VULTR_INFERENCE_API_KEY
docker compose --profile edge up -d --build && unset VULTR_INFERENCE_API_KEY
```

Docker keeps the value in the container's environment while it runs (visible to `docker inspect` on the VM).
On a fresh database the API loads the fictional training school by itself.

## 4. Check

```bash
curl -s https://203-0-113-7.sslip.io/health        # {"ok": true, "db": "ok", "mode": "live"} or "replay"
curl -s https://203-0-113-7.sslip.io/api/today | head -c 300
docker compose logs --tail=20 worker               # one "tick" line every 5 s
```

Open `https://203-0-113-7.sslip.io/` for the morning screen, `/offer?id=O-…` for the phone, `/case?id=C-1109-01`
for the case page.

Live smoke test of the models (one call each, the answer must name the model asked for; a fraction of a cent):

```bash
infisical run -- scripts/smoke_live.sh
```

## 5. Everyday

- Judge mode: the buttons on the morning screen call `POST /api/judge/run`; each run resets the training school.
- Update: `git pull && infisical run … -- docker compose --profile edge up -d --build`.
- Stop: `docker compose --profile edge down` (data stays in the `db-data` volume); `down -v` also deletes it.
- Logs: `docker compose logs -f api worker`.
