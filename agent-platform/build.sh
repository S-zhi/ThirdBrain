#!/usr/bin/env bash
RUN_NAME="thirdbrain.agent.platform"

# Generated Kitex bindings are intentionally not committed. Keep local and CI
# builds reproducible after an IDL change.
if [ ! -d kitex_gen ]; then
    command -v kitex >/dev/null 2>&1 || {
        echo "kitex is required to generate bindings" >&2
        exit 1
    }
    kitex -module github.com/S-zhi/ThirdBrain/agent-platform \
        -service thirdbrain.agent.platform idl/agent_platform.thrift
fi

mkdir -p output/bin
cp script/* output/
chmod +x output/bootstrap.sh

if [ "$IS_SYSTEM_TEST_ENV" != "1" ]; then
    go build -o output/bin/${RUN_NAME}
else
    go test -c -covermode=set -o output/bin/${RUN_NAME} -coverpkg=./...
fi
