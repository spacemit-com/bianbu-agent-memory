# Public source synchronization

[English](PUBLIC_SYNC.md) | [简体中文](PUBLIC_SYNC_CN.md)

The GitLab development repository is the implementation source; GitHub maintains a separate public snapshot history. Do not force-push internal history into the public repository.

For each synchronization:

1. Record the internal source commit, release tag, and public base commit.
2. Compare implementation changes and apply them to a clean public checkout. Preserve public-only documentation and test adaptations. GitHub Actions checks are not currently provided.
3. Exclude internal CI configuration, credentials, private logs, databases, exports, signing keys, and device evidence. Replace private device addresses and home paths with documented placeholders.
4. Preserve each executable file's mode. Do not apply global text replacements to binary files.
5. Review the complete diff and run tests from a fresh public checkout, rather than substituting internal-repository test results.
6. Verify relative links and bilingual documentation, then publish a normal public commit with its source baseline recorded.

Current public adaptations include sanitized device examples and the greeting test's newest-record expectation. The companion public plugin repository tests manifest/package version agreement without requiring internal Debian packaging files. Keep those checks independent of distribution package version numbers.

Changes to behavior or shared regression tests should also be proposed to GitLab. Public-only adaptations must be listed explicitly during every subsequent sync so they are not overwritten.
