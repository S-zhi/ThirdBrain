package main

import (
	"context"
	"time"

	"github.com/S-zhi/ThirdBrain/agent-platform/internal/capability"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/coredata"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/workflow"
	agentplatform "github.com/S-zhi/ThirdBrain/agent-platform/kitex_gen/agentplatform"
)

// AgentPlatformServiceImpl implements the last service interface defined in the IDL.
type AgentPlatformServiceImpl struct {
	workflow *workflow.KnowledgeAssistWorkflow
}

// NewAgentPlatformServiceImpl constructs the Kitex handler without granting it
// access to Core data stores or provider credentials.
func NewAgentPlatformServiceImpl(
	knowledgeWorkflow *workflow.KnowledgeAssistWorkflow,
) *AgentPlatformServiceImpl {
	return &AgentPlatformServiceImpl{workflow: knowledgeWorkflow}
}

// ExecuteKnowledgeAssist implements the AgentPlatformServiceImpl interface.
func (service *AgentPlatformServiceImpl) ExecuteKnowledgeAssist(
	ctx context.Context,
	request *agentplatform.ExecuteKnowledgeAssistRequest,
) (*agentplatform.ExecuteKnowledgeAssistResponse, error) {
	if request == nil || request.Scope == nil {
		return failedResponse("", "INVALID_REQUEST", "request and scope are required"), nil
	}
	if service.workflow == nil {
		return failedResponse(request.RequestId, "PLATFORM_UNAVAILABLE", "Agent Platform is unavailable"), nil
	}
	var deadlineMillis int32
	var maxToolCalls int16
	if request.Policy != nil {
		deadlineMillis = request.Policy.GetDeadlineMs()
		maxToolCalls = request.Policy.GetMaxToolCalls()
	}
	if deadlineMillis < 0 {
		return failedResponse(request.RequestId, "INVALID_REQUEST", "deadline_ms must not be negative"), nil
	}
	deadline := capability.KnowledgeRetrieveContextV1().Timeout
	if deadlineMillis > 0 {
		requestedDeadline := time.Duration(deadlineMillis) * time.Millisecond
		if requestedDeadline < deadline {
			deadline = requestedDeadline
		}
	}
	var cancel context.CancelFunc
	ctx, cancel = context.WithTimeout(ctx, deadline)
	defer cancel()

	result, err := service.workflow.Execute(ctx, workflow.KnowledgeAssistRequest{
		RequestID:     request.RequestId,
		CorrelationID: request.GetCorrelationId(),
		Query:         request.Query,
		Scope: coredata.RetrievalScope{
			WikiID:           request.Scope.WikiId,
			Namespace:        request.Scope.Namespace,
			Version:          request.Scope.Version,
			Language:         request.Scope.GetLanguage(),
			RAGCollectionIDs: request.Scope.GetRagCollectionIds(),
		},
		DeadlineMillis: deadlineMillis,
		MaxToolCalls:   maxToolCalls,
	})
	if err != nil {
		if workflow.IsInvalidRequest(err) {
			return failedResponse(request.RequestId, "INVALID_REQUEST", "request violates the v1 capability contract"), nil
		}
		return failedResponse(request.RequestId, "CORE_DATA_UNAVAILABLE", "Core retrieval data is unavailable"), nil
	}

	return &agentplatform.ExecuteKnowledgeAssistResponse{
		RequestId:     request.RequestId,
		Status:        agentplatform.AssistStatus_COMPLETED,
		ContextPacket: &result.ContextPacket,
		Warnings:      result.Warnings,
		Trace: []*agentplatform.AgentTraceEvent{
			{Stage: "capability", Detail: capability.KnowledgeRetrieveContextV1ID},
		},
	}, nil
}

func failedResponse(requestID, code, message string) *agentplatform.ExecuteKnowledgeAssistResponse {
	return &agentplatform.ExecuteKnowledgeAssistResponse{
		RequestId:    requestID,
		Status:       agentplatform.AssistStatus_FAILED,
		ErrorCode:    &code,
		ErrorMessage: &message,
	}
}
