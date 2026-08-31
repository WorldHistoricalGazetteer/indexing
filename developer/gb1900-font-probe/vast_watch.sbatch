#!/bin/bash
#SBATCH -M htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-l
#SBATCH -c 1
#SBATCH --mem=2G
#SBATCH -t 3-00:00:00
#SBATCH -J vastwatch
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/vastwatch.log
# The valve on /vast for the duration of the full-series spot.
#
# /vast/ishi is a 1TB quota shared with PRODUCTION Elasticsearch, and it stood at 83% before this sweep
# started writing ~30G of box records into it. Checks every 10 minutes; above 90% it tars the oldest
# finished regions to /ix1 until back under 86%, and indexes them so a restarted shard does not re-spot
# them. See vast_sweep.py for why the order (write -> verify -> delete -> index) is not negotiable.
#
# Slurm, not a login-node cron: tarring gigabytes is real I/O and login nodes are not for work, however
# briefly. 1 core and 2G for 3 days is cheaper than the outage a full /vast causes.
set -u
cd /vast/ishi/gb1900/probe/font
/vast/ishi/envs/mapreader/bin/python vast_sweep.py --watch 600
