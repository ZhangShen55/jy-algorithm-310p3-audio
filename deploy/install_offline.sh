#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Seacraft ASR 目标机一键安装脚本（离线宿主部署，无 Docker）
#
# 用法（必须在 release/ 目录里运行）：
#   sudo bash install_offline.sh                 # 全新安装
#   sudo bash install_offline.sh --upgrade-app   # 只替换业务 tar（env/sherpa 不动）
#   sudo bash install_offline.sh --uninstall     # 卸载（保留 /opt/seacraft，保险）
#
# 目标布局：
#   /opt/seacraft/
#     ├── env/       ← asr-env-310P3-arm64.tar.gz       (conda-pack Python 运行时)
#     ├── app/       ← seacraft-app-*.tar.gz            (编译后的 .so + config.toml)
#     │   ├── app/...*.so
#     │   ├── run.py
#     │   ├── config.toml
#     │   ├── launch.sh
#     │   ├── logs/
#     │   └── tmp_audio/
#     └── sherpa/    ← sherpa-onnx.tar                  (CLI + 模型)
#         ├── bin/
#         └── model/
# -----------------------------------------------------------------------------
set -euo pipefail

# ====== 常量 ======
INSTALL_ROOT="/opt/seacraft"
SERVICE_NAME="seacraft-asr.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"
WRAPPER_DST="/usr/local/bin/seacraft"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_TAR_GLOB="${SCRIPT_DIR}/asr-env-*.tar.gz"
APP_TAR_GLOB="${SCRIPT_DIR}/seacraft-app-*.tar.gz"
SHERPA_TAR="${SCRIPT_DIR}/sherpa-onnx.tar"
SERVICE_SRC="${SCRIPT_DIR}/seacraft-asr.service"
WRAPPER_SRC="${SCRIPT_DIR}/seacraft"
MANIFEST="${SCRIPT_DIR}/MANIFEST.txt"

MODE="install"
for arg in "$@"; do
    case "${arg}" in
        --upgrade-app) MODE="upgrade-app" ;;
        --uninstall)   MODE="uninstall" ;;
        -h|--help)
            sed -n '3,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: ${arg}"; exit 1 ;;
    esac
done

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root / sudo 执行"

# ====== 卸载分支 ======
if [ "${MODE}" = "uninstall" ]; then
    log "停止并禁用服务"
    systemctl stop    "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${SERVICE_DST}" "${WRAPPER_DST}"
    systemctl daemon-reload
    warn "已卸载 systemd 单元与 seacraft 命令；${INSTALL_ROOT} 保留（如需彻底清理：rm -rf ${INSTALL_ROOT}）"
    exit 0
fi

# ====== 解析产物文件 ======
# shellcheck disable=SC2086
ENV_TAR=$(ls -1t ${ENV_TAR_GLOB} 2>/dev/null | head -1 || true)
# shellcheck disable=SC2086
APP_TAR=$(ls -1t ${APP_TAR_GLOB} 2>/dev/null | head -1 || true)

[ -n "${APP_TAR}" ] && [ -f "${APP_TAR}" ]       || die "没找到 seacraft-app-*.tar.gz"
if [ "${MODE}" = "install" ]; then
    [ -n "${ENV_TAR}" ] && [ -f "${ENV_TAR}" ]   || die "没找到 asr-env-*.tar.gz"
    [ -f "${SHERPA_TAR}" ]                        || die "没找到 sherpa-onnx.tar"
    [ -f "${SERVICE_SRC}" ]                       || die "没找到 seacraft-asr.service"
    [ -f "${WRAPPER_SRC}" ]                       || die "没找到 seacraft (wrapper)"
fi

# ====== MANIFEST 校验（可选） ======
if [ -f "${MANIFEST}" ] && command -v sha256sum >/dev/null; then
    log "0/8 校验 MANIFEST.txt（sha256）"
    pushd "${SCRIPT_DIR}" >/dev/null
    bad=0
    while read -r fname size sha; do
        [ -z "${fname}" ] && continue
        [ -f "${fname}" ] || continue
        got=$(sha256sum "${fname}" | cut -d' ' -f1)
        if [ "${got}" != "${sha}" ]; then
            warn "${fname} sha256 不匹配"
            bad=1
        fi
    done < <(grep -E '\.tar(\.gz)?' "${MANIFEST}" | awk '{print $1, $2, $4}')
    [ "${bad}" -eq 0 ] && echo "    校验通过" || die "产物完整性校验失败"
    popd >/dev/null
fi

# ====== 如果服务已存在：先停 ======
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    log "检测到服务正在运行，先停止"
    systemctl stop "${SERVICE_NAME}"
fi

# ====== install 分支：铺 env + sherpa ======
if [ "${MODE}" = "install" ]; then
    log "1/8 创建目录 ${INSTALL_ROOT}"
    mkdir -p "${INSTALL_ROOT}"

    log "2/8 解压 Python 运行时 -> ${INSTALL_ROOT}/env"
    rm -rf "${INSTALL_ROOT}/env"
    mkdir -p "${INSTALL_ROOT}/env"
    tar xzf "${ENV_TAR}" -C "${INSTALL_ROOT}/env"

    log "3/8 执行 conda-unpack（修正硬编码路径）"
    if [ -x "${INSTALL_ROOT}/env/bin/conda-unpack" ]; then
        "${INSTALL_ROOT}/env/bin/conda-unpack"
    else
        warn "conda-unpack 不存在，跳过（如果运行报路径错误，请手动修）"
    fi

    log "4/8 解压 sherpa-onnx -> ${INSTALL_ROOT}/sherpa"
    rm -rf "${INSTALL_ROOT}/sherpa"
    mkdir -p "${INSTALL_ROOT}/sherpa"
    # 先试着直接解到 sherpa/，如果 tar 里本身带 sherpa-onnx/ 顶层，再往下平一层
    tar xf "${SHERPA_TAR}" -C "${INSTALL_ROOT}/sherpa"
    # 如果解出来是 /opt/seacraft/sherpa/sherpa-onnx/bin 这种双层，拍平
    if [ -d "${INSTALL_ROOT}/sherpa/sherpa-onnx/bin" ] && [ ! -d "${INSTALL_ROOT}/sherpa/bin" ]; then
        log "   检测到顶层目录嵌套，拍平一层"
        mv "${INSTALL_ROOT}/sherpa/sherpa-onnx/"* "${INSTALL_ROOT}/sherpa/"
        rmdir "${INSTALL_ROOT}/sherpa/sherpa-onnx"
    fi
    [ -d "${INSTALL_ROOT}/sherpa/bin" ]   || warn "sherpa/bin 未找到，请核对 sherpa-onnx.tar 结构"
    [ -d "${INSTALL_ROOT}/sherpa/model" ] || warn "sherpa/model 未找到，请核对 sherpa-onnx.tar 结构"
fi

# ====== 业务 app 解压 ======
log "5/8 解压业务 -> ${INSTALL_ROOT}/app"
rm -rf "${INSTALL_ROOT}/app"
mkdir -p "${INSTALL_ROOT}/app"
tar xzf "${APP_TAR}" -C "${INSTALL_ROOT}/app"
mkdir -p "${INSTALL_ROOT}/app/logs" "${INSTALL_ROOT}/app/tmp_audio"

# ====== 改 config.toml 中 working_dir ======
log "6/8 修正 config.toml 中 working_dir -> ${INSTALL_ROOT}/sherpa"
CONFIG="${INSTALL_ROOT}/app/config.toml"
[ -f "${CONFIG}" ] || die "${CONFIG} 不存在"
# 把所有 working_dir = "..." 替换成 /opt/seacraft/sherpa
sed -i -E "s|^working_dir *= *\".*\"|working_dir = \"${INSTALL_ROOT}/sherpa\"|g" "${CONFIG}"
echo "    修改后效果："
grep -n '^working_dir' "${CONFIG}" | sed 's/^/      /'

# ====== 生成 launch.sh（注入 CANN 环境） ======
log "7/8 生成 launch.sh 启动包装"
cat > "${INSTALL_ROOT}/app/launch.sh" <<'LAUNCH'
#!/usr/bin/env bash
# 由 install_offline.sh 自动生成。请勿手动修改，改完会被下次安装覆盖。
set -e

INSTALL_ROOT="/opt/seacraft"

# 1) 加载 Ascend CANN 运行时（宿主机必须已装 CANN）
for f in \
    /usr/local/Ascend/ascend-toolkit/set_env.sh \
    /usr/local/Ascend/nnae/set_env.sh ; do
    [ -f "$f" ] && source "$f"
done

# 2) sherpa-onnx 动态库（libonnxruntime 常在 _deps 下，不在 lib/）
SHERPA_LIB="${INSTALL_ROOT}/sherpa/lib"
ONNXRT_LIB="${INSTALL_ROOT}/sherpa/_deps/onnxruntime-src/lib"
if [ -d "${SHERPA_LIB}" ] || [ -d "${ONNXRT_LIB}" ]; then
    _lp=""
    [ -d "${SHERPA_LIB}" ] && _lp="${SHERPA_LIB}"
    [ -d "${ONNXRT_LIB}" ] && _lp="${_lp:+${_lp}:}${ONNXRT_LIB}"
    export LD_LIBRARY_PATH="${_lp}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi

# 3) conda-pack 环境：不走 conda activate，直接用 PATH
export PATH="${INSTALL_ROOT}/env/bin:$PATH"
# 防止被宿主 PYTHONPATH 污染
unset PYTHONPATH PYTHONHOME

cd "${INSTALL_ROOT}/app"
exec "${INSTALL_ROOT}/env/bin/python" run.py
LAUNCH
chmod +x "${INSTALL_ROOT}/app/launch.sh"

# ====== 落 systemd 和 wrapper，启动 ======
log "8/8 注册 systemd + 安装 seacraft 命令"
install -m 0644 "${SERVICE_SRC}" "${SERVICE_DST}"
install -m 0755 "${WRAPPER_SRC}" "${WRAPPER_DST}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl start  "${SERVICE_NAME}"

# ====== 冒烟 ======
port=$(awk -F' *= *' '/^\[server\]/{s=1;next} /^\[/{s=0} s&&/^port/{gsub(/"/,"",$2);print $2;exit}' "${CONFIG}")
port="${port:-8081}"
echo
log "等待服务就绪（最多 20 秒）"
ok=0
for i in $(seq 1 20); do
    if curl -fsS --max-time 1 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
        ok=1; break
    fi
    sleep 1
done

echo
if [ "${ok}" -eq 1 ]; then
    log "服务健康 (http://127.0.0.1:${port}/healthz)"
    curl -fsS "http://127.0.0.1:${port}/healthz" && echo
    echo
    echo "==== 常用运维命令 ===="
    echo "  seacraft status        查看服务状态"
    echo "  seacraft logs -f       实时日志"
    echo "  seacraft restart       重启"
    echo "  seacraft health        健康检查"
    echo "  seacraft edit-config   编辑配置"
    echo "======================="
else
    warn "服务 20 秒内未就绪，请查看日志：  journalctl -u ${SERVICE_NAME} -n 200 --no-pager"
    systemctl status "${SERVICE_NAME}" --no-pager -l | head -30 || true
    exit 1
fi
