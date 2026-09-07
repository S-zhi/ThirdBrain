package agent

import (
	"context"
	"time"

	"github.com/cloudwego/eino-ext/components/model/openai"
	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/schema"
)

type OpenAIModelConfig struct {
	APIKey  string
	BaseURL string
	Model   string
	Timeout time.Duration
}

func NewOpenAIModel(ctx context.Context, config OpenAIModelConfig) (model.ToolCallingChatModel, error) {
	return openai.NewChatModel(ctx, &openai.ChatModelConfig{
		APIKey:  config.APIKey,
		BaseURL: config.BaseURL,
		Model:   config.Model,
		Timeout: config.Timeout,
	})
}

// forcedToolModel guarantees that the first model turn cannot skip evidence
// retrieval. After a tool result is present, the model is free to synthesize
// the final answer.
type forcedToolModel struct {
	model.ToolCallingChatModel
	toolName string
}

func newForcedToolModel(base model.ToolCallingChatModel, toolName string) model.ToolCallingChatModel {
	return &forcedToolModel{ToolCallingChatModel: base, toolName: toolName}
}

func (m *forcedToolModel) WithTools(tools []*schema.ToolInfo) (model.ToolCallingChatModel, error) {
	bound, err := m.ToolCallingChatModel.WithTools(tools)
	if err != nil {
		return nil, err
	}
	return &forcedToolModel{ToolCallingChatModel: bound, toolName: m.toolName}, nil
}

func (m *forcedToolModel) Generate(ctx context.Context, input []*schema.Message, opts ...model.Option) (*schema.Message, error) {
	if !hasToolResult(input) {
		opts = append(opts, model.WithToolChoice(schema.ToolChoiceForced, m.toolName))
	}
	return m.ToolCallingChatModel.Generate(ctx, input, opts...)
}

func (m *forcedToolModel) Stream(ctx context.Context, input []*schema.Message, opts ...model.Option) (*schema.StreamReader[*schema.Message], error) {
	if !hasToolResult(input) {
		opts = append(opts, model.WithToolChoice(schema.ToolChoiceForced, m.toolName))
	}
	return m.ToolCallingChatModel.Stream(ctx, input, opts...)
}

func hasToolResult(messages []*schema.Message) bool {
	for _, message := range messages {
		if message != nil && message.Role == schema.Tool && message.ToolCallID != "" {
			return true
		}
	}
	return false
}
