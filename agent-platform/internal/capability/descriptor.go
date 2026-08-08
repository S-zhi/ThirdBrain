// Package capability contains compile-time declarations for Agent capabilities.
// It is intentionally not a dynamic registry or a second source of business logic.
package capability

import "time"

const KnowledgeRetrieveContextV1ID = "knowledge.retrieve_context.v1"

const APIDocRetrievalV1ID = "cap.api_doc.retrieval.v1"

// Descriptor records the governance contract for one statically wired capability.
type Descriptor struct {
	ID                  string
	Category            string
	Version             string
	Compatibility       string
	InvocationMode      string
	Risk                string
	InputContractOwner  string
	OutputContractOwner string
	InputSchema         string
	OutputSchema        string
	VersionStrategy     string
	Timeout             time.Duration
	Dependency          string
	Permission          string
	RedactedFields      [3]string
}

var knowledgeRetrieveContextV1 = Descriptor{
	ID:                  KnowledgeRetrieveContextV1ID,
	Category:            "knowledge_and_raw_rag_retrieval",
	Version:             "v1",
	Compatibility:       "Agent Platform v1; Python Core private data API v1",
	InvocationMode:      "online/synchronous",
	Risk:                "read-only/low-impact",
	InputContractOwner:  "Agent Platform Kitex IDL",
	OutputContractOwner: "Python Core QueryKnowledgeResult",
	InputSchema:         "idl/agent_platform.thrift#ExecuteKnowledgeAssistRequest",
	OutputSchema:        "src.knowledge.QueryKnowledgeResult",
	VersionStrategy:     "breaking input changes require a new Kitex method or v2 capability ID",
	Timeout:             30 * time.Second,
	Dependency:          "Python Core /internal/v1/agent-data/retrieval/context",
	Permission:          "core.private.retrieval.read",
	RedactedFields:      [3]string{"x-core-service-key", "x-agent-platform-key", "authorization"},
}

// KnowledgeRetrieveContextV1 returns a copy of the sole online v1 allowlisted declaration.
func KnowledgeRetrieveContextV1() Descriptor {
	return knowledgeRetrieveContextV1
}

// APIDocRetrievalV1 returns the statically allowlisted API document lookup.
func APIDocRetrievalV1() Descriptor {
	return Descriptor{
		ID:                  APIDocRetrievalV1ID,
		Category:            "api_doc_retrieval",
		Version:             "v1.0.0",
		Compatibility:       "Agent Platform v1; Python Core private retrieval API v1",
		InvocationMode:      "online/synchronous",
		Risk:                "read-only",
		InputContractOwner:  "THRI-240 §5.1",
		OutputContractOwner: "Agent Platform adapter",
		InputSchema:         "schemas/cap.api_doc.retrieval.v1.input.json",
		OutputSchema:        "schemas/cap.api_doc.retrieval.v1.output.json",
		VersionStrategy:     "breaking changes require a new capability ID",
		Timeout:             30 * time.Second,
		Dependency:          "knowledge.query",
		Permission:          "knowledge.query.read",
		RedactedFields:      [3]string{"authorization", "x-agent-platform-key", "query"},
	}
}
