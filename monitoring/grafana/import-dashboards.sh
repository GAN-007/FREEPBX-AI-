#!/bin/bash
# Import Grafana dashboards via API
# Usage: ./import-dashboards.sh [grafana_url] [admin_password]

set -e

GRAFANA_URL="${1:-http://localhost:3000}"
ADMIN_USER="${2:-admin}"
ADMIN_PASS="${3:-admin2025}"
DASHBOARD_DIR="$(dirname "$0")/dashboards"
FOLDER_NAME="AI Voice Agent"

echo "🔧 Importing Grafana dashboards to $GRAFANA_URL"
echo ""

# Get or create folder
echo "📁 Creating/getting folder: $FOLDER_NAME"
FOLDER_RESPONSE=$(curl -s -X POST "$GRAFANA_URL/api/folders" \
  -H "Content-Type: application/json" \
  -u "$ADMIN_USER:$ADMIN_PASS" \
  -d "{\"title\":\"$FOLDER_NAME\"}" 2>/dev/null || echo '{"uid":""}')

FOLDER_UID=$(echo "$FOLDER_RESPONSE" | grep -o '"uid":"[^"]*"' | cut -d'"' -f4)

if [ -z "$FOLDER_UID" ]; then
  echo "❌ Failed to create/get folder"
  exit 1
fi

echo "✅ Folder UID: $FOLDER_UID"
echo ""

# Import each dashboard
for dashboard_file in "$DASHBOARD_DIR"/*.json; do
  if [ ! -f "$dashboard_file" ]; then
    continue
  fi
  
  filename=$(basename "$dashboard_file")
  echo "📊 Importing $filename..."
  
  # Read dashboard JSON and wrap it in import format
  dashboard_json=$(cat "$dashboard_file")
  import_json=$(jq -n \
    --arg folder_uid "$FOLDER_UID" \
    --argjson dashboard "$dashboard_json" \
    '{
      dashboard: $dashboard,
      folderUid: $folder_uid,
      overwrite: true
    }')
  
  # Import dashboard
  response=$(curl -s -X POST "$GRAFANA_URL/api/dashboards/db" \
    -H "Content-Type: application/json" \
    -u "$ADMIN_USER:$ADMIN_PASS" \
    -d "$import_json")
  
  # Check result
  if echo "$response" | grep -q '"status":"success"'; then
    dashboard_uid=$(echo "$response" | grep -o '"uid":"[^"]*"' | head -1 | cut -d'"' -f4)
    dashboard_url="$GRAFANA_URL/d/$dashboard_uid"
    echo "  ✅ Imported successfully"
    echo "  🔗 URL: $dashboard_url"
  else
    echo "  ❌ Failed to import"
    echo "  Response: $response"
  fi
  echo ""
done

echo "🎉 Dashboard import complete!"
echo ""
echo "Access dashboards at:"
echo "  $GRAFANA_URL/dashboards/f/$FOLDER_UID"
