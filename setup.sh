#!/usr/bin/env bash
set -euo pipefail

# Quick setup helper for OpenAI Realtime / Deepgram / Local Hybrid baselines.
# - Copies a golden config to config/ai-agent.yaml
# - Ensures .env exists (won't overwrite)
# - Finds free AudioSocket/ExternalMedia ports and writes them into config/ai-agent.yaml
# - Optionally runs ./install.sh, agent quickstart, agent doctor
# - Optionally starts the engine (python main.py) and prints URLs to hit
#
# Usage: ./setup.sh [openai|deepgram|local] [--pipeline=<name>] [--run-install] [--run-quickstart] [--run-doctor] [--run-engine]

# Auto-activate local virtualenv if present
if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  export PATH="$(pwd)/.venv/bin:${PATH}"
fi

BASELINE="${1:-openai}"
RUN_INSTALL=false
RUN_QUICKSTART=false
RUN_DOCTOR=false
RUN_ENGINE=false
PIPELINE_OVERRIDE=""

for arg in "$@"; do
  case "$arg" in
    --run-install) RUN_INSTALL=true ;;
    --run-quickstart) RUN_QUICKSTART=true ;;
    --run-doctor) RUN_DOCTOR=true ;;
    --run-engine) RUN_ENGINE=true ;;
    --pipeline=*) PIPELINE_OVERRIDE="${arg#--pipeline=}" ;;
    openai|deepgram|local) BASELINE="$arg" ;;
  esac
done

bold() { printf "\033[1m%s\033[0m\n" "$*"; }

port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn | awk '{print $4}' | grep -q ":${port}\$"
  else
    netstat -lnt 2>/dev/null | awk '{print $4}' | grep -q ":${port}\$"
  fi
}

find_free_port() {
  local start="$1" end="$2" p
  for ((p=start; p<=end; p++)); do
    if ! port_in_use "$p"; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

copy_baseline() {
  local src=""
  case "$BASELINE" in
    openai)  src="config/ai-agent.golden-openai.yaml" ;;
    deepgram) src="config/ai-agent.golden-deepgram.yaml" ;;
    local) src="config/ai-agent.golden-local-hybrid.yaml" ;;
    *) echo "Unknown baseline '$BASELINE' (use openai|deepgram|local)" >&2; exit 1 ;;
  esac

  if [[ ! -f "$src" ]]; then
    echo "Baseline file not found: $src" >&2
    exit 1
  fi

  cp "$src" config/ai-agent.yaml
  bold "Config reset from $src -> config/ai-agent.yaml"
}

ensure_env() {
  if [[ -f ".env" ]]; then
    bold ".env exists; not overwriting. Update keys inside."
    return
  fi

  if [[ -f ".env.example" ]]; then
    cp .env.example .env
    bold "Created .env from .env.example. Fill in your keys."
  else
    cat > .env <<'EOF'
# Required
OPENAI_API_KEY=
DEEPGRAM_API_KEY=
ASTERISK_ARI_USERNAME=asterisk
ASTERISK_ARI_PASSWORD=asterisk
ASTERISK_ARI_HOST=127.0.0.1
ASTERISK_ARI_PORT=8088
# Optional email
RESEND_API_KEY=
SMTP_HOST=
SMTP_PORT=
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM_EMAIL=
# Optional Anthropic Claude
CLAUDE_API_KEY=
# Optional local server overrides
LOCAL_WS_URL=ws://127.0.0.1:8765
LOCAL_WS_CHUNK_MS=320
LOCAL_WS_CONNECT_TIMEOUT=2.0
LOCAL_WS_RESPONSE_TIMEOUT=10.0
EOF
    bold "Created .env with placeholders. Fill in your keys."
  fi
}

check_ports() {
  local audiosocket_default=8090
  local audiosocket_free
  audiosocket_free=$(find_free_port "$audiosocket_default" 8100 || true)
  export AUDIO_PORT_SELECTED="$audiosocket_default"
  if [[ -n "$audiosocket_free" && "$audiosocket_free" != "$audiosocket_default" ]]; then
    bold "AudioSocket port $audiosocket_default is busy; nearest free: $audiosocket_free"
    echo "Update config.audiosocket.port in config/ai-agent.yaml if you want to use $audiosocket_free."
    AUDIO_PORT_SELECTED="$audiosocket_free"
  else
    bold "AudioSocket port $audiosocket_default is available."
  fi

  local rtp_free
  rtp_free=$(find_free_port 18080 18099 || true)
  export RTP_PORT_SELECTED="18080"
  if [[ -n "$rtp_free" && "$rtp_free" != "18080" ]]; then
    bold "ExternalMedia RTP start port 18080 busy; nearest free in 18080-18099: $rtp_free"
    echo "Update external_media.rtp_port / port_range in config/ai-agent.yaml if using pipelines."
    RTP_PORT_SELECTED="$rtp_free"
  else
    bold "ExternalMedia RTP base port 18080 looks available."
  fi
}

apply_port_overrides() {
  local cfg="config/ai-agent.yaml"
  if [[ ! -f "$cfg" ]]; then
    echo "Config not found at $cfg; cannot patch ports" >&2
    return
  fi
  python - <<'PY' "$cfg" "$AUDIO_PORT_SELECTED" "$RTP_PORT_SELECTED"
import sys, yaml
cfg_path, as_port, rtp_port = sys.argv[1:]
try:
    as_port = int(as_port)
    rtp_port = int(rtp_port)
except Exception:
    sys.exit(0)
data = yaml.safe_load(open(cfg_path)) or {}
audiosocket = data.get("audiosocket") or {}
audiosocket["port"] = as_port
data["audiosocket"] = audiosocket
external = data.get("external_media") or {}
external["rtp_port"] = rtp_port
external["port_range"] = f"{rtp_port}:{rtp_port+19}"
data["external_media"] = external
with open(cfg_path, "w") as f:
    yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
print(f"Patched {cfg_path}: audiosocket.port={as_port}, external_media.rtp_port={rtp_port}")
PY
}

apply_pipeline_override() {
  if [[ -z "$PIPELINE_OVERRIDE" ]]; then
    return
  fi
  local cfg="config/ai-agent.yaml"
  if [[ ! -f "$cfg" ]]; then
    echo "Config not found at $cfg; cannot set active_pipeline" >&2
    return
  fi
  python - <<'PY' "$cfg" "$PIPELINE_OVERRIDE"
import sys, yaml
cfg_path, pipeline = sys.argv[1], sys.argv[2]
data = yaml.safe_load(open(cfg_path)) or {}
data["active_pipeline"] = pipeline
with open(cfg_path, "w") as f:
    yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
print(f"Set active_pipeline={pipeline} in {cfg_path}")
PY
  bold "Active pipeline set to ${PIPELINE_OVERRIDE}"
}

maybe_run_install() {
  if ! $RUN_INSTALL; then
    bold "Skipping ./install.sh (pass --run-install to execute)."
    return
  fi
  bold "Running ./install.sh ..."
  ./install.sh
}

maybe_run_quickstart() {
  if ! $RUN_QUICKSTART; then
    bold "Skipping agent quickstart (pass --run-quickstart to execute)."
    return
  fi
  if ! command -v agent >/dev/null 2>&1; then
    echo "agent CLI not found on PATH. Install it during ./install.sh or per cli/README.md." >&2
    return
  fi
  bold "Running agent quickstart ..."
  agent quickstart
}

maybe_run_doctor() {
  if ! $RUN_DOCTOR; then
    bold "Skipping agent doctor (pass --run-doctor to execute)."
    return
  fi
  if ! command -v agent >/dev/null 2>&1; then
    echo "agent CLI not found on PATH. Install it during ./install.sh or per cli/README.md." >&2
    return
  fi
  bold "Running agent doctor ..."
  agent doctor
}

maybe_run_engine() {
  if ! $RUN_ENGINE; then
    bold "Skipping engine start (pass --run-engine to execute)."
    return
  fi
  if [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
  else
    PY="python"
  fi
  bold "Starting engine with $PY main.py (background)..."
  mkdir -p logs
  nohup "$PY" main.py > logs/engine.log 2>&1 &
  ENGINE_PID=$!
  bold "Engine started (pid $ENGINE_PID). Logs: logs/engine.log"
  echo "Health endpoint: http://127.0.0.1:15000/health"
  echo "Metrics (Prometheus): http://127.0.0.1:15000/metrics"
}

print_dialplan_hint() {
  cat <<'EOF'
Dialplan hint (switch per call):
  [from-ai-agent]
  exten => s,1,NoOp(AI Voice Agent)
   same => n,Set(AI_CONTEXT=demo_openai)         ; or demo_deepgram, demo_hybrid
   same => n,Set(AI_PROVIDER=openai_realtime)    ; or deepgram, local_hybrid
   same => n,Stasis(asterisk-ai-voice-agent)
   same => n,Hangup()
EOF
}

main() {
  bold "=== Asterisk AI Voice Agent setup ==="
  bold "Selected baseline: $BASELINE"
  copy_baseline
  ensure_env
  check_ports
  apply_port_overrides
  apply_pipeline_override
  maybe_run_install
  maybe_run_quickstart
  maybe_run_doctor
  maybe_run_engine
  print_dialplan_hint
  bold "Next: fill .env keys, adjust ports if needed, then run agent doctor."
}

main "$@"
