# Concord terms hashing v1 specification

This defines the byte encoding used by the v1 terms-hash contract. The
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

V1 distinguishes an integer token from a decimal/exponent token.
An implementation must preserve that distinction when parsing terms for hashing.
In particular, JSON numbers `1` and `1.0` have different v1 canonical bytes.

Select the procedure from the original JSON token:

| Token syntax | Procedure |
| --- | --- |
| No decimal point or exponent | [Integer tokens](#integer-tokens). |
| A decimal point or exponent | [Decimal and exponent tokens](#decimal-and-exponent-tokens). |

### Integer tokens

1. Emit the exact base-10 integer value.
2. Omit leading zeros and a plus sign.
3. Emit integer `-0` as `0`.

Do not round large integers through a binary64 representation.

### Decimal and exponent tokens

1. Convert the token to a finite IEEE 754 binary64 value with round-to-nearest, ties-to-even.
2. Select the shortest decimal significand that converts back to that binary64 value.
3. If alternatives have the same length, select the decimal nearest to the binary64 value.
4. If those distances are equal, select the decimal with an even last digit.
5. Select the notation from the table below.

| Value | Notation |
| --- | --- |
| Nonzero, with a normalized decimal exponent less than -4 or at least 16 | Scientific notation. |
| All other values, including zero | Fixed notation. |

For scientific notation, apply these rules:

- Use one leading significand digit.
- Omit redundant fractional zeros.
- Use lowercase `e`.
- Include the exponent sign.
- Use at least two exponent digits.

For fixed notation, retain `.0` when the binary64 value is an integer.
Preserve negative binary64 zero as `-0.0`.

| Input token | Canonical number |
| --- | --- |
| `1.0` | `1.0` |
| `1e0` | `1.0` |
| `0.0001` | `0.0001` |
| `0.00001` | `1e-05` |
| `1000000000000000.0` | `1000000000000000.0` |
| `10000000000000000.0` | `1e+16` |
| `-0.0` | `-0.0` |

### Compatibility

These rules differ from RFC 8785/JCS and ordinary JavaScript `JSON.stringify`.
A consumer must use a number-preserving decoder and a matching encoder for
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
