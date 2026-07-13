# SyncBotsDeep

基于深度 AI 代理的 LLVM/MLIR 自动升级工具。通过循环工程驱动代理修改代码，直到构建和测试全部通过。

## 安装

```bash
cd SyncBotsDeep
pip install -e .
```

Python >= 3.10

## 配置

创建 `llm_config.yaml`：

```yaml
default:
  provider: anthropic        # anthropic / openai / openai_compatible
  model: claude-sonnet-4-6
  api_key: <your-key>
  base_url: ""               # 留空使用默认，或填自定义端点

# 可选：强弱模型分层（主代理用强模型，子代理用弱模型降本）
strong_model: claude-opus-4-8
weak_model: claude-haiku-4-5
```

也可通过命令行参数 `--provider`、`--api-key`、`--base-url`、`--model` 直接指定。

## 使用

```bash
# 交互式向导（首次使用推荐）
syncbots init

# 单仓库升级
syncbots upgrade-single --repo buddy-mlir --repo-path ./buddy-mlir \
  --target-llvm <commit-hash>

# 多仓库批量升级
syncbots upgrade --repos-dir . --target-hash <commit-hash>

# 仅分析（不修改代码）
syncbots analyze --repos-dir . --target-hash <commit-hash>

# 基准测试
syncbots bench --bench-config bench.yaml
```

常用选项：

| 选项 | 说明 |
|------|------|
| `--max-iterations N` | 最大修复迭代次数（0=不限） |
| `--strong-model` / `--weak-model` | 覆盖强弱模型 |
| `--segment-span N` | 分段升级跨度（默认 1000 commit，0 禁用） |
| `--auto-branch` / `--auto-pr` | 自动建分支并提 PR |
| `--show-agent` | 实时输出代理交互 |
| `--no-memory` | 禁用跨运行记忆 |

## 架构

```
确定性控制器循环
  │
  ├─ scan（解析 LLVM 锚点）
  ├─ prescan（grep 验证受影响 API）
  │
  └─ FIX LOOP:
       ├─ 深度代理（编辑代码）
       │   ├─ diff-digest 子代理（读 LLVM diff）
       │   ├─ log-analyst 子代理（分析构建日志）
       │   └─ check-regen 子代理（重写 FileCheck）
       │
       └─ verify（构建 + 测试）→ 通过则退出，失败则诊断后重试
```

退出条件：测试全部通过，或相同失败重复 3 次。

## 支持的仓库

内置配置：buddy-mlir、circt、iree、stablehlo、torch-mlir、triton

自定义仓库在项目根目录放置 `.syncbots.yml` 即可，格式参考 `syncbots/repos/*.yml`。

## 测试

```bash
python -m pytest syncbots/tests/ -q
```

## GitHub Actions 集成

本项目提供 [reusable workflow](.github/workflows/llvm-upgrade.yml)，其他仓库可以直接调用。

### 在 buddy-mlir 中使用

1. 在 `buddy-compiler/buddy-mlir` 仓库的 Settings > Secrets 中添加：
   - `LLM_API_KEY` — LLM API 密钥
   - `LLM_BASE_URL` — (可选) 自定义端点
   - `LLM_PROVIDER` — (可选) `anthropic` / `openai` / `openai_compatible`
   - `PAT_TOKEN` — 有 repo 写权限的 GitHub PAT（用于创建 PR）

2. 将 [`examples/caller-workflow-buddy-mlir.yml`](examples/caller-workflow-buddy-mlir.yml) 复制到 buddy-mlir 的 `.github/workflows/` 目录

3. 在 GitHub Actions 页面手动触发，或启用 `schedule` 定时运行

触发后，agent 会自动 checkout 仓库、升级 LLVM、提交 PR。
