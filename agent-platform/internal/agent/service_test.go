package agent

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"time"

	apidoctool "github.com/S-zhi/ThirdBrain/agent-platform/internal/agenttool/apidoc"
	coreapidoc "github.com/S-zhi/ThirdBrain/agent-platform/internal/apidoc"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/coredata"
	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/schema"
)

type recordingRetrieval struct {
	mu       sync.Mutex
	requests []coredata.RetrieveContextRequest
	payload  string
}

func (retrieval *recordingRetrieval) RetrieveContext(_ context.Context, request coredata.RetrieveContextRequest) (coredata.RetrievalResult, error) {
	retrieval.mu.Lock()
	retrieval.requests = append(retrieval.requests, request)
	retrieval.mu.Unlock()
	return coredata.RetrievalResult{Payload: json.RawMessage(retrieval.payload)}, nil
}

type scriptedModel struct{}

func (scriptedModel) Generate(_ context.Context, input []*schema.Message, _ ...model.Option) (*schema.Message, error) {
	for _, message := range input {
		if message != nil && message.Role == schema.Tool {
			return schema.AssistantMessage("Barrier is documented by the verified wiki result.", nil), nil
		}
	}
	return schema.AssistantMessage("", []schema.ToolCall{{
		ID: "tool-call-1",
		Function: schema.FunctionCall{
			Name:      apidoctool.Name,
			Arguments: `{"query":"Barrier","wiki_id":"attacker","namespace":"wrong","version":"old"}`,
		},
	}}), nil
}

func (scriptedModel) Stream(ctx context.Context, input []*schema.Message, opts ...model.Option) (*schema.StreamReader[*schema.Message], error) {
	message, err := (scriptedModel{}).Generate(ctx, input, opts...)
	if err != nil {
		return nil, err
	}
	return schema.StreamReaderFromArray([]*schema.Message{message}), nil
}

func (scriptedModel) WithTools(_ []*schema.ToolInfo) (model.ToolCallingChatModel, error) {
	return scriptedModel{}, nil
}

func newTestService(t *testing.T, payload string) (*Service, *recordingRetrieval) {
	t.Helper()
	retrieval := &recordingRetrieval{payload: payload}
	service, err := NewAPIDocumentService(context.Background(), scriptedModel{}, coreapidoc.New(retrieval))
	if err != nil {
		t.Fatal(err)
	}
	return service, retrieval
}

func testRunRequest() RunRequest {
	return RunRequest{
		InvocationID: "invocation-1",
		TraceID:      "trace-1",
		Query:        "Barrier",
		Scope: coredata.RetrievalScope{
			WikiID:    "wiki:trusted",
			Namespace: "AscendC.API",
			Version:   "v1",
		},
		MaxResults: 5,
		Deadline:   time.Second,
	}
}

func TestRunUsesVerifiedToolResultAndPreservesScope(t *testing.T) {
	service, retrieval := newTestService(t, `{"knowledge_hits":[{"title":"Barrier","summary":"Use the Barrier API before synchronization.","content":"full content","score":0.91,"provenance":[{"path":"api/barrier.md","namespace":"AscendC.API","version":"v1"}]}]}`)

	result, err := service.Run(context.Background(), testRunRequest())
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "completed" || result.ToolCalls != 1 {
		t.Fatalf("result = %+v", result)
	}
	if result.Answer == "" || len(result.Citations) != 1 || result.Citations[0].SourcePath != "api/barrier.md" {
		t.Fatalf("result = %+v", result)
	}
	retrieval.mu.Lock()
	defer retrieval.mu.Unlock()
	if len(retrieval.requests) != 1 {
		t.Fatalf("requests = %+v", retrieval.requests)
	}
	request := retrieval.requests[0]
	if request.Scope.WikiID != "wiki:trusted" || request.Scope.Namespace != "AscendC.API" || request.Scope.Version != "v1" {
		t.Fatalf("tool changed trusted scope: %+v", request.Scope)
	}
	if request.RequestID != "invocation-1" {
		t.Fatalf("request id = %q", request.RequestID)
	}
}

func TestRunAbstainsWhenVerifiedToolReturnsNoHits(t *testing.T) {
	service, _ := newTestService(t, `{"knowledge_hits":[],"source_hits":[]}`)

	result, err := service.Run(context.Background(), testRunRequest())
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "abstained" || len(result.Warnings) == 0 || result.ToolCalls != 1 {
		t.Fatalf("result = %+v", result)
	}
}

func TestRunRejectsMissingRequestScopeBeforeCallingModel(t *testing.T) {
	service, retrieval := newTestService(t, `{"knowledge_hits":[]}`)
	request := testRunRequest()
	request.Scope.Version = ""

	result, err := service.Run(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "abstained" || len(result.Warnings) != 1 {
		t.Fatalf("result = %+v", result)
	}
	retrieval.mu.Lock()
	defer retrieval.mu.Unlock()
	if len(retrieval.requests) != 0 {
		t.Fatalf("retrieval was called for invalid request: %+v", retrieval.requests)
	}
}

func TestParseToolResultRejectsUnsafeCitation(t *testing.T) {
	_, _, err := parseToolResult(`{"returned_count":1,"results":[{"title":"secret","source_path":"../secret.md","snippet":"x","score":0.8,"matched_at":"2026-08-11T00:00:00Z"}]}`)
	if err == nil {
		t.Fatal("expected unsafe citation path to be rejected")
	}
}
