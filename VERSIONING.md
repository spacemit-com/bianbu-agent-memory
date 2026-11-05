# Version correspondence

[English](VERSIONING.md) | [简体中文](VERSIONING_CN.md)

The public source currently includes the functionality corresponding to Debian packages `bianbu-agent-memory` and `bianbu-agent-memoryd` 0.1.33. This describes the synchronized source baseline, not a GitHub release or a package published by this repository.

The Cargo workspace and Python SDK metadata currently use 0.1.6. These metadata versions are not the Debian release number. Include both the source commit and installed package versions when reporting an issue.

The public repository has its own snapshot history. Internal release tags and public snapshot commits need not have the same identifiers. Public snapshots preserve sanitized examples and omit internal CI configuration.

Do not infer an available Debian package from the Cargo or Python version, and do not edit the main branch Debian changelog to match a package release. Debian releases are managed by the distribution release pipeline.
