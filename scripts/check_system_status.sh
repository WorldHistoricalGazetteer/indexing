#!/usr/bin/env bash
#
# Check system status on login nodes
#

echo "=== System Load Check ==="
echo ""

for node in login0 login1 login3; do
    echo "--- $node ---"
    ssh $node "uptime; free -h | head -2; df -h /ihome /ix1 | tail -2" 2>/dev/null || echo "Cannot reach $node"
    echo ""
done

echo "=== Your Running Jobs ==="
squeue -u stg135 -M gpu,htc -o '%.10i %.9P %.20j %.8u %.2t %.10M %.6D %R'

echo ""
echo "=== ES Status ==="
if [[ -f /ix1/ishi/esinfo/es-staging.env ]]; then
    source /ix1/ishi/esinfo/es-staging.env
    curl -s "http://$ES_NODE:$ES_PORT/_cat/health?h=status,node.total,active_shards_percent" || echo "ES not responding"
fi

