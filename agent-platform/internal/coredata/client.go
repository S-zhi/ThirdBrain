// Package coredata provides the Agent Platform's only path to Core data.
package coredata

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

const (
	retrievalContextPath = "/internal/v1/agent-data/retrieval/context"
	maxResponseBytes     = 4 << 20
)

// RetrievalScope is the version-isolated lookup scope accepted by the Python
// Core data Gateway.
type RetrievalScope struct {
	WikiID           string   `json:"wiki_id"`
	Namespace        string   `json:"namespace"`
	Version          string   `json:"version"`
	Language         string   `json:"language,omitempty"`
	RAGCollectionIDs []string `json:"rag_collection_ids,omitempty"`
}

// RetrieveContextRequest is deliberately read-only: it has no update_wiki or
// generic tool arguments field.
type RetrieveContextRequest struct {
	RequestID       string         `json:"-"`
	CorrelationID   string         `json:"-"`
	Query           string         `json:"query"`
	Scope           RetrievalScope `json:"-"`
	TopK            int            `json:"top_k,omitempty"`
	Budget          string         `json:"budget,omitempty"`
	IncludeStale    *bool          `json:"include_stale,omitempty"`
	ExpandRelations *bool          `json:"expand_relations,omitempty"`
	RelationLimit   int            `json:"relation_limit,omitempty"`
}

// MarshalJSON keeps the external Core data contract flat while retaining a
// nested Go representation for the Agent workflow.
func (request RetrieveContextRequest) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Query            string   `json:"query"`
		WikiID           string   `json:"wiki_id"`
		Namespace        string   `json:"namespace"`
		Version          string   `json:"version"`
		Language         string   `json:"language,omitempty"`
		RAGCollectionIDs []string `json:"rag_collection_ids,omitempty"`
		TopK             int      `json:"top_k,omitempty"`
		Budget           string   `json:"budget,omitempty"`
		IncludeStale     *bool    `json:"include_stale,omitempty"`
		ExpandRelations  *bool    `json:"expand_relations,omitempty"`
		RelationLimit    int      `json:"relation_limit,omitempty"`
	}{
		Query:            request.Query,
		WikiID:           request.Scope.WikiID,
		Namespace:        request.Scope.Namespace,
		Version:          request.Scope.Version,
		Language:         request.Scope.Language,
		RAGCollectionIDs: request.Scope.RAGCollectionIDs,
		TopK:             request.TopK,
		Budget:           request.Budget,
		IncludeStale:     request.IncludeStale,
		ExpandRelations:  request.ExpandRelations,
		RelationLimit:    request.RelationLimit,
	})
}

// RetrievalResult keeps Core's machine-readable payload opaque to Go. The
// Python domain service remains responsible for its schema and interpretation.
type RetrievalResult struct {
	Payload json.RawMessage
}

// Client is the private, authenticated Core data client used by Agent workflows.
type Client struct {
	baseURL    string
	apiKey     string
	httpClient *http.Client
}

// NewClient constructs a client without exposing database or provider options.
func NewClient(baseURL, apiKey string, timeout time.Duration) *Client {
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		apiKey:  apiKey,
		httpClient: &http.Client{
			Timeout: timeout,
		},
	}
}

// RetrieveContext calls Core's private read-only data Gateway.
func (client *Client) RetrieveContext(
	ctx context.Context,
	request RetrieveContextRequest,
) (RetrievalResult, error) {
	body, err := json.Marshal(request)
	if err != nil {
		return RetrievalResult{}, fmt.Errorf("marshal Core retrieval request: %w", err)
	}

	httpRequest, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		client.baseURL+retrievalContextPath,
		bytes.NewReader(body),
	)
	if err != nil {
		return RetrievalResult{}, fmt.Errorf("build Core retrieval request: %w", err)
	}
	httpRequest.Header.Set("Content-Type", "application/json")
	httpRequest.Header.Set("X-Agent-Platform-Key", client.apiKey)
	if request.RequestID != "" {
		httpRequest.Header.Set("X-Request-ID", request.RequestID)
	}
	if request.CorrelationID != "" {
		httpRequest.Header.Set("X-Correlation-ID", request.CorrelationID)
	}

	response, err := client.httpClient.Do(httpRequest)
	if err != nil {
		return RetrievalResult{}, fmt.Errorf("call Core retrieval data Gateway: %w", err)
	}
	defer response.Body.Close()

	payload, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	if err != nil {
		return RetrievalResult{}, fmt.Errorf("read Core retrieval response: %w", err)
	}
	if len(payload) > maxResponseBytes {
		return RetrievalResult{}, fmt.Errorf("Core retrieval data Gateway response exceeds %d bytes", maxResponseBytes)
	}
	if response.StatusCode != http.StatusOK {
		return RetrievalResult{}, fmt.Errorf("Core retrieval data Gateway returned HTTP %d", response.StatusCode)
	}
	if !json.Valid(payload) {
		return RetrievalResult{}, fmt.Errorf("Core retrieval data Gateway returned invalid JSON")
	}
	return RetrievalResult{Payload: payload}, nil
}
