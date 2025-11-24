Below is a clean, copy-pasteable Markdown section you can drop directly into your README. It incorporates:

* Bare-metal installation of Elasticsearch and Kibana (no sudo, no podman)
* Use of `/ix1/whcdh/elastic` as the git-managed repo
* `git pull origin main` workflow
* Config and wrapper script locations (`config/elasticsearch.yml`, `scripts/es.sh`)
* Requirement to keep `scripts/es.sh` executable across pulls
* Removal of the old `$HOME/es` script

You can paste this verbatim.

---

# WHG Elasticsearch + Kibana (Bare-Metal Install)

This document describes how Elasticsearch and Kibana are deployed and run on the Pitt VM *without sudo or podman*, using only user-space binaries.
All configuration and management scripts live in the repository under:

```
/ix1/whcdh/elastic
```

This directory is a **git clone of the WHG elastic config repo**, and must be updated using:

```
git pull origin main
```

## Directory Layout

```
/ix1/whcdh/
    elastic/                 ← git-managed repo (config + scripts)
        config/
            elasticsearch.yml
        scripts/
            es.sh            ← wrapper script (must remain executable)
        README.md
    elasticsearch-bin/       ← unpacked ES binary (user-space)
/ix1/whcdh/kibana-bin/       ← unpacked Kibana binary (user-space)
```

## Repository Setup

Clone the repository (first-time setup):

```bash
mkdir -p /ix1/whcdh/elastic
cd /ix1/whcdh
git clone git@github.com:whg/elastic.git elastic
```

Update to latest:

```bash
cd /ix1/whcdh/elastic
git pull origin main
```

## Elasticsearch Installation (Bare-Metal)

Download Elasticsearch 8.x (already done historically) and unpack it into:

```
/ix1/whcdh/elasticsearch-bin
```

ES stores its *data* and *logs* outside the binary directory:

```
Data: /ix1/whcdh/es/data
Logs: /ix1/whcdh/es/logs
```

Make sure they exist:

```bash
mkdir -p /ix1/whcdh/es/data
mkdir -p /ix1/whcdh/es/logs
```

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

### Make it permanently executable

Because this file is under git, an update (`git pull`) may drop its executable bit.
Run this after every pull:

```bash
chmod +x /ix1/whcdh/elastic/scripts/es.sh
```

### Remove old copies in `$HOME`

If you previously had:

```
~/es
```

Remove it:

```bash
rm -f ~/es
```

The only valid entry point is:

```
/ix1/whcdh/elastic/scripts/es.sh
```

### Running Elasticsearch

```bash
/ix1/whcdh/elastic/scripts/es.sh start
```

Logs will appear under:

```
/ix1/whcdh/es/logs/whg.log
```

### Stopping Elasticsearch

```bash
/ix1/whcdh/elastic/scripts/es.sh stop
```

## Kibana Installation (Bare-Metal)

Kibana 8.x is unpacked into:

```
/ix1/whcdh/kibana-bin
```

Kibana data directory:

```
/ix1/whcdh/kibana/data
```

Ensure it exists:

```bash
mkdir -p /ix1/whcdh/kibana/data
```

## Starting Kibana

You start Kibana manually using:

```bash
nohup /ix1/whcdh/kibana-bin/bin/kibana \
  --path.data=/ix1/whcdh/kibana/data \
  > /ix1/whcdh/kibana/kibana.out 2>&1 &
```

Kibana’s logging is controlled via its `kibana.yml` (already configured).

## Updating the Whole System

Whenever configs or scripts change:

```bash
cd /ix1/whcdh/elastic
git pull origin main
chmod +x scripts/es.sh
```

Then restart Elasticsearch if necessary:

```bash
scripts/es.sh stop
scripts/es.sh start
```

Kibana does not require restart unless its config changes.

---

If you want, I can add a troubleshooting section, a sysadmin-facing summary, or convert the above into a minimal README file.
