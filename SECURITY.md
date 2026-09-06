# Security

## Reporting a vulnerability

Report privately through GitHub, at
[Security -> Report a vulnerability](https://github.com/tonygwu/llm-quota-router/security/advisories/new).
Private reporting is enabled on this repository. Please do not open a public
issue for anything that touches credentials.

Expect a first response within seven days. This is a personal project with one
maintainer, so that is a best effort and not a guarantee.

## Why this project needs a policy at all

The router reads OAuth **access tokens** out of the macOS Keychain and spawns
vendor CLIs with them in scope. A defect here does not corrupt data. It can log
an operator out of an account, or expose a token. Both have happened on this
project once already, which is why the boundary below is enforced by tests
rather than by intent.

## The security properties this project claims

Items 1 to 3 are each asserted by a test. If you can defeat one, that is a
vulnerability even when nothing crashes. Item 4 is a design property of the
architecture and has no single test.

1. **It never redeems a refresh token.** Anthropic rotates refresh tokens, so
   redeeming one revokes the copy the vendor CLI holds. Exactly one process on
   a machine may do that, and it is the vendor's own CLI. This package is a
   pure reader. Guard: `tests/test_no_token_rotation.py`.
2. **It never proxies.** `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` are
   never emitted into a child environment. Guard: `tests/test_portability.py`,
   which checks the AST rather than grepping text.
3. **It never writes to a credential store.** Keychain access is limited to
   `security find-generic-password`. Any `add-`, `delete-` or `set-generic-password`
   call is a defect. Guard: `tests/test_adapters.py`.
4. **It moves no quota between people.** It selects among accounts the operator
   is already authorized to use, and stops when they are exhausted. Credentials
   are never shared, forwarded, or exposed to another party. This follows from
   properties 1 to 3 rather than from a test of its own.

A report showing that any of these four does not hold in some code path is in
scope, and is the most valuable kind of report this project can receive.

## Out of scope

- The vendor CLIs themselves, and the vendor usage endpoint. Report those to
  the vendor.
- Anything that requires an attacker to already have read access to the
  operator's Keychain or home directory. At that point the tokens are already
  theirs, and this tool changes nothing.
- Running the tool on an untrusted config file. `~/.config/quota-router/config.toml`
  is treated as trusted operator input, exactly like a shell profile.
- Missing hardening on a machine with a single account. The threat model is one
  operator's own laptop.

## Supported versions

`main` only. There are no release branches and no backports.
