# Security policy

## Supported versions

Until the first stable release, security fixes target the latest tagged alpha
and the canonical default branch.

## Reporting

Do not disclose vulnerabilities, credentials, provider exports, screenshots, or
ledger contents in a public issue. Use GitHub's private vulnerability reporting
for `LangeVC/ai-engineering-ledger` once enabled, or contact the repository owner
through the private channel published in the organization security policy.

Include the affected version, operating system, reproduction steps, and impact.
Use synthetic data wherever possible.

## Security boundaries

- AEL is local-first and must not transmit evidence or screenshots implicitly.
- API keys and authorization headers are not valid ledger input.
- Account aliases should not contain email addresses or provider credentials.
- Local databases, inboxes, evidence, exports, and backups must remain private.
- A public GitHub repository is a read-only distribution mirror; releases and
  tags originate from the private canonical Forgejo repository.
