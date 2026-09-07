// Package config loads the Agent Platform process configuration.
package config

import (
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"
)

const (
	defaultListenAddress = ":8890"
	defaultAgentTimeout  = 30 * time.Second
	maxAgentTimeout      = 30 * time.Second
	defaultAgentModelURL = "https://api.openai.com/v1"
)

// Config contains process wiring and the optional OpenAI-compatible model
// settings used to construct the public Eino Agent. It still has no database,
// vector-store, or source-crawler settings.
type Config struct {
	ListenAddress     string
	CoreRPCAPIKey     string
	CoreDataBaseURL   string
	CoreDataAPIKey    string
	AgentModelAPIKey  string
	AgentModelBaseURL string
	AgentModelName    string
	// CapabilityTimeout is the single deadline shared by the Kitex handler and
	// the private Core HTTP client.
	CapabilityTimeout   time.Duration
	CoreDataHTTPTimeout time.Duration // kept as a compatibility alias
}

// Load reads required environment variables and rejects a partially configured
// service before it accepts Core requests.
func Load() (Config, error) {
	baseURL := strings.TrimRight(strings.TrimSpace(os.Getenv("AGENT_PLATFORM_CORE_DATA_URL")), "/")
	if baseURL == "" {
		return Config{}, fmt.Errorf("AGENT_PLATFORM_CORE_DATA_URL is required")
	}
	parsedURL, err := url.ParseRequestURI(baseURL)
	if err != nil || parsedURL.Scheme == "" || parsedURL.Host == "" {
		return Config{}, fmt.Errorf("AGENT_PLATFORM_CORE_DATA_URL must be an absolute HTTP URL")
	}
	if parsedURL.Scheme != "http" && parsedURL.Scheme != "https" {
		return Config{}, fmt.Errorf("AGENT_PLATFORM_CORE_DATA_URL must use http or https")
	}
	if parsedURL.RawQuery != "" || parsedURL.Fragment != "" {
		return Config{}, fmt.Errorf("AGENT_PLATFORM_CORE_DATA_URL must not include a query or fragment")
	}

	apiKey := strings.TrimSpace(os.Getenv("AGENT_PLATFORM_CORE_DATA_KEY"))
	if apiKey == "" {
		return Config{}, fmt.Errorf("AGENT_PLATFORM_CORE_DATA_KEY is required")
	}
	coreRPCAPIKey := strings.TrimSpace(os.Getenv("AGENT_PLATFORM_CORE_RPC_KEY"))
	if coreRPCAPIKey == "" {
		return Config{}, fmt.Errorf("AGENT_PLATFORM_CORE_RPC_KEY is required")
	}
	timeout := defaultAgentTimeout
	if raw := strings.TrimSpace(os.Getenv("AGENT_PLATFORM_TIMEOUT_MS")); raw != "" {
		var milliseconds int
		if _, err := fmt.Sscanf(raw, "%d", &milliseconds); err != nil || milliseconds <= 0 {
			return Config{}, fmt.Errorf("AGENT_PLATFORM_TIMEOUT_MS must be a positive integer")
		}
		timeout = time.Duration(milliseconds) * time.Millisecond
	}
	if timeout > maxAgentTimeout {
		return Config{}, fmt.Errorf("AGENT_PLATFORM_TIMEOUT_MS must not exceed %dms", maxAgentTimeout/time.Millisecond)
	}

	agentModelBaseURL := strings.TrimRight(strings.TrimSpace(os.Getenv("AGENT_PLATFORM_LLM_BASE_URL")), "/")
	if agentModelBaseURL == "" {
		agentModelBaseURL = defaultAgentModelURL
	}
	agentModelName := strings.TrimSpace(os.Getenv("AGENT_PLATFORM_LLM_MODEL"))
	agentModelAPIKey := strings.TrimSpace(os.Getenv("AGENT_PLATFORM_LLM_API_KEY"))

	listenAddress := strings.TrimSpace(os.Getenv("AGENT_PLATFORM_LISTEN_ADDR"))
	if listenAddress == "" {
		listenAddress = defaultListenAddress
	}

	return Config{
		ListenAddress:       listenAddress,
		CoreRPCAPIKey:       coreRPCAPIKey,
		CoreDataBaseURL:     baseURL,
		CoreDataAPIKey:      apiKey,
		AgentModelAPIKey:    agentModelAPIKey,
		AgentModelBaseURL:   agentModelBaseURL,
		AgentModelName:      agentModelName,
		CapabilityTimeout:   timeout,
		CoreDataHTTPTimeout: timeout,
	}, nil
}
