# Deploy checklist — Phase 1 production gates

Phase 1 (Billing Core Foundation) code is committed; tests pass (60) and the
migration rehearsal ran on a fresh copy. But two Phase-1 success-criteria
items (#5) are operator-gated and **cannot be verified from the dev machine**
(no Docker / no VPS) — this checklist closes them. It also records how to
stop relying on the old in-process scheduler once the systemd timers are live.

Run these on the **VPS**, in a maintenance window, with a recent backup.

> DB file lives inside the `shop_data` docker volume at `/app/data/shop.db`.
> `jobs.py` resolves the target via `SHOP_DB_PATH` (or `database.DB_PATH`),
> and `migrate-money` refuses the configured DB unless `--verify-only`,
> `--dry-run`, or `MIGRATE_MONEY_ALLOW_PRODUCTION=1` is set (jobs.py:46-53).

---

## 1. Backup

```bash
ssh user@vps
cd /opt/vpn-shop
docker compose -f deploy/docker-compose.yml exec -T shop python -m jobs --help   # sanity: container healthy
# Snapshot the whole volume (or the DB file):
docker run --rm -v vpn-shop_shop_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/shop-data-$(date +%F-%H%M).tgz -C /data .
```

Verify the backup exists and is non-zero:

```bash
ls -lh shop-data-*.tgz
```

---

## 2. Regenerate the production lockfile (Docker / Python 3.12-slim)

`requirements.lock` was generated on the dev Python 3.14 and is marked
provisional. The `deploy/Dockerfile` uses `python:3.12-slim`, so the lockfile
must be regenerated there:

```bash
cd /opt/vpn-shop

# Build an ephemeral container pinned to the runtime image and freeze.
docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
  sh -c "pip install --no-cache-dir -r requirements.txt && pip freeze > requirements.lock"
```

Then confirm it is no longer provisional and head lines look correct:

```bash
head -3 requirements.lock          # NOTE line must be gone
grep -c '==' requirements.lock     # should be >= direct deps count
```

Rebuild the app image so it installs from the fresh lockfile:

```bash
docker compose -f deploy/docker-compose.yml build shop
docker compose -f deploy/docker-compose.yml up -d shop
```

---

## 3. Rehearse the money migration on a copy (always before the real run)

On the same VPS, take a throwaway copy of the DB and run `--verify-only`:

```bash
cd /opt/vpn-shop

docker run --rm -v vpn-shop_shop_data:/data -v "$PWD":/out alpine \
  sh -c "cp /data/shop.db /out/shop-copy.db && cp /data/shop.db-wal /out/shop-copy.db-wal 2>/dev/null; true"

docker compose -f deploy/docker-compose.yml exec -T shop \
  sh -c "SHOP_DB_PATH=/app/../__none__ MIGRATE_MONEY_ALLOW_PRODUCTION=0 python -m jobs migrate-money --verify-only"
```

> The `--verify-only` path is read-only on the configured DB (rolls back DDL
> on the live path) and reports per-column `max_delta`/`sum_delta`. Confirm
> every money column reports `max_delta<=1` and `sum_delta=0` before the real
> run, and that the timestamp rows are listed (to be normalized).

If you prefer to rehearse directly on a copy file, copy the DB out and pass a
real path instead of the env override:

```bash
docker compose -f deploy/docker-compose.yml exec -T shop \
  sh -c "SHOP_DB_PATH=/app/data/shop-copy.db python -m jobs migrate-money --verify-only"
```

---

## 4. Run the real migration (maintenance window)

Choose one of these two authorized forms:

**A. Explicit copy path (recommended for rehearsal of a staged copy):**
```bash
docker compose -f deploy/docker-compose.yml exec -T shop \
  sh -c "SHOP_DB_PATH=/app/data/shop-copy.db python -m jobs migrate-money"
```

**B. Directly on the configured DB — only with explicit authorization:**
```bash
docker compose -f deploy/docker-compose.yml exec -T shop \
  sh -c "MIGRATE_MONEY_ALLOW_PRODUCTION=1 python -m jobs migrate-money"
```

Then verify the columns became INTEGER (whole rubles) and the container is
healthy:

```bash
docker compose -f deploy/docker-compose.yml exec -T shop \
  sh -c "python -c \"import sqlite3;c=sqlite3.connect('/app/data/shop.db');print(c.execute('select type,pragma_table_info(plans)').fetchall() if False else [r for r in c.execute('pragma table_info(plans)')])\""
```

Expected: `price_rub` shows `INTEGER`. Then restart the app to pick up the
normalized schema:

```bash
docker compose -f deploy/docker-compose.yml up -d shop
```

---

## 5. Enable systemd timers & retire the in-process scheduler

The old in-process expiry watcher was removed from `app.py` (plan 01-06).
Enable the timers that run `python -m jobs expiry|reconcile` inside the
container (`ExecStartPre` ensures the shop container is up):

```bash
sudo cp deploy/systemd/vpnshop-expiry.service deploy/systemd/vpnshop-expiry.timer \
       deploy/systemd/vpnshop-reconcile.service deploy/systemd/vpnshop-reconcile.timer \
       /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vpnshop-expiry.timer vpnshop-reconcile.timer
```

Confirm a completed run and its persistent `job_runs` record:

```bash
systemctl list-timers | grep vpnshop
journalctl -u vpnshop-expiry.service -n 20
docker compose -f deploy/docker-compose.yml exec -T shop \
  sh -c "python -c \"import sqlite3;c=sqlite3.connect('/app/data/shop.db');print(list(c.execute('select job_name,ok,error from job_runs order by finished_at desc limit 5')))\""
```

- [ ] Backup taken and verified
- [ ] `requirements.lock` regenerated in `python:3.12-slim`, NOTE line removed
- [ ] App image rebuilt from the fresh lockfile
- [ ] `migrate-money --verify-only` rehearsal green (max_delta<=1, sum_delta=0)
- [ ] Real `migrate-money` run completed on VPS in a maintenance window
- [ ] Money columns verified as INTEGER after migration
- [ ] `vpnshop-expiry.timer` / `vpnshop-reconcile.timer` enabled and active
- [ ] A completed `expiry`/`reconcile` run recorded in `job_runs`
