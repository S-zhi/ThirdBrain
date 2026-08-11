// Package apidoctool exposes API document retrieval as a bounded Eino tool.
// The tool owns no retrieval logic; it delegates to the existing adapter.
package apidoctool

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"

	coreapidoc "github.com/S-zhi/ThirdBrain/agent-platform/internal/apidoc"
	"github.com/cloudwego/eino/components/tool"
	"github.com/cloudwego/eino/schema"
)

const Name = "tool.api_doc.retrieve.v1"

type Arguments struct {
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

type Tool struct {
	adapter *coreapidoc.Adapter
}

func New(adapter *coreapidoc.Adapter) *Tool { return &Tool{adapter: adapter} }

func (t *Tool) Info(context.Context) (*schema.ToolInfo, error) {
	return &schema.ToolInfo{
		Name: Name,
		Desc: "Retrieve verified API documentation for the exact wiki, namespace, and version. Always use this tool before answering API facts.",
		ParamsOneOf: schema.NewParamsOneOfByParams(map[string]*schema.ParameterInfo{
			"query":            {Type: schema.String, Desc: "The API question or lookup phrase.", Required: true},
			"wiki_id":          {Type: schema.String, Desc: "The exact Knowledge Wiki identifier.", Required: true},
			"namespace":        {Type: schema.String, Desc: "The exact API namespace.", Required: true},
			"version":          {Type: schema.String, Desc: "The exact API version.", Required: true},
			"language":         {Type: schema.String, Desc: "Optional response language."},
			"max_results":      {Type: schema.Integer, Desc: "Maximum number of evidence items, from 1 to 20."},
			"top_k":            {Type: schema.Integer, Desc: "Optional downstream retrieval count, from 1 to 50."},
			"budget":           {Type: schema.String, Desc: "Optional retrieval budget.", Enum: []string{"micro", "small", "medium", "large"}},
			"include_stale":    {Type: schema.Boolean, Desc: "Whether stale knowledge may be included."},
			"expand_relations": {Type: schema.Boolean, Desc: "Whether related entries may be expanded."},
			"relation_limit":   {Type: schema.Integer, Desc: "Maximum related entries, from 0 to 20."},
		}),
	}, nil
}

func (t *Tool) InvokableRun(ctx context.Context, argumentsInJSON string, _ ...tool.Option) (string, error) {
	if t.adapter == nil {
		return "", fmt.Errorf("api document retrieval tool is unavailable")
	}
	var args Arguments
	if err := json.Unmarshal([]byte(argumentsInJSON), &args); err != nil {
		return "", fmt.Errorf("invalid tool arguments: %w", err)
	}
	if err := ValidateArguments(args); err != nil {
		return "", err
	}
	payload, err := json.Marshal(args)
	if err != nil {
		return "", fmt.Errorf("marshal tool arguments: %w", err)
	}
	result, err := t.adapter.Invoke(ctx, payload, InvocationID(ctx))
	if err != nil {
		return "", err
	}
	return string(result), nil
}

func ValidateArguments(args Arguments) error {
	if strings.TrimSpace(args.Query) == "" || utf8.RuneCountInString(args.Query) > 512 {
		return fmt.Errorf("query must contain 1..512 characters")
	}
	if strings.TrimSpace(args.WikiID) == "" || strings.TrimSpace(args.Namespace) == "" || strings.TrimSpace(args.Version) == "" {
		return fmt.Errorf("wiki_id, namespace, and version are required")
	}
	if args.MaxResults < 0 || args.MaxResults > 20 || args.TopK < 0 || args.TopK > 50 {
		return fmt.Errorf("max_results must be 1..20 and top_k must be 1..50")
	}
	if args.RelationLimit < 0 || args.RelationLimit > 20 {
		return fmt.Errorf("relation_limit must be 0..20")
	}
	if args.Budget != "" && args.Budget != "micro" && args.Budget != "small" && args.Budget != "medium" && args.Budget != "large" {
		return fmt.Errorf("budget is invalid")
	}
	return nil
}

type invocationIDKey struct{}

func WithInvocationID(ctx context.Context, invocationID string) context.Context {
	return context.WithValue(ctx, invocationIDKey{}, invocationID)
}

func InvocationID(ctx context.Context) string {
	if value, ok := ctx.Value(invocationIDKey{}).(string); ok && value != "" {
		return value
	}
	return "unknown-invocation"
}
