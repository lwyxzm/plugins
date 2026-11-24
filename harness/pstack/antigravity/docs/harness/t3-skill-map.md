# T3 Code Antigravity Skill Map & Budget Recovery

In T3 Code, `antigravity-acp` shares one `customization_budget` (`20,000` tokens) across `<skills>` descriptions and eager MCP tool schemas (`t3-code` plus any eager plugin servers such as `agy-fast`). When user skills grow large (for example `pstack`'s 60 skills plus `brooks-*` and custom skills), Antigravity drops excess skills and silently evicts the largest eager MCP tools (`delegate_task`, `t3_thread_read`, `preview_snapshot`, `request_secret`, `schedule_task`, and `agy-fast`).

[sync-t3-skills.py](./sync-t3-skills.py) consolidates skills inside T3 Code Antigravity provider profiles (`~/.t3/userdata/providers/antigravity/<hash>/`) without modifying standalone `agy` CLI (`~/.gemini/config/skills/`, `skills.json`, or `plugins/pstack/`).

## How It Works

1. **Zero-token forwarding directory (`<profile>/config/skills/`):** Replaces the symlink to `~/.gemini/config/skills` with a non-empty real directory containing one stub per user skill with `disable-model-invocation: true`.
   - `antigravity-acp` advertises all user skills as native `/<name>` slash commands while charging `0` customization tokens.
   - T3 Code's UI `/` picker continues reading `~/.gemini/config/skills/` directly, so `$<name>` autocomplete works unchanged.
   - T3 Code's startup `linkAntigravityUserConfig` skips non-empty real directories (`ENOTEMPTY`), preserving the layout across restarts.
2. **Intent-aware canonical router (`<profile>/config/plugins/t3-skill-map/rules/AGENTS.md`):** Auto-enabled rules-only plugin (no `skills/` directory) placed in the separate `20,000`-token `Rules` budget.
   - Maps `/<name>`, `$<name>`, and natural-language prompts directly to the authoritative `~/.gemini/...` real paths via `read_file`, avoiding T3's protected profile boundary and keeping relative links (`../<sibling>/SKILL.md`, `../../docs/harness/antigravity.md`, `worker.md`, `playbooks/*.md`, `../_shared/*.md`) intact.
   - Preserves automatic activation for non-explicit skills (`setup-pstack`, `brooks-*`, `impeccable`, custom auto-skills), provides concise trigger phrases for `pstack`'s workflow skills so natural-language prompts load the right skill automatically, and indexes all 24 `principle-*` skills.
3. **Profile-local `agy-fast` lazy isolation (`--agy-fast-lazy`):** Replaces each profile's `config/plugins/agy-fast` symlink with a profile-local copy using lazy tool loading (`call_mcp_tool`) and string-mode `apply_edits`, leaving standalone `agy` CLI untouched. If a global `agy-fast` update relinks the profile directory, `--status` reports `agy_fast_resynced: true`, `--apply` restores the lazy copy atomically, and `--rollback` finishes cleanly.

## Usage

```sh
python3 ~/.gemini/config/plugins/pstack/docs/harness/sync-t3-skills.py \
  --profiles-dir "$HOME/.t3/userdata/providers/antigravity" --status

python3 ~/.gemini/config/plugins/pstack/docs/harness/sync-t3-skills.py \
  --profiles-dir "$HOME/.t3/userdata/providers/antigravity" --dry-run --agy-fast-lazy

python3 ~/.gemini/config/plugins/pstack/docs/harness/sync-t3-skills.py \
  --profiles-dir "$HOME/.t3/userdata/providers/antigravity" --apply --agy-fast-lazy

python3 ~/.gemini/config/plugins/pstack/docs/harness/sync-t3-skills.py \
  --profiles-dir "$HOME/.t3/userdata/providers/antigravity" --status --check

python3 ~/.gemini/config/plugins/pstack/docs/harness/sync-t3-skills.py \
  --profiles-dir "$HOME/.t3/userdata/providers/antigravity" --rollback
```

## Isolation & Verification

- Standalone `agy` CLI, Claude Code (`~/.claude`), and Codex (`~/.codex`) are untouched.
- Uses `renamex_np` (macOS) / `renameat2` (Linux) atomic directory exchange with `flock` locking and a recovery journal; `--rollback` restores exact original symlinks.
- After upstream `pstack` or user skill updates, run `--status --check` (exits `3` when stale or resynced) and `--apply` to refresh.
- See [the Antigravity adapter](./antigravity.md) and [the T3 Code overlay](./t3-code.md) for host tool and cross-provider delegation details.
