# Secure-partition logging and Linux observability

Static findings below refer to SoC 2.155.11 capsule SHA-256
`0985b848b1708421a399935b7f9b1afb5469588db6f26bd2953c6013f3db70ff`,
with file offset `0xa08f0f` mapped to VA `0x93942000`. Capsules and
disassembly remain outside Git. No logging RPC, raw SMC, secure-memory read,
or firmware-level change was attempted.

## The ring exists; the proposed export cluster is a different object

Character sink `0x93945e08` writes to the **inline array** at `0x9398a018`
using the index stored at `0x9398a010`, then wraps that index with `0x1fff`.
That establishes an 8192-byte ring. The ring address is not loaded as a
buffer pointer. Its live contents and write index are unknown. Even if an
export is later found, a small wrapping buffer cannot be assumed to retain
the original incident's messages.

The proposed export cluster at `0x93943ba8–0x93943ddc` instead works on a
linked-list head at **`0x9398a000`**. Entries contain a type, a 32-byte key,
another selector, a payload length, a next pointer at `+0x30`, and payload
at `+0x38`. The code creates, searches, enumerates and frees those entries.
It does not use the ring's index or array.

Callers identify this as the configuration store:

| Evidence | Interpretation |
| --- | --- |
| `0x93943f30` adds a record; failure log at `0x93943f58` refers to adding a device region to the config store | Configuration registration |
| `0x9396f2d8` / `0x9396f330` count types 1/0, limited to 3 memory / 6 device regions | Boot-region inventory |
| `0x9396f3a8` / `0x9396f408` call `0x93943dc4`; failure labels identify memory/device region queries | Configuration extraction during `sp_config_load` |

Consequently, following that cluster to callers does not establish a logger
RPC endpoint. The adjacent `0x93945bd4–0x93945c40` region is also not a log
reader: it checks a state word at `0x9398a008` and handles random-number
generation fallback, with a TRNG failure diagnostic.

A scan of direct page-plus-offset references in the disassembled image found
the ring/index accesses in the character sink, not a reader. This is bounded
evidence, not a proof excluding indirect references or an external privileged
reader. No host-accessible ring-export operation has been verified.

## Level gate and console output

Logger `0x93946a78` obtains a runtime level from `0x93945c44`, and formats
only when that value is nonzero and at least the requested level. The getter
can query monitor function `0xc2000032` and use the value at `0x9398c078`;
the live level is not established by the capsule. The presence of warning
format strings therefore does not prove the messages reached the ring.

The character sink also calls `0x93946ca8`. If its temporary output-buffer
pointer at `0x9398c018` is null, output reaches the line/chunk collector at
`0x93946b64`, then `0x93956ec4`, which issues **FF-A console-log call
`0xc400008a`**. With a nonnull pointer, it writes into that temporary buffer.
The flag at `0x9398c030` controls buffer flushing/reset; it is not sufficient
evidence that all production console output is disabled.

Where the secure monitor routes this console stream, and whether the
platform exposes it to Linux, remain open. A secure partition's console
call is not a Linux FF-A driver call, so a Linux function trace cannot observe
that stream merely by tracing the normal-world send functions.

## What the new tools actually expose

- `dgx-power-control --debug ...` records the command's existing hwmon
  attribute operations and errors. It does not obtain private firmware logs.
- The [passive FF-A collector](../diagnostics/ffa-trace/README.md) records
  existing Linux call boundaries, available arguments, timing and raw return
  registers. It submits no EC/FF-A requests and uses no arbitrary memory probe.
- Actual eSPI packet capture would need a verified controller trace facility,
  firmware instrumentation, or an external bus analyzer. None is supplied by
  these tools.

The image gives instructions and initialized data, not a snapshot of every
private RAM object. Runtime-allocated and zero-initialized regions require
separate layout/dataflow reconstruction, and their fault-time values cannot
be inferred from their initial state. Static state maps remain useful, but
the claimed logger export and live private-RAM access are still unproven.
