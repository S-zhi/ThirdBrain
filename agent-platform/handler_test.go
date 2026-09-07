package main

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/S-zhi/ThirdBrain/agent-platform/internal/coredata"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/workflow"
	agentplatform "github.com/S-zhi/ThirdBrain/agent-platform/kitex_gen/agentplatform"
)

type handlerRetrievalTool struct {
	err error
}

func (tool handlerRetrievalTool) RetrieveContext(
	ctx context.Context,
	request coredata.RetrieveContextRequest,
) (coredata.RetrievalResult, error) {
	return coredata.RetrievalResult{Payload: json.RawMessage(`{"query_id":"query-1"}`)}, tool.err
}

type deadlineRetrievalTool struct {
	deadline time.Time
}

func (tool *deadlineRetrievalTool) RetrieveContext(
	ctx context.Context,
	request coredata.RetrieveContextRequest,
) (coredata.RetrievalResult, error) {
	deadline, ok := ctx.Deadline()
	if !ok {
		return coredata.RetrievalResult{}, errors.New("workflow context has no deadline")
	}
	tool.deadline = deadline
	return coredata.RetrievalResult{Payload: json.RawMessage(`{"query_id":"query-1"}`)}, nil
}

func TestExecuteKnowledgeAssistReturnsCoreContext(t *testing.T) {
	t.Parallel()
	handler := NewAgentPlatformServiceImpl(
		workflow.NewKnowledgeAssistWorkflow(handlerRetrievalTool{}),
	)

	response, err := handler.ExecuteKnowledgeAssist(context.Background(), &agentplatform.ExecuteKnowledgeAssistRequest{
		RequestId: "request-1",
		Query:     "Barrier",
		Scope: &agentplatform.RetrievalScope{
			WikiId:    "wiki:test",
			Namespace: "AscendC.API",
			Version:   "v1",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if response.Status != agentplatform.AssistStatus_COMPLETED {
		t.Fatalf("status = %v", response.Status)
	}
	if response.GetContextPacket() != `{"query_id":"query-1"}` {
		t.Fatalf("context = %s", response.GetContextPacket())
	}
	if len(response.Trace) != 1 || response.Trace[0].Detail != "knowledge.retrieve_context.v1" {
		t.Fatalf("capability trace = %+v", response.Trace)
	}
}

func TestExecuteKnowledgeAssistDoesNotExposeCoreFailure(t *testing.T) {
	t.Parallel()
	handler := NewAgentPlatformServiceImpl(
		workflow.NewKnowledgeAssistWorkflow(handlerRetrievalTool{err: errors.New("connection refused")}),
	)

	response, err := handler.ExecuteKnowledgeAssist(context.Background(), &agentplatform.ExecuteKnowledgeAssistRequest{
		RequestId: "request-1",
		Query:     "Barrier",
		Scope: &agentplatform.RetrievalScope{
			WikiId:    "wiki:test",
			Namespace: "AscendC.API",
			Version:   "v1",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if response.Status != agentplatform.AssistStatus_FAILED {
		t.Fatalf("status = %v", response.Status)
	}
	if response.GetErrorCode() != "CORE_DATA_UNAVAILABLE" {
		t.Fatalf("error code = %s", response.GetErrorCode())
	}
}

func TestExecuteKnowledgeAssistClassifiesValidationFailure(t *testing.T) {
	t.Parallel()
	handler := NewAgentPlatformServiceImpl(
		workflow.NewKnowledgeAssistWorkflow(handlerRetrievalTool{}),
	)

	response, err := handler.ExecuteKnowledgeAssist(context.Background(), &agentplatform.ExecuteKnowledgeAssistRequest{
		RequestId: "request-1",
		Scope: &agentplatform.RetrievalScope{
			WikiId:    "wiki:test",
			Namespace: "AscendC.API",
			Version:   "v1",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if response.GetErrorCode() != "INVALID_REQUEST" {
		t.Fatalf("error code = %s", response.GetErrorCode())
	}
}

func TestExecuteKnowledgeAssistCapsCallerDeadlineAtCapabilityLimit(t *testing.T) {
	t.Parallel()
	tool := &deadlineRetrievalTool{}
	handler := NewAgentPlatformServiceImpl(workflow.NewKnowledgeAssistWorkflow(tool))
	startedAt := time.Now()
	deadlineMillis := int32(60_000)

	response, err := handler.ExecuteKnowledgeAssist(context.Background(), &agentplatform.ExecuteKnowledgeAssistRequest{
		RequestId: "request-1",
		Query:     "Barrier",
		Scope: &agentplatform.RetrievalScope{
			WikiId: "wiki:test", Namespace: "AscendC.API", Version: "v1",
		},
		Policy: &agentplatform.AssistPolicy{DeadlineMs: &deadlineMillis},
	})
	if err != nil {
		t.Fatal(err)
	}
	if response.Status != agentplatform.AssistStatus_COMPLETED {
		t.Fatalf("status = %v", response.Status)
	}
	if got := tool.deadline.Sub(startedAt); got <= 0 || got > 30*time.Second+time.Second {
		t.Fatalf("effective deadline = %s, want at most capability limit", got)
	}
}

func TestExecuteKnowledgeAssistRejectsNegativeDeadline(t *testing.T) {
	t.Parallel()
	handler := NewAgentPlatformServiceImpl(workflow.NewKnowledgeAssistWorkflow(handlerRetrievalTool{}))
	deadlineMillis := int32(-1)

	response, err := handler.ExecuteKnowledgeAssist(context.Background(), &agentplatform.ExecuteKnowledgeAssistRequest{
		RequestId: "request-1",
		Query:     "Barrier",
		Scope: &agentplatform.RetrievalScope{
			WikiId: "wiki:test", Namespace: "AscendC.API", Version: "v1",
		},
		Policy: &agentplatform.AssistPolicy{DeadlineMs: &deadlineMillis},
	})
	if err != nil {
		t.Fatal(err)
	}
	if response.GetErrorCode() != "INVALID_REQUEST" {
		t.Fatalf("error code = %q", response.GetErrorCode())
	}
}
