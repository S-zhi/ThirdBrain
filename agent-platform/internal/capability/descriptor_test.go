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
