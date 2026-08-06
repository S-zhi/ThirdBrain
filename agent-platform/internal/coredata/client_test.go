package coredata

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestClientRetrieveContextUsesPrivateReadOnlyGateway(t *testing.T) {
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost {
			t.Errorf("method = %s, want POST", request.Method)
			return
		}
		if request.URL.Path != retrievalContextPath {
			t.Errorf("path = %s, want %s", request.URL.Path, retrievalContextPath)
			return
		}
		if got := request.Header.Get("X-Agent-Platform-Key"); got != "agent-secret" {
			t.Errorf("agent key = %q", got)
			return
		}
		if got := request.Header.Get("X-Request-ID"); got != "request-1" {
			t.Errorf("request id = %q", got)
			return
		}
		if got := request.Header.Get("X-Correlation-ID"); got != "correlation-1" {
			t.Errorf("correlation id = %q", got)
			return
		}
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Errorf("read body: %v", err)
			return
		}
		var got map[string]any
		if err := json.Unmarshal(body, &got); err != nil {
			t.Errorf("decode body: %v", err)
			return
		}
		if _, exists := got["expand_relations"]; exists {
			t.Error("expand_relations must be omitted so Python owns its default")
			return
		}
		if _, exists := got["include_stale"]; exists {
			t.Error("include_stale must be omitted so Python owns its default")
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"query_id":"query-1","found":false}`))
	}))
	defer server.Close()

	client := NewClient(server.URL, "agent-secret", time.Second)
	result, err := client.RetrieveContext(context.Background(), RetrieveContextRequest{
		RequestID:     "request-1",
		CorrelationID: "correlation-1",
		Query:         "Barrier",
		Scope: RetrievalScope{
			WikiID:    "wiki:test",
			Namespace: "AscendC.API",
			Version:   "v1",
		},
		TopK: 3,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got := string(result.Payload); got != `{"query_id":"query-1","found":false}` {
		t.Fatalf("payload = %s", got)
	}
}

func TestClientRetrieveContextRejectsCoreFailure(t *testing.T) {
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()

	client := NewClient(server.URL, "agent-secret", time.Second)
	_, err := client.RetrieveContext(context.Background(), RetrieveContextRequest{
		Query: "Barrier",
		Scope: RetrievalScope{WikiID: "wiki:test", Namespace: "AscendC.API", Version: "v1"},
	})
	if err == nil {
		t.Fatal("expected Core failure")
	}
}
