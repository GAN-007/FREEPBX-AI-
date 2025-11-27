# AI Voice Agent - Monitoring Stack

Production monitoring infrastructure using Prometheus and Grafana.

## Overview

- **Prometheus**: Metrics collection and alerting (port 9090)
- **Grafana**: Visualization dashboards (port 3000)
- **Metrics Source**: ai_engine health endpoint (port 15000)

## Quick Start

### 1. Deploy Monitoring Stack

```bash
# From project root
docker-compose -f docker-compose.monitoring.yml up -d

# Check status
docker ps | grep -E "prometheus|grafana"
```

### 2. Access Dashboards

**Grafana**: http://localhost:3000
- Default credentials: `admin` / `admin` (change on first login)
- Dashboards auto-loaded in "AI Voice Agent" folder

**Prometheus**: http://localhost:9090
- Query metrics directly
- View active alerts
- Check targets status

### 3. Verify Metrics Collection

```bash
# Check Prometheus is scraping ai_engine
curl http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.labels.job=="ai-engine")'

# Test metrics endpoint directly
curl http://localhost:15000/metrics | head -20
```

## Architecture

```
ai_engine:15000/metrics
    ↓ scrape every 1s
Prometheus:9090 (30d retention)
    ↓ PromQL queries
Grafana:3000 (dashboards)
```

## Metrics Collected

### Call Quality (50+ metrics)
- Turn response latency (p50/p95/p99)
- STT→TTS processing time
- Barge-in reaction time
- Audio underflows and fallbacks
- Jitter buffer depth

### Audio Quality
- RMS levels (pre/post companding)
- DC offset
- Codec alignment
- Sample rate verification

### Provider Performance
- Deepgram: ACK latency, sample rates
- OpenAI Realtime: rate alignment, measured vs expected
- Connection health

### System Health
- Active calls
- AudioSocket connections
- Memory/CPU usage
- Container health

## Alert Rules

### Critical Alerts
- **CriticalTurnResponseLatency**: p95 > 5s (immediate action)
- **NoAudioSocketConnections**: No active connections for 1min
- **HealthEndpointDown**: Cannot scrape metrics

### Warning Alerts
- **HighTurnResponseLatency**: p95 > 2s
- **HighUnderflowRate**: > 5 underflows/sec
- **CodecMismatch**: Provider alignment issues
- **SlowBargeInReaction**: p95 > 1s

See `alerts/ai-engine.yml` for complete list and thresholds.

## Dashboards

### 1. System Overview
- Active calls
- Call rate (calls/min)
- Provider distribution
- Health status

### 2. Call Quality
- Latency histograms
- Underflow rates
- Streaming performance
- Quality score

### 3. Provider Performance
- Deepgram metrics
- OpenAI Realtime metrics
- Side-by-side comparison

### 4. Audio Quality
- RMS levels
- Codec alignment
- Bytes tx/rx
- VAD performance

### 5. Conversation Flow
- State transitions
- Gating events
- Barge-in activity
- Config exposure

## Configuration

### Prometheus

**File**: `prometheus.yml`

```yaml
global:
  scrape_interval: 1s        # High-resolution for call quality
  evaluation_interval: 5s    # Alert evaluation frequency

rule_files:
  - 'alerts/*.yml'

scrape_configs:
  - job_name: 'ai-engine'
    scrape_interval: 1s
    static_configs:
      - targets: ['127.0.0.1:15000']
```

### Data Retention

**Default**: 30 days local storage

To change retention:
```yaml
# In docker-compose.monitoring.yml
command:
  - '--storage.tsdb.retention.time=90d'  # Keep 90 days
```

### Alert Destinations

To enable Alertmanager:

1. Uncomment in `prometheus.yml`:
```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: ['localhost:9093']
```

2. Deploy Alertmanager container (not included by default)

## Troubleshooting

### Prometheus Not Scraping

```bash
# Check target status
curl http://localhost:9090/api/v1/targets

# Check ai_engine health endpoint
curl http://localhost:15000/health

# Check Prometheus logs
docker logs prometheus
```

### Grafana Dashboards Not Loading

```bash
# Check provisioning logs
docker logs grafana | grep -i provision

# Verify dashboard files exist
ls -lh monitoring/grafana/dashboards/

# Check file permissions
chmod 644 monitoring/grafana/dashboards/*.json
```

### No Metrics Showing

```bash
# Verify metrics are being scraped
curl 'http://localhost:9090/api/v1/query?query=up{job="ai-engine"}'

# Check for recent data
curl 'http://localhost:9090/api/v1/query?query=ai_agent_streaming_active'
```

## Querying Metrics

### PromQL Examples

**Active calls**:
```promql
count(ai_agent_streaming_active == 1)
```

**Turn response latency (p95, last 5min)**:
```promql
histogram_quantile(0.95, rate(ai_agent_turn_response_seconds_bucket[5m]))
```

**Underflow rate (per second)**:
```promql
rate(ai_agent_stream_underflow_events_total[1m])
```

**Provider comparison**:
```promql
histogram_quantile(0.95, sum by (provider, le) (rate(ai_agent_turn_response_seconds_bucket[5m])))
```

## Maintenance

### Backup Dashboards

```bash
# Export all dashboards
for db in monitoring/grafana/dashboards/*.json; do
  cp "$db" "backups/$(basename $db).$(date +%Y%m%d)"
done
```

### Clean Old Data

```bash
# Prometheus data is in Docker volume
docker volume inspect monitoring_prometheus_data

# To reset (WARNING: deletes all metrics):
docker-compose -f docker-compose.monitoring.yml down -v
```

### Update Grafana Password

```bash
# Set in environment
export GRAFANA_ADMIN_PASSWORD="your-secure-password"

# Restart Grafana
docker-compose -f docker-compose.monitoring.yml restart grafana
```

## Integration with RCA

Use Prometheus metrics in post-call analysis:

```bash
# After running agent troubleshoot, query metrics for that call_id
CALL_ID="1761424308.2043"

curl "http://localhost:9090/api/v1/query?query=ai_agent_turn_response_seconds_bucket{call_id=\"$CALL_ID\"}"
```

## Performance Baselines

Based on golden baseline validation (Oct 25-26, 2025):

| Metric | Target | Golden Baseline |
|--------|--------|-----------------|
| Turn response p95 | < 1.5s | 0.8-1.2s |
| STT→TTS p95 | < 1.0s | 0.4-0.7s |
| Barge-in reaction p95 | < 0.5s | 0.2-0.4s |
| Underflow rate | < 2/call | 0/call |
| Quality score | > 85 | 90-95 |
| Codec alignment | 100% | 100% |

## Next Steps

1. **Deploy to Production**: Run docker-compose.monitoring.yml on server
2. **Validate Collection**: Verify metrics appear in Prometheus
3. **Test Alerts**: Trigger test alerts (e.g., stop ai_engine briefly)
4. **Make Test Calls**: Generate real metrics with 10-20 calls
5. **Tune Thresholds**: Adjust alert rules based on actual performance
6. **Add Dashboards**: Create custom dashboards for specific needs

## Reference

- **Prometheus Docs**: https://prometheus.io/docs/
- **Grafana Docs**: https://grafana.com/docs/
- **PromQL Guide**: https://prometheus.io/docs/prometheus/latest/querying/basics/
- **Alert Rules**: https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/
