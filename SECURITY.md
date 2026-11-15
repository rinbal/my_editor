# Security

MyEditor handles private writing and, for people who use Nostr, private keys. Please report a security problem privately, so it can be fixed before anyone can misuse it.

## Report a problem

Use GitHub's private reporting: open the repository's [Security tab](https://github.com/rinbal/my_editor/security) and choose **Report a vulnerability**. Only the maintainer sees the report.

Please include what an attacker could do, the steps to reproduce it, and the version and system you used. Do not open a public issue, and do not share the problem elsewhere until it is fixed.

You will get an answer as soon as the maintainer has looked at it, and you will hear when a fix is released. With your permission, you are credited in the release notes.

## Supported versions

Fixes go into the latest release. Updating is the way to get them: MyEditor offers each new version in the app (**Help > Check for Updates…**).

| Version | Supported |
|---|---|
| The latest release | Yes |
| Older releases | No |

## What is in scope

MyEditor itself, its installers and its install guide, and the membership service in `sidecar/`. Problems in a Nostr relay, a signer app or a media server belong to those projects; if MyEditor trusts them more than it should, that is in scope.
