package apidoctool

import (
	"context"
	"encoding/json"
	"testing"

	coreapidoc "github.com/S-zhi/ThirdBrain/agent-platform/internal/apidoc"
	"github.com/S-zhi/ThirdBrain/agent-platform/internal/coredata"
)

type recordingToolDownstream struct {
	request coredata.RetrieveContextRequest
}

func (downstream *recordingToolDownstream) RetrieveContext(_ context.Context, request coredata.RetrieveContextRequest) (coredata.RetrievalResult, error) {
	downstream.request = request
	return coredata.RetrievalResult{Payload: json.RawMessage(`{"knowledge_hits":[{"title":"Barrier","summary":"verified","content":"content","score":0.8,"provenance":[{"path":"api/barrier.md","namespace":"AscendC.API","version":"v1"}]}]}`)}, nil
}

func TestInvokableRunDelegatesValidatedArguments(t *testing.T) {
	downstream := &recordingToolDownstream{}
	tool := New(coreapidoc.New(downstream))
	ctx := WithInvocationID(context.Background(), "invocation-42")

	result, err := tool.InvokableRun(ctx, `{"query":"Barrier","wiki_id":"wiki:test","namespace":"AscendC.API","version":"v1","max_results":1}`)
	if err != nil {
		t.Fatal(err)
	}
	var normalized coreapidoc.Result
	if err := json.Unmarshal([]byte(result), &normalized); err != nil {
		t.Fatal(err)
	}
	if normalized.ReturnedCount != 1 || downstream.request.RequestID != "invocation-42" {
		t.Fatalf("result=%s request=%+v", result, downstream.request)
	}
	if downstream.request.Scope.WikiID != "wiki:test" || downstream.request.TopK != 1 {
		t.Fatalf("request=%+v", downstream.request)
	}
}

func TestValidateArgumentsRejectsInvalidScopeAndBudget(t *testing.T) {
	if err := ValidateArguments(Arguments{Query: "Barrier", Namespace: "AscendC.API", Version: "v1"}); err == nil {
		t.Fatal("expected missing wiki id to be rejected")
	}
	if err := ValidateArguments(Arguments{Query: "Barrier", WikiID: "wiki:test", Namespace: "AscendC.API", Version: "v1", Budget: "unbounded"}); err == nil {
		t.Fatal("expected invalid budget to be rejected")
	}
}
