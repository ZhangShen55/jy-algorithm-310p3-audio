#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Seacraft ASR 一条龙发布脚本（构建机使用）
#
# 流程：
#   0. 环境检查（conda / Cython / 外部 tar）
#   1. 重建 dist-build/（干净拷贝源码进去）
#   2. Cython 编译 app/**.py -> .so
#   3. 清理源码（.py / .c / __pycache__ / build/ / setup.py）
#   4. 打业务 tar:            release/seacraft-app-${VERSION}.tar.gz
#   5. 汇总外部 tar 到 release/:
#        - asr-env-310P3-arm64.tar.gz    (conda-pack 打的 Python 运行时)
#        - sherpa-onnx.tar               (sherpa CLI + 模型)
#   6. 生成 MANIFEST.txt（文件大小 + sha256）
#
# 用法：
#   bash deploy/build_release.sh              # 默认版本号 1.1.8
#   VERSION=1.2.0 bash deploy/build_release.sh
#   CONDA_ENV=asr bash deploy/build_release.sh
# -----------------------------------------------------------------------------
set -euo pipefail

# ====== 配置 ======
VERSION="${VERSION:-1.1.8}"
CONDA_ENV="${CONDA_ENV:-asr}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="${REPO_ROOT}/dist-build"
RELEASE_DIR="${REPO_ROOT}/release"

ENV_TAR="${REPO_ROOT}/asr-env-310P3-arm64.tar.gz"
SHERPA_TAR="${REPO_ROOT}/sherpa-onnx.tar"

# ====== 彩色输出 ======
log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

# ====== 0. 环境检查 ======
log "0/6 环境检查"
command -v conda >/dev/null || die "未找到 conda，请先安装 miniconda/anaconda"

# shellcheck disable=SC1091
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate "${CONDA_ENV}" 2>/dev/null || die "conda 环境 ${CONDA_ENV} 不存在"

python -V
python -c "import Cython" 2>/dev/null || {
    warn "Cython 未安装到 ${CONDA_ENV} 环境，现在安装…"
    pip install --quiet cython
}

[ -f "${ENV_TAR}"    ] || die "缺少运行环境 tar: ${ENV_TAR}"
[ -f "${SHERPA_TAR}" ] || die "缺少 sherpa-onnx tar: ${SHERPA_TAR}"

# 防呆：sherpa-onnx.tar 不能是 0 字节占位
sherpa_size=$(stat -c %s "${SHERPA_TAR}")
if [ "${sherpa_size}" -lt 1024 ]; then
    die "sherpa-onnx.tar 大小 ${sherpa_size} 字节，看起来是占位文件，请先放入真实产物"
fi

# ====== 1. 重建 dist-build ======
log "1/6 重建 ${BUILD_DIR}"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"
cp -r  "${REPO_ROOT}/app"         "${BUILD_DIR}/"
cp     "${REPO_ROOT}/run.py"      "${BUILD_DIR}/"
cp     "${REPO_ROOT}/config.toml" "${BUILD_DIR}/"
cp     "${REPO_ROOT}/setup.py"    "${BUILD_DIR}/"

# ====== 2. Cython 编译 ======
log "2/6 Cython 编译"
pushd "${BUILD_DIR}" >/dev/null
python setup.py build_ext --inplace 2>&1 | \
    grep -E '^\[[0-9]+/[0-9]+\] Cythonizing|^error:|^Traceback' || true

so_count=$(find app -name "*.so" | wc -l)
[ "${so_count}" -gt 0 ] || die "编译后没有找到任何 .so 产物"
log "   生成 ${so_count} 个 .so"

# ====== 3. 清理源码 ======
log "3/6 清理源码（保留 __init__.py / run.py / config.toml）"
find app -type f -name "*.py" ! -name "__init__.py" -delete
find app -type f -name "*.c" -delete
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
rm -rf build
rm -f  setup.py
# 冒烟：再次 import，确认 .so 独立可用
python -c "
import app.main, app.services.asr_service, app.core.config, app.api.routes.asr, app.schemas.asr
from app.main import app as _a
assert any(getattr(r, 'path', '') == '/v1.1.8/seacraft_asr' for r in _a.routes), 'route missing'
print('[smoke] .so import OK, routes ready')
"

popd >/dev/null

# ====== 4. 打业务 tar ======
log "4/6 打业务发布 tar"
mkdir -p "${RELEASE_DIR}"
APP_TAR="${RELEASE_DIR}/seacraft-app-${VERSION}.tar.gz"
tar czf "${APP_TAR}" -C "${BUILD_DIR}" .
log "   -> $(basename "${APP_TAR}") ($(du -h "${APP_TAR}" | cut -f1))"

# ====== 5. 汇总外部 tar + 部署脚本 ======
log "5/6 汇总外部产物到 ${RELEASE_DIR}"
cp -v "${ENV_TAR}"    "${RELEASE_DIR}/"
cp -v "${SHERPA_TAR}" "${RELEASE_DIR}/"

# 部署三件套
DEPLOY_DIR="${REPO_ROOT}/deploy"
for f in install_offline.sh seacraft-asr.service seacraft; do
    [ -f "${DEPLOY_DIR}/${f}" ] || die "缺少 deploy/${f}"
    cp -v "${DEPLOY_DIR}/${f}" "${RELEASE_DIR}/"
done
chmod +x "${RELEASE_DIR}/install_offline.sh" "${RELEASE_DIR}/seacraft"

# ====== 6. 生成 MANIFEST ======
log "6/6 生成 MANIFEST.txt"
pushd "${RELEASE_DIR}" >/dev/null
{
    echo "# Seacraft ASR Release ${VERSION}"
    echo "# Built:   $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# Host:    $(uname -s) $(uname -m) $(uname -r)"
    echo "# Python:  $(python -V 2>&1)"
    echo "# glibc:   $(ldd --version | head -1)"
    echo
    echo "# name                                        size(bytes)    sha256"
    for f in $(ls -1 | grep -E '\.tar(\.gz)?$'); do
        size=$(stat -c %s "$f")
        sha=$(sha256sum "$f" | cut -d' ' -f1)
        printf "%-44s  %12s  %s\n" "$f" "$size" "$sha"
    done
} > MANIFEST.txt
cat MANIFEST.txt
popd >/dev/null

echo
log "发布完成。请把 ${RELEASE_DIR}/ 下所有 tar 和 MANIFEST.txt 拷到目标机。"
ls -lh "${RELEASE_DIR}"
