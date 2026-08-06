// Package workflow contains bounded Agent-facing workflows.
package workflow

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/S-zhi/ThirdBrain/agent-platform/internal/coredata"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/einobridge"
	"github.com/cloudwego/eino/schema"
)

const defaultToolCallLimit = 1

var errInvalidRequest = errors.New("invalid knowledge assist request")

// IsInvalidRequest reports whether workflow validation rejected the request.
func IsInvalidRequest(err error) bool {
	return errors.Is(err, errInvalidRequest)
}

// KnowledgeAssistRequest is the middleware-level input to the read-only
// knowledge-assistance workflow.
type KnowledgeAssistRequest struct {
	RequestID      string
	CorrelationID  string
	Query          string
	Scope          coredata.RetrievalScope
	DeadlineMillis int32
	MaxToolCalls   int16
}

// KnowledgeAssistResult is an opaque Core context payload plus middleware
// metadata. Go intentionally does not reinterpret retrieval semantics.
type KnowledgeAssistResult struct {
	ContextPacket string
	Warnings      []string
	Messages      []*schema.Message
}

// RetrievalTool is the sole v1 data tool that an Eino workflow may invoke.
// A later Eino Runner adapts this port without expanding its permissions.
type RetrievalTool interface {
	RetrieveContext(context.Context, coredata.RetrieveContextRequest) (coredata.RetrievalResult, error)
}

// KnowledgeAssistWorkflow is the deterministic v1 foundation for an Eino
// workflow. It has one explicitly allowlisted read-only tool and no model,
// file, network, database, or write capability of its own.
type KnowledgeAssistWorkflow struct {
	retrievalTool RetrievalTool
}

// NewKnowledgeAssistWorkflow wires the only allowed Core data tool.
func NewKnowledgeAssistWorkflow(retrievalTool RetrievalTool) *KnowledgeAssistWorkflow {
	return &KnowledgeAssistWorkflow{retrievalTool: retrievalTool}
}

// Execute obtains the bounded Core context that a subsequent Eino Agent turn
// may use. Keeping this first workflow deterministic prevents an unconfigured
// model from choosing arbitrary tools.
func (workflow *KnowledgeAssistWorkflow) Execute(
	ctx context.Context,
	request KnowledgeAssistRequest,
) (KnowledgeAssistResult, error) {
	if strings.TrimSpace(request.RequestID) == "" {
		return KnowledgeAssistResult{}, fmt.Errorf("%w: request_id is required", errInvalidRequest)
	}
	if strings.TrimSpace(request.Query) == "" {
		return KnowledgeAssistResult{}, fmt.Errorf("%w: query is required", errInvalidRequest)
	}
	if strings.TrimSpace(request.Scope.WikiID) == "" ||
		strings.TrimSpace(request.Scope.Namespace) == "" ||
		strings.TrimSpace(request.Scope.Version) == "" {
		return KnowledgeAssistResult{}, fmt.Errorf("%w: wiki_id, namespace, and version are required", errInvalidRequest)
	}
	if request.MaxToolCalls != 0 && request.MaxToolCalls != defaultToolCallLimit {
		return KnowledgeAssistResult{}, fmt.Errorf("%w: v1 permits exactly one retrieval tool call", errInvalidRequest)
	}
	if request.DeadlineMillis < 0 {
		return KnowledgeAssistResult{}, fmt.Errorf("%w: deadline_ms must not be negative", errInvalidRequest)
	}

	result, err := workflow.retrievalTool.RetrieveContext(ctx, coredata.RetrieveContextRequest{
		RequestID:     request.RequestID,
		CorrelationID: request.CorrelationID,
		Query:         request.Query,
		Scope:         request.Scope,
	})
	if err != nil {
		return KnowledgeAssistResult{}, err
	}
	contextPacket := string(result.Payload)
	return KnowledgeAssistResult{
		ContextPacket: contextPacket,
		Messages:      einobridge.BuildKnowledgeAssistMessages(request.Query, contextPacket),
	}, nil
}
