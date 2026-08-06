package config

import "testing"

func TestLoadRequiresBothGatewayCredentials(t *testing.T) {
	t.Setenv("AGENT_PLATFORM_CORE_DATA_URL", "http://127.0.0.1:8000")
	t.Setenv("AGENT_PLATFORM_CORE_DATA_KEY", "data-secret")
	t.Setenv("AGENT_PLATFORM_CORE_RPC_KEY", "")

	if _, err := Load(); err == nil {
		t.Fatal("expected missing Core RPC key to be rejected")
	}

	t.Setenv("AGENT_PLATFORM_CORE_RPC_KEY", "rpc-secret")
	config, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if config.CoreRPCAPIKey != "rpc-secret" || config.CoreDataAPIKey != "data-secret" {
		t.Fatal("gateway credentials were not loaded independently")
	}
}

func TestLoadRejectsCoreDataURLWithQuery(t *testing.T) {
	t.Setenv("AGENT_PLATFORM_CORE_DATA_URL", "http://127.0.0.1:8000?unexpected=value")
	t.Setenv("AGENT_PLATFORM_CORE_DATA_KEY", "data-secret")
	t.Setenv("AGENT_PLATFORM_CORE_RPC_KEY", "rpc-secret")

	if _, err := Load(); err == nil {
		t.Fatal("expected Core data URL query to be rejected")
	}
}
