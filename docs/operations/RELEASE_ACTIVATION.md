# AEL release, mirror and Homebrew activation

This document is the activation contract for AI Engineering Ledger. It does not
grant credentials, change repository visibility, create tags, or publish a
release by itself.

## Release topology

```text
private Forgejo canonical
  git.langevc.com/langevc/ai-engineering-ledger
            |
            | exact refs, gated
            v
public GitHub read-only mirror
  github.com/LangeVC/ai-engineering-ledger
            |
            | immutable vX.Y.Z source archive + SHA-256
            v
public GitHub Homebrew tap
  github.com/LangeVC/homebrew-tap
```

The canonical repository remains private. GitHub is a public distribution
surface and must never originate commits, branches, or tags that belong to the
canonical release line.

## Repository state required before activation

1. Configure this checkout's canonical remote:

   ```bash
   git remote add origin https://git.langevc.com/langevc/ai-engineering-ledger.git
   ```

   If `origin` already exists, verify it instead of replacing it.

2. Run the public-surface audit before changing the GitHub mirror from private
   to public. The public tree must contain no `.skillweave/` state, PRDs,
   private evidence, credentials, operator handoffs, or non-fixture personal
   paths.

3. Keep the Forgejo repository private. Set only these GitHub repositories to
   public after the corresponding audit passes:

   - `LangeVC/ai-engineering-ledger`
   - `LangeVC/homebrew-tap`

4. Approve and commit an OSS license before the first public release. A release
   without an owner-approved license is blocked.

## Forgejo Actions configuration

The workflow `.forgejo/workflows/mirror.yml` is a byte-identical deployment of
`lvc-ops/docs/templates/unified-mirror.yml`. Do not customize it per repository.
Its destination is supplied only through Actions configuration:

- organisation secret: `GH_MIRROR_TOKEN` — GitHub token with push permission
  to `LangeVC/ai-engineering-ledger`;
- organisation variable: `GH_REPO_OWNER=LangeVC`;
- repository variable: `GH_REPO=LangeVC/ai-engineering-ledger`.

The workflow's visibility gate blocks planning and contract paths, including
`.skillweave/**` and PRD files, before a public mirror push. The initial public
surface still requires an explicit audit because a gate on future changes is
not a proof that the existing history is safe.

## lvc-ops control-plane onboarding

The local `lvc-ops` canonical checkout now contains the following additive
repository entry under `langevc.repositories`; it remains an uncommitted,
separately reviewable control-plane change:

```yaml
ai-engineering-ledger:
  release:
    enabled: true
    trigger: "tag_push"
    tag_pattern: "v*"
    assets: none
  mirror:
    enabled: true
    verify_on_push: true
  destinations:
    - forge: github
      repo: LangeVC/ai-engineering-ledger
      role: mirror
      visibility: public
```

`assets: none` means the Homebrew formula consumes GitHub's immutable source
archive for the mirrored tag. If later releases attach wheels, SBOMs, or other
business-specific assets, add them explicitly rather than changing the generic
release-object mechanism.

The same local change extends the `pairs` inventory in
`lvc-ops/.forgejo/workflows/mirror-health.yml` with:

```text
ai-engineering-ledger:ai-engineering-ledger:private
```

The final field describes the canonical Forgejo visibility. Because it is
`private`, mirror-health needs the existing `FG_BOT_TOKEN` to inspect the
canonical default branch and tags; without it the check must skip neutrally.

Audit any other fixed seven-repository inventories in lvc-ops before merging
the onboarding change. Do not edit ops-engine product-specific self-release
workflows for this registration.

## ops-engine release contract

- `.version.yaml` makes `pyproject.toml` the release version source of truth.
- A tag `vX.Y.Z` is valid only when `X.Y.Z` equals that declaration.
- `.ops.yaml` declares a private Forgejo release destination and a public
  GitHub distribution destination.
- The generic ops-engine `ReleaseHandler` creates the canonical release object
  on Forgejo. An AEL-specific adapter workflow must pin a released ops-engine;
  it must not copy `ops-engine/.forgejo/workflows/forgejo-release.yml`, which
  contains ops-engine's own package, title, SBOM, and self-install policy.
- The exact tag ref is mirrored to GitHub. GitHub-side jobs may attach
  distribution assets but must not create or move the tag.

The adapter workflow is not enabled until the AEL license, changelog, release
title policy, public-surface audit, and ops-engine version pin have all passed
review.

## Homebrew activation order

1. Pass AEL tests, version gate, public-surface audit, and license gate.
2. Commit through the private Forgejo canonical and merge the release branch.
3. Create canonical tag `v0.1.0` through the governed release path.
4. Verify the exact branch and tag refs on the public GitHub mirror.
5. Resolve the public GitHub source archive and calculate its SHA-256.
6. Render `Formula/ael.rb` in the public `LangeVC/homebrew-tap` repository.
7. Run `brew style`, strict audit, formula test, source installation, upgrade,
   and reinstall persistence probes.
8. Merge the tap change only after all gates pass.

## Binary activation checklist

- [ ] Forgejo canonical remains private and has the expected `origin`.
- [ ] GitHub AEL mirror is public and writable by `GH_MIRROR_TOKEN`.
- [ ] GitHub Homebrew tap is public.
- [ ] `GH_REPO_OWNER` and `GH_REPO` match exactly.
- [ ] AEL public-surface and license gates pass.
- [ ] AEL is registered in lvc-ops config and mirror-health.
- [ ] Tag/version gate passes on Forgejo.
- [ ] GitHub branch and tag SHA match Forgejo.
- [ ] Formula source SHA matches the mirrored immutable archive.
- [ ] Homebrew style, audit, test, upgrade and reinstall gates pass.
