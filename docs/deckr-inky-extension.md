# Inky priority output extension

This optional hardware capability has family `io.github.kws.inky.output.priority`,
type `bitmap`, direction `output`, and access `settable`. Its commands are
`set_frame` and `clear`. Their parameters have the same structure as the standard
`dev.deckr.output.raster` bitmap commands. See the
[parameter schema](../contract/v1/schemas/deckr/extensions/inky-priority-command-params.v1.schema.json).
The capability identifier is local to the device descriptor.

The standard raster capability requests normal refresh timing. The priority
capability requests an earlier refresh. A priority request does not guarantee
immediate output. It does not change the panel waveform, voltage, BUSY checks,
or hardware fault handling. It cannot bypass the configured minimum interval.

Both capabilities on one screen use one refresh worker and one pending frame.
A new valid request replaces the pending request, including its priority.
An update that has started can finish. Compare converted native pixels with the
last successful frame and omit an identical update. Apply the same gate to clear.
Command acceptance means that the request entered the queue. It does not mean
that the physical update completed.

Use monotonic time for cooldowns. Measure each interval from the end of the
previous hardware attempt. A priority budget limits updates that start before
the normal deadline. Exhausted priority requests use the normal deadline.
Restart must not reset an interval or restore a spent priority allowance early.
An implementation can use conservative startup intervals instead of persistent
timestamps. A hardware error stops further attempts until recovery.

Concord remains the only authority for output. Retain the original command
context and check it again before panel I/O and before the display trigger.
Remove pending work when its claim ends. Do not replay it after restart or a
successor contract. A triggered update can finish its BUSY wait and enter sleep
after authority ends. Do not clear the retained image on claim loss.

The initial Rust implementation uses normal intervals of at least 300 seconds,
priority intervals of at least 60 seconds, and at most one shortened update per
900 seconds. These are deployment policy values. They are not manufacturer
guarantees about panel life. Exact panel selection and electrical timing require
hardware validation. See the [Rust hardware manager](https://github.com/kws/deckr-driver-inky-rust).
