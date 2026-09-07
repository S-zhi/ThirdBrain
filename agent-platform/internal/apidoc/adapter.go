// Package apidoc adapts the existing Python retrieval data plane to the
// capability-level API document contract. It contains no retrieval logic.
package apidoc

import (
	"context"
	"encoding/json"
	"fmt"
	"path"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/S-zhi/ThirdBrain/agent-platform/internal/coredata"
)

const (
	defaultMaxResults = 5
	maxMaxResults     = 20
)

type Request struct {
	Query           string `json:"query"`
	WikiID          string `json:"wiki_id"`
	Namespace       string `json:"namespace"`
	Version         string `json:"version"`
	Language        string `json:"language,omitempty"`
	MaxResults      int    `json:"max_results,omitempty"`
	TopK            int    `json:"top_k,omitempty"`
	Budget          string `json:"budget,omitempty"`
	IncludeStale    *bool  `json:"include_stale,omitempty"`
	ExpandRelations *bool  `json:"expand_relations,omitempty"`
	RelationLimit   int    `json:"relation_limit,omitempty"`
}

type Result struct {
	Results       []map[string]any `json:"results"`
	ReturnedCount int              `json:"returned_count"`
}

type RetrievalTool interface {
	RetrieveContext(context.Context, coredata.RetrieveContextRequest) (coredata.RetrievalResult, error)
}

type Adapter struct{ downstream RetrievalTool }

func New(downstream RetrievalTool) *Adapter { return &Adapter{downstream: downstream} }

func (adapter *Adapter) Invoke(ctx context.Context, payload []byte, requestID string) ([]byte, error) {
	var rawRequest map[string]any
	if err := json.Unmarshal(payload, &rawRequest); err != nil {
		return nil, fmt.Errorf("invalid request: %w", err)
	}
	if err := rejectSensitive(rawRequest); err != nil {
		return nil, err
	}
	if value, exists := rawRequest["max_results"]; exists {
		if number, ok := value.(float64); !ok || number < 1 || number > maxMaxResults || number != float64(int(number)) {
			return nil, fmt.Errorf("max_results must be an integer between 1 and 20")
		}
	}
	var request Request
	if err := json.Unmarshal(payload, &request); err != nil {
		return nil, fmt.Errorf("invalid request: %w", err)
	}
	if err := validate(request); err != nil {
		return nil, err
	}
	if adapter.downstream == nil {
		return nil, fmt.Errorf("dependency unavailable")
	}
	maxResults := request.MaxResults
	if maxResults == 0 {
		maxResults = defaultMaxResults
	}
	topK := request.TopK
	if topK == 0 || topK > maxResults {
		topK = maxResults
	}
	downstreamResult, err := adapter.downstream.RetrieveContext(ctx, coredata.RetrieveContextRequest{
		RequestID: requestID,
		Query:     request.Query,
		Scope:     coredata.RetrievalScope{WikiID: request.WikiID, Namespace: request.Namespace, Version: request.Version, Language: request.Language},
		TopK:      topK, Budget: request.Budget, IncludeStale: request.IncludeStale,
		ExpandRelations: request.ExpandRelations, RelationLimit: request.RelationLimit,
	})
	if err != nil {
		return nil, err
	}
	return normalize(downstreamResult.Payload, maxResults)
}

func validate(request Request) error {
	if strings.TrimSpace(request.Query) == "" || utf8.RuneCountInString(request.Query) > 512 {
		return fmt.Errorf("query must contain 1..512 characters")
	}
	if strings.TrimSpace(request.WikiID) == "" || strings.TrimSpace(request.Namespace) == "" || strings.TrimSpace(request.Version) == "" {
		return fmt.Errorf("wiki_id, namespace, and version are required")
	}
	if request.MaxResults < 0 || request.MaxResults > maxMaxResults || request.TopK < 0 || request.TopK > 50 {
		return fmt.Errorf("max_results must be 1..20 and top_k must be 1..50")
	}
	return nil
}

func rejectSensitive(value any) error {
	if object, ok := value.(map[string]any); ok {
		for key, nested := range object {
			lower := strings.ToLower(key)
			if lower == "api_key" || lower == "password" || lower == "secret" || lower == "token" || lower == "authorization" {
				return fmt.Errorf("sensitive field %q is not allowed", key)
			}
			if err := rejectSensitive(nested); err != nil {
				return err
			}
		}
	}
	return nil
}

func normalize(payload []byte, maxResults int) ([]byte, error) {
	var source struct {
		KnowledgeHits []struct {
			Title      string  `json:"title"`
			Summary    string  `json:"summary"`
			Content    string  `json:"content"`
			Score      float64 `json:"score"`
			Provenance []struct {
				Path      string `json:"path"`
				Namespace string `json:"namespace"`
				Version   string `json:"version"`
			} `json:"provenance"`
		} `json:"knowledge_hits"`
		SourceHits []struct {
			Title      string  `json:"title"`
			Summary    string  `json:"summary"`
			Content    string  `json:"content"`
			Score      float64 `json:"score"`
			Provenance []struct {
				Path      string `json:"path"`
				Namespace string `json:"namespace"`
				Version   string `json:"version"`
			} `json:"provenance"`
		} `json:"source_hits"`
	}
	if err := json.Unmarshal(payload, &source); err != nil {
		return nil, fmt.Errorf("downstream returned invalid JSON: %w", err)
	}
	type hit struct {
		Title, SourcePath, TopicSlug, Snippet, MatchedAt string
		Score                                            float64
	}
	items := make([]hit, 0, len(source.KnowledgeHits)+len(source.SourceHits))
	appendHit := func(title, summary, content string, score float64, provenance []struct {
		Path      string `json:"path"`
		Namespace string `json:"namespace"`
		Version   string `json:"version"`
	}) error {
		if len(items) >= maxResults {
			return nil
		}
		p := ""
		ns := ""
		if len(provenance) > 0 {
			p, ns = provenance[0].Path, provenance[0].Namespace
		}
		cleanPath := path.Clean(p)
		if p == "" || cleanPath != p || path.IsAbs(p) || strings.HasPrefix(p, "http://") || strings.HasPrefix(p, "https://") || cleanPath == ".." || strings.HasPrefix(cleanPath, "../") {
			return fmt.Errorf("invalid source_path")
		}
		if score < 0 || score > 1 {
			return fmt.Errorf("invalid score")
		}
		snippet := summary
		if snippet == "" {
			snippet = content
		}
		if utf8.RuneCountInString(snippet) > 500 {
			snippet = string([]rune(snippet)[:500])
		}
		items = append(items, hit{title, p, ns, snippet, time.Now().UTC().Format(time.RFC3339), score})
		return nil
	}
	for _, item := range source.KnowledgeHits {
		if err := appendHit(item.Title, item.Summary, item.Content, item.Score, item.Provenance); err != nil {
			return nil, err
		}
	}
	for _, item := range source.SourceHits {
		if err := appendHit(item.Title, item.Summary, item.Content, item.Score, item.Provenance); err != nil {
			return nil, err
		}
	}
	results := make([]map[string]any, 0, len(items))
	for _, item := range items {
		results = append(results, map[string]any{"title": item.Title, "source_path": item.SourcePath, "topic_slug": item.TopicSlug, "snippet": item.Snippet, "score": item.Score, "matched_at": item.MatchedAt})
	}
	return json.Marshal(Result{Results: results, ReturnedCount: len(results)})
}
