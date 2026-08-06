package main

import (
	"fmt"
	"net"
	"os"

	"github.com/S-zhi/ThirdBrain/agent-platform/internal/config"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/coredata"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/gatewayauth"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/workflow"
	agentplatform "github.com/S-zhi/ThirdBrain/agent-platform/kitex_gen/agentplatform/agentplatformservice"
	"github.com/cloudwego/kitex/server"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		fmt.Fprintln(os.Stderr, "agent-platform configuration error:", err)
		os.Exit(1)
	}
	address, err := net.ResolveTCPAddr("tcp", cfg.ListenAddress)
	if err != nil {
		fmt.Fprintln(os.Stderr, "agent-platform listen address error:", err)
		os.Exit(1)
	}

	dataClient := coredata.NewClient(
		cfg.CoreDataBaseURL,
		cfg.CoreDataAPIKey,
		cfg.CoreDataHTTPTimeout,
	)
	handler := NewAgentPlatformServiceImpl(workflow.NewKnowledgeAssistWorkflow(dataClient))
	svr := agentplatform.NewServer(
		handler,
		server.WithServiceAddr(address),
		server.WithMiddleware(gatewayauth.CoreServiceAuth(cfg.CoreRPCAPIKey)),
	)

	if err := svr.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "agent-platform server error:", err)
		os.Exit(1)
	}
}
