# Contributing

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING_CN.md)

Please describe the problem, expected behavior, and verification in your pull request. Keep changes focused and update documentation and regression tests when behavior changes.

## Reporting problems

Include the source commit, package versions, operating system, reproduction steps, expected result, and actual result. Use synthetic examples. Never attach real memory exports, user databases, credentials, signing keys, or unredacted conversation logs to a public issue.

## Versions

See [VERSIONING.md](VERSIONING.md) for the relationship between source metadata and Debian package versions.

## Development checks

GitHub Actions checks are not currently provided. Run the local checks below. Internal GitLab pipelines build and publish distribution packages; they are not GitHub PR verification.

No dedicated private vulnerability reporting channel is currently published. Do not disclose exploit details or sensitive data in public issues or PRs. Contact the project maintainers to arrange a suitable reporting method before sharing details.

The daemon targets Linux. Follow the build prerequisites in README.md, then run:

```bash
python -m pip install -e 'sdk/python[test]'
cargo fmt --all -- --check
cargo test --workspace --locked
python -m pytest sdk/python/tests -q
```

K3, real Gateway, power-loss, and soak tests are device-specific checks. Do not run them on a device with an active acceptance or soak test without coordinating with its owner. Report these separately from local tests.

Production identity checks must remain enabled. Use the documented isolated development stack for examples and tests.
