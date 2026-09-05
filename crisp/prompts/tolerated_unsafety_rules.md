Every change must pass the checker before review. The current checker rejects per-function metric increases; review cannot grant an exception for relocating or consolidating unsafe operations. If checker warnings are present, assess their soundness under the rules below as an additional gate.

Treat these patterns as laundering and reject them:
- a function turned into a general-purpose oracle handing out references or slices derived from raw pointers (e.g. `fn as_ref<'a>(p: *mut T) -> &'a mut T`);
- forged slice lengths or lifetimes (e.g. `slice::from_raw_parts` with an unverified length, or invented `'static` lifetimes);
- an `unsafe` qualifier removed, or a foreign declaration made safe, without its preconditions actually being discharged.
