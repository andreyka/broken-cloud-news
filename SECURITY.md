# Security Policy

## Reporting

Do not publish unpatched vulnerabilities or private credentials in a public issue.

If GitHub private vulnerability reporting is enabled for this repository, use that channel.
Otherwise, contact the maintainer privately before opening a public report.

## Scope

Security reports are most useful when they include:

- affected component or endpoint
- reproduction steps
- impact and expected blast radius
- any relevant logs, stack traces, or proof-of-concept details

## Secrets

This repository should not contain live secrets. If you believe a credential has been exposed:

1. rotate the credential first
2. report the exposure privately
3. treat git history, caches, forks, and CI logs as potential secondary exposure paths
