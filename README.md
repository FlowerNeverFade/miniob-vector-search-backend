# MiniOB 向量检索系统

[![build](https://github.com/FlowerNeverFade/miniob-vector-search-backend/actions/workflows/build-test.yml/badge.svg)](https://github.com/FlowerNeverFade/miniob-vector-search-backend/actions/workflows/build-test.yml)

本仓库为《数据库系统设计实践》课程项目 MiniOB 向量检索系统。项目基于 OceanBase MiniOB `main` 分支扩展向量数据库内核能力，并配套 Flask 网关后端与 React Vite 前端界面，用于完成课程要求的向量存储、距离计算、Top-N 查询、IVF_Flat 索引和本地功能验证。

项目由三部分组成：

- MiniOB 内核扩展：实现 `VECTOR` 类型、向量函数、排序、Limit、IVF_Flat 索引和 `VECTOR_INDEX_SCAN`。
- Flask 网关后端：位于 `backend/app.py`，负责把 Web 请求转发到 MiniOB plain 协议端口。
- React Vite 前端：位于 `frontend/`，提供 SQL 控制台、表结构展示、向量可视化和检索结果对比界面。

上游项目：<https://github.com/oceanbase/miniob>

课程实现基线：`oceanbase/miniob` main 分支 commit `9f856a542decb6dc678650406af7d6e351940dab`。

## 项目结构

| 路径 | 说明 |
| --- | --- |
| `src/` | MiniOB 向量检索内核实现 |
| `test/case/test/vector-search.test` | 向量检索专项 SQL 回归用例 |
| `backend/app.py` | Flask HTTP 网关，连接 MiniOB plain 协议服务 |
| `frontend/` | React Vite 前端界面 |
| `.github/workflows/build-test.yml` | MiniOB 构建、回归、集成与性能测试 CI |

## 课程任务完成状态

| 任务 | 课程要求 | 当前实现 |
| --- | --- | --- |
| A1 向量类型数据存储 | `VECTOR(N)`、默认维度、最大维度、建表、插入、维度校验、比较规则 | 已完成 |
| A2 向量距离计算 | `STRING_TO_VECTOR`、`VECTOR_TO_STRING`、`DISTANCE`，支持欧氏距离、余弦距离、内积 | 已完成 |
| A3 精确查询与排序 | `SELECT ... AS`、`ORDER BY` 字段/函数/别名、升降序、距离排序 | 已完成 |
| A4 IVF_Flat 近似搜索 | `CREATE VECTOR INDEX`、`lists/probes`、K-Means、`LIMIT` Top-N、优化器下压 | 已完成 |

专项回归用例位于 `test/case/test/vector-search.test`，期望结果位于 `test/case/result/vector-search.result`。GitHub Actions 已将 `basic` 与 `vector-search` 一起纳入 `basic-test`，并保留 MiniOB 原有 build、CTest、integration、memtracer、benchmark、sysbench 验证矩阵。

## SQL 功能

### A1 向量存储

- 新增 SQL 类型 `VECTOR(N)`。
- `VECTOR` 不带括号时默认维度为 `2048`。
- 最大维度为 `16383`。
- `VECTOR()` 和 `VECTOR(16384)` 等非法定义会失败。
- 内部使用连续 `float` 二进制存储。
- 插入时校验字段类型与向量维度。
- `WHERE` 比较中只允许 `VECTOR = VECTOR`，拒绝 `VECTOR <> VECTOR`、大小比较和跨类型比较。

```sql
create table t_vec(id int, emb vector(3), tag char);
insert into t_vec values(1, string_to_vector('[1, 0, 0]'), 'a');
select id from t_vec where emb = string_to_vector('[1,0,0]');
```

### A2 距离计算

- `STRING_TO_VECTOR(string)`：解析 `[1, 2, -3.5]` 格式，支持空白、小数和负数。
- `VECTOR_TO_STRING(vector)`：输出标准向量字符串。
- `DISTANCE(vec1, vec2, method)`：支持 `EUCLIDEAN`、`COSINE`、`DOT`。
- 兼容 `L2_DISTANCE`、`COSINE_DISTANCE`、`INNER_PRODUCT`。
- 非法格式、未知距离方法、维度不一致、零向量余弦距离计算会返回错误。

```sql
select vector_to_string(string_to_vector('[-1.5, 0, 2.25]')) as v from t_vec limit 1;
select distance(string_to_vector('[1,2]'), string_to_vector('[4,6]'), euclidean) as l2 from t_vec limit 1;
select distance(string_to_vector('[1,0]'), string_to_vector('[0,1]'), cosine) as cos from t_vec limit 1;
select distance(string_to_vector('[1,2,3]'), string_to_vector('[4,5,6]'), 'INNER_PRODUCT') as dot from t_vec limit 1;
```

### A3 精确查询、别名、排序与 Limit

- 支持 `SELECT ... AS alias`。
- 支持 `ORDER BY expr|alias [ASC|DESC]`。
- 支持 `LIMIT N`。
- 函数表达式可用于 SELECT 列表和 ORDER BY。
- ORDER BY 可引用 SELECT 别名。
- 排序阶段支持普通标量、距离结果和向量字段的确定性比较。

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

### A4 IVF_Flat 向量索引

- 支持默认向量索引创建：

```sql
create vector index idx_vec on t_vec(emb);
```

- 支持自定义参数：

```sql
create vector index idx_vec_custom on t_vec(emb)
with (distance=cosine, type=ivfflat, lists=2, probes=1);
```

- 默认参数为 `type=ivfflat`、`lists=245`、`probes=5`。
- 索引元数据记录 `is_vector`、`distance`、`type`、`lists`、`probes`，并参与 JSON 序列化。
- 创建索引时扫描已有记录并训练聚类。
- IVF_Flat 使用确定性 K-Means，固定初始化，最多 50 轮。
- 插入和删除记录时同步维护簇内 RID。
- 优化器识别 `ORDER BY DISTANCE(vector_col, constant_vector, method) LIMIT N`，存在匹配 IVF_Flat 索引时下压为 `VECTOR_INDEX_SCAN`。

```sql
explain select id, distance(emb, string_to_vector('[0,0,0]'), euclidean) as dis
from t_vec
order by distance(emb, string_to_vector('[0,0,0]'), euclidean) asc
limit 2;
```

执行计划中应出现 `VECTOR_INDEX_SCAN`。

## 关键代码位置

| 模块 | 文件 |
| --- | --- |
| 向量类型和值存储 | `src/observer/common/type/vector_type.*`, `src/observer/common/value.*` |
| 类型注册 | `src/observer/common/type/attr_type.*`, `src/observer/common/type/data_type.cpp` |
| SQL 词法/语法 | `src/observer/sql/parser/lex_sql.l`, `src/observer/sql/parser/yacc_sql.y` |
| 表达式与向量函数 | `src/observer/sql/expr/expression.*` |
| 表达式绑定与别名 | `src/observer/sql/parser/expression_binder.*` |
| Select 语句 | `src/observer/sql/stmt/select_stmt.*` |
| 创建索引语句 | `src/observer/sql/stmt/create_index_stmt.*` |
| 排序与 Limit 算子 | `src/observer/sql/operator/sort_*`, `src/observer/sql/operator/limit_*` |
| 向量索引扫描算子 | `src/observer/sql/operator/vector_index_*` |
| 逻辑/物理计划 | `src/observer/sql/optimizer/logical_plan_generator.cpp`, `src/observer/sql/optimizer/physical_plan_generator.cpp` |
| IVF_Flat 索引 | `src/observer/storage/index/ivfflat_index.*` |
| 索引元数据 | `src/observer/storage/index/index_meta.*` |
| 表与索引维护 | `src/observer/storage/table/table.*`, `src/observer/storage/table/heap_table_engine.*` |
| 向量回归测试 | `test/case/test/vector-search.test`, `test/case/result/vector-search.result` |

## MiniOB 构建与本地运行

本机运行方式采用 WSL2 Ubuntu-24.04 构建和启动 MiniOB Observer，Windows 主机启动 Flask 网关后端与 React 前端界面。MiniOB 必须以 `plain` 文本协议模式监听 `6789` 端口，前端请求经 Flask 转发到 MiniOB。

### 1. 准备 WSL2 Ubuntu-24.04

在 Windows PowerShell 中安装 Ubuntu：

```powershell
wsl --install -d Ubuntu-24.04
```

如果系统提示重启，请重启后打开 Ubuntu-24.04，按提示创建 Linux 用户名和密码。

### 2. 构建 MiniOB (Ubuntu)

建议将仓库放到 Linux 文件系统中构建，避免 Windows/WSL 混用导致换行或文件权限问题：

```bash
mkdir -p ~/MiniOB
cp -r /mnt/d/shujvku/miniob-vector-search-backend ~/MiniOB/
cd ~/MiniOB/miniob-vector-search-backend
```

安装课程环境依赖。课程资料包中的 `MiniOB原始环境包.tar.gz` 包含 `course_env/apt-packages.txt` 依赖清单，可先解压该目录再安装：

```bash
mkdir -p ~/miniob-course-env
tar -xzf "/mnt/d/shujvku/《数据库系统设计实践》课程资料包/MiniOB原始环境包和操作说明/MiniOB原始环境包.tar.gz" -C ~/miniob-course-env course_env
sudo apt update
sudo xargs -a ~/miniob-course-env/course_env/apt-packages.txt apt install -y
```

然后初始化子模块并编译 Debug 版本：

```bash
git submodule update --init --recursive
bash build.sh init
bash build.sh debug --make -j"$(nproc)"
```

### 3. 启动 MiniOB Observer (Ubuntu)

```bash
cd ~/MiniOB/miniob-vector-search-backend/build_debug
./bin/observer -f ../etc/observer.ini -p 6789 -P plain
```

### 4. 启动 Flask 网关后端 (Windows)

在仓库根目录执行：

```powershell
python -m pip install -r backend\requirements.txt
python backend\app.py
```

后端监听地址为 `http://localhost:5000`。MiniOB 未启动时访问 `http://localhost:5000/api/tables` 会返回连接错误；MiniOB 启动后会返回表信息。

### 5. 启动 React Vite 前端界面 (Windows)

PowerShell 执行策略可能拦截 `npm.ps1`，本项目统一使用 `npm.cmd`：

```powershell
npm.cmd --prefix frontend install
npm.cmd --prefix frontend run dev
```

浏览器打开：

```text
http://localhost:5173/
```

前端生产构建命令：

```powershell
npm.cmd --prefix frontend run build
```

### 6. 向量测试 SQL 参考

进入前端界面后，可以在 SQL Terminal 中执行以下语句来验证向量检索功能：

```sql
-- 创建 3 维向量表
create table t_vec(id int, emb vector(3), tag char);

-- 插入向量数据
insert into t_vec values(1, string_to_vector('[1.0, 0.0, 0.0]'), 'a');
insert into t_vec values(2, string_to_vector('[3.0, 0.0, 0.0]'), 'b');
insert into t_vec values(3, string_to_vector('[6.0, 0.0, 0.0]'), 'c');
insert into t_vec values(4, string_to_vector('[-2.0, 0.0, 0.0]'), 'd');

-- 创建 IVF_Flat 索引
create vector index idx_vec on t_vec(emb) with (distance=euclidean, type=ivfflat, lists=2, probes=1);

-- 向量检索并计算距离排序
select id, distance(emb, string_to_vector('[0,0,0]'), euclidean) as dis from t_vec order by dis asc limit 3;
```

## 测试与验收

运行课程专项 SQL 回归：

```bash
python3 test/case/miniob_test.py --test-cases=vector-search
```

运行基础 SQL 与向量专项回归：

```bash
python3 test/case/miniob_test.py --test-cases=basic,vector-search
```

GitHub Actions workflow：`.github/workflows/build-test.yml`

CI 覆盖：

- Ubuntu Debug build + CTest
- Ubuntu Release build
- macOS build
- `basic-test`，包含 `basic` 与 `vector-search`
- `integration-test`
- `memtracer-test`
- `benchmark-test`
- sysbench 矩阵

最新状态请以仓库顶部 badge 和 Actions 页面为准：<https://github.com/FlowerNeverFade/miniob-vector-search-backend/actions>

## 许可证

本项目继承 MiniOB 的 Mulan PSL v2 许可证。详见 [License](License)。
