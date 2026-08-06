package workflow

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/S-zhi/ThirdBrain/agent-platform/internal/coredata"
)

type fakeRetrievalTool struct {
	request coredata.RetrieveContextRequest
	err     error
}

func (tool *fakeRetrievalTool) RetrieveContext(
	ctx context.Context,
	request coredata.RetrieveContextRequest,
) (coredata.RetrievalResult, error) {
	tool.request = request
	return coredata.RetrievalResult{Payload: json.RawMessage(`{"query_id":"query-1"}`)}, tool.err
}

func TestKnowledgeAssistWorkflowUsesOnlyCoreRetrievalTool(t *testing.T) {
	t.Parallel()
	tool := &fakeRetrievalTool{}
	workflow := NewKnowledgeAssistWorkflow(tool)

	result, err := workflow.Execute(context.Background(), KnowledgeAssistRequest{
		RequestID:     "request-1",
		CorrelationID: "correlation-1",
		Query:         "Barrier",
		Scope: coredata.RetrievalScope{
			WikiID:    "wiki:test",
			Namespace: "AscendC.API",
			Version:   "v1",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if tool.request.Query != "Barrier" || tool.request.Scope.WikiID != "wiki:test" {
		t.Fatalf("unexpected Core request: %+v", tool.request)
	}
	if tool.request.RequestID != "request-1" {
		t.Fatalf("request id was not propagated: %q", tool.request.RequestID)
	}
	if tool.request.CorrelationID != "correlation-1" {
		t.Fatalf("correlation id was not propagated: %q", tool.request.CorrelationID)
	}
	if result.ContextPacket != `{"query_id":"query-1"}` {
		t.Fatalf("context packet = %s", result.ContextPacket)
	}
}

func TestKnowledgeAssistWorkflowRejectsMultipleToolCalls(t *testing.T) {
	t.Parallel()
	workflow := NewKnowledgeAssistWorkflow(&fakeRetrievalTool{})

	_, err := workflow.Execute(context.Background(), KnowledgeAssistRequest{
		RequestID:    "request-1",
		Query:        "Barrier",
		MaxToolCalls: 2,
		Scope: coredata.RetrievalScope{
			WikiID:    "wiki:test",
			Namespace: "AscendC.API",
			Version:   "v1",
		},
	})
	if err == nil {
		t.Fatal("expected tool limit error")
	}
	if !IsInvalidRequest(err) {
		t.Fatalf("error was not classified as invalid request: %v", err)
	}
}
