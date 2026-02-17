#!/bin/bash
#
# Wait for job 22327762 to complete, then run training data generation
# Run this directly on login node - it only monitors and submits
#

JOB_ID="22327762"
WAIT_MINUTES=60
CHECK_INTERVAL=60 # Check every 60 seconds

echo "=========================================="
echo "Automated Training Data Generation"
echo "=========================================="
echo "Job to monitor: $JOB_ID"
echo "Initial wait: $WAIT_MINUTES minutes"
echo "Check interval: $((CHECK_INTERVAL / 60)) minutes"
echo ""
echo "Running on: $(hostname)"
echo "Started at: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Initial wait
echo "Waiting $WAIT_MINUTES minutes before first check..."
sleep $((WAIT_MINUTES * 60))

# Poll until job completes
while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Checking job status..."

    # Check if job exists in queue (redirect errors to /dev/null and check exit code)
    if squeue -j "$JOB_ID" -h -o "%T" &>/dev/null; then
        JOB_STATUS=$(squeue -j "$JOB_ID" -h -o "%T" 2>/dev/null)
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Job $JOB_ID status: $JOB_STATUS - waiting..."
        sleep $CHECK_INTERVAL
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✓ Job $JOB_ID has completed!"
        break
    fi
done

echo ""
echo "=========================================="
echo "Job completed - starting training data generation"
echo "=========================================="
echo ""

# Source the es.sh script and run the command
cd /ix1/ishi/elastic
es -generate-training-data 6

echo ""
echo "=========================================="
echo "Training data generation submitted"
echo "=========================================="
echo "Completed at: $(date '+%Y-%m-%d %H:%M:%S')"

