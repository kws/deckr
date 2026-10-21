# Deckr KSI Invariant rendering extension v1

This optional extension uses the format ID
`com.k-si.deckr.render.invariant.v1`. The owner is `com.k-si`.
It does not change the two baseline Deckr render formats.
A controller advertises this exact ID only when its local policy permits graph
execution and HTTP image downloads. The format has no options in this revision.

Use the [content schema](../contract/v1/schemas/deckr/extensions/ksi-invariant-content.v1.schema.json)
and the reusable [graph schema](../contract/v1/schemas/deckr/extensions/ksi-invariant-graph.v1.schema.json).
The [extension catalog](../contract/v1/catalogs/render-extensions.v1.json)
records these entry points. The [semantic vectors](../contract/v1/vectors/ksi-invariant-rendering.v1.json)
provide execution cases.

## Message and authority

Send the content in an Action Runtime `binding_render` command over the managed
service boundary. Select a target and format from that binding's `rendering`
support. Use the controller's current binding generation. A graph does not
advance this generation. Do not send a graph or URL as a hardware `set_frame`.

Apply all [rendering admission, ordering and result rules](deckr-action-runtime-rendering.md).
Check authority and target identity again before dispatch. Cancel work when its
binding or service-use contract becomes invalid. Do not publish output from a
cancelled job under a successor contract.

## Graph document

The data object has exactly `format`, `version`, `output` and `graph`.
The format is `invariant-graph`; its version is the integer `1`.
The graph has between 1 and 64 named nodes. The output names one of these nodes.
Each node has exactly `kind`, `op_name`, `params` and `deps`.
Only `kind: node` is permitted. Subgraphs and switch nodes are prohibited.
Providers flatten reusable recipes before transmission.

Dependencies name graph nodes or the reserved input `canvas`. The graph cannot
redefine `canvas`. Dependencies are unique and cannot contain cycles.
A dependency path can contain at most 16 graph nodes. The controller supplies
`canvas.width` and `canvas.height` from the logical rendering target. No other
controller state is available to the graph.

Parameters contain JSON values. A `{"$ref": "name"}` refers to a declared
node dependency. A `{"$tuple": [...]}` represents a tuple. Other dollar-prefixed
tags are prohibited. Nested values have a maximum depth of 16. Each collection
has at most 64 entries. Numeric literals are finite and have an absolute value
of at most 4096. Each string has at most the larger of 2048 and the target's
`maxTextCodePoints` characters. Rendered text must also satisfy
`maxTextCodePoints`.

An expression occupies the whole string, between `${` and `}`. Expressions
permit numeric literals, dependency names, the fields `width`, `height` and
`size`, parentheses, unary signs, addition, subtraction, multiplication,
division, and the single-argument `decimal` conversion. Other functions,
operators, field traversal, interpolation and imports are prohibited.
Expressions cannot add dependencies. Check computed parameters before the
operation allocates an image.

## Operations and image output

| Operation | Purpose and restrictions |
| --- | --- |
| `gfx:create_solid` | Create an image of an explicit size and color. |
| `gfx:resolve_resource` | Resolve a packaged icon named `lucide:` followed by lowercase letters, digits or hyphens. No paths or URLs. |
| `gfx:render_svg` | Render a resolved icon. Its SVG input must reference a `gfx:resolve_resource` node. Both dimensions are explicit. |
| `gfx:colorize` | Apply a color to an image. |
| `gfx:render_text` | Render text with the packaged Inter font. |
| `gfx:packed_text` | Fit text into an explicit area with the packaged Inter font. |
| `gfx:composite` | Compose image layers with explicit anchors. |
| `gfx:resize` | Resize an image with explicit width and height. A scale parameter is prohibited. |
| `gfx:blob_to_image` | Decode a PNG or JPEG blob after byte and pixel checks. |
| `stdlib:make_dict` | Construct parameter data from declared inputs. |
| `deckr:fetch_image_url` | Download an image with the policy below. |

These operations use the Invariant graph v1 and Invariant GFX parameter shapes.
The owner maintains these dependencies. The controller uses an explicit registry
of these operations. A provider cannot add an operation, load code, or select
another registry.

Every image dimension is between 1 and 1024 pixels. Each image must satisfy the
target's `maxImagePixels`. Explicit font size is between 1 and 128.
Widths, heights, fit widths and maximum font sizes cannot exceed 1024.
Layer offsets have an absolute value of at most 1024. The sum of intermediate
image areas cannot exceed 16,777,216 pixels. These bounds limit the image memory retained by each job. Also limit the
number of jobs admitted at one time.

The output must be an image. Fit it inside the target canvas without changing
its aspect ratio. Center it and composite transparency over black. The logical
output is an opaque image at the target dimensions. Hardware encoding and
orientation remain controller and hardware manager responsibilities.

There is no persistent graph cache in this format. Execute jobs in an isolated
worker that the controller can terminate at the render deadline. Limit the
worker's operation registry, inputs and resource use. Cancellation or timeout
must prevent later output dispatch.

## Image download policy

Permit only absolute `http` and `https` URLs with a host. Reject user information
and fragments. Do not follow redirects. Do not use controller credentials,
cookies, environment proxy settings or a persistent download cache.
Each job obtains a new response. Do not accept a cached response from a previous
job as current content.

Local policy can permit private network hosts, including a local Sonos service.
The controller must not advertise this extension when its policy prohibits the
network access required by these operations. A narrower host policy can reject
a request. A URL never grants access to controller credentials or local files.

Accept successful image responses with media type `image/png` or `image/jpeg`.
Reject redirects, other media types, invalid image data and encoding mismatches.
Check decoded file bytes against `maxImageBytes`, then image dimensions and
`maxImagePixels` before image allocation. Each network operation has a timeout
of at most one second and cannot extend the total render deadline.

## Failure results

Use `invalid_content` for invalid graphs, expressions, image data or operations.
Use `limit_exceeded` for graph depth, parameter, image or allocation limits.
Use `deadline_exceeded` when the render deadline expires.
Other execution or network failures use `render_failed`. The normal controller
render result carries the original request correlation and binding generation.
The extension adds no new status or result code.
