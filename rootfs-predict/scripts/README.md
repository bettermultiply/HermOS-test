# Scripts Layout

The scripts are grouped by intent so experiment entry points, data extraction, and analysis utilities do not share one flat directory.

- `run/`: Firecracker experiment entry points and batch runners.
- `collect/`: Converts block traces and filesystem images into CSV datasets.
- `analysis/`: Computes similarity summaries, prefetch sets, and rendered figures.
- `dataset/`: SWE-bench instance metadata helpers.
- `build/`: Local build and environment setup helpers.
- `guest/`: Guest init/bootstrap files copied into images.
- `maintenance/`: Data cleanup and repair utilities.

Common commands:

```sh
scripts/run/run_fc_task.py <instance_id> <run_id>
scripts/run/run_fc_batch.sh <task_file> <repeat_count> [jobs] [start_run]
python3 scripts/analysis/analyze.py
python3 scripts/analysis/cross_task_analyze.py
python3 scripts/analysis/all_task_analyze.py
python3 scripts/analysis/plot_summary_figures.py
python3 scripts/analysis/build_prefetch_set.py <instance_id>
```

`build_prefetch_set.py` writes tiered prefetch JSONL files under `data/prefetch_sets/<instance_id>/`.
