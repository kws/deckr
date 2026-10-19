# Concord v1 canonical JSON

This defines the byte encoding used by the existing v1 terms-hash contract.
It preserves the extraction baseline's encoding; it does not switch existing
agreements to a different hashing algorithm. The
[acceptance vectors](../contract/v1/vectors/concord-terms-hash.v1.json) supply
input JSON text, canonical text and expected SHA-256 digests.

## Input and ordering

Accept UTF-8 JSON containing Unicode scalar strings, unique object keys and
finite numbers. Reject duplicate keys, unpaired surrogates, NaN and infinities.
Do not normalize Unicode. Sort object keys recursively by Unicode scalar value,
not locale collation or UTF-16 code-unit order. Preserve array order.

Separate entries with `,` and object keys/values with `:`. Do not emit whitespace
outside strings. Encode booleans and null as `true`, `false`, `null`.

## Strings

Use double quotes. Escape quote and backslash. Use the short escapes `\b`, `\f`,
`\n`, `\r`, `\t` for those five control characters; use lowercase `\u00xx`
for other characters from U+0000 through U+001F. Emit all other Unicode scalar
characters as UTF-8, without ASCII-only escaping. Do not escape `/`.

## Numbers

V1 retains the distinction between an integer token and a decimal/exponent token.
An implementation must preserve that distinction when parsing terms for hashing.
In particular, JSON numbers `1` and `1.0` have different v1 canonical bytes.

- An integer token has no decimal point or exponent. Emit its exact base-10
  integer value, without leading zeros or a plus sign; integer `-0` becomes `0`.
  Do not round large integers through a binary64 representation.
- A token containing a decimal point or exponent is interpreted as a finite
  IEEE 754 binary64 value using round-to-nearest, ties-to-even.
- Emit its shortest round-tripping decimal significand. Choose the nearest
  decimal where alternatives have the same length; use an even last digit on a tie.
- For a nonzero value whose normalized decimal exponent is less than -4 or at
  least 16, use scientific notation: one digit before the point, no redundant
  fractional zeros, lowercase `e`, an explicit exponent sign and at least two
  exponent digits. Otherwise use fixed notation, retaining `.0` for an integral
  binary64 value. Preserve negative binary64 zero as `-0.0`.

Examples include `1.0`, `0.0001`, `1e-05`, `1000000000000000.0`, `1e+16`,
and `-0.0`. The input text `1e0` therefore normalizes to `1.0`.

These rules preserve the former implementation's integer/float JSON encoding.
They are not RFC 8785/JCS or ordinary JavaScript `JSON.stringify` rules. A consumer
must use a number-preserving decoder and a matching encoder when it supports
generic numeric terms. Profiles can use integer-only or string-encoded decimal
terms to avoid unnecessary binary64 conversion. Adopting another canonicalization
would require an explicit protocol revision.

## Digest

Compute SHA-256 over the canonical UTF-8 bytes, without a byte-order mark or
trailing newline. The field value is `sha256:` followed by 64 lowercase hexadecimal
digits. The file formatting of a stored JSON record is not the hash input: only
the canonical encoding of its `terms` object is used.

The vectors preserve raw `inputJson` specifically so a parser cannot accidentally
erase numeric distinctions before a test starts. Implementations must compare
both canonical bytes and digest, including non-BMP key ordering and numeric cases.
