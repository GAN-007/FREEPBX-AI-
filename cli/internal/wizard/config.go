package wizard

import (
	"bufio"
	"fmt"
	"os"
	"strings"

	"gopkg.in/yaml.v3"
)

// Config holds all configuration
type Config struct {
	// .env values
	AsteriskHost        string
	AsteriskUsername    string
	AsteriskPassword    string
	AudioTransport      string
	AudioSocketHost     string
	AudioSocketPort     string
	OpenAIKey           string
	DeepgramKey         string
	AnthropicKey        string
	
	// YAML values
	ActivePipeline      string
	DefaultProvider     string
	
	// File paths
	EnvPath             string
	YAMLPath            string
}

// LoadConfig reads current configuration from .env and YAML
func LoadConfig() (*Config, error) {
	// Try to find .env - check current dir and parent dir
	envPath := ".env"
	if _, err := os.Stat(envPath); os.IsNotExist(err) {
		envPath = "../.env"
		if _, err := os.Stat(envPath); os.IsNotExist(err) {
			envPath = ".env" // Reset to current for creation
		}
	}
	
	yamlPath := "config/ai-agent.yaml"
	if _, err := os.Stat(yamlPath); os.IsNotExist(err) {
		yamlPath = "../config/ai-agent.yaml"
	}
	
	cfg := &Config{
		EnvPath:  envPath,
		YAMLPath: yamlPath,
	}
	
	// Load .env
	if err := cfg.loadEnv(); err != nil {
		return nil, fmt.Errorf("failed to load .env: %w", err)
	}
	
	// Load YAML
	if err := cfg.loadYAML(); err != nil {
		// YAML might not exist yet, that's okay
		PrintWarning(fmt.Sprintf("Could not load %s: %v", cfg.YAMLPath, err))
	}
	
	return cfg, nil
}

// loadEnv reads .env file
func (c *Config) loadEnv() error {
	file, err := os.Open(c.EnvPath)
	if err != nil {
		if os.IsNotExist(err) {
			// .env doesn't exist, create from example if available
			if _, err := os.Stat(".env.example"); err == nil {
				return c.createEnvFromExample()
			}
			return fmt.Errorf(".env file not found")
		}
		return err
	}
	defer file.Close()
	
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		
		// Skip empty lines and comments
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		
		// Parse KEY=VALUE
		parts := strings.SplitN(line, "=", 2)
		if len(parts) != 2 {
			continue
		}
		
		key := strings.TrimSpace(parts[0])
		value := strings.TrimSpace(parts[1])
		
		// Remove quotes if present
		value = strings.Trim(value, "\"'")
		
		switch key {
		case "ASTERISK_HOST":
			c.AsteriskHost = value
		case "ASTERISK_ARI_USERNAME":
			c.AsteriskUsername = value
		case "ASTERISK_ARI_PASSWORD":
			c.AsteriskPassword = value
		case "AUDIO_TRANSPORT":
			c.AudioTransport = value
		case "AUDIOSOCKET_HOST":
			c.AudioSocketHost = value
		case "AUDIOSOCKET_PORT":
			c.AudioSocketPort = value
		case "OPENAI_API_KEY":
			c.OpenAIKey = value
		case "DEEPGRAM_API_KEY":
			c.DeepgramKey = value
		case "ANTHROPIC_API_KEY":
			c.AnthropicKey = value
		}
	}
	
	return scanner.Err()
}

// createEnvFromExample creates .env from .env.example
func (c *Config) createEnvFromExample() error {
	input, err := os.ReadFile(".env.example")
	if err != nil {
		return err
	}
	
	err = os.WriteFile(c.EnvPath, input, 0644)
	if err != nil {
		return err
	}
	
	PrintSuccess("Created .env from .env.example")
	return c.loadEnv()
}

// loadYAML reads config/ai-agent.yaml
func (c *Config) loadYAML() error {
	data, err := os.ReadFile(c.YAMLPath)
	if err != nil {
		return err
	}
	
	var yamlData map[string]interface{}
	if err := yaml.Unmarshal(data, &yamlData); err != nil {
		return err
	}
	
	// Extract active_pipeline
	if val, ok := yamlData["active_pipeline"].(string); ok {
		c.ActivePipeline = val
	}
	
	// Extract default_provider
	if val, ok := yamlData["default_provider"].(string); ok {
		c.DefaultProvider = val
	}
	
	return nil
}

// SaveEnv updates .env file in-place
func (c *Config) SaveEnv() error {
	// Read existing .env
	lines := []string{}
	
	file, err := os.Open(c.EnvPath)
	if err == nil {
		scanner := bufio.NewScanner(file)
		for scanner.Scan() {
			lines = append(lines, scanner.Text())
		}
		file.Close()
	}
	
	// Update values
	updates := map[string]string{
		"ASTERISK_HOST":         c.AsteriskHost,
		"ASTERISK_ARI_USERNAME": c.AsteriskUsername,
		"ASTERISK_ARI_PASSWORD": c.AsteriskPassword,
		"AUDIO_TRANSPORT":       c.AudioTransport,
		"AUDIOSOCKET_HOST":      c.AudioSocketHost,
		"AUDIOSOCKET_PORT":      c.AudioSocketPort,
		"OPENAI_API_KEY":        c.OpenAIKey,
		"DEEPGRAM_API_KEY":      c.DeepgramKey,
		"ANTHROPIC_API_KEY":     c.AnthropicKey,
	}
	
	// Apply updates
	for key, value := range updates {
		if value == "" {
			continue // Skip empty values
		}
		
		found := false
		for i, line := range lines {
			trimmed := strings.TrimSpace(line)
			if strings.HasPrefix(trimmed, key+"=") || strings.HasPrefix(trimmed, "#"+key+"=") {
				lines[i] = fmt.Sprintf("%s=%s", key, value)
				found = true
				break
			}
		}
		
		if !found {
			// Append new key
			lines = append(lines, fmt.Sprintf("%s=%s", key, value))
		}
	}
	
	// Write back
	content := strings.Join(lines, "\n") + "\n"
	return os.WriteFile(c.EnvPath, []byte(content), 0644)
}

// SaveYAML updates config/ai-agent.yaml
func (c *Config) SaveYAML(template string) error {
	// Try to find template in current and parent directory
	templatePath := template
	if _, err := os.Stat(templatePath); os.IsNotExist(err) {
		templatePath = "../" + template
	}
	
	// Copy template to config/ai-agent.yaml
	input, err := os.ReadFile(templatePath)
	if err != nil {
		return fmt.Errorf("failed to read template %s: %w", templatePath, err)
	}
	
	// Read as YAML to modify
	var yamlData map[string]interface{}
	if err := yaml.Unmarshal(input, &yamlData); err != nil {
		return err
	}
	
	// Update active_pipeline or default_provider
	if c.ActivePipeline != "" {
		yamlData["active_pipeline"] = c.ActivePipeline
	}
	if c.DefaultProvider != "" {
		yamlData["default_provider"] = c.DefaultProvider
	}
	
	// Write back
	output, err := yaml.Marshal(yamlData)
	if err != nil {
		return err
	}
	
	return os.WriteFile(c.YAMLPath, output, 0644)
}

// GetMaskedKey returns masked version of API key for display
func GetMaskedKey(key string) string {
	if key == "" {
		return "(not set)"
	}
	if len(key) < 8 {
		return "****"
	}
	return "**..." + key[len(key)-3:]
}
