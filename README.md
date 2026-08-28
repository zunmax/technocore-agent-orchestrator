<h1 align="center">Technocore Agent Orchestrator</h1>

Run Claude and Codex as a supervised software team, let them exchange signed
handoffs through [Technocore Chat](https://github.com/flop-labs/technocore-chat),
watch the verified conversation in a local browser UI, and export the generated
codebase into a unique output folder.

---

<h2 align="center">⭐ Overview ⭐</h2>

Technocore Agent Orchestrator demonstrates how Technocore can become the
communication layer inside a real agentic engineering workflow.

You write one project prompt in `workflow.toml` and run one PowerShell command.
The orchestrator then creates a fresh Git repository and coordinates three
roles:

| Role | Responsibility |
|---|---|
| Planner | Turns the prompt into a criterion-mapped implementation plan. |
| Implementer | Challenges the plan, implements the accepted plan, and handles revisions. |
| Reviewer | Reviews the exact candidate commit and either approves it or requests changes. |

Claude and Codex must both participate in every real workflow. One provider
performs two roles and the other performs the remaining role.

```text
Your prompt
    ↓
Planner proposes a plan
    ↓ signed Technocore handoff
Implementer challenges the plan
    ↓ signed Technocore handoff
Planner finalizes the plan
    ↓ signed Technocore handoff
Implementer writes the code
    ↓ exact Git commit + signed handoff
Reviewer approves or requests a revision
    ↓
Deterministic checks run
    ↓
Generated project is exported to output\
```

Technocore does not generate code or invoke the models. It carries signed,
ordered messages between the agents. The local supervisor starts the model
CLIs, controls the workflow state, validates every structured result, manages
Git worktrees, runs verification, and exports the final artifacts.

---

<h2 align="center">🪟 Windows Only 🪟</h2>

> **Important:** This project supports native Windows only. The current tested
> and supported target is Windows 11 with PowerShell 7 or newer.

| Environment | Supported? |
|---|---|
| Native Windows 11 + PowerShell 7 | ✅ Yes |
| Windows PowerShell 5.1 | ❌ No |
| WSL or WSL2 terminal | ❌ No |
| Linux | ❌ No |
| macOS | ❌ No |
| Windows 10 | ❌ Not currently tested or supported |

This is a real technical boundary. The code uses Windows DPAPI, Job Objects,
Windows file locking, and the Windows TCP listener API. Importing the package on
a non-Windows Python runtime fails immediately.

Docker Desktop may use its WSL2 backend to run the **Technocore Linux
container**, but the orchestrator itself must still be launched from native
PowerShell 7 on Windows. Do not run `run-workflow.ps1` inside WSL.

---

<h2 align="center">🧩 What Each Component Does 🧩</h2>

| Component | What it does |
|---|---|
| Claude and Codex | Plan, challenge, implement, revise, and review. |
| Technocore Chat | Stores signed messages in one ordered, unlisted room. |
| Orchestrator supervisor | Enforces phases, acknowledgements, limits, evidence, and completion rules. |
| Git | Records the empty baseline, implementation changes, and candidate commits. |
| SQLite | Retains validated run state, attempts, handoffs, and verification evidence. |
| Local conversation UI | Shows the verified Claude/Codex conversation while the workflow runs. |

Without Technocore, a script could copy one model's output directly into the
other model's prompt. With Technocore, each agent must read and acknowledge the
exact ordered handoff it is acting on. Messages are associated with a verified
DID, signature, sequence, reply relationship, and payload digest.

Technocore is not a model, a code editor, a test runner, or a guarantee that the
generated product is correct. Deterministic checks and human review still
matter.

---

<h2 align="center">✅ Requirements ✅</h2>

Install these tools on the same Windows computer:

- Windows 11
- [PowerShell 7](https://learn.microsoft.com/en-us/powershell/scripting/install/install-powershell-on-windows)
- [Git for Windows](https://git-scm.com/install/windows)
- [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) using Linux containers
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Python 3.12, installed and managed by `uv`
- [OpenAI Codex CLI](https://developers.openai.com/codex/cli/) with a supported signed-in account
- [Claude Code](https://code.claude.com/docs/en/setup) with a supported signed-in account
- A clean, pinned checkout of `technocore-chat` beside this repository

The expected layout is:

```text
<parent-directory>\
├── technocore-chat\
└── technocore-agent-orchestrator\
```

The folder names and sibling layout matter because `run-workflow.ps1` resolves
the Technocore source from `..\technocore-chat`.

---

<h2 align="center">🛠️ Windows Installation 🛠️</h2>

The commands in Step 1 can be run from Windows Terminal or the built-in Windows
PowerShell because that step installs PowerShell 7. After Step 1, close the old
terminal, open **PowerShell 7**, and run every remaining command there.

### 1. Install every required application

WinGet is included with current Windows 11 installations. Install the exact
packages used by this project:

```powershell
winget --version
winget install --id Microsoft.PowerShell --exact --source winget
winget install --id Git.Git --exact --source winget
winget install --id Docker.DockerDesktop --exact --source winget
winget install --id astral-sh.uv --exact --source winget
winget install --id OpenAI.Codex --exact --source winget
winget install --id Anthropic.ClaudeCode --exact --source winget
```

Accept any installer or license prompts. Open Docker Desktop from the Windows
Start menu, finish its first-run setup, and wait for the engine to become ready.
Keep Docker Desktop in Linux-container mode.

Close the old terminal and open **PowerShell 7**. Verify every installed tool:

```powershell
$PSVersionTable.PSVersion
git --version
docker version
uv --version
codex --version
claude --version
```

The PowerShell major version must be `7` or newer. If `docker version` cannot
reach the server, wait for Docker Desktop to finish starting before continuing.

### 2. Prepare the pinned Technocore server

Create a neutral parent directory in your Windows Documents folder, enter it,
and clone both repositories:

```powershell
$projectRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'TechnocoreProjects'
New-Item -ItemType Directory -Path $projectRoot -Force | Out-Null
Set-Location $projectRoot
git clone https://github.com/flop-labs/technocore-chat.git
git clone https://github.com/zunmax/technocore-agent-orchestrator.git
git -C .\technocore-chat checkout --detach d8775c2c03e4fc96c24022ffa7103cc765ea94fc
git -C .\technocore-chat rev-parse HEAD
git -C .\technocore-chat status --short
```

The two clone commands create the required sibling directories. `rev-parse`
must print `d8775c2c03e4fc96c24022ffa7103cc765ea94fc`, and the final status command
must print nothing. The orchestrator intentionally refuses a different
Technocore commit or a dirty Technocore working tree.

### 3. Install the orchestrator environment

Enter this repository and install the locked Python 3.12 environment:

```powershell
Set-Location .\technocore-agent-orchestrator
uv python install 3.12
uv sync --frozen
uv run python --version
uv run technocore-orchestrator --version
uv run technocore-orchestrator doctor --json
```

Expected versions:

```text
Python 3.12.x
technocore-orchestrator 0.1.0
```

`doctor` without a configuration performs bounded prerequisite checks. It does
not start Claude or Codex.

### 4. Sign in to Codex and Claude

Complete each provider's supported interactive sign-in flow:

```powershell
codex login
claude auth login
```

Confirm both sessions and record their installed versions:

```powershell
codex login status
claude auth status
codex --version
claude --version
```

The orchestrator is designed for the providers' locally stored signed-in
sessions. You do not put an OpenAI or Anthropic API key in `workflow.toml`.
Raw provider API-key environment variables are rejected before real adapters
are created.

### 5. Record the native executables and exact versions

WinGet may expose an executable through a filesystem link, but the orchestrator
requires the real native `.exe`. Run this PowerShell 7 block to resolve any link
to its final target and print the exact values needed by `workflow.toml`:

```powershell
function Resolve-RealExecutable {
    param([Parameter(Mandatory)][string]$Name)

    $command = Get-Command $Name -CommandType Application -ErrorAction Stop
    $item = Get-Item -LiteralPath $command.Source -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        $target = $item.ResolveLinkTarget($true)
        if ($null -eq $target) {
            throw "Unable to resolve the native executable behind $($item.FullName)."
        }
        $item = $target
    }
    if ($item.Extension -ne '.exe') {
        throw "$($item.FullName) is not a native Windows .exe."
    }
    return $item.FullName
}

$codexExe = Resolve-RealExecutable -Name 'codex.exe'
$claudeExe = Resolve-RealExecutable -Name 'claude.exe'
$codexVersion = (codex --version) -replace '^codex-cli\s+', ''
$claudeVersion = ((claude --version) -split '\s+')[0]

[pscustomobject]@{ Provider = 'codex'; Executable = $codexExe; Version = $codexVersion }
[pscustomobject]@{ Provider = 'claude'; Executable = $claudeExe; Version = $claudeVersion }
```

Copy the printed `Executable` and `Version` values into the matching provider
section in `workflow.toml`. The orchestrator rejects `.cmd`, `.bat`, `.ps1`,
symlink, and reparse-point launchers.

---

<h2 align="center">⚙️ Configure the Workflow ⚙️</h2>

### 1. Create your local profile

Copy the machine-neutral example once:

```powershell
Copy-Item .\examples\workflow.example.toml .\workflow.toml
notepad .\workflow.toml
```

`workflow.toml` is ignored by Git because it contains machine-specific paths and
may contain a private project prompt.

### 2. Configure the providers

Update each provider entry with:

- `executable`: the exact absolute path printed in Installation Step 5;
- `model`: an exact model available to the signed-in account; and
- `expected_version`: the matching `Version` printed in Installation Step 5.

Example profile:

```toml
schema_version = 4

[task]
prompt = """
Describe the new codebase you want Claude and Codex to generate. Include the
required features, constraints, and expected behavior.
"""
allowed_paths = ["."]

[roles]
planner = "codex"
implementer = "claude"
reviewer = "codex"

[providers.codex]
executable = 'C:\absolute\path\to\codex.exe'
model = "REVIEW_AND_PIN_EXACT_MODEL"
expected_version = "0.0.0"

[providers.claude]
executable = 'C:\absolute\path\to\claude.exe'
model = "REVIEW_AND_PIN_EXACT_MODEL"
expected_version = "0.0.0"

[limits]
max_model_invocations = 20
claude_max_turns = 20

[verification]
include_git_diff_check = true

[storage]
root = ".local/workflow"

[output]
root = "output"
```

Replace every placeholder before running. Do not copy the example version
numbers literally.

### 3. Choose the role assignment

Both real providers must participate. These are the two simplest assignments:

```toml
[roles]
planner = "codex"
implementer = "claude"
reviewer = "codex"
```

```toml
[roles]
planner = "claude"
implementer = "codex"
reviewer = "claude"
```

A real all-Claude or all-Codex run is rejected. Cursor is not used by this
project. The all-`fake` provider mode exists only for credential-free automated
tests and cannot be mixed with real roles.

### 4. Understand the limits

| Setting | Meaning |
|---|---|
| `max_model_invocations` | Maximum total Claude and Codex process launches in one workflow, including repairs and revisions. |
| `claude_max_turns` | Maximum internal Claude Code turns for each Claude invocation. It is not the number of Technocore chat messages. |

Additional timeout, output-size, schema-repair, and revision limits have secure
defaults in the configuration model. They can be added to TOML only when you
have a specific reason to override them.

These controls are termination and safety limits, not a monetary budget. The
project does not estimate provider cost because account-backed Claude and Codex
sessions do not expose one shared billing contract.

### 5. Validate without invoking a model

```powershell
uv run technocore-orchestrator validate-config .\workflow.toml --json
uv run technocore-orchestrator doctor --config .\workflow.toml --json
```

`validate-config` checks the TOML structure. Profile-aware `doctor` also checks
the native executable paths and exact versions. Neither command asks a model to
perform work or confirms that the account is signed in; use the provider status
commands for that.

---

<h2 align="center">🚀 Run a Real Workflow 🚀</h2>

After the one-time setup, the normal process for every new generated project is:

1. Open `workflow.toml`.
2. Replace `[task].prompt` with the new product request.
3. Save the file.
4. Run one command:

```powershell
.\run-workflow.ps1
```

That launcher:

1. creates a unique run ID;
2. checks PowerShell 7 and the local profile;
3. checks the loopback Technocore health endpoint;
4. starts the existing container or builds it from the pinned sibling source;
5. opens the private local conversation viewer; and
6. starts the supervised Claude/Codex workflow.

You do not create a project directory, initialize Git, or pass an output folder
on the command line. Each run gets a fresh empty internal Git repository, and
its generated code is exported automatically.

`--allow-model-invocations` is supplied by the launcher as explicit consent to
start the two signed-in model CLIs. It is not an API-key flag or cost
calculator.

The equivalent lower-level command is available for debugging when Technocore
is already healthy:

```powershell
uv run technocore-orchestrator run --config .\workflow.toml `
  --allow-model-invocations --json
```

The PowerShell launcher is the recommended interface because it also manages
the local Technocore container, generates the run ID, and opens the viewer.

---

<h2 align="center">👀 Watch Claude and Codex in the UI 👀</h2>

The browser page updates while the run is active.

- Claude and Codex appear as agent messages with their provider identity.
- Supervisor lifecycle entries appear as centered system messages.
- Each message shows its verified sequence, sender, role, time, kind, and reply
  relationship.
- Mirrored workflow and collaboration events are de-duplicated.
- Private model chain-of-thought is never displayed.

To reopen a retained conversation, copy the run ID printed by the launcher:

```powershell
$runId = 'run_20260828_123456_789' # Replace with the ID printed by your run.
.\open-chat.ps1 -RunId $runId
```

The viewer binds to a random port on `127.0.0.1`. Use **Close viewer** in the
page when you are finished so its hidden local server exits cleanly.

---

<h2 align="center">📦 Find the Generated Project 📦</h2>

Each terminal workflow creates one collision-resistant directory under
`output`:

```text
output\
└── <task-id>__<date-time-offset>__<run-id>\
    ├── code\
    │   └── complete generated codebase
    ├── agent-outputs\
    │   └── validated planner, implementer, and reviewer JSON
    ├── reports\
    │   ├── run.json
    │   ├── events.jsonl
    │   ├── conversation.jsonl
    │   └── report.md
    └── artifact-manifest.json
```

Open the newest generated code folders with:

```powershell
Get-ChildItem .\output -Directory |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 5 FullName,LastWriteTime
```

`artifact-manifest.json` records the exported paths, sizes, statuses, and
SHA-256 digests. Existing output is accepted only after its complete manifest
passes integrity verification. Do not manually reuse or edit an existing run's
output directory.

Operational state is retained separately under `.local\workflow`, including
SQLite, generated repositories, worktrees, resolved profiles, protected
identities, and private room records. Both `.local` and `output` are ignored by
Git.

---

<h2 align="center">🔐 Privacy, Rooms, and Identities 🔐</h2>

### Is the Claude/Codex conversation public?

Not in the default configuration. Technocore is published only on
`127.0.0.1:8080`, so ordinary internet or LAN clients cannot connect to it. A
real-run preflight examines the actual Windows listener table and rejects a
wildcard or non-loopback listener.

Every run also receives a fresh random, unlisted room capability. The room value
is excluded from TOML, model prompts, SQLite reports, exported output, and the
browser page. Models use a short-lived random-path local MCP gateway and never
receive the room capability or signing keys directly.

However, an unlisted Technocore room is **not end-to-end encrypted**. Anyone who
obtains the room capability and can also reach the local service could read the
room. The design protects against ordinary remote access; it does not protect
against malware, an administrator, a compromised Windows account, or a process
already running as the same user.

### DPAPI-protected agent identities

The first approved real run automatically creates four distinct Ed25519
identities:

- supervisor;
- planner;
- implementer; and
- reviewer.

They are stored under `.local\workflow\identities` as `.identity.dpapi` files.
Each private key is encrypted with Windows Data Protection API for the current
Windows user. The application decrypts it when needed, derives its stable
`did:key:z6Mk...`, and signs Technocore messages.

There is no plaintext SEED, PEM file, or passphrase prompt. Plaintext
`.identity.seed` files are not supported.

The encrypted identity files normally work only for the same Windows user on
the same computer. They protect a copied file from another user or machine, but
not a hostile process already running under your account. Back them up
deliberately if retained runs matter to you; losing the DPAPI decryption context
can prevent old runs from being resumed.

Manual identity creation is optional:

```powershell
uv run technocore-orchestrator identity-create supervisor --config .\workflow.toml
uv run technocore-orchestrator identity-create planner --config .\workflow.toml
uv run technocore-orchestrator identity-create implementer --config .\workflow.toml
uv run technocore-orchestrator identity-create reviewer --config .\workflow.toml
```

Never commit, paste, or publish `.identity.dpapi`, `.seed`, `.pem`, `.key`, room,
credential, or provider-authentication files.

---

<h2 align="center">🧭 Operate Existing Runs 🧭</h2>

Replace `<run-id>` with the exact identifier printed by `run-workflow.ps1`:

| Task | Command |
|---|---|
| Show state and recent events | `uv run technocore-orchestrator status <run-id> --config .\workflow.toml --recent 20 --json` |
| Open the verified local UI | `uv run technocore-orchestrator view <run-id> --config .\workflow.toml` |
| Regenerate redacted reports | `uv run technocore-orchestrator report <run-id> --config .\workflow.toml --json` |
| Request cancellation | `uv run technocore-orchestrator cancel <run-id> --config .\workflow.toml --json` |
| Resume a non-terminal run | `uv run technocore-orchestrator resume <run-id> --config .\workflow.toml --allow-model-invocations --json` |
| Preview safe cleanup | `uv run technocore-orchestrator cleanup <run-id> --config .\workflow.toml --dry-run --json` |
| Apply safe cleanup | `uv run technocore-orchestrator cleanup <run-id> --config .\workflow.toml --apply --json` |

Retained-run commands load the immutable resolved configuration stored when the
run began. Editing today's prompt does not rewrite an older run.

Resume requires the original identities, room record, provider versions,
executable fingerprints, Git state, and SQLite evidence. A provider invocation
with an unknown terminal outcome fails closed instead of guessing. Failed or
canceled terminal runs remain evidence; fix the cause and start a new run.

Cleanup removes only clean, recognized linked worktrees using non-forced Git
operations. Dirty or unrecognized worktrees remain for investigation. The
orchestrator never merges, pushes, or deletes a user's external repository.

---

<h2 align="center">🧪 Deterministic Verification 🧪</h2>

The generic profile enables the built-in `git diff --check` gate. You can add
trusted reusable checks when the generated project family has a known command:

```toml
[[verification.commands]]
id = "example_test"
argv = ["C:\\absolute\\path\\to\\your-test.exe", "--check"]
timeout_seconds = 600
required = true
```

Verification commands come only from the operator-controlled TOML. Model output
is never executed as a command. A model cannot mark a failed deterministic
check as passed.

`task.allowed_paths = ["."]` permits the generated project to use its entire
fresh repository. Narrow it only when the prompt must restrict changes to known
repository-relative locations.

For matched A/B experiments, the report comparator is also available:

```powershell
uv run technocore-orchestrator compare-reports `
  .\mode-a\run.json .\mode-b\run.json `
  --seeded-criterion criterion_1 --json
```

An `inconclusive` comparison is not evidence that either workflow produced a
better result.

---

<h2 align="center">🧯 Troubleshooting 🧯</h2>

### `workflow.toml is missing`

Create it from the example and configure every provider placeholder:

```powershell
Copy-Item .\examples\workflow.example.toml .\workflow.toml
notepad .\workflow.toml
```

### PowerShell says the launcher requires version 7

You opened Windows PowerShell 5.1. Start `PowerShell 7` or run:

```powershell
pwsh
$PSVersionTable.PSVersion
```

Do not run the launcher inside WSL.

### Pinned Technocore source is missing or has the wrong commit

Verify the sibling checkout:

```powershell
git -C ..\technocore-chat rev-parse HEAD
git -C ..\technocore-chat status --short
```

The commit must be:

```text
d8775c2c03e4fc96c24022ffa7103cc765ea94fc
```

The status command must print nothing.

### Docker is unavailable or Technocore never becomes healthy

Start Docker Desktop and make sure it is using Linux containers:

```powershell
docker version
docker container inspect technocore-workflow-local
Invoke-RestMethod http://127.0.0.1:8080/healthz
```

If another process owns port 8080, inspect it without stopping anything:

```powershell
Get-NetTCPConnection -LocalPort 8080 -State Listen |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

Do not change the binding to `0.0.0.0`; non-loopback listeners are intentionally
rejected.

### A provider executable is rejected

```powershell
uv run technocore-orchestrator doctor --config .\workflow.toml --json
```

Configure the native `.exe`, not an alias, script, npm `.cmd` wrapper, symlink,
or reparse point. If the recorded version differs from the installed version,
either restore the reviewed version or deliberately update `expected_version`.

### Codex or Claude is not signed in

```powershell
codex login status
claude auth status
```

If needed, run `codex login` or `claude auth login`. Do not solve authentication
errors by adding API keys to `workflow.toml`.

### `[SSL: CERTIFICATE_VERIFY_FAILED]`

The local Technocore connection uses loopback HTTP, so this error normally comes
from a provider CLI, `uv`, a proxy, or another HTTPS dependency. Inspect the
complete error to identify the failing process and hostname.

On a managed network, install the organization's CA using the approved Windows
or tool-specific trust-store procedure. Never disable TLS verification and do
not add `verify=False`.

### The UI opens but waits for the run

The viewer may start a few seconds before the durable run record exists. It
waits for up to 120 seconds. Check the workflow terminal, then run:

```powershell
$runId = 'run_20260828_123456_789' # Replace with the ID printed by your run.
uv run technocore-orchestrator status $runId --config .\workflow.toml --json
```

### A message appears twice

Use the current viewer and exact run ID. It de-duplicates matching workflow and
collaboration projections. Close an older browser viewer with its **Close
viewer** button before reopening the run.

### `error[protocol]` mentions paths, evidence, criteria, or handoffs

The supervisor rejected model output that did not match the actual Git diff or
the signed collaboration contract. This is a safety gate. Preserve the failed
run and inspect its UI, status, and report. Do not manually modify
`.local\workflow` or force the rejected result through.

Correct any configuration problem or clarify the prompt, then start a fresh
run. Failed terminal runs are not restarted under the same run ID.

### A DPAPI identity cannot be decrypted

Run the orchestrator as the same Windows user on the same computer that created
the identity. Copying `.identity.dpapi` to another account or computer is not an
identity migration method.

### Existing output fails integrity verification

Do not edit or reuse the existing output folder. Keep it as evidence and start a
new run. The manifest check intentionally rejects unexpected, missing, linked,
or modified files.

---

<h2 align="center">🧑‍💻 Development and Verification 🧑‍💻</h2>

Install the locked environment and run the same Windows safety gates used by
continuous integration:

```powershell
uv sync --frozen
uv run ruff format --check src scripts tests
uv run ruff check src scripts tests
uv run ty check src scripts tests
uv run python scripts/export_schemas.py --check
uv run coverage run -m pytest -q
uv run coverage report
uv run pip-audit
uv build
```

The normal test suite uses the credential-free fake provider and does not call
Claude or Codex. Two account-backed provider contract tests require explicit
opt-in because they start external model CLIs.

The GitHub Actions workflow runs on Windows and uses the locked `uv` environment.
