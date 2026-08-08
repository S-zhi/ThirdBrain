package main

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/S-zhi/ThirdBrain/agent-platform/internal/capability"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/workflow"
	agentplatform "github.com/S-zhi/ThirdBrain/agent-platform/kitex_gen/agentplatform"
)

func TestDiscoverListsAPIDocRetrieval(t *testing.T) {
	handler := NewAgentPlatformServiceImpl(workflow.NewKnowledgeAssistWorkflow(handlerRetrievalTool{}), handlerRetrievalTool{})
	descriptors, err := handler.Discover(context.Background(), true)
	if err != nil {
		t.Fatal(err)
	}
	if len(descriptors) != 1 || descriptors[0].CapabilityId != capability.APIDocRetrievalV1ID {
		t.Fatalf("descriptors = %+v", descriptors)
	}
}

func TestInvokeAPIDocRetrievalReturnsTraceAndResult(t *testing.T) {
	handler := NewAgentPlatformServiceImpl(workflow.NewKnowledgeAssistWorkflow(handlerRetrievalTool{}), handlerRetrievalTool{})
	payload, _ := json.Marshal(map[string]any{"query": "Barrier", "wiki_id": "wiki:test", "namespace": "AscendC.API", "version": "v1"})
	traceID := "trace-fixed"
	response, err := handler.Invoke(context.Background(), &agentplatform.CapabilityRequest{CapabilityId: capability.APIDocRetrievalV1ID, PayloadJson: string(payload), TraceId: &traceID})
	if err != nil {
		t.Fatal(err)
	}
	if response.Status != "success" || response.TraceId != traceID || response.GetResultJson() == "" {
		t.Fatalf("response = %+v", response)
	}
}

func TestInvokeAPIDocRetrievalRejectsInvalidPayload(t *testing.T) {
	handler := NewAgentPlatformServiceImpl(workflow.NewKnowledgeAssistWorkflow(handlerRetrievalTool{}), handlerRetrievalTool{})
	response, err := handler.Invoke(context.Background(), &agentplatform.CapabilityRequest{CapabilityId: capability.APIDocRetrievalV1ID, PayloadJson: `{}`})
	if err != nil {
		t.Fatal(err)
	}
	if response.GetError().GetCode() != "INVALID_REQUEST" {
		t.Fatalf("error = %+v", response.GetError())
	}
}
