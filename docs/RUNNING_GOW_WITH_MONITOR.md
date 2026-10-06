# Running GOW with GOW Monitor

This guide explains how to install and run GOW Monitor alongside Generic
Optimization Workflow (GOW), monitor a live optimization campaign and inspect
a completed run.

## Architectural separation

GOW Monitor is an independent application and must be installed in a separate
directory from the original GOW repository.

The two repositories must never be nested inside one another and must never
share the same Python virtual environment.

Recommended directory layout:

```text
workspace/
+-- generic-optimization-workflow/
|   +-- Original GOW repository and its own virtual environment
|
+-- generic-optimization-workflow-monitor/
    +-- Independent monitor repository and its own virtual environment
```

GOW Monitor does not:

- modify the original GOW source code
- import private modules from the GOW repository
- patch or replace GOW files
- modify optimization specifications
- inject candidates into GOW
- alter optimizers or evaluators

Communication between both applications is external and read-only.

GOW writes result artifacts to an output directory. GOW Monitor reads those
artifacts while they are being produced.

Optionally, GOW Monitor can receive the operating-system PID of the running
GOW process to observe its CPU usage, memory usage, descendant processes and
threads.

Neither mechanism requires any modification to the original GOW repository.


## How the integration works

GOW Monitor observes a campaign through interfaces that already exist outside
the GOW source code.

The primary interface is the output directory selected when GOW is launched.
GOW writes optimization artifacts to this directory, and GOW Monitor reads them
while the campaign is running or after it has finished.

Typical output structure:

```text
<results-root>/
+-- results.jsonl
+-- summary.json
+-- runs/
    +-- <run-id>/
        +-- <candidate-id>/
            +-- input.json
            +-- output.json
            +-- result.json
            +-- stdout.txt
            +-- stderr.txt
```

The monitor treats these artifacts as read-only data sources.

For live process telemetry, the monitor can also receive the operating-system
PID of the root GOW process. It then observes that process and its descendants
using operating-system interfaces.

The PID is supplied externally when the monitor is launched. GOW does not need
to know that it is being monitored.

The complete integration boundary is therefore:

```text
GOW process
    |
    +-- writes result artifacts
    |
    +-- exposes an operating-system PID
    |
    v
GOW Monitor
    +-- reads result artifacts
    +-- observes process resources
    +-- never modifies GOW
```

## Requirements

The following components are required:

- Git
- Python 3.10 or newer
- the original Generic Optimization Workflow repository
- the independent GOW Monitor repository
- a separate Python virtual environment for each repository
- a graphical desktop session for the monitor interface

On Windows, PowerShell is recommended for launching GOW and capturing its
process identifier.

On Linux and macOS, Bash or another compatible shell can be used.

The user running GOW Monitor must have permission to:

- read the GOW output directory
- inspect the GOW process and its descendant processes
- create the monitor virtual environment
- open a desktop graphical application

Optional GPU telemetry depends on the operating system, graphics hardware,
installed drivers and available vendor tools.

A supported GPU is not required to monitor optimization results, CPU usage,
memory usage, processes or threads.

## Installing GOW Monitor

GOW Monitor must be cloned outside the original GOW repository.

Move to the parent directory that will contain both independent repositories:

```powershell
Set-Location "C:\path\to\workspace"
```

Clone the monitor repository:

```powershell
git clone --branch Antonio https://github.com/CST-Modelling-Tools/generic-optimization-workflow-monitor.git
```

Enter the monitor directory:

```powershell
Set-Location .\generic-optimization-workflow-monitor
```

Create a dedicated virtual environment for the monitor:

```powershell
py -3.10 -m venv .venv
```

Activate the monitor environment:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& .\.venv\Scripts\Activate.ps1
```

Install GOW Monitor:

```powershell
python -m pip install --upgrade pip
python -m pip install -e .
```

Verify the installation:

```powershell
gow-monitor --help
gow-monitor --version
```

The monitor virtual environment belongs only to GOW Monitor. It must not be
created inside the original GOW repository and must not be reused to run GOW.

## Installing the original GOW repository

The original Generic Optimization Workflow repository must be installed
separately from GOW Monitor.

Return to the parent workspace directory:

```powershell
Set-Location "C:\path\to\workspace"
```

Clone the official GOW repository:

```powershell
git clone https://github.com/CST-Modelling-Tools/generic-optimization-workflow.git
```

Enter the GOW directory:

```powershell
Set-Location .\generic-optimization-workflow
```

Create a dedicated virtual environment for GOW:

```powershell
py -3.10 -m venv .venv
```

Activate the GOW environment:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& .\.venv\Scripts\Activate.ps1
```

Install GOW:

```powershell
python -m pip install --upgrade pip
python -m pip install -e .
```

Verify the installation:

```powershell
gow --help
```

The GOW virtual environment belongs only to the original GOW repository.
It must not be reused by GOW Monitor.

After installation, the expected directory layout is:

```text
workspace/
+-- generic-optimization-workflow/
|   +-- .venv/
|
+-- generic-optimization-workflow-monitor/
    +-- .venv/
```

## Running and monitoring a GOW campaign on Windows

The recommended Windows procedure uses PowerShell as an external launcher.

PowerShell starts the original GOW executable, obtains its operating-system
PID and then starts GOW Monitor with the same results directory.

This procedure does not modify the GOW repository or its source code.

Open PowerShell and adjust the following paths:

```powershell
$gowRoot = "C:\path\to\workspace\generic-optimization-workflow"
$monitorRoot = "C:\path\to\workspace\generic-optimization-workflow-monitor"
$specification = "C:\path\to\optimization_specs.yaml"
$resultsRoot = "C:\path\to\gow-results"
$runId = "example-live-run"
```

Build the paths to both independent executables:

```powershell
$gowExecutable = Join-Path $gowRoot ".venv\Scripts\gow.exe"
$monitorExecutable = Join-Path $monitorRoot ".venv\Scripts\gow-monitor.exe"
```

Start GOW and retain its process information:

```powershell
$gowProcess = Start-Process `
    -FilePath $gowExecutable `
    -WorkingDirectory $gowRoot `
    -ArgumentList @(
        "run",
        $specification,
        "--outdir",
        $resultsRoot,
        "--run-id",
        $runId
    ) `
    -PassThru
```

Display the PID captured by PowerShell:

```powershell
Write-Host "GOW PID: $($gowProcess.Id)"
```

Start the independent monitor:

```powershell
& $monitorExecutable `
    --results-root $resultsRoot `
    --gow-pid $gowProcess.Id
```

The same absolute results directory must be supplied to both applications.

GOW writes the optimization artifacts to that directory. GOW Monitor reads
those artifacts and observes the process tree rooted at the supplied PID.

The monitor does not start, stop, patch or control the GOW campaign.

## Monitoring an existing or completed run

GOW Monitor can inspect a results directory without attaching to a running
GOW process.

This mode is useful when:

- GOW was started before the monitor
- the GOW process PID is unknown
- the optimization campaign has already finished
- only result artifacts and host telemetry are required

Set the paths to the independent monitor and the existing GOW results:

```powershell
$monitorRoot = "C:\path\to\workspace\generic-optimization-workflow-monitor"
$resultsRoot = "C:\path\to\gow-results"
$monitorExecutable = Join-Path $monitorRoot ".venv\Scripts\gow-monitor.exe"
```

Open the results directory in GOW Monitor:

```powershell
& $monitorExecutable --results-root $resultsRoot
```

The monitor reads the available GOW artifacts from the selected directory.

Without `--gow-pid`, GOW-specific CPU usage, memory usage, process count and
thread count are unavailable and are displayed as `N/A`.

Host CPU, host memory and supported host GPU telemetry remain available.

This mode does not reopen, resume or modify the optimization campaign.


## What to expect during a live campaign

When GOW and GOW Monitor are connected correctly, the monitor should update
while new evaluation artifacts are written to the results directory.

During an active campaign, verify that:

- the run state changes to `RUNNING`
- the evaluation count increases progressively
- successful and failed evaluation counts are updated
- the best objective and latest objective are refreshed
- the best-so-far chart evolves as new results arrive
- host CPU and memory telemetry continue updating
- GOW CPU and memory values are numeric when `--gow-pid` is supplied
- the GOW process and thread counts reflect the active process tree

The monitor may display several descendant processes when evaluators or
external scientific programs are launched by GOW.

When the GOW campaign finishes:

- the final result artifacts remain available for inspection
- objective statistics and charts remain visible
- host telemetry continues updating
- GOW-specific CPU, memory, process and thread metrics become `N/A`

The transition to `N/A` is expected because the operating-system process tree
no longer exists after GOW exits.

GPU telemetry represents the GPU activity of the host system.

It must not be interpreted as GPU usage attributed exclusively to GOW.
Other graphical applications, desktop composition and external programs can
also contribute to the displayed GPU utilization and memory values.

If a metric cannot be measured reliably, GOW Monitor displays `N/A` instead
of inventing a value.

## Troubleshooting

### The monitor opens, but no run appears

Verify that GOW and GOW Monitor are using exactly the same absolute results
directory.

The GOW command must use:

```text
--outdir <results-root>
```

The monitor must use:

```text
--results-root <same-results-root>
```

Also verify that GOW is creating `results.jsonl` or candidate `result.json`
files inside that directory.

### The evaluation count does not increase

Check whether the GOW campaign is still running and whether new result records
are being written.

Inspect the following files when available:

- `<results-root>/results.jsonl`
- `<results-root>/runs/<run-id>/<candidate-id>/result.json`
- `<results-root>/runs/<run-id>/<candidate-id>/stdout.txt`
- `<results-root>/runs/<run-id>/<candidate-id>/stderr.txt`

Evaluator failures or missing output files can prevent valid evaluations from
appearing in the monitor.

### GOW process metrics display `N/A`

Possible causes include:

- `--gow-pid` was not supplied
- the supplied PID is incorrect
- the GOW process has already finished
- the supplied PID belongs to a launcher that has already exited
- the current user cannot inspect the process

Capture the PID directly from the process that launches the GOW command.

### Host telemetry works, but GPU telemetry displays `N/A`

GPU telemetry depends on the operating system, graphics driver and available
backend.

Current support includes NVIDIA telemetry through `nvidia-smi` and generic
Windows telemetry through Windows CIM performance counters.

An unavailable GPU metric is displayed as `N/A`. This does not mean that GPU
usage is zero.

### GOW metrics become `N/A` after the campaign finishes

This is expected. The operating-system process tree no longer exists after the
GOW process exits.

Result statistics and charts remain available because they are read from the
artifacts stored in the results directory.

### The monitor cannot access the results directory

Verify:

- the path is correct
- the current user has read permission
- the parent directory exists or can be created
- no regular file already uses the requested directory name
- the path is not locked or disconnected

Using an absolute results path is recommended.

## Monitoring current GOW HTC campaigns

Recent GOW HTC runs publish generation-level artifacts specifically so external
observers do not need to traverse millions of candidate directories:

```text
<results-root>/runs/<run-id>/
├── summary.json
├── summary.jsonl
├── generations/
│   ├── g000000.jsonl
│   ├── g000000.launches.json
│   ├── g000001.jsonl
│   └── g000001.launches.json
└── archives/
    ├── candidates/g000000.tar.zst
    ├── launchers/g000000.tar.zst
    └── manifests/g000000.archive.json
```

The monitor does not unpack `.tar.zst` files. They are cold-storage artifacts;
the generation shards and manifests remain the monitoring interface.

### Data modes

`gow-monitor` supports three artifact-reading policies:

```bash
gow-monitor --results-root /path/to/results --data-mode auto
```

- `auto` (recommended): for campaigns with at least one million planned
  evaluations and a `summary.jsonl`, use the lightweight generation summary.
  Smaller campaigns retain exact shard statistics.
- `summary`: always prefer `summary.json` + `summary.jsonl`. This scales to very
  large campaigns without reading every candidate record. Evaluation count,
  completed generations and best objective are exact; success/failure rates,
  medians and population diversity are shown as unavailable because GOW does
  not store those aggregates in `summary.jsonl`.
- `full`: read `generations/g*.jsonl` for exact statistics. The reader processes
  immutable new generation shards incrementally instead of rereading the whole
  campaign on every refresh. Use this when exact failure rates, medians and
  diversity are more important than the I/O cost.

For a 10M-100M evaluation HTC scan, `auto` is normally the appropriate mode.

### FireWorks and launcher monitoring

When `generations/gXXXXXX.launches.json` is present, the Run Health panel also
reports FireWork state counts and Launch counts. This is read-only: the monitor
does not connect to MongoDB or import FireWorks.

Generation launcher archives are detected through their metadata and archive
paths. Historical `FIZZLED` FireWorks remain visible even if a retry later
succeeded; therefore a non-zero FIZZLED count is an operational diagnostic, not
by itself proof that the optimization run failed.

### Archived candidate workdirs

The monitor no longer requires
`runs/<run-id>/<candidate-id>/result.json` for current HTC runs. Once a
`gXXXXXX.jsonl` generation shard exists, candidate workdirs may be compressed
and removed by GOW without breaking monitoring. Legacy runs that only contain
individual `result.json` files remain supported in `full` mode.

### Before the first completed generation

For current HTC layouts, `auto` deliberately does not recursively scan candidate
workdirs while the first generation is still running. The monitor shows control
and FireWorks metadata until the first generation summary/shard is published.
This avoids turning the monitor itself into an inode-heavy filesystem scan.
