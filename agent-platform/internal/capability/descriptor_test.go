package capability

import "testing"

func TestKnowledgeRetrieveContextV1Declaration(t *testing.T) {
	t.Parallel()
	descriptor := KnowledgeRetrieveContextV1()
	if descriptor.ID != KnowledgeRetrieveContextV1ID {
		t.Fatalf("capability id = %q", descriptor.ID)
	}
	if descriptor.Risk != "read-only/low-impact" {
		t.Fatalf("risk = %q", descriptor.Risk)
	}
	if descriptor.InputContractOwner == "" || descriptor.OutputContractOwner == "" {
		t.Fatal("contract owners must be explicit")
	}
}

func TestAPIDocAgentV1Declaration(t *testing.T) {
	t.Parallel()
	descriptor := APIDocAgentV1()
	if descriptor.ID != APIDocAgentV1ID {
		t.Fatalf("capability id = %q", descriptor.ID)
	}
	if descriptor.Dependency != "tool.api_doc.retrieve.v1" || descriptor.InputSchema == "" || descriptor.OutputSchema == "" {
		t.Fatalf("descriptor = %+v", descriptor)
	}
	if descriptor.Risk != "read-only" || descriptor.Timeout <= 0 {
		t.Fatalf("descriptor = %+v", descriptor)
	}
}
