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

struct CapabilityRequest {
  1: string capability_id
  2: string payload_json
  3: optional string trace_id
  4: optional string caller
  5: optional i32 timeout_ms
}

struct CapabilityError {
  1: string code
  2: string message
  3: bool retryable
  4: optional string cause_ref
}

struct CapabilityResponse {
  1: string capability_id
  2: string trace_id
  3: string status
  4: optional string result_json
  5: i64 elapsed_ms
  6: optional CapabilityError error
}

struct CapabilityDescriptor {
  1: string capability_id
  2: string name
  3: string module
  4: string version
  5: string status
  6: string risk_level
  7: string invocation_mode
  8: string execution_mode
  9: list<string> dependencies
  10: i32 timeout_default_ms
  11: i32 timeout_max_ms
  12: bool client_retryable
  13: string input_schema_ref
  14: string output_schema_ref
}

service AgentPlatformService {
  ExecuteKnowledgeAssistResponse ExecuteKnowledgeAssist(1: ExecuteKnowledgeAssistRequest request)
  list<CapabilityDescriptor> Discover(1: bool read_only)
  CapabilityResponse Invoke(1: CapabilityRequest request)
}
