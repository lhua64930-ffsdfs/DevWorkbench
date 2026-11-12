#!/bin/sh
set -eu

case "$1" in
    amd64) go_sha=42d158b4d8f7b61ac0a830567c940a86098fb7aac52e467a5ebec03ef5cc2f8d ;;
    arm64) go_sha=4a97373d49fcacdcf3694fea368a500b00ee3e963974f3e7514132717632f052 ;;
    *) echo "Unsupported Milvus target architecture: $1" >&2; exit 1 ;;
esac

mkdir -p /src/milvus /src/storage /src/include/rocksdb /src/include/librdkafka /src/pkgconfig /out
cd /src
curl -fsSL --retry 3 -o go.tar.gz "https://go.dev/dl/go1.26.9.linux-$1.tar.gz"
printf '%s  %s\n' "$go_sha" go.tar.gz | sha256sum -c -
tar -xzf go.tar.gz -C /usr/local
export PATH="/usr/local/go/bin:$PATH"

# Match the Milvus and native libraries in the pinned official image.
commit=af15c807513eb6508ae22d0f3a6484e8937be1a5
curl -fsSL --retry 3 -o milvus.tar.gz "https://codeload.github.com/milvus-io/milvus/tar.gz/$commit"
curl -fsSL --retry 3 -o storage.tar.gz https://codeload.github.com/milvus-io/milvus-storage/tar.gz/f38765d
curl -fsSL --retry 3 -o include/rocksdb/c.h https://raw.githubusercontent.com/facebook/rocksdb/refs/tags/v6.29.5/include/rocksdb/c.h
curl -fsSL --retry 3 -o include/librdkafka/rdkafka.h https://raw.githubusercontent.com/confluentinc/librdkafka/v2.6.1/src/rdkafka.h
curl -fsSL --retry 3 -o common.tar.gz https://codeload.github.com/zilliztech/milvus-common/tar.gz/78f2c29
curl -fsSL --retry 3 -o jemalloc.tar.bz2 https://github.com/jemalloc/jemalloc/releases/download/5.2.1/jemalloc-5.2.1.tar.bz2
curl -fsSL --retry 3 -o include/librdkafka/rdkafka_mock.h https://raw.githubusercontent.com/confluentinc/librdkafka/v2.6.1/src/rdkafka_mock.h
sha256sum -c <<'EOF'
2d4d52168b669257fbe936554af6af543fa0fa4f2cccc5a201a2c7d301dad500  include/librdkafka/rdkafka_mock.h
34330e5ce276099e2e8950d9335db5a875689a4c6a56751ef3b1d8c537f887f6  jemalloc.tar.bz2
ab8a74befc827d1098064dce6a7f32e9f6f2f088e5d03c8d6b9bf0b92b5d65be  common.tar.gz
fbafa4e11afa853f8380b1693a4c70c6e6e3bbf3f0bb2716f466dc7aaff76f8a  milvus.tar.gz
e66acc50596c8633865bef259d4933a027938df60b2b23707681689abfb572bf  storage.tar.gz
02cd268bf1d144bc937b2c1e53c7ed80b50a7be8fef27ab2cbbe444ddce7ac72  include/rocksdb/c.h
981210446c732d19e1de1feced64899fbef8daaf881c2bc7e2c1ac67953da970  include/librdkafka/rdkafka.h
EOF
tar -xzf milvus.tar.gz -C /src/milvus --strip-components=1
tar -xzf storage.tar.gz -C /src/storage --strip-components=1

mkdir /src/common
tar -xzf common.tar.gz -C /src/common --strip-components=1
mkdir /src/jemalloc
tar -xjf jemalloc.tar.bz2 -C /src/jemalloc --strip-components=1
(cd /src/jemalloc && ./configure --libdir=/milvus/lib --includedir=/src/jemalloc/include >/dev/null)
cd /src/milvus
# The reused embedded etcd client may already be canceled during shutdown.
file=internal/util/dependency/kv/kv_client_handler.go
grep -Fq 'err := clientCreator.client.Close()' "$file"
sed -i '/^import (/a\	"context"\n\	"errors"' "$file"
sed -i '/err := clientCreator.client.Close()/ { n; s/if err != nil {/if err != nil \&\& !errors.Is(err, context.Canceled) {/; }' "$file"
grep -Fq 'if err != nil && !errors.Is(err, context.Canceled) {' "$file"
sed -e 's|@CMAKE_INSTALL_FULL_LIBDIR@|/milvus/lib|g' \
    -e 's|@CMAKE_INSTALL_FULL_INCLUDEDIR@|/src/milvus/internal/core/src|g' \
    -e 's|@MILVUS_VERSION@|3.0.5|g' internal/core/src/milvus_core.pc.in > /src/pkgconfig/milvus_core.pc
sed -e 's|@CMAKE_INSTALL_FULL_LIBDIR@|/milvus/lib|g' \
    -e 's|@CMAKE_INSTALL_FULL_INCLUDEDIR@|/src/storage/cpp/include|g' \
    -e 's|@MILVUS_VERSION@|3.0.5|g' internal/core/thirdparty/milvus-storage/milvus-storage.pc.in > /src/pkgconfig/milvus-storage.pc
sed -e 's|@CMAKE_INSTALL_FULL_LIBDIR@|/milvus/lib|g' \
    -e 's|@CMAKE_INSTALL_FULL_INCLUDEDIR@|/src/include|g' \
    -e 's|@ROCKSDB_VERSION@|6.29.5|g' internal/core/thirdparty/rocksdb/rocksdb.pc.in > /src/pkgconfig/rocksdb.pc
sed -e 's|@CMAKE_INSTALL_FULL_LIBDIR@|/milvus/lib|g' \
    -e 's|@CMAKE_INSTALL_FULL_INCLUDEDIR@|/src/include|g' \
    -e 's|@RDKAFKA_VERSION@|2.6.1|g' internal/core/thirdparty/rdkafka/rdkafka.pc.in > /src/pkgconfig/rdkafka.pc
export PKG_CONFIG_PATH=/src/pkgconfig:/src/jemalloc
export CGO_ENABLED=1 CGO_CFLAGS="-I/src/milvus/internal/core/src -I/src/common/include -I/src/include -I/src/storage/cpp/include"
export CGO_LDFLAGS="-L/milvus/lib -Wl,-rpath-link,/milvus/lib"
export GOTOOLCHAIN=local GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org GOMAXPROCS=4

go get github.com/andybalholm/brotli@v1.2.7 github.com/go-jose/go-jose/v4@v4.1.6 \
    github.com/klauspost/compress@v1.20.2 golang.org/x/net@v0.62.0
prefix=github.com/milvus-io/milvus/cmd/milvus
go build -p 4 -mod=readonly -pgo=configs/pgo/default.pgo \
    -tags dynamic,sonic,with_jemalloc,bytedance_tango \
    -ldflags "-checklinkname=0 -r /milvus/lib -X $prefix.BuildTags=v3.0.5-codepilot1 -X $prefix.GitCommit=$commit -X $prefix.MilvusVersion=3.0.5" \
    -o /out/milvus ./cmd/main.go
# The upstream image also links a separate Go plan parser with its own modules.
go build -p 4 -mod=readonly -buildvcs=false -buildmode=c-shared \
    -o /out/libmilvus-planparser.so ./internal/parser/planparserv2/cwrapper
go version -m /out/milvus > /out/milvus.modules
grep -F "$(printf '\tgithub.com/andybalholm/brotli\tv1.2.7\t')" /out/milvus.modules
grep -F "$(printf '\tgithub.com/go-jose/go-jose/v4\tv4.1.6\t')" /out/milvus.modules
grep -F "$(printf '\tgithub.com/klauspost/compress\tv1.20.2\t')" /out/milvus.modules
grep -F "$(printf '\tgolang.org/x/net\tv0.62.0\t')" /out/milvus.modules
go version -m /out/libmilvus-planparser.so > /out/planparser.modules
grep -F "$(printf '\tgithub.com/klauspost/compress\tv1.20.2\t')" /out/planparser.modules
