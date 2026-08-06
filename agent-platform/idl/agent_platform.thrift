namespace go agentplatform

enum AssistStatus {
  COMPLETED = 1,
  ABSTAINED = 2,
  FAILED = 3,
}

struct RetrievalScope {
  1: string wiki_id
  2: string namespace
  3: string version
  4: optional string language
  5: optional list<string> rag_collection_ids
}

struct AssistPolicy {
  1: optional i32 deadline_ms
  2: optional i16 max_tool_calls
}

struct ExecuteKnowledgeAssistRequest {
  1: string request_id
  2: string query
  3: RetrievalScope scope
  4: optional AssistPolicy policy
  5: optional string correlation_id
}

struct AgentTraceEvent {
  1: string stage
  2: string detail
}

struct ExecuteKnowledgeAssistResponse {
  1: string request_id
  2: AssistStatus status
  3: optional string context_packet
  4: optional list<string> warnings
  5: optional list<AgentTraceEvent> trace
  6: optional string error_code
  7: optional string error_message
}

service AgentPlatformService {
  ExecuteKnowledgeAssistResponse ExecuteKnowledgeAssist(1: ExecuteKnowledgeAssistRequest request)
}
