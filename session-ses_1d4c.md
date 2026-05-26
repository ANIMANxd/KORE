# Creating AGENTS.md instruction file

**Session ID:** ses_1d4ca867affeZEFQfjFUVatFH0
**Created:** 5/15/2026, 4:06:25 PM
**Updated:** 5/16/2026, 10:22:36 AM

---

## User

Create or update `AGENTS.md` for this repository.

The goal is a compact instruction file that helps future OpenCode sessions avoid mistakes and ramp up quickly. Every line should answer: "Would an agent likely miss this without help?" If not, leave it out.

User-provided focus or constraints (honor these):


## How to investigate

Read the highest-value sources first:
- `README*`, root manifests, workspace config, lockfiles
- build, test, lint, formatter, typecheck, and codegen config
- CI workflows and pre-commit / task runner config
- existing instruction files (`AGENTS.md`, `CLAUDE.md`, `.cursor/rules/`, `.cursorrules`, `.github/copilot-instructions.md`)
- repo-local OpenCode config such as `opencode.json`

If architecture is still unclear after reading config and docs, inspect a small number of representative code files to find the real entrypoints, package boundaries, and execution flow. Prefer reading the files that explain how the system is wired together over random leaf files.

Prefer executable sources of truth over prose. If docs conflict with config or scripts, trust the executable source and only keep what you can verify.

## What to extract

Look for the highest-signal facts for an agent working in this repo:
- exact developer commands, especially non-obvious ones
- how to run a single test, a single package, or a focused verification step
- required command order when it matters, such as `lint -> typecheck -> test`
- monorepo or multi-package boundaries, ownership of major directories, and the real app/library entrypoints
- framework or toolchain quirks: generated code, migrations, codegen, build artifacts, special env loading, dev servers, infra deploy flow
- repo-specific style or workflow conventions that differ from defaults
- testing quirks: fixtures, integration test prerequisites, snapshot workflows, required services, flaky or expensive suites
- important constraints from existing instruction files worth preserving

Good `AGENTS.md` content is usually hard-earned context that took reading multiple files to infer.

## Questions

Only ask the user questions if the repo cannot answer something important. Use the `question` tool for one short batch at most.

Good questions:
- undocumented team conventions
- branch / PR / release expectations
- missing setup or test prerequisites that are known but not written down

Do not ask about anything the repo already makes clear.

## Writing rules

Include only high-signal, repo-specific guidance such as:
- exact commands and shortcuts the agent would otherwise guess wrong
- architecture notes that are not obvious from filenames
- conventions that differ from language or framework defaults
- setup requirements, environment quirks, and operational gotchas
- references to existing instruction sources that matter

Exclude:
- generic software advice
- long tutorials or exhaustive file trees
- obvious language conventions
- speculative claims or anything you could not verify
- content better stored in another file referenced via `opencode.json` `instructions`

When in doubt, omit.

Prefer short sections and bullets. If the repo is simple, keep the file simple. If the repo is large, summarize the few structural facts that actually change how an agent should work.

If `AGENTS.md` already exists at `/`, improve it in place rather than rewriting blindly. Preserve verified useful guidance, delete fluff or stale claims, and reconcile it with the current codebase.

---

## Assistant (Build · Qwen3.6 Plus · 17.0s)

_Thinking:_

I need to read the highest-value sources first to understand the repository structure, commands, and conventions before creating or updating AGENTS.md.

**Tool: glob**

**Input:**
```json
{
  "pattern": "README*"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "AGENTS.md"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "CLAUDE.md"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": ".cursorrules"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": ".cursor/rules/*"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": ".github/copilot-instructions.md"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "opencode.json"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "opencode.jsonc"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "package.json"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "Cargo.toml"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "pyproject.toml"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "tsconfig.json"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": ".eslintrc*"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": ".prettierrc*"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "Makefile"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "Justfile"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "Taskfile*"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": ".github/workflows/*"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": ".pre-commit-config.yaml"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "vitest.config*"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "jest.config*"
}
```

**Output:**
```
No files found
```

**Tool: glob**

**Input:**
```json
{
  "pattern": "playwright.config*"
}
```

**Output:**
```
No files found
```

---

## Assistant (Build · Qwen3.6 Plus · 2.8s)

_Thinking:_

All standard config files returned empty, so I need to list the root directory to see what's actually in this repository.

**Tool: read**

**Input:**
```json
{
  "filePath": "C:\\Users\\krypt\\Documents\\dev\\KORE"
}
```

**Output:**
```
<path>C:\Users\krypt\Documents\dev\KORE</path>
<type>directory</type>
<entries>


(0 entries)
</entries>
```

---

## Assistant (Build · Qwen3.6 Plus · 5.4s)

_Thinking:_

The directory is empty, so I should check if it's a git repository to understand the project state before creating AGENTS.md.


The repository is empty — no files, no git history, no manifests. There's nothing to extract or infer.



**Tool: bash**

**Input:**
```json
{
  "command": "git status 2>&1",
  "description": "Check git repo status"
}
```

**Output:**
```
git : fatal: not a git repository (or any of the parent directories): .git
At line:1 char:1
+ git status 2>&1
+ ~~~~~~~~~~~~~~~
    + CategoryInfo          : NotSpecified: (fatal: not a gi...ectories): .git:String) [], RemoteException
    + FullyQualifiedErrorId : NativeCommandError
 

```

---

## Assistant (Build · Qwen3.6 Plus · 3.4s)

_Thinking:_

Since the repository is completely empty with no files, git history, or configuration to extract guidance from, I should inform the user and offer to create a minimal template instead.

Confirmed: empty directory, not a git repo. There's no basis for an `AGENTS.md`.

Want me to create a minimal starter template you can fill in as the project takes shape, or would you prefer to wait until there's actual content to capture?

---

