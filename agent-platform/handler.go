package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"strings"
	"time"

	"github.com/S-zhi/ThirdBrain/agent-platform/internal/apidoc"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/capability"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/coredata"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/workflow"
	agentplatform "github.com/S-zhi/ThirdBrain/agent-platform/kitex_gen/agentplatform"
)

// AgentPlatformServiceImpl implements the last service interface defined in the IDL.
type AgentPlatformServiceImpl struct {
	workflow *workflow.KnowledgeAssistWorkflow
	apiDoc   *apidoc.Adapter
	timeout  time.Duration
}

// NewAgentPlatformServiceImpl constructs the Kitex handler without granting it
// access to Core data stores or provider credentials.
func NewAgentPlatformServiceImpl(
	knowledgeWorkflow *workflow.KnowledgeAssistWorkflow,
	retrievalTools ...apidoc.RetrievalTool,
) *AgentPlatformServiceImpl {
	service := &AgentPlatformServiceImpl{workflow: knowledgeWorkflow, timeout: capability.APIDocRetrievalV1().Timeout}
	if len(retrievalTools) > 0 {
		service.apiDoc = apidoc.New(retrievalTools[0])
	}
	return service
}

// NewAgentPlatformServiceImplWithTimeout wires the single process-wide
// capability deadline into both handler paths. The existing constructor keeps
// its default for backwards-compatible tests and embedders.
func NewAgentPlatformServiceImplWithTimeout(
	knowledgeWorkflow *workflow.KnowledgeAssistWorkflow,
	timeout time.Duration,
	retrievalTools ...apidoc.RetrievalTool,
) *AgentPlatformServiceImpl {
	service := NewAgentPlatformServiceImpl(knowledgeWorkflow, retrievalTools...)
	if timeout > 0 {
		service.timeout = timeout
	}
	return service
}

// Discover returns the statically registered, read-only capabilities.
func (service *AgentPlatformServiceImpl) Discover(_ context.Context, readOnly bool) ([]*agentplatform.CapabilityDescriptor, error) {
	if !readOnly {
		return []*agentplatform.CapabilityDescriptor{}, nil
	}
	descriptor := capability.APIDocRetrievalV1()
	return []*agentplatform.CapabilityDescriptor{{
		CapabilityId: descriptor.ID, Name: "API 文档检索（LLM Wiki 信息 Loop 查找）", Module: descriptor.Category,
		Version: descriptor.Version, Status: "available", RiskLevel: descriptor.Risk,
		InvocationMode: "online", ExecutionMode: "sync", Dependencies: []string{descriptor.Dependency},
		TimeoutDefaultMs: 30000, TimeoutMaxMs: 30000, ClientRetryable: true,
		InputSchemaRef: descriptor.InputSchema, OutputSchemaRef: descriptor.OutputSchema,
	}}, nil
}

// Invoke dispatches the capability without exposing any data-store client.
func (service *AgentPlatformServiceImpl) Invoke(ctx context.Context, request *agentplatform.CapabilityRequest) (*agentplatform.CapabilityResponse, error) {
	startedAt := time.Now()
	response := &agentplatform.CapabilityResponse{CapabilityId: "", Status: "failure"}
	if request != nil {
		response.CapabilityId = request.CapabilityId
	}
	response.TraceId = requestTraceID(request)
	defer func() { response.ElapsedMs = time.Since(startedAt).Milliseconds() }()
	if request == nil || request.CapabilityId != capability.APIDocRetrievalV1ID {
		response.Error = capabilityError("INVALID_REQUEST", "unknown or missing capability", false)
		return response, nil
	}
	if service.apiDoc == nil {
		response.Error = capabilityError("CAPABILITY_UNAVAILABLE", "API document retrieval is unavailable", false)
		return response, nil
	}
	deadline := service.timeout
	if deadline <= 0 {
		deadline = capability.APIDocRetrievalV1().Timeout
	}
	if request.IsSetTimeoutMs() {
		if request.GetTimeoutMs() <= 0 || time.Duration(request.GetTimeoutMs())*time.Millisecond > deadline {
			response.Error = capabilityError("INVALID_REQUEST", "timeout_ms exceeds the configured capability timeout", false)
			return response, nil
		}
		deadline = time.Duration(request.GetTimeoutMs()) * time.Millisecond
	}
	ctx, cancel := context.WithTimeout(ctx, deadline)
	defer cancel()
	result, err := service.apiDoc.Invoke(ctx, []byte(request.PayloadJson), response.TraceId)
	if err != nil {
		code, retryable := classifyCapabilityError(ctx, err)
		response.Error = capabilityError(code, sanitizedMessage(code), retryable)
		return response, nil
	}
	resultJSON := string(result)
	response.Status = "success"
	response.ResultJson = &resultJSON
	return response, nil
}

func requestTraceID(request *agentplatform.CapabilityRequest) string {
	if request != nil && strings.TrimSpace(request.GetTraceId()) != "" {
		return request.GetTraceId()
	}
	var bytes [16]byte
	if _, err := rand.Read(bytes[:]); err == nil {
		bytes[6] = (bytes[6] & 0x0f) | 0x40
		bytes[8] = (bytes[8] & 0x3f) | 0x80
		hexValue := hex.EncodeToString(bytes[:])
		return hexValue[:8] + "-" + hexValue[8:12] + "-" + hexValue[12:16] + "-" + hexValue[16:20] + "-" + hexValue[20:]
	}
	return "trace-generation-failed"
}

func capabilityError(code, message string, retryable bool) *agentplatform.CapabilityError {
	return &agentplatform.CapabilityError{Code: code, Message: message, Retryable: retryable}
}

func classifyCapabilityError(ctx context.Context, err error) (string, bool) {
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return "TIMEOUT", true
	}
	message := strings.ToLower(err.Error())
	if strings.Contains(message, "invalid request") || strings.Contains(message, "required") || strings.Contains(message, "must ") || strings.Contains(message, "sensitive") {
		return "INVALID_REQUEST", false
	}
	if strings.Contains(message, "401") || strings.Contains(message, "403") {
		return "UNAUTHORIZED", false
	}
	if strings.Contains(message, "unavailable") || strings.Contains(message, "connection") || strings.Contains(message, "timeout") || strings.Contains(message, "503") {
		return "DEPENDENCY_FAILED", true
	}
	return "INTERNAL_ERROR", false
}

func sanitizedMessage(code string) string {
	messages := map[string]string{"INVALID_REQUEST": "request violates the capability contract", "TIMEOUT": "downstream retrieval timed out", "UNAUTHORIZED": "downstream authorization failed", "DEPENDENCY_FAILED": "retrieval dependency is unavailable", "INTERNAL_ERROR": "capability execution failed"}
	if message, ok := messages[code]; ok {
		return message
	}
	return "capability execution failed"
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
