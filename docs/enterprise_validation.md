# DevWorkbench 企业私有部署验收

截至 2026 年 11 月 10 日，重建镜像通过本地部署与恢复验证。正式放行业务流量前，还需要通过模型质量门槛、处置剩余镜像安全告警，并在目标服务器完成部署验收。

## 已验证的应用行为

| 检查 | 结果 |
| --- | --- |
| 后端回归 | 293 项通过，包括新增的检索结果参与回答复核测试。另跑 Docker 沙箱套件，4 项容器专用测试通过 |
| PostgreSQL 18.6 多实例 | 两个 API 同时收到 30 次草稿确认时，仅执行一次并写入一条审计记录。跨进程请求配额、并发槽位和仓库同步锁均通过 |
| 后台任务 | 两个 Worker 领取并完成 20 个任务。故障后租约到期，第二次领取完成恢复 |
| 访问控制 | 验证令牌到期和撤销、仓库隔离、路径与请求体仓库 ID 冲突、指标权限和请求体上限 |
| 整套容器部署 | 使用新数据卷通过初始化和健康检查。验证 PostgreSQL PID 1 的 UID 为 70，生效的 Linux capabilities 为空。使用验证证书连接 HTTPS，验证首页、令牌登录与撤销、指标鉴权，以及默认不开放 API 文档和健康端点 |
| 加密恢复 | 实际转储并恢复到空 PostgreSQL 数据库，比较 37 张表和上传文件摘要。篡改归档在写入前被拒绝，非空恢复目标被拒绝 |
| 备份镜像 | 使用 Compose 中的 PostgreSQL 18 客户端镜像创建加密归档，并恢复至独立空库，读取到迁移版本 `0003_enterprise` |
| 前端和来源 | 生产构建、5 项 lint 兼容测试通过。508 个 npm 归档的来源和完整性字段通过检查。该次 npm 审计报告 0 个已知漏洞 |

多实例验证使用独立进程和同一个隔离数据库。恢复测试使用小型合成数据。目标环境的容量、故障切换、备份窗口和恢复时间仍需现场验证。

## 真实模型评测

固定测试集包含 10 个问题，知识输入为版本化的 [CodePilot 基准文档](datas/evals/codepilot_baseline.md)。历次完整评测均使用 DashScope `text-embedding-v4`（1024 维）、Milvus 和 RAGAS 0.4.3。前两轮用 DeepSeek V4 Pro 生成与评分，Flash 轮的回答模型与评分模型均为 `deepseek-flash`。

| 指标 | Pro 基线 | Pro 直接回答 | Flash 修复证据传递后 | 门槛 |
| --- | ---: | ---: | ---: | ---: |
| 检索上下文精确度 | 0.9167 | 0.9083 | 0.9167 | 0.75 |
| 检索上下文召回率 | 1.0000 | 1.0000 | 0.9667 | 0.80 |
| 忠实度 | 0.9762 | 0.8800 | 0.9800 | 0.90 |
| 回答相关性 | 0.7102 | 0.7270 | 0.7815 | 0.80 |
| Agent 目标达成度 | 0.9000 | 0.9000 | 1.0000 | 0.80 |
| 硬性用例规则 | 10/10 | 9/10 | 10/10 | 10/10 |

前次基线对同一批回答重评了相关性，保持重构问题与回答同语种，每题生成 3 个问题。随后让回答先交代对象、条件和直接结论，并重新生成及评分全部 10 题。原题、参考答案和放行门槛沿用基线。工具调用指标缺少可评分样例，单独留空。两次整体质量结果均为 `failed`。

直接回答让数据库迁移和同步失败两题的相关性分别达到 0.8646 和 0.9769。RAG 资料范围一题暴露了证据传递缺口，检索工具找到了清单，回答复核只读取了预取片段，导致漏答。已修复证据传递，并用回归测试验证普通对话也能复核检索结果。不可回答的 SLA 问题按硬性规则正确拒答，RAGAS 的相关性分数仍为 0，计入整体均分。

证据传递修复后的一次 Pro 复跑在完成 4 题后收到 `HTTP 402 Insufficient Balance`。切换 Flash 后全部 10 题评分完成，原先漏答的 RAG 资料清单已完整回答，单题相关性为 0.9575。整体相关性 0.7815，低于 0.80 门槛，整体质量结果为 `failed`。回答模型、评分模型与证据传递都发生了变化，分数差异不能单独归因于其中一项。

另用同一套题对进一步限制回答范围的候选提示做了完整复测，相关性为 0.7525，忠实度为 0.9080，Agent 目标达成度为 0.7000，硬性规则通过 9/10。一次说明性问答在生成前额外调用了要求认证的操作风险工具，该次调用失败。该候选提示已撤回，保留相关性为 0.7815 的版本和两份原始报告 `real-ragas-flash.json`、`real-ragas-flash-focused.json`。

复跑使用独立的 PostgreSQL `*_validation` 库和隔离的 Milvus 集合。预先完成数据库迁移，并通过本地环境配置模型密钥。脚本输出逐题报告，在质量不通过时返回退出码 2。

```bash
python scripts/run_real_ragas.py --allow-isolated-database --output .data/enterprise-validation/real-ragas.json
```

### 100 题扩展验收

[100 题用例集](datas/evals/enterprise_100_cases.json)保留原 10 题，并为部署、仓库权限、CI 沙盒、知识检索、Agent 工作流、备份恢复各增加 15 题。其中 3 题测试资料缺失时的回答。扩展集索引六份版本化项目文档，和原来单文档 10 题的结果分别记录。

在隔离 PostgreSQL 与 Milvus 上将 `LLM_MODEL`、`RAGAS_JUDGE_MODEL` 都设为 `deepseek-flash`。使用独立的 Milvus 集合与输出路径运行。

```bash
python scripts/run_real_ragas.py --allow-isolated-database --suite codepilot-enterprise-100 --workers 1 --output .data/enterprise-validation/real-ragas-flash-100.json
```

脚本每完成一题保存一次检查点。中断后用同一条命令可继续，脚本会校验题集、证据文档、代码、仓库、向量配置及评分配置。修改输入后使用新的报告路径。相关性门槛保持 0.80，正式判定同时检查其他指标和硬性规则。

2026 年 11 月 10 日，本机已完成六份文档索引。真实 Flash 运行及重试均因连接 `api.deepseek.com` 超时中断。DashScope 分批修复与评测续跑通过离线回归，后端合计 294 项通过、4 项 Docker 专用测试跳过。

2026 年 11 月 11 日，官方接口恢复连通，Flash 生成和评分共完成 14/100 题。14 条记录均有相关性分数，评分无报错，硬性规则通过 13 条。SLA 题正确说明资料没有给出具体数值，但未命中基线规则要求的原词「未提供」。评测随后遇到 DeepSeek 并发限流（429）和余额不足（402），剩余 86 题待运行。续跑示例改用 1 个评测工作进程，以减少同时提交的请求。

同日以 1 个工作进程续跑，新增 9 题，检查点累计 23/100。23 题均有相关性分数，硬性规则通过 22 题。DeepSeek 在后续评分时再次返回余额不足（402），剩余 77 题待运行。

## 安全与放行条件

2026 年 11 月 11 日使用 Docker Scout 复扫六个 `enterprise-security-20261111` 发布镜像。原始报告保存在本机 `.data/enterprise-validation/*-20261111.sarif`，11 月 10 日的报告继续留存。同一公告可以出现在多个镜像的扫描报告中。

| 镜像 | 告警记录 | 严重 | 高危 |
| --- | ---: | ---: | ---: |
| 后端 | 75 | 0 | 8 |
| 前端 | 1 | 0 | 1 |
| 备份工具 | 53 | 0 | 6 |
| Nginx 网关 | 1 | 0 | 1 |
| PostgreSQL | 12 | 0 | 2 |
| Milvus | 39 | 1 | 5 |

按镜像统计，11 月 10 日到 11 月 11 日的原始扫描记录从 216 条降到 181 条，严重记录从 5 条降到 1 条，高危记录从 49 条降到 23 条。后端和备份工具本轮各新增两条 Debian Kerberos 高危记录。

- PostgreSQL 从固定摘要的官方 Alpine 镜像提取清理后的文件系统，交付镜像的唯一文件层不包含 `gosu`。复扫消除了它对应的 4 条严重和 30 条高危 Go 标准库记录。新镜像安装了 Alpine 稳定源的 `krb5-libs 1.22.3-r0` 和 `libxslt 1.1.43-r4`，对应的高危项也未出现在最终扫描。PostgreSQL 仍有一条 libxml2 高危记录。[PostgreSQL 官方入口](https://github.com/docker-library/postgres/blob/master/docker-entrypoint.sh) · [Alpine v3.24 官方安全数据库](https://secdb.alpinelinux.org/v3.24/main.json)
- 前端、网关和 PostgreSQL 构建时检查 zlib 1.4 和 musl `1.2.6-r3` 的最低版本。最终镜像实际安装的 musl 均为 `1.2.6-r3`，Alpine v3.24 官方安全数据库将该版本列为 CVE-2026-107891 的修复版本。Scout 仍在这三个镜像中报告该 CVE，原始统计保留这三条记录。[Alpine 稳定版安全公告](https://www.alpinelinux.org/posts/alpine-3.21.9-3.22.7-3.23.7-3.24.3-released/) · [Alpine v3.24 官方安全数据库](https://secdb.alpinelinux.org/v3.24/main.json)
- 后端和备份工具从 Debian 稳定版安全源更新 zlib 后，原有两条 zlib 高危告警消失。当前两个镜像各有六条 Debian Kerberos 高危记录，已确认运行镜像不包含 `krb5kdc`、`kadmind` 等服务器程序，共享库仍需跟踪修复。后端还有两条 Expat 高危告警。[Debian zlib 安全公告](https://security-tracker.debian.org/tracker/DSA-6613-1) · [Debian Kerberos 公告](https://security-tracker.debian.org/tracker/CVE-2026-112321) · [Debian Expat 公告](https://security-tracker.debian.org/tracker/CVE-2026-118370)
- Milvus 镜像保留官方二进制，将 Ubuntu APT 升级至 `2.4.15ubuntu0.1`，复扫已无 CVE-2026-109518。[Ubuntu 安全公告](https://ubuntu.com/security/CVE-2026-109518)
- 生产构建固定基础镜像摘要，Python wheel 固定版本和 SHA-256，npm 安装使用官方 registry 并关闭安装脚本。备份工具从 [PostgreSQL 官方签名源](https://www.postgresql.org/download/linux/debian/)安装客户端，密钥指纹在构建时校验。
- Python 包版本扫描仍会报告 RAGAS 和 diskcache 的上游公告。生产镜像对相关文件应用可审查补丁，启动和评测验证补丁哈希。相关攻击回归随后端测试执行。[补丁与范围](../backend/security_patches/README.md)
- 正式上线前应逐项记录高危和严重告警的受影响版本、调用路径与处理结论，并在目标环境重扫实际运行的镜像摘要。
- 接入正式仓库前，在公司目标服务器验证 DNS、企业 CA、令牌轮换、外部模型的数据出境策略、负载和告警。用代表性内部数据复跑质量评测，并用同等规模数据执行备份恢复。

### 11 月 11 日的 Milvus 严重告警

Docker Scout 将 Milvus 中的 CVE-2026-111497 标为严重，匹配的是二进制内的 Go 版 `andybalholm/brotli` 1.1.0。Go 模块维护者将其实现中的影响评为低危，畸形输入可能引发 panic 或无限循环，造成拒绝服务。维护者确认 C 实现的缓冲区溢出和远程代码执行影响在 Go 实现中不成立。Go 模块在 1.2.7 修复，Milvus 3.0.6 的官方发布源码仍声明 1.1.0。生产定义保留经验证的 3.0.5 固定摘要，原始扫描保留该条记录。[Milvus 3.0.6 依赖清单](https://github.com/milvus-io/milvus/blob/v3.0.6/go.mod) · [Go Brotli 维护者公告](https://github.com/andybalholm/brotli/security/advisories/GHSA-c9gr-gmg4-2vqv)

### 本轮部署复验

全新隔离数据卷通过 PostgreSQL 非 root 初始化、HTTPS、个人令牌撤销、受保护指标和加密归档恢复。另在临时数据卷用旧 PostgreSQL 镜像写入数据，切换到新镜像后确认原数据可读。

### Expat 安全回补

在 Debian trixie 构建阶段校验 Debian 源包哈希并重新编译 Expat 2.10.0。使用固定 Python 基础镜像对应的 python.org 源码与哈希重编译 CPython 的 `pyexpat`，将原内置 Expat 替换为动态链接的系统库。官方 CPython 的 `test_pyexpat`、`test_xml_etree` 和 `test_xml_etree_c` 三组测试均通过，报告记录 491 项和 29 项跳过。最终后端镜像确认 `pyexpat.EXPAT_VERSION` 为 `expat_2.10.0`，并链接 `libexpat.so.1`。在全新隔离卷通过 PostgreSQL 迁移和 Milvus 初始化后，API 就绪，健康检查返回 `DevWorkbench`。

Docker Scout 复扫仍将 CVE-2026-117610 和 CVE-2026-118370 标为 trixie 未修复。它扫描到的是本项目在 trixie 编译的 `2.10.0-1~deb13u0+codepilot1` 包，Expat 官方 2.10.0 已包含这两项修复。原始后端扫描记录继续保存在本机 `.data/enterprise-validation/api-expat-20261111.sarif`。[Expat 2.10.0 发布记录](https://github.com/libexpat/libexpat/releases/tag/R_2_10_0) · [Debian CVE-2026-117610](https://security-tracker.debian.org/tracker/CVE-2026-117610) · [Debian CVE-2026-118370](https://security-tracker.debian.org/tracker/CVE-2026-118370)

### 11 月 12 日镜像复验

在 Linux amd64 上重建后端、前端、备份、PostgreSQL 和 Milvus，并为网关复用同一已验证的镜像摘要。六个镜像使用统一标签 `enterprise-security-20261112`。Docker Scout 原始记录从 181 条降到 163 条，严重从 1 条降到 0 条，高危从 23 条降到 19 条。六份原始报告保存在本机 `.data/enterprise-validation/*-20261112.sarif`。

使用统一发布标签在全新隔离卷上启动完整服务，HTTPS 首页显示 DevWorkbench，未认证请求收到 HTTP 401，Worker 和 Scheduler 均启动。关闭该服务组合时 Milvus 退出码为 0，测试容器与测试卷已清理。

| 镜像 | 原始告警 | 严重 | 高危 |
| --- | ---: | ---: | ---: |
| 后端 | 75 | 0 | 8 |
| 前端 | 1 | 0 | 1 |
| 备份 | 53 | 0 | 6 |
| 网关 | 1 | 0 | 1 |
| PostgreSQL | 1 | 0 | 1 |
| Milvus | 32 | 0 | 2 |

- 后端和备份镜像使用 MIT Kerberos 1.22.3 源码在 Debian trixie 重建运行库。新增符号 `k5_ser_unpack_len` 已记入 Debian 包元数据，构建保留严格的符号检查。后端确认加载了 1.22.3 的 Kerberos 库及 Expat 2.10.0，隔离环境数据库迁移、加密备份和空库恢复通过。[MIT Kerberos 1.22.3 发布说明](https://web.mit.edu/kerberos/krb5-1.22/)
- PostgreSQL 镜像用 Alpine 打包配方重建 libxml2 2.13.9，并应用 GNOME 的 CVE-2026-86140 补丁。上游测试通过，镜像仍以 UID 70 运行，能读取上一镜像写入的 XML 数据。复扫原始记录从 12 条降到 1 条，高危从 2 条降到 1 条，剩余记录是 Alpine 已修复的 musl CVE。[GNOME 修复提交](https://gitlab.gnome.org/GNOME/libxml2/-/commit/d1686f91dbda141a752200419d35639fd6b38340) · [Alpine 稳定版安全公告](https://www.alpinelinux.org/posts/alpine-3.21.9-3.22.7-3.23.7-3.24.3-released/)
- Milvus 从固定的 3.0.5 官方源码重编译服务程序和 Go 计划解析库，更新 Brotli、go-jose、compress 和 x/net。发布镜像复制合并后的文件系统，移除旧程序和旧解析库所在的镜像层。CVE-2026-111497、CVE-2026-116587、CVE-2026-116591 和 CVE-2026-78661 已从复扫消失。[Brotli 修复](https://github.com/andybalholm/brotli/security/advisories/GHSA-c9gr-gmg4-2vqv) · [go-jose 修复](https://github.com/go-jose/go-jose/releases/tag/v4.1.6) · [compress 修复](https://github.com/klauspost/compress/releases/tag/v1.20.2) · [x/net 修复](https://pkg.go.dev/vuln/GO-2026-6860)
- 原始 19 条高危记录中，14 条是 Docker Scout 按 Debian trixie 官方包状态对本地已重建的 Kerberos 和 Expat 报告的记录，3 条是已应用 Alpine 稳定分支修复的 musl 记录。Milvus 的 2 条 Avro 高危记录尚无原 `hamba/avro` 模块的修复版本。[Debian Kerberos 跟踪](https://security-tracker.debian.org/tracker/source-package/krb5) · [Debian Expat 跟踪](https://security-tracker.debian.org/tracker/source-package/expat) · [Alpine 安全数据库](https://secdb.alpinelinux.org/v3.24/main.json) · [Avro CVE-2026-46384](https://pkg.go.dev/vuln/GO-2026-5047) · [Avro CVE-2026-46385](https://pkg.go.dev/vuln/GO-2026-5046)

### Milvus 崩溃转储

2026 年 11 月 11 日 22:30，隔离验收容器收到停止请求后，Docker Desktop 的 WSL 日志记录到 Milvus `SIGABRT`，随后 WSL 启动崩溃转储。旧容器的堆栈定位到 `CloseEtcdClient`。内嵌 etcd 的共享客户端在关闭时返回 `context canceled`，原代码将该返回值升级为 panic。Milvus 3.0.5 和 3.0.6 均有此逻辑。[Milvus 3.0.5 关闭逻辑](https://github.com/milvus-io/milvus/blob/v3.0.5/internal/util/dependency/kv/kv_client_handler.go) · [Milvus 3.0.6 关闭逻辑](https://github.com/milvus-io/milvus/blob/v3.0.6/internal/util/dependency/kv/kv_client_handler.go)

补丁允许关闭阶段的 `context.Canceled`，并清除复用客户端。2026 年 11 月 12 日，最终发布镜像在隔离环境通过 API 健康检查、带认证的向量写入和标量过滤检索、复用同一数据卷重启，以及两次退出码为 0 的停机。WSL 无新增 Milvus 转储。开发和生产 Compose 同时限制连续重启次数、容器日志和 core 文件，并为停机预留 60 秒。WSL 宿主级转储由 WSL 独立管理。[Docker Compose 服务配置](https://docs.docker.com/reference/compose-file/services/) · [WSL 转储配置](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)

[部署与运维手册](enterprise_deployment.md) · [应用安全设计](architecture/security.md)
