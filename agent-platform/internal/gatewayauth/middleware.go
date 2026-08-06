// Package gatewayauth protects the Core-to-Agent Platform Kitex boundary.
package gatewayauth

import (
	"context"
	"crypto/subtle"
	"errors"

	"github.com/bytedance/gopkg/cloud/metainfo"
	"github.com/cloudwego/kitex/pkg/endpoint"
)

// CoreServiceKeyMeta is the Kitex metainfo key Core must set on each call.
const CoreServiceKeyMeta = "x-core-service-key"

var errUnauthorized = errors.New("unauthorized Core service caller")

// CoreServiceAuth rejects calls that do not carry the configured Core service key.
func CoreServiceAuth(expectedKey string) endpoint.Middleware {
	return func(next endpoint.Endpoint) endpoint.Endpoint {
		return func(ctx context.Context, request, response interface{}) error {
			providedKey, ok := metainfo.GetValue(ctx, CoreServiceKeyMeta)
			if !ok || subtle.ConstantTimeCompare([]byte(providedKey), []byte(expectedKey)) != 1 {
				return errUnauthorized
			}
			return next(ctx, request, response)
		}
	}
}
