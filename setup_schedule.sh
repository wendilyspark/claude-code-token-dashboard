#!/bin/bash
# Setup nightly launchd job to regenerate the Claude Code token dashboard at 21:00
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.claude.token-dashboard.plist"
PYTHON="$(which python3)"
LOG="$SCRIPT_DIR/cron.log"

echo "Installing launchd job..."
echo "  Script: $SCRIPT_DIR/generate_dashboard.py"
echo "  Python: $PYTHON"
echo "  Log:    $LOG"
echo "  Schedule: every day at 21:00"

# Unload existing if present
if launchctl list | grep -q "com.claude.token-dashboard" 2>/dev/null; then
  launchctl unload "$PLIST" 2>/dev/null || true
  echo "  (Unloaded existing job)"
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.token-dashboard</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPT_DIR}/generate_dashboard.py</string>
        <string>--days</string>
        <string>7</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>21</integer>
        <key>Minute</key><integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl load "$PLIST"

echo ""
echo "✅ Scheduler installed successfully!"
echo "   The dashboard will auto-regenerate every day at 21:00."
echo "   Dashboard: $SCRIPT_DIR/dashboard.html"
echo ""
echo "   To uninstall:  launchctl unload \"$PLIST\" && rm \"$PLIST\""
echo "   To run now:    python3 \"$SCRIPT_DIR/generate_dashboard.py\""
echo "   View logs:     tail -f \"$LOG\""
