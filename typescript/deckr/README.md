# @deckr/core

TypeScript implementation of Deckr's language-neutral core contracts.

This package lives inside the `deckr` repository so Python, Rust, and
TypeScript implementations can share the same checked `contract/v1` schemas,
fixtures, and vectors while Python remains the current artifact authoring
implementation.

```bash
npm test
npm run typecheck
```

The pure contract and lifecycle modules do not require a NATS client. The
`nats` peer dependency is only needed when using `@deckr/core/nats`.
