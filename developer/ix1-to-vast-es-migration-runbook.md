# Runbook — Relocate prod Elasticsearch (+ gateway) off `/ix1` onto `/vast`

> Tracking issue: **WorldHistoricalGazetteer/place#118**.
> Motivation: the CRC `ix1.crc.pitt.edu` NFS mount is a hard mount and a recurring
> single point of failure. On **2026-07-15** an `ix` failover left the `gazetteer`
> VM's `/ix1` **client** mount wedged (server + `crc0` were fine); prod ES — whose
> binaries, config and `path.logs` live on `/ix1` — stalled and the public site went
> down intermittently even though `/vast` and the ES **data** were healthy. This
> runbook makes the ES **serving path** independent of `/ix1`.
>
> **Status: ✅ COMPLETE (2026-07-15).** Cutover executed and verified: ES runs from
> `/vast` (binary/config/keystore/logs, `es.sh`-managed, PID via `es -start`), all doc
> counts identical to baseline, gateway relocated to the `/vast` clone (`gateway_ctl.sh`,
> `/vast` password), `es`/`gw` aliases + `@reboot` autostart + gaz_relay all on `/vast`,
> `/vast` clone unshallowed to deep primary (`REPO_DIR=/vast`), secrets (keystore, ES
> passwords, `WHG_API_TOKEN`) copied to `/vast`, and the `/ix1` code clone retired to
> `/ix1/ishi/elastic.retired-20260715` (reversible). `/ix1` now holds only data,
> snapshots, secret originals + `kibana/data` — all off the serving path.
> **Remaining (optional/soak):** end-to-end reboot test before `rm`-ing the `.retired`
> clone; move Kibana `path.data` to `/vast` (step 8); fix `es.sh -health`'s gateway/Kibana
> PID-file checks (cosmetic false "STOPPED" now that `gateway_ctl.sh` manages the gateway).

## The data is already on `/vast` — this is a RUNTIME relocation, not a data move
`path.data=/vast/ishi/es/data` is unchanged. We only move binaries, config and logs.

## Already staged by the prep pass (safe, live ES untouched)
| Item | Location | Notes |
|------|----------|-------|
| ES 9.0.0 binaries | `/vast/ishi/es-bin-vast/elasticsearch-9.0.0/` | Exact build `112859b85d50de2a7e63f73c8fc70b99eea24291` (matches live). SHA512 verified. |
| Config dir | `/vast/ishi/es-config-vast/` | `elasticsearch.yml` + `jvm.options` (gc.log repointed to `/vast`) + `jvm.options.d/heap.options` (`-Xms28g -Xmx28g`). **Missing by design:** `elasticsearch.keystore` — CUTOVER-TODO. |
| Logs dir | `/vast/ishi/es-logs-vast/` | `2775` (group `ishi`, setgid) so `gazetteer` can write. |
| Perms | new dirs `g+rX`, logs `g+w` | so `gazetteer` (group `ishi`) can run stg135-owned binaries/config. |

## CUTOVER-TODO — copy from `/ix1` during the stable window (do NOT reconstruct)
1. `elasticsearch.keystore` (seed / secure settings) — `cp /ix1/ishi/es-bin/config/elasticsearch.keystore /vast/ishi/es-config-vast/`
2. `cp /ix1/ishi/es/config/elastic.password /vast/ishi/es/config/` and `cp /ix1/ishi/es/config/kibana_system.password /vast/ishi/es/config/`
   (VALUES unchanged — the `elastic` creds live in the `.security` index inside the
   unchanged `path.data`, so **no password reset**. The gateway/Kibana just need the file.)
3. Re-diff `/vast/ishi/es-config-vast/elasticsearch.yml` vs live `/ix1/ishi/es-bin/config/elasticsearch.yml`
   (only 2 active lines seen 2026-07-15 — both preserved) in case an operator added settings.

## ⚠️ SAFETY — ONE ES per data path
The OLD `/ix1`-launched ES and this NEW `/vast` ES both open `/vast/ishi/es/data`.
Two writers = index corruption. **The OLD ES MUST be fully stopped (process gone,
`node.lock` released, nothing listening on 9201) BEFORE starting the new one.** Also
disable the `@reboot` autostart before cutover so a reboot mid-window cannot double-start.

## Pre-migration baseline (verify identical after cutover — same data dir)
```
boundaries                            877120
places_postbarrier-20260502t130000z   342949723
toponyms_postbarrier-20260502t130000z 68319931
wdgn_20240316                         13616287
whg_2025_11_12                        2134062
pub_v2                                38767
types_20260404_150351                58996
cluster_name=whg-production  node.name=whg-prod-1  version=9.0.0
```

---

# Cutover (maintenance window; run as `gazetteer` unless noted)

### 0. Pre-checks
```bash
# /ix1 must be responsive from the VM:
timeout 8 stat -f /ix1/ishi && echo IX1_OK        # must NOT hang
# capture live baseline:
curl -s -u elastic:$(cat /ix1/ishi/es/config/elastic.password) \
  "http://localhost:9201/_cat/indices?v&h=index,docs.count&s=index"
```

### 1. Disable autostart, then STOP the old ES
```bash
# comment out the @reboot line so a reboot cannot double-start onto the data dir:
crontab -l | sed 's#^@reboot /home/gazetteer/bin/boot_start_bootstrap.sh#\# &#' | crontab -
# stop ES (via es.sh / gaz_relay). Kibana + gateway too if using full -stop:
es es-stop            # or: gaz_relay es es-stop
# VERIFY it is really gone:
pgrep -af org.elasticsearch.bootstrap || echo ES_DEAD
ss -ltnp | grep :9201 || echo PORT_9201_FREE
ls /vast/ishi/es/data/node.lock 2>/dev/null; fuser /vast/ishi/es/data/node.lock 2>/dev/null || echo LOCK_FREE
```

### 2. Copy the CUTOVER-TODO files from `/ix1`
```bash
cp /ix1/ishi/es-bin/config/elasticsearch.keystore /vast/ishi/es-config-vast/
mkdir -p /vast/ishi/es/config
cp /ix1/ishi/es/config/elastic.password        /vast/ishi/es/config/
cp /ix1/ishi/es/config/kibana_system.password  /vast/ishi/es/config/
chmod 640 /vast/ishi/es-config-vast/elasticsearch.keystore
```

### 3. Start the NEW `/vast` ES (as `gazetteer`)
```bash
ES_PATH_CONF=/vast/ishi/es-config-vast ES_TMPDIR=/tmp \
nohup /vast/ishi/es-bin-vast/elasticsearch-9.0.0/bin/elasticsearch \
  > /vast/ishi/es-logs-vast/nohup.out 2>&1 &
echo $! > /vast/ishi/es-logs-vast/es.pid
```
(Heap is set via `jvm.options.d/heap.options`; no `ES_JAVA_OPTS` needed. `network.host`,
`http.port`, security, discovery, paths all come from `elasticsearch.yml` — no `-E` flags.)

### 4. Verify NEW ES (must match baseline exactly — same data)
```bash
for i in $(seq 1 60); do curl -s -u elastic:$(cat /vast/ishi/es/config/elastic.password) \
  http://localhost:9201/_cluster/health && break; sleep 5; done
curl -s -u elastic:$(cat /vast/ishi/es/config/elastic.password) \
  "http://localhost:9201/_cat/indices?v&h=index,docs.count&s=index"
curl -s -u elastic:$(cat /vast/ishi/es/config/elastic.password) http://localhost:9201/ | grep number
# Expect: status green/yellow, doc counts == baseline, version 9.0.0, node whg-prod-1.
# Confirm logs now land on /vast: ls -la /vast/ishi/es-logs-vast/
```

### 5. Relocate the gateway off `/ix1`
The gateway reads the ES password from `{IX1_BASE}/es/config/elastic.password`
(`gateway/config.py:31`; `IX1_BASE` is env-overridable, `config.py:27`). It does **not**
import `processing.settings`, so the `STAGING_INFO_FILE` gotcha (below) does not apply to it.
- Run it from the `/vast` clone `/vast/ishi/elastic` (already current) instead of the `/ix1` clone.
- Point its password source at `/vast`: set `IX1_BASE=/vast/ishi` in the gateway's
  `.env` (its only `/ix1` use is the password file) **or** add `ELASTIC_PASS_FILE` as an
  env override. Then restart the gateway (`es gateway-restart` / `gaz_relay gateway-restart`).
- Verify: `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:9200/`  → 401 (alive),
  and an authed `/_cluster/health` via 9200 returns green/yellow.

> **`STAGING_INFO_FILE` gotcha (for any `processing.*` tool run off `/vast`):**
> `processing/settings.py:35` does `os.path.exists(STAGING_INFO_FILE)` at **import**, and
> the default is `/ix1/ishi/esinfo/es-staging.env` — a stat that hangs if `/ix1` is wedged.
> Export `STAGING_INFO_FILE=/vast/ishi/_no_staging.env` for any such process.

### 6. Move the gaz_relay log off `/ix1`
The per-minute cron currently appends to `/ix1/ishi/elastic/logs/gaz_relay.log` — a hung
`/ix1` write here is what amplified the outage into a D-state pileup.
```bash
crontab -l | sed 's#/ix1/ishi/elastic/logs/gaz_relay.log#/vast/ishi/elastic/logs/gaz_relay.log#' | crontab -
mkdir -p /vast/ishi/elastic/logs
```

### 7. Re-point the `@reboot` autostart at the `/vast` runtime
Edit `scripts/boot_start_whg.sh` (and `/home/gazetteer/bin/boot_start_bootstrap.sh`) so the
autostart launches the `/vast` ES (steps 3) rather than the `/ix1` `es -start`, then
**re-enable** the `@reboot` line commented out in step 1. Confirm the wrapper points at
`/vast/ishi/es-bin-vast` + `/vast/ishi/es-config-vast`.

### 8. (Optional, lower priority) Kibana `path.data`
Move `/ix1/ishi/kibana/data` → `/vast` so Kibana also survives `/ix1` outages
(binaries already on `/vast`). Not on the public serving path.

### 9. Retire the `/ix1` code clone — single clone on `/vast` (ends git-sync to `/ix1`)
Goal: after the runtime is confirmed healthy on `/vast`, make `/vast/ishi/elastic` the
**only** working clone so we never again `git pull` into `/ix1` (a hung `/ix1` was what
blocked `gw restart`'s pull in the past). Do this only **after a soak period** (steps 3–7
verified stable). This retires **code** only — authority DATA, snapshots and secrets
originals deliberately stay on `/ix1` (see "What is intentionally LEFT on `/ix1`"); code
running from `/vast` still *references* them via `IX1_BASE`, but only off the serving path.

```bash
# 9a. Find everything still pointing at the /ix1 clone (alias, relay, cron, boot scripts):
grep -RIn '/ix1/ishi/elastic' /home/gazetteer/.bashrc /home/gazetteer/bin \
     /home/gazetteer/gaz_relay.sh 2>/dev/null; crontab -l | grep -n '/ix1/ishi/elastic'
```
- **`es` alias** → re-point at `/vast/ishi/elastic/scripts/es.sh` (edit the alias line, or
  re-run `es -install` from the `/vast` clone).
- **`gaz_relay`** → its `ES=`/`es.sh` path + log path → `/vast/ishi/elastic` (log move already
  done in step 6).
- **cron / Slurm / ingestion invocations** that `cd /ix1/ishi/elastic` → `/vast/ishi/elastic`.
- **`@reboot` wrapper** → already repointed in step 7; confirm it sources the `/vast` clone.
```bash
# 9b. Stop depending on the /ix1 clone. After the soak, park it (reversible) — do NOT rm yet:
mv /ix1/ishi/elastic /ix1/ishi/elastic.retired-$(date +%Y%m%d)
# Watch for anything that breaks; if something still needs it, mv it back. Remove for good
# only once a full cycle (restart, gateway-restart, an ingest, a reboot) has passed clean.
```
> **Reversible:** every 9a change is a one-line revert; 9b is a rename (`mv` back). Nothing
> is deleted until you're satisfied. `git` thereafter syncs to `/vast` only.

---

## Rollback (if the `/vast` ES fails to come up healthy)
```bash
# stop the /vast ES
kill $(cat /vast/ishi/es-logs-vast/es.pid); pgrep -af org.elasticsearch.bootstrap || echo GONE
# restart the original /ix1 ES (requires /ix1 healthy):
es es-start           # or gaz_relay es es-start
# re-enable the @reboot line if you disabled it.
```
Data is untouched by a failed start (ES fails fast on lock/version mismatch before writing),
so rollback is just "start the old one." The `/vast` staging dirs are additive — nothing to undo.

## What is intentionally LEFT on `/ix1` (per CLAUDE.md — do not move)
Authority source data `/ix1/ishi/data/*`, snapshots `/ix1/ishi/es/snapshots` (= `path.repo`,
declared but not on the serving path), secrets originals `/ix1/ishi/secrets/`,
`/ix1/ishi/es/config/`. After this migration an `/ix1` outage pauses *ingestion/snapshots*
but no longer takes *serving* down.

## Remaining blockers (as of prep, 2026-07-15)
- **(a) Stable `/ix1`:** needed ONLY to copy the keystore + 2 password files (CUTOVER-TODO)
  and to re-diff the yml. Everything else is `/vast`-resident.
- **(b) `gazetteer` privileges:** to stop/start ES + edit gazetteer's crontab. Available via
  `gaz_relay` (ES/Kibana/gateway restart) — but the crontab edits + the `/vast` launch in
  steps 1/3/6/7 need an interactive `gazetteer` shell or an extension of the `gaz_relay` allowlist.
- **Separately:** the *current* wedged pitt `/ix1` client mount still needs a **root/CRC
  remount** to end the flapping — that is the immediate-incident fix, independent of this migration.
