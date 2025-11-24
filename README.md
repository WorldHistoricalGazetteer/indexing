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

Create an alias by using `vi` to add to `~/.bash_profile`:

```bash
alias es='/ix1/whcdh/elastic/scripts/es.sh'
```

> Subsequently, update to latest:
>
>```bash
>cd /ix1/whcdh/elastic
>git pull origin main
>```

## Elasticsearch Installation (Bare-Metal)

1. Download tarball (as user)
2. Extract and rename:

```
cd /ix1/whcdh
tar xf elasticsearch-8.11.1-linux-x86_64.tar.gz
mv elasticsearch-8.11.1 es-bin
```

3. Create data/log/repo dirs:

```
mkdir -p /ix1/whcdh/es/{data,logs,repo,config}
```

4. Copy config from repository to runtime location:

```
cp /ix1/whcdh/elastic/config/elasticsearch.yml /ix1/whcdh/es/config/elasticsearch.yml
```

### Kibana

```
cd /ix1/whcdh
tar xf kibana-8.11.1-linux-x86_64.tar.gz
mv kibana-8.11.1 kibana-bin
mkdir -p /ix1/whcdh/kibana/{data,logs}
```

---

## Elasticsearch Configuration

All configuration is under:

```
/ix1/whcdh/elastic/config/elasticsearch.yml
```

This file is passed directly to Elasticsearch via the wrapper script.

You **must not** put copies in `$HOME` or anywhere else. The repo copy is authoritative.

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

SSH tunnelling example:

```
ssh -L 5601:localhost:5601 gazetteer@<host>
```

---

## Notes

* No systemd, podman, or sudo is used.
* Everything is isolated to `/ix1/whcdh`.
* Elasticsearch 8.11.x must **not** be configured with outdated `xpack.searchable.snapshot.cache.*` settings.
* The wrapper script cleanly manages startup order and PIDs.