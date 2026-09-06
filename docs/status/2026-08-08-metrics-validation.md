# Metrics Validation Report

更新时间：2026-08-08

## Scope

本报告记录本次真实执行结果。测试通过数只表示实现行为通过 deterministic 验收，不等于检索质量、引用有效率、证据覆盖率或生产可靠性百分比。

验证工作区：

`/Users/lystran/programming/github/deep-researcher/deep-researcher`

## Environment Boundary

只检查环境变量是否存在，没有打印其值：

| 条件 | 结果 |
|---|---|
| `DEEP_RESEARCHER_POSTGRES_TEST_URL` | absent |
| `OPENAI_API_KEY` | absent |
| `LITELLM_API_KEY` | absent |
| `BRAVE_SEARCH_API_KEY` | absent |
| `DEEP_RESEARCHER_LIVE_RETRIEVAL` | absent |
| `DEEP_RESEARCHER_LIVE_HYBRID_RETRIEVAL` | absent |

此外，hybrid retrieval 专项实际使用以下显式开关和独立变量：`DEEP_RESEARCHER_RUN_HYBRID_RETRIEVAL_LIVE`、`DEEP_RESEARCHER_HYBRID_RETRIEVAL_TEST_DATABASE_URL`、embedding/rerank key、model 和可选 base URL。本次均未配置完整条件。

## Executed Results

以下命令均在当前工作区执行：

```text
uv run pytest apps/api/tests -vv --tb=short
216 passed, 13 skipped in 77.9s

npm --prefix apps/web run test -- --run
2 test files passed, 8 tests passed

uv run ruff check apps/api/src apps/api/tests
All checks passed

uv run mypy apps/api/src/deep_researcher
Success: no issues found in 42 source files

npm --prefix apps/web run typecheck
passed

npm --prefix apps/web run build
passed
```

API 的 13 个 skipped 为：

- 4 个真实 PostgreSQL + embedding + rerank hybrid retrieval 场景；
- 3 个真实 Jina Provider 场景；
- 5 个 PostgreSQL event log 场景；
- 1 个真实 ticket #12 Provider + 隔离 PostgreSQL 场景。

未配置 `DEEP_RESEARCHER_POSTGRES_TEST_URL`，因此没有执行 PostgreSQL 专项命令；这些场景不能计入 verified。

## Five Metric Classes

### 1. Retrieval quality

状态：**合成数据集已测得；真实 Provider 质量未测得**。

新增固定数据集和 evaluator：

`apps/api/tests/metrics/test_retrieval_benchmark.py`

数据集版本为 `retrieval-synthetic-v1`，包含 6 个 query、每个 query 10 个候选和人工标注的 relevant candidate IDs。使用项目同样的 RRF `1 / (rrf_k + rank)` 公式，`rrf_k=60`。结果如下：

| 策略 | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---:|---:|---:|---:|
| lexical | 66.67% | 100.00% | 100.00% | 0.7333 |
| vector | 83.33% | 100.00% | 100.00% | 0.8750 |
| RRF | 83.33% | 100.00% | 100.00% | 0.8750 |
| RRF + rerank | 100.00% | 100.00% | 100.00% | 1.0000 |

运行命令：

```text
uv run python apps/api/tests/metrics/test_retrieval_benchmark.py
uv run pytest apps/api/tests/metrics/test_retrieval_benchmark.py -vv --tb=short
2 passed in 0.01s
```

这些数字只说明该合成数据集和固定排序输入上的结果，不能宣称真实语料上的提升。真实 hybrid retrieval 仍因缺少 PostgreSQL、embedding 和 rerank 配置未执行；简历表述已加入“固定合成评测集”的限定。

### 2. Answer citation validity

状态：**deterministic 机制已验证，引用有效率百分比未测得**。

已验证的行为包括：

- 精确 answer marker range 可接受；
- 未知 label、非法 range、重复 label 和重复 visible marker 被拒绝；
- 未知 Citation 不会被改写后发布；
- 真实来源 hash 和快照一致性由 deterministic API 场景校验。

当前没有按 `Claim / Citation / Evidence Span` 导出的固定评测样本和分母，因此不能报告“有效引用率达到 X%”。

### 3. Evidence coverage

状态：**deterministic 状态转换已验证，覆盖率百分比未测得**。

已验证 `covered`、`missing`、冲突触发 replan、预算耗尽进入 partial、阻塞失败不能 complete，以及 cancelled 不进入正常验证发布路径。缺少独立 Coverage Matrix fixture/evaluator，所以不能把测试通过率作为“必要目标覆盖率”或“完整结果率”。

### 4. Runtime reliability

状态：**SQLite deterministic 恢复行为已验证，PostgreSQL 多 Worker 可靠性未验证**。

已通过的本地行为覆盖 Worker 执行、Graph 更新后崩溃接管、Map 派生事实幂等提交、取消边界和终态发布约束。交接文档所要求的真实行锁、lease、并发 event writer 和持久 checkpoint 场景属于 PostgreSQL 专项，本次因缺少数据库未执行。

因此本次不能报告恢复成功率、重复终态事件数或生产级 SLA。

### 5. Tool safety

状态：**deterministic MCP/审批安全行为已验证，外部工具安全性未验证**。

本地测试已验证拒绝、过期、取消、Workspace/allowlist 过滤、参数校验和幂等副作用边界；拒绝路径使用会在实际调用时失败的 gateway，能够证明不应执行路径未进入下游调用。真实远程 MCP、外部签名服务和生产部署边界未验证。

本次结果支持的结论是：确定性负向路径的外部副作用次数为 0，批准的本地幂等路径按测试约束只执行一次。不能推广为所有外部集成的安全保证。

## Conclusion

当前可复现结论：

- deterministic API：`216/216` 个实际执行测试通过，`13` 个外部条件缺失而跳过；
- Web：`8/8` 个测试通过；
- lint、API/Web typecheck 和 Web build 全部通过；
- 检索质量已经产生一组可用于内部简历草稿的合成评测数字，但必须保留数据集限定；
- 引用有效率、证据覆盖率、运行可靠性和工具安全仍没有可用于简历的生产或真实 Provider 百分比；
- PostgreSQL、live hybrid retrieval、真实模型/Jina、Brave、远程 MCP 和外部签名服务仍应标为 `unverified` 或 `skipped`。

下一步若要把检索数字升级为真实语料结果，需要使用固定 query/candidate/gold fixture 连接真实语料、PostgreSQL、embedding 和 rerank Provider 重新运行。其余四类指标仍需加入 Claim/Citation/Span 评测记录、Coverage Matrix evaluator、恢复场景计数和工具副作用计数；未完成前不要替换成估算值。
