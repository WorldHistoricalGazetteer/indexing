#!/bin/bash
#SBATCH -J detadmin
#SBATCH --account=ishi
#SBATCH -c 4
#SBATCH --mem=16G
#SBATCH -t 00:20:00
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/detadmin.log
cd /vast/ishi/gb1900/probe/font
PY=/vast/ishi/envs/boundary/bin/python
$PY detect_admin.py --lon -4.06082 --lat 52.39991 --tag gb_4270_2870
$PY detect_admin.py --lon -2.08328 --lat 53.54112 --tag gb_4318_2824
$PY detect_admin.py --lon 1.29501  --lat 52.62556 --tag gb_4400_2861
$PY detect_admin.py --lon -2.53647 --lat 51.46000 --tag gb_4307_2907
echo DETADMINDONE
