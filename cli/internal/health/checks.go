package health

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

func (c *Checker) checkDocker() Check {
	// Check if docker command exists
	if _, err := exec.LookPath("docker"); err != nil {
		return Check{
			Name:        "Docker",
			Status:      StatusFail,
			Message:     "Docker not found",
			Remediation: "Install Docker: https://docs.docker.com/get-docker/",
		}
	}
	
	// Check if docker daemon is running
	cmd := exec.Command("docker", "ps")
	if err := cmd.Run(); err != nil {
		return Check{
			Name:        "Docker",
			Status:      StatusFail,
			Message:     "Docker daemon not running",
			Remediation: "Start Docker daemon: sudo systemctl start docker",
		}
	}
	
	// Get Docker version
	cmd = exec.Command("docker", "version", "--format", "{{.Server.Version}}")
	output, _ := cmd.Output()
	version := strings.TrimSpace(string(output))
	
	return Check{
		Name:    "Docker",
		Status:  StatusPass,
		Message: fmt.Sprintf("Docker daemon running (v%s)", version),
	}
}

func (c *Checker) checkContainers() Check {
	// Check if ai_engine container is running (note: underscore not hyphen)
	cmd := exec.Command("docker", "ps", "--format", "{{.Names}}\t{{.Status}}", "--filter", "name=ai_engine")
	output, err := cmd.Output()
	if err != nil {
		return Check{
			Name:        "Containers",
			Status:      StatusFail,
			Message:     "Failed to check container status",
			Details:     err.Error(),
			Remediation: "Run: docker-compose ps",
		}
	}
	
	lines := strings.Split(strings.TrimSpace(string(output)), "\n")
	if len(lines) == 0 || lines[0] == "" {
		return Check{
			Name:        "Containers",
			Status:      StatusFail,
			Message:     "No AI containers running",
			Remediation: "Start services: docker-compose up -d",
		}
	}
	
	running := 0
	for _, line := range lines {
		if strings.Contains(line, "Up") {
			running++
		}
	}
	
	if running == 0 {
		return Check{
			Name:        "Containers",
			Status:      StatusFail,
			Message:     "AI containers not running",
			Remediation: "Start services: docker-compose up -d",
		}
	}
	
	return Check{
		Name:    "Containers",
		Status:  StatusPass,
		Message: fmt.Sprintf("%d container(s) running", running),
		Details: string(output),
	}
}

func (c *Checker) checkAsteriskARI() Check {
	// Get ARI credentials from environment
	ariHost := GetEnv("ASTERISK_HOST", c.envMap)
	ariUsername := GetEnv("ASTERISK_ARI_USERNAME", c.envMap)
	ariPassword := GetEnv("ASTERISK_ARI_PASSWORD", c.envMap)
	
	if ariHost == "" {
		ariHost = "127.0.0.1"  // Default
	}
	
	if ariUsername == "" || ariPassword == "" {
		return Check{
			Name:        "Asterisk ARI",
			Status:      StatusWarn,
			Message:     "ARI credentials not configured",
			Details:     "ASTERISK_ARI_USERNAME or ASTERISK_ARI_PASSWORD not set in .env",
			Remediation: "Set ASTERISK_ARI_USERNAME and ASTERISK_ARI_PASSWORD in .env file",
		}
	}
	
	// Try to connect to ARI HTTP endpoint
	cmd := exec.Command("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
		"-u", fmt.Sprintf("%s:%s", ariUsername, ariPassword),
		fmt.Sprintf("http://%s:8088/ari/asterisk/info", ariHost))
	
	output, err := cmd.Output()
	if err != nil {
		return Check{
			Name:        "Asterisk ARI",
			Status:      StatusWarn,
			Message:     "Cannot connect to ARI",
			Details:     fmt.Sprintf("Host: %s, error: %v", ariHost, err),
			Remediation: "Check if Asterisk is running and ARI is enabled",
		}
	}
	
	httpCode := strings.TrimSpace(string(output))
	if httpCode == "200" {
		return Check{
			Name:    "Asterisk ARI",
			Status:  StatusPass,
			Message: fmt.Sprintf("ARI accessible at %s:8088", ariHost),
		}
	}
	
	return Check{
		Name:    "Asterisk ARI",
		Status:  StatusWarn,
		Message: fmt.Sprintf("ARI returned HTTP %s", httpCode),
		Details: fmt.Sprintf("Expected 200, got %s from %s:8088", httpCode, ariHost),
	}
}

func (c *Checker) checkAudioSocket() Check {
	// Check if port 8090 is listening (typical AudioSocket port)
	cmd := exec.Command("sh", "-c", "netstat -tuln 2>/dev/null | grep :8090 || ss -tuln 2>/dev/null | grep :8090")
	if err := cmd.Run(); err != nil {
		return Check{
			Name:    "AudioSocket",
			Status:  StatusWarn,
			Message: "AudioSocket port 8090 not detected",
			Details: "This is normal when idle (no active calls)",
		}
	}
	
	return Check{
		Name:    "AudioSocket",
		Status:  StatusPass,
		Message: "AudioSocket port 8090 listening",
	}
}

func (c *Checker) checkConfiguration() Check {
	// Look for config file in common locations
	configPaths := []string{
		"config/ai-agent.yaml",
		"/app/config/ai-agent.yaml",
		"../config/ai-agent.yaml",
	}
	
	var configPath string
	for _, path := range configPaths {
		if _, err := os.Stat(path); err == nil {
			configPath = path
			break
		}
	}
	
	if configPath == "" {
		return Check{
			Name:        "Configuration",
			Status:      StatusFail,
			Message:     "config/ai-agent.yaml not found",
			Remediation: "Run: agent init",
		}
	}
	
	// Check if file is readable
	if _, err := os.ReadFile(configPath); err != nil {
		return Check{
			Name:        "Configuration",
			Status:      StatusFail,
			Message:     "Cannot read config file",
			Details:     err.Error(),
			Remediation: "Check file permissions",
		}
	}
	
	absPath, _ := filepath.Abs(configPath)
	return Check{
		Name:    "Configuration",
		Status:  StatusPass,
		Message: "Configuration file found",
		Details: absPath,
	}
}

func (c *Checker) checkProviderKeys() Check {
	// Check for common provider API keys in environment or .env file
	keys := map[string]string{
		"OPENAI_API_KEY":   "OpenAI",
		"DEEPGRAM_API_KEY": "Deepgram",
		"ANTHROPIC_API_KEY": "Anthropic",
	}
	
	found := []string{}
	missing := []string{}
	
	for env, name := range keys {
		// Check both OS env and .env file
		if val := GetEnv(env, c.envMap); val != "" {
			found = append(found, name)
		} else {
			missing = append(missing, name)
		}
	}
	
	if len(found) == 0 {
		return Check{
			Name:        "Provider Keys",
			Status:      StatusFail,
			Message:     "No provider API keys found",
			Remediation: "Set API keys in .env file",
		}
	}
	
	status := StatusPass
	if len(missing) > 0 {
		status = StatusInfo
	}
	
	return Check{
		Name:    "Provider Keys",
		Status:  status,
		Message: fmt.Sprintf("%d provider(s) configured", len(found)),
		Details: fmt.Sprintf("Found: %s", strings.Join(found, ", ")),
	}
}

func (c *Checker) checkAudioPipeline() Check {
	// Check if we can find recent audio pipeline logs (note: ai_engine with underscore)
	cmd := exec.Command("docker", "logs", "--tail", "100", "ai_engine")
	output, err := cmd.Output()
	
	if err != nil {
		return Check{
			Name:    "Audio Pipeline",
			Status:  StatusWarn,
			Message: "Cannot check audio pipeline logs",
			Details: err.Error(),
		}
	}
	
	logs := string(output)
	
	// Look for key indicators
	indicators := map[string]string{
		"StreamingPlaybackManager initialized": "Streaming manager active",
		"AudioSocket server listening":         "AudioSocket ready",
		"VAD":                                   "VAD configured",
	}
	
	found := []string{}
	for pattern, desc := range indicators {
		if strings.Contains(logs, pattern) {
			found = append(found, desc)
		}
	}
	
	if len(found) == 0 {
		return Check{
			Name:    "Audio Pipeline",
			Status:  StatusWarn,
			Message: "No audio pipeline indicators in logs",
			Details: "This may be normal if engine just started",
		}
	}
	
	return Check{
		Name:    "Audio Pipeline",
		Status:  StatusPass,
		Message: fmt.Sprintf("%d component(s) detected", len(found)),
		Details: strings.Join(found, ", "),
	}
}

func (c *Checker) checkNetwork() Check {
	// Check Docker network and ARI connectivity
	cmd := exec.Command("docker", "network", "ls", "--format", "{{.Name}}")
	output, err := cmd.Output()
	
	if err != nil {
		return Check{
			Name:    "Network",
			Status:  StatusWarn,
			Message: "Cannot check Docker networks",
			Details: err.Error(),
		}
	}
	
	networks := strings.Split(strings.TrimSpace(string(output)), "\n")
	
	// Check if using bridge, host, or custom network
	ariHost := GetEnv("ASTERISK_HOST", c.envMap)
	if ariHost == "" {
		ariHost = "127.0.0.1"
	}
	
	var networkMode string
	if ariHost == "127.0.0.1" || ariHost == "localhost" {
		networkMode = "host network (localhost)"
	} else if strings.Contains(ariHost, ".") {
		networkMode = fmt.Sprintf("remote host (%s)", ariHost)
	} else {
		networkMode = fmt.Sprintf("container name (%s)", ariHost)
	}
	
	return Check{
		Name:    "Network",
		Status:  StatusPass,
		Message: fmt.Sprintf("Using %s", networkMode),
		Details: fmt.Sprintf("Networks available: %d", len(networks)),
	}
}

func (c *Checker) checkMediaDirectory() Check {
	// Check common media directory locations
	dirs := []string{
		"/mnt/asterisk_media/ai-generated",
		"/var/spool/asterisk/monitor",
		"./media",
	}
	
	for _, dir := range dirs {
		if stat, err := os.Stat(dir); err == nil && stat.IsDir() {
			// Check if writable
			testFile := filepath.Join(dir, ".agent_test")
			if err := os.WriteFile(testFile, []byte("test"), 0644); err == nil {
				os.Remove(testFile)
				return Check{
					Name:    "Media Directory",
					Status:  StatusPass,
					Message: "Media directory accessible and writable",
					Details: dir,
				}
			}
		}
	}
	
	return Check{
		Name:    "Media Directory",
		Status:  StatusWarn,
		Message: "Media directory not found or not writable",
		Details: "Checked: " + strings.Join(dirs, ", "),
	}
}

func (c *Checker) checkLogs() Check {
	// Check for recent errors in ai_engine logs (note: underscore)
	cmd := exec.Command("docker", "logs", "--tail", "100", "ai_engine")
	output, err := cmd.Output()
	
	if err != nil {
		return Check{
			Name:    "Logs",
			Status:  StatusWarn,
			Message: "Cannot read container logs",
			Details: err.Error(),
		}
	}
	
	logs := string(output)
	
	// Count errors and warnings
	errorCount := strings.Count(strings.ToUpper(logs), "ERROR")
	warnCount := strings.Count(strings.ToUpper(logs), "WARN")
	
	if errorCount > 10 {
		return Check{
			Name:    "Logs",
			Status:  StatusFail,
			Message: fmt.Sprintf("%d errors in last 100 lines", errorCount),
			Details: "Check logs: docker logs ai-engine",
			Remediation: "Run: agent troubleshoot",
		}
	}
	
	if errorCount > 0 || warnCount > 5 {
		return Check{
			Name:    "Logs",
			Status:  StatusWarn,
			Message: fmt.Sprintf("%d errors, %d warnings in last 100 lines", errorCount, warnCount),
			Details: "May indicate recent issues",
		}
	}
	
	return Check{
		Name:    "Logs",
		Status:  StatusPass,
		Message: "No critical errors in recent logs",
	}
}

func (c *Checker) checkRecentCalls() Check {
	// Try to find recent call info from logs (note: ai_engine with underscore)
	cmd := exec.Command("docker", "logs", "--tail", "500", "ai_engine")
	output, err := cmd.Output()
	
	if err != nil {
		return Check{
			Name:    "Recent Calls",
			Status:  StatusInfo,
			Message: "Cannot check recent calls",
			Details: err.Error(),
		}
	}
	
	logs := string(output)
	
	// Look for call indicators
	callIndicators := []string{
		"call_id",
		"Stasis start",
		"Channel answered",
	}
	
	found := false
	for _, indicator := range callIndicators {
		if strings.Contains(logs, indicator) {
			found = true
			break
		}
	}
	
	if !found {
		return Check{
			Name:    "Recent Calls",
			Status:  StatusInfo,
			Message: "No recent calls detected in logs",
			Details: "This is normal if no calls have been placed recently",
		}
	}
	
	return Check{
		Name:    "Recent Calls",
		Status:  StatusInfo,
		Message: "Recent call activity detected",
		Details: "See logs for details",
	}
}
