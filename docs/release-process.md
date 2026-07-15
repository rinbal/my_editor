# MyEditor Release Runbook

Reusable runbook for cutting a MyEditor release. Paste the prompt block below into
a Claude Code session at the repository root, fill in the version, and it writes
the release notes and ships the release end to end.

When this finishes:
  - Release notes are written to `docs/releases/v{VERSION}.md` and committed.
  - The latest code is pushed to main.
  - Tag v{VERSION} is pushed, which triggers GitHub Actions to build the Windows,
    macOS (Apple Silicon + Intel) and Linux one-click installers and publish a
    GitHub Release that uses the release notes as its body and attaches the three
    installers as assets.

The three OS installers can only be built on their own runners, so the build and
the asset upload happen in CI (`.github/workflows/build-installers.yml`), not
locally. Everything the local machine needs is git and python.

---

## Prompt (copy from here)

You are cutting release v{VERSION} of MyEditor on the rinbal/my_editor repo. Work
at the repository root. Do not use em dashes anywhere.

### Step 1. Confirm the version
If {VERSION} was not given, ask for it. Never guess. The tag will be v{VERSION}.

### Step 2. Write the release notes
1. Read the git state, read-only. Do not stash or discard anything:
   - git status
   - git describe --tags
   - LAST=$(git describe --tags --abbrev=0) for the previous release tag
   - git log "$LAST"..HEAD --oneline for the commit list since that tag
   - git diff "$LAST" --stat for overall scope
2. Find every "Merge pull request #N" in that range. If the gh CLI is installed,
   run `gh pr view N --repo rinbal/my_editor --json number,title,author,url` for
   real PR authorship. If gh is not installed, use the commit authors
   (git log --format='%an <%ae>' "$LAST"..HEAD) and the PR branch names to
   attribute the work.
3. Where a commit's purpose is not obvious, read the real diff (git show <hash>,
   git diff "$LAST"..HEAD -- <path>) before describing it. Never guess, and verify
   every feature against the actual diff.
4. Write the notes to `docs/releases/v{VERSION}.md` in exactly this structure:

```markdown
# MyEditor v{VERSION}

## Highlights

- User-facing, notable changes only. Curated and synthesized, not a 1:1 commit
  dump. Merge related commits into one bullet. Bold a short feature name, then one
  plain-language line. Most impactful first.

## Technical updates & fixes

- Under-the-hood changes: refactors, bug fixes, dependency bumps, platform fixes,
  tooling. Short, terse bullets.

## Contributors

Thanks to everyone who shipped this release:

- One bullet per person who authored a merged PR in this range. Link each name to
  https://github.com/<login> . For external contributors, name what they
  contributed and link a representative PR, for example
  - [@name](https://github.com/name): short description ([#N](https://github.com/rinbal/my_editor/pull/N))

## Community

- Nostr: [Community Chat](https://nostr-ecosystem.netlify.app/join/g/groups.0xchat.com/my-editor-public-talk?n=My+Editor+-+Public+Talk&a=A+community+for+users%2C+contributors%2C+and+anyone+interested+in+a+clean%2C+distraction-free+note-taking+editor+built+with+Python+and+PySide6.%0A%0AS&p=https%3A%2F%2Fblossom.primal.net%2F6173a8dc06038a1d67d6149755166fb48d74d92d41ef6af8b6cd863489eb3095)

## Download

- Github: https://github.com/rinbal/my_editor
```

The Community and Download sections are always exactly those links, verbatim.
Style: valuable and concise. No filler, no padding. This is a GitHub release
message, not documentation.

### Step 3. Commit everything
Review git status. Commit all code intended for this release with clear messages,
leaving out build junk and scratch files. Then commit `docs/releases/v{VERSION}.md`.
The working tree must be clean before the next step. Do not bump APP_VERSION by
hand, the release script does that.

### Step 4. Ship it
This step pushes to main and publishes a public release, so confirm with the user
first. Then run:

    bash packaging/release.sh {VERSION} --yes

That script bumps APP_VERSION in constants.py, commits it, pushes the code to main,
and pushes the tag v{VERSION}. The tag push starts the CI build. When CI finishes,
verify that https://github.com/rinbal/my_editor/releases/tag/v{VERSION} has the
three installers attached and the release notes as its body.

## Prompt (copy to here)

---

## Usage

- Replace {VERSION} with the target version (for example 3.0) before sending. If
unsure what the next version should be, ask rather than guessing.

- DO NOT use em dashes

- Compare Dev against main
