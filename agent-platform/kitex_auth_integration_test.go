package main

import (
	"context"
	"net"
	"testing"
	"time"

	"github.com/S-zhi/ThirdBrain/agent-platform/internal/gatewayauth"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/workflow"
	"github.com/S-zhi/ThirdBrain/agent-platform/kitex_gen/agentplatform"
	"github.com/S-zhi/ThirdBrain/agent-platform/kitex_gen/agentplatform/agentplatformservice"
	"github.com/bytedance/gopkg/cloud/metainfo"
	kitexclient "github.com/cloudwego/kitex/client"
	"github.com/cloudwego/kitex/server"
	"github.com/cloudwego/kitex/transport"
)

func TestKitexTransportEnforcesCoreServiceMetainfo(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	handler := NewAgentPlatformServiceImpl(
		workflow.NewKnowledgeAssistWorkflow(handlerRetrievalTool{}),
	)
	rpcServer := agentplatformservice.NewServer(
		handler,
		server.WithListener(listener),
		server.WithExitWaitTime(time.Second),
		server.WithMiddleware(gatewayauth.CoreServiceAuth("core-secret")),
	)
	serverResult := make(chan error, 1)
	go func() {
		serverResult <- rpcServer.Run()
	}()
	t.Cleanup(func() {
		if stopErr := rpcServer.Stop(); stopErr != nil {
			t.Errorf("stop Kitex server: %v", stopErr)
		}
		select {
		case runErr := <-serverResult:
			if runErr != nil {
				t.Errorf("run Kitex server: %v", runErr)
			}
		case <-time.After(2 * time.Second):
			t.Error("Kitex server did not stop")
		}
	})

	rpcClient, err := agentplatformservice.NewClient(
		"thirdbrain.agent.platform",
		kitexclient.WithHostPorts(listener.Addr().String()),
		kitexclient.WithRPCTimeout(2*time.Second),
		kitexclient.WithTransportProtocol(transport.TTHeader),
	)
	if err != nil {
		t.Fatal(err)
	}
	request := &agentplatform.ExecuteKnowledgeAssistRequest{
		RequestId: "request-1",
		Query:     "Barrier",
		Scope: &agentplatform.RetrievalScope{
			WikiId: "wiki:test", Namespace: "AscendC.API", Version: "v1",
		},
	}

	authorizedContext := metainfo.WithValue(
		context.Background(),
		gatewayauth.CoreServiceKeyMeta,
		"core-secret",
	)
	response, err := rpcClient.ExecuteKnowledgeAssist(authorizedContext, request)
	if err != nil {
		t.Fatalf("authorized Kitex call failed: %v", err)
	}
	if response.Status != agentplatform.AssistStatus_COMPLETED {
		t.Fatalf("authorized status = %v", response.Status)
	}

	if _, err := rpcClient.ExecuteKnowledgeAssist(context.Background(), request); err == nil {
		t.Fatal("unauthorized Kitex call unexpectedly succeeded")
	}
}
