#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { cpSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readlinkSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function parseArgs(argv) {
  const opts = {
    check: false,
    push: false,
    install: true,
    installDir: "",
    remote: "upstream",
    branch: "main",
    selfTest: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--check") {
      opts.check = true;
    } else if (arg === "--push") {
      opts.push = true;
    } else if (arg === "--no-install") {
      opts.install = false;
    } else if (arg === "--self-test") {
      opts.selfTest = true;
    } else if (arg === "--install-dir" && i + 1 < argv.length) {
      opts.installDir = resolve(argv[++i]);
    } else if (arg === "--remote" && i + 1 < argv.length) {
      opts.remote = argv[++i];
    } else if (arg === "--branch" && i + 1 < argv.length) {
      opts.branch = argv[++i];
    } else {
      throw new Error(`Unknown or incomplete option: ${arg}`);
    }
  }
  return opts;
}

function run(cmd, args, cwd, { allowFailure = false, inherit = false } = {}) {
  const res = spawnSync(cmd, args, {
    cwd,
    encoding: "utf8",
    stdio: inherit ? "inherit" : "pipe",
  });
  if (res.error) throw res.error;
  if (!allowFailure && res.status !== 0) {
    const detail = [res.stderr, res.stdout].filter(Boolean).join("\n").trim();
    throw new Error(`${cmd} ${args.join(" ")} failed (exit ${res.status}): ${detail}`);
  }
  return res;
}

function git(args, cwd, options = {}) {
  return run("git", args, cwd, options);
}

function gitOut(args, cwd) {
  return git(args, cwd).stdout.trim();
}

function parseLines(text) {
  return text ? text.split("\n").map((s) => s.trim()).filter(Boolean) : [];
}

function runHarnessScripts(repoRoot, opts) {
  run("node", ["scripts/generate-pstack-harness-trees.mjs", "--check"], repoRoot, { inherit: true });
  run("python3", ["scripts/check-pstack-parity.py"], repoRoot, { inherit: true });
  run("node", ["scripts/check-pstack-docs.mjs"], repoRoot, { inherit: true });
  if (opts.install) {
    const agyArgs = ["scripts/generate-pstack-agy.mjs", "--install"];
    if (opts.installDir) agyArgs.push("--install-dir", opts.installDir);
    run("node", agyArgs, repoRoot, { inherit: true });
  }
}

function printUpstreamSummary(repoRoot, remoteRef) {
  const behind = Number.parseInt(gitOut(["rev-list", "--count", `HEAD..${remoteRef}`], repoRoot), 10);
  const ahead = Number.parseInt(gitOut(["rev-list", "--count", `${remoteRef}..HEAD`], repoRoot), 10);
  const commits = parseLines(gitOut(["log", "--oneline", `HEAD..${remoteRef}`], repoRoot));
  const touched = parseLines(gitOut(["diff", "--name-only", `HEAD...${remoteRef}`], repoRoot)).filter(
    (f) => f.startsWith("pstack/") || f.startsWith("harness/") || f.startsWith("scripts/"),
  );
  console.log(`Status vs ${remoteRef}: ahead ${ahead}, behind ${behind}`);
  for (const line of commits.slice(0, 30)) console.log(`  ${line}`);
  if (commits.length > 30) console.log(`  ... (${commits.length} total)`);
  if (touched.length > 0) {
    console.log(`Upstream files affecting pstack/harness/scripts (${touched.length}):`);
    for (const f of touched.slice(0, 30)) console.log(`  ${f}`);
  } else if (behind > 0) {
    console.log("Upstream changes are outside pstack/harness/scripts.");
  }
  return behind;
}

function syncRepo(repoRoot, opts) {
  const dirty = gitOut(["status", "--porcelain"], repoRoot);
  if (!opts.check && dirty) {
    throw new Error(`Working tree in ${repoRoot} is not clean; commit or stash changes before syncing.\n${dirty}`);
  }

  git(["fetch", opts.remote, opts.branch], repoRoot, { inherit: true });
  const remoteRef = `${opts.remote}/${opts.branch}`;
  const upstreamSha = gitOut(["rev-parse", "--short", remoteRef], repoRoot);

  if (opts.check) {
    printUpstreamSummary(repoRoot, remoteRef);
    return { status: "checked", upstreamSha };
  }

  const ancestorRes = git(["merge-base", "--is-ancestor", remoteRef, "HEAD"], repoRoot, { allowFailure: true });
  if (ancestorRes.status === 0) {
    runHarnessScripts(repoRoot, opts);
    if (opts.push) git(["push", "origin", "HEAD"], repoRoot, { inherit: true });
    console.log(`Already up to date with ${remoteRef} (${upstreamSha}).`);
    return { status: "up_to_date", upstreamSha };
  }

  const mergeRes = git(["merge", "--no-commit", "--no-ff", remoteRef], repoRoot, { allowFailure: true });
  if (mergeRes.status !== 0) {
    const conflicts = parseLines(gitOut(["diff", "--name-only", "--diff-filter=U"], repoRoot));
    const generated = conflicts.filter((f) => f.startsWith("harness/pstack/"));
    const source = conflicts.filter((f) => !f.startsWith("harness/pstack/"));
    if (source.length > 0) {
      git(["merge", "--abort"], repoRoot, { allowFailure: true });
      throw new Error(`Upstream merge aborted due to source conflicts:\n${source.map((f) => `  ${f}`).join("\n")}`);
    }
    if (conflicts.length === 0) {
      git(["merge", "--abort"], repoRoot, { allowFailure: true });
      throw new Error(`git merge ${remoteRef} failed:\n${mergeRes.stderr || mergeRes.stdout}`);
    }
    if (generated.length > 0) {
      console.log(`Auto-resolving ${generated.length} generated conflict(s) under harness/pstack/...`);
    }
  }

  try {
    run("node", ["scripts/generate-pstack-harness-trees.mjs"], repoRoot, { inherit: true });
    git(["add", "-A", "harness/pstack"], repoRoot);
    const remaining = parseLines(gitOut(["diff", "--name-only", "--diff-filter=U"], repoRoot));
    if (remaining.length > 0) {
      throw new Error(`Unmerged paths remain after regenerating harness/pstack/: ${remaining.join(", ")}`);
    }
    runHarnessScripts(repoRoot, opts);
  } catch (err) {
    git(["merge", "--abort"], repoRoot, { allowFailure: true });
    throw err;
  }

  git(
    ["commit", "-m", `chore(sync): merge upstream cursor/plugins (${upstreamSha}) and regenerate pstack packages`],
    repoRoot,
    { inherit: true },
  );
  if (opts.push) git(["push", "origin", "HEAD"], repoRoot, { inherit: true });
  console.log(`Synced ${remoteRef} (${upstreamSha}) cleanly.`);
  return { status: "merged", upstreamSha };
}

function seedMinimalRepo(targetDir) {
  cpSync(join(ROOT, "pstack"), join(targetDir, "pstack"), { recursive: true });
  cpSync(join(ROOT, "cursor-team-kit", "skills"), join(targetDir, "cursor-team-kit", "skills"), { recursive: true });
  for (const name of ["generate-pstack-agy.mjs", "generate-pstack-harness-trees.mjs", "check-pstack-docs.mjs", "check-pstack-parity.py", "sync-upstream.mjs"]) {
    const dst = join(targetDir, "scripts", name);
    mkdirSync(dirname(dst), { recursive: true });
    cpSync(join(ROOT, "scripts", name), dst);
  }
  run("node", ["scripts/generate-pstack-harness-trees.mjs"], targetDir);
}

function initGitUser(repoDir) {
  git(["config", "user.name", "Test User"], repoDir);
  git(["config", "user.email", "test@example.com"], repoDir);
}

function runSelfTest() {
  const base = mkdtempSync(join(tmpdir(), "pstack-sync-test-"));
  try {
    const upstreamBare = join(base, "upstream.git");
    const workUp = join(base, "work-upstream");
    const forkDir = join(base, "fork");
    const installDir = join(base, "installed-pstack");

    git(["init", "--bare", "-b", "main", upstreamBare], base);
    mkdirSync(workUp, { recursive: true });
    git(["init", "-b", "main", workUp], base);
    initGitUser(workUp);
    seedMinimalRepo(workUp);
    git(["add", "."], workUp);
    git(["commit", "-m", "initial pstack"], workUp);
    git(["remote", "add", "origin", upstreamBare], workUp);
    git(["push", "-u", "origin", "main"], workUp);

    git(["clone", "--branch", "main", upstreamBare, forkDir], base);
    git(["remote", "rename", "origin", "upstream"], forkDir);
    initGitUser(forkDir);

    const readmePath = join(forkDir, "harness", "pstack", "README.md");
    const broPathUp = join(workUp, "pstack", "skills", "bro", "SKILL.md");
    const readmePathUp = join(workUp, "harness", "pstack", "README.md");

    writeFileSync(readmePath, `${readFileSync(readmePath, "utf8")}\n<!-- fork-side generated edit -->\n`, "utf8");
    git(["add", "harness/pstack/README.md"], forkDir);
    git(["commit", "-m", "fork generated header note"], forkDir);

    writeFileSync(broPathUp, `${readFileSync(broPathUp, "utf8")}\nUpstream sync marker sentence.\n`, "utf8");
    run("node", ["scripts/generate-pstack-harness-trees.mjs"], workUp);
    writeFileSync(readmePathUp, `${readFileSync(readmePathUp, "utf8")}\n<!-- upstream-side conflict line -->\n`, "utf8");
    git(["add", "-A"], workUp);
    git(["commit", "-m", "upstream update bro skill and README"], workUp);
    git(["push", "origin", "main"], workUp);

    const customRules = "# custom model rule\nfeature, refactoring: pro\n";
    mkdirSync(join(installDir, "rules"), { recursive: true });
    writeFileSync(join(installDir, "rules", "AGENTS.md"), customRules, "utf8");

    const scriptPath = join(forkDir, "scripts", "sync-upstream.mjs");
    const checkOut = run("node", [scriptPath, "--check"], forkDir);
    if (!checkOut.stdout.includes("behind 1")) {
      throw new Error(`Expected --check to report behind 1, got:\n${checkOut.stdout}`);
    }

    const r1 = run("node", [scriptPath, "--install-dir", installDir], forkDir);
    for (const tree of ["claude-code", "antigravity", "codex"]) {
      const mergedBro = readFileSync(join(forkDir, "harness", "pstack", tree, "skills", "bro", "SKILL.md"), "utf8");
      if (!mergedBro.includes("Upstream sync marker sentence.")) {
        throw new Error(`Expected regenerated ${tree} bro skill to contain upstream update; stdout=${r1.stdout}`);
      }
    }
    const installedBro = readFileSync(join(installDir, "skills", "bro", "SKILL.md"), "utf8");
    if (!installedBro.includes("Upstream sync marker sentence.")) {
      throw new Error("Expected installed bro skill to be updated by sync");
    }
    const preserved = readFileSync(join(installDir, "rules", "AGENTS.md"), "utf8");
    if (preserved !== customRules) {
      throw new Error(`Expected custom AGENTS.md without marker to be preserved, got:\n${preserved}`);
    }
    if (gitOut(["status", "--porcelain"], forkDir) !== "") {
      throw new Error("Expected clean working tree after merge");
    }

    const r2 = run("node", [scriptPath, "--install-dir", installDir], forkDir);
    if (!r2.stdout.includes("Already up to date")) {
      throw new Error(`Expected idempotent second sync, got:\n${r2.stdout}`);
    }

    const agyScript = join(forkDir, "scripts", "generate-pstack-agy.mjs");
    const skillSyncInstallDir = join(base, "installed-pstack-with-skills");
    const skillsDir = join(base, "agy-skills");
    mkdirSync(join(skillsDir, "custom-skill"), { recursive: true });
    writeFileSync(
      join(skillsDir, "custom-skill", "SKILL.md"),
      '---\nname: custom-skill\ndescription: "Custom non-pstack test skill."\n---\n\nBody.\n',
      "utf8",
    );
    symlinkSync(join(skillSyncInstallDir, "skills", "obsolete-skill"), join(skillsDir, "obsolete-skill"), "dir");

    const s1 = run("node", [agyScript, "--install", "--install-dir", skillSyncInstallDir, "--skills-dir", skillsDir], forkDir);
    if (!s1.stdout.includes("Synced skill links")) {
      throw new Error(`Expected skill link sync output, got:\n${s1.stdout}`);
    }
    if (readlinkSync(join(skillsDir, "reframe")) !== join(skillSyncInstallDir, "skills", "reframe")) {
      throw new Error("Expected reframe skill symlink to be installed");
    }
    try {
      lstatSync(join(skillsDir, "obsolete-skill"));
      throw new Error("Expected stale pstack symlink obsolete-skill to be removed");
    } catch (err) {
      if (err?.code !== "ENOENT") throw err;
    }
    if (lstatSync(join(skillsDir, "custom-skill")).isSymbolicLink() || !existsSync(join(skillsDir, "custom-skill", "SKILL.md"))) {
      throw new Error("Expected non-pstack custom-skill directory to stay intact");
    }
    const s2 = run("node", [agyScript, "--install", "--install-dir", skillSyncInstallDir, "--skills-dir", skillsDir], forkDir);
    if (s2.stdout.includes("Synced skill links")) {
      throw new Error(`Expected idempotent second skill-link sync, got:\n${s2.stdout}`);
    }

    const t3Profiles = join(base, "t3-profiles");
    const t3State = join(base, "t3-state");
    const noAgyFast = join(base, "no-agy-fast");
    const t3ProfileCfg = join(t3Profiles, "profile-ci", "config");
    mkdirSync(join(t3ProfileCfg, "plugins"), { recursive: true });
    symlinkSync(skillsDir, join(t3ProfileCfg, "skills"), "dir");
    writeFileSync(join(t3ProfileCfg, "config.json"), JSON.stringify({ plugins: { swarm: { enabled: true } } }), "utf8");

    const t3SyncScript = join(skillSyncInstallDir, "docs", "harness", "sync-t3-skills.py");
    const runT3Sync = (...flags) =>
      run(
        "python3",
        [
          t3SyncScript,
          "--profiles-dir",
          t3Profiles,
          "--source-skills-dir",
          skillsDir,
          "--pstack-dir",
          skillSyncInstallDir,
          "--agy-fast-dir",
          noAgyFast,
          "--state-dir",
          t3State,
          ...flags,
        ],
        forkDir,
        { allowFailure: true },
      );

    const t3InitApply = runT3Sync("--apply");
    if (t3InitApply.status !== 0) {
      throw new Error(`Expected initial sync-t3-skills --apply to succeed: ${t3InitApply.stderr || t3InitApply.stdout}`);
    }
    const t3InitCheck = runT3Sync("--status", "--check");
    if (t3InitCheck.status !== 0) {
      throw new Error(`Expected initial sync-t3-skills --status --check exit 0, got ${t3InitCheck.status}: ${t3InitCheck.stderr || t3InitCheck.stdout}`);
    }
    if (!existsSync(join(t3ProfileCfg, "skills", "reframe", "SKILL.md")) || !existsSync(join(t3ProfileCfg, "skills", "custom-skill", "SKILL.md"))) {
      throw new Error("Expected initial T3 profile stubs for reframe and custom-skill");
    }

    const syntheticSkillDir = join(forkDir, "pstack", "skills", "synthetic-upstream-skill");
    mkdirSync(syntheticSkillDir, { recursive: true });
    writeFileSync(
      join(syntheticSkillDir, "SKILL.md"),
      '---\nname: synthetic-upstream-skill\ndescription: "Synthetic skill added by an upstream sync test. Use for /synthetic-upstream-skill."\ndisable-model-invocation: true\n---\n\n# Synthetic upstream skill\n\nBody.\n',
      "utf8",
    );
    run("node", [agyScript, "--install", "--install-dir", skillSyncInstallDir, "--skills-dir", skillsDir], forkDir);
    if (readlinkSync(join(skillsDir, "synthetic-upstream-skill")) !== join(skillSyncInstallDir, "skills", "synthetic-upstream-skill")) {
      throw new Error("Expected synthetic-upstream-skill symlink after install");
    }
    const t3AddStatus = runT3Sync("--status", "--check");
    if (t3AddStatus.status !== 3) {
      throw new Error(`Expected exit 3 after adding upstream skill, got ${t3AddStatus.status}: ${t3AddStatus.stderr || t3AddStatus.stdout}`);
    }
    const t3AddReport = JSON.parse(t3AddStatus.stdout).profiles["profile-ci"];
    if (!t3AddReport.source_stale || !t3AddReport.needs_apply || t3AddReport.drift.length !== 0) {
      throw new Error(`Unexpected T3 status after upstream skill addition: ${t3AddStatus.stdout}`);
    }
    const t3AddApply = runT3Sync("--apply");
    if (t3AddApply.status !== 0) {
      throw new Error(`Expected sync-t3-skills --apply after addition to succeed: ${t3AddApply.stderr || t3AddApply.stdout}`);
    }
    if (runT3Sync("--status", "--check").status !== 0) {
      throw new Error("Expected sync-t3-skills --status --check exit 0 after applying addition");
    }
    const t3RulesPath = join(t3ProfileCfg, "plugins", "t3-skill-map", "rules", "AGENTS.md");
    if (!existsSync(join(t3ProfileCfg, "skills", "synthetic-upstream-skill", "SKILL.md"))) {
      throw new Error("Expected synthetic-upstream-skill stub in T3 profile");
    }
    if (!readFileSync(t3RulesPath, "utf8").includes("synthetic-upstream-skill")) {
      throw new Error("Expected synthetic-upstream-skill in T3 skill-map rules/AGENTS.md");
    }

    rmSync(syntheticSkillDir, { recursive: true, force: true });
    run("node", [agyScript, "--install", "--install-dir", skillSyncInstallDir, "--skills-dir", skillsDir], forkDir);
    try {
      lstatSync(join(skillsDir, "synthetic-upstream-skill"));
      throw new Error("Expected synthetic-upstream-skill symlink to be pruned after removal");
    } catch (err) {
      if (err?.code !== "ENOENT") throw err;
    }
    const t3RemStatus = runT3Sync("--status", "--check");
    if (t3RemStatus.status !== 3) {
      throw new Error(`Expected exit 3 after removing upstream skill, got ${t3RemStatus.status}: ${t3RemStatus.stderr || t3RemStatus.stdout}`);
    }
    const t3RemReport = JSON.parse(t3RemStatus.stdout).profiles["profile-ci"];
    if (!t3RemReport.source_stale || t3RemReport.drift.length !== 0) {
      throw new Error(`Unexpected T3 status after upstream skill removal: ${t3RemStatus.stdout}`);
    }
    const t3RemApply = runT3Sync("--apply");
    if (t3RemApply.status !== 0) {
      throw new Error(`Expected sync-t3-skills --apply after removal to succeed: ${t3RemApply.stderr || t3RemApply.stdout}`);
    }
    if (runT3Sync("--status", "--check").status !== 0) {
      throw new Error("Expected sync-t3-skills --status --check exit 0 after applying removal");
    }
    if (existsSync(join(t3ProfileCfg, "skills", "synthetic-upstream-skill"))) {
      throw new Error("Expected synthetic-upstream-skill stub to be removed from T3 profile");
    }
    if (readFileSync(t3RulesPath, "utf8").includes("synthetic-upstream-skill")) {
      throw new Error("Expected synthetic-upstream-skill to be removed from T3 rules/AGENTS.md");
    }
    if (!existsSync(join(skillsDir, "custom-skill", "SKILL.md")) || !existsSync(join(t3ProfileCfg, "skills", "custom-skill", "SKILL.md"))) {
      throw new Error("Expected custom-skill to remain intact after upstream add/remove cycle");
    }

    console.log("ALL sync-upstream.mjs SELF-TESTS PASSED");
  } finally {
    rmSync(base, { recursive: true, force: true });
  }
}

const opts = parseArgs(process.argv.slice(2));
if (opts.selfTest) {
  runSelfTest();
} else {
  syncRepo(ROOT, opts);
}
