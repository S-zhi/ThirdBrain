package gatewayauth

import (
	"context"
	"testing"

	"github.com/bytedance/gopkg/cloud/metainfo"
	"github.com/cloudwego/kitex/pkg/endpoint"
)

func TestCoreServiceAuth(t *testing.T) {
	t.Parallel()
	called := false
	next := endpoint.Endpoint(func(context.Context, interface{}, interface{}) error {
		called = true
		return nil
	})
	middleware := CoreServiceAuth("core-secret")(next)

	if err := middleware(context.Background(), nil, nil); err == nil {
		t.Fatal("expected missing service key to be rejected")
	}
	if called {
		t.Fatal("unauthorized call reached the handler")
	}

	ctx := metainfo.WithValue(context.Background(), CoreServiceKeyMeta, "core-secret")
	if err := middleware(ctx, nil, nil); err != nil {
		t.Fatalf("authorized call failed: %v", err)
	}
	if !called {
		t.Fatal("authorized call did not reach the handler")
	}
}
