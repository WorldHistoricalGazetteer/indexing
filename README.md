# WHG Elasticsearch + Kibana (Bare-Metal Install)

This document describes how Elasticsearch and Kibana are deployed and run on the Pitt VM *without sudo or podman*, using only user-space binaries.

---

## Repository Setup

Clone the repository (first-time setup):

```bash
mkdir -p /ix1/whcdh/elastic
cd /ix1/whcdh
git clone git@github.com:whg/elastic.git elastic
```

Set the wrapper script as permanently executable:

```bash
chmod +x /ix1/whcdh/elastic/scripts/es.sh
git add /ix1/whcdh/elastic/scripts/es.sh
git commit -m "Make wrapper script executable"
git push origin main
```

Create an alias by using `vi` to add to `~/.bashrc`:

```bash
alias es='/ix1/whcdh/elastic/scripts/es.sh'
```

And then update your current shell:

```bash
source ~/.bashrc
```

> Subsequently, update to latest:
>
>```bash
>cd /ix1/whcdh/elastic
>git pull origin main
>```

## Elasticsearch Installation (Bare-Metal)

```
cd /ix1/whcdh

# --- Download Elasticsearch 9.2.1 ---
curl -L -O https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-9.2.1-linux-x86_64.tar.gz

# --- Extract and Rename ---
tar xf elasticsearch-9.2.1-linux-x86_64.tar.gz
mv elasticsearch-9.2.1 es-bin
rm elasticsearch-9.2.1-linux-x86_64.tar.gz

# --- Create Data Directories ---
mkdir -p /ix1/whcdh/es/{data,logs,repo,config}
mkdir -p /ix1/whcdh/es-staging/{data,logs,repo,config}

```

### Kibana (Bare-Metal)

```
cd /ix1/whcdh

# --- Download Kibana 9.2.1 ---
curl -L -O https://artifacts.elastic.co/downloads/kibana/kibana-9.2.1-linux-x86_64.tar.gz

# --- Extract and Rename ---
tar xf kibana-9.2.1-linux-x86_64.tar.gz
mv kibana-9.2.1 kibana-bin
rm kibana-9.2.1-linux-x86_64.tar.gz

# --- Create Data Directories ---
mkdir -p /ix1/whcdh/kibana/{data,logs}

```

---

## Elasticsearch Configuration

All configuration is under:

```
/ix1/whcdh/elastic/config/elasticsearch.yml
```

This file is copied to the Elasticsearch config folder via the wrapper script.

The repo copy is authoritative. Do not edit the live copy directly as it will be overwritten on next start.

## Elasticsearch Wrapper Script (`scripts/es.sh`)

The wrapper script ensures:

* correct `ES_HOME`
* correct `path.data` and `path.logs`
* correct config path
* correct JVM options directory
* non-interactive startup
* automatically backgrounded operation

Location:

```
/ix1/whcdh/elastic/scripts/es.sh
```

---

### Start both:

```
~/es -start
```

### Stop both:

```
~/es -stop
```

### Restart:

```
~/es -restart
```

### Elasticsearch only:

```
~/es es-start
~/es es-stop
~/es es-restart
```

### Kibana only:

```
~/es kibana-start
~/es kibana-stop
~/es kibana-restart
```

---

## Basic Status Checks

### Elasticsearch
```
curl -X GET "localhost:9200/_cluster/health?pretty"
```
### Kibana
```
curl -X GET "localhost:5601/api/status" -H "kbn-xsrf: true"
```

---
## Create Indices, Snapshot Repositories, and Initial Staging Snapshots

> IMPORTANT: **Run this _once only_, as it will destroy existing indices!**
>
>```bash
>cd /ix1/whcdh/elastic
>python -m processing.create_indices
>```

---

## Log Locations

### Elasticsearch

```
/ix1/whcdh/es/logs/whg.log
/ix1/whcdh/es/logs/nohup.out
```

### Kibana

```
/ix1/whcdh/kibana/logs/kibana.log
```

Tail logs:

```
tail -f /ix1/whcdh/es/logs/whg.log
tail -f /ix1/whcdh/kibana/logs/kibana.log
```

---

## Access URLs

Local only unless tunneled:

| Service       | URL                                            |
| ------------- | ---------------------------------------------- |
| Elasticsearch | [http://localhost:9200](http://localhost:9200) |
| Kibana        | [http://localhost:5601](http://localhost:5601) |

SSH tunnelling example for Kibana **from local machine**:

```
ssh -o PubkeyAuthentication=no -L 5602:localhost:5601 stg135@gazetteer.crcd.pitt.edu
```

Then access Kibana at [http://localhost:5602](http://localhost:5602).

---

## Notes

* No systemd, podman, or sudo is used.
* Everything is isolated to `/ix1/whcdh`.
* Elasticsearch 8.11.x must **not** be configured with outdated `xpack.searchable.snapshot.cache.*` settings.
* The wrapper script cleanly manages startup order and PIDs.