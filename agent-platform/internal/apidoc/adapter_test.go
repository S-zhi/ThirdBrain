package apidoc

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/S-zhi/ThirdBrain/agent-platform/internal/coredata"
)

type fakeRetrieval struct{ payload []byte }

func (fake fakeRetrieval) RetrieveContext(context.Context, coredata.RetrieveContextRequest) (coredata.RetrievalResult, error) {
	return coredata.RetrievalResult{Payload: fake.payload}, nil
}

func TestInvokeNormalizesKnowledgeHits(t *testing.T) {
	adapter := New(fakeRetrieval{payload: []byte(`{"knowledge_hits":[{"title":"API","summary":"short","score":0.8,"provenance":[{"path":"topics/api.md","namespace":"demo"}]}]}`)})
	result, err := adapter.Invoke(context.Background(), []byte(`{"query":"find","wiki_id":"wiki","namespace":"demo","version":"v1","max_results":1}`), "trace")
	if err != nil {
		t.Fatal(err)
	}
	var decoded Result
	if err := json.Unmarshal(result, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.ReturnedCount != 1 || decoded.Results[0]["source_path"] != "topics/api.md" {
		t.Fatalf("unexpected result: %#v", decoded)
	}
}

func TestInvokeRejectsSensitivePayload(t *testing.T) {
	adapter := New(fakeRetrieval{})
	if _, err := adapter.Invoke(context.Background(), []byte(`{"query":"find","wiki_id":"wiki","namespace":"demo","version":"v1","token":"secret"}`), "trace"); err == nil {
		t.Fatal("sensitive payload unexpectedly accepted")
	}
}

func TestInvokeRejectsAbsoluteSourcePath(t *testing.T) {
	adapter := New(fakeRetrieval{payload: []byte(`{"knowledge_hits":[{"title":"API","score":0.8,"provenance":[{"path":"/etc/passwd"}]}]}`)})
	if _, err := adapter.Invoke(context.Background(), []byte(`{"query":"find","wiki_id":"wiki","namespace":"demo","version":"v1"}`), "trace"); err == nil {
		t.Fatal("absolute source path unexpectedly accepted")
	}
}
