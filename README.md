# Generic Optimization Workflow Monitor

Independent desktop framework for monitoring and analysing optimization
runs produced by Generic Optimization Workflow (GOW).

## Architectural principles

- The monitor is a separate application and repository.
- It does not import or modify GOW internals.
- Communication is based initially on GOW result artifacts.
- Live and completed runs use the same data model.
- Monitoring and execution control are separate capabilities.
- Cooperative stop and checkpoint-based resume will be added through
  explicit control adapters.

## Initial architecture

```text
domain          Pure models and rules
application     Use cases and framework ports
infrastructure  GOW file readers and control adapters
ui              PySide6 desktop interface
```

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\gow-monitor.exe
```

## Installation and usage

For installation, live campaign monitoring and completed-run inspection, see:

- [Running GOW with GOW Monitor](docs/RUNNING_GOW_WITH_MONITOR.md)

### Large HTC campaigns

The monitor understands the generation-oriented artifacts produced by current
GOW HTC runs (`summary.jsonl`, `generations/g*.jsonl`, FireWorks launch
manifests and `.tar.zst` archive metadata). For million-scale campaigns the
default `--data-mode auto` avoids scanning every candidate result. See
[Running GOW with GOW Monitor](docs/RUNNING_GOW_WITH_MONITOR.md#monitoring-current-gow-htc-campaigns).
