package main

import (
	"context"
	"fmt"
	"net"
	"os"

	agentservice "github.com/S-zhi/ThirdBrain/agent-platform/internal/agent"
	coreapidoc "github.com/S-zhi/ThirdBrain/agent-platform/internal/apidoc"
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
		cfg.CapabilityTimeout,
	)
	var apiDocAgent *agentservice.Service
	if cfg.AgentModelAPIKey != "" && cfg.AgentModelName != "" {
		chatModel, modelErr := agentservice.NewOpenAIModel(context.Background(), agentservice.OpenAIModelConfig{
			APIKey:  cfg.AgentModelAPIKey,
			BaseURL: cfg.AgentModelBaseURL,
			Model:   cfg.AgentModelName,
			Timeout: cfg.CapabilityTimeout,
		})
		if modelErr != nil {
			fmt.Fprintln(os.Stderr, "agent-platform LLM configuration error:", modelErr)
			os.Exit(1)
		}
		apiDocAgent, modelErr = agentservice.NewAPIDocumentService(context.Background(), chatModel, coreapidoc.New(dataClient))
		if modelErr != nil {
			fmt.Fprintln(os.Stderr, "agent-platform Agent initialization error:", modelErr)
			os.Exit(1)
		}
	}
	handler := NewAgentPlatformServiceImplWithAgent(
		workflow.NewKnowledgeAssistWorkflow(dataClient),
		cfg.CapabilityTimeout,
		apiDocAgent,
	)
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
