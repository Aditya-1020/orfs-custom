# Path configuration

The `paths` mapping in `config/flow.yaml` controls project and run output locations. Relative paths resolve from `ROOT_DIR`. Values support `{platform}`, `{design}`, `{run_tag}`, `{flow_variant}`, `{fakeram_tech}` and `{work_home}`.

| Key | Default |
|---|---|
| `work_home` | `build/runs/{platform}/{design}/{run_tag}` |
| `results_dir` | `{work_home}/results/{platform}/{design}/{flow_variant}` |
| `logs_dir` | `{work_home}/logs/{platform}/{design}/{flow_variant}` |
| `reports_dir` | `{work_home}/reports/{platform}/{design}/{flow_variant}` |
| `analysis_dir` | `{work_home}/analysis` |
| `metrics_db` | `build/analysis/metrics.db` |
| `placements_static` | `design-configs/placements` |
| `placements_generated` | `{work_home}/placements` |
| `rtl_dir` | `rtl` |
| `constraints_dir` | `constraints` |
| `fakeram_dir` | `fakeram/{fakeram_tech}` |
| `fakeram_config` | `fakeram/{fakeram_tech}_fakeram.cfg` |
| `tcl_dir` | Package-provided Tcl scripts |

Example:

```yaml
paths:
  work_home: build/runs/{platform}/{design}/{run_tag}
  results_dir: artifacts/{platform}/{design}/{flow_variant}/results
  logs_dir: artifacts/{platform}/{design}/{flow_variant}/logs
  reports_dir: artifacts/{platform}/{design}/{flow_variant}/reports
  analysis_dir: artifacts/{platform}/{design}/{run_tag}/analysis
  metrics_db: artifacts/metrics/metrics.db
  placements_static: floorplan/placements
  placements_generated: artifacts/{platform}/{design}/{run_tag}/placements
  rtl_dir: rtl
  constraints_dir: constraints
  fakeram_dir: macro-models/{fakeram_tech}
  fakeram_config: macro-models/{fakeram_tech}.cfg
  tcl_dir: scripts/openroad
```

Set `ORFS_FLOW` in the environment or set `paths.orfs_flow` explicitly. The framework deliberately does not assume a developer-specific installation directory.
