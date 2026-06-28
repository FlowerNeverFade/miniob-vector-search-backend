# MiniOB 向量检索性能实验

本实验用于定量分析数据规模、向量维度和 IVF_Flat 索引参数对查询性能与召回率的影响。实验脚本直接连接 MiniOB `plain` 协议端口，统计真实 SQL 查询延迟。

## 指标

- `Brute Force avg(ms)`：无索引表上执行精确 `ORDER BY DISTANCE(...) LIMIT K` 的平均延迟。
- `IVF_Flat avg(ms)`：创建向量索引后执行同形 Top-K 查询的平均延迟。
- `Recall@K`：IVF_Flat 返回结果与精确 Top-K 的交集比例。
- `Speedup`：`Brute Force avg(ms) / IVF_Flat avg(ms)`。
- `Index build(ms)`：`CREATE VECTOR INDEX` 的耗时。

## 参数矩阵

默认实验覆盖：

- 数据规模：`N=100,1000,5000,10000`
- 向量维度：`d=16,128`
- IVF_Flat 参数：`lists=2,4,8,16`，`probes=1,2,4`
- 查询：`K=10`，每组 5 个查询向量，每个查询重复 3 次取中位数

本次已完成的完整实验使用同一参数矩阵，查询向量数为 3，每个查询重复 2 次，覆盖 96 组参数组合，全部成功执行。结果文件：

- `benchmark/vector_results/vector_search_benchmark_full.csv`
- `benchmark/vector_results/vector_search_benchmark_full.md`

## 结果摘要

本次实验使用确定性合成数据集，数据分布为多簇高斯分布，查询向量在已有数据点附近扰动生成。主要结论如下：

- 数据规模扩大时，精确扫描延迟近似随 `N` 增长：`d=16` 下 Brute Force 从 `9.167ms` 增至 `848.68ms`；`d=128` 下从 `13.108ms` 增至 `1429.97ms`。
- IVF_Flat 查询延迟整体保持在毫秒级，大规模数据下加速比显著提高；`N=10000,d=128,lists=16,probes=1` 的 Speedup 达到 `488.04x`。
- `probes` 增大时通常会扫描更多簇，召回率提升或保持为 1.0，但查询延迟也会上升。例如 `N=100,d=16,lists=8` 时，`probes=1/2/4` 的 Recall@10 分别为 `0.5667/0.9333/1.0`。
- `lists` 增大时单次扫描候选数量减少，延迟通常下降；但在小数据集且 `probes` 较小时，召回率会下降，需要通过提高 `probes` 补偿。
- 在 `N>=1000` 的本次合成数据上，所有参数组合 Recall@10 均达到 `1.0`，说明该数据分布下 IVF_Flat 聚类质量较稳定。

## 运行方式

先启动 MiniOB Observer：

```bash
cd build_debug
./bin/observer -f ../etc/observer.ini -p 6789 -P plain
```

在仓库根目录运行快速自检：

```bash
python benchmark/vector_search_benchmark.py --quick
```

运行完整实验：

```bash
python benchmark/vector_search_benchmark.py
```

也可以指定更大的或更小的矩阵：

```bash
python benchmark/vector_search_benchmark.py \
  --n-values 100,1000,5000,10000 \
  --dims 16,128 \
  --lists 2,4,8,16 \
  --probes 1,2,4 \
  --k 10 \
  --query-count 5 \
  --repeats 3
```

实验结果会写入：

- `benchmark/vector_results/*.csv`
- `benchmark/vector_results/*.md`

脚本默认会删除以 `vp` 为前缀的临时表；如需保留表用于手工检查，可增加 `--keep-tables`。
