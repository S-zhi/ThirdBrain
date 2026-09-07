package main

import (
	"context"
	"encoding/json"
	"testing"

	agentservice "github.com/S-zhi/ThirdBrain/agent-platform/internal/agent"
	coreapidoc "github.com/S-zhi/ThirdBrain/agent-platform/internal/apidoc"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/capability"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/coredata"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/workflow"
	agentplatform "github.com/S-zhi/ThirdBrain/agent-platform/kitex_gen/agentplatform"
	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/schema"
)

type handlerAPIDocRetrievalTool struct{}

func (handlerAPIDocRetrievalTool) RetrieveContext(_ context.Context, _ coredata.RetrieveContextRequest) (coredata.RetrievalResult, error) {
	return coredata.RetrievalResult{Payload: json.RawMessage(`{"knowledge_hits":[{"title":"Barrier","summary":"verified Barrier usage","content":"full content","score":0.9,"provenance":[{"path":"api/barrier.md","namespace":"AscendC.API","version":"v1"}]}]}`)}, nil
}

type handlerAgentModel struct{}

func (handlerAgentModel) Generate(_ context.Context, input []*schema.Message, _ ...model.Option) (*schema.Message, error) {
	for _, message := range input {
		if message != nil && message.Role == schema.Tool {
			return schema.AssistantMessage("Barrier is available in the verified wiki.", nil), nil
		}
	}
	return schema.AssistantMessage("", []schema.ToolCall{{
		ID: "handler-tool-call",
		Function: schema.FunctionCall{
			Name:      "tool.api_doc.retrieve.v1",
			Arguments: `{"query":"Barrier","wiki_id":"spoofed","namespace":"spoofed","version":"spoofed"}`,
		},
	}}), nil
}

func (handlerAgentModel) Stream(ctx context.Context, input []*schema.Message, opts ...model.Option) (*schema.StreamReader[*schema.Message], error) {
	message, err := (handlerAgentModel{}).Generate(ctx, input, opts...)
	if err != nil {
		return nil, err
	}
	return schema.StreamReaderFromArray([]*schema.Message{message}), nil
}

func (handlerAgentModel) WithTools(_ []*schema.ToolInfo) (model.ToolCallingChatModel, error) {
	return handlerAgentModel{}, nil
}

func newAPIDocHandler(t *testing.T) *AgentPlatformServiceImpl {
	t.Helper()
	downstream := handlerAPIDocRetrievalTool{}
	apiDocAgent, err := agentservice.NewAPIDocumentService(
		context.Background(),
		handlerAgentModel{},
		coreapidoc.New(downstream),
	)
	if err != nil {
		t.Fatal(err)
	}
	return NewAgentPlatformServiceImplWithAgent(
		workflow.NewKnowledgeAssistWorkflow(downstream),
		capability.APIDocAgentV1().Timeout,
		apiDocAgent,
	)
}

func TestDiscoverListsAPIDocAgent(t *testing.T) {
	handler := newAPIDocHandler(t)
	descriptors, err := handler.Discover(context.Background(), true)
	if err != nil {
		t.Fatal(err)
	}
	if len(descriptors) != 1 || descriptors[0].CapabilityId != capability.APIDocAgentV1ID {
		t.Fatalf("descriptors = %+v", descriptors)
	}
}

func TestInvokeAPIDocAgentReturnsTraceResultAndCitation(t *testing.T) {
	handler := newAPIDocHandler(t)
	payload, _ := json.Marshal(map[string]any{
		"query": "Barrier", "wiki_id": "wiki:test", "namespace": "AscendC.API", "version": "v1",
	})
	traceID := "trace-fixed"
	response, err := handler.Invoke(context.Background(), &agentplatform.CapabilityRequest{CapabilityId: capability.APIDocAgentV1ID, PayloadJson: string(payload), TraceId: &traceID})
	if err != nil {
		t.Fatal(err)
	}
	if response.Status != "success" || response.TraceId != traceID || response.GetResultJson() == "" {
		t.Fatalf("response = %+v", response)
	}
	var result agentservice.RunResult
	if err := json.Unmarshal([]byte(response.GetResultJson()), &result); err != nil {
		t.Fatal(err)
	}
	if result.Status != "completed" || len(result.Citations) != 1 {
		t.Fatalf("agent result = %+v", result)
	}
}

func TestInvokeAPIDocAgentRejectsInvalidPayload(t *testing.T) {
	handler := newAPIDocHandler(t)
	response, err := handler.Invoke(context.Background(), &agentplatform.CapabilityRequest{CapabilityId: capability.APIDocAgentV1ID, PayloadJson: `{}`})
	if err != nil {
		t.Fatal(err)
	}
	if response.GetError().GetCode() != "INVALID_REQUEST" {
		t.Fatalf("error = %+v", response.GetError())
	}
}

func TestInvokeAPIDocAgentRejectsUnknownOrSensitiveFields(t *testing.T) {
	handler := newAPIDocHandler(t)
	response, err := handler.Invoke(context.Background(), &agentplatform.CapabilityRequest{
		CapabilityId: capability.APIDocAgentV1ID,
		PayloadJson:  `{"query":"Barrier","wiki_id":"wiki:test","namespace":"AscendC.API","version":"v1","api_key":"secret"}`,
	})
	if err != nil {
		t.Fatal(err)
	}
	if response.GetError().GetCode() != "INVALID_REQUEST" {
		t.Fatalf("error = %+v", response.GetError())
	}
}
