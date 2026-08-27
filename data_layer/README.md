# Data Layer

数据采集 / 校验 / 存储 / 质检 层。W2 路线图产物。

## 模块分工

| 子包 | 职责 | 状态 |
|---|---|---|
| `ingestion/` | 数据源适配（akshare / adata / Ashare） | W2.1 ✅ akshare |
| `storage/` | Parquet + DuckDB I/O 与 schema | W2.1 ✅ |
| `validation/` | 多源差异校验（akshare vs adata） | W2.2 待补 |
| `quality/` | 缺失日期 / 异常值 / 复权口径一致性 | W2.3 待补 |

## 为什么 `data_layer/` 是顶层包而不是 `data/` 子目录

`data/` 在 `.gitignore` 里被划成 `data/raw/`、`data/clean/`、
`data/duckdb/`、`data/quality/` 四个**数据文件**目录（各有 `.gitkeep`
占位）。如果再把 Python 包塞进 `data/`，命名会冲突（`data/quality/`
既是质检报告输出又是代码包）。所以代码放在顶层 `data_layer/`，与
`research/`、`backtest/`、`execution/`、`ops/` 并列。

## 数据目录布局（gitignored）

```
data/
├── raw/         # 原始 fetch 的 parquet，按股票切文件：000001.parquet
├── clean/       # 复权因子统一、前复权后的 parquet
├── duckdb/      # DuckDB 数据库文件：daily.duckdb
└── quality/     # 质检报告 JSON / CSV
```

## 设计原则

- **不可变原始数据**：fetcher 永远只写 `data/raw/`，从不覆盖；任何
  清洗 / 复权后产物落 `data/clean/`，保留 `data/raw/` 用于溯源。
- **DuckDB 与 Parquet 双写**：Parquet 是查询 / 分析的列存加速层，
  DuckDB 提供 SQL 入口和跨股票 join；两者同步更新。
- **source-of-truth 标注**：每条记录必须能反查到数据源 + fetch 时间
  + 复权口径（`df.attrs['fetcher']` / `'adjust'` / `'fetched_at'`）。
- **降级而非失败**：fetcher 拿不到数据先打 warning + 空 df + 抛出
  明确错误，调用方决定是否降级到 adata（W2.2 实装）。

## W2.1 已交付

- `akshare_fetcher.fetch_daily_bars(symbol, start, end, adjust)` — 标准化列、空 df 显式报错、df.attrs 标注来源
- `parquet_io.write_bars(path, df)` / `read_bars(path)` — 强制列序、date 类型
- `duck.DuckStore` — context-managed 连接、`daily_bars` 表 schema、`upsert_daily_bars` / `query_daily_bars`
- `tests/test_data_layer.py` — fetcher + parquet + DuckDB round-trip + 指标漂移检测（against [[ma-cross-baseline-000001-20240826]]）