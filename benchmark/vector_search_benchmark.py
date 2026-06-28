#!/usr/bin/env python3
"""Run quantitative MiniOB vector-search performance experiments.

The script talks to MiniOB through the plain protocol and records real SQL
latency for exact scan and IVF_Flat scan. It intentionally avoids the Flask
demo benchmark endpoint because that endpoint adds UI-oriented simulation.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import random
import socket
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_N_VALUES = [100, 1000, 5000, 10000]
DEFAULT_DIMS = [16, 128]
DEFAULT_LISTS = [2, 4, 8, 16]
DEFAULT_PROBES = [1, 2, 4]
CSV_COLUMNS = [
    "timestamp",
    "n",
    "dimension",
    "lists",
    "probes",
    "k",
    "query_count",
    "repeat_count",
    "insert_ms",
    "index_build_ms",
    "bruteforce_avg_ms",
    "bruteforce_p50_ms",
    "ivfflat_avg_ms",
    "ivfflat_p50_ms",
    "recall_at_k",
    "speedup",
    "status",
    "error",
]


class MiniObError(RuntimeError):
    pass


class MiniObClient:
    """Minimal persistent client for MiniOB plain protocol."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)

    def close(self) -> None:
        self.sock.close()

    def execute(self, sql: str) -> tuple[str, float]:
        payload = sql.encode("utf-8") + b"\0"
        start = time.perf_counter()
        self.sock.sendall(payload)
        chunks: list[bytes] = []
        while True:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise MiniObError("MiniOB closed the connection")
            nul = chunk.find(b"\0")
            if nul >= 0:
                chunks.append(chunk[:nul])
                break
            chunks.append(chunk)
        elapsed_ms = (time.perf_counter() - start) * 1000
        return b"".join(chunks).decode("utf-8", errors="replace").strip(), elapsed_ms


@dataclass(frozen=True)
class DataSet:
    n: int
    dimension: int
    rows: list[tuple[int, list[float], str]]
    queries: list[list[float]]


def parse_int_list(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def sql_vector(values: Iterable[float]) -> str:
    return "[" + ",".join(f"{value:.4f}" for value in values) + "]"


def generate_dataset(n: int, dimension: int, query_count: int, seed: int) -> DataSet:
    rng = random.Random(seed + n * 1009 + dimension * 9176)
    cluster_count = 16
    centers = [
        [rng.uniform(-8.0, 8.0) for _ in range(dimension)]
        for _ in range(cluster_count)
    ]

    rows: list[tuple[int, list[float], str]] = []
    for row_id in range(1, n + 1):
        cluster = (row_id - 1) % cluster_count
        noise = 1.15 if dimension >= 128 else 0.85
        vector = [
            round(centers[cluster][j] + rng.gauss(0.0, noise), 4)
            for j in range(dimension)
        ]
        rows.append((row_id, vector, sql_vector(vector)))

    queries: list[list[float]] = []
    for i in range(query_count):
        base_id = 1 + ((i * 997) % n)
        base_vec = rows[base_id - 1][1]
        query_noise = 0.50 if dimension >= 128 else 0.35
        query = [round(value + rng.gauss(0.0, query_noise), 4) for value in base_vec]
        queries.append(query)

    return DataSet(n=n, dimension=dimension, rows=rows, queries=queries)


def euclidean(left: list[float], right: list[float]) -> float:
    return math.sqrt(sum((a - b) * (a - b) for a, b in zip(left, right)))


def exact_topk(dataset: DataSet, query: list[float], k: int) -> list[int]:
    ranked = [
        (euclidean(vector, query), row_id)
        for row_id, vector, _ in dataset.rows
    ]
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [row_id for _, row_id in ranked[:k]]


def parse_table_ids(raw: str) -> list[int]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    ids: list[int] = []
    for line in lines[1:]:
        first = line.split("|", 1)[0].strip()
        try:
            ids.append(int(first))
        except ValueError:
            continue
    return ids


def require_success(raw: str, sql: str) -> None:
    text = raw.strip()
    if text == "SUCCESS":
        return
    raise MiniObError(f"SQL failed: {sql}\nResponse: {text}")


def execute_ignore_error(client: MiniObClient, sql: str) -> None:
    try:
        client.execute(sql)
    except Exception:
        pass


def show_tables(client: MiniObClient) -> list[str]:
    raw, _ = client.execute("show tables;")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    return [line for line in lines[1:] if line and "|" not in line]


def cleanup_prefix(client: MiniObClient, prefix: str) -> None:
    for table in show_tables(client):
        if table.startswith(prefix):
            execute_ignore_error(client, f"drop table {table};")


def create_table(client: MiniObClient, table_name: str, dimension: int) -> None:
    raw, _ = client.execute(f"create table {table_name}(id int, emb vector({dimension}));")
    require_success(raw, f"create table {table_name}")


def insert_rows(client: MiniObClient, table_name: str, dataset: DataSet) -> float:
    start = time.perf_counter()
    for row_id, _, vector_text in dataset.rows:
        sql = f"insert into {table_name} values({row_id}, string_to_vector('{vector_text}'));"
        raw, _ = client.execute(sql)
        require_success(raw, sql)
    return (time.perf_counter() - start) * 1000


def create_vector_index(client: MiniObClient, table_name: str, lists: int, probes: int) -> float:
    sql = (
        f"create vector index idx_{table_name} on {table_name}(emb) "
        f"with (distance=euclidean, type=ivfflat, lists={lists}, probes={probes});"
    )
    raw, elapsed_ms = client.execute(sql)
    require_success(raw, sql)
    return elapsed_ms


def query_topk(client: MiniObClient, table_name: str, query: list[float], k: int) -> tuple[list[int], float]:
    vector_text = sql_vector(query)
    sql = (
        f"select id, distance(emb, string_to_vector('{vector_text}'), euclidean) as dis "
        f"from {table_name} order by dis asc limit {k};"
    )
    raw, elapsed_ms = client.execute(sql)
    if raw.strip().upper().startswith("FAILURE"):
        raise MiniObError(f"Query failed: {sql}\nResponse: {raw}")
    return parse_table_ids(raw), elapsed_ms


def measure_queries(
    client: MiniObClient,
    table_name: str,
    dataset: DataSet,
    k: int,
    repeats: int,
) -> tuple[list[list[int]], list[float]]:
    ids_by_query: list[list[int]] = []
    latencies: list[float] = []
    for query in dataset.queries:
        query_ids: list[int] = []
        query_latencies: list[float] = []
        for _ in range(repeats):
            ids, elapsed_ms = query_topk(client, table_name, query, k)
            query_ids = ids
            query_latencies.append(elapsed_ms)
        ids_by_query.append(query_ids)
        latencies.append(statistics.median(query_latencies))
    return ids_by_query, latencies


def recall_at_k(exact_ids: list[list[int]], indexed_ids: list[list[int]], k: int) -> float:
    recalls = []
    for exact, indexed in zip(exact_ids, indexed_ids):
        recalls.append(len(set(exact) & set(indexed)) / float(k))
    return sum(recalls) / len(recalls) if recalls else 0.0


def summarize_latencies(latencies: list[float]) -> tuple[float, float]:
    if not latencies:
        return 0.0, 0.0
    return sum(latencies) / len(latencies), statistics.median(latencies)


def append_csv(path: Path, row: dict[str, object]) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=CSV_COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})


def write_markdown(csv_path: Path, md_path: Path) -> None:
    rows: list[dict[str, str]] = []
    with csv_path.open("r", newline="", encoding="utf-8") as fp:
        rows = list(csv.DictReader(fp))

    ok_rows = [row for row in rows if row.get("status") == "ok"]
    lines = [
        "# MiniOB 向量检索性能实验结果",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "实验统计真实 MiniOB SQL 延迟；Brute Force 为无索引表上的精确 `ORDER BY DISTANCE ... LIMIT K`，IVF_Flat 为创建向量索引后的同形查询。",
        "",
        "| N | d | lists | probes | Brute Force avg(ms) | IVF_Flat avg(ms) | Recall@K | Speedup | Index build(ms) |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in ok_rows:
        lines.append(
            "| {n} | {dimension} | {lists} | {probes} | {bf} | {ivf} | {recall} | {speedup} | {build} |".format(
                n=row["n"],
                dimension=row["dimension"],
                lists=row["lists"],
                probes=row["probes"],
                bf=row["bruteforce_avg_ms"],
                ivf=row["ivfflat_avg_ms"],
                recall=row["recall_at_k"],
                speedup=row["speedup"],
                build=row["index_build_ms"],
            )
        )

    failed = [row for row in rows if row.get("status") != "ok"]
    if failed:
        lines.extend(["", "## 失败组合", ""])
        lines.append("| N | d | lists | probes | error |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in failed:
            lines.append(
                f"| {row['n']} | {row['dimension']} | {row['lists']} | {row['probes']} | {row.get('error', '')} |"
            )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_benchmark(args: argparse.Namespace) -> Path:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = output_dir / f"vector_search_benchmark_{timestamp}.csv"
    md_path = output_dir / f"vector_search_benchmark_{timestamp}.md"

    client = MiniObClient(args.host, args.port, args.timeout)
    try:
        if args.cleanup_before:
            cleanup_prefix(client, args.table_prefix)

        for dimension in args.dims:
            for n in args.n_values:
                dataset = generate_dataset(n, dimension, args.query_count, args.seed)
                exact_ids = [exact_topk(dataset, query, args.k) for query in dataset.queries]

                base_table = f"{args.table_prefix}_bf_n{n}_d{dimension}"
                execute_ignore_error(client, f"drop table {base_table};")
                create_table(client, base_table, dimension)
                insert_ms = insert_rows(client, base_table, dataset)
                _, bf_latencies = measure_queries(client, base_table, dataset, args.k, args.repeats)
                bf_avg, bf_p50 = summarize_latencies(bf_latencies)
                print(
                    f"[base] N={n} d={dimension} insert={insert_ms:.2f}ms "
                    f"bf_avg={bf_avg:.2f}ms bf_p50={bf_p50:.2f}ms",
                    flush=True,
                )

                for lists in args.lists_values:
                    for probes in args.probes_values:
                        table_name = f"{args.table_prefix}_n{n}_d{dimension}_l{lists}_p{probes}"
                        row = {
                            "timestamp": timestamp,
                            "n": n,
                            "dimension": dimension,
                            "lists": lists,
                            "probes": probes,
                            "k": args.k,
                            "query_count": args.query_count,
                            "repeat_count": args.repeats,
                            "insert_ms": round(insert_ms, 3),
                            "bruteforce_avg_ms": round(bf_avg, 3),
                            "bruteforce_p50_ms": round(bf_p50, 3),
                        }
                        try:
                            execute_ignore_error(client, f"drop table {table_name};")
                            create_table(client, table_name, dimension)
                            combo_insert_ms = insert_rows(client, table_name, dataset)
                            index_build_ms = create_vector_index(client, table_name, lists, probes)
                            indexed_ids, idx_latencies = measure_queries(
                                client, table_name, dataset, args.k, args.repeats
                            )
                            idx_avg, idx_p50 = summarize_latencies(idx_latencies)
                            recall = recall_at_k(exact_ids, indexed_ids, args.k)
                            speedup = (bf_avg / idx_avg) if idx_avg > 0 else 0.0
                            row.update({
                                "insert_ms": round(combo_insert_ms, 3),
                                "index_build_ms": round(index_build_ms, 3),
                                "ivfflat_avg_ms": round(idx_avg, 3),
                                "ivfflat_p50_ms": round(idx_p50, 3),
                                "recall_at_k": round(recall, 4),
                                "speedup": round(speedup, 3),
                                "status": "ok",
                                "error": "",
                            })
                            print(
                                f"[ok] N={n} d={dimension} lists={lists} probes={probes} "
                                f"ivf_avg={idx_avg:.2f}ms recall={recall:.3f} speedup={speedup:.2f}x",
                                flush=True,
                            )
                        except Exception as exc:
                            row.update({
                                "index_build_ms": "",
                                "ivfflat_avg_ms": "",
                                "ivfflat_p50_ms": "",
                                "recall_at_k": "",
                                "speedup": "",
                                "status": "failed",
                                "error": str(exc).replace("\n", " ")[:500],
                            })
                            print(
                                f"[failed] N={n} d={dimension} lists={lists} probes={probes}: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                        finally:
                            append_csv(csv_path, row)
                            write_markdown(csv_path, md_path)
                            if args.cleanup_each:
                                execute_ignore_error(client, f"drop table {table_name};")

                if args.cleanup_each:
                    execute_ignore_error(client, f"drop table {base_table};")

        if args.cleanup_after:
            cleanup_prefix(client, args.table_prefix)
    finally:
        client.close()

    write_markdown(csv_path, md_path)
    return csv_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MiniOB vector-search performance benchmark")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6789)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--n-values", type=parse_int_list, default=DEFAULT_N_VALUES)
    parser.add_argument("--dims", type=parse_int_list, default=DEFAULT_DIMS)
    parser.add_argument("--lists", dest="lists_values", type=parse_int_list, default=DEFAULT_LISTS)
    parser.add_argument("--probes", dest="probes_values", type=parse_int_list, default=DEFAULT_PROBES)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--query-count", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260628)
    parser.add_argument("--table-prefix", default="vp")
    parser.add_argument("--output-dir", default="benchmark/vector_results")
    parser.add_argument("--no-cleanup-before", dest="cleanup_before", action="store_false")
    parser.add_argument("--keep-tables", dest="cleanup_each", action="store_false")
    parser.add_argument("--cleanup-after", action="store_true")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run a small smoke benchmark: N=100, dims=16, lists=2/4, probes=1/2.",
    )
    parser.set_defaults(cleanup_before=True, cleanup_each=True)
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.quick:
        args.n_values = [100]
        args.dims = [16]
        args.lists_values = [2, 4]
        args.probes_values = [1, 2]
        args.query_count = min(args.query_count, 3)
        args.repeats = min(args.repeats, 2)

    csv_path = run_benchmark(args)
    print(f"CSV written to {csv_path}")
    print(f"Markdown written to {csv_path.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
