# MiniOB Vector Search Backend

[![build](https://github.com/FlowerNeverFade/miniob-vector-search-backend/actions/workflows/build-test.yml/badge.svg)](https://github.com/FlowerNeverFade/miniob-vector-search-backend/actions/workflows/build-test.yml)

本仓库是《数据库系统设计实践》课程项目的 MiniOB 后端实现，基于 OceanBase MiniOB `main` 分支扩展向量检索能力。实现内容覆盖向量类型存储、向量函数、精确 Top-N 查询、`ORDER BY`/`LIMIT` 查询扩展，以及 IVF_Flat 向量索引和向量索引扫描执行路径。

上游项目：https://github.com/oceanbase/miniob

当前课程实现基线：`oceanbase/miniob` main 分支 commit `9f856a542decb6dc678650406af7d6e351940dab`。

## 项目目标

本项目面向 MiniOB SQL 引擎补齐向量检索后端能力，使 MiniOB 可以完成如下典型工作流：

```sql
create table items(id int, emb vector(3), tag char);
insert into items values(1, string_to_vector('[1, 0, 0]'), 'a');
insert into items values(2, string_to_vector('[0, 1, 0]'), 'b');

select id, distance(emb, string_to_vector('[0,0,0]'), euclidean) as dis
from items
order by dis asc
limit 2;

create vector index idx_items_emb on items(emb)
with (distance=euclidean, type=ivfflat, lists=245, probes=5);
```

## 功能清单

### A1 向量存储

- 新增 SQL 类型 `VECTOR(N)`。
- 支持 `VECTOR` 默认维度 `2048`。
- 最大维度为 `16383`。
- `VECTOR()` 和超限维度会报错。
- 内部使用连续 `float` 二进制存储。
- 插入阶段校验字段类型和维度。
- 支持 `VECTOR = VECTOR` / `VECTOR != VECTOR`，拒绝向量大小比较和跨类型比较。

### A2 距离计算

- 新增 `STRING_TO_VECTOR(string)`，解析 `[1, 2, -3.5]` 格式，支持空白、小数和负数。
- 新增 `VECTOR_TO_STRING(vector)`，输出标准向量字符串。
- 新增 `DISTANCE(vec1, vec2, method)`。
- 支持 `EUCLIDEAN`、`COSINE`、`DOT`。
- 兼容 `L2_DISTANCE`、`COSINE_DISTANCE`、`INNER_PRODUCT`。
- 对非法格式、未知距离方法、维度不一致、零向量余弦计算等场景返回错误。

### A3 精确查询与排序

- 支持 `SELECT ... AS alias`。
- 支持 `ORDER BY expr|alias [ASC|DESC]`。
- 支持 `LIMIT N`。
- 函数表达式可以出现在 SELECT 列表和 ORDER BY 中。
- 新增内存排序物理算子和 Limit 物理算子。
- `ORDER BY` 可以引用 SELECT 别名。

### A4 IVF_Flat 向量索引

- 新增语法：

```sql
create vector index idx on table_name(vector_col);

create vector index idx on table_name(vector_col)
with (distance=euclidean, type=ivfflat, lists=245, probes=5);
```

- 默认参数：`type=ivfflat`、`lists=245`、`probes=5`。
- 索引元数据记录 `is_vector`、`distance`、`type`、`lists`、`probes`，并参与 JSON 序列化。
- 创建索引时扫描已有记录并训练聚类。
- 使用确定性 k-means，固定初始化，最多 50 轮迭代。
- 插入和删除记录时维护簇内 RID。
- 新增 `VectorIndexScanPhysicalOperator`。
- 优化器识别 `ORDER BY DISTANCE(vector_col, constant_vector, method) LIMIT N`，存在可用 IVF_Flat 索引时下推为 `VECTOR_INDEX_SCAN`，否则回退为 TableScan + Sort。

## SQL 示例

### 建表与插入

```sql
create table t_vec(id int, emb vector(3), tag char);

insert into t_vec values(1, string_to_vector('[1, 0, 0]'), 'a');
insert into t_vec values(2, string_to_vector('[0, 1, 0]'), 'b');
insert into t_vec values(3, string_to_vector('[0, 0, 2]'), 'c');
insert into t_vec values(4, string_to_vector('[-1, 0, 0]'), 'd');
```

### 向量转换与等值查询

```sql
select id, vector_to_string(emb) as emb_text
from t_vec
where emb = string_to_vector('[1,0,0]');
```

### 距离计算、别名排序与 Limit

```sql
select id, distance(emb, string_to_vector('[0,0,0]'), euclidean) as dis
from t_vec
order by dis asc
limit 2;

select id, distance(emb, string_to_vector('[1,0,0]'), 'DOT') as score
from t_vec
order by score desc
limit 2;
```

### 创建向量索引并查看执行计划

```sql
create vector index idx_vec on t_vec(emb);

explain select id, distance(emb, string_to_vector('[0,0,0]'), euclidean) as dis
from t_vec
order by dis asc
limit 2;
```

## 关键代码位置

| 模块 | 文件 |
| --- | --- |
| 向量类型与值存储 | `src/observer/common/value.*`, `src/observer/common/type/vector_type.*` |
| SQL 词法/语法 | `src/observer/sql/parser/lex_sql.l`, `src/observer/sql/parser/yacc_sql.y` |
| 表达式与函数 | `src/observer/sql/expr/expression.*` |
| 表达式绑定 | `src/observer/sql/parser/expression_binder.*` |
| Select 语句 | `src/observer/sql/stmt/select_stmt.*` |
| 创建索引语句 | `src/observer/sql/stmt/create_index_stmt.*` |
| 排序与 Limit 算子 | `src/observer/sql/operator/sort_*`, `src/observer/sql/operator/limit_*` |
| 向量索引扫描算子 | `src/observer/sql/operator/vector_index_*` |
| 逻辑/物理计划 | `src/observer/sql/optimizer/logical_plan_generator.cpp`, `src/observer/sql/optimizer/physical_plan_generator.cpp` |
| IVF_Flat 索引 | `src/observer/storage/index/ivfflat_index.*` |
| 表引擎索引维护 | `src/observer/storage/table/heap_table_engine.*`, `src/observer/storage/table/table.*` |
| 向量回归测试 | `test/case/test/vector-search.test` |

## 构建与运行

推荐使用课程资料包中的 WSL2 + Docker / MiniOB 官方开发环境，或使用 GitHub Actions 中的 Ubuntu runner 环境。

初始化依赖：

```bash
sudo bash build.sh init
```

Debug 构建：

```bash
bash build.sh debug --make -j"$(nproc)"
```

Release 构建：

```bash
bash build.sh release --make -j"$(nproc)"
```

运行 CTest：

```bash
cd build_debug
ctest --verbose
```

运行课程 SQL case：

```bash
python3 test/case/miniob_test.py --test-cases=vector-search
```

## 验证结果

最近一次完整代码验证已通过：

- Run: https://github.com/FlowerNeverFade/miniob-vector-search-backend/actions/runs/27521948650
- Commit: `212d17d29271cb2a1daf7128a700bc6f961310fb`
- Ubuntu Debug 编译与 CTest：通过
- Ubuntu Release 编译：通过
- macOS 编译：通过
- `basic-test`：通过
- `integration-test`：通过
- `memtracer-test`：通过
- `benchmark-test`：通过
- sysbench 矩阵：通过

## GitHub Actions

主要 CI workflow 为 `.github/workflows/build-test.yml`，在 `main` 分支 push 和 PR 时触发。上游 MiniOB 的 GitHub Pages 文档发布 workflow 已保留为手动触发，避免课程仓库未启用 GitHub Pages 时产生无关失败。

## 许可证

本项目继承 MiniOB 的 Mulan PSL v2 许可证。详见 [License](License)。
