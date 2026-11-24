#!/usr/bin/env python3
"""Generate and install a profile-local Antigravity skill registry for T3 Code."""

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROUTER = "t3-skill-map"
SKILLS = "config/skills"
RULES = f"config/plugins/{ROUTER}"
FAST = "config/plugins/agy-fast"
NAME = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
EXPLICIT_PREFIX = re.compile(
    r"^Explicitly invoked skill:\s*activate ONLY when\b.*?never auto-activate on ordinary prompts\.\s*",
    re.I,
)
TRIGGER_SENTENCE = re.compile(r"^(?:Use (?:for|when|only when)|Triggers when:|Type /)\b", re.I)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        try:
            tmp.write(json_text(value))
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(tmp.name, path)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


def summarize_intent(text, limit=140):
    """Build a concise single-line summary, prioritizing explicit trigger phrases."""
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    if not TRIGGER_SENTENCE.match(sentences[0]):
        trig_idx = next((i for i, s in enumerate(sentences[1:], 1) if TRIGGER_SENTENCE.match(s)), None)
        if trig_idx is not None:
            sentences = [sentences[trig_idx], *sentences[:trig_idx], *sentences[trig_idx + 1:]]
    if len(sentences[0]) <= limit:
        acc = sentences[0]
        for sentence in sentences[1:]:
            candidate = f"{acc} {sentence}"
            if len(candidate) <= limit:
                acc = candidate
            else:
                break
        return acc
    cut = sentences[0][: limit - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(",;:-") + "..."


def frontmatter(path):
    """Read scalar frontmatter fields in the authoritative registry; reject unknown forms."""
    text = path.read_text(encoding="utf-8")
    fields = re.match(r"\A---\n(.*?)\n---(?:\n|$)", text, re.S)
    require(fields is not None, f"Missing frontmatter in {path}")
    name = re.search(r"^name: ([a-z0-9-]+)$", fields[1], re.M)
    desc = re.search(r"^description:[ \t]*(.*)$", fields[1], re.M)
    require(name is not None and NAME.fullmatch(name[1]) and desc, f"Invalid skill metadata in {path}")
    value = desc[1]
    if value in (">", ">-", ">+"):
        body = re.match(r"\n((?: +[^\n]+\n?)+)", fields[1][desc.end():])
        require(body is not None, f"Empty folded description in {path}")
        value = " ".join(line.strip() for line in body[1].splitlines())
        if desc[1] != ">-" and not fields[1].endswith(body[1]):
            value += "\n"
    elif value.startswith('"'):
        value = json.loads(value)
    elif value.startswith("'") and value.endswith("'"):
        value = value[1:-1].replace("''", "'")
    else:
        require(not re.match(r"[>|\[\]{@&*!]", value), f"Unsupported description in {path}")
    require(isinstance(value, str) and value.strip(), f"Empty description in {path}")
    disable_match = re.search(r"^disable-model-invocation:\s*(true|false)\s*$", fields[1], re.M | re.I)
    has_disable = bool(disable_match and disable_match[1].lower() == "true")
    return name[1], value.strip(), has_disable


def collect(source, pstack):
    pstack_skills = pstack / "skills"
    expected = {entry.name for entry in pstack_skills.iterdir() if (entry / "SKILL.md").is_file()}
    rows = {}
    registered = set()
    for entry in sorted(source.iterdir()):
        path = entry / "SKILL.md"
        if not path.is_file():
            require(not entry.is_symlink(), f"Broken or unsupported registry link {entry}")
            continue
        canonical = path.resolve(strict=True)
        name, original, has_disable = frontmatter(canonical)
        require(name == entry.name and name not in rows, f"Noncanonical or duplicate name {entry}")
        is_pstack = canonical.is_relative_to(pstack_skills)
        if is_pstack:
            require(canonical == pstack_skills / name / "SKILL.md", f"Unexpected pstack route {canonical}")
            registered.add(name)
        else:
            require(canonical == source / name / "SKILL.md", f"Unexpected user skill route {canonical}")
        prefix_match = EXPLICIT_PREFIX.match(original)
        clean_desc = original[prefix_match.end():].strip() if prefix_match else original
        explicit_only = has_disable or bool(prefix_match)
        stub_desc = "Explicit invocation only." if explicit_only else summarize_intent(clean_desc, limit=160)
        intent_desc = summarize_intent(clean_desc, limit=160 if not explicit_only else 135)
        rows[name] = {
            "source": str(canonical),
            "original": original,
            "description": stub_desc,
            "intent_summary": intent_desc,
            "pstack": is_pstack,
            "explicit_only": explicit_only,
        }
    require(rows and expected == registered,
            f"Pstack and global picker disagree. Missing registrations: {', '.join(sorted(expected - registered))}")
    return rows


def supporting_paths(source, pstack):
    adapter = pstack / "docs/harness/antigravity.md"
    overlay = pstack / "docs/harness/t3-code.md"
    worker = pstack / "skills/poteto-mode/worker.md"
    playbooks = pstack / "skills/poteto-mode/playbooks"
    shared = source / "_shared"
    for required_file in (adapter, overlay, worker):
        require(required_file.is_file(), f"Missing supporting file {required_file}")
    require(playbooks.is_dir(), f"Missing playbooks directory {playbooks}")
    lines = [
        f"- Antigravity adapter (read once per pstack session): `{adapter}`",
        f"- T3 Code overlay (apply when `orchestrator_capabilities`/`delegate_task` exist): `{overlay}`",
        f"- Poteto worker & playbooks: `{worker}` and `{playbooks}/<slug>.md`",
    ]
    if shared.is_dir():
        lines.append(f"- Brooks shared guides: `{shared}/` (resolved via `../_shared/*.md` from `{source}/brooks-*/SKILL.md`)")
    lines.append(
        f"- Newly added skill fallback: if `$NAME` or `/NAME` is not listed above, read `{source}/NAME/SKILL.md` "
        "(following any symlink to its canonical directory)."
    )
    return lines


def outputs(rows, source, pstack):
    stubs = {}
    for name, row in rows.items():
        description = json.dumps(row["description"], ensure_ascii=False)
        target = row["source"]
        stubs[f"{name}/SKILL.md"] = (
            f"---\nname: {name}\ndescription: {description}\ndisable-model-invocation: true\n---\n\n"
            f"Read the complete canonical skill at `{target}` with read_file before proceeding. "
            "Follow that skill with the user's arguments and resolve relative references from the "
            "canonical file containing each reference. Use its canonical directory as the skill base.\n"
        ).encode()

    pstack_names = [name for name, row in rows.items() if row["pstack"]]
    non_pstack_names = [name for name, row in rows.items() if not row["pstack"]]
    auto_skills = [(name, row) for name, row in rows.items() if not row["explicit_only"]]
    workflow_skills = [
        (name, row) for name, row in rows.items() if row["explicit_only"] and not name.startswith("principle-")
    ]
    principle_names = [
        name for name, row in rows.items() if row["explicit_only"] and name.startswith("principle-")
    ]

    rules = [
        "# T3 Skill Routing",
        "",
        "For any `$NAME` (T3 picker), `/NAME` command, or prompt-matched skill below, "
        "read its canonical `SKILL.md` in full with `read_file` before acting. Pass the rest of the "
        "message as arguments. Canonical paths take precedence over the profile forwarding path in ACP metadata; "
        "resolve relative links from the canonical file containing each reference.",
        "",
    ]
    if pstack_names:
        rules.append(f"- `{pstack / 'skills'}/NAME/SKILL.md` for: {', '.join(pstack_names)}.")
    if non_pstack_names:
        rules.append(f"- `{source}/NAME/SKILL.md` for: {', '.join(non_pstack_names)}.")
    rules.extend(["", "## Supporting Canonical Paths", *supporting_paths(source, pstack)])

    if auto_skills:
        rules.extend([
            "",
            "## Auto-Activate Skills (No Slash Required)",
            "Automatically `read_file` and follow these skills whenever a user prompt matches their triggers:",
        ])
        for name, row in auto_skills:
            rules.append(f"- `{name}` — {row['intent_summary']}")

    if workflow_skills:
        rules.extend([
            "",
            "## pstack Workflow & Utility Skills (Intent & Slash Routed)",
            "Load via `read_file` on `/<name>`, `$<name>`, when another pstack skill routes to it, "
            "or when the user's prompt directly matches one of the workflow trigger phrases below "
            "(do not activate on unrelated ordinary coding prompts):",
        ])
        for name, row in workflow_skills:
            rules.append(f"- `{name}`: {row['intent_summary']}")

    if principle_names:
        rules.extend([
            "",
            "## pstack Principles (Explicit & poteto-mode Leaf Rules)",
            "Load on `/principle-*`, `$principle-*`, or when `poteto-mode` applies that principle:",
            f"- {', '.join(principle_names)}.",
        ])

    router = {
        "plugin.json": json_text({"name": ROUTER, "version": "1.0.0"}).encode(),
        "rules/AGENTS.md": ("\n".join(rules) + "\n").encode(),
        "skill-map.json": json_text({name: row["source"] for name, row in rows.items()}).encode(),
    }
    return {SKILLS: stubs, RULES: router}


def fast_rules(router):
    result = dict(router)
    result["rules/AGENTS.md"] += (
        "\n## agy-fast in T3 Code\n\n"
        "Use `call_mcp_tool` with server `agy-fast_fast` and the schemas in the agy-fast rule. "
        "For lazy apply_edits, encode edits as a JSON array string.\n"
    ).encode()
    return result


def fast_outputs(upstream):
    files = {}
    for file in sorted(upstream.rglob("*")):
        require(not file.is_symlink(), f"Unexpected link in agy-fast package {file}")
        if file.is_file() and not any(part.startswith(".") for part in file.relative_to(upstream).parts):
            files[file.relative_to(upstream).as_posix()] = file.read_bytes()
    mcp = json.loads(files["mcp_config.json"])
    servers = mcp["mcpServers"]
    require(set(servers) == {"fast"}, "Unexpected agy-fast MCP layout")
    server = servers["fast"]
    require(server["args"] == ["${PLUGIN_ROOT}/dist/mcp-server.mjs"] and "dist/mcp-server.mjs" in files,
            "Unexpected agy-fast executable layout")
    server.pop("tools", None)
    server.setdefault("env", {}).update({
        "AGY_FAST_TOOL_LOADING": "lazy",
        "AGY_FAST_AUTO_UPDATE": "off",
        "AGY_FAST_EDITS_FORMAT": "string",
        "AGY_FAST_CACHE_DIR": "${PLUGIN_ROOT}/../../../.t3-agy-fast-cache",
    })
    files["mcp_config.json"] = json_text(mcp).encode()
    hooks = json.loads(files["hooks.json"])
    require(set(hooks) == {"agy-fast-snapshot"}, "Unexpected agy-fast hook layout")
    for hook in hooks["agy-fast-snapshot"]["SessionStart"]:
        require(hook["type"] == "command" and "dist" in hook["command"], "Unexpected agy-fast hook")
        hook["command"] = (
            'env AGY_FAST_AUTO_UPDATE=off AGY_FAST_TOOL_LOADING=lazy AGY_FAST_EDITS_FORMAT=string '
            'AGY_FAST_CACHE_DIR="$GEMINI_HOME/.t3-agy-fast-cache" ' + hook["command"]
        )
    files["hooks.json"] = json_text(hooks).encode()
    return files


def payload_hash(files):
    digest = hashlib.sha256()
    for rel, data in sorted(files.items()):
        digest.update(json.dumps((rel, hashlib.sha256(data).hexdigest())).encode() + b"\n")
    return {"tree": digest.hexdigest()}


def source_fingerprint(rows, source, pstack, upstream=None):
    payload = {
        "skills": [
            [name, row["source"], row["explicit_only"], row["original"]]
            for name, row in sorted(rows.items())
        ],
        "supporting": supporting_paths(source, pstack),
    }
    if upstream is not None and upstream.exists():
        payload["agy_fast"] = identity(upstream)
    return hashlib.sha256(json_text(payload).encode()).hexdigest()


def identity(path):
    if path.is_symlink():
        return {"link": os.readlink(path)}
    if not path.exists():
        return None
    if not path.is_dir():
        return {"foreign": str(path)}
    files = {}
    for file in sorted(path.rglob("*")):
        if file.is_symlink() or not (file.is_dir() or file.is_file()):
            return {"foreign": str(file)}
        if file.is_file():
            files[file.relative_to(path).as_posix()] = file.read_bytes()
    return payload_hash(files)


def is_fast_resynced(profile, record, upstream, actual_fast):
    original_fast = record["original"].get(FAST)
    if not record.get("lazy") or original_fast is None:
        return False
    if actual_fast == original_fast:
        return True
    fast_path = profile / FAST
    return fast_path.is_symlink() and fast_path.resolve() == upstream


def real_directory(path):
    require(path.is_dir() and not path.is_symlink(), f"Expected real directory {path}")


def write_payload(path, payload):
    if isinstance(payload, str):
        path.symlink_to(payload)
    elif payload is not None:
        path.mkdir(mode=0o700)
        for rel, data in payload.items():
            destination = path / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)


def remove_owned(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def exchange(left, right):
    """Swap a directory and link atomically without a missing-path window."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        libc.renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        libc.renamex_np.restype = ctypes.c_int
        code = libc.renamex_np(os.fsencode(left), os.fsencode(right), 2)  # RENAME_SWAP
    elif sys.platform == "linux":
        libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        libc.renameat2.restype = ctypes.c_int
        code = libc.renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2)  # RENAME_EXCHANGE
    else:
        raise ValueError(f"No atomic directory exchange on {sys.platform}")
    if code != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(left), None, str(right))


def work_path(path, transaction_id):
    return path.parents[2 if path.parent.name == "plugins" else 1] / f".t3-skill-map-{transaction_id}" / path.name


def finish(journal, state):
    pending = state["pending"]
    transaction = pending["id"]
    root = Path(state["root"])
    require(re.fullmatch(r"[a-f0-9]{16}", transaction), "Invalid transaction ID")
    work_directories = set()
    for operation in pending["operations"]:
        dest = Path(operation["dest"])
        relative = dest.relative_to(root)
        profile_name, *rest = relative.parts
        require(NAME.fullmatch(profile_name) and "/".join(rest) in (SKILLS, RULES, FAST),
                f"Invalid transaction destination {dest}")
        real_directory(root / profile_name)
        real_directory(root / profile_name / "config")
        real_directory(dest.parent)
        staged = work_path(dest, transaction)
        real_directory(staged.parent)
        work_directories.add(staged.parent)
        current = identity(dest)
        before, after = operation["before"], operation["after"]
        if current != after:
            require(current == before and identity(staged) == after, f"Transaction conflict at {dest}")
            if before is None:
                staged.rename(dest)
            elif after is None:
                dest.rename(staged)
            else:
                exchange(staged, dest)
        require(identity(dest) == after and identity(staged) == before,
                f"Transaction verification failed at {dest}")
    # Commit before cleanup. An interruption during cleanup leaves only an inert backup.
    committed = {"schema": 1, "root": state["root"], "profiles": pending["profiles"]}
    atomic_json(journal, committed)
    for directory in work_directories:
        remove_owned(directory)
    return committed


def profiles_selected(root, selectors, plugins_source=None):
    real_directory(root)
    if not selectors:
        selectors = [p.name for p in root.iterdir() if not p.name.startswith(".") and (p / "config").is_dir()]
    require(selectors and len(selectors) == len(set(selectors)), "Select one or more distinct profiles")
    profiles = []
    for name in sorted(selectors):
        require(NAME.fullmatch(name), f"Invalid profile name {name}")
        profile = root / name
        real_directory(profile)
        real_directory(profile / "config")
        plugins_dir = profile / "config/plugins"
        if plugins_dir.is_symlink() and plugins_source is not None:
            require(plugins_dir.resolve() == plugins_source.resolve(), f"Unexpected plugins link target {plugins_dir}")
            plugins_dir.unlink()
            plugins_dir.mkdir(mode=0o755)
            for plugin in plugins_source.iterdir():
                if not plugin.name.startswith("."):
                    (plugins_dir / plugin.name).symlink_to(plugin)
        real_directory(plugins_dir)
        config = profile / "config/config.json"
        if config.exists():
            require(config.is_file() and not config.is_symlink(), f"Unexpected profile preferences {config}")
        profiles.append(profile)
    return profiles


def original_profile(profile, source, upstream):
    def original_link(rel, target):
        path = profile / rel
        require(path.is_symlink() and path.resolve() == target,
                f"Expected original link at {path} pointing to {target}")
        return identity(path)

    require(identity(profile / RULES) is None, f"Routing plugin already exists in {profile}")
    return {
        SKILLS: original_link(SKILLS, source),
        RULES: None,
        FAST: original_link(FAST, upstream) if (profile / FAST).is_symlink() else None,
    }


def check_enabled(profile, lazy):
    config = profile / "config/config.json"
    plugins = read_json(config).get("plugins", {}) if config.is_file() and config.stat().st_size else {}
    for plugin in [ROUTER] + (["agy-fast"] if lazy else []):
        require(plugins.get(plugin, {}).get("enabled") is not False, f"{plugin} is disabled in {config}")


def summary(rows, generated):
    original = sum(len(r["original"].encode()) for r in rows.values())
    compact = sum(len(r["description"].encode()) for r in rows.values())
    rule = len(generated[RULES]["rules/AGENTS.md"])
    return {
        "skills": len(rows),
        "pstack": sum(r["pstack"] for r in rows.values()),
        "description_bytes": {"before": original, "after": compact, "saved": original - compact},
        "added_rules_bytes": rule,
    }


def run(args):
    root = args.profiles_dir.resolve(strict=True)
    source = args.source_skills_dir.resolve()
    pstack = args.pstack_dir.resolve()
    upstream = args.agy_fast_dir.resolve()
    state_dir = (args.state_dir or root / ".t3-skill-map-state").resolve()
    protected = [Path.home() / name for name in (".gemini", ".claude", ".codex", "Developer")]
    protected += [source, pstack, upstream]
    for path in (root, state_dir):
        require(not any(path.is_relative_to(p.resolve()) or p.resolve().is_relative_to(path) for p in protected),
                f"Write root overlaps authoritative sources or protected directories: {path}")
    plugins_source = upstream.parent if upstream.parent.is_dir() else None
    profiles = profiles_selected(root, args.profile, plugins_source=plugins_source)
    for profile in profiles:
        require(not state_dir.is_relative_to(profile), f"State directory is inside profile {profile}")
    journal = state_dir / "state.json"
    if journal.exists() or journal.is_symlink():
        require(journal.is_file() and not journal.is_symlink(), f"Invalid journal {journal}")
    state = read_json(journal) if journal.exists() else {"schema": 1, "root": str(root), "profiles": {}}
    require(state["schema"] == 1 and state.get("root", str(root)) == str(root), f"Invalid state {journal}")
    if state.get("pending"):
        require(args.apply or args.rollback, "Interrupted transaction. Resume with --apply or --rollback.")
        state = finish(journal, state)
    records = state["profiles"]
    rows = None
    generated = None
    report = {}
    if args.apply or args.dry_run or args.status:
        rows = collect(source, pstack)
        generated = outputs(rows, source, pstack)
        if args.apply or args.dry_run:
            report = summary(rows, generated)
        any_lazy = args.agy_fast_lazy or any(
            records.get(str(profile), {}).get("lazy", False) for profile in profiles
        )
        if any_lazy and FAST not in generated:
            generated[FAST] = fast_outputs(upstream)
        report["source_fingerprint"] = source_fingerprint(rows, source, pstack, upstream if any_lazy else None)

    desired_hashes = None
    if generated is not None:
        desired_hashes = {
            "skills": payload_hash(generated[SKILLS]),
            "rules_plain": payload_hash(generated[RULES]),
            "rules_fast": payload_hash(fast_rules(generated[RULES])),
            "fast": payload_hash(generated[FAST]) if FAST in generated else None,
        }

    changes = []
    profile_report = {}
    for profile in profiles:
        key = str(profile)
        managed = key in records
        record = records.get(key)
        if record is None:
            original = original_profile(profile, source, upstream)
            record = {"original": original, "current": dict(original), "lazy": False}
        actual = {rel: identity(profile / rel) for rel in (SKILLS, RULES, FAST)}
        resynced_fast = is_fast_resynced(profile, record, upstream, actual[FAST])
        drift = []
        for rel, wanted in record["current"].items():
            if rel == FAST and resynced_fast:
                continue
            if actual[rel] != wanted:
                drift.append(rel)

        source_stale = False
        if managed and desired_hashes is not None:
            expected_rules = desired_hashes["rules_fast"] if record["lazy"] else desired_hashes["rules_plain"]
            expected_fast = desired_hashes["fast"] if record["lazy"] else record["original"][FAST]
            source_stale = (
                record["current"][SKILLS] != desired_hashes["skills"]
                or record["current"][RULES] != expected_rules
                or record["current"][FAST] != expected_fast
            )
        needs_apply = (not managed) or source_stale or resynced_fast
        profile_report[profile.name] = {
            "managed": managed,
            "agy_fast_lazy": record["lazy"],
            "agy_fast_resynced": resynced_fast,
            "source_stale": source_stale,
            "drift": drift,
            "needs_apply": needs_apply,
        }
        if args.status:
            continue
        require(not drift, f"Profile drift in {profile}: {', '.join(drift)}")
        if args.rollback:
            payloads = {rel: value["link"] if value else None for rel, value in record["original"].items()}
            records.pop(key, None)
        else:
            lazy = args.agy_fast_lazy or record["lazy"]
            check_enabled(profile, lazy)
            payloads = {rel: files for rel, files in generated.items() if rel != FAST}
            if lazy:
                require(record["original"][FAST] is not None, f"agy-fast link missing in {profile}")
                payloads[FAST] = generated[FAST]
                payloads[RULES] = fast_rules(generated[RULES])
                profile_report[profile.name]["planned_rules_bytes"] = len(payloads[RULES]["rules/AGENTS.md"])
            records[key] = {**record, "lazy": lazy, "current": dict(record["current"])}
        for rel, payload in payloads.items():
            before = actual[rel]
            after = ({"link": payload} if isinstance(payload, str)
                     else payload_hash(payload) if payload is not None else None)
            if before != after:
                changes.append((profile / rel, before, after, payload))
            if args.apply or args.dry_run:
                records[key]["current"][rel] = after

    report.update({"profiles": profile_report, "changed_paths": [str(path) for path, *_ in changes]})
    if args.dry_run or args.status or not changes:
        print(json_text(report), end="")
        if args.status and args.check and any(p["drift"] or p["needs_apply"] for p in profile_report.values()):
            sys.exit(3)
        return
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    transaction = os.urandom(8).hex()
    operations = []
    for dest, before, after, payload in changes:
        require(identity(dest) == before, f"Concurrent change at {dest}")
        staged = work_path(dest, transaction)
        staged.parent.mkdir(exist_ok=True, mode=0o700)
        write_payload(staged, payload)
        require(identity(staged) == after, f"Invalid stage {staged}")
        operations.append({"dest": str(dest), "before": before, "after": after})
    state["pending"] = {"id": transaction, "operations": operations, "profiles": records}
    atomic_json(journal, state)
    finish(journal, state)
    print(json_text(report), end="")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for option in ("dry-run", "apply", "rollback", "status"):
        mode.add_argument(f"--{option}", action="store_true")
    gemini = Path.home() / ".gemini/config"
    parser.add_argument("--profiles-dir", type=Path, required=True)
    parser.add_argument("--profile", action="append", help="Profile directory name. Repeat to select multiple.")
    parser.add_argument("--source-skills-dir", type=Path, default=gemini / "skills")
    parser.add_argument("--pstack-dir", type=Path, default=gemini / "plugins/pstack")
    parser.add_argument("--agy-fast-dir", type=Path, default=gemini / "plugins/agy-fast")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--agy-fast-lazy", action="store_true")
    parser.add_argument("--check", action="store_true", help="With --status, exit 3 if any profile has drift or needs apply.")
    args = parser.parse_args()
    require(not args.agy_fast_lazy or args.apply or args.dry_run, "--agy-fast-lazy requires --apply or --dry-run")
    require(not args.check or args.status, "--check requires --status")
    if args.apply or args.rollback:
        root = args.profiles_dir.resolve(strict=True)
        require(not root.is_relative_to(Path.home() / ".gemini"), f"Refusing standalone write to {root}")
        # Lock the real profiles directory inode, independently of the selected state directory.
        lock = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            run(args)
        finally:
            os.close(lock)
    else:
        run(args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print(f"sync-t3-skills: {error}", file=sys.stderr)
        sys.exit(1)
