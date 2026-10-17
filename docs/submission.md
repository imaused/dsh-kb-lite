# Submitting dsh-kb-lite to awesome-dsh-plugin

The plugin list is generated from `data/plugins/*.yml`. A submission is one pull request that adds or edits **one file**. This document states the rules as they are written in the registry's own contributing guide, then gives the exact file for this plugin and the steps only the repository owner can perform.

Source of the rules quoted here: [awesome-dsh-plugin contributing guide](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/blob/main/contributing.md). Check it before submitting; the registry changes its rules and it stays authoritative over this page.

Placeholders: every `REPLACE_OWNER` below must become the GitHub account that owns the repository.

## 1. What the list requires

A pull request that adds:

- **Exactly one new file**, named after the repository: `data/plugins/<owner>__<repo>.yml` — two underscores between owner and repository name. For this plugin that is `data/plugins/REPLACE_OWNER__dsh-kb-lite.yml`.
- **`dsh.bundle` declared in the repository's `package.json`.** This is what makes the package installable with `dsh plugin add`. A package that declares only `dsh.client` is not installable and is rejected at this check. `dsh-kb-lite` declares it:

  ```json
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
  ```

  with `cordis.patch.yml` next to it.
- **The repository is at least 1 day old.** This is checked automatically against the repository's creation time. It exists to filter out repos created minutes before the PR.
- **The `dsh-plugin` topic** on the repository.
- **An accurate description.** The registry reads it as a claim about the plugin and checks it against the code — including any number of tools or any API name in it. Overstating is the main reason an otherwise good plugin is sent back. No superlatives or marketing language.
- **A `category` from the fixed list:** `agi` `ui` `usage` `theme` `model` `identity` `session` `memory` `tools` `wsl` `browser` `vision` `voice` `docs` `skill` `workflow` `git` `notify` `dev` `security` `remote` `market` `fun`.
- **At most 3 entries per pull request.** Over that, CI rejects the PR and asks for a split.

Two rules that shape the file itself:

- **Only `description.en` is required.** A missing `zh` is explicitly the maintainers' work, not a reason to reject the entry. This submission supplies both.
- **No `npm:` key.** The mapping between a listing and a published npm package is collected automatically from the registry, using the package's `repository` field. A hand-written `npm:` key is rejected.

Not part of the file, but part of being listed: the repository must contain real working code (not a placeholder or a README-only repo), and the project must stay maintained — a periodic scan flags repositories that are gone, archived or long dormant.

Do not edit the registry's `README.md` or `README.zh.md`. They are generated from `data/plugins/*.yml` after merge. A PR that touches them, or that rewrites anyone else's entry, is a review problem.

## 2. The exact YAML to add

File to create: `data/plugins/REPLACE_OWNER__dsh-kb-lite.yml`

```yaml
url: https://github.com/REPLACE_OWNER/dsh-kb-lite
name: REPLACE_OWNER/dsh-kb-lite
category: memory
description:
  en: 'Local knowledge base: ingest Markdown, plain text and PDF into a local index, then retrieve passages with BM25 or an optional hybrid dense mode. Five tools; BM25 needs only the Python standard library, and the engine makes no network requests.'
  zh: '本地知识库：把 Markdown、纯文本与 PDF 入库到本地索引，用 BM25 或可选的混合稠密模式取回原文段落。五个工具，BM25 只需 Python 标准库，引擎不发起网络请求。'
```

Notes on this file:

- **The `en` value is single-quoted because it contains `": "`.** An unquoted colon-space is parsed by YAML as a nested key and the CI fails on the schema. The Chinese full-width colon `：` does not have this problem, but quoting it as well does no harm.
- **`url` must match the repository exactly**, and `name` is the link text — `owner/repo`.
- **`category: memory`.** The plugin is a local knowledge store that a session queries, so `memory` is the closest of the fixed values. If a maintainer re-files it (for example to `docs` or `tools`), that is normal maintenance: the contributing guide says category misses are corrected rather than bounced.
- **No other keys.** No `npm:`, no `tarball:` (there is no prebuilt release), no install command.
- The description is deliberately claim-for-claim checkable: five tools, the three input formats, the local index, BM25 from the standard library, optional hybrid dense, no network from the engine. All six are true in this repository and all six are easy to grep.

A wording change that keeps every claim true is fine. Adding a claim that the code does not support is what gets the PR sent back.

## 3. Step by step

### 3.1 Create the repository

Public repository named `dsh-kb-lite`. Create it empty — no README, no `.gitignore`, no license file — because the local tree already has them and a generated initial commit only produces a merge.

```sh
gh repo create REPLACE_OWNER/dsh-kb-lite --public --description "Local knowledge base plugin for DeepSeek Harness: five tools, BM25 plus optional dense retrieval, no network"
```

Or create it in the web UI and leave "Initialize this repository with" unchecked.

### 3.2 Pre-flight, before the first commit

Do these in the working copy, once:

1. **Replace `REPLACE_OWNER` in `package.json`.** The `repository.url` field must be `git+https://github.com/REPLACE_OWNER/dsh-kb-lite.git` with the owner filled in. This is not cosmetic: the registry links a published npm package to the listing by reading that field, and with a placeholder in it nothing links. It is also the first thing a maintainer sees when they open the repo.
2. **Replace `REPLACE_OWNER` in `README.md` and `README.zh.md`** (the `github:` install command), and in this document.
3. **Run the test suite** and make sure it passes:

   ```sh
   npm test
   ```

4. **Check what would be published**, that `lib/` and `engine/` are included and no index or temporary files are:

   ```sh
   npm pack --dry-run
   ```

   `files` already lists `lib`, `engine`, `cordis.patch.yml`, both READMEs, `SECURITY.md` and `LICENSE`. `docs/` is intentionally not published.

5. **Check the working tree is clean of generated data.** `.gitignore` already covers `node_modules/`, `.kb-lite/`, `__pycache__/`, `*.pyc`, `.venv/`, `test/tmp/`, `*.log`:

   ```sh
   git status --short
   ```

### 3.3 Initialise and push

```sh
git init
git add .
git commit -m "dsh-kb-lite 0.1.0"
git branch -M main
git remote add origin https://github.com/REPLACE_OWNER/dsh-kb-lite.git
git push -u origin main
```

If `gh repo create` in 3.1 already added a remote and pushed, skip the `remote add` and `push` lines — but do commit and push the placeholder fixes from 3.2 first.

### 3.4 Add the topic

In the web UI: repository page → **About** (the gear icon) → **Topics** → add `dsh-plugin` → save.

Or with the CLI:

```sh
gh repo edit REPLACE_OWNER/dsh-kb-lite --add-topic dsh-plugin
```

Verify it is there:

```sh
gh repo view REPLACE_OWNER/dsh-kb-lite --json repositoryTopics
```

### 3.5 Wait at least one day

The repository's creation time is checked by CI. Nothing needs to be done during that day except making sure the tree you pushed is the tree you want reviewed. Resubmitting later costs nothing if you are just under the bar.

### 3.6 Fork the registry, add the file, open the PR

```sh
gh repo fork awesome-dsh-plugin/awesome-dsh-plugin --clone
cd awesome-dsh-plugin
git switch -c add-dsh-kb-lite
```

Create `data/plugins/REPLACE_OWNER__dsh-kb-lite.yml` with exactly the content in section 2, then:

```sh
git add data/plugins/REPLACE_OWNER__dsh-kb-lite.yml
git commit -m "Add REPLACE_OWNER/dsh-kb-lite"
git push -u origin add-dsh-kb-lite
gh pr create --title "Add REPLACE_OWNER/dsh-kb-lite" --body "Adds dsh-kb-lite: host-only knowledge base plugin. Five tools (kb_ingest, kb_search, kb_ask, kb_stats, kb_clear); ingests Markdown, plain text and PDF; BM25 with the Python standard library, optional dense retrieval, RRF hybrid; local index directory, no network from the engine."
```

The PR should contain exactly one added file and touch nothing else. If a check fails, the comment says what to change — push the fix to the same branch. Do not open a second PR.

If a later release changes the description, edit your own YAML file only and let the READMEs regenerate. Never edit the generated READMEs by hand: line positions shift as the list grows, and the edit lands on a neighbour.

### 3.7 What CI checks

In this order:

1. **Entry count** — at most 3 added entries per PR. Checked first, before anything is fetched.
2. **`dsh.bundle`** — fetched from the target repository's `package.json` (root, or a `packages/`, `plugins/` or `apps/` subpackage). Declaring only `dsh.client` fails here.
3. **Repository age** — the 1-day bar.
4. **`awesome-lint` and the site build** — locale parity, separators, dates, screenshots.

CI verifies the *shape* of the submission. It cannot tell whether the plugin does what the entry says.

### 3.8 What a maintainer reviews

The maintainers read the target repository before merging. They check:

1. **Whether the code does what the entry claims** — including every number and API name in the description.
2. **Whether the category fits.** Nobody is sent back over a category; a better fit is simply changed.
3. **Whether it is real, working code** rather than a placeholder or a wrapper around nothing.
4. **Whether an existing entry already covers it.** Where two plugins do the same thing, the better-kept one keeps the slot — this is a tiebreaker, not tenure.
5. **Anything alarming in the source** — obfuscated code, credential exfiltration, surprising install-time behaviour. Being listed is still not a security review.
6. **Whether the PR touches entries it has no business touching.** Every existing entry a PR modifies is listed by the gate.
7. **Whether it is a meta-package** whose only content is a dependency list. `dsh-kb-lite` ships its own code, so this does not apply.
8. **Whether dependencies point at the original authors.** It has no dependencies at all.

After merge, the READMEs and the site rebuild automatically.

## 4. Optional: publish to npm

Publishing is optional and does not affect whether the plugin is listed. It does two things: storefronts can show download counts, and the short install command works.

```sh
npm login
npm publish --access public
```

Before publishing, confirm:

- **`repository` points at the repository being listed.** It must be `git+https://github.com/REPLACE_OWNER/dsh-kb-lite.git` with the real owner. If it points anywhere else, or still says `REPLACE_OWNER`, the package and the listing are not linked — silently, with no error.
- **The version is new.** `npm publish` fails on a version that already exists; bump `version` in `package.json` first.
- **`npm pack --dry-run` shows what you expect**: `lib/`, `engine/`, `cordis.patch.yml`, `README.md`, `README.zh.md`, `SECURITY.md`, `LICENSE`, plus `package.json` itself.
- **The README install command works.** `dsh plugin --profile web add dsh-kb-lite` resolves through the npm registry, so it only works after a successful publish. Before that, the `github:REPLACE_OWNER/dsh-kb-lite` form is the only one that installs.

**Do not add an `npm:` key to the entry YAML.** The mapping is collected automatically from the registry via the `repository` field; a hand-written `npm:` is rejected. There is nothing to announce and no field to fill in.

If you would rather not publish to npm, that is fine: the `github:` install works and the listing works. The one case where a release artifact is required is a repository that cannot be installed from source at all — not this one.

## 5. Checklist: what only the repository owner can do

Everything below needs an account or a credential. It cannot be done by a tool, an agent or a contributor working in the repository.

| # | Owner-only action | Why it is owner-only | Command or place |
| --- | --- | --- | --- |
| 1 | Create the GitHub repository | Needs a GitHub account with permission to create repos | `gh repo create REPLACE_OWNER/dsh-kb-lite --public` |
| 2 | Replace `REPLACE_OWNER` in `package.json`, both READMEs and this document | The owner is the only one who knows the account name | edit `repository.url` and the `github:` install lines |
| 3 | Make the first commit and push | Push rights on the repository | `git push -u origin main` |
| 4 | Add the `dsh-plugin` topic | Repository settings | `gh repo edit REPLACE_OWNER/dsh-kb-lite --add-topic dsh-plugin` |
| 5 | Let the repository age past 1 day | It is a property of the repository | wait |
| 6 | Fork the registry and open the PR | Needs a GitHub account, and the PR is authored by a person | `gh repo fork awesome-dsh-plugin/awesome-dsh-plugin --clone` |
| 7 | Answer review comments and push fixes | The PR author's branch | same branch, new commit |
| 8 | Create the npm account, log in, publish | Ownership of the npm package name and 2FA | `npm login` then `npm publish --access public` |
| 9 | Keep the repository maintained | The periodic scan looks at the owner's repo | ordinary maintenance |

Items 1–5 and 6–7 are required for listing. Item 8 is optional. Item 9 is what keeps the entry from being removed later.

If a submission is sent back, the fix is a description or a detail, not a resubmission. The guide says a corrected line goes in; resubmitting later carries no penalty.
