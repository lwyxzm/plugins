#!/usr/bin/env python3
"""Cross-platform hermetic tests for pstack/docs/harness/sync-t3-skills.py."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "pstack/docs/harness/sync-t3-skills.py"
AGY_PSTACK = ROOT / "harness/pstack/antigravity"


def must(condition, message):
    if not condition:
        raise AssertionError(message)


def simulate_startup(link: Path, target: Path):
    """Port of T3 linkAntigravityUserConfig when target exists."""
    try:
        existing = (link.parent / os.readlink(link)).resolve()
    except FileNotFoundError:
        existing = None
    except OSError:
        try:
            link.rmdir()
        except OSError:
            return "preserved-real-directory"
        existing = None
    if existing != target:
        if existing is not None:
            link.unlink()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    return "linked"


def main():
    stage = Path(tempfile.mkdtemp(prefix="test-sync-t3-skills-"))
    try:
        source = stage / "source-skills"
        source.mkdir()
        pstack_count = 0
        for d in sorted((AGY_PSTACK / "skills").iterdir()):
            if (d / "SKILL.md").is_file():
                (source / d.name).symlink_to(d)
                pstack_count += 1

        # Synthetic non-pstack skills + _shared
        (source / "_shared").mkdir()
        (source / "_shared/common.md").write_text("# Shared\n", encoding="utf-8")
        (source / "brooks-audit").mkdir()
        (source / "brooks-audit/SKILL.md").write_text(
            '---\nname: brooks-audit\ndescription: "Architecture audit that maps module dependencies. Triggers when: user asks to audit architecture."\n---\n\nSee `../_shared/common.md`.\n',
            encoding="utf-8",
        )
        (source / "impeccable").mkdir()
        (source / "impeccable/SKILL.md").write_text(
            '---\nname: impeccable\ndescription: "Use when the user wants to design, redesign, critique, or improve a frontend interface."\n---\n\nBody.\n',
            encoding="utf-8",
        )

        # Synthetic agy-fast plugin
        fast = stage / "agy-fast"
        (fast / "dist").mkdir(parents=True)
        (fast / "dist/mcp-server.mjs").write_text("// stub\n", encoding="utf-8")
        (fast / "mcp_config.json").write_text(
            json.dumps({
                "mcpServers": {
                    "fast": {
                        "command": "node",
                        "args": ["${PLUGIN_ROOT}/dist/mcp-server.mjs"],
                        "tools": {
                            "batch_search": {"eager": True},
                            "apply_edits": {"eager": True},
                            "read_slices": {"eager": True},
                            "quick_outline": {"eager": True},
                            "run_checks": {"eager": True},
                        },
                    }
                }
            }),
            encoding="utf-8",
        )
        (fast / "hooks.json").write_text(
            json.dumps({
                "agy-fast-snapshot": {
                    "SessionStart": [{"type": "command", "command": "node dist/session-start.mjs", "timeout": 5}]
                }
            }),
            encoding="utf-8",
        )

        # Two staged T3 Antigravity profiles
        profiles = stage / "profiles"
        state = stage / "state"
        profile_ids = ("profile-alpha", "profile-beta")
        for pid in profile_ids:
            p = profiles / pid
            (p / "config").mkdir(parents=True)
            if pid == "profile-beta":
                # Simulate stock T3 Code startup: config/plugins is a symlink to gemini config/plugins
                (p / "config/plugins").symlink_to(fast.parent)
            else:
                (p / "config/plugins").mkdir(parents=True)
                (p / "config/plugins/agy-fast").symlink_to(fast)
            (p / "config/skills").symlink_to(source)
            (p / "config/config.json").write_text(json.dumps({"plugins": {"swarm": {"enabled": True}}}), encoding="utf-8")

        base_cmd = [
            sys.executable,
            str(CLI),
            "--profiles-dir", str(profiles),
            "--source-skills-dir", str(source),
            "--pstack-dir", str(AGY_PSTACK),
            "--agy-fast-dir", str(fast),
            "--state-dir", str(state),
        ]

        def run_cli(*flags, override_source=None):
            cmd = list(base_cmd)
            if override_source is not None:
                cmd[cmd.index("--source-skills-dir") + 1] = str(override_source)
            return subprocess.run([*cmd, *flags], text=True, capture_output=True)

        # 1. Dry-run makes zero mutations
        dry = run_cli("--dry-run", "--agy-fast-lazy")
        must(dry.returncode == 0, f"--dry-run failed: {dry.stderr}")
        dry_json = json.loads(dry.stdout)
        must(
            dry_json["pstack"] == pstack_count
            and dry_json["skills"] == pstack_count + 2
            and len(dry_json["changed_paths"]) == 6,
            f"Unexpected dry-run: {dry_json}",
        )
        must(not state.exists(), "--dry-run created state dir")
        for pid in profile_ids:
            must((profiles / pid / "config/skills").is_symlink(), "--dry-run mutated skills symlink")

        # 2. Apply & verify stubs + intent-aware rules/AGENTS.md + lazy agy-fast
        app = run_cli("--apply", "--agy-fast-lazy")
        must(app.returncode == 0, f"--apply failed: {app.stderr}")
        app_json = json.loads(app.stdout)
        must(len(app_json["changed_paths"]) == 6, f"Unexpected --apply changes: {app_json}")

        for pid in profile_ids:
            p = profiles / pid
            skills_dir = p / "config/skills"
            must(skills_dir.is_dir() and not skills_dir.is_symlink(), "config/skills must be real dir")
            must((skills_dir / "interrogate/SKILL.md").is_file(), "Missing interrogate stub")
            stub = (skills_dir / "interrogate/SKILL.md").read_text(encoding="utf-8")
            must("disable-model-invocation: true" in stub, "Stub missing disable-model-invocation: true")
            must(str((AGY_PSTACK / "skills/interrogate/SKILL.md").resolve()) in stub, "Stub missing canonical target")

            rules_path = p / "config/plugins/t3-skill-map/rules/AGENTS.md"
            rules = rules_path.read_text(encoding="utf-8")
            must(len(rules.encode("utf-8")) < 12000, f"rules/AGENTS.md too big: {len(rules.encode('utf-8'))}")
            must("Explicitly invoked skill:" not in rules, "Boilerplate leaked into rules/AGENTS.md")

            # Verify Section A (auto-activate)
            sec_a = rules.split("## Auto-Activate Skills", 1)[1].split("## pstack Workflow", 1)[0]
            for name in ("brooks-audit", "impeccable", "setup-pstack"):
                must(f"- `{name}`" in sec_a, f"Missing auto-activate skill {name} in Section A")

            # Verify Section B (prompt-intent & slash routed workflows)
            sec_b = rules.split("## pstack Workflow", 1)[1].split("## pstack Principles", 1)[0]
            for name, phrase in (
                ("interrogate", "adversarial review"),
                ("arena", "arena this"),
                ("how", "how does X work"),
                ("why", "why does X work this way"),
                ("teach", "teach me this"),
                ("recall", "catch me up"),
                ("blast-radius", "what could this break"),
                ("bro", "plain human language"),
                ("unslop", "Cut AI tells"),
            ):
                line_match = re.search(rf"^- `{re.escape(name)}`: (.+)$", sec_b, re.M)
                must(line_match and phrase in line_match[1], f"Missing workflow trigger {name}/{phrase} in Section B")

            # Verify Section C (24 principles) & supporting canonical paths
            sec_c = rules.split("## pstack Principles", 1)[1].split("## agy-fast", 1)[0]
            must("principle-prove-it-works" in sec_c and "principle-laziness-protocol" in sec_c, "Missing principles in Section C")
            for needle in ("docs/harness/antigravity.md", "docs/harness/t3-code.md", "worker.md", "playbooks", "_shared"):
                must(needle in rules, f"Missing supporting path {needle}")

            # Verify lazy agy-fast copy
            fast_cfg = json.loads((p / "config/plugins/agy-fast/mcp_config.json").read_text(encoding="utf-8"))
            must("tools" not in fast_cfg["mcpServers"]["fast"], "Expected eager tools removed")

        # 3. Second --apply is strict no-op
        app2 = run_cli("--apply", "--agy-fast-lazy")
        must(app2.returncode == 0 and json.loads(app2.stdout)["changed_paths"] == [], "Second apply not idempotent")

        # 4. Restart simulation (linkAntigravityUserConfig)
        for pid in profile_ids:
            must(simulate_startup(profiles / pid / "config/skills", source) == "preserved-real-directory",
                 "Restart clobbered real skills dir")
        ctrl_link = stage / "ctrl/custom/config/skills"
        ctrl_link.parent.mkdir(parents=True)
        ctrl_link.symlink_to(stage / "nonexistent")
        must(simulate_startup(ctrl_link, source) == "linked" and ctrl_link.resolve() == source.resolve(),
             "Control custom symlink should be overwritten")
        ctrl_empty = stage / "ctrl/empty/config/skills"
        ctrl_empty.mkdir(parents=True)
        must(simulate_startup(ctrl_empty, source) == "linked" and ctrl_empty.is_symlink(),
             "Control empty dir should be replaced")

        # 5. Status & source staleness
        st_ok = run_cli("--status", "--check")
        must(st_ok.returncode == 0, f"Expected --status --check exit 0, got {st_ok.returncode}")
        mod_source = stage / "source-mod"
        shutil.copytree(source, mod_source, symlinks=True)
        (mod_source / "extra-skill").mkdir()
        (mod_source / "extra-skill/SKILL.md").write_text(
            '---\nname: extra-skill\ndescription: "Extra test skill."\n---\n\nBody.\n',
            encoding="utf-8",
        )
        st_stale = run_cli("--status", "--check", override_source=mod_source)
        must(st_stale.returncode == 3, f"Expected stale exit 3, got {st_stale.returncode}")
        must(all(v["source_stale"] and v["needs_apply"] for v in json.loads(st_stale.stdout)["profiles"].values()),
             "Expected source_stale=true")

        # 6. Simulated agy-fast global installer resync & recovery under --apply and --rollback
        p0_fast = profiles / profile_ids[0] / "config/plugins/agy-fast"
        shutil.rmtree(p0_fast)
        p0_fast.symlink_to(fast)
        st_resync = run_cli("--status", "--check")
        must(st_resync.returncode == 3, "Expected resync exit 3")
        p0_rep = json.loads(st_resync.stdout)["profiles"][profile_ids[0]]
        must(p0_rep["agy_fast_resynced"] and p0_rep["needs_apply"] and not p0_rep["drift"], f"Bad resync status: {p0_rep}")
        rec = run_cli("--apply")
        must(rec.returncode == 0 and str(p0_fast) in json.loads(rec.stdout)["changed_paths"], "Failed resync --apply recovery")
        must(run_cli("--status", "--check").returncode == 0, "Expected exit 0 after resync recovery")

        shutil.rmtree(p0_fast)
        p0_fast.symlink_to(fast)
        rb = run_cli("--rollback")
        must(rb.returncode == 0, f"--rollback failed: {rb.stderr}")
        for pid in profile_ids:
            p = profiles / pid
            must(os.readlink(p / "config/skills") == str(source), "Rollback failed to restore skills symlink")
            must(os.readlink(p / "config/plugins/agy-fast") == str(fast), "Rollback failed to restore agy-fast symlink")
            must(not (p / "config/plugins/t3-skill-map").exists(), "Rollback left t3-skill-map behind")
        rb2 = run_cli("--rollback")
        must(rb2.returncode == 0 and json.loads(rb2.stdout)["changed_paths"] == [], "Second rollback not idempotent")

        print("test-sync-t3-skills: ALL ASSERTIONS PASSED")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    main()
